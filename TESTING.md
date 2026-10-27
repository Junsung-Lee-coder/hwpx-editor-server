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

For portable Linux validation, install the hash-pinned `requirements-portable.lock` in a temporary environment. The Windows lock file remains the installation input on Windows; do not claim that a Linux environment proves pyhwpx/Hancom behavior. These tests cover local planners, parsers, readback helpers, command-package contracts, portability, source bundles, installer contracts, service helpers, the targeted four-margin getter (`tests/test_cell_margins_get.py`), the shared MCP schema (`tests/test_mcp_schema.py`), optional API token authentication (`tests/test_api_auth.py`), exact table-structure planning, grid readback and service flow against a fake table (`tests/test_table_structure_exact.py`), malformed-input handling in parsing helpers (`tests/test_narrowed_exception_handlers.py`), object insertion and layout edits against fake Hancom models (`tests/test_object_insert_exact.py`, `tests/test_layout_exact.py`, `tests/test_feature_parity_integration.py`), local HWPX reading tools (`tests/test_hwpx_reader.py`), and blank-document generation and Mermaid rendering (`tests/test_hwpx_new_and_mermaid.py`).

The page-restart evidence regressions use two hash-pinned, disposable generated-document snapshots bundled under `tests/fixtures/page_restart`; no private document or external workspace is required. A bounded Windows/Hancom run confirmed start 7, bottom-center, no decoration, save/reopen and the caller's preservation checks. This is not full feature acceptance: rendered page-by-page increments, other settings, insert/delete renumbering, Undo and native official-document numbering remain unverified.

The page-number and official-numbering tests (`tests/test_native_page_number.py`, `tests/test_native_official_numbering.py`, `tests/test_native_numbering_surfaces.py`) drive a faithful fake of the documented `HAction`/`HParameterSet` calls from `tests/native_numbering_fakes.py`. `tests/test_native_numbering_census.py` covers the in-memory HWPML2X protection reader on synthetic samples (its element names are not verified against real Hancom output), and `tests/test_native_numbering_repair.py` and `tests/test_native_numbering_bundle_translation.py` cover the four-level outline definition, header/footer protection and the unknown-outcome translation through `LocalCliService.command_bundle`. `tests/test_native_numbering_preservation.py` adds synthetic structure corruptions (character height, paragraph indent, picture and table size, definitions, unreferenced media) that must be refused before a mutation when unprovable and reported as unknown outcomes afterwards, driven through the library, `command_bundle`, `apply_edit_operations` and the worker edit path. They prove call sequencing, validation, refusal with zero mutation, and CLI/MCP/service parity; they are not Hancom execution evidence. `tests/test_native_numbering_lifetime_safety.py` covers two public-caller safety rules on the same synthetic fakes: the queued edit-job runtime (`app.worker.edit_and_convert_with_pyhwpx`, the `/edit-and-convert` and `/edit-and-convert-from-path` submissions and `/capabilities`) refuses any job containing `page_number` or `official_numbering`: `edit_and_convert_with_pyhwpx` raises before its own COM initialization, SDK import/constructor, open, any edit and any quit/CoUninitialize (the surrounding `process_one_command` still runs its native export preflight first, which constructs and quits an empty probe instance, so the whole child process does not perform zero SDK work), because that worker closes the SDK and uninitializes COM in its own `finally` and so cannot keep the document open after an uncertain numbering outcome (the supported-op control job is unchanged); and neither the page-number library nor the bundle `official_numbering_apply` step moves the caret after an uncertain outcome, while success and clean pre-mutation refusals still restore it. `tests/test_native_numbering_uncertainty_propagation.py` covers the official-numbering uncertainty boundary on the same fakes: once `OutlineNumber` or `ParagraphShape` has been issued, an ordinary failure in a later readback, census or preservation comparison is reported as an unknown outcome with the original cause (through the library, the bundle step and the real `command_bundle`, which then records the step dirty), while `KeyboardInterrupt`, `SystemExit` and another `BaseException` propagate unchanged, tagged as unknown, with no caret move, retry, Undo, Close, Clear, Quit or discard from this code. `page_number` has the same boundary: after `PageNumPos` is issued, an ordinary failure (including a raw error in the post-call comparison) is an unknown outcome with its cause, and a `BaseException` propagates unchanged and tagged. `tests/test_native_numbering_runtime_custody.py` drives the real `LocalCliRuntimeManager`, the live session's owning thread, the real `command_bundle` and the real numbering callbacks over fake SDK/COM objects only. At the protected owned command boundary, a numbering step that reached a native mutator and ended unknown (a returned unknown outcome or a control-flow signal) holds the session: the command is recorded as `failed` with `may_have_mutated=true` and the original signal in its cause chain, the caller receives a noncomplete result (`ok=false`, `dirty=true`, `native_custody_held=true`), and the owning thread and registry binding stay in place with no Clear, Quit, CoUninitialize, navigation, Undo, save, retry or new native callback. Later execute, close (including forced), reap, and reconcile/acknowledge calls without preserved evidence do not release the hold; journal, status-write and pruning errors are reported on the held command and do not release it. Release requires an operator action that this repository does not implement, so a held session lasts until its process ends. The caller-owned library keeps propagating the original signal. `tests/test_native_numbering_retention_races.py` covers two held-session races on the same fakes: a close admitted while a numbering callback was still in flight is refused as held when the owning thread dequeues it (it is recorded `failed`, never `succeeded`, and the owning thread keeps servicing its queue), and a `BaseException` raised by the journal or pruning reporter after the hold is published is recorded on the held command, resolves the waiting caller without a timeout and does not end the owning thread. These are source tests against fakes, not Hancom evidence. N1/N2 job-API parity is therefore not provided; the caller-owned library and the protected interactive CLI/MCP `command_bundle` paths are the supported surfaces.

