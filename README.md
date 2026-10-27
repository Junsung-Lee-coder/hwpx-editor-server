# HWPX editor/server

Edit Hancom `.hwpx`/`.hwp` documents programmatically on Windows. A local HTTP API drives the real Hancom editor through [`pyhwpx`](https://pypi.org/project/pyhwpx/), and a thin command-line client (`hwpx`) plans each edit, sends it to the API, and prints structured proof of what changed.

```text
hwpx CLI / MCP client  ──HTTP──▶  API server (127.0.0.1:8765)  ──COM──▶  Hancom editor (visible desktop)
```

What you get:

- **Find and navigate:** text search with page and cursor context, table and control inventories.
- **Targeted edits:** text entry, character styles, bullets, tables, cell formatting, paragraph and control operations. Each edit is validated before it touches the document.
- **Page numbers and official-document numbering:** `hwpx page-number-apply` inserts one native page number at one of six top/bottom positions with a start number and optional native dash side characters (arbitrary prefix/suffix text is refused). Before editing it reads an in-memory structural snapshot of the live document (body, run/paragraph/table/picture formatting, headers/footers, styles, numberings, media references) and refuses with no mutation when that snapshot is unreadable; afterwards only the new native page-number control and its settings may differ, and a changed or unreadable post-state is reported as an unknown outcome (not as inserted) with the document left as is. `hwpx official-numbering-apply` applies native outline numbering levels 1-4 (`1.` → `가.` → `1)` → `가)`) to one proven paragraph, verifying the current section's outline definition by readback and defining it only when no other outline paragraph in that section depends on it. It applies the level with one native ParagraphShape action (HeadingType plus Level) when the verified definition already exists, requires the same structural snapshot to be readable before the call and unchanged outside the target paragraph's numbering afterwards, and refuses a target it cannot identify uniquely. Both are bundle-backed, do not save automatically, and need rendered proof before save. A bounded Windows/Hancom check has confirmed bottom-center page numbering starting at 7 without dash decoration through save and reopen. The saved HWPX contains native page-number and page-restart controls, and the protected-content checks passed. Rendered numbering on every page, other settings, insertion/deletion renumbering, Undo, and native official-document numbering remain unverified.
- **Table structure:** insert or delete rows and columns, merge a cell range, or split a cell on one exact table (`table-structure-exact`). The table grid is read back before and after, and the edit only counts as done when the grid changed exactly as planned.
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

### Native page numbers and official-document numbering

After opening a working copy, use the existing bundle-backed commands:

```text
hwpx page-number-apply --position bottom-center --start-number 3 --side-char dash --confirm-layout
hwpx official-numbering-apply --match "First item" --expected-page 1 --level 1 --confirm-layout
hwpx official-numbering-apply --match "Second item" --expected-page 1 --level 2 --confirm-layout
hwpx official-numbering-apply --match "Third item" --expected-page 1 --level 3 --confirm-layout
hwpx official-numbering-apply --match "Fourth item" --expected-page 1 --level 4 --confirm-layout
```

- Page positions are `top-left`, `top-center`, `top-right`, `bottom-left`, `bottom-center`, `bottom-right`. Start numbers are integers from 1 through 65535. `--side-char dash` (or the pair `--prefix - --suffix -`) requests native `- 1 -` decoration; `none` is the default. Arbitrary prefix/suffix strings are not supported.
- Official levels are native outline metadata, not characters inserted into the paragraph. Use a unique match in the intended body paragraph and its observed page. If match text begins with a hyphen, keep it attached to its option, for example `--match=--항목`.
- Applying identical page settings or the same verified outline level again is a no-op: no native mutation is issued and the step reports `dirty=false`. It does not clear dirtiness from earlier edits. A conflicting existing page number or a paragraph using another list/bullet is refused rather than overwritten.
- These two commands, and `bundle-run`, `bundle` and `tx-commit`, exit 0 only when the backend explicitly reports `ok=true`; a refusal or uncertain outcome exits 1, including with `--json`. Failure output keeps the step error and mutation evidence and does not claim successful execution. An unknown native outcome is not permission to retry, save, close or discard automatically.
- No command here saves automatically. Render all affected pages before accepting the working copy. Native Hancom save/reopen, automatic renumbering and Undo behavior still require separate Windows evidence; portable tests do not establish them. Stateless queued numbering jobs remain unsupported.

### Style and action guards

- `hwpx font` and `hwpx fontsize`: after styling a selected range, no selection may remain (Hancom can collapse it, and the reply's paragraph-context reads replace it). The next `font` or `fontsize` without a selection is then refused instead of styling only the caret. Reselect the range for each property, or pass `--at-caret` to style the caret position on purpose. The reply reports `selection_consumed`.
- `hwpx action NAME`: the picture and OLE controls, `BinItem` references and embedded BinData are read from the whole document before and after the action. If they cannot be read before (including a readback above 16M characters), or reading them moved the caret or selection, the action is not run. If any of them changed, or they cannot be read afterwards, the command fails with the reason, marks the working copy dirty and refuses `hwpx undo`; close the working copy without saving and reopen it. Pass `--allow-media-change` when removing or changing a picture is intended.

## Document tools

Native edits (bundle recipes; each checks the caret from `hwpx where`, runs the Hancom action once, and reads the document back before it reports success):

| Recipe | What it does |
|---|---|
| `object-insert-exact` | Insert a footnote, endnote, memo, hyperlink, 책갈피 (named anchor), equation, line/rectangle/ellipse, header or footer |
| `layout-inspect` | Read the page setup and column definition at the caret (read-only) |
| `layout-exact` | Change paper size, orientation and margins; set columns; insert or remove a section break; apply an automatic hanging indent (내어쓰기) |

Run a recipe with `python -m local_cli_v1.main bundle-run <recipe> ...` and see its options with `bundle-help <recipe>`. Images use the existing `image` command (`--fit-cell` inside a table cell). Resizing or deleting an image uses `section-control-move-resize-exact` / `section-control-delete-exact`.

Local commands (no server call, no Hancom; their output is static evidence, not rendered proof):

| Command | What it does |
|---|---|
| `new FILE.hwpx` | Write a minimal blank document (paper size and margins), then `open` it |
| `mermaid-render SRC.mmd --out OUT.png` | Render a Mermaid diagram with mermaid-cli (`mmdc` on `PATH` or `HWPX_MMDC`), then insert it with `image` |
| `doc-chunks`, `doc-search` | Split a document into chunks and search them |
| `doc-index`, `doc-outline` | Paragraph position index; headings as a table of contents |
| `doc-export --format text\|html`, `doc-stats` | Export as text or HTML; count characters, words and paragraphs |

The native edits above have not yet been run on a Hancom desktop. They are designed to fail closed, so treat a first run on a disposable copy as verification.

## Security

- The API binds to `127.0.0.1` by default and has no CORS.
- Set `HWP_API_TOKEN` (32+ ASCII characters, no whitespace) to require `Authorization: Bearer <token>` on every request. Without a token, an unauthenticated `GET /health` returns only `status` and `api_port`.
- The server refuses to start on a non-loopback `HWP_API_HOST` unless `HWP_API_TOKEN` is set.
- The server itself speaks plain HTTP. A non-loopback bind is only safe behind a TLS-terminating proxy or tunnel, and clients must reach it through `https://`. The CLI refuses to send its token over `http://` to a non-loopback host, and the MCP adapter rejects non-loopback HTTP backends.
- Clients send the token from their own environment: `HWPX_API_TOKEN` for the CLI, `HWPX_MCP_BACKEND_TOKEN` for the MCP adapter. The CLI sends it only to the configured server's origin, never through an `http_proxy`/`https_proxy` from the environment, and refuses to follow a redirect on a request that carries it.
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
