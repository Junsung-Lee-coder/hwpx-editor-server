"""Native13 first-level regression: a native-assigned InstId on the numbered target paragraph.

The actual Native13 raw1 -> raw2 HWPML2X snapshots differ only by one appended outline PARASHAPE, the target paragraph's
ParaShape reference and a new InstId on that paragraph. These tests drive the public apply_official_numbering_at_match with a
fake caller whose get_text_file returns such before/after text. The synthetic class builds its documents in memory; the
actual-snapshot class reads the two hash-pinned native snapshots of a generated test document bundled under
tests/fixtures/native_numbering/native13.
Fakes prove call shapes and refusal behaviour of this source only; they are not Hancom evidence.
"""
from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path
from typing import Any, Callable

from app import native_numbering as nn
from app.native_numbering_census import live_protection_reader
from tests.native_numbering_fakes import FakeCtrl, FakeHwp, OFFICIAL_SHAPE_LEVELS, STOCK_TAIL_LEVELS, shape_items
from tests.test_native_numbering_census import _HwpmlHwp

TEXTS = ('Title', 'Item text', 'Tail')
TARGET = 'Item text'
FIRST_ID = '3121190098'
NEW_ID = '153176620'
LEVEL_ONE_ACTION = ['Execute:ParagraphShape(HeadingType=1,Level=0)']
FOREIGN = (('Ⅰ.', 2), ('(^2)', 3), ('^3>', 4), ('[^4]', 1))
FOREIGN_TAIL = {
    f'{prefix}Level{index}': value
    for index, (text, number_format) in enumerate(STOCK_TAIL_LEVELS, start=len(OFFICIAL_SHAPE_LEVELS))
    for prefix, value in (('StrFormat', text), ('NumFormat', number_format))
}
_PARAGRAPH = re.compile(r'<P (ParaShape="\d+" Style="0")>(.*?)</P>', re.DOTALL)


def with_inst_ids(text: str, ids: dict[str, str | None]) -> str:
    def put(match: re.Match[str]) -> str:
        last = re.findall(r'<CHAR>(.*?)</CHAR>', match.group(2))[-1]
        value = ids.get(last)
        head = f'InstId="{value}" ' if value is not None else ''
        return f'<P {head}{match.group(1)}>{match.group(2)}</P>'

    return _PARAGRAPH.sub(put, text)


class _NativeIdsHwp(_HwpmlHwp):
    """Synthetic HWPML2X plus InstId values; `assign` and `after` apply once the named native action ran."""

    def __init__(
        self,
        *,
        before_ids: dict[str, str | None] | None = None,
        assign: dict[str, str | None] | None = None,
        after: Callable[[str], str] | None = None,
        after_action: str = 'ParagraphShape',
        target_heading: tuple[int, int] = (0, 0),
        foreign: bool = False,
    ) -> None:
        paragraphs = [{'text': t, 'heading_type': 0, 'level': 0, 'section': 0} for t in TEXTS]
        paragraphs[1]['heading_type'], paragraphs[1]['level'] = target_heading
        super().__init__(paragraphs=paragraphs, controls=[FakeCtrl('secd')])
        if foreign:
            self.outline_shape = {**shape_items(FOREIGN), **FOREIGN_TAIL}
        self.before_ids = {'Title': FIRST_ID, **(before_ids or {})}
        self.assign = dict(assign or {})
        self.after = after
        self.after_action = after_action

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> Any:
        text = super().get_text_file(fmt, option)
        if fmt != 'HWPML2X':
            return text
        ids = dict(self.before_ids)
        if any(call[0] == 'Execute' and call[1] == self.after_action for call in self.calls):
            ids.update(self.assign)
            text = with_inst_ids(text, ids)
            return self.after(text) if self.after is not None else text
        return with_inst_ids(text, ids)


def run_level(hwp: FakeHwp, level: int = 1) -> dict[str, Any]:
    return nn.apply_official_numbering_at_match(hwp, find=TARGET, level=level, protection=live_protection_reader(hwp))


