# Edit an existing header or footer

These commands edit an existing native area in place; they do not delete/recreate it or rewrite the HWPX package. Send them through `hwpx_command` (one `request`) or `hwpx_bundle` (each item in `steps`). The CLI's JSON command/bundle surface accepts the same fields.

## 1. Read and bind the area

```json
{"op":"header_footer_inventory"}
```

The read-only result lists `areas` in document order. Each has `area_id`, `kind` (`header`/`footer`), zero-based owning `section`, `apply_to` (`both`/`odd`/`even`), `hash`, text, paragraph/run properties, automatic-number `fields` and `shown_in_sections`. `sections` lists each kind's own page scopes and `inherited_from` owner. Text previews stop at 300 characters; the hash binds the full resolved area, not just the preview. Paragraph and occurrence indexes apply inside that area, not inside the body.

Copy `kind`, `section`, `apply_to`, `expected_area_hash` (the inventory's `hash`) and the complete `shown_in_sections` array as `expected_affected_sections`. The latter must be a nonempty ascending unique array of exact integer section numbers (0..10000). It is required when an owner is shared; omission remains supported for an ordinary single-owner area. Every supplied array must exactly match the current full affected set, even for a single owner. Read again after an edit: the old hash is stale. Identical words in the body or another area are not targets.

A section without areas of its own inherits the previous owner's areas. Requesting that inheriting section returns `area_inherited` without editing. A shared owner without `expected_affected_sections` returns `affected_sections_required`; a subset, foreign or stale affected set returns `affected_sections_mismatch` before mutation. Only the exact explicitly acknowledged current set authorizes changing every shown section. The definition hash alone does not bind topology. This increment does not detach inherited definitions or promise a section-only change.

## 2. Replace one exact range

Replace `<inventory hash>` with the value just read:

```json
{"op":"header_footer_text_exact","kind":"header","section":0,"apply_to":"both","expected_area_hash":"<inventory hash>","old_text":"SAMPLE","new_text":"UPDATED","confirm_layout":true}
```

Korean and English are supported. The replacement keeps the replaced range's single uniform character format (`format_from: "replaced_text"`, the default). `format_from: "last_character"` explicitly uses its last character's format for a mixed-format old range. It does not merge arbitrary rich text. A replacement stays within one ordinary paragraph and must not span an automatic page-number control or another object. Newline/tab/control insertion and empty replacement are not supported.

`match` optionally narrows the enclosing text. Repeated occurrences require both the one-based `occurrence` and `expected_occurrences`. Alternatively bind a paragraph with zero-based `paragraph_index` and its exact full `expected_text`. Refusals distinguish not found, ambiguity, count mismatch and stale paragraph text.

The result names the area and before/after hashes, target occurrence, paragraph text before/after, resulting character format, retained fields and native actions. Native `InsertText` plus selected-range `Delete` is two measured native history steps, presented as one logical Undo.

## 3. Apply direct formatting

```json
{"op":"header_footer_format_exact","kind":"header","section":0,"apply_to":"both","expected_area_hash":"<inventory hash>","match":"REPORT","bold":true,"text_color":"#C00000","align":"center","confirm_layout":true}
```

Character properties apply only to the exact matched range: `font_family` (must be available), `size_pt` (0.1 pt increments), `bold`, `italic`, `underline`, `text_color`. Paragraph properties apply to its containing paragraph: `align`, `left_indent_pt`, `right_indent_pt`, `first_line_indent_pt`, and the paired `line_spacing_type`/`line_spacing_value`. A paragraph may instead be bound by `paragraph_index` + `expected_text`. Match and paragraph selectors cannot be combined. This command does not support multi-paragraph ranges, numbered/bulleted header paragraphs, text inside a table/textbox, or arbitrary named-style editing.

The reply includes requested properties, resolved before/after runs and paragraph properties, scope, hashes and native actions. Only properties that differ issue actions. Character-only or paragraph-only changes count one native step; both together count two. Already-present values return `changed:false`, `status:"already_set"` with zero native actions.

## Safety and history

| Case | Behavior |
|---|---|
| Missing area/wrong page scope/section | `area_not_found`, before any edit |
| Inherited-only section | `area_inherited`, names the actual owner |
| Multiple same-kind/scope definitions | `area_ambiguous`, never choose the first |
| Stale content/format hash | `area_hash_mismatch` |
| Repeated range without binding | `target_ambiguous` / `target_count_mismatch` |
| Range crosses page-number or another control | `target_crosses_control` |
| Body command points to header/footer text | `target_in_header_footer`; use the area commands |
| Unreadable/malformed native caret, selection, block, parent-control, HeadCtrl/CtrlID/Next preservation walk or action result | stop as UNKNOWN; retain native owner/session rather than attempt Cancel/restoration/diagnostics |

A known target refusal restores and proves the original context and unchanged document. A successful edit restores the original caret and verifies full-document/body/table/media/style/control preservation plus the unedited areas. Native uncertainty is not a clean refusal or a rollback claim. Existing held-session recovery/save restrictions remain in force.

Use public `undo`/`redo` requests after a qualified edit; the service checks the recorded pre/post state, not just the native return. History is measured on Hancom 2024 13.0.0.3457 / pyhwpx 1.7.2 for the ordinary single-paragraph text and direct-format variants in both/odd/even owning areas and inherited coverage described above. Do not infer untested multi-paragraph, complex-object or arbitrary document support from these examples.

## Verify the saved result

A bundle with more than one actually dirty step and any header/footer mutation has unqualified composed history, even when each step reports a native count. The public bundle result reports `history.qualified=false`, `reason=header_footer_multi_dirty_composition`, and the actual dirty-step count. Undo refuses with `undo_not_counted` before any native history call; Redo has no admitted undone edit (`no_redo_available`) and also makes no native call. The edits, subsequent context reads and save/reopen remain useful. A bundle with one dirty edit plus inventory or no-op companions retains its measured pre/post state comparisons. This is a conservative bound, not a composed-history engine.

After editing, call `hwpx_save`, retain the returned saved-output file, request `hwpx_proof`, then close and reopen that saved file for another inventory/proof. Keep the PDF and **every** page PNG. Proof's existing body-oriented comparison can report partial coverage; the area inventory's before/after properties and an independent read of saved `Contents/section*.xml` plus referenced `Contents/header.xml` definitions are required to verify header/footer text, page scope, properties and automatic-number semantics. XML is inspection-only, not the implementation.

The native qualification examples additionally compare all unedited area definitions, body text/table cells, control references, resolved body/run/paragraph properties, picture-to-BinData mappings, image hashes, numbering/styles and page-number controls. Native duplicate-area creation is not part of the ordinary editor workflow: that defensive guard is fault-injected-test coverage, not a manufactured corrupt native fixture. Qualification on one Hancom version and synthetic fixtures is not a guarantee for every legacy document.
