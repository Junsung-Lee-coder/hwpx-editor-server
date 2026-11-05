# Existing pictures

## Availability and qualification

The backend command packages, CLI bundle serializer and MCP `hwpx_command` / `hwpx_bundle` expose `picture_inventory`, `picture_replace_exact`, `picture_crop_exact` and `picture_layout_exact`.

This implementation is not a native-qualified release. Portable tests use an explicitly synthetic SDK and exercise the real backend command service. MCP transport tests use the official MCP SDK with a fake HTTP backend. Neither proves that a Hancom edit survives save/reopen, renders correctly, or has qualified Undo/Redo behavior.

## Discover and bind

Start with `{"op":"picture_inventory"}`. Its picture records include an exact `target_id`, fresh `hash`, frame, placement, crop, image/media references and structural context. `edit_contract` distinguishes implemented input bounds from native qualification. Structural eligibility is not a promise of qualified editing.

Every edit requires `target_id`, `expected_hash` (the freshly returned `hash`) and `confirm_layout: true`. The picture namespace is HWPML `SHAPEOBJECT InstId`, not `SHAPECOMPONENT InstID`. Before selection the backend enumerates current native controls, obtains `GetCtrlInstID()` and binds the unique matching native control. It does not try alternative XML identifiers.

## Public inventory contract

The public response uses a typed picture projection, separate from journal
redaction. `TextWrap` and `FlowWithText` retain their actual bounded layout
values. Arbitrary attributes, private paths and credentials are not copied into
the public result. Caption text is omitted; presence and a digest describe its
association.

An inventory returns all records up to 1,000 pictures, with `total_count`,
`returned_count` and `complete`. Larger documents return the clean
`picture_inventory_limit` refusal. They never return a truncated list with a
string placeholder or `complete: true`. Structural eligibility and the separate
`edit_contract` context restriction still apply to every returned binding.

## Implemented input bounds

- Replacement: `picture_replace_exact`, plus `source_path`, an absolute regular PNG path on the backend host. The file must decode within size limits and have the existing image's decoded aspect ratio. The backend stages verified bytes below the owned live session before passing the image to Hancom. The path is not a local path on a remote MCP client's machine. JPEG/BMP, resizing and aspect-ratio changes are not implemented variants.
- Crop: `picture_crop_exact`, plus finite normalized source bounds `left`, `top`, `right`, `bottom` in 0..1, with positive visible extent. The engine requests native crop metadata while retaining frame dimensions and embedded payload. Rendered geometry and absence of distortion still require real native qualification; portable XML assertions do not establish either.
- Layout: `picture_layout_exact`, `mode: "inline"` or `"square"`, with optional finite `horizontal_offset_mm` / `vertical_offset_mm` for floating placement. Offsets are not dimensions. Inline mode cannot request nonzero floating offsets. Inline requests `TreatAsChar` only, so the picture's `TextWrap` stays unchanged; square requests `TreatAsChar` off and `TextWrap` Square. Other schema-listed layout modes are refused as unqualified before native editing.

The implemented edit context is an ordinary body picture without a native caption. Table-contained, grouped, caption-bearing, header/footer and other nested picture edits are not qualified and are refused rather than silently rebuilding a picture or its caption. The inventory retains those contexts for discovery. No claim of caption preservation follows from editing a captionless fixture.

Ungrouped admission requires the native `SHAPECOMPONENT.GroupLevel` attribute to be exactly `"0"`. A missing, malformed, noncanonical, nonzero or aliased field is not an implicit zero. A misplaced `SHAPEOBJECT.GroupLevel` cannot override the component value; conflicting shape/component fields are refused before anchor movement, selection or parameter mutation.

Example requests, after replacing the binding with a new inventory result:

```json
{"op":"picture_replace_exact","target_id":"picture:12345","expected_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaa","confirm_layout":true,"source_path":"C:/owned/replacement.png"}
{"op":"picture_crop_exact","target_id":"picture:12345","expected_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaa","confirm_layout":true,"left":0.25,"top":0.25,"right":0.75,"bottom":0.75}
{"op":"picture_layout_exact","target_id":"picture:12345","expected_hash":"sha256:aaaaaaaaaaaaaaaaaaaaaaaa","confirm_layout":true,"mode":"square","horizontal_offset_mm":15.0,"vertical_offset_mm":0.0}
```

