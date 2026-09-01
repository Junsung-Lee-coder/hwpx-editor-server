# Windows rollback

The installer is fail-closed before mutation and records a task/root snapshot before activation. The hash-bound transaction snapshot is retained after a successful run; do not delete the receipt, backup root, or snapshot until the result has been reviewed and an explicit retention decision has been recorded.

## Automatic rollback

For failures after the preflight phase, `install_windows.ps1` stops the candidate tasks, removes the candidate root, restores the saved scheduled-task XML, and moves a `PreserveMove` backup root back to the requested install path. The receipt changes to `ROLLED_BACK` when restoration succeeds and records `rollback.restore_error` if restoration itself needs operator attention.

A failed preflight (`FAIL_PREFLIGHT`) should not have created a venv, candidate root, or task mutation. Confirm this from the receipt and the task identities before retrying.

## Preserve an existing installation

The default is `-ExistingInstallDisposition Fail`. If the requested install root contains an unrelated directory or an installation with a different source identity, the installer stops without overwriting it. To authorize a reversible move:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File (Join-Path $source 'scripts\install_windows.ps1') `
  -SourceRoot $source `
  -InstallRoot $install `
  -ExistingInstallDisposition PreserveMove `
  -ReplaceExistingTasks `
  -DependencyMode CheckOnly `
  -ReceiptPath $receipt
```

The old root is moved to a run-owned sibling such as `hwpx-editor-server.backup-<run-id>`. Its file count, byte total, and object identity are recorded. Existing spool files are not deleted. Keep that sibling and the retained transaction snapshot until the new installation has passed `verify_windows.ps1` and the retention decision is explicit.

## Manual recovery when automatic rollback reports an error

1. Do not start either task again.
2. Read `rollback.restore_error`, `snapshot_path`, and `rollback.backup_root` from the install receipt.
3. Confirm the old task XML and working directories from the snapshot and the receipt. Do not paste credentials or document paths into a ticket.
4. If a candidate task remains, stop and unregister only the named `hwpx-editor-api` and `hwpx-editor-worker` tasks after confirming their action points to the failed candidate root.
5. Restore the backup sibling to the requested install root only when that path is absent.
6. Re-run the read-only verifier with a new receipt path.

The common module exposes `Restore-InstallSnapshot` for an operator-approved recovery script. Use the exact snapshot path recorded in the receipt; do not guess a task name or root. The function restores the previous XML and unregisters tasks that did not exist in the snapshot.

## Forced activation-failure rehearsal

Use a disposable test directory and a synthetic fixture only. Do not run a rehearsal against the live book installation or production scheduled tasks. A safe rehearsal records:

- the pre-install task XML/root identity;
- the candidate and backup roots;
- the forced activation failure and direct exit code;
- the post-rollback task XML/root identity;
- the backup inventory and source/fixture hashes.

The expected terminal state is `ROLLED_BACK` with the original root and task identities restored. If identity or source immutability cannot be proven, stop and treat the run as an operational failure rather than deleting state.
