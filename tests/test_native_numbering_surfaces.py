from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest import mock

from pydantic import ValidationError

from app import native_numbering as nn
from app.command_packages.common import validate_step_for_op
from app.command_packages.runtime import get_command_package_registry
from app.local_cli_bundle_numbering import LocalCliBundleNumberingMixin
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service import LocalCliService, LocalCliServiceError
from app.local_cli_service_support import _BUNDLE_ALLOWED_OPS, LocalCliMutationError
from hwpx_mcp import schema as mcp_schema
from local_cli_v1 import main as cli_main
from local_cli_v1.bundles import (
    BUNDLE_SERVER_OPS,
    BundleError,
    CommandStep,
    build_named_bundle,
    bundle_help,
    recipe_names,
)
from tests.native_numbering_fakes import FakeCtrl, FakeHwp

SESSION = 'a' * 32
PAGE_ARGS = ['--position', 'bottom-center', '--start-number', '3', '--side-char', 'dash', '--confirm-layout']
NUMBERING_ARGS = ['--match', 'Item text', '--expected-page', '2', '--level', '3', '--confirm-layout']
PAGE_FIELDS = {
    'position': 'bottom-center',
    'start_number': 3,
    'side_char': 'dash',
    'confirm_layout': True,
}
NUMBERING_FIELDS = {'match': 'Item text', 'expected_page': 2, 'level': 3, 'confirm_layout': True}


def _mutating_step(spec):
    steps = spec.server_payload()['steps']
    return steps[1]