For `hwpx_command`, place one request under `request` beside `session_id`. For `hwpx_bundle`, use `request: {"steps": [...]}`. Omitted labels receive nonempty MCP defaults. Request a new inventory after a changed edit rather than reusing a stale hash.

## Preservation, refusal and history

The backend checks the complete target picture against a request-derived allowed delta, including nested rotation/rendering transforms and image alpha, brightness, contrast and effects. Other target properties remain unchanged. It also protects non-target pictures, body/style definitions and complete HEAD/TAIL media state. Replacement may reuse an unchanged embedded asset or append exactly one target-bound PNG member; original/shared members are not rewritten. Unexpected identity renewal or crop-transform representation is not normalized away. A failed mandatory comparison is not success. Technical uncertainty latches the owner before any optional diagnostic or cleanup SDK call. A clean pre-action target/input refusal and an entered SDK failure have different outcomes.

No-op crop/layout requests issue no native selection, private copy or edit action and preserve earlier logical-history metadata. Changed picture operations are explicitly unqualified for native history, including a single dirty step. Their logical Undo/Redo refuses before native history dispatch; one `Execute` is not evidence of one Undo transaction. No aggregate multi-dirty picture history is claimed. Inventory/no-op companions do not independently erase a previously tracked history boundary. The one exception is a kind declared by a configured saved-HWPX profile (below).

## Preservation authority: legacy HWPML or native HWPX package

The authority is selected once per request, before any native call, and is
reported as `preservation_strategy`:

- `legacy_hwpml_strict` (default, no profile configured): the comparison above,
  including complete HEAD/TAIL media state. A failed comparison holds the
  session; it never falls back to another strategy, Save or export.
- `native_hwpx_package`: selected only when `HWP_PICTURE_HWPX_PROFILE` names an
  absolute profile file and `HWP_PICTURE_HWPX_PROFILE_SHA256` is its exact
  SHA-256 (`.env` or environment). A configured profile that is unreadable,
  mismatched or malformed refuses the request with no native call.

Under `native_hwpx_package` the backend writes two owned private copies with
`SaveAs(path, "HWPX", "lock:false;export")` on the owning STA, immediately before
and after the action, in one command epoch below the live session root. The
before copy is the current in-memory document, including earlier unsaved edits.
Every member, including unknown, script, empty and directory members, and its
ZIP container fields are read completely. The expected after-state is the before
package plus the request-derived target delta (`hp:pos` placement and offsets,
`hp:pic@textWrap` for square, `hp:imgClip`, or one `hc:img` binding), the predeclared caret
at the target anchor and the same observed effective `textFlow`. Replacement
may reuse one identical existing asset or add exactly one embedded PNG member
and manifest item; the original and shared members stay byte-identical. Every
other member must be byte-identical with identical container fields and order.
Edited XML members are compared as complete XML information sets with their XML
declaration and namespace declarations unchanged, and with every byte before the
root start tag and after the root end tag (byte order mark, comments, processing
instructions, whitespace) unchanged. A DOCTYPE or entity declaration in any member
parsed as XML, including `.rdf` members, refuses the comparison.

The HWPML `SHAPEOBJECT InstId` and the HWPX `hp:pic` identifiers are different
namespaces and are never compared. The target is bridged by the complete
document-order picture census: every picture must agree on frame, placement,
crop, source dimensions and embedded-payload digest. A census or caret that does
not bridge exactly is refused as `picture_saved_bridge_unproven` before selection.

