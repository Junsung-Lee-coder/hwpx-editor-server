# Windows verification

`verify_windows.ps1` is independent and read-only with respect to the installed root, scheduled tasks, documents, and fixture source. It runs from any caller directory when `-InstallRoot` is explicit and records direct native exit codes rather than inferring results from transcript text. Each invocation has a bounded `run_id`; an existing `-ReceiptPath` must be paired with an explicit `-RunId`, while omitted paths receive a unique external receipt path. Same-path concurrent admission fails closed.

## Runtime-only verification

```powershell
$install = Join-Path $env:LOCALAPPDATA 'HWPX\hwpx-editor-server'
$receipt = Join-Path $env:LOCALAPPDATA 'HWPX\receipts\verify.json'
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File (Join-Path $install 'scripts\verify_windows.ps1') `
  -InstallRoot $install `
  -ApiPort 18765 `
  -ReceiptPath $receipt
```

For a deliberate receipt replacement, supply a fresh invocation identity:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $install 'scripts\verify_windows.ps1') `
  -InstallRoot $install -ReceiptPath $receipt -RunId 'operator-verify-001'
```

Omit `-ApiPort` to resolve the port from the installation `.env`; when no `.env` override exists, the default is `127.0.0.1:8765`. The verifier records `api_port` and `api_base_url`, builds every health/readiness and local-CLI URL from that value, and passes `--base-url` to fixture CLI commands so a cached legacy session URL cannot redirect the check to another service.

The verifier checks:

- installed Python identity and source manifest per-file hashes;
- both scheduled task identities, action executables, working directories, principals, logon triggers, run levels, settings, and identity hashes;
- the worker process and loopback listener identities against the exact installed venv/root and module;
- Poppler resolution, including explicit path, current `PATH`, and validated WinGet roots;
- loopback API health and runtime-readiness;
- machine/root/task/port lifecycle locking, candidate-generation binding, and a final root/manifest/marker/readiness readback before PASS;
- the publication/static smoke gate;
- optional advisory full discovery when `-RunFullDiscovery` is supplied.

Windows PowerShell 5.1 can persist `-LogonType Interactive` as `InteractiveToken` and omit the default `Limited` run level from task XML. The installer and verifier treat only those two documented readback forms as equivalent to the requested `Interactive`/`Limited` contract; raw persisted values remain available in the task identity receipt for diagnostics.

Every command record includes its name, executable, arguments, working directory, explicit base URL, candidate generation, bounded stdout/stderr, captured byte counts, truncation flags, output encoding/fallback diagnostics, and direct native exit code. Native output is decoded as strict UTF-8 first and falls back to the Windows system code page when a tool emits locale-specific bytes; the raw byte counts and bounded capture remain authoritative. `publication_gate` and `advisory_test_debt` are separate receipt sections. A repository test failure must not be silently converted to a runtime failure.

## Fixture verification

Use a disposable, approved `.hwp` or `.hwpx` fixture outside the source checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File (Join-Path $install 'scripts\verify_windows.ps1') `
  -InstallRoot $install `
  -FixturePath 'C:\QA\fixtures\sample.hwpx' `
  -ApiPort 18765 `
  -ReceiptPath (Join-Path $env:LOCALAPPDATA 'HWPX\receipts\verify-fixture.json')
```

The managed sequence is `open -> status -> where -> page-screenshot -> close -> status`. The verifier runs it with a disposable CLI state file and checks that:

- the original fixture SHA-256 is unchanged;
- open JSON identifies the exact managed fixture, session, and working copy;
- every command returns parseable JSON and remains bound to the same explicit API URL, candidate generation, and live session until close;
- the close JSON and post-close status report a closed session;
- the rendered PNG is a valid PNG with positive dimensions and sufficient non-white content in a deterministic bounded scan;
- the page proof manifest exists, identifies the managed source/session/requested page, and matches the PNG size/SHA-256;
- every sequence command returned exit code `0`.

The temporary managed copy and proof files are removed after their hashes and dimensions are recorded. The receipt retains the proof SHA-256, byte count, dimensions, and command records; it does not publish the fixture or rendered proof.

## Receipt interpretation

- `PASS` with status code `0` means all required checks in that invocation passed.
- `PASS_WITH_ADVISORY_TEST_DEBT` with status code `0` means the runtime/publication gates passed but the optional `-RunFullDiscovery` command returned nonzero; inspect `advisory_test_debt` before promoting the result.
- `VERIFIER_ERROR` means the verifier itself could not establish a trustworthy result; inspect `errors` and do not treat it as a product PASS.
- `FAIL_RECEIPT` means the receipt could not be durably committed or read back; do not treat any in-memory result as authoritative.
- `FAIL_PREFLIGHT`, `FAIL_TASK`, `FAIL_API`, `FAIL_WORKER`, `FAIL_RENDERER`, `FAIL_PUBLICATION`, and `FAIL_NATIVE_E2E` identify the first failing gate and bound the next diagnostic step.

Never edit a receipt to change a verdict. Re-run the verifier after correcting the reported condition and write a new receipt path.
