from __future__ import annotations

import unittest

from app import edit_ops
from app import native_numbering as nn
from app import worker
from tests.native_numbering_fakes import FakeCtrl, FakeHwp
from tests.test_native_numbering_census import _HwpmlHwp


def _paragraphs(*texts, heading_type=0, level=0):
    return [{'text': text, 'heading_type': heading_type, 'level': level} for text in texts]


def _doc(texts=('항목 하나', '항목 둘', '항목 셋'), **kwargs):
    controls = kwargs.pop('controls', None)
    if controls is None:
        controls = [FakeCtrl('secd'), FakeCtrl('tbl')]
    paragraphs = kwargs.pop('paragraphs', None) or _paragraphs(*texts)
    return _HwpmlHwp(paragraphs=paragraphs, controls=controls)


def _at(hwp, index):
    hwp.cursor = (0, index, 0)
    return hwp


def _mutations(hwp):
    return [call for call in hwp.calls if call[0] in {'Run', 'Execute'}]


class NormalizeOfficialNumberingTests(unittest.TestCase):
    def test_levels_one_to_four_map_to_zero_based_native_levels(self) -> None:
        for level in (1, 2, 3, 4):
            request = nn.normalize_official_numbering_request({'level': level})
            self.assertEqual(request['level'], level)
            self.assertEqual(request['native_level'], level - 1)

    def test_rejects_bad_levels_without_coercion_or_defaults(self) -> None:
        for bad in (True, False, 0, -1, 5, 2.0, '2', None, [2]):
            with self.assertRaises(nn.NativeNumberingInputError, msg=repr(bad)):
                nn.normalize_official_numbering_request({'level': bad})
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.normalize_official_numbering_request({})

    def test_rejects_unknown_keys_and_non_mappings(self) -> None:
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.normalize_official_numbering_request({'level': 1, 'format': '1.'})
        for bad in (None, [], 2, 'level'):
            with self.assertRaises(nn.NativeNumberingInputError):
                nn.normalize_official_numbering_request(bad)

    def test_normalized_request_carries_no_format_claim(self) -> None:
        request = nn.normalize_official_numbering_request({'level': 1})
        self.assertNotIn('numbering_format_enforced', request)