Representation differences are admitted only from profile data: ZIP container
fields that a fresh serialization renews, the fixed preview members
`Preview/PrvImage.png` and `Preview/PrvText.txt` regenerated by a changed
picture edit, typed volatile metadata values and the serialized caret mapping.
Nothing else is normalized. A volatile value must be an exact UTC
second (`YYYY-MM-DDTHH:MM:SSZ`) inside the measured window of the native call
that wrote that package, and must not move backwards. A regenerated preview
must exist and decode in both copies (a text preview strictly in its declared
encoding; a PNG preview keeps its pixel size when declared);
without a changed picture edit, for example in a saved-file comparison, the
preview must be byte-identical. Under this strategy HWPML HEAD/TAIL media are not the authority;
the result reports `legacy_hwpml_bindata_size: unknown_unqualified_not_compared`
and `head_tail_media: false`. Legacy HWPML BINDATA `Size` is not certified.

### Request-bound line reflow

Hancom stores each paragraph's computed line layout in `hp:linesegarray`
(HWP 5.0 revision 1.3, 4.3.4, table 62: the cache used to print each line). A
changed picture layout recomputes the line boxes of the picture's own paragraph.
Only a profile that declares `line_reflow` for `picture_layout_exact` admits
that, under contract `request_bound_line_reflow/v1`:

- Before any selection, the backend binds the scope from the before copy and the
  request: the target picture's top-level body paragraph (by its child index in
  the section root, not its `id`), its one `hp:linesegarray` and the section page
  geometry. Each line must carry exactly the nine OWPML attributes as exact
  integers within their types, with non-negative line-box metrics, the baseline
  and text height within the line height, the line within the section text width
  and no taller than the paper. Only a single-column section with `landscape`
  `WIDELY` and a zero gutter is covered. Anything else refuses with
  `picture_saved_proof_unqualified`.
- After Execute, only the declared fields among `baseline`, `horzpos`,
  `horzsize`, `textheight` and `vertsize` of those lines may differ, and only to
  values that pass the same checks. `textpos`, `vertpos`, `spacing`, `flags`,
  the line count and the line order stay as before. Every other node, attribute
  and member, including the lines of every other paragraph, is compared exactly.
  A reflow that also moves other lines or paragraphs is outside v1 and is
  refused by the comparison.
- The step result reports `saved_proof.derived_line_reflow` (paragraph, line
  count, changed lines and fields) apart from the unchanged formatting, and
  `evidence_used` includes `line_reflow` when a line changed.
- The allowance applies to that edit's comparison only. Saved-file, fresh-copy
  and history comparisons bind the exact recomputed lines of the verified state.

A private copy is not a user Save or a latest output. It does not discharge
logical dirty debt or qualify history. When the export clears the native
modified flag, the backend records that exact transition; the profile's
`export_native_dirty` declaration must match the observed transition, or the
outcome is uncertain and the session is held.

Before any selection, the backend also refuses with
`picture_saved_proof_unqualified` when the original caret or the target anchor
lies outside the profile's observed caret domain, and when a replacement would
need a new manifest item but the bound item carries an attribute the profile
does not qualify for new items.

The step result `saved_proof` reports the profile identity and its literal
`qualification`, `native_qualified`, `evidence_used`, `evidence_not_observed`,
both package digests, member counts by verdict, the bridge ordinal and the
command epoch. `native_qualified` is true only for a qualified profile whose
every field used by that request was observed natively.

### Profile contract

Schema `hwpx-saved-proof-profile/v2`, exact keys (`line_reflow` is optional):

