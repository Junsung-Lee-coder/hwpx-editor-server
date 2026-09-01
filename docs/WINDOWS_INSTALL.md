# Windows installation

This repository is source-only. Install from a clean checkout or from a verified source bundle. The default `.hwpx-install` is a Git-ignored runtime directory inside the checkout, not an untouched checkout; it can contain `.venv`, `.env`, `spool`, receipts, queues, logs, backups, and rendered proof. Pass an external `-InstallRoot` when documents and runtime state must be physically outside the repository.

## Prerequisites

- Windows PowerShell 5.1 or newer.
- A logged-in interactive desktop session. Hancom automation cannot run from session 0.
- CPython 3.13 x86-64. The installer probes the selected interpreter and the
  final venv for implementation, major/minor, pointer width, and machine
  identity before dependency installation.
- Hancom HWP with the `HWPFrame.HwpObject` COM ProgID registered.
- A free loopback port. The default is `127.0.0.1:8765`; use `-ApiPort <free-port>` for an isolated installation when the default is occupied.
- Poppler `pdftoppm`, either on the current `PATH` or at `HWP_PDFTOPPM`.

Do not use a production document as the optional fixture. The fixture is copied for verification and its source SHA-256 is checked before and after the run.

## Source checkout

From PowerShell, run from any working directory:

```powershell
$source = (Resolve-Path .).Path
$install = Join-Path $env:LOCALAPPDATA 'HWPX\hwpx-editor-server'
$receipt = Join-Path $env:LOCALAPPDATA 'HWPX\receipts\install.json'

powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $source 'scripts\install_windows.ps1') `
  -SourceRoot $source `
  -InstallRoot $install `
  -DependencyMode CheckOnly `
  -ReceiptPath $receipt
```

The selected API port is recorded as `api_port` in the installer receipt and in the generated `.env` for a new installation. For example, an isolated installation can use `-ApiPort 18765`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $source 'scripts\install_windows.ps1') `
  -SourceRoot $source `
  -InstallRoot $install `
  -ApiPort 18765 `
  -DependencyMode CheckOnly `
  -ReceiptPath $receipt
```

The installer checks the selected loopback port before venv, config, or scheduled-task mutation. A listener owned by an unrelated process fails closed with bounded owner diagnostics; an existing compatible installation is reused only when its root and port contract match. Do not run two installations on the same port.

Installer self-verification writes its verifier receipt to a fresh external `%TEMP%` path rather than beneath `InstallRoot`. The independent verifier deliberately rejects an explicit receipt path inside an existing install root, so this caller contract must not be weakened; the external path is recorded as `verification_receipt_path` in the installer receipt.

`SourceRoot` and `InstallRoot` must be different paths. The installer verifies the source manifest before mutation. Supply `HWP_SOURCE_MANIFEST` or place `source-manifest.json`, `manifest.json`, or `source_bundle_manifest.json` in `SourceRoot`.

For a Git-less extracted bundle, the installer must receive the release record's independent repository, commit, tree, and (when available) manifest SHA-256. The installer compares these values before creating a venv or changing tasks; it never treats the manifest's self-reported identity as the external binding:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $source 'scripts\install_windows.ps1') `
  -SourceRoot $verifiedSourceRoot `
  -InstallRoot $install `
  -ExpectedRepository 'github:Junsung-Lee-coder/hwpx-editor-server' `
  -ExpectedCommit $commit `
  -ExpectedTree $tree `
  -ExpectedManifestSha256 $manifestSha256 `
  -DependencyMode CheckOnly `
  -ReceiptPath $receipt
```

All three repository/commit/tree values are required together for a Git-less receiver. Missing, partial, malformed, conflicting, or mismatched values fail closed in preflight. A Git checkout still performs its independent Git repository/commit/tree readback; supplying the external values adds a second binding rather than weakening that check.

If Poppler is not already available, the default `CheckOnly` mode fails closed before creating the venv or changing scheduled tasks. An explicit user-scope installation may be requested:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $source 'scripts\install_windows.ps1') `
  -SourceRoot $source `
  -InstallRoot $install `
  -DependencyMode InstallUserScope `
  -ApiPort 18765 `
  -ReceiptPath $receipt