def mutating(hwp: FakeHwp) -> list[tuple[Any, ...]]:
    return [call for call in hwp.calls if call[0] in {'Execute', 'Run'}]


class SyntheticTargetInstIdTests(unittest.TestCase):
    def assert_unknown_after_one_write(self, hwp: _NativeIdsHwp) -> None:
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            run_level(hwp)
        self.assertIn('outline numbering outcome is not as requested', str(ctx.exception))
        self.assertEqual(ctx.exception.mutation_state, 'unknown')
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
        self.assertEqual(len(mutating(hwp)), 1)
        self.assertEqual([call for call in hwp.calls if call[0] in {'Run', 'Undo', 'Close', 'Discard', 'save', 'save_as'}], [])

    def assert_transform_changes_the_after_text(self, change: Callable[[str], str]) -> None:
        hwp = _NativeIdsHwp(assign={TARGET: NEW_ID})
        hwp.calls.append(('Execute', 'ParagraphShape', {}))
        after_text = hwp.get_text_file('HWPML2X', '')
        self.assertIn(f'InstId="{NEW_ID}"', after_text)
        self.assertNotEqual(change(after_text), after_text)

    def test_a_new_native_instid_on_the_numbered_target_is_accepted_as_the_only_target_change(self) -> None:
        hwp = _NativeIdsHwp(assign={TARGET: NEW_ID})
        result = run_level(hwp)
        self.assertEqual(result['status'], 'applied')
        self.assertTrue(result['changed'])
        self.assertEqual(result['native_actions'], LEVEL_ONE_ACTION)
        executes = [call[1] for call in hwp.calls if call[0] == 'Execute']
        self.assertEqual(executes, ['ParagraphShape'])
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertTrue(result['protection']['checked'])
        self.assertIn('paragraphs', result['protection']['compared'])

    def test_a_target_without_any_new_instid_still_succeeds(self) -> None:
        result = run_level(_NativeIdsHwp())
        self.assertEqual(result['native_actions'], LEVEL_ONE_ACTION)

    def test_every_level_accepts_the_new_instid_with_one_paragraph_shape_action(self) -> None:
        for level in (1, 2, 3, 4):
            with self.subTest(level):
                hwp = _NativeIdsHwp(assign={TARGET: NEW_ID})
                result = run_level(hwp, level)
                self.assertEqual(result['native_actions'], [f'Execute:ParagraphShape(HeadingType=1,Level={level - 1})'])
                self.assertEqual(len(mutating(hwp)), 1)

    def test_an_existing_target_identifier_is_preserved_not_replaced_or_removed(self) -> None:
        for label, assign in (('changed', {TARGET: '222'}), ('removed', {TARGET: None})):
            with self.subTest(label):
                self.assert_unknown_after_one_write(_NativeIdsHwp(before_ids={TARGET: '111'}, assign=assign))
        result = run_level(_NativeIdsHwp(before_ids={TARGET: '111'}, assign={TARGET: '111'}))
        self.assertEqual(result['native_actions'], LEVEL_ONE_ACTION)

    def test_a_new_instid_on_any_other_paragraph_is_a_change_outside_the_target(self) -> None:
        for label, assign in (
            ('only-other', {'Tail': '153176621'}),
            ('with-target', {TARGET: NEW_ID, 'Tail': '153176621'}),
            ('first-changed', {TARGET: NEW_ID, 'Title': '999'}),
            ('first-removed', {TARGET: NEW_ID, 'Title': None}),
        ):
            with self.subTest(label):
                self.assert_unknown_after_one_write(_NativeIdsHwp(assign=assign))

    def test_a_duplicate_or_foreign_identifier_is_refused(self) -> None:
        for label, hwp in (
            ('duplicates-existing', _NativeIdsHwp(assign={TARGET: FIRST_ID})),
            ('duplicates-later-paragraph', _NativeIdsHwp(before_ids={'Tail': NEW_ID}, assign={TARGET: NEW_ID})),
        ):
            with self.subTest(label):
                self.assert_unknown_after_one_write(hwp)

    def test_malformed_identifiers_are_refused(self) -> None:
        for value in ('', 'abc', '12 3', ' 12', '-5', '+5', '0153176620', '4294967296', '１２３', '1e3', '0x10', '1.5'):
            with self.subTest(repr(value)):
                hwp = _NativeIdsHwp(assign={TARGET: value})
                hwp.calls.append(('Execute', 'ParagraphShape', {}))
                self.assertIn(f'InstId="{value}"', hwp.get_text_file('HWPML2X', ''))
                self.assert_unknown_after_one_write(_NativeIdsHwp(assign={TARGET: value}))

    def test_a_new_instid_does_not_excuse_any_other_target_change(self) -> None:
        changes = (
            ('extra-attribute', lambda text: text.replace(f'<P InstId="{NEW_ID}"', f'<P Extra="1" InstId="{NEW_ID}"')),
            ('second-spelling', lambda text: text.replace(f'<P InstId="{NEW_ID}"', f'<P instid="{NEW_ID}1" InstId="{NEW_ID}"')),
            ('target-text', lambda text: text.replace('<CHAR>Item text</CHAR>', '<CHAR>Item text!</CHAR>')),
            ('target-content', lambda text: text.replace('<CHAR>Item text</CHAR>', '<CHAR>Item text</CHAR><CHAR>x</CHAR>')),
        )
        for label, change in changes:
            with self.subTest(label):
                self.assert_transform_changes_the_after_text(change)
                self.assert_unknown_after_one_write(_NativeIdsHwp(assign={TARGET: NEW_ID}, after=change))

    def test_a_new_instid_does_not_excuse_changes_outside_the_target(self) -> None:
        changes = (
            ('later-paragraph-attribute', lambda text: text.replace('<P ParaShape="0" Style="0"><TEXT CharShape="1"><CHAR>Tail', '<P Extra="1" ParaShape="0" Style="0"><TEXT CharShape="1"><CHAR>Tail')),
            ('character-shape', lambda text: text.replace('<CHARSHAPE Id="1" Height="1000"/>', '<CHARSHAPE Id="1" Height="2000"/>')),
            ('style-list', lambda text: text.replace('Name="styles-0"', 'Name="Changed"')),
            ('media', lambda text: text.replace('>bin-0<', '>changed-media<')),
        )
        for label, change in changes:
            with self.subTest(label):
                self.assert_transform_changes_the_after_text(change)
                self.assert_unknown_after_one_write(_NativeIdsHwp(assign={TARGET: NEW_ID}, after=change))

    def test_the_exception_is_bound_to_the_paragraph_shape_action(self) -> None:
        hwp = _NativeIdsHwp(
            assign={TARGET: NEW_ID}, after_action='OutlineNumber', target_heading=(1, 0), foreign=True
        )
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            run_level(hwp)
        self.assertEqual([call[1] for call in hwp.calls if call[0] == 'Execute'], ['OutlineNumber'])
        self.assertIn('outline numbering outcome is not as requested', str(ctx.exception))
        self.assertEqual(ctx.exception.mutation_state, 'unknown')


