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
hwpx status
hwpx open <file>
hwpx find <text>
hwpx where [--json]
hwpx context [--json]
hwpx selected-text-proof [--json]
hwpx bundle-list
hwpx bundle-dump <bundle-name> [args...]
hwpx tx-preview <out.json> --recipe <name> [args...]
hwpx tx-commit <plan.json>
hwpx export-proof-range --pages <range> --out-dir <dir>
hwpx save
hwpx close
```

The API base URL is selected by `--base-url`, the cached URL from `hwpx open`, `HWPX_BASE_URL`, then the local loopback default. Keep local configuration outside Git.
