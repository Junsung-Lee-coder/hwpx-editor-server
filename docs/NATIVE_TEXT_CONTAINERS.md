# Native text containers

These are dedicated command-bundle operations, also exposed through
`hwpx_command` and `hwpx_bundle`. They drive the native Hancom interface;
HWPML2X is read-only evidence, never an editing format.

## Contracts

- `text_container_inventory`: no arguments beyond op/label. Returns `containers`
  with kind, `target_id`, complete owner `hash`, eligibility and refusal code.
  `text` is complete up to 2,000 characters. Longer text is `null`, with
  `text_complete=false`, `text_chars`, and a 300-character `text_preview`.
- `text_container_read`: `target_id`, `expected_hash`. Returns the complete
  container text and the same binding. Never substitute the preview for text.
- `text_container_text_exact`: `target_id`, `expected_hash`, complete
  `expected_text` (1..20,000 characters), `new_text` (1..2,000 characters),
  `confirm_layout=true`. Replaces the entire text, not a substring or occurrence.
- `text_box_create`: `text` (1..2,000 characters), `width_mm`, `height_mm`
  (each 5..200), exact three-integer `expected_pos` from the current body caret,
  `confirm_layout=true`. Creates one inline horizontal text box and returns its
  new binding. The verified caret remains inside the created box; move to a
  plain body caret before another container edit.

All text inputs are single-line, preserve whitespace, and reject control
characters and invalid Unicode surrogate code points. Edits start from a plain
unselected body caret. Creation uses `DrawObjCreatorTextBox`/`HShapeObject`,
rectangle `ShapeDrawLayOut` points, inline dimensions and `AdjustTextbox=1`;
text uses `HInsertText`, with native `Delete` of the proven old range for a
replacement. Raw exact-case methods are used rather than pyhwpx compound
selection/scanning helpers that could make calls after an internal failure.

Creation explicitly sets ShapeObject WidthRelTo=4 and HeightRelTo=2 (absolute
HWPUNIT per ParameterSetTable), rather than inheriting percentage modes. The
first post-creation document read must prove the single new owner at the exact
pristine request-specific body paragraph and native offset, before any further
navigation or text insertion. Removing the new owner from a document is only
an outside-content check, never an insertion-location oracle.

That same first read checks the actual four rectangle points, original/effective
component extents and finite rendering matrices, not merely declared SIZE.
This narrow creation path admits unscaled, unrotated, unflipped horizontal
geometry only. Explicit zero current extents/scales, coincident/nonfinite or
malformed points, percentage size modes and vertical/linked text are refused
technically without a follow-on SDK call. Both current extents may be omitted
as in retained native rectangle readback; extents are then derived from the
explicit original canvas and matrices, not a guessed current-size default.
No decorative border or source-only native-render gate is imposed.

The unlinked native constructor representation may emit the exact PARALIST pair
`LinkListID="0"` / `LinkListIDNext="0"`. This pair, or the pre-existing absent/empty
pair representation, is admitted without numeric coercion. Nonzero, partial-zero,
malformed or ambiguously cased links remain unsupported. This does not relax
points, current extents, absolute size, grouping or transform proof.

At an independently proven body-end creation anchor, the constructor can append
one empty CHAR immediately after the new owner in the same final TEXT run.
Only when no empty end marker existed before, that exact attribute-free,
child-free, text/tail-free terminal CHAR is excluded together with the new owner
from the request-bound pristine paragraph/outside-content comparison. Existing
empty markers, unrelated empty nodes, CHAR attributes and run/end formatting
remain covered. Exact owner cardinality, paragraph and native offset must still
match; both post-create and post-text proofs remain mandatory. This is a narrow
constructor serialization allowance, not a document-wide empty-node mask or a
saved/reopened/native-render qualification claim.

## Native owner binding and identifier namespaces

Inventory/read remain full HWPML content observations: `target_id` and the hash
bind the serialized owner, not an SDK selector. No text mutation is authorized
by comparing an XML SHAPEOBJECT InstId or SHAPECOMPONENT InstID directly with a
native GetCtrlInstID string.