UNDO_RESTORED_ID = '2304559795'
REWRITTEN_ID = '157076148'


class UndoRestoredTargetInstIdTests(unittest.TestCase):
    """A target restored by Undo carries its identifier with bit 31 set; the next write allocates a newer one."""

    assert_unknown_after_one_write = SyntheticTargetInstIdTests.assert_unknown_after_one_write

    def test_a_write_on_an_undo_restored_identifier_accepts_one_newer_native_identifier(self) -> None:
        for before, after in ((UNDO_RESTORED_ID, REWRITTEN_ID), ('2147483648', '1')):
            with self.subTest(before=before, after=after):
                hwp = _NativeIdsHwp(before_ids={TARGET: before}, assign={TARGET: after})
                result = run_level(hwp)
                self.assertEqual(result['native_actions'], LEVEL_ONE_ACTION)
                self.assertEqual(len(mutating(hwp)), 1)

    def test_replacements_that_are_not_one_newer_fresh_identifier_refuse(self) -> None:
        for value in ('157076147', '157076146', '2304559796', None, FIRST_ID, '0157076148', '4294967295', ''):
            with self.subTest(value=value):
                self.assert_unknown_after_one_write(_NativeIdsHwp(before_ids={TARGET: UNDO_RESTORED_ID}, assign={TARGET: value}))
        self.assert_unknown_after_one_write(
            _NativeIdsHwp(before_ids={TARGET: UNDO_RESTORED_ID, 'Tail': REWRITTEN_ID}, assign={TARGET: REWRITTEN_ID}))

    def test_an_unflagged_existing_identifier_still_cannot_be_replaced(self) -> None:
        self.assert_unknown_after_one_write(_NativeIdsHwp(before_ids={TARGET: '157076147'}, assign={TARGET: REWRITTEN_ID}))

    def test_a_newer_identifier_does_not_excuse_other_changes(self) -> None:
        for label, change in (
            ('extra-attribute', lambda text: text.replace(f'<P InstId="{REWRITTEN_ID}"', f'<P Extra="1" InstId="{REWRITTEN_ID}"')),
            ('target-text', lambda text: text.replace('<CHAR>Item text</CHAR>', '<CHAR>Item text!</CHAR>')),
            ('other-paragraph-identifier', lambda text: text.replace(f'<P InstId="{FIRST_ID}"', '<P InstId="999"')),
            ('media', lambda text: text.replace('>bin-0<', '>changed-media<')),
        ):
            with self.subTest(label):
                self.assert_unknown_after_one_write(
                    _NativeIdsHwp(before_ids={TARGET: UNDO_RESTORED_ID}, assign={TARGET: REWRITTEN_ID}, after=change))

    def test_the_allowance_is_bound_to_the_paragraph_shape_action(self) -> None:
        hwp = _NativeIdsHwp(before_ids={TARGET: UNDO_RESTORED_ID}, assign={TARGET: REWRITTEN_ID}, after_action='OutlineNumber',
                            target_heading=(1, 0), foreign=True)
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            run_level(hwp)
        self.assertEqual([call[1] for call in hwp.calls if call[0] == 'Execute'], ['OutlineNumber'])
        self.assertEqual(ctx.exception.mutation_state, 'unknown')


