from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import readiness


def _raise_runtime_error() -> None:
    raise RuntimeError("simulated CoUninitialize failure")


class G3ReadinessRuntimeTests(unittest.TestCase):
    def test_probe_cleanup_failure_is_a_readiness_failure(self) -> None:
        class BrokenCloseProbe:
            def Quit(self) -> None:
                raise RuntimeError("close failed")

        fake_pyhwpx = types.ModuleType("pyhwpx")
        setattr(fake_pyhwpx, "Hwp", object)
        with patch.dict(sys.modules, {"pyhwpx": fake_pyhwpx}), patch.object(
            readiness, "_construct_probe_hwp", return_value=(BrokenCloseProbe(), "fake")
        ):
            result = readiness._probe_hwp_automation()

        self.assertFalse(result["ok"])
        self.assertFalse(result["cleanup_ok"])
        self.assertTrue(result["cleanup_errors"])
        self.assertFalse(result["probe_closed"])
        self.assertEqual(result["cleanup_state"], "failed")

    def test_constructor_no_handle_cleanup_unconfirmed(self) -> None:
        """No returned handle means cleanup can never be confirmed true."""

        class ExplodingHwp:
            def __init__(self, **kwargs: object) -> None:
                raise TypeError("simulated constructor failure after possible COM contact")

        fake_pyhwpx = types.ModuleType("pyhwpx")
        setattr(fake_pyhwpx, "Hwp", ExplodingHwp)
        with patch.dict(sys.modules, {"pyhwpx": fake_pyhwpx}):
            result = readiness._probe_hwp_automation()

        self.assertFalse(result["ok"])
        self.assertIs(result["probe_closed"], False)
        self.assertNotIn("cleanup_ok", result)
        self.assertEqual(result["cleanup_state"], "unconfirmed")

    def test_received_probe_confirmed_cleanup(self) -> None:
        """A returned handle that closes cleanly confirms cleanup."""

        class CleanProbe:
            def __init__(self) -> None:
                self.quit_calls = 0

            def Quit(self) -> None:
                self.quit_calls += 1

        probe = CleanProbe()
        fake_pyhwpx = types.ModuleType("pyhwpx")
        setattr(fake_pyhwpx, "Hwp", object)
        with patch.dict(sys.modules, {"pyhwpx": fake_pyhwpx}), patch.object(
            readiness, "_construct_probe_hwp", return_value=(probe, "fake")
        ):
            result = readiness._probe_hwp_automation()

        self.assertTrue(result["ok"])
        self.assertTrue(result["probe_closed"])
        self.assertEqual(result["cleanup_state"], "confirmed")
        self.assertTrue(result["cleanup_ok"])
        self.assertEqual(probe.quit_calls, 1)

    def test_received_probe_com_uninitialize_failure_not_cleanup_success(self) -> None:
        """A close that succeeds but a failing COM teardown is a cleanup failure."""

        class CleanCloseProbe:
            def Quit(self) -> None:
                return None

        fake_pythoncom = types.ModuleType("pythoncom")
        fake_pythoncom.CoInitialize = lambda: None
        fake_pythoncom.CoUninitialize = _raise_runtime_error  # type: ignore[attr-defined]
        probe = CleanCloseProbe()
        fake_pyhwpx = types.ModuleType("pyhwpx")
        setattr(fake_pyhwpx, "Hwp", object)
        with patch.dict(sys.modules, {"pythoncom": fake_pythoncom, "pyhwpx": fake_pyhwpx}), patch.object(
            readiness, "_construct_probe_hwp", return_value=(probe, "fake")
        ):
            result = readiness._probe_hwp_automation()

        self.assertFalse(result["ok"])
        self.assertTrue(result["probe_closed"])
        self.assertFalse(result["com_uninitialized"])
        self.assertFalse(result["cleanup_ok"])
        self.assertEqual(result["cleanup_state"], "failed")
        self.assertTrue(result["cleanup_errors"])

    def test_probe_without_com_teardown_confirms_after_close(self) -> None:
        """Without an initialized COM teardown is applicable, close alone confirms."""

        class CleanProbe:
            def Quit(self) -> None:
                return None

        probe = CleanProbe()
        fake_pyhwpx = types.ModuleType("pyhwpx")
        setattr(fake_pyhwpx, "Hwp", object)
        with patch.dict(sys.modules, {"pyhwpx": fake_pyhwpx}), patch.object(
            readiness, "_construct_probe_hwp", return_value=(probe, "fake")
        ):
            result = readiness._probe_hwp_automation()

        self.assertTrue(result["ok"])
        self.assertTrue(result["cleanup_ok"])
        self.assertEqual(result["cleanup_state"], "confirmed")
        self.assertNotIn("com_uninitialized", result)

    def test_readiness_rejects_stale_heartbeat_and_accepts_current_lease(self) -> None:
        identity = readiness.current_worker_identity()
        fake_pythoncom = types.ModuleType("pythoncom")
        fake_pyhwpx = types.ModuleType("pyhwpx")
        setattr(fake_pyhwpx, "Hwp", object)
        with patch.dict(sys.modules, {"pythoncom": fake_pythoncom, "pyhwpx": fake_pyhwpx}), patch.object(
            readiness.sys, "platform", "win32"
        ), patch.object(
            readiness, "build_pdf_renderer_check", return_value={"ok": True, "source": "test"}
        ):
            snapshot = readiness.build_runtime_readiness_snapshot(
                probe_hwp=False,
                run_id="g3-run",
                candidate_generation="a" * 40 + ":" + "b" * 40 + ":" + "c" * 64,
                worker_identity=identity,
                ttl_seconds=60,
            )
            self.assertTrue(
                readiness.readiness_matches_current_worker(
                    snapshot,
                    candidate_generation=snapshot["candidate_generation"],
                    run_id="g3-run",
                )
            )

            old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
            snapshot["heartbeat"]["last_at"] = old
            self.assertFalse(
                readiness.readiness_matches_current_worker(
                    snapshot,
                    candidate_generation=snapshot["candidate_generation"],
                    run_id="g3-run",
                )
            )

    def test_current_run_not_ready_snapshot_is_atomic_and_generation_bound(self) -> None:
        with TemporaryDirectory() as raw:
            spool = Path(raw) / "spool"
            fake_settings = types.SimpleNamespace(spool_root=spool, worker_name="g3-worker")
            with patch.object(readiness, "settings", fake_settings):
                snapshot = readiness.build_current_run_not_ready_snapshot(
                    run_id="g3-current",
                    candidate_generation="commit:tree:manifest",
                    worker_identity=readiness.current_worker_identity(),
                )
                path = readiness.write_runtime_readiness_snapshot(snapshot)
                self.assertEqual(path, spool / "readiness" / "worker_ready.json")
                readback = readiness.load_runtime_readiness_snapshot()

        self.assertIsNotNone(readback)
        assert readback is not None
        self.assertFalse(readback["ready"])
        self.assertEqual(readback["status"], "not_ready")
        self.assertEqual(readback["run_id"], "g3-current")
        self.assertEqual(readback["candidate_generation"], "commit:tree:manifest")

    def test_readiness_owner_cannot_replace_a_newer_run(self) -> None:
        with TemporaryDirectory() as raw:
            spool = Path(raw) / "spool"
            fake_settings = types.SimpleNamespace(spool_root=spool, worker_name="g3-worker")
            identity = readiness.current_worker_identity()
            with patch.object(readiness, "settings", fake_settings):
                readiness.write_runtime_readiness_snapshot(
                    readiness.build_current_run_not_ready_snapshot(
                        run_id="g3-old", candidate_generation="commit:tree:manifest", worker_identity=identity
                    )
                )
                readiness.write_runtime_readiness_snapshot(
                    readiness.build_current_run_not_ready_snapshot(
                        run_id="g3-new", candidate_generation="commit:tree:manifest", worker_identity=identity
                    )
                )
                stale = readiness.build_runtime_readiness_snapshot(
                    probe_hwp=False,
                    run_id="g3-old",
                    candidate_generation="commit:tree:manifest",
                    worker_identity=identity,
                )
                with self.assertRaises(readiness.ReadinessOwnershipError):
                    readiness.write_runtime_readiness_snapshot(stale, expected_run_id="g3-old")
                readback = readiness.load_runtime_readiness_snapshot()

        self.assertIsNotNone(readback)
        assert readback is not None
        self.assertEqual(readback["run_id"], "g3-new")

    def test_windows_process_generation_check_never_uses_os_kill_zero(self) -> None:
        with patch.object(readiness.os, "name", "nt"), patch.object(
            readiness, "_process_start_identity", return_value="win-filetime:abc"
        ), patch.object(
            readiness.os,
            "kill",
            side_effect=AssertionError("os.kill(pid, 0) is destructive on Windows"),
        ):
            self.assertTrue(
                readiness._process_generation_alive(123, "win-filetime:abc")
            )


if __name__ == "__main__":
    unittest.main()
