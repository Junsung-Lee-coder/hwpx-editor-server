# Testing

## Native save and normal close contract

`save` reads exact typed `IsModified` on the healthy owning STA and journals it before proceeding. Exact `False` deliberately skips ordinary Save as a **clean no-op**; the working-copy/file/custody/location readbacks still run. It does not fabricate a Save return or clear prior dirty evidence. Exact `True` calls the unchanged ordinary Save once; native False, errors, UNKNOWN and receipt/readback failures retain custody, with no forced Save or retry. Historical failed Saves remain failures.

Normal `close` is implemented for the original constructor-owned instance with exactly one bound clean document and window. It calls raw void `Quit` once (not `pyhwpx.quit`, which hides a destructive Clear), verifies the original process handle exited, releases the SDK dispatch and observes both owning-STA COM balances. Dirty, unbound, timed-out, UNKNOWN or held state is retained with no teardown. A reporting-only failure after completion remains completed, with managed files retained for cleanup; Quit is never replayed. Download approved artifacts **before** successful close removes the managed root.

Portable tests establish source behavior only. Real Windows SDK/COM/native-exit acceptance of this candidate still requires a separately reviewed verification run on an interactive Windows/Hancom desktop. See [native lifecycle contract](docs/NATIVE_LIFECYCLE.md) for the locked dependency, API sources, destructor balance and limits.


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

The page-restart evidence regressions use two hash-pinned, disposable generated-document snapshots bundled under `tests/fixtures/page_restart`; no private document or external workspace is required. A bounded Windows/Hancom run confirmed start 7, bottom-center, no decoration, save/reopen and the caller's preservation checks. The MCP scenario below later covered page-by-page increments for one dashed bottom-center setting, numbering, renumbering, Undo and Redo; other page positions remain unverified.

The official-numbering snapshot regressions (`tests/test_native13_first_level_inst_id.py`, `tests/test_native13_f1f2_regressions.py`, `tests/test_official_unicode_census_guard.py`) read hash-pinned native HWPML2X snapshots of a generated four-level test document and one verbatim harness function, bundled under `tests/fixtures/native_numbering`; they run in every portable checkout and need no external workspace. Replaying retained snapshots exercises the real parser and comparisons; it is not a new native run.

`tests/test_action_media_guard.py` and `tests/test_style_selection_scope.py` drive the service `action`, `font` and `fontsize` paths against fakes of the reported behavior: a deletion that also removes the adjacent picture and its BinData, and a style edit after which no selection remains, whether Hancom collapsed it or the service's own paragraph-context reads replaced it. `tests/test_integration_held_undo.py` covers plain undo after a numbering bundle that left the session held. They are not Hancom evidence.