OUTLINE_ID = '157355336'
RELEVELED_ID = '157355346'
RELEVEL_ACTION = ['Execute:ParagraphShape(Level=1)']


class OutlineLevelChangeInstIdTests(unittest.TestCase):
    """An outline paragraph moved to another level by the ParagraphShape write receives a newer unused identifier."""

    assert_unknown_after_one_write = SyntheticTargetInstIdTests.assert_unknown_after_one_write

    def relevel(self, **kw: Any) -> _NativeIdsHwp:
        return _NativeIdsHwp(target_heading=(1, 2), before_ids={TARGET: OUTLINE_ID, **kw.pop('before_ids', {})}, **kw)

    def test_a_level_change_on_an_outline_target_accepts_one_newer_unused_identifier(self) -> None:
        hwp = self.relevel(assign={TARGET: RELEVELED_ID})
        result = run_level(hwp, 2)
        # An outline target is moved with a Level-only ParagraphShape write.
        self.assertEqual(result['native_actions'], RELEVEL_ACTION)
        self.assertEqual(len(mutating(hwp)), 1)
        self.assertEqual([(p['heading_type'], p['level']) for p in hwp.paragraphs][1], (1, 1))

    def test_the_identifier_may_still_stay_unchanged(self) -> None:
        self.assertEqual(run_level(self.relevel(assign={TARGET: OUTLINE_ID}), 2)['native_actions'], RELEVEL_ACTION)

    def test_replacements_that_are_not_one_newer_unused_identifier_refuse(self) -> None:
        for value in ('157355335', OUTLINE_ID[:-1], str(int(RELEVELED_ID) | 0x80000000), '0' + RELEVELED_ID, '4294967295', '', None, FIRST_ID):
            with self.subTest(value=value):
                self.assert_unknown_after_one_write_at(self.relevel(assign={TARGET: value}), 2)
        self.assert_unknown_after_one_write_at(self.relevel(before_ids={'Tail': RELEVELED_ID}, assign={TARGET: RELEVELED_ID}), 2)

    def test_a_non_outline_target_holding_an_identifier_still_cannot_be_replaced(self) -> None:
        self.assert_unknown_after_one_write(_NativeIdsHwp(before_ids={TARGET: OUTLINE_ID}, assign={TARGET: RELEVELED_ID}))

    def test_a_newer_identifier_does_not_excuse_other_changes(self) -> None:
        for label, change in (
            ('extra-attribute', lambda text: text.replace(f'<P InstId="{RELEVELED_ID}"', f'<P Extra="1" InstId="{RELEVELED_ID}"')),
            ('target-text', lambda text: text.replace('<CHAR>Item text</CHAR>', '<CHAR>Item text!</CHAR>')),
            ('other-paragraph-identifier', lambda text: text.replace(f'<P InstId="{FIRST_ID}"', '<P InstId="999"')),
            ('media', lambda text: text.replace('>bin-0<', '>changed-media<')),
        ):
            with self.subTest(label):
                self.assert_unknown_after_one_write_at(self.relevel(assign={TARGET: RELEVELED_ID}, after=change), 2)

    def test_the_rule_requires_a_written_outline_level_change(self) -> None:
        def census(heading: str, level: int, ident: str) -> dict[str, Any]:
            return {'paragraphs': [{'inst_ids': [FIRST_ID]}, {'heading': heading, 'level': level, 'inst_ids': [ident]}, {'inst_ids': []}]}
        admitted = nn._target_inst_id_problems(census('OUTLINE', 2, OUTLINE_ID), census('OUTLINE', 1, RELEVELED_ID), target_index=1, target_written=True)
        self.assertEqual(admitted, [])
        for label, before, after, written in (
            ('same-level', census('OUTLINE', 1, OUTLINE_ID), census('OUTLINE', 1, RELEVELED_ID), True),
            ('not-written', census('OUTLINE', 2, OUTLINE_ID), census('OUTLINE', 1, RELEVELED_ID), False),
            ('to-non-outline', census('OUTLINE', 2, OUTLINE_ID), census('NONE', 0, RELEVELED_ID), True),
            ('from-non-outline', census('NONE', 0, OUTLINE_ID), census('OUTLINE', 1, RELEVELED_ID), True),
        ):
            with self.subTest(label):
                self.assertEqual(nn._target_inst_id_problems(before, after, target_index=1, target_written=written),
                                 ['the target paragraph identifier changed beyond one new native assignment'])

    def assert_unknown_after_one_write_at(self, hwp: _NativeIdsHwp, level: int) -> None:
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            run_level(hwp, level)
        self.assertIn('outline numbering outcome is not as requested', str(ctx.exception))
        self.assertEqual(ctx.exception.mutation_state, 'unknown')
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
        self.assertEqual(len(mutating(hwp)), 1)