The user-workflow regressions exercise the real CLI parser, named-bundle builder, service/package dispatch, native numbering mutators and protection reader over the same synthetic SDK/HWPML2X fixtures:

```bash
python -m unittest -v tests.test_numbering_user_workflow tests.test_numbering_bundle_cli_status
```

They cover all six page positions, start-number/dash parameter forwarding, four sequential official levels using one native outline definition, preservation of unrelated paragraphs/formatting/tables/pictures/headers/footers, hyphen-leading match text, no-op dirty deltas without clearing prior dirtiness, and failure exit codes/evidence through direct commands and `bundle-run`/`bundle`/`tx-commit`. Only transport, session ownership/persistence, page evidence and wrapper location snapshots are fixtures; generic JSON/transaction loader boundaries are also fixtures in the exit-status tests. The tests do not launch Hancom or prove save/reopen, rendered output, automatic renumbering or Undo. `tests/test_native_numbering_surfaces.py` keeps an explicit `ok=true` in its successful backend stub to match the actual response contract.

The MCP adapter tests (`mcp_tests/`) need the MCP dependencies. On Linux, run `python -m unittest discover -s mcp_tests -p "test_*.py"` in a supplement environment; this is not a qualification of the Windows `requirements-mcp.lock`.

The local CLI custody tests also cover managed-root identity replacement, symlinked artifact paths, custody hash mismatches, public route suppression for unmanaged or closed sessions, and streaming from an already validated file handle. On POSIX, the response test replaces the path after the handle is opened and requires the original bytes; on Windows, native file-sharing semantics prevent that replacement until the response closes.

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

For this source-only snapshot, the deterministic publication gate is the syntax check, all fourteen static smoke checks above, and these dependency-light unit modules:

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

The MCP adapter tests verify that `opened_via` is retained as the managed-session origin, bridge-only and closed-only markers are rejected before mutation, closed sessions remain status-queryable but cannot receive a POST, and the adapter does not expose a backend filesystem path as a download route. The `hwpx_cell_margins_get` cases cover tenth-tool discovery, the canonical request body, a fully proven positive result, and the reject paths for a fabricated success, a missing margin side, cached provenance, a restore failure, and a timed-out read. These are fake-backend contract tests, not Hancom execution evidence.

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

PowerShell syntax is checked on a Windows runner. The Linux source gates can only perform static contract checks; they cannot prove scheduled-task, WinGet, COM, or native renderer behavior. Follow `docs/WINDOWS_INSTALL.md` for install and `docs/WINDOWS_VERIFY.md` for the independent read-only receipt. Use `docs/WINDOWS_ROLLBACK.md` for a disposable rollback rehearsal and never run it against a live installation. Installer, verifier, writer, and generated `.env` use the canonical `HWP_API_TASK_NAME`, `HWP_WORKER_TASK_NAME`, `HWP_API_PORT`, `HWP_PDFTOPPM`, and `HWP_SOURCE_MANIFEST` names; legacy Poppler path aliases are compatibility-only.

The focused Windows PowerShell 5.1 proof regression creates disposable valid, blank, and corrupt PNGs and exercises the verifier's bounded content detector without opening Hancom:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests\windows\test_verify_windows_proof_png.ps1
```

The G13 Windows PowerShell 5.1 regression exercises code-page native output from a non-ASCII working directory, verifies bounded fallback diagnostics, and proves the independent verifier still rejects an in-root receipt:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests\windows\test_g13_receipt_unicode.ps1
```