class _ServiceError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class BundleBuilderTests(unittest.TestCase):
    def test_ops_and_recipes_are_registered(self) -> None:
        for op in ('page_number_apply', 'official_numbering_apply'):
            self.assertIn(op, BUNDLE_SERVER_OPS)
            self.assertIn(op, _BUNDLE_ALLOWED_OPS)
        for name in ('page-number-apply', 'official-numbering-apply'):
            self.assertIn(name, recipe_names())
            self.assertTrue(bundle_help(name))

    def test_page_number_bundle_wraps_one_mutation_with_before_after_where(self) -> None:
        spec = build_named_bundle('page-number-apply', PAGE_ARGS)
        steps = spec.server_payload()['steps']
        self.assertEqual([step['op'] for step in steps], ['where', 'page_number_apply', 'where'])
        mutate = steps[1]
        self.assertEqual({k: v for k, v in mutate.items() if k not in {'op', 'label'}}, PAGE_FIELDS)

    def test_page_number_defaults_are_emitted_explicitly(self) -> None:
        spec = build_named_bundle('page-number-apply', ['--position', 'top-left', '--confirm-layout'])
        mutate = _mutating_step(spec)
        self.assertEqual(mutate['start_number'], 1)
        self.assertEqual(mutate['side_char'], 'none')
        self.assertNotIn('header_footer_checked', mutate)

    def test_bundle_descriptions_state_the_supported_contract(self) -> None:
        page = build_named_bundle('page-number-apply', PAGE_ARGS)
        numbering = build_named_bundle('official-numbering-apply', NUMBERING_ARGS)
        self.assertNotIn('not enforced', numbering.changed)
        self.assertIn('1.', numbering.changed)
        self.assertIn('verif', numbering.how.lower() + numbering.changed.lower())
        self.assertNotIn('are not edited', page.changed)
        self.assertIn('fingerprint', (page.how + page.changed).lower())
        for text in (bundle_help('page-number-apply'), bundle_help('official-numbering-apply')):
            self.assertNotIn('header_footer_checked', text)
            self.assertNotIn('header-footer-checked', text)

    def test_all_six_positions_build(self) -> None:
        for position in ('top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right'):
            spec = build_named_bundle('page-number-apply', ['--position', position, '--confirm-layout'])
            self.assertEqual(_mutating_step(spec)['position'], position)

    def test_dash_prefix_suffix_pair_builds_as_side_char_dash(self) -> None:
        spec = build_named_bundle('page-number-apply', ['--position', 'bottom-center', '--prefix', '-', '--suffix', '-', '--confirm-layout'])
        self.assertEqual(_mutating_step(spec)['side_char'], 'dash')

    def test_page_number_builder_refuses_bad_input(self) -> None:
        base = ['--position', 'bottom-center', '--confirm-layout']
        bad_cases = (
            ['--confirm-layout'],
            ['--position', 'bottom-center'],
            ['--position', 'middle', '--confirm-layout'],
            ['--position', 'outside-top', '--confirm-layout'],
            base + ['--start-number', '0'],
            base + ['--start-number', '65536'],
            base + ['--start-number', '-1'],
            base + ['--start-number', 'x'],
            base + ['--start-number', '1.5'],
            base + ['--prefix', 'Page '],
            base + ['--suffix', ' 쪽'],
            base + ['--prefix', '-'],
            base + ['--side-char', 'bold'],
            base + ['--side-char', 'none', '--prefix', '-', '--suffix', '-'],
            base + ['--unknown-flag', 'x'],
        )
        for argv in bad_cases:
            with self.assertRaises(BundleError, msg=repr(argv)):
                build_named_bundle('page-number-apply', argv)

    def test_official_numbering_bundle_wraps_one_mutation(self) -> None:
        spec = build_named_bundle('official-numbering-apply', NUMBERING_ARGS)
        steps = spec.server_payload()['steps']
        self.assertEqual([step['op'] for step in steps], ['where', 'official_numbering_apply', 'where'])
        self.assertEqual({k: v for k, v in steps[1].items() if k not in {'op', 'label'}}, NUMBERING_FIELDS)

    def test_official_numbering_builder_covers_four_levels_and_refuses_the_rest(self) -> None:
        for level in ('1', '2', '3', '4'):
            spec = build_named_bundle('official-numbering-apply', ['--match', 'x', '--expected-page', '1', '--level', level, '--confirm-layout'])
            self.assertEqual(_mutating_step(spec)['level'], int(level))
        for level in ('0', '5', '-1', 'two', '1.5'):
            with self.assertRaises(BundleError, msg=level):
                build_named_bundle('official-numbering-apply', ['--match', 'x', '--expected-page', '1', '--level', level, '--confirm-layout'])
        for argv in (
            ['--expected-page', '1', '--level', '1', '--confirm-layout'],
            ['--match', 'x', '--level', '1', '--confirm-layout'],
            ['--match', 'x', '--expected-page', '1', '--confirm-layout'],
            ['--match', 'x', '--expected-page', '1', '--level', '1'],
            ['--match', 'x', '--expected-page', '0', '--level', '1', '--confirm-layout'],
            ['--match', '  ', '--expected-page', '1', '--level', '1', '--confirm-layout'],
            ['--match', 'x', '--expected-page', '1', '--level', '1', '--confirm-layout', '--text', 'extra'],
        ):
            with self.assertRaises(BundleError, msg=repr(argv)):
                build_named_bundle('official-numbering-apply', argv)

    def test_command_step_rejects_unknown_keys_for_the_new_ops(self) -> None:
        with self.assertRaises(BundleError):
            CommandStep(op='page_number_apply', label='x', fields={**PAGE_FIELDS, 'number_format': 'roman'})
        with self.assertRaises(BundleError):
            CommandStep(op='official_numbering_apply', label='x', fields={**NUMBERING_FIELDS, 'format': 'x'})
        CommandStep(op='page_number_apply', label='x', fields=dict(PAGE_FIELDS))
        CommandStep(op='official_numbering_apply', label='x', fields=dict(NUMBERING_FIELDS))

    def test_builder_and_native_normalizer_agree_on_page_number_inputs(self) -> None:
        cases = [
            (['--position', 'top-left', '--confirm-layout'], {'position': 'top-left'}),
            (['--position', 'bottom-right', '--start-number', '65535', '--confirm-layout'], {'position': 'bottom-right', 'start_number': 65535}),
            (['--position', 'bottom-right', '--start-number', '65536', '--confirm-layout'], {'position': 'bottom-right', 'start_number': 65536}),
            (['--position', 'bottom-right', '--start-number', '0', '--confirm-layout'], {'position': 'bottom-right', 'start_number': 0}),
            (['--position', 'top-center', '--prefix', 'p', '--confirm-layout'], {'position': 'top-center', 'prefix': 'p'}),
            (['--position', 'top-center', '--side-char', 'dash', '--confirm-layout'], {'position': 'top-center', 'side_char': 'dash'}),
        ]
        for argv, request in cases:
            try:
                build_named_bundle('page-number-apply', argv)
                builder_ok = True
            except BundleError:
                builder_ok = False
            try:
                nn.normalize_page_number_request(request)
                native_ok = True
            except nn.NativeNumberingInputError:
                native_ok = False
            self.assertEqual(builder_ok, native_ok, msg=repr(argv))


