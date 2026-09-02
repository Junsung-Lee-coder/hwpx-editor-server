from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import readiness


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

    def test_readiness_rejects_stale_heartbeat_and_accepts_current_lease(self) -> None:
        identity = readiness.current_worker_identity()
        snapshot = readiness.build_runtime_readiness_snapshot(
            probe_hwp=False,
            run_id="g3-run",
            candidate_generation="commit:tree:manifest",
            worker_identity=identity,
            ttl_seconds=60,
        )
        snapshot["ready"] = True
        snapshot["status"] = "ready"
        self.assertTrue(
            readiness.readiness_matches_current_worker(
                snapshot,
                candidate_generation="commit:tree:manifest",
                run_id="g3-run",
            )
        )

        old = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        snapshot["heartbeat"]["last_at"] = old
        self.assertFalse(
            readiness.readiness_matches_current_worker(
                snapshot,
                candidate_generation="commit:tree:manifest",
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