The page-number and official-numbering tests (`tests/test_native_page_number.py`, `tests/test_native_official_numbering.py`, `tests/test_native_numbering_surfaces.py`) drive a faithful fake of the documented `HAction`/`HParameterSet` calls from `tests/native_numbering_fakes.py`. `tests/test_native_numbering_census.py` covers the in-memory HWPML2X protection reader on synthetic samples (its element names are not verified against real Hancom output), and `tests/test_native_numbering_repair.py` and `tests/test_native_numbering_bundle_translation.py` cover the four-level outline definition, header/footer protection and the unknown-outcome translation through `LocalCliService.command_bundle`. `tests/test_native_numbering_preservation.py` adds synthetic structure corruptions (character height, paragraph indent, picture and table size, definitions, unreferenced media) that must be refused before a mutation when unprovable and reported as unknown outcomes afterwards, driven through the library, `command_bundle`, `apply_edit_operations` and the worker edit path. They prove call sequencing, validation, refusal with zero mutation, and CLI/MCP/service parity; they are not Hancom execution evidence. `tests/test_native_numbering_shared_outline.py` covers a numbered paragraph whose numbering reference is the definition the section outline uses: the census marks it (`shares_outline_numbering`), the outline redefinition is refused before `OutlineNumber` is issued, and a numbered paragraph with a separate definition is unaffected; its HWPML2X samples are synthetic like the census samples. `tests/test_native_numbering_outline_scope.py` covers the conservative scope of the same refusal while the section outline definition would change: before `OutlineNumber` is issued, a numbered paragraph whose numbering reference cannot be read (`shares_outline_numbering` is unknown), an outline paragraph in another section, and a second section that references the same outline definition are each refused as unprovable, because the census has no field that binds another section's paragraphs to their own definition; this is a refusal, not multi-section support. Paths that do not change the definition (an already-official definition, or an already-applied target) and page numbers are unaffected, and a numbered paragraph with a known separate reference is still allowed. These cases run on synthetic fakes only. `tests/test_native_numbering_lifetime_safety.py` covers two public-caller safety rules on the same synthetic fakes: the queued edit-job runtime (`app.worker.edit_and_convert_with_pyhwpx`, the `/edit-and-convert` and `/edit-and-convert-from-path` submissions and `/capabilities`) refuses any job containing `page_number` or `official_numbering`: `edit_and_convert_with_pyhwpx` raises before its own COM initialization, SDK import/constructor, open, any edit and any quit/CoUninitialize (the surrounding `process_one_command` still runs its native export preflight first, which constructs and quits an empty probe instance, so the whole child process does not perform zero SDK work), because that worker closes the SDK and uninitializes COM in its own `finally` and so cannot keep the document open after an uncertain numbering outcome (the supported-op control job is unchanged); and neither the page-number library nor the bundle `official_numbering_apply` step moves the caret after an uncertain outcome, while success and clean pre-mutation refusals still restore it. `tests/test_native_numbering_uncertainty_propagation.py` covers the official-numbering uncertainty boundary on the same fakes: once `OutlineNumber` or `ParagraphShape` has been issued, an ordinary failure in a later readback, census or preservation comparison is reported as an unknown outcome with the original cause (through the library, the bundle step and the real `command_bundle`, which then records the step dirty), while `KeyboardInterrupt`, `SystemExit` and another `BaseException` propagate unchanged (only exact builtin control-flow classes are legacy-tagged; untrusted subclasses are not inspected or tagged), with no caret move, retry, Undo, Close, Clear, Quit or discard from this code. `page_number` has the same boundary: after `PageNumPos` is issued, an ordinary failure (including a raw error in the post-call comparison) is an unknown outcome with its cause, and a `BaseException` propagates unchanged; only exact builtin control-flow classes are legacy-tagged. Owning runtime intent independently retains UNKNOWN and STA for all subclasses. `tests/test_native_numbering_runtime_custody.py` drives the real `LocalCliRuntimeManager`, the live session's owning thread, the real `command_bundle` and the real numbering callbacks over fake SDK/COM objects only. At the protected owned command boundary, a numbering step that reached a native mutator and ended unknown (a returned unknown outcome or a control-flow signal) holds the session: the command is recorded as `failed` with `may_have_mutated=true` and the original signal in its cause chain, the caller receives a noncomplete result (`ok=false`, `dirty=true`, `native_custody_held=true`), and the owning thread and registry binding stay in place with no Clear, Quit, CoUninitialize, navigation, Undo, save, retry or new native callback. Later execute, close (including forced), reap, and reconcile/acknowledge calls without preserved evidence do not release the hold; journal, status-write and pruning errors are reported on the held command and do not release it. Release requires an operator action that this repository does not implement, so a held session lasts until its process ends. The caller-owned library keeps propagating the original signal. `tests/test_native_numbering_retention_races.py` covers two held-session races on the same fakes: a close admitted while a numbering callback was still in flight is refused as held when the owning thread dequeues it (it is recorded `failed`, never `succeeded`, and the owning thread keeps servicing its queue), and a `BaseException` raised by the journal or pruning reporter after the hold is published is recorded on the held command, resolves the waiting caller without a timeout and does not end the owning thread. These are source tests against fakes, not Hancom evidence. N1/N2 job-API parity is therefore not provided; the caller-owned library and the protected interactive CLI/MCP `command_bundle` paths are the supported surfaces.