class ApplyOfficialNumberingTests(unittest.TestCase):
    def test_level_one_uses_one_native_paragraph_shape_action_and_keeps_text(self) -> None:
        hwp = _at(_doc(), 1)
        before = hwp.get_text_file()
        result = nn.apply_official_numbering(hwp, {'level': 1})
        actions = _mutations(hwp)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][:2], ('Execute', 'ParagraphShape'))
        self.assertEqual((actions[0][2]['HeadingType'], actions[0][2]['Level']), (1, 0))
        self.assertEqual(hwp.paragraphs[1]['heading_type'], 1)
        self.assertEqual(hwp.paragraphs[1]['level'], 0)
        self.assertEqual(hwp.get_text_file(), before)
        self.assertTrue(result['changed'])
        self.assertEqual(result['status'], 'applied')

    def test_deeper_levels_set_heading_type_and_the_zero_based_native_level_in_one_action(self) -> None:
        for level in (2, 3, 4):
            hwp = _at(_doc(), 0)
            nn.apply_official_numbering(hwp, {'level': level})
            actions = _mutations(hwp)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0][:2], ('Execute', 'ParagraphShape'))
            self.assertEqual((actions[0][2]['HeadingType'], actions[0][2]['Level']), (1, level - 1))
            self.assertEqual(hwp.paragraphs[0]['heading_type'], 1)
            self.assertEqual(hwp.paragraphs[0]['level'], level - 1)

    def test_numbers_are_never_written_into_paragraph_text(self) -> None:
        hwp = _at(_doc(), 0)
        nn.apply_official_numbering(hwp, {'level': 3})
        self.assertEqual(hwp.paragraphs[0]['text'], '항목 하나')

    def test_already_numbered_at_same_level_is_a_noop_because_the_action_is_a_toggle(self) -> None:
        hwp = _at(_doc(paragraphs=_paragraphs('항목', heading_type=1, level=1)), 0)
        result = nn.apply_official_numbering(hwp, {'level': 2})
        self.assertEqual(_mutations(hwp), [])
        self.assertFalse(result['changed'])
        self.assertEqual(result['status'], 'already_applied')
        self.assertEqual(hwp.paragraphs[0]['heading_type'], 1)

    def test_outline_paragraph_at_other_level_changes_level_without_toggling_outline(self) -> None:
        hwp = _at(_doc(paragraphs=_paragraphs('항목', heading_type=1, level=0)), 0)
        result = nn.apply_official_numbering(hwp, {'level': 4})
        actions = _mutations(hwp)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0][:2], ('Execute', 'ParagraphShape'))
        self.assertEqual(actions[0][2]['Level'], 3)
        self.assertEqual(hwp.paragraphs[0]['heading_type'], 1)
        self.assertEqual(hwp.paragraphs[0]['level'], 3)
        self.assertTrue(result['changed'])
        self.assertEqual(result['status'], 'level_changed')

    def test_number_or_bullet_paragraphs_are_refused_with_zero_mutation(self) -> None:
        for heading_type in (2, 3):
            hwp = _at(_doc(paragraphs=_paragraphs('항목', heading_type=heading_type)), 0)
            with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                nn.apply_official_numbering(hwp, {'level': 1})
            self.assertEqual(ctx.exception.mutation_state, 'none')
            self.assertEqual(_mutations(hwp), [])
            self.assertEqual(hwp.paragraphs[0]['heading_type'], heading_type)

    def test_unreadable_heading_type_is_refused_before_any_action(self) -> None:
        hwp = _at(_doc(), 0)
        hwp.HAction.heading_unreadable = True
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            nn.apply_official_numbering(hwp, {'level': 1})
        self.assertEqual(ctx.exception.mutation_state, 'none')
        self.assertEqual(_mutations(hwp), [])

        hwp = _at(_doc(), 0)
        hwp.HAction.get_default_result = False
        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_official_numbering(hwp, {'level': 1})
        self.assertEqual(_mutations(hwp), [])

    def test_missing_haction_is_refused(self) -> None:
        class Bare:
            pass

        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_official_numbering(Bare(), {'level': 1})

    def test_invalid_request_does_not_touch_the_document(self) -> None:
        hwp = _at(_doc(), 0)
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.apply_official_numbering(hwp, {'level': True})
        self.assertEqual(hwp.calls, [])

    def test_action_exception_is_reported_as_possible_mutation(self) -> None:
        hwp = _at(_doc(), 0)
        hwp.HAction.execute_raises = RuntimeError('boom')
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_official_numbering(hwp, {'level': 1})
        error = ctx.exception
        self.assertTrue(error.mutation_may_have_persisted)
        self.assertEqual(error.rollback, {'attempted': False, 'succeeded': False})
        self.assertNotIn('no mutation', str(error).lower())

    def test_action_false_return_is_possible_mutation(self) -> None:
        hwp = _at(_doc(), 0)
        hwp.HAction.execute_result = False
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_official_numbering(hwp, {'level': 1})
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_action_that_does_not_change_heading_type_is_an_unknown_outcome(self) -> None:
        hwp = _at(_doc(), 0)
        hwp.HAction.shape_mode = 'noop'
        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_official_numbering(hwp, {'level': 1})

    def test_literal_text_prefix_or_text_edit_is_detected(self) -> None:
        for mode, edit in (('prefix-text', None), ('apply', '다른 본문')):
            hwp = _at(_doc(), 0)
            hwp.HAction.shape_mode = mode
            hwp.HAction.execute_text_edit = edit
            with self.assertRaises(nn.NativeNumberingMutationError, msg=mode) as ctx:
                nn.apply_official_numbering(hwp, {'level': 1})
            self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_wrong_level_after_level_action_is_detected(self) -> None:
        hwp = _at(_doc(), 0)
        hwp.HAction.shape_execute_level_offset = 1
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_official_numbering(hwp, {'level': 3})
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_shape_action_failure_does_not_claim_no_mutation(self) -> None:
        hwp = _at(_doc(), 0)
        hwp.HAction.execute_result = False
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_official_numbering(hwp, {'level': 2})
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertNotIn('no mutation', str(ctx.exception).lower())

    def test_four_level_sequence_touches_only_targets_and_keeps_all_text(self) -> None:
        texts = ('제목', '첫째', '둘째', '셋째', '넷째', '다섯째', '본문 끝')
        hwp = _doc(texts=texts)
        before = hwp.get_text_file()
        wanted = {1: 1, 2: 2, 3: 3, 4: 4, 5: 2}
        for index, level in wanted.items():
            nn.apply_official_numbering(_at(hwp, index), {'level': level})
        self.assertEqual(hwp.get_text_file(), before)
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 1, 1, 1, 1, 0])
        self.assertEqual([p['level'] for p in hwp.paragraphs], [0, 0, 1, 2, 3, 1, 0])
        self.assertEqual([c.CtrlID for c in hwp.controls], ['secd', 'tbl'])