RAW_DIR = Path(__file__).resolve().parent / 'fixtures' / 'native_numbering' / 'native13'
RAW_PINS = {
    'raw-1.hwpml2x': ('d89205c1910f0f0a9f289395f435464cb8a8fc11f806d418b52d7dd53298910a', 29194),
    'raw-2.hwpml2x': ('d1198ed5f39466342b3051fdea1616a414ac1bbdb2c90053655a8b69e8198e94', 29753),
}
NATIVE_TEXTS = ['N2PLAIN', 'N2L1A', 'N2L2A', 'N2L3A', 'N2L4A', 'N2L4B', 'N2L2B', 'N2OTHERNUM', 'N2L1B']


class _SnapshotHwp(FakeHwp):
    def __init__(self, before: str, after: str) -> None:
        super().__init__(
            paragraphs=[{'text': t, 'heading_type': 0, 'level': 0, 'section': 0} for t in NATIVE_TEXTS], controls=[FakeCtrl('secd')]
        )
        self.before, self.after = before, after

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> Any:
        if fmt != 'HWPML2X':
            return super().get_text_file(fmt, option)
        self.calls.append(('get_text_file', fmt, option))
        written = any(call[0] == 'Execute' and call[1] == 'ParagraphShape' for call in self.calls)
        return self.after if written else self.before


