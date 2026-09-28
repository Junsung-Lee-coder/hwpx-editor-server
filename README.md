# HWPX editor/server

Edit Hancom `.hwpx`/`.hwp` documents programmatically on Windows. A local HTTP API drives the real Hancom editor through [`pyhwpx`](https://pypi.org/project/pyhwpx/), and a thin command-line client (`hwpx`) plans each edit, sends it to the API, and prints structured proof of what changed.

```text
hwpx CLI / MCP client  ──HTTP──▶  API server (127.0.0.1:8765)  ──COM──▶  Hancom editor (visible desktop)
```

What you get:

- **Find and navigate:** text search with page and cursor context, table and control inventories.
- **Targeted edits:** text entry, character styles, bullets, tables, cell formatting, paragraph and control operations. Each edit is validated before it touches the document.
- **Rendered proof:** page screenshots and PDF renders, so you check the edit visually. Page count alone is never treated as proof.
- **Honest failure reporting:** when Hancom's outcome is unknown, the server says so and keeps the evidence. It does not guess or silently retry.

This repository is source-only. It contains no working documents, generated output, logs, credentials, or machine-specific state.

## Quick start (Windows)

Requirements: Windows with a logged-in desktop session, Hancom Office, Python 3.13, and Poppler `pdftoppm` (on `PATH` or set with `HWP_PDFTOPPM`).

1. **Install.** From a command prompt in the checkout:

   ```bat
   scripts\setup_venv.bat
   ```

   This installs a portable runtime into `.hwpx-install` (Git-ignored) using the hash-pinned `requirements-windows.lock`. See [docs/WINDOWS_INSTALL.md](docs/WINDOWS_INSTALL.md) for custom install roots, ports, and troubleshooting.

2. **Start the server.**

   ```bat
   scripts\writer_v1_manual.cmd start
   scripts\writer_v1_manual.cmd status
   ```

3. **Run an edit session.** Work on a copy, never on the only copy of a document:

   ```bat
   python -m local_cli_v1.main status
   python -m local_cli_v1.main open C:\work\copy-of-report.hwpx
   python -m local_cli_v1.main find "Quarterly results" --with-page
   python -m local_cli_v1.main where
   python -m local_cli_v1.main page-screenshot --page 1 --out-dir C:\work\proof
   python -m local_cli_v1.main save --out C:\work\report-edited.hwpx
   python -m local_cli_v1.main close
   ```

   `python -m local_cli_v1.main help` prints the full workflow and `bundle-list` shows the available edit operations.

4. **Stop the server** when you are done: `scripts\writer_v1_manual.cmd stop`.

## How an edit works

```text
status -> open -> find/where/select -> edit -> render proof -> save -> close
```

The CLI turns each command into an explicit command bundle, the server runs it against Hancom under a runtime lock, and the CLI formats the structured result locally. Review the rendered proof before treating an edit as done.

The CLI picks its server in this order: `--base-url`, the URL cached by `open`, `HWPX_BASE_URL`, then `http://127.0.0.1:8765`.

## Security

- The API binds to `127.0.0.1` by default and has no CORS.
- Set `HWP_API_TOKEN` (32+ ASCII characters, no whitespace) to require `Authorization: Bearer <token>` on every request. Without a token, an unauthenticated `GET /health` returns only `status` and `api_port`.
- The server refuses to start on a non-loopback `HWP_API_HOST` unless `HWP_API_TOKEN` is set.
- The server itself speaks plain HTTP. A non-loopback bind is only safe behind a TLS-terminating proxy or tunnel, and clients must reach it through `https://`. The CLI refuses to send its token over `http://` to a non-loopback host, and the MCP adapter rejects non-loopback HTTP backends.
- Clients send the token from their own environment: `HWPX_API_TOKEN` for the CLI, `HWPX_MCP_BACKEND_TOKEN` for the MCP adapter. The CLI sends it only to the configured server's origin and refuses to follow a redirect on a request that carries it.
- Settings validation errors never echo input values, so an invalid token does not appear in startup errors or logs.
- The browser observation viewer and the independent Windows verifier do not send a token. Use them with the default token-less loopback setup.

Configuration lives in a local `.env` (Git-ignored). `config.example` is the redacted template.

## Hancom automation limits

- **Visible desktop only.** Hancom must run in a visible window on a logged-in, unlocked Windows desktop. Minimized windows, background-only operation, and SSH Session 0 are not supported, and no response certifies them.
- **No exclusive ownership.** The runtime creates the Hancom object with `new=True`, but that does not guarantee exclusive ownership of a new native process. A constructor error may arrive after another Hancom object was contacted, so the helper makes one attempt and reports the original failure.
- **Unknown outcomes stay unknown.** A pending reconciliation replies with HTTP 200 and `ok=false`, `reconciled=false`, `reconciliation="pending"`. That means the native result is unknown, not that the edit succeeded. Keep the session ID, command ID, and original document. Observation is bounded to one recovery request with a caller wait of at most 125 seconds. After that the binding, logs, and artifacts are preserved for an operator decision.

Recovery steps are in [docs/WINDOWS_ROLLBACK.md](docs/WINDOWS_ROLLBACK.md).

## Repository layout

| Path | Contents |
|---|---|
| `app/` | HTTP API, runtime state, Hancom worker, editing operations, command packages, artifact custody |
| `app/local_cli_service.py` | Local CLI session service: sessions, find/select, text edits, save/export |
| `app/local_cli_bundle_controls.py`, `app/local_cli_bundle_paragraphs.py`, `app/local_cli_cell_margins.py` | Command-bundle operations, split out of the service as mixins |
| `app/api_auth.py` | Optional bearer-token middleware |
| `local_cli_v1/` | CLI planner, command-bundle registry, transport, output parser, readback helpers |
| `hwpx_mcp/` | Optional MCP adapter over the HTTP API ([docs/MCP_SETUP.md](docs/MCP_SETUP.md)) |
| `scripts/` | Windows installer, verifier, launchers, and dependency-light static smoke checks |
| `tests/`, `mcp_tests/`, `fixtures/` | Unit and contract tests with synthetic fixtures |

## Development

Checks that run on any OS:

```bash
python -m pip install --require-hashes -r requirements-portable.lock
python -m compileall -q app local_cli_v1 scripts tests
python -m unittest discover -s tests -p 'test_*.py'
```

[TESTING.md](TESTING.md) lists the static smoke checks and the Windows-only suites. Linux checks cannot prove native Windows/Hancom behavior. Native verification is done by `scripts\verify_windows.ps1` on an interactive desktop ([docs/WINDOWS_VERIFY.md](docs/WINDOWS_VERIFY.md)). Never use production documents as test fixtures.

## Runtime state and the checkout

Git-ignored runtime state is kept inside the checkout by the default portable install, under `.hwpx-install`. It may include `.venv`, `.env`, `spool`, receipts, queues, logs, backups, and rendered proof. Git ignore rules keep these paths out of commits, but they don't make the paths absent or read-only. Pass an external `-InstallRoot` when the checkout must stay free of runtime state.

## Further reading

- [docs/WINDOWS_INSTALL.md](docs/WINDOWS_INSTALL.md): installation, side-by-side installs, field notes
- [docs/WINDOWS_VERIFY.md](docs/WINDOWS_VERIFY.md): independent read-only verification
- [docs/WINDOWS_ROLLBACK.md](docs/WINDOWS_ROLLBACK.md): rollback and timed-out command recovery
- [docs/MCP_SETUP.md](docs/MCP_SETUP.md): MCP adapter setup (Korean)
- [local_cli_v1/README.md](local_cli_v1/README.md): CLI command status and pipeline
