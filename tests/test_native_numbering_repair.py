"""Behavioral regressions for the repaired page-number / official-numbering contract.

Synthetic fakes only: these tests prove call shapes and refusal/unknown-state handling, not Hancom behavior.
"""
from __future__ import annotations

import unittest

from app import native_numbering as nn
from app.local_cli_bundle_numbering import LocalCliBundleNumberingMixin
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError
from tests.native_numbering_fakes import (
    OFFICIAL_SHAPE_LEVELS,
    STOCK_TAIL_LEVELS,
    FakeCtrl,
    FakeHwp,
    FakeProps,
    shape_items,
)

POSITION = {'position': 'bottom-center'}
OFFICIAL = shape_items(OFFICIAL_SHAPE_LEVELS)
FOREIGN_LEVELS = (('Ⅰ.', 2), ('(^2)', 3), ('^3>', 4), ('[^4]', 1))
TAIL = {
    f'{prefix}Level{index}': value
    for index, (text, number_format) in enumerate(STOCK_TAIL_LEVELS, start=len(OFFICIAL_SHAPE_LEVELS))
    for prefix, value in (('StrFormat', text), ('NumFormat', number_format))
}


def _mutating_calls(hwp: FakeHwp) -> list[tuple]:
    return [call for call in hwp.calls if call[0] in {'Execute', 'Run'}]


def _paragraphs(*specs):
    return [{'text': text, 'heading_type': heading, 'level': level, 'section': section} for text, heading, level, section in specs]


def _body(**kwargs) -> FakeHwp:
    paragraphs = kwargs.pop('paragraphs', None) or _paragraphs(
        ('Title', 0, 0, 0), ('Item text', 0, 0, 0), ('Tail', 0, 0, 0)
    )
    return FakeHwp(paragraphs=paragraphs, **kwargs)


class _Service(LocalCliBundleNumberingMixin):
    def _find_text_on_expected_page(self, hwp, *, match, expected_page, numbering_guard=False):
        for index, para in enumerate(hwp.paragraphs):
            if match in str(para['text']):
                hwp.cursor = (0, index, 0)
                return {'page': expected_page, 'pos': [0, index, 0]}
        raise LocalCliRuntimeError('target not found')


STEP = {'position': 'bottom-center', 'start_number': 1, 'side_char': 'none', 'confirm_layout': True}
NUMBERING_STEP = {'match': 'Item text', 'expected_page': 1, 'level': 2, 'confirm_layout': True}


class NoCallerBypassTests(unittest.TestCase):
    def test_header_footer_checked_is_not_a_request_key(self) -> None:
        self.assertNotIn('header_footer_checked', nn.PAGE_NUMBER_REQUEST_KEYS)
        for value in (True, False):
            hwp = _body()
            with self.assertRaises(nn.NativeNumberingInputError):
                nn.apply_page_number(hwp, {**POSITION, 'header_footer_checked': value})
            self.assertEqual(hwp.calls, [])

    def test_normalized_request_and_result_do_not_carry_the_bypass(self) -> None:
        self.assertNotIn('header_footer_checked', nn.normalize_page_number_request(POSITION))
        result = nn.apply_page_number(_body(), POSITION)
        self.assertNotIn('header_footer_checked', result)

    def test_step_validation_rejects_the_bypass(self) -> None:
        with self.assertRaises(nn.NativeNumberingInputError):
            nn.validate_page_number_step({**STEP, 'header_footer_checked': True})

    def test_bundle_mixin_rejects_the_bypass_without_native_calls(self) -> None:
        hwp = _body()
        with self.assertRaises(LocalCliRuntimeError):
            _Service()._bundle_page_number_apply(hwp, {**STEP, 'header_footer_checked': True})
        self.assertEqual(hwp.calls, [])

    def test_cli_service_schema_and_package_surfaces_do_not_accept_the_bypass(self) -> None:
        from pydantic import ValidationError

        from app.command_packages.runtime import get_command_package_registry
        from hwpx_mcp import schema as mcp_schema
        from local_cli_v1 import main as cli_main
        from local_cli_v1.bundles import BundleError, CommandStep, build_named_bundle

        self.assertNotIn('header_footer_checked', get_command_package_registry().get('page_number_apply').allowed_keys)
        with self.assertRaises(BundleError):
            CommandStep(op='page_number_apply', label='x', fields={**STEP, 'header_footer_checked': True})
        with self.assertRaises(BundleError):
            build_named_bundle('page-number-apply', ['--position', 'top-left', '--confirm-layout', '--header-footer-checked'])
        with self.assertRaises(SystemExit):
            import contextlib
            import io

            with contextlib.redirect_stderr(io.StringIO()):
                cli_main.build_parser().parse_args(['page-number-apply', '--position', 'top-left', '--confirm-layout', '--header-footer-checked'])
        with self.assertRaises(ValidationError):
            mcp_schema.Command.model_validate({'session_id': 'a' * 32, 'request': {'op': 'page_number_apply', **STEP, 'header_footer_checked': True}})

    def test_service_command_validation_rejects_the_bypass(self) -> None:
        from types import SimpleNamespace

        from app.command_packages.common import validate_step_for_op
        from app.command_packages.runtime import get_command_package_registry
        from app.local_cli_service import LocalCliService, LocalCliServiceError

        step = {'op': 'page_number_apply', 'label': 'x', **STEP, 'header_footer_checked': True}
        with self.assertRaises(LocalCliServiceError):
            LocalCliService._validate_command_bundle_steps(SimpleNamespace(command_packages=get_command_package_registry()), [dict(step)])

        class _Err(Exception):
            def __init__(self, message, *, status_code=400):
                super().__init__(message)

        with self.assertRaises(_Err):
            validate_step_for_op('page_number_apply', service=None, index=1, step=dict(step), manifest={}, error_type=_Err)


