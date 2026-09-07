from __future__ import annotations

import unittest
from unittest.mock import patch

from app import readiness
from app import worker


class _RecordingHwp:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        type(self).calls.append(kwargs)


class _LegacyHwp:
    def __init__(self, *, visible: bool = True, register_module: bool = True) -> None:
        raise AssertionError('unsafe legacy constructor was invoked')


class _SideEffectTypeErrorHwp:
    calls = 0

    def __init__(self, *, new: bool, visible: bool = True, register_module: bool = True) -> None:
        type(self).calls += 1
        raise TypeError('simulated internal constructor failure after allocation')


class _OpaqueHwp:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        type(self).calls.append(kwargs)


class HwpConstructorSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        _RecordingHwp.calls = []
        _SideEffectTypeErrorHwp.calls = 0
        _OpaqueHwp.calls = []

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


if __name__ == '__main__':
    unittest.main()