The user-workflow regressions exercise the real CLI parser, named-bundle builder, service/package dispatch, native numbering mutators and protection reader over the same synthetic SDK/HWPML2X fixtures:

```bash
python -m unittest -v tests.test_numbering_user_workflow tests.test_numbering_bundle_cli_status
```

They cover all six page positions, start-number/dash parameter forwarding, four sequential official levels using one native outline definition, preservation of unrelated paragraphs/formatting/tables/pictures/headers/footers, hyphen-leading match text, no-op dirty deltas without clearing prior dirtiness, and failure exit codes/evidence through direct commands and `bundle-run`/`bundle`/`tx-commit`. Only transport, session ownership/persistence, page evidence and wrapper location snapshots are fixtures; generic JSON/transaction loader boundaries are also fixtures in the exit-status tests. The tests do not launch Hancom or prove save/reopen, rendered output, automatic renumbering or Undo. `tests/test_native_numbering_surfaces.py` keeps an explicit `ok=true` in its successful backend stub to match the actual response contract.

`tests/test_repair_runtime_safety.py` drives review, export, pre-edit refusal, Undo/Redo boundaries and close refusals through a real manager and owning thread over a fake SDK. `tests/test_repair_paragraph_targets.py` replays HWPML2X captured from Hancom (`tests/fixtures/native_repairs`) for level changes, single-use shapes and nested paragraphs, and covers exact removal intent and control preservation. `tests/test_repair_review_rules.py` runs the review assembly over truncated, missing and hostile inputs. `mcp_tests/test_repair_saved_output_and_review.py` covers the close guard, verified publication of the saved file and review coverage over sockets with controlled download faults. None of these is a native run.

`tests/test_native_observed_contracts.py` replays return shapes that stopped real numbering requests on Hancom. `tests/test_native_census_observed.py` runs the real census over HWPML2X captured from Hancom for a disposable synthetic document (`tests/fixtures/native_paragraph_edit`): a page number that renumbers font and character-shape references is not a document change, while a changed existing definition still is. `tests/test_native_paragraph_edit.py` drives paragraph insert and remove through the real service over a fake SDK built on the observed behavior, including refusals with no native edit. `tests/test_mcp_review_rules.py` covers the readable review judgement. These replay or fake native behavior; they are not native runs.

`tests/test_native_table.py` and `tests/test_table_repairs.py` drive `table_inventory`, `table_read`, `table_edit_exact`, `table_style_exact`, `table_width_exact` and `table_create_exact` through the real bundle validation, package dispatch, service, census and a real manager for Undo and Redo, over the fake in `tests/native_table_fakes.py`, which is built on measured behavior: how a block grows, which neighbor sides a border writes, the `BorderCorlorLeft` item, what a sparse CellFill does, how merges and splits place content and lines, and how lines at the table's edge move the frame. `tests/test_table_review.py` and `tests/test_table_review_repairs.py` run the review's table comparison and the independent reader of saved tables over readbacks and over three HWPX files Hancom saved from synthetic documents (`tests/fixtures/native_tables`). `mcp_tests/test_table_mcp.py` covers the schema, forwarding, refusal fields and review output at the real MCP boundary over a fake backend. None of these is a native run.

`tests/test_native_format.py` drives `character_format_exact` and `paragraph_format_exact` through the real bundle validation, package dispatch, service and census, and through a real manager and owning thread for held outcomes and Undo/Redo, over the fake in `tests/native_format_fakes.py`. That fake is built on measured behavior: position widths of tabs and controls, sparse action sets, caret paragraph edits merging in the Undo history, and block edits not doing so. `tests/test_native_format_repairs.py` covers number marks, the top underline, direct-caller input, the section-control range and the full-shape facts the same way. `tests/test_format_review.py` runs the review's format comparison, number-mark coverage, readable rows and the independent HWPX reader on synthetic packages. `mcp_tests/test_format_mcp.py` covers the schema, forwarding, refusal fields and review output at the real MCP boundary over a fake backend. None of these is a native run.

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

