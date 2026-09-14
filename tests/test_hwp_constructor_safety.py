from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import readiness
from app import worker


class _RecordingHwp:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        type(self).calls.append(kwargs)


class _FirstCallFailsThenSucceedsHwp:
    """Second call would succeed: exactly what the helper must never reach for."""

    calls = 0

    def __init__(self, **kwargs: object) -> None:
        type(self).calls += 1
        if type(self).calls == 1:
            raise RuntimeError('simulated visible construction failure after allocation')


class _AlwaysFailingHwp:
    calls = 0

    def __init__(self, **kwargs: object) -> None:
        type(self).calls += 1
        raise TypeError('simulated internal constructor failure after allocation')


class _LegacyHwp:
    def __init__(self, *, visible: bool = True, register_module: bool = True) -> None:
        raise AssertionError('unsafe legacy constructor was invoked')


class _SideEffectTypeErrorHwp:
    calls = 0

    def __init__(self, *, new: bool, visible: bool = True, register_module: bool = True) -> None:
        type(self).calls += 1
        raise TypeError('simulated internal constructor failure after allocation')


class _QuietHwp:
    """A no-method instance whose configuration hooks all degrade gracefully."""

    def __init__(self, **kwargs: object) -> None:
        return None

    def SetMessageBoxMode(self, mode: int) -> None:
        return None

    def RegisterModule(self, dll_name: str, module_name: str) -> None:
        return None

    class XHwpWindows:
        Active_XHwpWindow = None


class _OpaqueHwp:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        type(self).calls.append(kwargs)


class HwpConstructorSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        _RecordingHwp.calls = []
        _SideEffectTypeErrorHwp.calls = 0
        _OpaqueHwp.calls = []
        _FirstCallFailsThenSucceedsHwp.calls = 0
        _AlwaysFailingHwp.calls = 0

    def test_worker_constructor_requests_a_new_instance_first(self) -> None:
        instance, label = worker._instantiate_hwp_without_builtin_register_module(_RecordingHwp)

        self.assertIsInstance(instance, _RecordingHwp)
        self.assertEqual(label, 'Hwp(new=True, visible=True, register_module=False)')
        self.assertEqual(
            _RecordingHwp.calls,
            [{'new': True, 'visible': True, 'register_module': False}],
        )

    def test_readiness_probe_requests_a_new_instance_first(self) -> None:
        instance, label = readiness._construct_probe_hwp(_RecordingHwp)

        self.assertIsInstance(instance, _RecordingHwp)
        self.assertEqual(label, 'Hwp(new=True, visible=True, register_module=False)')
        self.assertEqual(
            _RecordingHwp.calls,
            [{'new': True, 'visible': True, 'register_module': False}],
        )

    def test_worker_does_not_fall_back_to_legacy_reuse_constructor(self) -> None:
        with self.assertRaises(RuntimeError):
            worker._instantiate_hwp_without_builtin_register_module(_LegacyHwp)

    def test_worker_does_not_retry_after_side_effecting_type_error(self) -> None:
        with self.assertRaises(TypeError):
            worker._instantiate_hwp_without_builtin_register_module(_SideEffectTypeErrorHwp)

        self.assertEqual(_SideEffectTypeErrorHwp.calls, 1)

    def test_worker_invokes_opaque_constructor_once_with_safe_signature(self) -> None:
        with patch.object(worker.inspect, 'signature', side_effect=ValueError('opaque callable')):
            instance, label = worker._instantiate_hwp_without_builtin_register_module(_OpaqueHwp)

        self.assertIsInstance(instance, _OpaqueHwp)
        self.assertEqual(label, 'Hwp(new=True, visible=True, register_module=False)')
        self.assertEqual(
            _OpaqueHwp.calls,
            [{'new': True, 'visible': True, 'register_module': False}],
        )

    def test_readiness_rejects_constructor_without_new_parameter(self) -> None:
        with self.assertRaises(RuntimeError):
            readiness._construct_probe_hwp(_LegacyHwp)

    def test_broad_runtime_cleanup_preserves_unowned_processes(self) -> None:
        with patch.object(worker.subprocess, 'run') as run:
            result = worker.kill_hwp_runtime()

        run.assert_not_called()
        self.assertFalse(result['attempted'])
        self.assertEqual(result['reason'], 'task_owned_process_identity_required')

    def test_outer_helper_does_not_retry_side_effect_exception(self) -> None:
        """The product-visible outer helper performs exactly one constructor attempt."""

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'jobs' / 'job' / 'logs' / 'worker.log'
            with patch.object(worker, 'kill_hwp_runtime', side_effect=AssertionError('no runtime cleanup may run')):
                with self.assertRaises(TypeError):
                    worker.create_hwp_instance_with_recovery(
                        _AlwaysFailingHwp,
                        log_path=log_path,
                        phase='test_phase',
                        detail='test detail',
                    )

        self.assertEqual(_AlwaysFailingHwp.calls, 1)

    def test_outer_helper_first_failure_cannot_become_success(self) -> None:
        """A first failed attempt must reach the caller even if a retry could succeed."""

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'jobs' / 'job' / 'logs' / 'worker.log'
            with patch.object(worker, 'kill_hwp_runtime', side_effect=AssertionError('no runtime cleanup may run')):
                with self.assertRaises(RuntimeError):
                    worker.create_hwp_instance_with_recovery(
                        _FirstCallFailsThenSucceedsHwp,
                        log_path=log_path,
                        phase='test_phase',
                        detail='test detail',
                        max_attempts=2,
                    )

        self.assertEqual(_FirstCallFailsThenSucceedsHwp.calls, 1)

    def test_outer_helper_positive_max_attempts_is_one_effective_call(self) -> None:
        """Positive compatibility values are capped: one visible attempt, one log ceiling."""

        with tempfile.TemporaryDirectory() as temp_dir:
            for declared_max_attempts in (1, 2, 5):
                _AlwaysFailingHwp.calls = 0
                log_path = Path(temp_dir) / f'jobs-{declared_max_attempts}' / 'job' / 'logs' / 'worker.log'
                with patch.object(worker, 'kill_hwp_runtime', side_effect=AssertionError('no runtime cleanup may run')):
                    with self.assertRaises(TypeError):
                        worker.create_hwp_instance_with_recovery(
                            _AlwaysFailingHwp,
                            log_path=log_path,
                            phase='test_phase',
                            detail='test detail',
                            max_attempts=declared_max_attempts,
                        )

                self.assertEqual(_AlwaysFailingHwp.calls, 1)

            # Zero-attempt behavior is preserved: no constructor call, and the
            # existing failure is raised to the caller.
            log_path = Path(temp_dir) / 'jobs-zero' / 'job' / 'logs' / 'worker.log'
            with self.assertRaises(RuntimeError):
                worker.create_hwp_instance_with_recovery(
                    _QuietHwp,
                    log_path=log_path,
                    phase='test_phase',
                    detail='test detail',
                    max_attempts=0,
                )

    def test_outer_helper_success_has_one_attempt_log_entry(self) -> None:
        """A successful construction logs the single attempt and no success phase after failure."""

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / 'jobs' / 'job' / 'logs' / 'worker.log'
            instance = worker.create_hwp_instance_with_recovery(
                _QuietHwp,
                log_path=log_path,
                phase='test_phase',
                detail='test detail',
            )
            self.assertIsInstance(instance, _QuietHwp)

            history_path = log_path.parent.parent / 'metadata' / 'runtime_status.history.jsonl'
            entries = [json.loads(line) for line in history_path.read_text(encoding='utf-8').splitlines() if line.strip()]
            # The existing design logs the start attempt and the completion of
            # the same single attempt. The compatibility parameter must appear
            # as its effective capped value, never the declared ceiling.
            self.assertEqual(len(entries), 2)
            for entry in entries:
                self.assertEqual(entry['hwp_start_attempt'], 1)
                self.assertEqual(entry['hwp_start_max_attempts'], 1)


if __name__ == '__main__':
    unittest.main()
