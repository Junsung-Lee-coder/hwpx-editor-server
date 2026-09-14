# Local CLI

`local_cli_v1` is the local planner and formatter for the HWPX runtime. It turns a command into an explicit bundle, sends the bundle to the local API when execution is requested, and keeps the response formatting on the client side.

## Standard safe edit+proof workflow

```text
status -> open -> find/where/select -> edit -> render proof -> save -> close
```

Use managed or disposable copies. Capture rendered proof before saving a mutation. Page count is not proof of layout correctness.

## Command status

`command-status` reports each command as `bundle-backed`, `direct-backlog`, or `disabled`.

The machine-readable status classes are: bundle-backed / direct-backlog / disabled.

- `where`: migrated to the bundle-backed public command path.
- `context` and `selected-text-proof`: bundle-backed read-only proofs.
- Commands marked `not migrated yet` remain on the migration backlog and should not gain a hidden server-side command lane.

## Uniform command pipeline and migration backlog

The uniform command pipeline is:

1. Parse intent locally.
2. Build an explicit command bundle.
3. Execute the bundle through the local API when requested.
4. Parse and format the structured result locally.

Bundle output parsing and user-facing formatting now live locally. Commands that are not migrated yet keep their existing behavior until a matching primitive and rendered proof are available. The CLI must not treat a page count or a text-only response as final layout proof.

## Useful commands

```text
python -m local_cli_v1.main status
python -m local_cli_v1.main open <file>
python -m local_cli_v1.main command-reconcile --command-id <id> [--session-id <id>]
python -m local_cli_v1.main find <text>
python -m local_cli_v1.main where [--json]
python -m local_cli_v1.main context [--json]
python -m local_cli_v1.main selected-text-proof [--json]
python -m local_cli_v1.main bundle-list
python -m local_cli_v1.main bundle-dump <bundle-name> [args...]
python -m local_cli_v1.main tx-preview <out.json> --recipe <name> [args...]
python -m local_cli_v1.main tx-commit <plan.json>
python -m local_cli_v1.main export-proof-range --pages <range> --out-dir <dir>
python -m local_cli_v1.main native-border-readback --pre-quit <pre.json> --persisted <reopened.json> --target-identity <target.json> --out <readback.json>
python -m local_cli_v1.main proof-packet --out-dir <packet-dir>
python -m local_cli_v1.main save
python -m local_cli_v1.main close
```

`command-reconcile` is the only retry/close handoff for a timed-out native
command. It reads the durable server journal, waits while the native call is
still pending, and commits the late result before normal commands are admitted.

`native-border-readback` is a local evidence-only command. It reads the native
value maps captured before save/quit and after reopening, binds them to the
installed module-root marker plus verified `source-manifest.json` and target
identity, atomically writes the `local-cli/native-border-readback/v1` artifact,
and records its path in local CLI state. Run `proof-packet` afterward to copy
and hash the bound artifact; the command never mutates the document or calls
the API.

The API base URL is selected by `--base-url`, the cached URL from `hwpx open`, `HWPX_BASE_URL`, then the local loopback default. Keep local configuration outside Git.