`mcp_tests/test_review_and_saved_output.py` runs the review, saved-output copy, reopen, undo/redo and paragraph-edit contract over a socket with a fake REST backend and real local Poppler (skipped when Poppler is not on `PATH`).

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

The MCP-facing numbering workflow runs against a dedicated backend and worker on an interactive Windows/Hancom desktop with disposable synthetic documents: open, review, page number, four numbering levels, level changes, same-level continuation, insert (including after a paragraph holding a table), remove, single-use style paths, refused targets, empty undo and redo, undo and redo for each edit kind, save, review, close, reopen of the saved file, review, close, plus an oversized document whose review is unverified and which still closes. A pass means the review's printed labels, page numbers, comparison and preservation fields hold on the exact commit, not merely that each tool returned ok.

The formatting workflow runs the same way on disposable synthetic documents: each character property alone and together, a substring, a range across mixed runs, tabs, spaces and special spaces, text beside a picture and a table, a table cell, a range across paragraphs, each paragraph property, a blank paragraph, a paragraph holding only a table, numbered paragraphs, a style followed by review, context, find, where and readback and then another style on the same text, one Undo and Redo per command back to the opened state, save, close, reopen and review. Number marks run as pairs of ranges ending before and at the paragraph end, for automatic numbers at two levels, outline numbers at two levels, bullets and a definition naming its own mark shape, each property on its own, a table cell, `number_mark: "follow"`, Undo and Redo. The accepted endpoints of size, indents and line spacing are applied through MCP without rendering them. Controls beside a range run on their own documents: `page_number_apply` and then the whole title, its prefix and a title-and-body paragraph range, with Undo, Redo, save and reopen; controls in a shape other than the title's; a place marker (책갈피), page hiding, a hidden comment and an index mark before the first text; a picture or table right before it; ranges starting right after or ending right before place markers, page controls, hidden comments, index marks, section headers and footers, pictures, tables, a text box, equations, footnotes, endnotes and automatic numbers; a final place marker on a numbered paragraph; official numbering followed by review, save and reopen, with all-new, compared and mixed marks. Page numbers run from an omitted start and an explicit 1, at bottom-center with dashes and top-center without, against a start of 3, a first-paragraph restart of 3 and of 1, a section starting at 5, and restarts in a later paragraph on the same and on the next page, each followed where accepted by title formatting, Undo, Redo, save, close, reopen and the printed number. A pass means the saved HWPX and the PDF, read independently of this code, hold the requested values and nothing else changed.

The table workflow runs through MCP on disposable synthetic documents with similar tables, a blank table, repeated rows, merged cells, unequal column widths, a nested table, Korean, Latin, tabs, double spaces, mixed runs, several paragraphs in a cell, a picture in a cell and a body picture, headers, footers and numbered paragraphs. It also runs a full-width table widened past the page, disclosed and fitted back, exact widths on plain and merged tables, landscape paper, none, thin, dashed and thick lines beside a wider merged neighbour, whole-table inner and all lines on plain and merged tables, a range holding a merge, a nested table's lines, all and inner on one cell, one row and one column, tables and text in headers and footers, pattern and gradation fills beside an edit, a package with an unused image, a picture in a nested table that is shared and one that is the last, long cells deleted by hash, creation text with line breaks and blanks, cell alignment and margins named in the review, and a document whose control walk cannot be proved. It covers inserts and deletes in every direction at the first, middle and last line, several lines in one action, merges with text, blanks, pictures and already merged cells, a merge reducing the grid, splits, unmerges of equal and unequal widths, every line type and width, independent edges, range outlines and inner lines, shared edges with plain and merged neighbors, removing lines, fills on a cell, a range and the table, white against none, border and fill together, tables with a frame and with marked lines around structure edits, Undo and Redo of each, reads and reviews between edits, a stale hash, a wrong page and grid, refusals with earlier unsaved work, a saved copy that cannot be written, new tables, save, close, reopen and review. A pass means the live values, the saved HWPX read independently of this code and the PDF agree, and nothing else changed.