class HeaderFooterProtectionTests(unittest.TestCase):
    def _doc_with_header(self) -> FakeHwp:
        hwp = _body(controls=[FakeCtrl('secd'), FakeCtrl('head')])
        hwp.package_state['header_footer'] = ['PROTECTED_HEADER']
        return hwp

    def test_header_present_without_a_protection_reader_is_refused_before_mutation(self) -> None:
        hwp = self._doc_with_header()
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            nn.apply_page_number(hwp, POSITION)
        self.assertEqual(ctx.exception.code, 'header_footer_unprovable')
        self.assertEqual(_mutating_calls(hwp), [])

    def test_header_altered_during_native_call_is_an_unknown_noncomplete_outcome(self) -> None:
        hwp = self._doc_with_header()
        original = hwp.HAction.Execute

        def alter_header(name, hset):
            result = original(name, hset)
            if name == 'PageNumPos':
                hwp.package_state['header_footer'] = ['ALTERED_UNRELATED_HEADER']
            return result

        hwp.HAction.Execute = alter_header
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, POSITION, protection=hwp.census)
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
        self.assertEqual(hwp.package_state['header_footer'], ['ALTERED_UNRELATED_HEADER'])
        self.assertEqual([c.CtrlID for c in hwp.controls], ['secd', 'head', 'pgnp'])
        executes = [call for call in hwp.calls if call[0] == 'Execute']
        self.assertEqual(len(executes), 1)

    def test_styles_numberings_media_and_body_changes_are_unknown_outcomes(self) -> None:
        for field, value in (
            ('styles', 'styles-changed'),
            ('numberings', 'numberings-changed'),
            ('bindata', ['bin-0', 'bin-1']),
        ):
            hwp = self._doc_with_header()
            original = hwp.HAction.Execute

            def mutate(name, hset, _orig=original, _field=field, _value=value, _hwp=hwp):
                result = _orig(name, hset)
                _hwp.package_state[_field] = _value
                return result

            hwp.HAction.Execute = mutate
            with self.assertRaises(nn.NativeNumberingMutationError, msg=field):
                nn.apply_page_number(hwp, POSITION, protection=hwp.census)

    def test_unchanged_header_with_reader_is_the_ordinary_positive_path(self) -> None:
        hwp = self._doc_with_header()
        result = nn.apply_page_number(hwp, POSITION, protection=hwp.census)
        self.assertEqual(result['status'], 'inserted')
        self.assertTrue(result['readback']['available'])
        self.assertTrue(result['protection']['checked'])
        self.assertEqual(hwp.package_state['header_footer'], ['PROTECTED_HEADER'])
        self.assertEqual(result['warnings'], [])

    def test_body_only_document_inserts_without_a_reader(self) -> None:
        hwp = _body()
        result = nn.apply_page_number(hwp, POSITION)
        self.assertEqual(result['status'], 'inserted')
        self.assertFalse(result['protection']['checked'])

    def test_reader_that_cannot_see_the_header_content_refuses_before_mutation(self) -> None:
        hwp = self._doc_with_header()
        hwp.package_state['header_footer'] = []
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            nn.apply_page_number(hwp, POSITION, protection=hwp.census)
        self.assertEqual(ctx.exception.code, 'header_footer_unprovable')
        self.assertEqual(_mutating_calls(hwp), [])

    def test_reader_failure_before_mutation_refuses_and_after_mutation_is_unknown(self) -> None:
        hwp = _body()
        hwp.census_error = RuntimeError('snapshot failed')
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            nn.apply_page_number(hwp, POSITION, protection=hwp.census)
        self.assertEqual(ctx.exception.code, 'protection_unavailable')
        self.assertEqual(_mutating_calls(hwp), [])

        hwp = _body()
        calls = {'n': 0}

        def flaky():
            calls['n'] += 1
            if calls['n'] > 1:
                raise RuntimeError('snapshot failed after')
            return hwp.census()

        with self.assertRaises(nn.NativeNumberingMutationError) as ctx2:
            nn.apply_page_number(hwp, POSITION, protection=flaky)
        self.assertTrue(ctx2.exception.mutation_may_have_persisted)
        self.assertEqual([c.CtrlID for c in hwp.controls], ['secd', 'pgnp'])
        self.assertEqual(len([c for c in hwp.calls if c[0] == 'Execute']), 1)

    def test_bundle_mixin_maps_header_corruption_to_mutation_error(self) -> None:
        hwp = self._doc_with_header()
        original = hwp.HAction.Execute

        def alter_header(name, hset):
            result = original(name, hset)
            hwp.package_state['header_footer'] = ['ALTERED']
            return result

        hwp.HAction.Execute = alter_header
        with self.assertRaises(LocalCliMutationError) as ctx:
            _Service()._bundle_page_number_apply(hwp, dict(STEP), protection=hwp.census)
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})

    def test_bundle_mixin_without_reader_refuses_header_documents_as_runtime_error(self) -> None:
        hwp = self._doc_with_header()
        with self.assertRaises(LocalCliRuntimeError) as ctx:
            _Service()._bundle_page_number_apply(hwp, dict(STEP))
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(_mutating_calls(hwp), [])


