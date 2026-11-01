"""F04, F06, F09, F10: paragraph addressing, destructive intent, single-use definitions and controls.

Snapshots under tests/fixtures/native_repairs are HWPML2X captured from real Hancom on the unchanged baseline for a
disposable synthetic document. Replaying them runs real comparisons; it is not a new native run.
"""
from __future__ import annotations

from pathlib import Path
import unittest

from app import native_numbering as nn, native_numbering_census as census, native_paragraph_edit as pe
from tests.native_numbering_fakes import FakeCtrl
from tests.test_native_numbering_census import _HwpmlHwp
from tests.test_native_paragraph_edit import Flow, _doc

ROOT = Path(__file__).parent / 'fixtures' / 'native_repairs'
INSERT = {'op': 'paragraph_insert_exact', 'expected_page': 1, 'confirm_layout': True}
REMOVE = {'op': 'paragraph_remove_exact', 'expected_page': 1, 'confirm_remove': True}


def snapshot(name):
    return (ROOT / f'{name}.hwpml2x').read_text(encoding='utf-8')


class Replay(_HwpmlHwp):
    """Serves the captured before-snapshot until the native edit, then the captured after-snapshot."""

    def __init__(self, before, after):
        parsed = census.parse_hwpml2x_census(before)
        heading = {'NONE': 0, 'OUTLINE': 1, 'NUMBER': 2, 'BULLET': 3}
        body = [p for p in parsed['paragraphs'] if p['body_index'] is not None]
        super().__init__(paragraphs=[{'text': p['text'], 'heading_type': heading[p['heading']], 'level': p['level']} for p in body],
                         controls=[FakeCtrl('secd'), FakeCtrl('cold'), FakeCtrl('head'), FakeCtrl('foot'), FakeCtrl('tbl'), FakeCtrl('gso'), FakeCtrl('tbl')])
        self.hwpml_text = before
        self.after = after
        self.selection = None
        execute, run = self.HAction.Execute, self.HAction.Run

        def replay_execute(name, hset):
            result = execute(name, hset)
            self.hwpml_text = self.after
            return result

        def replay_run(name):
            if name == 'Delete':
                self.calls.append(('Run', 'Delete'))
                self.hwpml_text = self.after
                return True
            return run(name)

        self.HAction.Execute, self.HAction.Run = replay_execute, replay_run

    def select_text(self, spara, spos, epara, epos, list_id=0):
        self.selection = (list_id, spara, spos, list_id, epara, epos)
        return True

    def get_selected_pos(self):
        if self.selection is not None:
            return (True, *self.selection)
        if self.selection_para is None:
            return (False, None, None, None, None, None, None)
        return super().get_selected_pos()