class CliParserAndDispatchTests(unittest.TestCase):
    def _run(self, argv: list[str]):
        calls: dict[str, object] = {}

        def fake_execute(base_url, bundle_name, bundle_args=None, **_kw):
            calls['bundle'] = (bundle_name, list(bundle_args or []))
            spec = build_named_bundle(bundle_name, bundle_args or [])
            calls['payload'] = spec.server_payload()
            return spec, {'ok': True, 'summary': 'fixture', 'steps': []}

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli_main, 'load_state', lambda path=None: {'base_url': 'http://fixture.local'}), \
                mock.patch.object(cli_main, '_execute_named_bundle', fake_execute), \
                mock.patch.object(cli_main, '_print_bundle_payload', lambda payload, json_output=False: None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main.main(argv)
        return rc, calls, out.getvalue(), err.getvalue()

    def test_parser_accepts_page_number_apply(self) -> None:
        args = cli_main.build_parser().parse_args(['page-number-apply', *PAGE_ARGS])
        self.assertEqual(args.command, 'page-number-apply')
        self.assertEqual(args.position, 'bottom-center')
        self.assertEqual(args.start_number, 3)
        self.assertEqual(args.side_char, 'dash')
        self.assertFalse(hasattr(args, 'header_footer_checked'))
        self.assertTrue(args.confirm_layout)

    def test_parser_accepts_official_numbering_apply(self) -> None:
        args = cli_main.build_parser().parse_args(['official-numbering-apply', *NUMBERING_ARGS])
        self.assertEqual(args.command, 'official-numbering-apply')
        self.assertEqual((args.match, args.expected_page, args.level), ('Item text', 2, 3))
        self.assertTrue(args.confirm_layout)

    def test_parser_requires_confirmation_and_targets(self) -> None:
        parser = cli_main.build_parser()
        for argv in (
            ['page-number-apply', '--position', 'top-left'],
            ['page-number-apply', '--confirm-layout'],
            ['official-numbering-apply', '--match', 'x', '--expected-page', '1', '--level', '1'],
            ['official-numbering-apply', '--match', 'x', '--level', '1', '--confirm-layout'],
        ):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit, msg=repr(argv)):
                parser.parse_args(argv)

    def test_command_status_lists_both_as_bundle_backed_and_not_safe_schema(self) -> None:
        status = cli_main.build_command_status()
        for name in ('page-number-apply', 'official-numbering-apply'):
            self.assertEqual(status[name]['status'], 'bundle-backed', name)
            self.assertIn('rendered proof', status[name]['note'])
        from local_cli_v1.safe_schema import build_safe_agent_schema

        denied = build_safe_agent_schema(status)['denied_parser_commands']
        self.assertIn('page-number-apply', denied)
        self.assertIn('official-numbering-apply', denied)

    def test_page_number_dispatch_builds_the_bundle_payload(self) -> None:
        rc, calls, _out, _err = self._run(['page-number-apply', *PAGE_ARGS, '--json'])
        self.assertEqual(rc, 0)
        self.assertEqual(calls['bundle'][0], 'page-number-apply')
        mutate = calls['payload']['steps'][1]
        self.assertEqual({k: v for k, v in mutate.items() if k not in {'op', 'label'}}, PAGE_FIELDS)

    def test_official_numbering_dispatch_builds_the_bundle_payload(self) -> None:
        rc, calls, _out, _err = self._run(['official-numbering-apply', *NUMBERING_ARGS, '--json'])
        self.assertEqual(rc, 0)
        self.assertEqual(calls['bundle'][0], 'official-numbering-apply')
        mutate = calls['payload']['steps'][1]
        self.assertEqual({k: v for k, v in mutate.items() if k not in {'op', 'label'}}, NUMBERING_FIELDS)

    def test_dispatch_with_invalid_values_does_not_reach_the_server(self) -> None:
        posted: list[object] = []
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli_main, 'load_state', lambda path=None: {'base_url': 'http://fixture.local'}), \
                mock.patch.object(cli_main, 'post_json', lambda *a, **k: posted.append((a, k))), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli_main.main(['page-number-apply', '--position', 'bottom-center', '--start-number', '0', '--confirm-layout'])
        self.assertNotEqual(rc, 0)
        self.assertEqual(posted, [])