| Key | Meaning |
| --- | --- |
| `profile_id` | Bounded identifier. |
| `qualification` | `status` `qualified` or `unqualified`, `basis` text, `evidence_sha256` (required when qualified). Reported literally; source never upgrades it. |
| `materialization` | Exactly `{"method": "SaveAs", "format": "HWPX", "args": "lock:false;export"}`. |
| `bridge` | Exactly `owpml_physical_ordinal/v1`. |
| `export_native_dirty` | `unchanged` or `cleared`: the native modified flag across the export. |
| `container_volatile` | ZIP fields a fresh serialization renews: subset of `date_time`, `extra`, `create_version`, `extract_version`, `create_system`. |
| `regenerated_members` | Only `Preview/PrvImage.png` with `kind` `png`, and `Preview/PrvText.txt` with `kind` `text` and `encoding` `utf-8`, `utf-16-le` or `utf-16-be`; each with `when` exactly `rendering_request` and `same_pixel_size` (`false` for text). No other member can be declared. A preview member cannot also hold volatile values or be the caret member. |
| `volatile_values` | Named element values (`member`, expanded `element`, `where` selector, `text` or `@attribute`) outside body and media, `value_type` `utc_second_z`, `bound` `native_write_window`. Selectors are distinct and never the caret element. |
| `caret` | Serialized caret `member`, expanded `element`, the attributes for `List`, `Para` and `Pos`, and the `observed_domain` of `List` and `Para` values. Required, because a changed edit moves the caret to the target anchor. |
| `replacement_manifest_attributes` | Attributes a new image manifest item may carry (`id`, `href`, `media-type` at least). |
| `public_save` | `save_false_after_export_clear`: whether the bounded public `Save(False)` below is declared. When `export_native_dirty` is `cleared` and this is not declared, no public save could complete an edit, so every picture edit is refused with `picture_saved_proof_unqualified` before any native call. |
| `history` | Per picture edit kind, declared native `undo_native_steps` and `redo_native_steps` (1..4). |
| `line_reflow` | Optional. `{"picture_layout_exact": {"scope": "target_paragraph_lines", "fields": [...]}}` with a non-empty subset of `baseline`, `horzpos`, `horzsize`, `textheight` and `vertsize`; see Request-bound line reflow. Absent means the line cache is compared exactly. |
| `evidence` | For each field above, `status` `observed` or `not_run` and a `basis`; `line_reflow` has a row only when declared. |

### Native data behind the current candidate

`docs/profiles/hwpx-saved-proof-e01-candidate.json` is an `unqualified`
candidate built from four actual private copies (clean, dirty, Undo, Redo) of
one ordinary native-authored document on Hancom 13.0.0.3457 with backend Python
3.13.14. `tests/fixtures/native_e01` holds those exact archives as base64
envelopes that decode to the native bytes, the matching `GetTextFile("HWPML2X")`
producer strings and the facts taken from the producer witness. The product
reader, bridge, comparator and history digest run on them in
`tests/test_picture_native_e01.py`.

Observed on that runtime: `SaveAs(path, "HWPX", "lock:false;export")` completed
behind the exact-`True` guard four times, kept the working path, caret and empty
selection, and wrote complete 12-member packages. The export changed the native
modified flag false→false and true→false. Every ZIP container field and the
member order stayed identical. Clean versus Undo and dirty versus Redo differed
only in the `content.hpf` `ModifiedDate` text, each value inside its own write
window. `settings.xml` `CaretPosition` matched native `GetPos` for List 0 and
Para 0. The physical bridge and the native control census matched for the one
body picture. Previews were identical for identical content. The persisted
producer strings are strict UTF-8 matching the witness digest and length,
although they keep the in-memory `encoding="UTF-16"` declaration.

Not observed, and `not_run` in the candidate: a picture edit and its preview,
ordinary Save, `Save(False)`, close and reopen, picture Undo/Redo, consecutive
unchanged no-op copies, carets outside List 0 / Para 0, script members, and a
replacement manifest allocation. The actual image manifest item also carries
`hashkey`, which is not MD5, SHA-1 or SHA-256 of the member bytes, so a
replacement that needs a new item is refused under this candidate. The candidate
declares no picture history kind. Because `Save(False)` was not observed, it
declares no completing public save (`save_false_after_export_clear: false`);
with `export_native_dirty` `cleared`, picture edits are refused under it before
any native call, and it serves the qualification measurements only.

### Native data behind the h01 test profile

`docs/profiles/hwpx-saved-proof-h01-reflow-test.json` is the `unqualified` test
profile used for the public picture run described below. It keeps the candidate's observed
fields and adds two later native observations on Hancom 13.0.0.3457, kept in
`tests/fixtures/native_h01` (archives as base64 envelopes that decode to the
native bytes):

- g01: after one public body insertion, one private copy changed the native flag
  true→false and one `Save(False)` returned through the exact-`True` guard. The
  saved file is byte-identical to the private copy (17,767 bytes). This is the
  observed `public_save` for that method; it is not a saved picture edit.
