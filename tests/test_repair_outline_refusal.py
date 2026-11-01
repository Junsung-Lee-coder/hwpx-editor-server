"""R1-R5 through the real service, bundle, owning thread and review assembly.

The SDK is a fake and the HWPML2X of the outline documents is synthetic. Faults are labelled; none is native evidence.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import local_cli_runtime as rt
from app import native_numbering as nn
from app.local_cli_service import LocalCliServiceError, as_http_error
from app.native_numbering_census import parse_hwpml2x_census
from hwpx_mcp import review
from tests.native_numbering_fakes import FakeCtrl
from tests.test_native_numbering_outline_scope import scoped_doc, split_sections, state
from tests.test_native_numbering_census import _HwpmlHwp
from tests.test_native_numbering_shared_outline import _SharedHwp
from tests.test_native_paragraph_edit import _EditHwp
from tests.test_repair_ordinary_refusals import SYNTHETIC, Base, CloseIsModified, NUMBERING, mutator_calls
from tests.test_repair_ordinary_review import assembled, capture, captured_readback, printed
from tests.test_repair_review_rules import built, para
from tests.test_repair_runtime_safety import OVERSIZED, history_doc, owned_doc, readback

STEP = {**NUMBERING, 'match': 'Item text', 'level': 2}
INSERT = {'op': 'paragraph_insert_exact', 'label': 'x', 'match': 'Item text', 'expected_page': 1, 'text': 'New', 'position': 'after', 'confirm_layout': True}
UNPROVABLE = 'outline_usage_unprovable'


class OutlineScopeRefusals(Base):
    """R1: what the document census shows is a refusal, once the state it found is proved back."""

    def refused(self, hwp, code):
        owned = owned_doc(self, hwp)
        before, caret = state(hwp), hwp.get_pos()
        result, error = owned.run(STEP)
        self.assert_clean_refusal(owned, result, error, code)
        self.assertEqual(state(hwp), before)
        self.assertEqual(hwp.get_pos(), caret)
        self.assertEqual([call[1] for call in hwp.calls if call[0] == 'Execute'], [])
        return owned

    def test_automatic_number_with_unknown_sharing(self):
        self.refused(scoped_doc(), UNPROVABLE)

    def test_outline_paragraph_in_another_section(self):
        self.refused(scoped_doc(split_sections(_HwpmlHwp), heading=1), UNPROVABLE)

    def test_another_section_using_the_same_definition(self):
        self.refused(scoped_doc(split_sections(_SharedHwp, '1'), number_ref='2'), UNPROVABLE)

    def test_other_outline_paragraph_in_the_section(self):
        self.refused(scoped_doc(heading=1), 'foreign_outline_definition')

    def test_automatic_number_known_to_share_the_definition(self):
        self.refused(scoped_doc(_SharedHwp, number_ref='1'), 'foreign_outline_definition')

    def test_the_session_keeps_working_and_closes_after_the_refusal(self):
        hwp = scoped_doc(_EditHwp)
        hwp.IsModified = False
        owned = self.refused(hwp, UNPROVABLE)
        self.assertTrue(owned.run(INSERT)[0]['ok'])
        self.assertFalse(owned.session._native_custody_held)
        self.assertTrue(owned.session._normal_close_requires_save)

    def test_earlier_edits_survive_the_refusal(self):
        hwp = scoped_doc(_EditHwp)
        owned = owned_doc(self, hwp, working_copy_dirty=False, dirty=False)
        self.assertTrue(owned.run(INSERT)[0]['ok'])
        result, error = owned.run(STEP)
        self.assertIsNone(error)
        step = result['steps'][0]
        self.assertEqual(step['refusal_code'], UNPROVABLE)
        self.assertIs(step['mutation_may_have_persisted'], False)
        self.assertIs(step['dirty'], False)
        self.assertFalse(owned.session._native_custody_held)
        self.assertTrue(owned.session._normal_close_requires_save)
        self.assertTrue(owned.session._lifetime_edit_possible)
        self.assertIs(owned.store['working_copy_dirty'], True)
        self.assertIn('New', [p['text'] for p in hwp.paragraphs])
        with self.assertRaises(rt.LocalCliRuntimeError) as caught:
            owned.manager.close_session(owned.sid, timeout=5, prior_dirty=True)
        self.assertTrue(getattr(caught.exception, 'save_required', False))

    def test_an_active_selection_comes_back(self):
        hwp = scoped_doc()
        owned = owned_doc(self, hwp)
        hwp.set_pos(0, 0, 0)
        result, error = owned.run(STEP)
        self.assert_clean_refusal(owned, result, error, UNPROVABLE)
        self.assertEqual(hwp.get_pos(), (0, 0, 0))

    def assert_unknown(self, owned, result, error, mark=None):
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertNotIn('refusal_code', result['steps'][0])
        self.assertEqual(mutator_calls(owned.hwp), [])
        if mark is not None:
            self.assertTrue(mark)
            self.assertEqual(owned.hwp.calls[mark[0]:], [], 'an SDK call followed the unknown read')

    def test_failed_restoration_is_not_a_refusal(self):
        hwp = scoped_doc()
        owned = owned_doc(self, hwp)
        real, seen = hwp.set_pos, []

        def set_pos(*args):
            seen.append(args)
            if len(seen) > 1:  # The first move reaches the target; the next one is the restoration.
                raise OSError(SYNTHETIC)
            return real(*args)
        hwp.set_pos = set_pos
        self.assert_unknown(owned, *owned.run(STEP))
        self.assertEqual(len(seen), 2)

    def test_a_document_changed_during_the_investigation_is_not_a_refusal(self):
        hwp = scoped_doc()
        owned = owned_doc(self, hwp)
        real = hwp.HAction.GetDefault

        def get_default(name, hset):
            if name == 'OutlineNumber':
                hwp.paragraphs[0]['text'] = 'Changed behind the read'
            return real(name, hset)
        hwp.HAction.GetDefault = get_default
        self.assert_unknown(owned, *owned.run(STEP))

    def test_definition_getter_failures_stay_unknown_with_no_later_call(self):
        for fault in ('raise', False, None):
            with self.subTest(fault=fault):
                hwp = scoped_doc()
                owned = owned_doc(self, hwp)
                real, mark = hwp.HAction.GetDefault, []

                def get_default(name, hset, _fault=fault):
                    if name == 'OutlineNumber':
                        hwp.calls.append(('failed_outline_read',))
                        mark[:] = [len(hwp.calls)]
                        if _fault == 'raise':
                            raise OSError(SYNTHETIC)
                        return _fault
                    return real(name, hset)
                hwp.HAction.GetDefault = get_default
                self.assert_unknown(owned, *owned.run(STEP), mark=mark)

    def test_a_refusal_class_thrown_by_the_sdk_is_not_trusted(self):
        for code in (UNPROVABLE, 'target_ambiguous'):
            with self.subTest(code=code):
                hwp = scoped_doc()
                owned = owned_doc(self, hwp)
                armed, real, mark = [0], hwp.find, []

                def find(*args, **kwargs):
                    armed[0] += 1
                    if armed[0] == 2:  # The strict finder's call passes; this is the library's own search.
                        hwp.calls.append(('failed_find',))
                        mark[:] = [len(hwp.calls)]
                        raise nn.NativeNumberingRefusal('synthetic', code=code)
                    return real(*args, **kwargs)
                hwp.find = find
                self.assert_unknown(owned, *owned.run(STEP), mark=mark)

    def test_many_matches_in_several_paragraphs_are_ambiguous(self):
        from tests.test_native_numbering_preservation import make_doc
        hwp = make_doc(texts=('Title', 'Item one', 'Item two', 'Item three'))
        owned = owned_doc(self, hwp)
        with patch.object(nn, '_MAX_FIND_HITS', 2):
            result, error = owned.run({**NUMBERING})
        self.assert_clean_refusal(owned, result, error, 'target_ambiguous')

    def test_a_search_stuck_on_one_paragraph_stays_unknown(self):
        from tests.test_native_numbering_preservation import make_doc
        hwp = make_doc(texts=('Title', 'Item one', 'Tail'))
        hwp.MoveRight = lambda: None
        owned = owned_doc(self, hwp)
        with patch.object(nn, '_MAX_FIND_HITS', 2):
            self.assert_unknown(owned, *owned.run({**NUMBERING}))


class ListCoverageWhenUnread(unittest.TestCase):
    """R2: 'none' is a statement about a whole document that was read."""

    def test_unread_structure_is_unverified_with_no_counts(self):
        result = built(None, ['text\n'], available=False, reason='census_unavailable', evidence={})
        self.assertEqual(result['coverage']['list_numbering'], {'state': 'unverified', 'number': None, 'bullet': None, 'nested_outline': None})
        self.assertEqual(result['review_status'], 'unverified')
        self.assertEqual(result['summary_text'].splitlines()[0], '확인 결과: 확인하지 못함. 문서 구조를 읽지 못했습니다.')
        self.assertIn('확인하지 못함', result['summary_text'])

    def test_truncated_structure_is_partial_even_with_none_seen(self):
        result = built([para('Alpha')], ['Alpha\n'], truncated=True, total=2500)
        self.assertEqual(result['coverage']['list_numbering'], {'state': 'partial', 'number': 0, 'bullet': 0, 'nested_outline': 0})
        self.assertEqual(result['review_status'], 'partial')

    def test_truncated_structure_counts_what_it_read_as_a_lower_bound(self):
        result = built([para('Alpha', 'NUMBER'), para('Beta', 'BULLET')], ['Alpha\nBeta\n'], truncated=True, total=2500)
        self.assertEqual(result['coverage']['list_numbering'], {'state': 'partial', 'number': 1, 'bullet': 1, 'nested_outline': 0})
        self.assertIn('읽은 문단 중 자동 번호 1개, 글머리표 1개', result['summary_text'].splitlines()[0])
        self.assertIn('읽은 문단 중 자동 번호 1개', review.to_markdown(result))

    def test_a_whole_document_with_none_is_still_none(self):
        result = built([para('Alpha')], ['Alpha\n'])
        self.assertEqual(result['coverage']['list_numbering'], {'state': 'none', 'number': 0, 'bullet': 0, 'nested_outline': 0})
        self.assertEqual(result['review_status'], 'complete')

    def test_a_whole_document_with_lists_keeps_its_counts(self):
        result = built([para('Alpha', 'NUMBER')], ['Alpha\n'])
        self.assertEqual(result['coverage']['list_numbering'], {'state': 'unverified', 'number': 1, 'bullet': 0, 'nested_outline': 0})


class CloseReportsTheHold(unittest.TestCase):
    """R3: a close stopped by a hold says so with the backend's own label; a save refusal stays recoverable."""

    def close_error(self, owned):
        with self.assertRaises(LocalCliServiceError) as caught:
            owned.service.close()
        return as_http_error(caught.exception)

    def test_fault_at_close_reports_the_hold(self):
        owned = owned_doc(self)
        CloseIsModified.raising(self, owned.hwp)
        http = self.close_error(owned)
        self.assertEqual(http.status_code, 409)
        self.assertEqual(http.detail, f'native_custody_held: {rt._HELD_CLOSE_MESSAGE}')
        self.assertTrue(owned.session._native_custody_held)
        self.assertIs(owned.store['native_custody_held'], True)
        self.assertEqual(owned.cleanup_events(), [])

    def test_a_session_already_held_reports_the_hold_again(self):
        owned = owned_doc(self)
        CloseIsModified.raising(self, owned.hwp)
        self.close_error(owned)
        self.assertEqual(self.close_error(owned).detail, f'native_custody_held: {rt._HELD_CLOSE_MESSAGE}')

    def test_a_hold_from_an_earlier_command_reports_the_hold(self):
        hwp = scoped_doc()
        owned = owned_doc(self, hwp)
        real = hwp.HAction.GetDefault

        def get_default(name, hset):
            if name == 'OutlineNumber':
                raise OSError(SYNTHETIC)
            return real(name, hset)
        hwp.HAction.GetDefault = get_default
        owned.run(STEP)
        self.assertTrue(owned.session._native_custody_held)
        self.assertTrue(self.close_error(owned).detail.startswith('native_custody_held: '))

    def test_a_held_command_error_carries_the_same_label(self):
        owned = owned_doc(self)

        def read(*args):
            raise OSError(SYNTHETIC)
        owned.hwp.get_text_file = read
        with self.assertRaises(LocalCliServiceError) as caught:
            readback(owned)
        self.assertTrue(as_http_error(caught.exception).detail.startswith('native_custody_held: '))

    def test_a_save_refusal_is_not_a_hold(self):
        owned = owned_doc(self, working_copy_dirty=True, dirty=True)
        http = self.close_error(owned)
        self.assertTrue(http.detail.startswith('save_required: '))
        self.assertFalse(owned.session._native_custody_held)
        self.assertNotIn('native_custody_held', owned.store)

    def test_another_close_failure_is_not_called_a_hold(self):
        owned = owned_doc(self)
        with patch.object(owned.manager, 'close_session', side_effect=rt.LocalCliRuntimeError('synthetic close failure')):
            http = self.close_error(owned)
        self.assertEqual(http.status_code, 500)
        self.assertFalse(http.detail.startswith('native_custody_held'))
        self.assertNotIn('native_custody_held', owned.store)