class McpSchemaParityTests(unittest.TestCase):
    def _command(self, request: dict):
        return mcp_schema.Command.model_validate({'session_id': SESSION, 'request': request})

    def test_page_number_request_round_trips_to_the_cli_payload_fields(self) -> None:
        model = self._command({'op': 'page_number_apply', **PAGE_FIELDS})
        dumped = model.request.model_dump(exclude_none=True)
        fields = {k: v for k, v in dumped.items() if k not in {'op', 'label'}}
        self.assertEqual(fields, PAGE_FIELDS)
        step = CommandStep(op=dumped['op'], label=dumped['label'], fields=fields)
        cli_step = _mutating_step(build_named_bundle('page-number-apply', PAGE_ARGS))
        self.assertEqual({k: v for k, v in step.to_server_json().items() if k != 'label'}, {k: v for k, v in cli_step.items() if k != 'label'})

    def test_page_number_defaults_match_the_cli_defaults(self) -> None:
        dumped = self._command({'op': 'page_number_apply', 'position': 'top-left', 'confirm_layout': True}).request.model_dump(exclude_none=True)
        cli_step = _mutating_step(build_named_bundle('page-number-apply', ['--position', 'top-left', '--confirm-layout']))
        self.assertEqual({k: v for k, v in dumped.items() if k != 'label'}, {k: v for k, v in cli_step.items() if k != 'label'})

    def test_official_numbering_request_round_trips_to_the_cli_payload_fields(self) -> None:
        model = self._command({'op': 'official_numbering_apply', **NUMBERING_FIELDS})
        dumped = model.request.model_dump(exclude_none=True)
        fields = {k: v for k, v in dumped.items() if k not in {'op', 'label'}}
        self.assertEqual(fields, NUMBERING_FIELDS)
        CommandStep(op=dumped['op'], label=dumped['label'], fields=fields)

    def test_page_number_schema_is_strict(self) -> None:
        base = {'op': 'page_number_apply', **PAGE_FIELDS}
        bad_overrides = (
            {'start_number': True}, {'start_number': 0}, {'start_number': 65536}, {'start_number': '3'}, {'start_number': 1.0},
            {'position': 'center'}, {'position': 'outside-top'}, {'position': 5},
            {'side_char': 'bold'}, {'side_char': 1},
            {'header_footer_checked': True}, {'header_footer_checked': False}, {'header_footer_checked': 1},
            {'confirm_layout': False}, {'confirm_layout': 1},
            {'number_format': 'roman'},
        )
        for override in bad_overrides:
            with self.assertRaises(ValidationError, msg=repr(override)):
                self._command({**base, **override})
        missing = dict(base)
        del missing['position']
        with self.assertRaises(ValidationError):
            self._command(missing)
        missing = dict(base)
        del missing['confirm_layout']
        with self.assertRaises(ValidationError):
            self._command(missing)

    def test_official_numbering_schema_is_strict(self) -> None:
        base = {'op': 'official_numbering_apply', **NUMBERING_FIELDS}
        bad_overrides = (
            {'level': 0}, {'level': 5}, {'level': True}, {'level': '2'}, {'level': 2.0},
            {'expected_page': 0}, {'expected_page': True}, {'expected_page': '1'},
            {'match': ''}, {'confirm_layout': False}, {'format': '1.'}, {'text': 'x'},
        )
        for override in bad_overrides:
            with self.assertRaises(ValidationError, msg=repr(override)):
                self._command({**base, **override})
        for key in ('match', 'expected_page', 'level', 'confirm_layout'):
            missing = dict(base)
            del missing[key]
            with self.assertRaises(ValidationError, msg=key):
                self._command(missing)


