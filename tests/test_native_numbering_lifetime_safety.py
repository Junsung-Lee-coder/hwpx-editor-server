"""Public-caller lifetime safety: queued-job runtime refusal (F4) and no caret navigation after an unknown outcome (F5).

Synthetic fakes only. They prove call ordering and refusal of this source; they are NOT Windows/Hancom
evidence and say nothing about native behaviour.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app import edit_ops
from app import native_numbering as nn
from app import worker
from app.local_cli_bundle_numbering import LocalCliBundleNumberingMixin
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError
from tests.native_numbering_fakes import FakeHwp
from tests.test_native_numbering_review_contract import doc, executes, hook_execute, run_official

OFFICIAL_OP = {'op': 'official_numbering', 'find': 'Item text', 'level': 2}
PAGE_OP = {'op': 'page_number', 'position': 'bottom-center'}
PAGE_REQUEST = {'position': 'bottom-center'}
SUPPORTED_OP = {'op': 'replace_all', 'find': 'a', 'replace': 'b'}


def after_execute_set_pos(hwp: FakeHwp, action: str, returns=None):
    """Record every set_pos issued once the named native action has run."""
    calls: list[tuple] = []
    state = {'after': False}
    original = hwp.set_pos

    def set_pos(*args):
        if state['after']:
            calls.append(args)
        return original(*args)

    hwp.set_pos = set_pos
    hook_execute(hwp, after=lambda name: state.update(after=True) if name == action else None, returns=returns)
    return calls


class WorkerHarness:
    """The real worker entry with only external dependencies replaced; counts every lifetime call."""

    def __init__(self, operations, *, apply_edit=None):
        self.events: list[str] = []
        self.hwp = doc()
        events = self.events
        self.hwp.open = lambda _path: events.append('open') or True
        self.hwp.quit = lambda: events.append('quit')
        self.operations = operations
        self.apply_edit = apply_edit

    def run(self):
        events = self.events
        hwp = self.hwp
        fake_com = SimpleNamespace(
            CoInitialize=lambda: events.append('CoInitialize'),
            CoUninitialize=lambda: events.append('CoUninitialize'),
        )

        def hwp_constructor():
            events.append('Hwp()')
            return hwp

        def create(cls, **_kwargs):
            events.append('create_hwp_instance')
            return cls()

        def apply(h, operations, *, step_recorder=None):
            events.append('apply')
            if self.apply_edit is not None:
                return self.apply_edit(h)
            return []

        replacements = {
            'configure_logger': MagicMock(return_value=MagicMock()),
            'update_runtime_status': MagicMock(),
            'load_instruction_payload_from_file': MagicMock(return_value={'operations': self.operations}),
            'build_tracking_fields': MagicMock(return_value={}),
            '_initialize_step_journal': MagicMock(return_value=None),
            '_build_action_tracker_state': MagicMock(return_value={}),
            '_build_gui_edit_scaffolding': MagicMock(return_value={}),
            'create_hwp_instance_with_recovery': create,
            'start_runtime_watchdog': MagicMock(return_value=(None, None)),
            '_set_hwp_window_maximized': MagicMock(return_value={}),
            'write_native_capabilities_artifact': MagicMock(return_value={}),
            'emit_runtime_observation': MagicMock(),
            'needs_source_pdf_validation': MagicMock(return_value=False),
            '_append_step_journal_terminal_event': MagicMock(),
            'apply_edit_operations': apply,
        }
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'pythoncom': fake_com, 'pyhwpx': SimpleNamespace(Hwp=hwp_constructor)}))
            stack.enter_context(patch.object(worker.sys, 'platform', 'win32'))
            for name, value in replacements.items():
                stack.enter_context(patch.object(worker, name, value))
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            worker.edit_and_convert_with_pyhwpx(
                root / 'source.hwpx',
                root / 'out.hwpx',
                root / 'out.pdf',
                root / 'instructions.json',
                root / 'job' / 'logs' / 'job.log',
            )


class QueuedJobRuntimeRefusalTests(unittest.TestCase):
    """The stateless queued-job worker quits the SDK and uninitializes COM in its own finally, so it cannot
    retain owned custody after a numbering UNKNOWN outcome. It must refuse N1/N2 before any lifetime call."""

    def assert_refused_with_no_lifetime_calls(self, operations) -> None:
        harness = WorkerHarness(operations)
        with self.assertRaises(edit_ops.NativeNumberingJobRuntimeUnsupported) as ctx:
            harness.run()
        self.assertEqual(harness.events, [])
        self.assertEqual(ctx.exception.code, 'native_numbering_job_runtime_unsupported')
        self.assertEqual(harness.hwp.calls, [])

    def test_official_numbering_job_is_refused_before_any_lifetime_call(self) -> None:
        self.assert_refused_with_no_lifetime_calls([dict(OFFICIAL_OP)])

    def test_page_number_job_is_refused_before_any_lifetime_call(self) -> None:
        self.assert_refused_with_no_lifetime_calls([dict(PAGE_OP)])

    def test_mixed_job_is_refused_whole_before_the_earlier_edit(self) -> None:
        self.assert_refused_with_no_lifetime_calls([dict(SUPPORTED_OP), dict(OFFICIAL_OP), dict(PAGE_OP)])

    def test_unsupported_runtime_never_mutates_to_reach_the_old_unknown_cleanup(self) -> None:
        harness = WorkerHarness([dict(OFFICIAL_OP)], apply_edit=lambda h: [run_official(h, 2)])
        with self.assertRaises(edit_ops.NativeNumberingJobRuntimeUnsupported):
            harness.run()
        self.assertEqual(executes(harness.hwp), [])
        self.assertNotIn('quit', harness.events)
        self.assertNotIn('CoUninitialize', harness.events)

    def test_supported_job_control_still_runs_the_normal_lifetime(self) -> None:
        harness = WorkerHarness([dict(SUPPORTED_OP)])
        # The fixture has no PDF export primitive, so the run ends in the worker's own export failure after
        # the edit; reaching it proves the guard did not block the supported job.
        with self.assertRaisesRegex(RuntimeError, 'Native PDF export failed'):
            harness.run()
        self.assertEqual(harness.events[0], 'CoInitialize')
        for event in ('Hwp()', 'open', 'apply', 'quit', 'CoUninitialize'):
            self.assertIn(event, harness.events)
        self.assertEqual(harness.events[-1], 'CoUninitialize')

    def test_guard_names_the_unsupported_ops_and_the_supported_surfaces(self) -> None:
        with self.assertRaises(edit_ops.NativeNumberingJobRuntimeUnsupported) as ctx:
            edit_ops.require_queued_job_runtime_support([dict(SUPPORTED_OP), dict(OFFICIAL_OP), dict(PAGE_OP)])
        message = str(ctx.exception)
        self.assertIn('official_numbering', message)
        self.assertIn('page_number', message)
        self.assertIn('command_bundle', message)
        edit_ops.require_queued_job_runtime_support([dict(SUPPORTED_OP)])

    def test_library_normalisation_and_dispatch_are_unchanged(self) -> None:
        normalized = edit_ops.normalize_operations([dict(OFFICIAL_OP), dict(PAGE_OP)])
        self.assertEqual([item['op'] for item in normalized], ['official_numbering', 'page_number'])
        self.assertIn('page_number', edit_ops.SUPPORTED_OPS)
        self.assertIn('official_numbering', edit_ops.SUPPORTED_OPS)
        hwp = doc()
        with patch('app.native_numbering_census.live_protection_reader', return_value=hwp.census):
            edit_ops.apply_edit_operations(hwp, [dict(PAGE_OP)])
        self.assertEqual(executes(hwp), ['PageNumPos'])


class JobApiSubmissionTests(unittest.TestCase):
    def test_submission_helper_refuses_numbering_ops_and_accepts_others(self) -> None:
        from app import api_server

        from app.models import EditAndConvertFromPathRequest

        for instructions in (
            [dict(OFFICIAL_OP)],
            {'operations': [dict(SUPPORTED_OP), dict(PAGE_OP)]},
        ):
            request = EditAndConvertFromPathRequest(source_path='unused.hwpx', instructions_json=json.dumps(instructions))
            with patch.object(api_server, 'create_job_layout', side_effect=AssertionError('job layout created')):
                with self.assertRaises(api_server.HTTPException) as ctx:
                    api_server.edit_and_convert_from_path(request)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn('command_bundle', str(ctx.exception.detail))
        with self.assertRaises(edit_ops.NativeNumberingJobRuntimeUnsupported):
            api_server._normalize_job_instructions([dict(PAGE_OP)])
        payload = api_server._normalize_job_instructions([dict(SUPPORTED_OP)])
        self.assertEqual(payload['operations'][0]['op'], 'replace_all')

    def test_capabilities_state_the_queued_runtime_limit(self) -> None:
        from app import api_server

        body = json.loads(api_server.capabilities().body)
        self.assertEqual(sorted(body['queued_job_unsupported_ops']), ['official_numbering', 'page_number'])
        self.assertIn('command_bundle', body['queued_job_unsupported_ops_note'])


class BundleOfficialNumberingNavigationTests(unittest.TestCase):
    STEP = {
        'op': 'official_numbering_apply',
        'match': 'Item text',
        'expected_page': 1,
        'level': 2,
        'confirm_layout': True,
    }

    class Service(LocalCliBundleNumberingMixin):
        def _find_text_on_expected_page(self, *_args, **_kwargs):
            return {'page': 1}

    def run_step(self, hwp):
        return self.Service()._bundle_official_numbering_apply(hwp, dict(self.STEP), protection=hwp.census)

    def test_unknown_native_status_does_not_move_the_caret_afterwards(self) -> None:
        for label, returns in (('none', None), ('false', False), ('truthy-int', 1)):
            hwp = doc()
            after = after_execute_set_pos(hwp, 'ParagraphShape', returns={'ParagraphShape': returns})
            with self.subTest(label), self.assertRaises(LocalCliMutationError) as ctx:
                self.run_step(hwp)
            self.assertEqual(after, [], label)
            self.assertTrue(ctx.exception.mutation_may_have_persisted)
            self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})

    def test_native_exception_does_not_move_the_caret_afterwards(self) -> None:
        hwp = doc()
        after = after_execute_set_pos(hwp, 'ParagraphShape')
        original = hwp.HAction.Execute

        def explode(name, hset):
            out = original(name, hset)
            if name == 'ParagraphShape':
                raise RuntimeError('COM exploded')
            return out

        hwp.HAction.Execute = explode
        with self.assertRaises(LocalCliMutationError) as ctx:
            self.run_step(hwp)
        self.assertEqual(after, [])
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_post_define_drift_does_not_move_the_caret_afterwards(self) -> None:
        hwp = doc(foreign=True)
        after = after_execute_set_pos(hwp, 'OutlineNumber')
        hwp.HAction.shape_mode = 'noop'
        with self.assertRaises(LocalCliMutationError) as ctx:
            self.run_step(hwp)
        self.assertEqual(after, [])
        self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_unknown_outcome_issues_no_undo_close_or_discard(self) -> None:
        hwp = doc()
        after_execute_set_pos(hwp, 'ParagraphShape', returns={'ParagraphShape': None})
        with self.assertRaises(LocalCliMutationError):
            self.run_step(hwp)
        self.assertEqual([c for c in hwp.calls if c[0] in {'Undo', 'Close', 'Discard', 'Clear', 'Quit'}], [])

    def test_success_restores_the_original_caret(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 2, 3)
        result = self.run_step(hwp)
        self.assertEqual(result['mutation'], 'official-numbering-apply')
        self.assertEqual(hwp.cursor, (0, 2, 3))
        self.assertEqual(hwp.calls[-1], ('set_pos', 0, 2, 3))

    def test_clean_refusal_before_mutation_restores_the_original_caret(self) -> None:
        hwp = doc(foreign=True, texts=('Other heading', 'Title', 'Item text'))
        hwp.paragraphs[0]['heading_type'] = 1
        hwp.cursor = (0, 2, 3)
        with self.assertRaises(LocalCliRuntimeError):
            self.run_step(hwp)
        self.assertEqual(executes(hwp), [])
        self.assertEqual(hwp.cursor, (0, 2, 3))
        self.assertEqual(hwp.calls[-1], ('set_pos', 0, 2, 3))

    def test_unprovable_target_page_restores_the_original_caret(self) -> None:
        class Service(LocalCliBundleNumberingMixin):
            def _find_text_on_expected_page(self, *_args, **_kwargs):
                return {'page': None}

        hwp = doc()
        hwp.cursor = (0, 1, 1)
        with self.assertRaises(LocalCliRuntimeError):
            Service()._bundle_official_numbering_apply(hwp, dict(self.STEP), protection=hwp.census)
        self.assertEqual(executes(hwp), [])
        self.assertEqual(hwp.cursor, (0, 1, 1))


class PageNumberLibraryNavigationTests(unittest.TestCase):
    def run_unknown(self, mutate) -> list[tuple]:
        hwp = doc()
        hwp.cursor = (0, 2, 3)
        after = after_execute_set_pos(hwp, 'PageNumPos')
        mutate(hwp)
        with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
            nn.apply_page_number(hwp, dict(PAGE_REQUEST), protection=hwp.census)
        self.assertEqual(ctx.exception.mutation_state, 'unknown')
        self.assertTrue(ctx.exception.mutation_may_have_persisted)
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
        self.assertEqual([c for c in hwp.calls if c[0] in {'Undo', 'Close', 'Discard', 'Run'}], [])
        return after

    def test_execute_exception_does_not_move_the_caret(self) -> None:
        self.assertEqual(self.run_unknown(lambda h: setattr(h.HAction, 'execute_raises', RuntimeError('boom'))), [])

    def test_execute_status_not_true_does_not_move_the_caret(self) -> None:
        self.assertEqual(self.run_unknown(lambda h: setattr(h.HAction, 'execute_result', None)), [])
        self.assertEqual(self.run_unknown(lambda h: setattr(h.HAction, 'execute_result', False)), [])

    def test_unverifiable_or_wrong_outcome_does_not_move_the_caret(self) -> None:
        for mode in ('duplicate', 'wrong-values', 'insert-unreadable', 'noop'):
            with self.subTest(mode):
                self.assertEqual(self.run_unknown(lambda h, m=mode: setattr(h.HAction, 'page_number_mode', m)), [])

    def test_census_failure_after_the_call_does_not_move_the_caret(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 2, 3)
        after = after_execute_set_pos(hwp, 'PageNumPos')
        reads = {'n': 0}

        def reader():
            reads['n'] += 1
            if reads['n'] > 1:
                raise RuntimeError('census unavailable')
            return hwp.census()

        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_page_number(hwp, dict(PAGE_REQUEST), protection=reader)
        self.assertEqual(after, [])

    def test_success_restores_the_caret(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 2, 3)
        result = nn.apply_page_number(hwp, dict(PAGE_REQUEST), protection=hwp.census)
        self.assertEqual(result['status'], 'inserted')
        self.assertEqual(hwp.cursor, (0, 2, 3))

    def test_clean_pre_mutation_refusal_restores_the_caret(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 2, 3)
        hwp.HAction.get_default_result = False
        with self.assertRaises(nn.NativeNumberingRefusal):
            nn.apply_page_number(hwp, dict(PAGE_REQUEST), protection=hwp.census)
        self.assertEqual(executes(hwp), [])
        self.assertEqual(hwp.cursor, (0, 2, 3))


class BundleStepPropagationTests(unittest.TestCase):
    def test_page_number_unknown_through_the_bundle_step_stays_dirty_without_navigation(self) -> None:
        hwp = doc()
        hwp.cursor = (0, 2, 3)
        after = after_execute_set_pos(hwp, 'PageNumPos', returns={'PageNumPos': None})

        class Service(LocalCliBundleNumberingMixin):
            pass

        with self.assertRaises(LocalCliMutationError) as ctx:
            Service()._bundle_page_number_apply(hwp, {'op': 'page_number_apply', 'position': 'bottom-center', 'confirm_layout': True}, protection=hwp.census)
        self.assertEqual(after, [])
        self.assertTrue(ctx.exception.mutation_may_have_persisted)


if __name__ == '__main__':
    unittest.main()