class HistoryEvidenceLimits(unittest.TestCase):
    """R4: no comparison, no native Undo or Redo; nothing is held and earlier debt stays."""

    def owned(self, **binding):
        hwp = history_doc()
        owned = owned_doc(self, hwp, undo_stack=[1], redo_stack=[1], **binding)
        for name in ('_snapshot_cursor_context', '_capture_nearby_text_context', 'snapshot_live_location'):
            patcher = patch(f'app.local_cli_service.{name}', return_value={})
            patcher.start()
            self.addCleanup(patcher.stop)
        return owned

    def over_limit(self, hwp):
        hwp.controls.extend(FakeCtrl('tbl') for _ in range(5000))

    def native(self, owned):
        return [c for c in owned.hwp.calls if c in (('Run', 'Undo'), ('Run', 'Redo'))]

    def assert_refused(self, owned, call, code, stacks=([1], [1])):
        with self.assertRaises(Exception) as caught:
            call()
        http = as_http_error(caught.exception)
        self.assertEqual(http.status_code, 409)
        self.assertTrue(http.detail.startswith(code), http.detail)
        self.assertEqual(self.native(owned), [])
        self.assertFalse(owned.session._native_custody_held)
        self.assertEqual((owned.store['undo_stack'], owned.store['redo_stack']), stacks)

    def test_control_limit_refuses_undo_and_redo(self):
        owned = self.owned()
        self.over_limit(owned.hwp)
        self.assert_refused(owned, owned.service.undo, 'undo_evidence_unavailable')
        self.assert_refused(owned, owned.service.redo, 'redo_evidence_unavailable')
        self.assertFalse(owned.session._normal_close_requires_save)
        self.assertIsNot(owned.store.get('working_copy_dirty'), True)

    def test_unusable_document_text_refuses_undo(self):
        owned = self.owned()
        owned.hwp.hwpml_text = OVERSIZED
        self.assert_refused(owned, owned.service.undo, 'undo_evidence_unavailable')

    def test_earlier_debt_stays_and_the_session_stays_usable(self):
        owned = self.owned(working_copy_dirty=True, dirty=True)
        owned.session._normal_close_requires_save = True
        owned.session._lifetime_edit_possible = True
        self.over_limit(owned.hwp)
        self.assert_refused(owned, owned.service.undo, 'undo_evidence_unavailable')
        self.assertTrue(owned.session._normal_close_requires_save)
        self.assertTrue(owned.session._lifetime_edit_possible)
        self.assertIs(owned.store['working_copy_dirty'], True)
        self.assertEqual(owned.manager.execute(session_id=owned.sid, command_name='where', handler=lambda h: {'still': 'usable'}, timeout=5)['still'], 'usable')

    def test_a_control_read_fault_stays_held_with_no_native_undo(self):
        class Raising(FakeCtrl):
            @property
            def CtrlID(self):  # noqa: N802
                raise OSError(SYNTHETIC)

            @CtrlID.setter
            def CtrlID(self, value):  # noqa: N802
                pass
        owned = self.owned()
        owned.hwp.controls.append(Raising('tbl'))
        with self.assertRaises(LocalCliServiceError):
            owned.service.undo()
        self.assertEqual(self.native(owned), [])
        self.assertTrue(owned.session._native_custody_held)

    def test_evidence_lost_after_the_native_call_is_reported_unavailable(self):
        owned = self.owned()
        hwp = owned.hwp
        hwp.undone.append(list(hwp.paragraphs))
        run = hwp.HAction.Run

        def tracked(name):
            result = run(name)
            if name == 'Undo':
                self.over_limit(hwp)
            return result
        hwp.HAction.Run = tracked
        result = owned.service.undo()
        self.assertEqual(result['confirmation'], {'available': False, 'document_changed': None})
        self.assertFalse(owned.session._native_custody_held)
        self.assertEqual(self.native(owned), [('Run', 'Undo')])