Before navigation for a non-no-op edit, and after the creation proof for a new
box, the engine chooses one identity route up front: enumerate HeadCtrl/Next,
filter exact CtrlID before target-only getters, read GetAnchorPos(0) with strict
List/Para/Pos integers, and bind the uniquely matching parsed body owner anchor
to the observed native instance ID. The complete same-kind body anchor census,
unique native IDs and all observed same-kind anchors are checked. A section or
other well-typed foreign CtrlID is not asked for a target-selector instance ID.
Required gso/footnote target IDs remain strict: empty/malformed results HOLD.

Every full census is centrally guarded as a native read-only state boundary,
including the initial and post-list-scan observations. Before its first native
getter, `_binding` captures the full immutable native witness: exact GetPos,
raw BOOL+six-LONG selection operands, unmasked SelectionMode, and observed
CurSelectedCtrl kind/native ID (or positively observed absence). GetPos, mode
and selected identity are rechecked. Only a normally completed, valid census
reaches the outgoing witness; all fields participate in equality. Any difference
HOLDs before dependent navigation, selection, mutation or restoration can erase
it. A getter or state-capture fault/invalid value stops immediately: no outgoing
SDK query runs in a `finally` or other failure path. The strict state interface
is required even for a direct binding call; a missing member is not permission
to default the state or skip the guard. Existing `_require_owner` guards remain.

List discovery and ParentCtrl selection checks compare against this observed
native binding, never an XML ID alias. The full binding census is refreshed at
selection/complete-block/insertion/post-proof boundaries; cross-snapshot identity
or position/selection drift holds before further dependent work. Duplicate or
foreign anchors/IDs, invalid required getters and incomplete/over-budget census
are technical uncertainty, not an alternate lookup, default ID or retry route.
No SelectCtrl/FindCtrl/BySet fallback is introduced. Full exact text/block,
physical geometry and non-target preservation proofs remain mandatory. This
contract does not by itself establish native qualification of a variant.

## Constructor state and request-bound text entry

The native state witness is not a linear text range or a selected-object
cardinality proof. Exact known mode values and the Desktop strict bit are kept,
not reduced with an unknown-bit mask. Normal/strict-normal text ranges are
projected only for explicit linear intent checks. Column/table/object modes do
not become linear ranges or empty selections by coercion. Missing state members,
malformed operands, inconsistent normal state or unknown bits remain technical
HOLD. The raw selection validator and native/XML identity namespaces are unchanged.

After the one typed DrawObjCreatorTextBox Execute, the FIRST native document
read and complete new-owner/anchor/physical-geometry/outside proof still precede
navigation. A newly empty owner may lack nonempty exact-edit eligibility only;
all other owner/list/paragraph/group/control restrictions remain required.

Creation then uses one predeclared protocol, not a locator fallback:

- Capture the complete witness and fresh unique native owner binding. Require
  explicit unlocked/ungrouped geometry and constructor state0 or object4/20.
  Object state must identify the newly created native gso; a foreign selected
  identity is HOLD, not an invitation to clear it and try again.
- A positively known object state gets one exact BOOL HAction.Run('Cancel').
  Then prove mode0, no text block and CurSelectedCtrl=None. State0 gets the same
  positive empty-selection proof without Cancel. Unknown/column/table state
  never authorizes this transition.
- Move to the proven new body anchor and refresh the binding. From verified
  empty selection, one ordinary SelectCtrlFront on the unlocked, ungrouped
  unique owner establishes a singleton. Verify selected native gso identity and
  body ParentCtrl. Mode4 or CurSelectedCtrl alone is never cardinality authority.
- Run ShapeObjTextBoxEdit once through the exact BOOL HAction interface (not
  VOID IHwpObject.Run). Prove empty normal state, actual nonzero-list/para0/pos0,
  and required ParentCtrl identity equals the new native binding. Complete
  document/read-purity checks surround UI transitions; no constructor list scan
  or alternate text-entry attempt follows any failure.
- Only then prepare/execute typed InsertText. Full witness equality protects
  parameter preparation. Pure complete text/frame/outside proofs precede any
  outgoing state/owner SDK checks; failure never triggers restoration or cleanup.

Desktop authority is the publisher-linked HwpAutomation_2504/ActionTable_2504
and the installed typed Automation interface. Official forum topics610/890/899
illustrate the shared action lifecycle but their HwpCtrl wrappers are not Desktop
return-value authority. The source contract alone does not establish a native
SelectionMode/GetPos value or qualify an untested variant.