```

Only this mode may invoke `winget.exe`; it uses the user scope and re-resolves the absolute executable in the same installer process. The receipt records an explicit `dependency` phase, whether the renderer was resolvable before the call, and any retained external mutation. A failure after `winget` has been attempted is reported as `FAIL_DEPENDENCY`, never as `FAIL_PREFLIGHT`; inspect the receipt before retrying. Set `HWP_POPPLER_PACKAGE_ID` only when the approved package identity differs from the default. `HWPX_POPPLER_PACKAGE_ID` is a legacy spelling and is not the canonical setting.

For isolated or portable QA, pass an explicit executable without relying on the host PATH or package layout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $source 'scripts\install_windows.ps1') `
  -SourceRoot $source `
  -InstallRoot $install `
  -PopplerPath (Join-Path $portablePoppler 'bin\pdftoppm.exe') `
  -ApiPort 18765 `
  -ReceiptPath $receipt
```

`-PopplerPath` is validated as a regular `.exe` file, takes precedence over environment aliases and discovery, and is forwarded to the verifier. The path is supplied by the operator; the installer contains no machine, user, or version-specific Poppler path.

## Source bundle

A bundle can be built without Git in the receiving environment. Build and verify it from the source checkout, recording the exact commit/tree values supplied by the release operator:

```powershell
$commit = (git rev-parse HEAD).Trim()
$tree = (git rev-parse 'HEAD^{tree}').Trim()
New-Item -ItemType Directory -Force source-bundle | Out-Null
python scripts\build_source_bundle.py `
  --source-root . `
  --archive source-bundle\hwpx-source.zip `
  --manifest source-bundle\source-manifest.json `
  --repository 'github:Junsung-Lee-coder/hwpx-editor-server' `
  --commit $commit `
  --tree $tree

$manifestSha256 = (Get-FileHash -Algorithm SHA256 source-bundle\source-manifest.json).Hash.ToLowerInvariant()
$archiveSha256 = (Get-FileHash -Algorithm SHA256 source-bundle\hwpx-source.zip).Hash.ToLowerInvariant()
python scripts\verify_source_bundle.py `
  --archive source-bundle\hwpx-source.zip `
  --manifest source-bundle\source-manifest.json `
  --destination source-bundle\verified `
  --expected-repository 'github:Junsung-Lee-coder/hwpx-editor-server' `
  --expected-commit $commit `
  --expected-tree $tree `
  --expected-manifest-sha256 $manifestSha256 `
  --expected-archive-sha256 $archiveSha256
```

The expected repository/commit/tree values are an independent release binding; keep them with the authorized release record rather than copying identity values from an untrusted manifest. The manifest and archive SHA-256 values bind the exact bytes to the archive verification. A Git-less receiver must provide all three identity fields plus both independently recorded SHA-256 values. Copy the archive and manifest together. After extraction, pass the extracted directory as `SourceRoot` and set `HWP_SOURCE_MANIFEST=source-manifest.json` if the manifest was copied into that directory. Never copy `.env`, receipts, runtime spools, screenshots, or documents into a bundle.

## Configuration and tasks

For a new installation, the installer creates `.env` from `config.example`, writes the selected `HWP_API_PORT`, and records the resolved absolute Poppler path. If `.env` already exists, it is never overwritten; an explicit `-ApiPort` must match its `HWP_API_PORT` value, and the receipt records its before/after SHA-256 and preservation status. Edit only the local `.env` after installation, then rerun verification.

The installer registers `hwpx-editor-api` and `hwpx-editor-worker` for the interactive user. Each action uses the installation venv and each task has the exact installation root as its working directory. Existing tasks that point to another root fail closed unless `-ReplaceExistingTasks` is explicitly supplied.

The installer creates one JSON receipt. Read it before treating the installation as complete. The meaningful states are:

- `PASS`: runtime and optional fixture E2E passed.
- `PASS_RUNTIME_ONLY`: runtime verification passed without a fixture.
- `FAIL_PREFLIGHT`: no installation mutation should have occurred.
- `FAIL_INSTALL`: venv, dependency, or config preparation failed.
- `FAIL_ACTIVATION`: task registration or activation failed.
- `ROLLED_BACK`: the candidate failed and the saved task/root state was restored.

For verification, use `docs/WINDOWS_VERIFY.md`. For recovery, use `docs/WINDOWS_ROLLBACK.md`.