class UnknownPostStateTests(unittest.TestCase):
    def test_unreadable_inserted_settings_are_unknown_not_inserted(self) -> None:
        hwp = _body()
        hwp.HAction.page_number_mode = 'insert-unreadable'
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, POSITION)
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
        self.assertEqual([c.CtrlID for c in hwp.controls], ['secd', 'pgnp'])
        self.assertEqual(len([c for c in hwp.calls if c[0] == 'Execute']), 1)

    def test_mistyped_or_missing_readback_values_are_not_proof(self) -> None:
        good = {'DrawPos': 5, 'NewNumber': 1, 'SideChar': 0, 'NumberFormat': 0}
        bad_values = (
            ('DrawPos', True), ('DrawPos', 5.0), ('DrawPos', '5'), ('DrawPos', None), ('DrawPos', -1), ('DrawPos', 256),
            ('NewNumber', True), ('NewNumber', 1.0), ('NewNumber', '1'), ('NewNumber', 65536),
            ('SideChar', False), ('SideChar', 0.0), ('SideChar', '0'),
            ('NumberFormat', False), ('NumberFormat', 0.0), ('NumberFormat', '0'),
        )
        for key, value in bad_values:
            hwp = _body()
            original = hwp.HAction.Execute

            def run(name, hset, _orig=original, _hwp=hwp, _key=key, _value=value):
                result = _orig(name, hset)
                if name == 'PageNumPos':
                    _hwp.controls[-1].Properties = FakeProps({**good, _key: _value})
                return result

            hwp.HAction.Execute = run
            with self.assertRaises(nn.NativeNumberingMutationError, msg=(key, value)):
                nn.apply_page_number(hwp, POSITION)
        hwp = _body()
        original = hwp.HAction.Execute

        def drop_key(name, hset):
            result = original(name, hset)
            if name == 'PageNumPos':
                hwp.controls[-1].Properties = FakeProps({k: v for k, v in good.items() if k != 'SideChar'})
            return result

        hwp.HAction.Execute = drop_key
        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_page_number(hwp, POSITION)

    def test_existing_pgnp_with_mistyped_values_is_refused_not_matched(self) -> None:
        values = {'DrawPos': True, 'NewNumber': 1, 'SideChar': 0, 'NumberFormat': 0}
        hwp = _body(controls=[FakeCtrl('secd'), FakeCtrl('pgnp', FakeProps(values))])
        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_page_number(hwp, {'position': 'top-left'})
        self.assertEqual(_mutating_calls(hwp), [])

    def test_native_false_and_exception_are_unknown_without_cleanup(self) -> None:
        hwp = _body()
        hwp.HAction.execute_result = False
        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_page_number(hwp, POSITION)
        self.assertEqual(len([c for c in hwp.calls if c[0] == 'Execute']), 1)

        hwp = _body()
        hwp.HAction.execute_raises = RuntimeError('com error')
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, POSITION)
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(len([c for c in hwp.calls if c[0] == 'Execute']), 1)

    def test_post_readback_failure_keeps_the_new_control(self) -> None:
        hwp = _body()
        original = hwp.HAction.Execute

        def break_enumeration(name, hset):
            result = original(name, hset)
            hwp.enumeration_available = False
            return result

        hwp.HAction.Execute = break_enumeration
        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_page_number(hwp, POSITION)
        self.assertEqual([c.CtrlID for c in hwp.controls], ['secd', 'pgnp'])

    def test_bundle_mixin_maps_unreadable_settings_to_a_mutation_error(self) -> None:
        hwp = _body()
        hwp.HAction.page_number_mode = 'insert-unreadable'
        with self.assertRaises(LocalCliMutationError) as ctx:
            _Service()._bundle_page_number_apply(hwp, dict(STEP))
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})

    def test_bundle_mixin_maps_native_false_and_exception(self) -> None:
        for attr, value in (('execute_result', False), ('execute_raises', RuntimeError('boom'))):
            hwp = _body()
            setattr(hwp.HAction, attr, value)
            with self.assertRaises(LocalCliMutationError, msg=attr) as ctx:
                _Service()._bundle_page_number_apply(hwp, dict(STEP))
            self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_ordinary_positive_control_still_reports_verified_values(self) -> None:
        hwp = _body()
        result = _Service()._bundle_page_number_apply(hwp, {**STEP, 'position': 'top-right', 'start_number': 7, 'side_char': 'dash'})
        self.assertEqual(result['result']['status'], 'inserted')
        self.assertEqual(result['result']['readback']['values'], {'DrawPos': 3, 'NewNumber': 7, 'SideChar': 45, 'NumberFormat': 0})