## Empty census kind: closed-coverage product criterion

Exact empty str is pending unclassified, NOT terminal/foreign/target.
HeadCtrl/Next must still reach actual None within2048; never trim/casefold/default
or substitute an empty target/native ID. Other malformed/case values and required
ParentCtrl/selected kind/instance-ID checks are unchanged and immediately HOLD.

Empty exclusion requires complete independent BODY coverage: ALL HML supported
gso shape owners and fn must exactly equal the body owner-node universe for the
wanted kind. Mixed-kind/nested/grouped universes are not admitted by this narrow
route. Every required native target must uniquely match its exact body anchor,
all native IDs unique, no extra/nonbody/other-kind target. No target-only getter
is called on unclassified empty or foreign controls. Missing/duplicate/wronganchor/
empty required owner, Next fault/budget exhaustion or any failed proof remains HOLD.

Full census witness equality is checked BEFORE an additional HML read, so that
read cannot erase getter drift. Full resolved document signature and the read's
outgoing witness must also match; every foreign subtree/HEAD/field/style/media/
numbering/outline/body participates. For exact-edit's sole intermediate append,
request-bound old+new text must match exactly and only that paragraph's text
characters are masked, retaining all formats/prefix controls/foreign content.
After complete original post-edit/post-create preservation/geometry/read-purity
proof, binding context advances to that proven after-model before final owner
check. No unvalidated fresh snapshot supplies its own authority or cleanup path.

This closed-coverage rule admits only the explicitly described control universe;
it does not establish a native cause or qualification for another topology.

### Bounded CtrlID fault observation

No trim/casefold/default/sentinel rule is inferred from an invalid diagnostic
label or later saved serialization. Existing finite capture/reader is unchanged.

At the existing control.CtrlID, ParentCtrl.CtrlID and selected.CtrlID returns,
a finite inert capsule records exact-builtin category/length and bounded
primitive references BEFORE validation, while retaining raw privately. It never
asks an opaque object/subclass for repr, str, bool, equality, attributes, length
or a type name. A getter exception records reached-but-unreturned, not a value.
Only the actual same failing return boundary (or its direct selected-kind check)
can carry the capsule; unrelated later faults never publish a stale observation.

After existing owning HOLD, the failure reader may publish six bounded strings:
`native_ctrlid_boundary`, `native_ctrlid_type`, `native_ctrlid_length`,
`native_ctrlid_primitive`, `native_ctrlid_sha256`, `native_ctrlid_returned`.
String length is Python exact-builtin code-point length. Up to8 characters/bytes
are encoded, not formatted via object callbacks; string samples use UTF16LE hex
with surrogatepass, bytes use hex, int32 uses bounded decimal and float64 uses
binary hex. Exact string/bytes digests are emitted only within4096 input units;
otherwise sample/digest is omitted/unavailable. No exception message, document
text/path, opaque class name or raw object is published. All encoding/hash/I/O
is post-HOLD. Optional capture/publication failure cannot change acceptance,
replace the original cause/debt, issue SDK follow-on, cleanup or unsettle Future.
Foreign exception-dict keys/existing slots are not overwritten for diagnostics.

These fields supply a narrow prospective observation, not a behavioral repair,
a historical native CtrlID value or permission for another native operation.

## Binding-document comparison operands

No acceptance/normalization/preservation criterion is relaxed. A real
`binding.document/invalid` is a false comparison, not the `raised` setup/exception
branch. Public Create does not set the append marker; its eligible comparator is
the full resolved document signature. Missing phase/operands remain unknown.

At ONLY this already-entered comparison failure, a finite private tuple retains
branch, fixed caller phase, actual compared fingerprints/count/text operands,
XML owner and both exact HML strings ALREADY obtained by existing reads. Parsed
models keep an inert private HML reference within existing MAX_HWPML_CHARS; no
additional SDK/getter, serialization, digest or I/O occurs for capture. Existing
first fault and preservation guards remain, with optional cache-entry failure caught.

