# Testing

The runtime has two distinct test surfaces:

1. Local checks that do not start Hancom.
2. Windows checks that exercise the native desktop runtime.

## Local syntax check

```bash
python -m compileall -q app local_cli_v1 scripts tests
```

## Unit tests

After installing the locked Windows dependencies in a Windows virtual environment:

```bash
python -m unittest discover -s tests -v
```

For portable Linux validation, install the hash-pinned `requirements-portable.lock` in a temporary environment. The Windows lock file remains the installation input on Windows; do not claim that a Linux environment proves pyhwpx/Hancom behavior. These tests cover local planners, parsers, readback helpers, command-package contracts, portability, source bundles, installer contracts, and service helpers.

## Static smoke checks

The following scripts validate local CLI and command-bundle contracts without opening a document. The final envelope smoke uses an OS cache path and creates no repository fixture:

```bash
python scripts/smoke_cli_workflow_status_static.py
python scripts/smoke_local_cli_health_static.py
python scripts/smoke_output_parser_static.py
python scripts/smoke_command_bundle_static.py
python scripts/smoke_command_packages_static.py
python scripts/smoke_native_table_command_static.py
python scripts/smoke_export_proof_range_clamp_static.py
python scripts/smoke_find_context_static.py
python scripts/smoke_cli_json_envelope_parity_static.py
python scripts/smoke_readback_diff_static.py
python scripts/smoke_readback_schema_static.py
python scripts/smoke_selection_proof_static.py
python scripts/smoke_text_table_cleanup_static.py
python scripts/smoke_cli_envelope_static.py
```

## Publication gate

For this source-only snapshot, the deterministic publication gate is the syntax check, all thirteen static smoke checks above, and these dependency-light unit modules:

```bash
python -m unittest \
  tests.test_local_cli_status_payload \
  tests.test_raw_target_readback \
  tests.test_readback_command_status \
  tests.test_readback_diff \
  tests.test_table_scoped_cell_and_list_primitives \
  tests.test_table4_anchor_range_replace_bundle
```

Full discovery is a required source gate for this candidate. If it fails, report the exact failing test and do not relabel the result as a runtime-only PASS.

## Source-bundle checks

The builder uses Git-tracked paths when Git is present and a safe Git-less walk otherwise. A Git-backed build requires a clean worktree and verifies each staged member against the named commit tree; Git-less manifests explicitly record `identity_verified=false`. It rejects symlinks, unsafe members, duplicate case-folded paths, runtime output, and oversized source/archive resources. The archive and manifest are deterministic for the same source tree:

```bash
python scripts/build_source_bundle.py \
  --source-root . \
  --archive /tmp/hwpx-source.zip \
  --manifest /tmp/hwpx-source-manifest.json \
  --repository github:Junsung-Lee-coder/hwpx-editor-server \
  --commit "$(git rev-parse HEAD)" \
  --tree "$(git rev-parse 'HEAD^{tree}')"
manifest_sha256="$(sha256sum /tmp/hwpx-source-manifest.json | cut -d ' ' -f 1)"
python scripts/verify_source_bundle.py \
  --archive /tmp/hwpx-source.zip \
  --manifest /tmp/hwpx-source-manifest.json \
  --destination /tmp/hwpx-source-verified \
  --expected-repository github:Junsung-Lee-coder/hwpx-editor-server \
  --expected-commit "$(git rev-parse HEAD)" \
  --expected-tree "$(git rev-parse 'HEAD^{tree}')" \
  --expected-manifest-sha256 "$manifest_sha256" \
  --expected-archive-sha256 "$(sha256sum /tmp/hwpx-source.zip | cut -d ' ' -f 1)"
```

The expected repository, commit, and tree are an independent release binding; the manifest and archive digests are not trusted from the bundle itself. A Git-less receiver must supply all three identity values plus the authorized manifest and archive SHA-256 values.

## Native checks

Native checks must run on a Windows desktop session with Hancom available. Use a disposable or managed copy of a document, capture rendered proof, and keep all outputs outside the repository. Do not run these checks against customer or submission documents. The shipped verifier gives fixture commands an isolated `HWPX_LOCAL_STATE_PATH`, uses an explicit `--base-url`, parses each JSON response, and binds the session, working copy, requested page, proof-manifest output hash, and candidate generation before reporting success.

For the native border persistence seam, write the selected-cell value maps captured immediately before save/quit and after reopening as bounded JSON objects, then seal them from the installed candidate root:

```text
hwpx native-border-readback --pre-quit <pre.json> --persisted <reopened.json> --target-identity <target.json> --out <native-border-readback.json>
hwpx proof-packet --out-dir <packet-dir>
```

The producer requires the complete `.hwpx-install.json` repository/commit/tree/manifest identity, atomically records `last_native_border_readback_path` in local state, and never calls the API or mutates the document. `proof-packet` copies and hashes the artifact only when candidate generation, source manifest hash, target identity, and both non-empty readback maps match.

The server is local by default. Verify health and readiness before relying on an edit command:

```text
GET /health
GET /runtime-readiness
GET /capabilities
```

Do not add the resulting documents, logs, screenshots, or runtime state to Git.

## Windows installer and verifier

PowerShell syntax is checked on a Windows runner. The Linux source gates can only perform static contract checks; they cannot prove scheduled-task, WinGet, COM, or native renderer behavior. Follow `docs/WINDOWS_INSTALL.md` for install and `docs/WINDOWS_VERIFY.md` for the independent read-only receipt. Use `docs/WINDOWS_ROLLBACK.md` for a disposable rollback rehearsal and never run it against the live book installation. Installer, verifier, writer, and generated `.env` use the canonical `HWP_API_TASK_NAME`, `HWP_WORKER_TASK_NAME`, `HWP_API_PORT`, `HWP_PDFTOPPM`, and `HWP_SOURCE_MANIFEST` names; legacy Poppler path aliases are compatibility-only.

The focused Windows PowerShell 5.1 proof regression creates disposable valid, blank, and corrupt PNGs and exercises the verifier's bounded content detector without opening Hancom:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests\windows\test_verify_windows_proof_png.ps1
```

The G13 Windows PowerShell 5.1 regression exercises code-page native output from a non-ASCII working directory, verifies bounded fallback diagnostics, and proves the independent verifier still rejects an in-root receipt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests\windows\test_g13_receipt_unicode.ps1
```