- h01: one public `picture_layout_exact` with `mode: "square"` on the inline
  TopAndBottom picture reached a typed `Execute` returning exact `True`. Between
  the before and after private copies only the requested `treatAsChar` and
  `textWrap`, the caret at the target anchor, `ModifiedDate` and the `lineseg`
  `baseline`, `horzpos`, `horzsize`, `textheight` and `vertsize` of the
  picture's own single-line paragraph changed. That run was held by the
  comparison before the reflow contract existed; no save of it was observed.
  `line_reflow` records this observation.

With this profile and the reflow contract above, `picture_layout_exact` with
`mode: "square"` on an ordinary inline body picture without a caption has been
exercised once natively on Hancom 13.0.0.3457 through public MCP. The bounded
public `Save(False)` was issued immediately after the edit; the saved HWPX was
verified externally by digest; the reopened file showed the picture
Square/floating with the same image payload; and native PDF and page PNG
rendering of the saved file succeeded, followed by a public guarded close. The
automatic review reports text as unverified for this image-only document.

Not established: a save issued later than the edit, crop, replacement, the
inline mode, offsets, picture history, multi-line or multi-paragraph reflow, a preview change
under an edit, replacement allocation, rich, shared and multi-picture documents,
other Hancom versions and general native qualification.

### History under a profile

A changed step of a kind listed in `history` records the semantic digests of its
complete before and after packages (volatile values and caret fields masked;
previews and every other member compared).
Undo or Redo issues the declared number of native calls, each requiring exact
`True`, then writes a private copy and compares its digest with the recorded
state. A match makes that package the verified current state. A mismatch is
reported as a state mismatch and stops further history; the reached package,
an exact private copy of the in-memory document, becomes the save baseline. A
first exact `False` whose private copy proves the recorded origin state is
refused as not applied, and that copy becomes the save baseline. Several dirty picture
steps in one bundle compose only when every step is declared and each step's
before state equals the previous step's after state. Undeclared kinds, mixed
bundles and a changed or missing profile refuse before native history dispatch.

## Clean refusal codes

The public step carries one of these finite codes and a bounded explanation
when a known target/input preflight is refused without an uncertain SDK result:

- `picture_aspect_unqualified`
- `picture_asset_root_unproven`
- `picture_binding_stale`
- `picture_context_unqualified`
- `picture_crop_empty`
- `picture_inventory_limit`
- `picture_layout_unqualified`
- `picture_native_mapping_unqualified`
- `picture_saved_bridge_unproven`
- `picture_saved_proof_unqualified`
- `picture_source_unreadable`
- `picture_source_unsupported`
- `picture_target_unproven`

An exception raised inside an entered SDK call is not granted clean-refusal
provenance merely because it carries one of these codes. Technical or foreign
failures remain `UNKNOWN` and retain the owning native session.

## Caret and effective TextFlow

A changed picture operation explicitly moves to the verified target anchor.
It does not promise to preserve the caller's original caret or selection.
Before moving, the backend requires the original exact native coordinates to
match the unique `HEAD/DOCSETTING/CARETPOS` coordinates. It declares the target
anchor before `SetPosBySet`, verifies the exact position immediately afterward
and again before Execute, and checks the exported post-state against that plan.
Only those three fields at that exact location may reflect this UI transition;
all other HEAD content, definitions, BODY, TAIL and media stay protected.
The public `ui_position` result reports `original`, `expected`,
`serialized_after`, `moved` and `selection_preserved` separately from content
preservation. Missing, duplicate, malformed or conflicting caret evidence holds
the session; it does not authorize normalization or restoration after failure.

For layout, the backend reads the current typed `HShapeObject.TextFlow` after
successful GetDefault and before request puts, then verifies it again before
Execute. The installed getter is DISPID 16431, `VT_UI2`; only exact integer enum
values 0–3 are admitted. The official parameter table maps them to both sides,
left only, right only and the larger side respectively. The HWPML spellings are
`BothSides`, `LeftOnly`, `RightOnly` and `LargestOnly`.