After actual owning HOLD, the existing PRIVATE command reconciliation journal
(`metadata/command-journal.json` under the same owned session) may store
`container_binding_comparison` with exact `expected_hml`/`observed_hml`, signature
operands and request-bound text when present. Same private permissions/retention/
protected-held-command lifetime; no new path, public artifact/URL or raw document
in public result/status. This permits offline reparse/delta analysis, not a new
SDK query. No healthy comparison pair is persisted. Missing/capture/write failure
means unavailable, never fabricated/reconstructed data.

Public diagnostics contain only seven bounded strings: native_binding_branch,
native_binding_phase, native_binding_expected, native_binding_observed,
native_binding_xml_owner, native_binding_expected_hml_length and
native_binding_observed_hml_length. Logical fingerprints preserve the existing
`sha256:` +24hex operand exactly (96-bit truncation, NOT a new full256-bit proof).
Append text fingerprints are optional UTF16LE SHA256 computed ONLY after HOLD;
count mismatch reports actual count, not an unread peer value. No raw HML/text,
exception message, arbitrary attribute/type-name/repr or opaque object projected.
Cache/digest/public/private journal failures cannot replace first cause/debt,
unhold or unsettle Future. Original mixed/nested/grouped EMPTY limitation stays.
Diagnostics do not establish a behavior repair or qualify an untested variant.

## Logical document identity and serialized view state

The HML contract distinguishes document content/settings from the current caret
location. Official HWPML revision1.2 table15 defines `CARETPOS` under
`HEAD/DOCSETTING` with `List`, `Para`, `Pos` location components; Desktop
Automation's `GetPos` separately observes the live caret.

Logical signatures omit ONLY the values of these three attributes on ONE
well-formed, unqualified, direct `HWPML/HEAD/DOCSETTING/CARETPOS` leaf. All three
must exist, be the ONLY attributes, and have canonical nonnegative signed-LONG
decimal representation. The path must be unique. Unsupported/malformed/extended/
namespaced/wrong-path/duplicate structures receive no projection. Node presence,
all other attributes/text/tails/children and every sibling remain significant.

Qualified eligibility is encoded OUTSIDE XML data: the qualified Python signature
shell has a third structural domain slot; every raw/unqualified shell retains
exactly the original two slots. Coordinates are omitted only in the temporary
qualified clone. No XML-string sentinel is used. Raw malformed strings, including
strings resembling a domain label, cannot forge the shell's structure. This is
prehash domain separation, not a claim that the existing96-bit digest is a full
file hash or collision-free cryptographic authority. No XML/model/file/SDK value
is rewritten.

This allows intentional cursor navigation and text entry to update serializer
view coordinates without falsely classifying unchanged document content as damage.
It does NOT authorize read-side caret/selection/mode/owner changes: full incoming/
outgoing native witnesses and read-purity checks are still mandatory. Native
instance IDs, owner cardinality, anchors, geometry, first-HML/outside preservation,
DOCSUMMARY (including dates), BEGINNUMBER, numbering/outline/fields/styles/media
and document text remain protected. A meaningful fresh-read delta still HOLDs.

