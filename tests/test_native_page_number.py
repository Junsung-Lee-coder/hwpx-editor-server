from __future__ import annotations

import unittest

from app import native_numbering as nn
from tests.native_numbering_fakes import FakeCtrl, FakeHwp, FakeProps


def _doc(**kwargs):
    paragraphs = kwargs.pop('paragraphs', [{'text': 'first body', 'heading_type': 0, 'level': 0}, {'text': 'second body', 'heading_type': 0, 'level': 0}])
    controls = kwargs.pop('controls', None)
    if controls is None:
        controls = [FakeCtrl('secd'), FakeCtrl('tbl'), FakeCtrl('gso')]
    return FakeHwp(paragraphs=paragraphs, controls=controls, **kwargs)


def _ids(hwp):
    return [ctrl.CtrlID for ctrl in hwp.controls]


def _executes(hwp):
    return [call for call in hwp.calls if call[0] == 'Execute']


class NormalizePageNumberTests(unittest.TestCase):
    def test_all_six_documented_positions_map_to_draw_pos(self) -> None:
        expected = {
            'top-left': 1, 'top-center': 2, 'top-right': 3,
            'bottom-left': 4, 'bottom-center': 5, 'bottom-right': 6,
        }
        for name, draw_pos in expected.items():
            request = nn.normalize_page_number_request({'position': name})
            self.assertEqual(request['draw_pos'], draw_pos, name)

    def test_documented_defaults_are_explicit_in_the_normalized_request(self) -> None:
        request = nn.normalize_page_number_request({'position': 'bottom-center'})
        self.assertEqual(request['start_number'], 1)
        self.assertEqual(request['side_char'], 'none')
        self.assertNotIn('header_footer_checked', request)

    def test_dash_decoration_by_side_char_or_matching_prefix_suffix(self) -> None:
        by_side = nn.normalize_page_number_request({'position': 'bottom-center', 'side_char': 'dash'})
        by_text = nn.normalize_page_number_request({'position': 'bottom-center', 'prefix': '-', 'suffix': '-'})
        self.assertEqual(by_side['side_char'], 'dash')
        self.assertEqual(by_text['side_char'], 'dash')

    def test_empty_prefix_suffix_means_no_decoration(self) -> None:
        request = nn.normalize_page_number_request({'position': 'top-right', 'prefix': '', 'suffix': ''})
        self.assertEqual(request['side_char'], 'none')

    def test_rejects_bad_start_numbers_without_coercion(self) -> None:
        for bad in (True, False, 0, -1, 65536, 1.0, '3', None, [1]):
            with self.assertRaises(nn.NativeNumberingInputError, msg=repr(bad)):
                nn.normalize_page_number_request({'position': 'bottom-center', 'start_number': bad})

    def test_accepts_start_number_range_edges(self) -> None:
        self.assertEqual(nn.normalize_page_number_request({'position': 'top-left', 'start_number': 1})['start_number'], 1)
        self.assertEqual(nn.normalize_page_number_request({'position': 'top-left', 'start_number': 65535})['start_number'], 65535)

    def test_rejects_missing_or_unsupported_positions(self) -> None:
        for bad in (None, '', 'Bottom-Center', 'bottom_center', 'outside-top', 'inside-bottom', 5, True, ['top-left']):
            with self.assertRaises(nn.NativeNumberingInputError, msg=repr(bad)):
                nn.normalize_page_number_request({'position': bad})
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.normalize_page_number_request({})

    def test_rejects_unknown_keys_and_non_mappings(self) -> None:
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.normalize_page_number_request({'position': 'top-left', 'number_format': 'roman'})
        for bad in (None, [], 'bottom-center', 3):
            with self.assertRaises(nn.NativeNumberingInputError):
                nn.normalize_page_number_request(bad)

    def test_refuses_arbitrary_prefix_suffix_instead_of_faking_them(self) -> None:
        cases = (
            {'prefix': '쪽 ', 'suffix': ''},
            {'prefix': '', 'suffix': ' 쪽'},
            {'prefix': 'Page ', 'suffix': ' of'},
            {'prefix': '-', 'suffix': ''},
            {'prefix': '- ', 'suffix': ' -'},
            {'prefix': 1, 'suffix': 1},
            {'prefix': None, 'suffix': '-'},
        )
        for extra in cases:
            with self.assertRaises(nn.NativeNumberingInputError, msg=repr(extra)) as ctx:
                nn.normalize_page_number_request({'position': 'bottom-center', **extra})
            self.assertTrue(str(ctx.exception))

    def test_prefix_suffix_conflict_with_side_char_is_rejected(self) -> None:
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.normalize_page_number_request({'position': 'top-left', 'side_char': 'none', 'prefix': '-', 'suffix': '-'})
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.normalize_page_number_request({'position': 'top-left', 'side_char': 'bold'})
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.normalize_page_number_request({'position': 'top-left', 'side_char': True})

    def test_header_footer_acknowledgement_is_not_accepted_with_any_value(self) -> None:
        for value in (True, False, 1, 0, 'true', None):
            with self.assertRaises(nn.NativeNumberingInputError, msg=repr(value)):
                nn.normalize_page_number_request({'position': 'top-left', 'header_footer_checked': value})

    def test_unsupported_prefix_message_names_the_native_limit(self) -> None:
        with self.assertRaises(nn.NativeNumberingInputError) as ctx:
            nn.normalize_page_number_request({'position': 'bottom-center', 'prefix': 'Page '})
        self.assertIn('SideChar', str(ctx.exception))