For an explicit inline-to-square request only, a previously absent target
`TextFlow` attribute may materialize with that same positively observed value.
There is no missing-value default. An existing value must agree with the getter
and remain exact. This allowance changes no other attribute or nested subtree.
The public `effective_text_flow` result identifies the observed prestate and any
representation materialization; it does not describe the raw XML as unchanged.

References: Hancom `ParameterSetTable_2504`, ShapeObject, pp. 127–128; Hancom
HWPML revision 1.2, CARETPOS table 15 and SHAPEOBJECT table 103.

## Saving

Saving uses the existing conditional consumer. It observes exact `IsModified`
before ordinary Save: exact `False` skips that call, while exact `True` reaches
one ordinary Save and still requires exact successful return. Missing or
malformed pre-save state stops before Save. A clean no-op does not erase earlier
possible unsaved-edit debt. An ordinary-save positive needs a genuine unsaved
edit, not an unconditional Save immediately after SaveAs.

Under `native_hwpx_package` the backend keeps the last recorded private copy as
the current in-memory state until the next command that marks mutating intent:
every command other than save and export, including navigation, selection and
read-only or export bundle steps. When such a command leaves the native flag
exactly `False` after this session's own export cleared it, save takes one fresh
private copy with the same pinned profile and binds it to the last recorded
package by its semantic digest (caret fields and declared volatile values
masked, every other member compared). Only a bound copy becomes the save
baseline. Otherwise nothing is saved, the debt stays and `debt_reason` names
why: `hwpx_profile_changed`, `hwpx_profile_unavailable`,
`hwpx_save_false_undeclared`, `hwpx_fresh_copy_unbound` or
`hwpx_no_recorded_package`. A failed fresh copy holds the session.

The save response reports `save_decision`:

- `ordinary_save`: exact `True`, as above.
- `verified_save_false`: exact `False` only because this session's own private
  export cleared the native flag, with logical debt remaining, a current
  verified package and a profile that declares `save_false_after_export_clear`.
  The working document path must still be the owned working copy. One
  documented unconditional `Save(False)` is issued and must return exact `True`.
- `clean_noop`: otherwise. With earlier debt, `logical_debt_retained` is true,
  nothing new was written and close stays refused.

When a current verified package exists, the saved working copy is read back
completely and must equal that package under the same profile allowances.
`saved_proof` then reports `saved_package_equals_verified_private_copy`, the
saved package digest, the member-manifest digest and member counts, and
`baseline`, the recorded package the save was proved against:
`verified_edit`, `before_copy`, `history_match`, `history_not_applied`,
`history_mismatch_reached` or `save_fresh_copy` (with `fresh_copy_bound_to`
naming the package the fresh copy was bound to). With `save_fresh_copy`,
`evidence_used` also lists `caret` and `export_native_dirty`, which that binding
and its export rely on. Debt is
discharged only with this proof; a non-`True` Save return or any saved-package
difference holds the session with the debt retained.

`hwpx save --out FILE` checks that the downloaded bytes equal the proved saved
package before recording it. `hwpx verify-saved FILE [--proof proof.json]`
re-reads a downloaded file completely, with no server call, and binds it to the
recorded proof by package digest, member manifest and member count. The MCP
adapter keeps a saved copy only when its bytes equal the proved package. Only an
absent (`null`) `saved_proof` keeps the earlier capture contract: a present proof
without a lowercase hex SHA-256 `saved_package_sha256` makes the CLI keep
nothing, and the MCP adapter refuses the copy with `ARTIFACT_IDENTITY_MISMATCH`
at save and again at close. These checks are static: a Hancom reopen and
rendered pages are separate evidence.

Beyond the one square layout run described above, required acceptance still includes real public MCP edits of the other operations and modes, per-variant Undo/Redo, saved/reopened whole-document and shared-media preservation, PDF export, all-page PNG inspection and actual readable native examples. These are not satisfied by schema discovery, synthetic success, a pre-edit baseline, or safe retirement of a failed test owner.