def _pinned_snapshot(name: str) -> str:
    raw = (RAW_DIR / name).read_bytes()
    if (hashlib.sha256(raw).hexdigest(), len(raw)) != RAW_PINS[name]:
        raise RuntimeError('native snapshot pin mismatch: ' + name)
    return raw.decode('utf-8')


class ActualNative13SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw1 = _pinned_snapshot('raw-1.hwpml2x')
        cls.raw2 = _pinned_snapshot('raw-2.hwpml2x')

    def run_pair(self, after: str) -> _SnapshotHwp:
        hwp = _SnapshotHwp(self.raw1, after)
        nn.apply_official_numbering_at_match(hwp, find='N2L1A', level=1, protection=live_protection_reader(hwp))
        return hwp

    def refused(self, after: str) -> None:
        hwp = _SnapshotHwp(self.raw1, after)
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_official_numbering_at_match(hwp, find='N2L1A', level=1, protection=live_protection_reader(hwp))
        self.assertIn('outline numbering outcome is not as requested', str(ctx.exception))
        self.assertEqual(ctx.exception.mutation_state, 'unknown')
        self.assertEqual([call[1] for call in hwp.calls if call[0] == 'Execute'], ['ParagraphShape'])

    def test_the_actual_first_level_snapshots_are_the_documented_token_delta(self) -> None:
        self.assertIn('InstId="153176620" ParaShape="20"', self.raw2)
        self.assertNotIn('153176620', self.raw1)
        self.assertIn('<PARASHAPELIST Count="21">', self.raw2)
        self.assertIn('<PARASHAPELIST Count="20">', self.raw1)

    def test_the_actual_first_level_result_is_accepted_through_the_public_entry_point(self) -> None:
        hwp = _SnapshotHwp(self.raw1, self.raw2)
        result = nn.apply_official_numbering_at_match(hwp, find='N2L1A', level=1, protection=live_protection_reader(hwp))
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['native_actions'], LEVEL_ONE_ACTION)
        self.assertEqual([call[1] for call in hwp.calls if call[0] == 'Execute'], ['ParagraphShape'])
        self.assertEqual([call[1] for call in hwp.calls if call[0] == 'GetDefault'].count('OutlineNumber'), 2)
        self.assertTrue(result['protection']['checked'])

    def test_actual_snapshot_variants_still_refuse_unbound_identifiers(self) -> None:
        marker = 'InstId="153176620" ParaShape="20"'
        variants = {
            'duplicates-first-paragraph': self.raw2.replace(marker, 'InstId="3121190098" ParaShape="20"', 1),
            'malformed': self.raw2.replace(marker, 'InstId="01531766" ParaShape="20"', 1),
            'empty': self.raw2.replace(marker, 'InstId="" ParaShape="20"', 1),
            'also-on-another-paragraph': self.raw2.replace(
                '<P ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>N2L2A', '<P InstId="153176621" ParaShape="0" Style="0"><TEXT CharShape="0"><CHAR>N2L2A', 1
            ),
            'first-paragraph-changed': self.raw2.replace('InstId="3121190098"', 'InstId="3121190099"', 1),
        }
        for label, after in variants.items():
            with self.subTest(label):
                self.assertNotEqual(after, self.raw2)
                self.refused(after)

    def test_actual_snapshot_without_the_new_identifier_is_unchanged_behaviour(self) -> None:
        after = self.raw2.replace('<P InstId="153176620" ParaShape="20"', '<P ParaShape="20"', 1)
        self.assertNotEqual(after, self.raw2)
        self.run_pair(after)


if __name__ == '__main__':
    unittest.main()