class ApplyPageNumberTests(unittest.TestCase):
    def test_inserts_with_documented_pagenumpos_call_shape(self) -> None:
        hwp = _doc()
        text_before = hwp.get_text_file()
        others_before = [c for c in hwp.controls]
        result = nn.apply_page_number(hwp, {'position': 'bottom-center', 'start_number': 3, 'side_char': 'dash'})

        self.assertEqual(_ids(hwp), ['secd', 'tbl', 'gso', 'pgnp'])
        self.assertEqual(hwp.controls[:3], others_before)
        names = [call[:2] for call in hwp.calls if call[0] in {'GetDefault', 'Execute'}]
        self.assertEqual(names, [('GetDefault', 'PageNumPos'), ('Execute', 'PageNumPos')])
        params = _executes(hwp)[0][2]
        self.assertEqual(params['DrawPos'], 5)
        self.assertEqual(params['NumberFormat'], 0)
        self.assertEqual(params['NewNumber'], 3)
        self.assertEqual(params['SideChar'], 45)
        self.assertTrue(result['changed'])
        self.assertEqual(result['status'], 'inserted')
        self.assertEqual(hwp.get_text_file(), text_before)

    def test_no_decoration_sets_side_char_zero(self) -> None:
        hwp = _doc()
        nn.apply_page_number(hwp, {'position': 'top-right'})
        params = _executes(hwp)[0][2]
        self.assertEqual((params['DrawPos'], params['NewNumber'], params['SideChar']), (3, 1, 0))

    def test_moves_to_document_begin_and_restores_the_caret(self) -> None:
        hwp = _doc()
        hwp.cursor = (0, 1, 4)
        nn.apply_page_number(hwp, {'position': 'bottom-right'})
        order = [call[0] for call in hwp.calls]
        self.assertLess(order.index('MoveDocBegin'), order.index('Execute'))
        self.assertEqual(hwp.cursor, (0, 1, 4))

    def test_existing_header_footer_is_preserved_when_the_protection_reader_proves_it(self) -> None:
        head, foot = FakeCtrl('head'), FakeCtrl('foot')
        hwp = _doc(controls=[FakeCtrl('secd'), head, foot, FakeCtrl('tbl')])
        hwp.package_state['header_footer'] = ['HEADER', 'FOOTER']
        result = nn.apply_page_number(hwp, {'position': 'bottom-center'}, protection=hwp.census)
        self.assertIs(hwp.controls[1], head)
        self.assertIs(hwp.controls[2], foot)
        self.assertEqual(_ids(hwp), ['secd', 'head', 'foot', 'tbl', 'pgnp'])
        self.assertTrue(result['protection']['checked'])

    def test_refuses_existing_header_footer_without_a_protection_reader_before_any_mutation(self) -> None:
        for kind in ('head', 'foot'):
            hwp = _doc(controls=[FakeCtrl('secd'), FakeCtrl(kind)])
            with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                nn.apply_page_number(hwp, {'position': 'bottom-center'})
            self.assertEqual(ctx.exception.code, 'header_footer_unprovable')
            self.assertEqual(ctx.exception.mutation_state, 'none')
            self.assertEqual(_executes(hwp), [])
            self.assertEqual(_ids(hwp), ['secd', kind])

    def test_identical_existing_page_number_is_a_provable_noop(self) -> None:
        props = FakeProps({'DrawPos': 5, 'NewNumber': 1, 'SideChar': 45, 'NumberFormat': 0})
        hwp = _doc(controls=[FakeCtrl('secd'), FakeCtrl('pgnp', props)])
        result = nn.apply_page_number(hwp, {'position': 'bottom-center', 'side_char': 'dash'})
        self.assertFalse(result['changed'])
        self.assertEqual(result['status'], 'already_present_identical')
        self.assertEqual(_executes(hwp), [])
        self.assertEqual(_ids(hwp), ['secd', 'pgnp'])

    def test_different_or_unreadable_existing_page_number_is_refused_not_duplicated(self) -> None:
        different = FakeProps({'DrawPos': 1, 'NewNumber': 1, 'SideChar': 0, 'NumberFormat': 0})
        unreadable = FakeProps({}, readable=False)
        for props in (different, unreadable):
            hwp = _doc(controls=[FakeCtrl('secd'), FakeCtrl('pgnp', props)])
            with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                nn.apply_page_number(hwp, {'position': 'bottom-center'})
            self.assertEqual(ctx.exception.code, 'existing_page_number')
            self.assertEqual(ctx.exception.mutation_state, 'none')
            self.assertEqual(_executes(hwp), [])
            self.assertEqual(_ids(hwp).count('pgnp'), 1)

    def test_two_existing_page_numbers_are_refused_even_if_one_matches(self) -> None:
        props = FakeProps({'DrawPos': 5, 'NewNumber': 1, 'SideChar': 0, 'NumberFormat': 0})
        hwp = _doc(controls=[FakeCtrl('secd'), FakeCtrl('pgnp', props), FakeCtrl('pgnp', props)])
        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertEqual(_executes(hwp), [])

    def test_multi_section_or_unprovable_section_count_is_refused(self) -> None:
        for controls in ([FakeCtrl('secd'), FakeCtrl('secd')], []):
            hwp = _doc(controls=controls)
            with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
                nn.apply_page_number(hwp, {'position': 'bottom-center'})
            self.assertEqual(ctx.exception.code, 'section_count_unprovable')
            self.assertEqual(_executes(hwp), [])

    def test_unavailable_control_enumeration_or_text_readback_refuses_before_mutation(self) -> None:
        hwp = _doc()
        hwp.enumeration_available = False
        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertEqual(_executes(hwp), [])

        hwp = _doc()
        hwp.text_available = False
        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertEqual(_executes(hwp), [])

    def test_get_default_failure_is_a_pre_mutation_refusal(self) -> None:
        hwp = _doc()
        hwp.HAction.get_default_result = False
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertEqual(ctx.exception.mutation_state, 'none')
        self.assertEqual(_executes(hwp), [])
        self.assertEqual(_ids(hwp), ['secd', 'tbl', 'gso'])

    def test_missing_haction_is_refused(self) -> None:
        class Bare:
            pass

        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_page_number(Bare(), {'position': 'bottom-center'})

    def test_invalid_request_never_touches_the_document(self) -> None:
        hwp = _doc()
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.apply_page_number(hwp, {'position': 'bottom-center', 'start_number': True})
        self.assertEqual(hwp.calls, [])

    def test_execute_exception_reports_possible_mutation_without_claiming_none(self) -> None:
        hwp = _doc()
        hwp.HAction.execute_raises = RuntimeError('com exploded')
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        error = ctx.exception
        self.assertTrue(error.mutation_may_have_persisted)
        self.assertEqual(error.rollback, {'attempted': False, 'succeeded': False})
        self.assertNotIn('no mutation', str(error).lower())
        self.assertEqual(hwp.cursor, (0, 0, 0))

    def test_execute_false_is_possible_mutation_not_clean_refusal(self) -> None:
        hwp = _doc()
        hwp.HAction.execute_result = False
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_success_return_without_a_new_control_is_an_unknown_outcome(self) -> None:
        hwp = _doc()
        hwp.HAction.page_number_mode = 'noop'
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_duplicate_insertion_is_detected_after_the_fact(self) -> None:
        hwp = _doc()
        hwp.HAction.page_number_mode = 'duplicate'
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertIn('pgnp', str(ctx.exception))

    def test_body_text_change_during_execute_is_detected(self) -> None:
        hwp = _doc()
        hwp.HAction.execute_text_edit = 'changed body'
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_readback_mismatch_or_unreadable_settings_after_insert_are_unknown_outcomes(self) -> None:
        for mode in ('wrong-values', 'insert-unreadable'):
            hwp = _doc()
            hwp.HAction.page_number_mode = mode
            with self.assertRaises(nn.NativeNumberingMutationError, msg=mode) as ctx:
                nn.apply_page_number(hwp, {'position': 'bottom-center'})
            self.assertTrue(ctx.exception.mutation_may_have_persisted, msg=mode)
            self.assertEqual(_ids(hwp).count('pgnp'), 1, msg=mode)

    def test_caret_restore_failure_does_not_hide_a_successful_insert(self) -> None:
        hwp = _doc()
        hwp.cursor = (0, 1, 2)
        original_set_pos = hwp.set_pos

        def failing_restore(list_id, para, pos):
            if (list_id, para, pos) == (0, 1, 2):
                raise RuntimeError('restore failed')
            original_set_pos(list_id, para, pos)

        hwp.set_pos = failing_restore
        result = nn.apply_page_number(hwp, {'position': 'bottom-center'})
        self.assertTrue(result['changed'])
        self.assertTrue(any('caret' in item.lower() for item in result['warnings']))


if __name__ == '__main__':
    unittest.main()
