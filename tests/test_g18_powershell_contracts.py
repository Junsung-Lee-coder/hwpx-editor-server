from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class G18PowerShellContractTests(unittest.TestCase):
    def test_preserve_move_seals_inventory_only_after_task_and_process_quiescence(self) -> None:
        source = (ROOT / 'scripts' / 'install_windows.ps1').read_text(encoding='utf-8')
        snapshot_seal = source.index('Seal-InstallerSnapshot -TaskStateOverride')
        task_quiesce = source.index('foreach ($taskName in $taskNames)', snapshot_seal)
        process_quiesce = source.index('$preMoveProcessRelease', task_quiesce)
        task_unregister = source.index('Unregister-ScheduledTask', process_quiesce)
        inventory = source.index('$preMoveBackupInventory = Get-InstallInventory', task_unregister)
        root_identity = source.index('$preMoveRootIdentity', inventory)
        self.assertLess(snapshot_seal, task_quiesce)
        self.assertLess(process_quiesce, inventory)
        self.assertLess(snapshot_seal, task_unregister)
        self.assertLess(task_unregister, inventory)
        self.assertLess(inventory, root_identity)
        self.assertIn('object_identity', source[inventory:source.index('Move-PathIdentityExact -Source $install -Destination $backupRoot', inventory)])

    def test_preserve_move_disables_task_admission_before_process_quiescence(self) -> None:
        source = (ROOT / 'scripts' / 'install_windows.ps1').read_text(encoding='utf-8')
        self.assertIn('$preMoveTaskIdentity[$taskName]', source)
        task_identity = source.index('$preMoveTaskIdentity[$taskName]')
        snapshot_seal = source.index('Seal-InstallerSnapshot -TaskStateOverride')
        disable = source.index('Disable-ScheduledTask', task_identity)
        process_quiesce = source.index('$preMoveProcessRelease', disable)
        unregister = source.index('Unregister-ScheduledTask', process_quiesce)

        self.assertLess(task_identity, disable)
        self.assertLess(snapshot_seal, disable)
        self.assertLess(disable, process_quiesce)
        self.assertLess(process_quiesce, unregister)
        self.assertIn('-TaskIdentityOverride', source[snapshot_seal:disable])

    def test_restore_requires_snapshot_hash_and_stable_identity_before_side_effects(self) -> None:
        source = (ROOT / 'scripts' / 'windows_install_common.psm1').read_text(encoding='utf-8')
        restore = source.index('function Restore-InstallSnapshot')
        body = source[restore:]
        self.assertIn('$ExpectedSnapshotSha256', body)
        self.assertIn('$ExpectedSnapshotIdentity', body)
        self.assertIn('Read-VerifiedInstallSnapshot', body)
        self.assertLess(body.index('Read-VerifiedInstallSnapshot'), body.index('Stop-InstallProcesses'))

    def test_native_timeout_requires_owned_job_and_launch_identity(self) -> None:
        source = (ROOT / 'scripts' / 'windows_install_common.psm1').read_text(encoding='utf-8')
        native = source[source.index('function Invoke-NativeChecked'):source.index('function Get-InstallProcessSnapshot')]
        process_tree = source[source.index('function Stop-NativeProcessTree'):source.index('function Invoke-NativeChecked')]
        self.assertIn('Job', native)
        self.assertIn('launch_identity', native)
        self.assertIn('identity_capture_failed', native)
        self.assertIn('late_spawn', native)
        self.assertIn('$nativeJobTerminated', native)
        self.assertIn('job_object_terminated = [bool]$nativeJobTerminated', native)
        self.assertIn('$lateSpawnObserved', process_tree)
        self.assertIn('-not $initialByPid.ContainsKey($candidatePid)', process_tree)
        self.assertIn('late_spawn = [bool]$lateSpawnObserved', process_tree)


if __name__ == '__main__':
    unittest.main()
