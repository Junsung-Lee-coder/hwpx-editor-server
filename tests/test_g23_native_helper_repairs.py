from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class G23NativeHelperRepairTests(unittest.TestCase):
    def read_common(self) -> str:
        return (ROOT / "scripts" / "windows_install_common.psm1").read_text(encoding="utf-8")

    def test_quick_child_identity_fallback_uses_managed_process_start_time(self) -> None:
        common = self.read_common()
        invoke = common[common.index("function Invoke-NativeChecked") : common.index("function Get-SourceManifest")]
        self.assertIn("$process.ManagedProcess.StartTime.ToUniversalTime().Ticks", invoke)
        self.assertNotIn("$process.StartTime.ToUniversalTime().Ticks", invoke)

    def test_create_pipe_streams_are_opened_as_synchronous_handles(self) -> None:
        common = self.read_common()
        native = common[common.index("public sealed class SuspendedProcess") : common.index("function Limit-Text")]
        expected = "new FileStream(new SafeFileHandle(handle, true), FileAccess.Read, 8192, false)"
        self.assertEqual(2, native.count(expected))
        self.assertNotIn(
            "new FileStream(new SafeFileHandle(handle, true), FileAccess.Read, 8192, true)",
            native,
        )

    def test_exit_code_readback_uses_the_native_process_handle(self) -> None:
        common = self.read_common()
        native = common[common.index("public sealed class SuspendedProcess") : common.index("function Limit-Text")]
        exit_code = native[native.index("public int ExitCode") : native.index("public static SuspendedProcess Create")]
        self.assertIn("GetExitCodeProcess", native)
        self.assertIn("GetExitCodeProcess(_processHandle", exit_code)
        self.assertIn("unchecked((int)exitCode)", exit_code)

    def test_stop_boundary_keeps_identity_check_and_surfaces_vanished_pid(self) -> None:
        common = self.read_common()
        stop = common[common.index("function Stop-InstallProcesses") : common.index("function Wait-ScheduledTaskInactive")]
        self.assertIn("ProcessAuthority]::Terminate($processHandle)", stop)
        self.assertNotIn("Stop-Process -Id $processId", stop)
        self.assertIn("ExpectedProcessIdentity", stop)
        self.assertIn("Get-InstallProcessSnapshot -RootPath $RootPath", stop)
        self.assertIn("race_released_process_ids", stop)

    def test_stop_boundary_accepts_caller_snapshot_for_vanished_pid_accounting(self) -> None:
        common = self.read_common()
        stop = common[common.index("function Stop-InstallProcesses") : common.index("function Wait-ScheduledTaskInactive")]
        self.assertIn("$InitialProcessSnapshot", stop)
        self.assertIn("InitialProcessSnapshot", stop)
        self.assertIn("race_released_process_ids", stop)

    def test_installer_release_callers_forward_caller_process_snapshots(self) -> None:
        installer = (ROOT / "scripts" / "install_windows.ps1").read_text(encoding="utf-8")
        self.assertIn(
            "-InitialProcessSnapshot $preMoveInitialProcessSnapshot",
            installer,
        )
        self.assertIn(
            "-InitialProcessSnapshot $preSwapInitialProcessSnapshot",
            installer,
        )

    def test_gitless_manifest_probe_does_not_promote_git_stderr_to_installer_failure(self) -> None:
        common = self.read_common()
        identity = common[
            common.index("function Get-IndependentGitIdentity") : common.index(
                "function Get-SourceManifest"
            )
        ]
        self.assertEqual(4, identity.count("Invoke-NativeChecked"))
        self.assertEqual(0, identity.count("@(& $git.Source"))
        self.assertGreaterEqual(identity.count("-AllowNonZero"), 4)


if __name__ == "__main__":
    unittest.main()