class OfficialNumberingEditOpsTests(unittest.TestCase):
    def test_new_ops_are_registered(self) -> None:
        self.assertIn('page_number', edit_ops.SUPPORTED_OPS)
        self.assertIn('official_numbering', edit_ops.SUPPORTED_OPS)
        self.assertIn('official_numbering', worker.MATCH_REQUIRED_OPS)

    def test_normalize_rejects_malformed_payloads_for_both_ops(self) -> None:
        bad_payloads = (
            {'op': 'page_number', 'position': 'bottom-center', 'start_number': True},
            {'op': 'page_number', 'position': 'middle'},
            {'op': 'page_number', 'position': 'bottom-center', 'prefix': '쪽 '},
            {'op': 'page_number', 'position': 'bottom-center', 'unknown': 1},
            {'op': 'official_numbering', 'find': '항목', 'level': 5},
            {'op': 'official_numbering', 'find': '항목', 'level': True},
            {'op': 'official_numbering', 'find': '항목'},
            {'op': 'official_numbering', 'level': 1},
            {'op': 'official_numbering', 'find': '', 'level': 1},
            {'op': 'official_numbering', 'find': '두 줄\n항목', 'level': 1},
            {'op': 'official_numbering', 'find': '항목', 'level': 1, 'kind': 'number'},
        )
        for payload in bad_payloads:
            with self.assertRaises(edit_ops.EditOperationError, msg=repr(payload)):
                edit_ops.normalize_operations([payload])

    def test_normalize_accepts_valid_payloads(self) -> None:
        ops = edit_ops.normalize_operations([
            {'op': 'page_number', 'position': 'top-right', 'start_number': 5, 'side_char': 'dash'},
            {'op': 'official_numbering', 'find': '항목', 'level': 2, 'step_id': 's1'},
        ])
        self.assertEqual([op['op'] for op in ops], ['page_number', 'official_numbering'])

    def test_apply_page_number_through_dispatch(self) -> None:
        hwp = _doc()
        ops = edit_ops.normalize_operations([{'op': 'page_number', 'position': 'bottom-center', 'start_number': 2}])
        summary = edit_ops.apply_edit_operations(hwp, ops)
        self.assertEqual(summary[0]['op'], 'page_number')
        self.assertTrue(summary[0]['changed'])
        self.assertEqual([c.CtrlID for c in hwp.controls].count('pgnp'), 1)

    def test_apply_page_number_refusal_surfaces_as_an_edit_error(self) -> None:
        hwp = _doc(controls=[FakeCtrl('secd'), FakeCtrl('secd')])
        ops = edit_ops.normalize_operations([{'op': 'page_number', 'position': 'bottom-center'}])
        with self.assertRaises(edit_ops.EditOperationError):
            edit_ops.apply_edit_operations(hwp, ops)
        self.assertEqual([c for c in hwp.calls if c[0] == 'Execute'], [])

    def test_apply_official_numbering_hits_only_the_matched_paragraph(self) -> None:
        hwp = _doc(texts=('항목 하나', '항목 둘', '맺음말'))
        ops = edit_ops.normalize_operations([{'op': 'official_numbering', 'find': '항목 둘', 'level': 2}])
        summary = edit_ops.apply_edit_operations(hwp, ops)
        self.assertEqual(summary[0]['matches'], 1)
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertEqual([p['level'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertEqual(hwp.paragraphs[1]['text'], '항목 둘')

    def test_ambiguous_or_absent_target_is_refused_with_zero_mutation(self) -> None:
        for find in ('항목', '없는 문장'):
            hwp = _doc(texts=('항목 하나', '항목 둘', '맺음말'))
            ops = edit_ops.normalize_operations([{'op': 'official_numbering', 'find': find, 'level': 1}])
            with self.assertRaises(edit_ops.EditOperationError, msg=find):
                edit_ops.apply_edit_operations(hwp, ops)
            self.assertEqual(_mutations(hwp), [])
            self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 0, 0])

    def _hwpml_doc(self, *, header: bool, texts=('항목 하나', '항목 둘', '맺음말')):
        controls = [FakeCtrl('secd')] + ([FakeCtrl('head')] if header else [])
        hwp = _HwpmlHwp(paragraphs=_paragraphs(*texts), controls=controls)
        if header:
            hwp.package_state['header_footer'] = ['HEADER']
        return hwp

    def test_dispatch_page_number_protects_the_header_through_the_real_reader(self) -> None:
        ops = edit_ops.normalize_operations([{'op': 'page_number', 'position': 'bottom-center'}])
        hwp = self._hwpml_doc(header=True)
        summary = edit_ops.apply_edit_operations(hwp, ops)
        self.assertTrue(summary[0]['protection']['checked'])

        hwp = self._hwpml_doc(header=True)
        original = hwp.HAction.Execute

        def alter(name, hset):
            result = original(name, hset)
            hwp.package_state['header_footer'] = ['ALTERED']
            return result

        hwp.HAction.Execute = alter
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            edit_ops.apply_edit_operations(hwp, ops)
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(hwp.package_state['header_footer'], ['ALTERED'])

    def test_dispatch_page_number_with_header_and_an_unreadable_census_refuses_before_mutation(self) -> None:
        ops = edit_ops.normalize_operations([{'op': 'page_number', 'position': 'bottom-center'}])
        hwp = self._hwpml_doc(header=True)
        hwp.hwpml_text = '<HWPML><BODY>'
        with self.assertRaises(nn.NativeNumberingRefusal):
            edit_ops.apply_edit_operations(hwp, ops)
        self.assertEqual(_mutations(hwp), [])

    def test_dispatch_official_numbering_refuses_to_redefine_an_outline_used_by_other_paragraphs(self) -> None:
        from tests.native_numbering_fakes import shape_items

        hwp = self._hwpml_doc(header=False)
        hwp.paragraphs[0].update(heading_type=1)
        hwp.outline_shape.update(shape_items((('Ⅰ.', 2), ('^2.', 8), ('^3)', 0), ('^4)', 8))))
        ops = edit_ops.normalize_operations([{'op': 'official_numbering', 'find': '항목 둘', 'level': 1}])
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            edit_ops.apply_edit_operations(hwp, ops)
        self.assertEqual(ctx.exception.code, 'foreign_outline_definition')
        self.assertEqual(_mutations(hwp), [])

    def test_unknown_outcome_error_propagates_from_dispatch(self) -> None:
        hwp = _doc(texts=('항목 하나', '맺음말'))
        hwp.HAction.execute_raises = RuntimeError('boom')
        ops = edit_ops.normalize_operations([{'op': 'official_numbering', 'find': '항목 하나', 'level': 1}])
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            edit_ops.apply_edit_operations(hwp, ops)
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertIsInstance(ctx.exception, edit_ops.EditOperationError)


if __name__ == '__main__':
    unittest.main()
