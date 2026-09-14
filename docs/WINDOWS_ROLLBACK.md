# Windows rollback

The installer is fail-closed before mutation and records a task/root snapshot before activation. After successful terminal cleanup, the transaction snapshot and journal are removed; retain the install receipt. A `PreserveMove` backup root remains only when that run created one, while failed or incomplete rollback may retain hash-bound recovery material. Check the receipt before assuming any backup or snapshot exists.

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

The old root is moved to a run-owned sibling such as `hwpx-editor-server.backup-<run-id>`. Its file count, byte total, and object identity are recorded. Existing spool files are not deleted. Keep that sibling while the new installation is being verified. The transaction snapshot and journal are normally removed after successful terminal cleanup; use the receipt to determine whether a `PreserveMove` backup or failed-run recovery material remains.

## Manual recovery when automatic rollback reports an error

1. Do not start either task again.
2. Read `rollback.restore_error`, `snapshot_path`, and `rollback.backup_root` from the install receipt.
3. Confirm the old task XML and working directories from the snapshot and the receipt. Do not paste credentials or document paths into a ticket.
4. If a candidate task remains, stop and unregister only the named `hwpx-editor-api` and `hwpx-editor-worker` tasks after confirming their action points to the failed candidate root.
5. Restore the backup sibling to the requested install root only when that path is absent.
6. Re-run the read-only verifier with a new receipt path.

The common module exposes `Restore-InstallSnapshot` for an operator-approved recovery script. Use the exact snapshot path recorded in the receipt; do not guess a task name or root. The function restores the previous XML and unregisters tasks that did not exist in the snapshot.

## Recovering a timed-out Hancom command

The local-CLI backend quarantines a document when a native command exceeds its wait limit. This is different from installer rollback: the Hancom document remains owned by its original session, ordinary commands and `close` are fenced, and a retry must not create a second native command.

1. Keep the session ID and command ID from the timeout response. Do not replay the timed-out command.
2. Query the session status and call `command_reconcile` with the same session ID and command ID.
3. If reconciliation is pending, wait and query the same command again. Do not close the session or remove its managed root.
4. Treat a response that says the native recovery snapshot is unavailable, missing, or failed verification as unknown. Keep the binding and logs for operator review; do not claim that the document was recovered.
5. Only after a recovery artifact has been committed with its managed relative path, size, and SHA-256 may the session be released and closed. Download any artifact that must be kept before closing, because explicit close removes the server-managed working copy and temporary output.

The recovery snapshot is a new native `SaveAs` output. It does not overwrite the source file or silently replace the working copy. A native save that returns anything other than an affirmative success result, produces no regular nonempty file, or changes during custody verification is a recovery failure. A hung or dirty native process remains fenced; do not kill it merely because the client-side timeout elapsed.

## Truthful cleanup and native limitations

The runtime needs a visible Hancom window on a logged-in interactive Windows desktop; minimized or unfocused windows, a locked or disconnected desktop, and background-only operation are not supported.

The Hancom document is created with `new=True`, but that argument does not establish exclusive ownership of a new native process before COM activation takes effect. A construction error can therefore arrive after an unrelated Hancom object was contacted or a process was started. The construction helper makes one attempt and reports the original failure; it does not retry, because a retry could turn that failure into an apparent success. When no native handle was returned, automated cleanup stays `unconfirmed` even if the Python call ended without an exception, and cleanup is `confirmed` only after a returned handle was closed and an applicable COM teardown succeeded.

A pending reconciliation reply uses HTTP 200 with `ok=false`, `reconciled=false`, and `reconciliation="pending"`. It is an observation, not a success: the native outcome is unknown, nothing was replayed, and the same command ID is preserved. Ordinary status queries can succeed while the native command they describe is still pending.

Recovery observation is bounded. After a timed-out command, allow one recovery request with a caller wait of at most 125 seconds, then stop automated observation. If the outcome is unresolved, preserve the session binding, logs, and existing artifacts for operator review; the bound does not guarantee that the native process has terminated, and no cleanup is confirmed while its result is unknown.

## Forced activation-failure rehearsal

Use a disposable test directory and a synthetic fixture only. Do not run a rehearsal against a live installation or production scheduled tasks. A safe rehearsal records:

- the pre-install task XML/root identity;
- the candidate and backup roots;
- the forced activation failure and direct exit code;
- the post-rollback task XML/root identity;
- the backup inventory and source/fixture hashes.

The expected terminal state is `ROLLED_BACK` with the original root and task identities restored. If identity or source immutability cannot be proven, stop and treat the run as an operational failure rather than deleting state.