class ServiceValidationParityTests(unittest.TestCase):
    def _service_validate(self, step: dict):
        stub = SimpleNamespace(command_packages=get_command_package_registry())
        return LocalCliService._validate_command_bundle_steps(stub, [step])

    def test_command_packages_exist_for_both_ops(self) -> None:
        registry = get_command_package_registry()
        for op, keys in (('page_number_apply', set(PAGE_FIELDS)), ('official_numbering_apply', set(NUMBERING_FIELDS))):
            package = registry.get(op)
            self.assertIsNotNone(package, op)
            self.assertFalse(package.read_only)
            self.assertTrue(package.dirty_default)
            self.assertTrue(keys | {'op', 'label'} <= package.allowed_keys)

    def test_valid_steps_pass_both_validators(self) -> None:
        for op, fields in (('page_number_apply', PAGE_FIELDS), ('official_numbering_apply', NUMBERING_FIELDS)):
            step = {'op': op, 'label': 'x', **fields}
            self.assertEqual(validate_step_for_op(op, service=None, index=1, step=dict(step), manifest={}, error_type=_ServiceError)['op'], op)
            self.assertEqual(self._service_validate(dict(step))[0]['op'], op)

    def test_invalid_page_number_steps_are_rejected_identically(self) -> None:
        base = {'op': 'page_number_apply', 'label': 'x', **PAGE_FIELDS}
        cases = (
            {'start_number': True}, {'start_number': 0}, {'start_number': 65536}, {'start_number': '3'},
            {'position': 'center'}, {'position': None}, {'side_char': 'bold'},
            {'header_footer_checked': True}, {'header_footer_checked': False}, {'header_footer_checked': 1},
            {'confirm_layout': False}, {'confirm_layout': 1},
            {'prefix': 'Page '}, {'suffix': ' 쪽'},
        )
        for override in cases:
            step = {**base, **override}
            with self.assertRaises(_ServiceError, msg=repr(override)):
                validate_step_for_op('page_number_apply', service=None, index=1, step=dict(step), manifest={}, error_type=_ServiceError)
            with self.assertRaises(LocalCliServiceError, msg=repr(override)):
                self._service_validate(dict(step))
        with self.assertRaises(LocalCliServiceError):
            self._service_validate({**base, 'unknown': 1})
        without = dict(base)
        del without['confirm_layout']
        with self.assertRaises(LocalCliServiceError):
            self._service_validate(without)

    def test_invalid_official_numbering_steps_are_rejected_identically(self) -> None:
        base = {'op': 'official_numbering_apply', 'label': 'x', **NUMBERING_FIELDS}
        cases = (
            {'level': 0}, {'level': 5}, {'level': True}, {'level': '2'}, {'level': None},
            {'expected_page': 0}, {'expected_page': True}, {'match': ''}, {'match': 'x' * 501},
            {'confirm_layout': False}, {'confirm_layout': 1},
        )
        for override in cases:
            step = {**base, **override}
            with self.assertRaises(_ServiceError, msg=repr(override)):
                validate_step_for_op('official_numbering_apply', service=None, index=1, step=dict(step), manifest={}, error_type=_ServiceError)
            with self.assertRaises(LocalCliServiceError, msg=repr(override)):
                self._service_validate(dict(step))


class _NumberingService(LocalCliBundleNumberingMixin):
    def __init__(self, expected_page_ok: bool = True) -> None:
        self.expected_page_ok = expected_page_ok
        self.finder_calls: list[tuple[str, int]] = []

    def _find_text_on_expected_page(self, hwp, *, match, expected_page):
        self.finder_calls.append((match, expected_page))
        if not self.expected_page_ok:
            raise LocalCliRuntimeError('target not found on expected page')
        for index, para in enumerate(hwp.paragraphs):
            if match in str(para['text']):
                hwp.cursor = (0, index, 0)
                return {'page': expected_page, 'pos': [0, index, 0]}
        raise LocalCliRuntimeError('target not found')


def _doc(**kwargs) -> FakeHwp:
    paragraphs = kwargs.pop('paragraphs', [
        {'text': 'Title', 'heading_type': 0, 'level': 0},
        {'text': 'Item text', 'heading_type': 0, 'level': 0},
        {'text': 'Tail', 'heading_type': 0, 'level': 0},
    ])
    controls = kwargs.pop('controls', [FakeCtrl('secd'), FakeCtrl('tbl')])
    return FakeHwp(paragraphs=paragraphs, controls=controls, **kwargs)


def _mutations(hwp: FakeHwp) -> list[tuple]:
    return [call for call in hwp.calls if call[0] in {'Execute', 'Run'}]