class SingleUseDefinitions(unittest.TestCase):
    """F06, observed natively: Hancom drops a character or paragraph shape when its only user goes away."""

    def number(self, before, after, find, level):
        hwp = Replay(before, after)
        return nn.apply_official_numbering_at_match(hwp, find=find, level=level, protection=census.live_protection_reader(hwp))

    def test_level_change_of_the_only_paragraph_at_that_level(self):
        result = self.number(snapshot('level_change_before'), snapshot('level_change_after'), '항목을 둘째', 3)
        self.assertEqual(result['status'], 'level_changed')

    def test_numbering_a_paragraph_with_its_own_paragraph_shape(self):
        result = self.number(snapshot('unique_parashape_before'), snapshot('unique_parashape_after'), '단독문단모양센티널', 1)
        self.assertEqual(result['status'], 'applied')

    def test_text_changed_elsewhere_during_a_level_change_stays_unknown(self):
        changed = snapshot('level_change_after').replace('항목병 셋째 수준', '항목병 다른 글', 1)
        with self.assertRaises(nn.NativeNumberingMutationError):
            self.number(snapshot('level_change_before'), changed, '항목을 둘째', 3)

    def test_the_rule_requires_that_nothing_still_uses_the_dropped_shape(self):
        before = nn._validated_census(census.parse_hwpml2x_census(snapshot('unique_parashape_before')))
        after = nn._validated_census(census.parse_hwpml2x_census(snapshot('unique_parashape_after')))
        index = next(i for i, p in enumerate(before['paragraphs']) if p['text'].startswith('단독문단모양센티널'))
        target = before['paragraphs'][index]
        self.assertEqual(nn._official_definition_problems(before['definitions'], after['definitions'], define=False, target=target, remaining=after['paragraphs']), [])
        still_used = [*after['paragraphs'], {'parashape_sha256': target['parashape_sha256']}]
        self.assertTrue(nn._official_definition_problems(before['definitions'], after['definitions'], define=False, target=target, remaining=still_used))

    def test_losing_an_unrelated_paragraph_shape_stays_unknown(self):
        before = census.parse_hwpml2x_census(snapshot('unique_parashape_before'))
        after = census.parse_hwpml2x_census(snapshot('unique_parashape_after'))
        wrong_target = next(index for index, p in enumerate(before['paragraphs']) if p['text'].startswith('서식센티널'))
        problems = nn._official_definition_problems(
            nn._validated_definitions(before['definitions']), nn._validated_definitions(after['definitions']),
            define=False, target=before['paragraphs'][wrong_target], remaining=after['paragraphs'],
        )
        self.assertTrue(problems)

    def test_removing_the_only_paragraph_with_a_character_shape(self):
        hwp = Replay(snapshot('unique_charshape_before'), snapshot('unique_charshape_after'))
        text = '단독글자모양센티널-파랑 12'
        result = pe.remove_paragraph(hwp, match='단독글자모양센티널', expected_text=text, protection=census.live_protection_reader(hwp))
        self.assertEqual(result['status'], 'removed')

    def test_a_character_shape_lost_beside_the_removed_paragraph_stays_unknown(self):
        after = snapshot('unique_charshape_after').replace('Height="1400"', 'Height="1500"')
        hwp = Replay(snapshot('unique_charshape_before'), after)
        with self.assertRaises(nn.NativeNumberingMutationError):
            pe.remove_paragraph(hwp, match='단독글자모양센티널', expected_text='단독글자모양센티널-파랑 12', protection=census.live_protection_reader(hwp))


class NestedParagraphs(unittest.TestCase):
    """F04: a body paragraph's table cells are listed after it; the new paragraph comes after those."""

    def test_real_snapshot_of_an_insert_after_a_paragraph_with_a_table(self):
        hwp = Replay(snapshot('nested_insert_before'), snapshot('nested_insert_after'))
        hwp.HParameterSet.HInsertText = type('S', (), {'HSet': None, 'Text': ''})()
        hwp.HParameterSet.HInsertText.HSet = hwp.HParameterSet.HInsertText
        default = hwp.HAction.GetDefault
        hwp.HAction.GetDefault = lambda name, hset: True if name == 'InsertText' else default(name, hset)
        hwp.MoveParaEnd = lambda: None
        result = pe.insert_paragraph(hwp, match='표포함센티널', text='표문단뒤 새 문단', position='after', protection=census.live_protection_reader(hwp))
        self.assertEqual(result['status'], 'inserted')

    def test_through_the_real_service(self):
        hwp = _doc('Title', 'Item', 'Tail')
        result = Flow(hwp).step(match='Title', text='After the table owner', position='after', **INSERT)
        self.assertTrue(result['ok'], result)
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'After the table owner', 'Item', 'Tail'])

    def test_the_new_paragraph_is_found_by_body_position_not_list_position(self):
        hwp = _doc('Title', 'Item', 'Tail')
        hwp.corrupt_neighbor = True
        result = Flow(hwp).step(match='Item', text='Next', position='after', **INSERT)
        self.assertFalse(result['ok'])
        self.assertIs(result['steps'][0]['mutation_may_have_persisted'], True)