References: Hancom HWPML revision1.2 tables13–15 (publisher document,
https://cdn.hancom.com/link/docs/%ED%95%9C%EA%B8%80%EB%AC%B8%EC%84%9C%ED%8C%8C%EC%9D%BC%ED%98%95%EC%8B%9D3.0_HWPML_revision1.2.pdf),
Desktop HwpAutomation2504 GetPos/GetTextFile contracts. This is a logical proof
criterion, not a broad HEAD/ID/time mask or a claim of real Save/reopen/render.
The existing bounded private comparison capture remains unchanged and preserves
original HML strings, not a projected or rewritten document.

## Supported source variants and limitations

Text boxes, drawing/picture captions and footnotes are editable only when the
owner has one native list, one paragraph, one unambiguous native instance
identity and uniform character format. A leading native `AUTONUM` in a caption
or footnote stays outside the replacement range. Owners with no provable native
identity remain readable but are not editable. Captions on tables (multiple
owner lists), objects combining caption and draw-text lists, grouped/nested
owners, linked/vertical/floating creation, multiple paragraphs, empty values,
embedded fields/objects, mixed character format and text containing tabs or
line breaks are not supported by this increment. Footnote identity availability
and the raw native list mapping must be measured on the admitted host; their
presence in a portable model is not native evidence.

Shape grouping admission reads the authoritative SHAPECOMPONENT GroupLevel and
container ancestry. A nonzero level makes inventory `editable=false` with
`container_grouped_owner` and edits refuse before caret/navigation. Malformed,
duplicate, noncanonical or conflicting component/SHAPEOBJECT grouping evidence
is a technical HOLD during the document read. An omitted GroupLevel defaults
to zero only on an actual component, as HWPML table 116 specifies; a missing
component is not invented. Footnotes are not drawing components and do not need
a fabricated SHAPECOMPONENT. Source SDK models establish no native group claim.

Binding is by full owner identity and full owner hash, never by text alone.
Every native list candidate is read back and its `ParentCtrl` kind and exact
`GetCtrlInstID()` checked. Exactly one owner/list mapping is required. Before
replacement the selected block must contain exactly the complete expected text.
The document and caret are reread before the first edit, and after mutation the
entire resolved document is checked, allowing only the requested text delta or
one newly created box. Non-target body, owner shells, controls, styles,
definitions and embedded media participate in this live-document comparison.
No saved-package preservation or rendered acceptance is inferred from it.

An entered malformed/ambiguous native read, setter result, selection identity or
mutation result is a technical HOLD. No alternate method, restoration, diagnostic
getter, reread, save or close is issued afterward by the operation. A healthy
pre-edit ineligibility refusal is separate; navigation, if entered, is restored
and the unchanged document proved. No automatic rollback is attempted.

The complete-document signature retains the exact section OutlineShape
reference identity and its fully resolved numbering definition. It does not
inherit the numbering engine's deliberate OutlineShape mask; that shared
engine is unchanged. A valid unrelated section reference 1 -> 2 cannot be
certified preserved even when both definitions remain present and unchanged.

Selected-position evidence is validated at the first native read: exact BOOL
plus six nonnegative signed-LONG coordinates (0..2147483647), or canonical
`(False, None, None, None, None, None, None)`. Malformed, negative, bool/float
coordinates or an out-of-LONG integer latch before SelectionMode, a position
recheck or restoration. This local validation does not modify shared numbering
selection parsers or invent an observed raw positional ABI.

## Read-failure diagnostics

The existing `failure_diagnostic.native_site` and `native_return` may also carry
fixed `container.*` labels. These identify the entered raw read/check, or the
pure document parser phase (`document.input`, `xml`, `body`, `definitions`,
`outline`, `format`, `containers`, `identity`). `native_return` is a fixed
exact-builtin shape or `raised`, never a native value, document text, exception
message, type name, HRESULT, path or arbitrary metadata. An absent/unknown label
remains unknown. The shared `NativeNumberingReadFailure` class alone does not
identify a numbering getter or an underlying historical cause.

The owning runtime is held before diagnostic projection/reporting. Labels use
only already available Python state and add no SDK call, restoration, retry,
save or close. They do not change string/position/selection/metadata acceptance,
preservation, dirty accounting or source-owner guards, and confer no release or
replay authority. A diagnostic-only improvement is not a native behavior fix or
feature acceptance; the first fresh edit and saved/reopened/rendered artifacts
remain necessary.

A post-intent `NativeNumberingMutationError` also carries the already cached
fixed read/check labels from its exact `NativeNumberingReadFailure` cause.
Only the bounded exact-builtin fields are copied at producer wrapping; public
projection does not walk a cause chain or query an SDK object to recover them.
Grouped anchor/geometry proof failures use `created_box_geometry` and
`created_box_geometry.after_text`; these are proof-group labels, not claims
about a particular native method or an unretained historical leaf.
Missing/unusable labels stay missing. The original exception/cause identity,
mutation-possible/dirty debt, owning HOLD, mandatory post-proofs and no-follow-on
rules remain. Such a response is never pristine or a clean pre-edit refusal,
and diagnostic continuity alone is not a native editing/persistence success.

## Qualification and persistence

The tested native increment is one inline horizontal 40 × 20 mm text box:
public MCP creation and inventory, ordinary Save, reopening the exact saved file,
complete textbox read, a one-page native PDF with every PNG page decoded and
visually checked, and healthy normal public close. Saved-package preservation
and the delivered text/layout were independently checked for that exact workflow.
The test qualifies that bounded workflow, not all inputs admitted by the schema.

Existing-box replacement, drawing/picture captions, footnotes, other dimensions
or orientations, and history are NOT native-qualified by this test. Inventory
and read evidence is limited to the tested created/saved textbox; it does not
qualify every other owner type. Portable models are not native evidence.
Conservative per-variant runtime capability metadata is unchanged; the bounded
workflow result does not promote every creation or edit variant to qualified.

Undo/Redo are deliberately not counted and native history is refused after a
changed container command. Source/model tests are not a native edit claim.
Use the existing save/retrieve/reopen/PDF/all-page PNG workflow on an owned
fixture and independently compare the saved HWPX package before accepting any
variant. In particular, preserve every non-target control, style definition and
original media member, and verify the actual note/caption numbering, frame,
text and page layout after reopen. Do not continue native calls after a HOLD.

### Positional ABI and host scope

The retained Automation manual declares raw SetPos as void. The pinned pyhwpx
wrapper ignores that raw return and constructs its own Boolean from a subsequent
GetPos. Its get_selected_pos tuple is assembled with two ListParaPos sets and
GetSelectedPosBySet, not proof of raw no-argument GetSelectedPos behavior.
Current strict raw return checks must not be broadened to truthiness or supplied
an after-UNKNOWN alternate based on a model fixture.

The successful bounded workflow used its qualified installed Desktop binding.
That observation does not establish compatibility across other host versions,
typelibs or wrapper families. Preserve exact typed return checks and the selected
positional protocol; a different binding requires its own compatibility evidence.
An incompatible binding is a technical refusal, never permission for a fallback
after an uncertain result. Source publication does not install or requalify a host.

## Sources

Hancom's official automation landing page links the ActionTable_2504,
ParameterSetTable_2504 and HwpAutomation_2504 manuals:
https://developer.hancom.com/hwpautomation

ActionTable lists `DrawObjCreatorTextBox` with `ShapeObject`. ParameterSetTable
lists the dated item spelling `AdjustTextBox` as PIT_UI1, dimensions in HWPUNIT,
and `DrawLayOut` point arrays. That item-table spelling/type is not the installed
generated COM accessor contract: accepted HShapeObject get/put maps declare exact
`AdjustTextbox`, DISPID16471, with a VT_UI2 getter. Its requested value remains 1;
no autofit/value/geometry policy is changed. The same existing requested-field
audit declares Width/Height VT_I4 and WidthRelTo/HeightRelTo/TreatAsChar VT_UI2.
Exact integers and the immediate requested-value equality check are required;
out-of-representation, bool/float/coerced values, mismatches and read exceptions
hold before Execute. The generated put-map does not encode a value VARTYPE;
the existing typed HSet.SetItem separately accepts BSTR/VARIANT and returns VOID.
No legacy CreateSet family, case alias or default getter is substituted. Hancom
forum examples at https://forum.developer.hancom.com/t/topic/685/2 and
https://forum.developer.hancom.com/t/insertctrl/2506 use `AdjustTextbox` as well.
Accessor diagnostics identify this corrected source expression, not a reconstructed
historical native exception. Automation documents GetPos/SetPos, GetSelectedPos, SelectText,
GetTextFile and ParentCtrl. Actual host typelib signatures are still required
for native admission, especially raw positional outputs and setter returns.
Hancom forum topic 610 explains the distinct text-box editing caret:
https://forum.developer.hancom.com/t/topic/610

The lock-pinned pyhwpx 1.7.2 source confirms that its `select_text` and selected
text helpers contain additional native calls; this family avoids those helpers.

HWPML revision 1.2 (Hancom, 2014-11-05), tables 79, 96, 104, 116--119 and 134,
specifies outline references, horizontal text, size modes, grouping defaults,
component extents and rendering matrices:
https://cdn.hancom.com/link/docs/%ED%95%9C%EA%B8%80%EB%AC%B8%EC%84%9C%ED%8C%8C%EC%9D%BC%ED%98%95%EC%8B%9D3.0_HWPML_revision1.2.pdf
Retained native rectangle structure is also represented in
tests/fixtures/native_shapes/rectangle_inline.xml; it is not a native text-box
qualification and does not establish any COM ABI from a fixture.
