# HWPX editor/server

Windows-native HWPX editing runtime with a local HTTP API and a thin command-line client. The runtime uses Hancom automation through `pyhwpx`; the CLI builds explicit command bundles, sends them to the server, and formats structured results locally.

This repository is source-only. It does not include working documents, generated files, uploads, rendered output, queues, logs, databases, screenshots, credentials, or machine-specific runtime state.

This core preview contains the document-processing runtime, local HTTP API, and local CLI. The optional MCP adapter and its adapter-specific dependencies are intentionally not included.

## Requirements

- Windows with a logged-in desktop session for Hancom automation.
- Python 3.13 and a Hancom installation that supports the automation calls used by `pyhwpx`.
- The Windows installer uses the hash-pinned `requirements-windows.lock` dependency set.
- Poppler `pdftoppm`, either on `PATH` or configured with `HWP_PDFTOPPM`.

The server defaults to loopback address `127.0.0.1` and port `8765`. Set `HWP_API_HOST`, `HWP_API_PORT`, and the other `HWP_*` settings in a local `.env` file when needed. `.env` is ignored by Git; `config.example` is the redacted configuration template.

## Setup

From a Windows command prompt, the supported portable installer is:

```bat
scripts\setup_venv.bat
```

The wrapper invokes `scripts\install_windows.ps1` in user-scope dependency mode and installs into `.hwpx-install`. That default is a portable, Git-ignored install inside the checkout: runtime state such as `.venv`, `.env`, `spool`, receipts, and proof output can be created there, so the checkout is not physically untouched. Use an external `-InstallRoot` when the source checkout must remain free of runtime state. For explicit preflight-only or separate source/install roots, use the PowerShell command documented in `docs/WINDOWS_INSTALL.md`. The installer copies `config.example` only when `.env` is absent; edit `.env` only on the local machine and never commit it.

Read `docs/WINDOWS_INSTALL.md` before installing, `docs/WINDOWS_VERIFY.md` before accepting a receipt, and `docs/WINDOWS_ROLLBACK.md` before replacing an existing root or task.

## Run

The packaged launcher is manual and on-demand when a task-based installation is not desired:

```bat
scripts\writer_v1_manual.cmd status
scripts\writer_v1_manual.cmd start
python -m local_cli_v1.main help
scripts\writer_v1_manual.cmd stop
```

The same manual launcher is available from the installed runtime at `.hwpx-install\scripts\writer_v1_manual.cmd`; it selects the packaged `.hwpx-install\.venv` and the task names in that root's canonical `HWP_*` `.env` settings. The installer is the supported path for creating or replacing the interactive scheduled tasks.

For direct development entry points:

```bat
.venv\Scripts\python -m app.api_server
.venv\Scripts\python -m app.worker
```

The CLI resolves its base URL in this order: `--base-url`, the cached URL from `python -m local_cli_v1.main open`, `HWPX_BASE_URL`, then `http://127.0.0.1:8765`.

A normal edit loop is:

```text
status -> open -> find/where/select -> edit -> render proof -> save -> close
```

Work on managed copies. Review rendered proof before treating a mutation as complete; page count alone is not validation.

## Hancom automation limits

The runtime needs a visible Hancom window on a logged-in Windows desktop. Minimized windows, an unfocused window, a locked or disconnected desktop, and background-only operation are not supported, and no successful response certifies them.

The runtime constructs the Hancom object with `new=True`, but that argument does not establish exclusive ownership of a new native process before COM activation takes effect. An error from a constructor call may therefore arrive after an unrelated Hancom object was contacted or a process was started, and a later automated cleanup step cannot confirm anything about a process this code never received a handle for. The construction helper makes one attempt and reports the original failure instead of retrying.

A pending reconciliation reply answers with `ok=false`, `reconciled=false`, and `reconciliation="pending"` while the HTTP status stays 200. That reply is an observation, not a success: the native outcome is unknown, nothing was replayed, and the command keeps its identity. Ordinary status queries can succeed while the command they describe is still pending, so a query success never means the edit succeeded.

Keep the session and command IDs and the original document when a command times out. Observation is bounded: one recovery request with a caller wait of at most 125 seconds, then automated observation stops. If the outcome is still unresolved, the binding, logs, and existing artifacts are preserved for an operator decision; these limits do not guarantee that the native process has ended.

## Layout

- `app/` — HTTP routes, runtime state, Hancom worker, editing operations, command packages, and artifact handling.
- `local_cli_v1/` — local planner, command-bundle registry, transport, output parser, and readback helpers.
- `python -m local_cli_v1.main` — public CLI entry point.
- `scripts/` — Windows launcher and dependency-light static smoke checks.
- `tests/` — unit tests for pure helpers and local contracts.
- `fixtures/local_cli/` — synthetic JSON responses used by local tests.

## Testing

See `TESTING.md`. The dependency-light syntax gate is safe to run on any platform:

```bash
python -m compileall -q app local_cli_v1 scripts tests
```

The full runtime and native Hancom checks require Windows and the installed dependencies. Do not use production documents as test fixtures.

The deterministic source bundle and manifest commands are documented in `docs/WINDOWS_INSTALL.md`. Native installation verification is read-only after activation and is performed by `scripts\verify_windows.ps1`; Linux checks do not prove native Windows/Hancom runtime behavior.

## Checkout and runtime-state boundary

The Git checkout contains source and test inputs. Git-ignored runtime state is kept inside the checkout by the default portable install, in a separate directory named `.hwpx-install` below that checkout, and its scheduled tasks use that directory as their working directory. The installed runtime may therefore create `.venv`, `.env`, `spool`, receipts, queues, logs, backups, and rendered proof below `.hwpx-install`; Git ignore rules prevent accidental tracking but do not make those paths absent or immutable. Set `-InstallRoot` to an external local-data directory when that separation is required. Keep all documents and runtime state outside the tracked source files.

## Data boundary

Keep documents and runtime state outside the repository. In particular, do not add `.hwp`, `.hwpx`, `.pdf`, `.docx`, `.pptx`, or `.xlsx` files, or files from `spool/`, `uploads/`, `output/`, `logs/`, or `backups/`. Use synthetic fixtures for local tests.

Local CLI artifact responses expose route URLs rather than server filesystem paths. A route is advertised only for a server-managed session with a custody record for the artifact; the service rechecks the managed root, symlink-free path, file identity, size, and SHA-256 before opening a response stream. Download the working copy and recovery artifacts before closing the session.

## License

Licensed under the [MIT License](LICENSE).
