"""Integration of object_insert_exact, layout_exact and layout_inspect into the service and CLI."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.command_packages.runtime import get_command_package_registry
from app.local_cli_runtime import LocalCliRuntimeError, snapshot_live_location
from app.local_cli_service import (
    LocalCliService,
    LocalCliServiceError,
    _record_bundle_undo_state,
)
from app.local_cli_service_support import _BUNDLE_ALLOWED_OPS
from local_cli_v1.bundles import BUNDLE_SERVER_OPS, BundleError, build_named_bundle
from tests.test_layout_exact import A4, _FakeLayoutHwp
from tests.test_layout_exact import _run as _run_layout
from tests.test_object_insert_exact import _FakeHwp
from tests.test_object_insert_exact import _run as _run_object_insert


def _service() -> LocalCliService:
    service = object.__new__(LocalCliService)
    service.command_packages = get_command_package_registry()
    return service


class RegistrationTests(unittest.TestCase):
    def test_ops_are_registered_everywhere(self) -> None:
        registry = get_command_package_registry()
        for op, read_only in (('object_insert_exact', False), ('layout_exact', False), ('layout_inspect', True)):
            with self.subTest(op=op):
                self.assertIn(op, _BUNDLE_ALLOWED_OPS)
                self.assertIn(op, BUNDLE_SERVER_OPS)
                self.assertEqual(registry.get(op).read_only, read_only)
                self.assertTrue(callable(getattr(LocalCliService, f'_bundle_{op}')))


class CliBundleTests(unittest.TestCase):
    def _validated(self, name: str, argv: list[str]) -> dict[str, Any]:
        steps = build_named_bundle(name, argv).server_payload()['steps']
        return _service()._validate_command_bundle_steps(steps)[1]

    def test_object_insert_payloads_pass_server_validation(self) -> None:
        cases = (
            ['--kind', 'footnote', '--text', '각주 내용'],
            ['--kind', 'memo', '--text', 'check this'],
            ['--kind', 'hyperlink', '--url', 'https://example.com/a', '--display-text', '여기'],
            ['--kind', 'bookmark', '--name', 'intro'],
            ['--kind', 'equation', '--script', '{a} over {b}'],
            ['--kind', 'ellipse', '--width-mm', '20', '--height-mm', '10', '--treat-as-char', 'off'],
            ['--kind', 'footer', '--text', '- 1 -', '--apply-to', 'odd'],
        )
        for extra in cases:
            with self.subTest(kind=extra[1]):
                step = self._validated('object-insert-exact', ['--expected-pos', '0,3,5', *extra, '--confirm-mutation'])
                self.assertEqual((step['kind'], step['expected_pos']), (extra[1], [0, 3, 5]))

    def test_object_insert_rejections(self) -> None:
        with self.assertRaisesRegex(BundleError, 'LIST,PARA,POS'):
            build_named_bundle('object-insert-exact', ['--kind', 'bookmark', '--name', 'x', '--expected-pos', '0,3', '--confirm-mutation'])
        with self.assertRaisesRegex(LocalCliServiceError, 'does not take'):
            self._validated('object-insert-exact', ['--kind', 'bookmark', '--name', 'x', '--text', 'y', '--expected-pos', '0,3,5', '--confirm-mutation'])
        with self.assertRaisesRegex(LocalCliServiceError, 'url'):
            self._validated('object-insert-exact', ['--kind', 'hyperlink', '--url', 'javascript:alert(1)', '--display-text', 'x',
                                                    '--expected-pos', '0,3,5', '--confirm-mutation'])

    def test_layout_payloads_pass_server_validation(self) -> None:
        cases = (
            ['--kind', 'page_setup', '--margin-left-mm', '25', '--expected-before', 'margin_left_mm=30', '--apply-to', 'whole_document'],
            ['--kind', 'page_setup', '--landscape', 'on', '--expected-before', 'landscape=false'],
            ['--kind', 'columns', '--count', '2', '--gap-mm', '8'],
            ['--kind', 'section_insert'],
            ['--kind', 'hanging_indent', '--marker-text', '1. '],
        )
        for extra in cases:
            with self.subTest(extra=extra):
                step = self._validated('layout-exact', ['--expected-pos', '0,0,0', *extra, '--confirm-layout'])
                self.assertEqual(step['kind'], extra[1])
        step = self._validated('layout-exact', ['--expected-pos', '0,9,0', '--kind', 'section_delete', '--section-index', '2', '--confirm-layout'])
        self.assertEqual(step['section_index'], 2)
        self.assertEqual(self._validated('layout-inspect', [])['op'], 'layout_inspect')

    def test_layout_rejections(self) -> None:
        with self.assertRaisesRegex(BundleError, 'KEY=VALUE'):
            build_named_bundle('layout-exact', ['--kind', 'page_setup', '--expected-pos', '0,0,0', '--margin-left-mm', '25',
                                                '--expected-before', 'margin_left_mm', '--confirm-layout'])
        with self.assertRaisesRegex(LocalCliServiceError, 'expected_before'):
            self._validated('layout-exact', ['--kind', 'page_setup', '--expected-pos', '0,0,0', '--margin-left-mm', '25', '--confirm-layout'])
        with self.assertRaises(LocalCliServiceError):
            self._validated('layout-exact', ['--kind', 'section_insert', '--expected-pos', '0,0,0', '--count', '2', '--confirm-layout'])


class LayoutInspectTests(unittest.TestCase):
    def test_reads_page_and_columns_without_executing(self) -> None:
        hwp = _FakeLayoutHwp([['a'], ['b']])
        hwp.caret = (0, 1, 0)
        result = _service()._bundle_layout_inspect(hwp, {'op': 'layout_inspect'})
        self.assertTrue(result['read_only'])
        self.assertEqual(result['caret_pos'], [0, 1, 0])
        self.assertEqual(result['section'], {'count': 2, 'current': 2})
        self.assertAlmostEqual(result['page_setup']['margin_left_mm'], A4['LeftMargin'] * 25.4 / 7200, places=1)
        self.assertFalse(result['page_setup']['landscape'])
        self.assertEqual(result['columns'], {'count': 1, 'same_width': True, 'gap_mm': 0.0})
        self.assertEqual(hwp.log, ['GetDefault(PageSetup)', 'GetDefault(MultiColumn)'])

    def test_values_feed_layout_exact_expected_before(self) -> None:
        hwp = _FakeLayoutHwp()
        service = _service()
        service._bundle_compact_snapshot = lambda _hwp: {  # type: ignore[method-assign]
            'pos': hwp.caret, 'is_cell': hwp.in_cell, 'has_selection': hwp.selection is not None, 'selection_mode': 0,
        }
        before = service._bundle_layout_inspect(hwp, {'op': 'layout_inspect'})['page_setup']
        step = service._validate_command_bundle_steps([{
            'op': 'layout_exact', 'kind': 'page_setup', 'expected_pos': [0, 0, 0], 'margin_left_mm': 25.0,
            'expected_before': {'margin_left_mm': before['margin_left_mm']}, 'confirm_layout': True,
        }])[0]
        result = service._bundle_layout_exact(hwp, step)
        self.assertTrue(result['succeeded'])


class _SelectingHwp(_FakeHwp):
    """Object-insert fake whose paragraph reads select text and whose set_pos drops the selection, like Hancom."""

    def select_text(self, spara: int, spos: int, epara: int, epos: int, slist: int = 0) -> bool:
        self.selection = (spos, len(self.paras[spara]) if epos == -1 else epos)
        return True

    def set_pos(self, list_id: int, para: int, pos: int) -> bool:
        self.caret, self.selection = [list_id, para, pos], None
        return True


class WhereKeepsSelectionTests(unittest.TestCase):
    def test_nearby_capture_is_what_drops_a_selection(self) -> None:
        hwp = _SelectingHwp()
        hwp.select(6, 11)
        snapshot_live_location(hwp=hwp, source_filename='a.hwpx', working_copy_id='s')
        self.assertIsNone(hwp.selection)

    def test_where_then_hyperlink_recipe_keeps_the_selection(self) -> None:
        hwp = _SelectingHwp()
        hwp.select(6, 11)
        service = _service()
        steps = build_named_bundle('object-insert-exact', ['--kind', 'hyperlink', '--url', 'https://example.com/a', '--display-text', 'world',
                                                           '--expected-pos', '0,0,11', '--confirm-mutation']).server_payload()['steps']
        self.assertEqual(steps[0]['op'], 'where')
        service._bundle_where_location(hwp, source_filename='a.hwpx', working_copy_id='s')
        self.assertEqual(hwp.selection, (6, 11))
        step = service._validate_command_bundle_steps(steps)[1]
        result = service._bundle_object_insert_exact(hwp, step)
        self.assertTrue(result['verification']['ok'], result['verification'])

    def test_where_refuses_if_the_selection_moved(self) -> None:
        hwp = _SelectingHwp()
        hwp.select(6, 11)

        def clobber(**kwargs: Any) -> dict[str, Any]:
            kwargs['hwp'].selection = None
            return {}

        with patch('app.local_cli_service.snapshot_live_location', side_effect=clobber), \
                self.assertRaisesRegex(LocalCliRuntimeError, 'where changed the live'):
            _service()._bundle_where_location(hwp, source_filename='a.hwpx', working_copy_id='s')


class UndoAfterUnverifiedOpsTests(unittest.TestCase):
    def _undo(self, binding: dict[str, Any]) -> list[str]:
        service = _service()
        calls: list[str] = []
        service._load_active_binding = lambda session_id=None: binding  # type: ignore[method-assign]

        def execute_live(**kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs['command_name'])
            raise RuntimeError('stop after dispatch')

        service._execute_live = execute_live  # type: ignore[method-assign]
        with self.assertRaises((LocalCliRuntimeError, RuntimeError)) as caught:
            service.undo()
        return calls if calls else [str(caught.exception)]

    def test_undo_is_refused_after_object_or_layout_edits(self) -> None:
        for op in ('object_insert_exact', 'layout_exact'):
            with self.subTest(op=op):
                binding: dict[str, Any] = {}
                _record_bundle_undo_state(binding, [{'op': 'where', 'dirty': False}, {'op': op, 'dirty': True}], {})
                self.assertIsNone(binding['pending_logical_undo_count'])
                result = self._undo(binding)
                self.assertIn('undo refused', result[0])
                self.assertIn(op, result[0])

    def test_later_counted_edit_can_be_undone_once_then_refused_again(self) -> None:
        binding: dict[str, Any] = {}
        _record_bundle_undo_state(binding, [{'op': 'object_insert_exact', 'dirty': True}], {})
        binding['pending_logical_undo_count'] = 1  # a later edit recorded its own undo count
        self.assertEqual(self._undo(binding), ['undo'])
        binding['pending_logical_undo_count'] = None  # what undo() stores after it runs
        self.assertIn('undo refused', self._undo(binding)[0])

    def test_other_dirty_bundles_keep_their_step_count(self) -> None:
        binding: dict[str, Any] = {}
        _record_bundle_undo_state(binding, [{'op': 'insert_text', 'dirty': True}, {'op': 'where', 'dirty': False}, {'op': 'insert_text', 'dirty': True}], {})
        self.assertEqual(binding['pending_logical_undo_count'], 2)
        self.assertNotIn('logical_undo_unverified', binding)
        self.assertEqual(self._undo(binding), ['undo'])


class BundlePathUndoTests(unittest.TestCase):
    """`command_bundle` end to end (validation, step dispatch, binding update), then `undo`."""

    def _run_bundle(self, hwp: Any, steps: list[dict[str, Any]], store: dict[str, Any] | None = None) -> tuple[LocalCliService, dict[str, Any], dict[str, Any]]:
        service = _service()
        if store is None:
            store = {'binding': {'session_id': 's', 'command_generation': 0, 'native_command_sequence': 0}}
        undo_dispatched: list[str] = []

        def execute_live(*, handler: Any, command_name: str = '', **kwargs: Any) -> Any:
            if command_name == 'undo':
                undo_dispatched.append(command_name)
                raise AssertionError('undo reached the live document')
            return handler(SimpleNamespace(hwp=hwp, source_filename='a.hwpx', session_id='s'))

        def save(binding: dict[str, Any]) -> dict[str, Any]:
            store['binding'] = binding
            return binding

        service._load_active_binding = lambda session_id=None: store['binding']  # type: ignore[method-assign]
        service._save_binding = save  # type: ignore[method-assign]
        service._update_live_binding = lambda current, **kwargs: current  # type: ignore[method-assign]
        service._record_local_cli_command = lambda *args, **kwargs: None  # type: ignore[method-assign]
        service._execute_live = execute_live  # type: ignore[method-assign]
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            result = service.command_bundle(steps=steps)
        return service, store, result

    def _object_steps(self, hwp: Any) -> list[dict[str, Any]]:
        pos = ','.join(str(item) for item in hwp.caret)
        return build_named_bundle('object-insert-exact', ['--kind', 'footnote', '--text', 'note', '--expected-pos', pos, '--confirm-mutation']).server_payload()['steps']

    def test_undo_refused_after_successful_object_insert_bundle(self) -> None:
        hwp = _SelectingHwp()
        service, store, result = self._run_bundle(hwp, self._object_steps(hwp))
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['dirty'])
        self.assertIn('object_insert_exact', store['binding']['logical_undo_unverified'])
        with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
            service.undo()

    def test_undo_refused_after_failed_object_insert_bundle(self) -> None:
        hwp = _SelectingHwp(noop={'InsertFootnote'})
        service, _store, result = self._run_bundle(hwp, self._object_steps(hwp))
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(result['steps'][1]['mutation_may_have_persisted'])
        with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
            service.undo()

    def test_undo_refused_after_layout_bundle(self) -> None:
        hwp = _FakeLayoutHwp()
        steps = build_named_bundle('layout-exact', ['--kind', 'columns', '--count', '2', '--expected-pos', '0,0,0', '--confirm-layout']).server_payload()['steps']
        with patch.object(LocalCliService, '_bundle_compact_snapshot', lambda self, _hwp: {
                'pos': list(hwp.caret), 'is_cell': False, 'has_selection': False, 'selection_mode': 0}):
            service, _store, result = self._run_bundle(hwp, steps)
        self.assertTrue(result['ok'], result)
        with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
            service.undo()


    def test_later_no_change_bundles_on_a_dirty_document_keep_undo_refused(self) -> None:
        for label in ('refused before mutation', 'where only'):
            with self.subTest(label=label):
                hwp = _SelectingHwp()
                store: dict[str, Any] = {'binding': {'session_id': 's', 'command_generation': 0, 'native_command_sequence': 0,
                                                     'working_copy_dirty': True, 'pending_logical_undo_count': 1}}
                _service_obj, store, first = self._run_bundle(hwp, self._object_steps(hwp), store)
                self.assertTrue(first['ok'], first)
                if label == 'where only':
                    steps = [{'op': 'where', 'label': 'where:only'}]
                else:
                    steps = build_named_bundle('object-insert-exact', ['--kind', 'footnote', '--text', 'n', '--expected-pos', '0,1,0',
                                                                       '--confirm-mutation']).server_payload()['steps']
                service, store, second = self._run_bundle(hwp, steps, store)
                self.assertTrue(second['dirty'], 'the document stays dirty from the first edit')
                self.assertFalse(any(step.get('dirty') for step in second['steps']))
                self.assertIsNone(store['binding']['pending_logical_undo_count'])
                with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
                    service.undo()

    def test_timeout_mid_bundle_leaves_undo_refused(self) -> None:
        hwp = _SelectingHwp()
        service = _service()
        store: dict[str, Any] = {'binding': {'session_id': 's', 'pending_logical_undo_count': 1}}
        service._load_active_binding = lambda session_id=None: store['binding']  # type: ignore[method-assign]
        service._save_binding = lambda binding: store.__setitem__('binding', binding) or binding  # type: ignore[method-assign]

        def timed_out(**kwargs: Any) -> Any:
            raise TimeoutError('bundle exceeded 120s')

        service._execute_live = timed_out  # type: ignore[method-assign]
        with self.assertRaises(TimeoutError):
            service.command_bundle(steps=self._object_steps(hwp))
        with self.assertRaisesRegex(LocalCliRuntimeError, 'undo refused'):
            service.undo()

    def test_clean_exact_bundle_restores_the_prior_undo_state(self) -> None:
        hwp = _SelectingHwp()
        hwp.caret = [0, 1, 0]  # the recipe's expected position no longer matches
        steps = build_named_bundle('object-insert-exact', ['--kind', 'footnote', '--text', 'n', '--expected-pos', '0,0,5', '--confirm-mutation']).server_payload()['steps']
        _service_obj, store, result = self._run_bundle(hwp, steps)
        self.assertFalse(result['ok'])
        self.assertFalse(result['dirty'])
        self.assertIsNone(store['binding'].get('logical_undo_unverified'))


class ReadbackLimitTests(unittest.TestCase):
    def test_one_exact_step_per_bundle(self) -> None:
        step = build_named_bundle('object-insert-exact', ['--kind', 'bookmark', '--name', 'a', '--expected-pos', '0,0,0', '--confirm-mutation']).server_payload()['steps'][1]
        layout = build_named_bundle('layout-exact', ['--kind', 'section_insert', '--expected-pos', '0,0,0', '--confirm-layout']).server_payload()['steps'][1]
        _service()._validate_command_bundle_steps([step])
        for pair in ([step, dict(step)], [step, layout]):
            with self.assertRaisesRegex(LocalCliServiceError, 'at most one exact'):
                _service()._validate_command_bundle_steps(pair)

    def test_before_budget_includes_a_dry_run_of_the_proof(self) -> None:
        calls: list[str] = []
        real = __import__('app.local_cli_object_insert', fromlist=['evaluate_insert']).evaluate_insert

        def spy(plan: Any, before: Any, after: Any) -> Any:
            calls.append('same' if before is after else 'after')
            return real(plan, before, after)

        with patch('app.local_cli_object_insert.evaluate_insert', spy):
            _run_object_insert(_FakeHwp())
        self.assertEqual(calls, ['same', 'after'])

    def test_oversized_readback_is_refused_before_mutation(self) -> None:
        with patch('app.hwpml_invariants.MAX_READBACK_CHARS', 50):
            hwp = _FakeHwp()
            with self.assertRaisesRegex(LocalCliRuntimeError, 'above the 50 limit'):
                _run_object_insert(hwp)
            self.assertEqual(hwp.log, [])
            layout = _FakeLayoutHwp()
            with self.assertRaisesRegex(LocalCliRuntimeError, 'above the 50 limit'):
                _run_layout(layout, {'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2})
            self.assertEqual(layout.mutations(), [])

    def test_slow_before_readback_is_refused_before_mutation(self) -> None:
        clock = iter([0.0, 35.0])
        with patch('app.local_cli_object_insert.time.monotonic', lambda: next(clock)):
            hwp = _FakeHwp()
            with self.assertRaisesRegex(LocalCliRuntimeError, 'took 35.0s'):
                _run_object_insert(hwp)
            self.assertEqual(hwp.log, [])
        clock = iter([0.0, 35.0])
        with patch('app.local_cli_layout.time.monotonic', lambda: next(clock)):
            layout = _FakeLayoutHwp()
            with self.assertRaisesRegex(LocalCliRuntimeError, 'took 35.0s'):
                _run_layout(layout, {'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2})
            self.assertEqual(layout.mutations(), [])


if __name__ == '__main__':
    unittest.main()