class BundleMixinTests(unittest.TestCase):
    def test_page_number_apply_inserts_and_reports_a_mutation_result(self) -> None:
        hwp = _doc()
        result = _NumberingService()._bundle_page_number_apply(hwp, dict(PAGE_FIELDS))
        self.assertEqual([c.CtrlID for c in hwp.controls], ['secd', 'tbl', 'pgnp'])
        self.assertIs(result['read_only'], False)
        self.assertEqual(result['mutation'], 'page-number-apply')
        self.assertTrue(result['warnings'])
        self.assertEqual(result['result']['status'], 'inserted')

    def test_page_number_apply_requires_confirm_layout(self) -> None:
        hwp = _doc()
        with self.assertRaises(LocalCliRuntimeError):
            _NumberingService()._bundle_page_number_apply(hwp, {**PAGE_FIELDS, 'confirm_layout': False})
        self.assertEqual(hwp.calls, [])

    def test_page_number_invalid_input_is_a_runtime_error_with_zero_native_calls(self) -> None:
        hwp = _doc()
        for override in ({'start_number': 0}, {'position': 'nowhere'}, {'prefix': 'Page '}):
            with self.assertRaises(LocalCliRuntimeError) as ctx:
                _NumberingService()._bundle_page_number_apply(hwp, {**PAGE_FIELDS, **override})
            self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(hwp.calls, [])

    def test_page_number_refusal_on_existing_different_number_is_not_a_mutation_error(self) -> None:
        from tests.native_numbering_fakes import FakeProps

        hwp = _doc(controls=[FakeCtrl('secd'), FakeCtrl('pgnp', FakeProps({'DrawPos': 1, 'NewNumber': 1, 'SideChar': 0, 'NumberFormat': 0}))])
        with self.assertRaises(LocalCliRuntimeError) as ctx:
            _NumberingService()._bundle_page_number_apply(hwp, dict(PAGE_FIELDS))
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(_mutations(hwp), [])

    def test_page_number_unknown_outcome_maps_to_mutation_error(self) -> None:
        hwp = _doc()
        hwp.HAction.page_number_mode = 'noop'
        with self.assertRaises(LocalCliMutationError) as ctx:
            _NumberingService()._bundle_page_number_apply(hwp, dict(PAGE_FIELDS))
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
        self.assertNotIn('no mutation', str(ctx.exception).lower())

    def test_official_numbering_apply_numbers_only_the_matched_paragraph(self) -> None:
        hwp = _doc()
        service = _NumberingService()
        result = service._bundle_official_numbering_apply(hwp, dict(NUMBERING_FIELDS))
        self.assertEqual(service.finder_calls, [('Item text', 2)])
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertEqual([p['level'] for p in hwp.paragraphs], [0, 2, 0])
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Item text', 'Tail'])
        self.assertIs(result['read_only'], False)
        self.assertEqual(result['mutation'], 'official-numbering-apply')
        self.assertIs(result['numbering_format_enforced'], True)
        self.assertEqual(result['numbering_format']['status'], 'verified_existing')
        self.assertEqual(len(result['numbering_format']['levels']), 4)
        self.assertEqual(result['result']['numbering_format_enforced'], True)

    def test_official_numbering_zero_mutation_when_target_is_not_provable(self) -> None:
        hwp = _doc(paragraphs=[{'text': 'Item text', 'heading_type': 0, 'level': 0}, {'text': 'Item text again', 'heading_type': 0, 'level': 0}])
        with self.assertRaises(LocalCliRuntimeError) as ctx:
            _NumberingService()._bundle_official_numbering_apply(hwp, dict(NUMBERING_FIELDS))
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(_mutations(hwp), [])
        hwp = _doc()
        with self.assertRaises(LocalCliRuntimeError):
            _NumberingService(expected_page_ok=False)._bundle_official_numbering_apply(hwp, dict(NUMBERING_FIELDS))
        self.assertEqual(_mutations(hwp), [])

    def test_official_numbering_invalid_step_has_zero_native_calls(self) -> None:
        hwp = _doc()
        for override in ({'level': 0}, {'level': 5}, {'level': True}, {'expected_page': 0}, {'confirm_layout': False}, {'match': ''}):
            with self.assertRaises(LocalCliRuntimeError, msg=repr(override)):
                _NumberingService()._bundle_official_numbering_apply(hwp, {**NUMBERING_FIELDS, **override})
        self.assertEqual(hwp.calls, [])

    def test_official_numbering_unknown_outcome_maps_to_mutation_error(self) -> None:
        hwp = _doc()
        hwp.HAction.shape_mode = 'prefix-text'
        with self.assertRaises(LocalCliMutationError) as ctx:
            _NumberingService()._bundle_official_numbering_apply(hwp, {**NUMBERING_FIELDS, 'level': 1})
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_official_numbering_refuses_existing_bullet_without_mutation(self) -> None:
        hwp = _doc(paragraphs=[{'text': 'Item text', 'heading_type': 3, 'level': 0}])
        with self.assertRaises(LocalCliRuntimeError) as ctx:
            _NumberingService()._bundle_official_numbering_apply(hwp, dict(NUMBERING_FIELDS))
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(_mutations(hwp), [])


if __name__ == '__main__':
    unittest.main()