class OfficialFormatTests(unittest.TestCase):
    def _doc(self, shape_levels=None, extra=None):
        hwp = _body(paragraphs=_paragraphs(*(extra or []), ('Title', 0, 0, 0), ('Item text', 0, 0, 0), ('Tail', 0, 0, 0)))
        if shape_levels is not None:
            hwp.outline_shape = {**shape_items(shape_levels), **TAIL}
        return hwp

    def _target_index(self, hwp):
        return next(i for i, p in enumerate(hwp.paragraphs) if p['text'] == 'Item text')

    def _official_in(self, hwp) -> bool:
        return all(hwp.outline_shape[k] == v for k, v in OFFICIAL.items())

    def _outline_executes(self, hwp):
        return [c for c in hwp.calls if c[0] == 'Execute' and c[1] == 'OutlineNumber']

    def test_stock_definition_matching_the_official_levels_is_verified_and_reused(self) -> None:
        for level in (1, 2, 3, 4):
            hwp = self._doc()
            result = nn.apply_official_numbering_at_match(hwp, find='Item text', level=level, protection=hwp.census)
            self.assertEqual(self._outline_executes(hwp), [], msg=level)
            self.assertIs(result['numbering_format_enforced'], True)
            self.assertEqual(result['numbering_format']['status'], 'verified_existing')
            self.assertEqual(hwp.paragraphs[self._target_index(hwp)]['level'], level - 1)

    def test_all_four_levels_with_non_default_input_formats_are_redefined_in_the_current_section(self) -> None:
        for level in (1, 2, 3, 4):
            hwp = self._doc(FOREIGN_LEVELS)
            other_before = dict(hwp.other_section_shape)
            result = nn.apply_official_numbering_at_match(hwp, find='Item text', level=level, protection=hwp.census)
            executes = self._outline_executes(hwp)
            self.assertEqual(len(executes), 1, msg=level)
            self.assertEqual(executes[0][2]['ApplyTo'], 2, msg=level)
            # ParameterSetTable_2504 SecDef: ApplyClass must carry the flag of the chosen ApplyTo (2 -> 0x04).
            self.assertEqual(executes[0][2]['ApplyClass'], {2: 0x04, 3: 0x08, 4: 0x10}[executes[0][2]['ApplyTo']], msg=level)
            self.assertTrue(self._official_in(hwp), msg=level)
            self.assertEqual({k: hwp.outline_shape[k] for k in TAIL}, TAIL)
            self.assertEqual(hwp.other_section_shape, other_before)
            self.assertIs(result['numbering_format_enforced'], True)
            self.assertEqual(result['numbering_format']['status'], 'defined_in_current_section')
            self.assertEqual(hwp.paragraphs[self._target_index(hwp)]['level'], level - 1)
            self.assertEqual(hwp.paragraphs[self._target_index(hwp)]['heading_type'], 1)

    def test_each_single_level_difference_is_detected(self) -> None:
        for index in range(4):
            for item, wrong in ((f'StrFormatLevel{index}', 'zz'), (f'NumFormatLevel{index}', 11)):
                hwp = self._doc()
                hwp.outline_shape[item] = wrong
                nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)
                self.assertEqual(len(self._outline_executes(hwp)), 1, msg=item)
                self.assertTrue(self._official_in(hwp), msg=item)

    def test_foreign_definition_used_by_other_outline_paragraphs_in_the_section_is_refused(self) -> None:
        hwp = self._doc(FOREIGN_LEVELS, extra=[('Other heading', 1, 0, 0)])
        before = dict(hwp.outline_shape)
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)
        self.assertEqual(ctx.exception.code, 'foreign_outline_definition')
        self.assertEqual(_mutating_calls(hwp), [])
        self.assertEqual(hwp.outline_shape, before)

    def test_foreign_outline_paragraph_in_another_section_is_left_alone(self) -> None:
        hwp = self._doc(FOREIGN_LEVELS, extra=[('Other section heading', 1, 0, 1)])
        shapes_before = (dict(hwp.outline_shape), dict(hwp.other_section_shape))
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)
        self.assertEqual(ctx.exception.code, 'outline_usage_unprovable')
        self.assertEqual(_mutating_calls(hwp), [])
        self.assertEqual((hwp.outline_shape, hwp.other_section_shape), shapes_before)
        other = next(p for p in hwp.paragraphs if p['text'] == 'Other section heading')
        self.assertEqual((other['heading_type'], other['level']), (1, 0))

    def test_numbered_and_bulleted_paragraphs_are_untouched_by_a_redefinition(self) -> None:
        extra = [('Numbered', 2, 1, 0), ('Bullet', 3, 0, 0)]
        hwp = self._doc(FOREIGN_LEVELS, extra=extra)
        before = [dict(p) for p in hwp.paragraphs if p['text'] in {'Numbered', 'Bullet', 'Title', 'Tail'}]

        def census_with_independent_numbering_proof():
            value = hwp.census()
            for item in value['paragraphs']:
                item['shares_outline_numbering'] = False
            return value

        nn.apply_official_numbering_at_match(hwp, find='Item text', level=3, protection=census_with_independent_numbering_proof)
        after = [dict(p) for p in hwp.paragraphs if p['text'] in {'Numbered', 'Bullet', 'Title', 'Tail'}]
        self.assertEqual(after, before)
        self.assertEqual(hwp.package_state['numberings'], 'numberings-0')

    def test_redefinition_without_a_usage_reader_is_refused_before_mutation(self) -> None:
        hwp = self._doc(FOREIGN_LEVELS)
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            nn.apply_official_numbering_at_match(hwp, find='Item text', level=1)
        self.assertEqual(ctx.exception.code, 'outline_usage_unprovable')
        self.assertEqual(_mutating_calls(hwp), [])

    def test_silent_noop_or_partial_definition_is_an_unknown_outcome_never_enforced(self) -> None:
        for mode in ('noop', 'partial'):
            hwp = self._doc(FOREIGN_LEVELS)
            hwp.outline_mode = mode
            with self.assertRaises(nn.NativeNumberingMutationError, msg=mode) as ctx:
                nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)
            self.assertTrue(ctx.exception.mutation_may_have_persisted)
            self.assertEqual(len(self._outline_executes(hwp)), 1, msg=mode)

    def test_outline_execute_false_or_exception_is_unknown_without_cleanup(self) -> None:
        for attr, value in (('execute_result', False), ('execute_raises', RuntimeError('com'))):
            hwp = self._doc(FOREIGN_LEVELS)
            setattr(hwp.HAction, attr, value)
            with self.assertRaises(nn.NativeNumberingMutationError, msg=attr):
                nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)
            self.assertEqual(len(self._outline_executes(hwp)), 1)

    def test_unreadable_or_mistyped_definition_refuses_before_mutation(self) -> None:
        for readback in ('missing', 'bool', 'str', 'float'):
            hwp = self._doc()
            hwp.outline_readback = readback
            with self.assertRaises(nn.NativeNumberingRefusal, msg=readback) as ctx:
                nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)
            self.assertEqual(ctx.exception.code, 'outline_definition_unreadable', msg=readback)
            self.assertEqual(_mutating_calls(hwp), [], msg=readback)
        hwp = self._doc()
        hwp.outline_unreadable = True
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx2:
            nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)
        self.assertEqual(ctx2.exception.code, 'outline_definition_unreadable')
        self.assertEqual(_mutating_calls(hwp), [])

    def test_post_definition_readback_failure_is_unknown(self) -> None:
        hwp = self._doc(FOREIGN_LEVELS)
        original = hwp.HAction.Execute

        def break_readback(name, hset):
            result = original(name, hset)
            if name == 'OutlineNumber':
                hwp.outline_unreadable = True
            return result

        hwp.HAction.Execute = break_readback
        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)

    def test_reused_definition_with_existing_level_is_already_applied_and_enforced(self) -> None:
        hwp = self._doc()
        hwp.paragraphs[1].update(heading_type=1, level=1)
        result = nn.apply_official_numbering_at_match(hwp, find='Item text', level=2, protection=hwp.census)
        self.assertEqual(result['status'], 'already_applied')
        self.assertIs(result['numbering_format_enforced'], True)
        self.assertEqual(_mutating_calls(hwp), [])

    def test_unrelated_header_or_media_changes_during_numbering_are_unknown(self) -> None:
        hwp = self._doc()
        hwp.package_state['header_footer'] = ['H']
        hwp.controls = [FakeCtrl('secd'), FakeCtrl('head')]
        original = hwp.HAction.Execute

        def alter(name, hset):
            result = original(name, hset)
            hwp.package_state['header_footer'] = ['ALTERED']
            return result

        hwp.HAction.Execute = alter
        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)

    def test_other_paragraph_changes_during_numbering_are_unknown(self) -> None:
        hwp = self._doc()
        original = hwp.HAction.Execute

        def renumber_tail(name, hset):
            result = original(name, hset)
            hwp.paragraphs[2]['heading_type'] = 2
            return result

        hwp.HAction.Execute = renumber_tail
        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_official_numbering_at_match(hwp, find='Item text', level=1, protection=hwp.census)

    def test_bundle_mixin_reports_enforced_format_and_maps_unknown_outcomes(self) -> None:
        from tests.test_numbering_guard_read_order import complete_protection
        hwp = self._doc(FOREIGN_LEVELS)
        result = _Service()._bundle_official_numbering_apply(hwp, dict(NUMBERING_STEP), protection=complete_protection(hwp))
        self.assertIs(result['numbering_format_enforced'], True)
        self.assertTrue(self._official_in(hwp))
        self.assertFalse(any('not set or verified' in w for w in result['warnings']))

        hwp = self._doc(FOREIGN_LEVELS)
        hwp.outline_mode = 'noop'
        with self.assertRaises(LocalCliMutationError) as ctx:
            _Service()._bundle_official_numbering_apply(hwp, dict(NUMBERING_STEP), protection=complete_protection(hwp))
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

        hwp = self._doc(FOREIGN_LEVELS, extra=[('Other heading', 1, 0, 0)])
        with self.assertRaises(LocalCliRuntimeError) as ctx2:
            _Service()._bundle_official_numbering_apply(hwp, dict(NUMBERING_STEP), protection=complete_protection(hwp))
        self.assertNotIsInstance(ctx2.exception, LocalCliMutationError)
        self.assertEqual(_mutating_calls(hwp), [])

    def test_official_level_table_is_the_documented_four_level_system(self) -> None:
        self.assertEqual(
            [(level['str_format'], level['num_format']) for level in nn.OFFICIAL_OUTLINE_LEVELS],
            [('^1.', 0), ('^2.', 8), ('^3)', 0), ('^4)', 8)],
        )


if __name__ == '__main__':
    unittest.main()