class EmptyParagraphCharacterStyle(unittest.TestCase):
    """R5: an empty run is ignored only beside other content; in an empty paragraph it is the paragraph's style."""

    EMPTY = '<P ParaShape="0" Style="0"><TEXT CharShape="{}"/></P>'
    ANCHOR = '<P ParaShape="0" Style="0"><TEXT CharShape="1"><CHAR>서식센티널'

    def with_empty(self, shape):
        text = capture('tabbed')
        self.assertIn(self.ANCHOR, text)
        return text.replace(self.ANCHOR, self.EMPTY.format(shape) + self.ANCHOR, 1)

    def empty_paragraph(self, text):
        parsed = parse_hwpml2x_census(text)['paragraphs']
        index = next(i for i, item in enumerate(parsed) if item['text'].startswith('서식센티널'))
        self.assertEqual(parsed[index - 1]['text'], '')
        return parsed[index - 1]

    def test_a_changed_style_on_an_empty_paragraph_changes_its_content_digest(self):
        self.assertNotEqual(self.empty_paragraph(self.with_empty(0))['content_sha256'], self.empty_paragraph(self.with_empty(1))['content_sha256'])

    def test_the_same_style_stays_equal(self):
        self.assertEqual(self.empty_paragraph(self.with_empty(0))['content_sha256'], self.empty_paragraph(self.with_empty(0))['content_sha256'])

    def test_comparison_reports_it(self):
        owned = owned_doc(self)
        owned.hwp.hwpml_text = self.with_empty(0)
        first = readback(owned)
        owned.hwp.hwpml_text = self.with_empty(1)
        second = readback(owned)
        result = assembled(second, printed(second), baseline=assembled(first, printed(first)))
        self.assertEqual([(c['kind'], c['text']) for c in result['comparison']['changes']], [('format_changed', '')])

    def test_the_undo_normalization_for_a_paragraph_with_content_stays(self):
        before, _a = captured_readback(self, 'tabbed-before-remove-undo')
        after, _b = captured_readback(self, 'tabbed-after-remove-undo')
        result = assembled(after, printed(after), baseline=assembled(before, printed(before)))
        self.assertEqual(result['comparison']['changes'], [])

    def test_a_table_holding_paragraph_keeps_its_table_as_content(self):
        text = capture('tabbed')
        self.assertNotIn('<TEXT CharShape="0"><CHAR/></TEXT></P>', text)
        end = '</TEXT></P><P ParaShape="0" Style="0"><TEXT CharShape="2"><CHAR>단독글자'
        self.assertIn(end, text)
        changed = text.replace(end, '</TEXT><TEXT CharShape="0"><CHAR/></TEXT>' + end.removeprefix('</TEXT>'), 1)
        left = parse_hwpml2x_census(text)['paragraphs']
        right = parse_hwpml2x_census(changed)['paragraphs']
        self.assertEqual([p['content_sha256'] for p in left], [p['content_sha256'] for p in right])


if __name__ == '__main__':
    unittest.main()