Measured on that Hancom for tables and relied on by the code: the official table prints `BorderCorlorLeft`, and `BorderColorLeft` is ignored; a sparse CellFill set returns True and changes nothing; a line of type none keeps its width and color; the HWPX file calls the line that prints as dashes DOT and the one that prints as dots DASH; a neighbor side is written only when its whole side lies on the edge, and with NoNeighborCell a top edge alone did not print, so that option is not offered; TableInsertUpperRow and its siblings ignore Count; TableDeleteRow does nothing on a block; pyhwpx `goto_addr` keeps an address cache across edits, so it is not used; an Undo of an insert through a merged cell leaves that cell's stored height different though the grid, text, hash and print return; Redo of a new empty table gives it another stacking order; beside a wider merged cell both cells' lines print one over the other, so none leaves the wider line showing, a dashed line shows it in its gaps, a solid line of the same width prints half and half, and a thicker solid line hides it; TablePropertyDialog with ShapeType 3 and ShapeCellSize 1 sets the width of every cell of a block, a merged one included, and without those items returns True and changes nothing; native accepts any cell width; `SIZE` follows the cells; landscape paper is as wide as its stored Height; past the page's edge a table is clipped in print; an image no picture uses is dropped at open; a deleted picture's data goes with its last reference; field text takes CR LF as a paragraph break and silently drops a lone LF.

Table cases on the fake only, not in a native run: floating tables, several columns, a gutter, cell spacing and relative widths reported as unverified, an image fill, a saved package whose kept picture lost its data, an action returning False, a raising native call, a failed second action, a change outside the request and a wrong block, which are deliberate faults a healthy Hancom does not produce on demand. Tables spanning pages, captions, `.hwp` input and real-world documents have not been run.

Refusal codes that run has exercised: `target_ambiguous`, `target_not_found`, `target_crosses_control`, `target_in_header_footer`, `target_unsupported_context` (for a footnote), `target_wrong_page`, `target_text_mismatch`, `target_empty`, `target_range_unsupported` (`through_match` before `match`), `existing_page_number`, `existing_page_restart`, `existing_section_page_start`, `target_has_controls`, `target_has_invisible_controls`, `target_crosses_invisible_control`, `target_beside_unproven_control`, `control_enumeration_unavailable` (for a document holding a hyperlink), `target_changes_number_mark`, including a range ending exactly at its last numbered paragraph's end, and `font_not_available`.

Measured on that Hancom and recorded in a native run, because the review or a later command depends on them: Hancom gives a field's end an empty control identifier; `page_number_apply` with `start_number: 1` writes no page restart, so a section starting elsewhere goes on printing its own start; a page restart on the first page is rewritten to the request and Undo does not put it back; rendering merges two same-shape runs either side of a joining control, so an Undo of a later command reaching the state from before that merge reports `undo_state_mismatch` rather than a false match.

Covered by source tests over the fake only, not by a native run: text box and caption refusals, `target_unselectable` (a blank last paragraph), `target_unlocatable` (a blank cell paragraph), `target_range_unsupported` across a cell or over two paragraphs of one cell, `selection_unverified`, `font_availability_unknown`, `preservation_unprovable`, a built-in HFT font, holding the session after an unknown outcome, including a failed second action at the paragraph end or a control whose data or values differ after the action, `redo_state_mismatch`, a number definition that cannot be read in a review (`not_compared` beside compared marks), and a paragraph whose object facts are unavailable (`(글 없음, 내용 확인 못함)`).

Not run at all for the formatting commands: page 1 at the other four positions, comparison with the Hancom GUI, multi-section documents, merged cells, nested tables, cells holding several paragraphs, `.hwp` input and real-world documents.

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