class ExactDestructiveIntent(unittest.TestCase):
    """F09: a unique phrase locates a paragraph; only its full text authorizes removing it."""

    LONG = 'A long paragraph holding a unique phrase in the middle of it'

    def test_a_unique_phrase_alone_never_removes_the_paragraph(self):
        hwp = _doc('Title', self.LONG, 'Tail')
        try:
            result = Flow(hwp).step(match='unique phrase', **REMOVE)
        except Exception:
            result = None
        self.assertIn(self.LONG, [p['text'] for p in hwp.paragraphs])
        self.assertNotIn(('Run', 'Delete'), hwp.calls)
        if result is not None:
            self.assertFalse(result['ok'])

    def test_a_different_full_text_is_refused_before_any_edit(self):
        hwp = _doc('Title', self.LONG, 'Tail')
        result = Flow(hwp).step(match='unique phrase', expected_text='What the caller saw earlier', **REMOVE)
        self.assertFalse(result['ok'])
        self.assertEqual(result['steps'][0].get('refusal_code'), 'target_text_mismatch')
        self.assertIs(result['steps'][0]['mutation_may_have_persisted'], False)
        self.assertNotIn(('Run', 'Delete'), hwp.calls)

    def test_the_exact_full_text_removes_it(self):
        hwp = _doc('Title', self.LONG, 'Tail')
        result = Flow(hwp).step(match='unique phrase', expected_text=self.LONG, **REMOVE)
        self.assertTrue(result['ok'], result)
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Tail'])


class ControlPreservation(unittest.TestCase):
    """F10: page-number elements are stripped from paragraph content digests, so controls are compared separately."""

    def after_edit(self, hwp, name, change):
        execute, run = hwp.HAction.Execute, hwp.HAction.Run

        def changed_execute(action, hset):
            result = execute(action, hset)
            if action == name:
                change()
            return result

        def changed_run(action):
            result = run(action)
            if action == name:
                change()
            return result

        hwp.HAction.Execute, hwp.HAction.Run = changed_execute, changed_run

    def page_doc(self):
        hwp = _doc('Title', 'Item', 'Tail')
        hwp.controls.append(FakeCtrl('pgnp'))
        return hwp

    def drop_control(self, hwp):
        hwp.controls[:] = [c for c in hwp.controls if c.CtrlID != 'pgnp']

    def test_insert_losing_a_page_number_control_is_unknown(self):
        hwp = self.page_doc()
        self.after_edit(hwp, 'InsertText', lambda: self.drop_control(hwp))
        result = Flow(hwp).step(match='Item', text='Next', position='after', **INSERT)
        self.assertFalse(result['ok'])
        self.assertIs(result['steps'][0]['mutation_may_have_persisted'], True)

    def test_remove_losing_a_page_number_control_is_unknown(self):
        hwp = self.page_doc()
        self.after_edit(hwp, 'Delete', lambda: self.drop_control(hwp))
        result = Flow(hwp).step(match='Item', expected_text='Item', **REMOVE)
        self.assertFalse(result['ok'])
        self.assertIs(result['steps'][0]['mutation_may_have_persisted'], True)

    def test_insert_losing_only_the_page_number_element_in_another_paragraph_is_unknown(self):
        hwp = _doc('Title', 'Item', 'Tail')
        hwp.hwpml_extras['pagenum'] = True
        self.after_edit(hwp, 'InsertText', lambda: hwp.hwpml_extras.update(pagenum=False))
        result = Flow(hwp).step(match='Item', text='Next', position='after', **INSERT)
        self.assertFalse(result['ok'])
        self.assertIs(result['steps'][0]['mutation_may_have_persisted'], True)

    def test_unchanged_controls_still_succeed(self):
        hwp = self.page_doc()
        self.assertTrue(Flow(hwp).step(match='Item', text='Next', position='after', **INSERT)['ok'])


if __name__ == '__main__':
    unittest.main()
