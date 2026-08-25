# HWPX editor/server

Windows-native HWPX editing runtime with a local HTTP API and a thin command-line client. The runtime uses Hancom automation through `pyhwpx`; the CLI builds explicit command bundles, sends them to the server, and formats structured results locally.

This repository is source-only. It does not include working documents, generated files, uploads, rendered output, queues, logs, databases, screenshots, credentials, or machine-specific runtime state.

## Requirements

- Windows with a logged-in desktop session for Hancom automation.
- Python 3 and a Hancom installation that supports the automation calls used by `pyhwpx`.
- The dependencies listed in `requirements.txt`.

The server defaults to loopback address `127.0.0.1` and port `8765`. Set `HWP_API_HOST`, `HWP_API_PORT`, and the other `HWP_*` settings in a local `.env` file when needed. `.env` is ignored by Git; `config.example` is the redacted configuration template.

## Setup

From a Windows command prompt:

```bat
scripts\setup_venv.bat
copy config.example .env
```

The second command is optional. Edit `.env` only on the local machine. Do not commit it.

## Run

The packaged launcher is manual and on-demand:

```bat
scripts\writer_v1_manual.cmd status
scripts\writer_v1_manual.cmd start
python hwpx_cli_v1.py help
scripts\writer_v1_manual.cmd stop
```

For direct development entry points:

```bat
.venv\Scripts\python -m app.api_server
.venv\Scripts\python -m app.worker
```

The CLI resolves its base URL in this order: `--base-url`, the cached URL from `hwpx open`, `HWPX_BASE_URL`, then `http://127.0.0.1:8765`.

A normal edit loop is:

```text
status -> open -> find/where/select -> edit -> render proof -> save -> close
```

Work on managed copies. Review rendered proof before treating a mutation as complete; page count alone is not validation.

## Layout

- `app/` — HTTP routes, runtime state, Hancom worker, editing operations, command packages, and artifact handling.
- `local_cli_v1/` — local planner, command-bundle registry, transport, output parser, and readback helpers.
- `hwpx_cli_v1.py` — public CLI entry point.
- `scripts/` — Windows launcher and dependency-light static smoke checks.
- `tests/` — unit tests for pure helpers and local contracts.
- `fixtures/local_cli/` — synthetic JSON responses used by local tests.

## Testing

See `TESTING.md`. The dependency-light syntax gate is safe to run on any platform:

```bash
python -m compileall -q app local_cli_v1 scripts tests
```

The full runtime and native Hancom checks require Windows and the installed dependencies. Do not use production documents as test fixtures.

## Data boundary

Keep documents and runtime state outside the repository. In particular, do not add `.hwp`, `.hwpx`, `.pdf`, `.docx`, `.pptx`, or `.xlsx` files, or files from `spool/`, `uploads/`, `output/`, `logs/`, or `backups/`. Use synthetic fixtures for local tests.

No license file is included in this repository.
