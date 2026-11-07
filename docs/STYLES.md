# List, apply, create and change named styles

These commands work on the document's own named styles (스타일): paragraph styles such as 바탕글, 본문 and 개요 1, and character styles such as 쪽 번호.

- **List:** `style_inventory`.
- **Apply one by name:** `style_apply_exact`.
- **Create one:** `style_create_exact`.
- **Change one:** `style_update_exact`.

They are not direct formatting: `character_format_exact` and `paragraph_format_exact` set a range's own format, and `paragraph_style_apply_exact` sets keep-with-next, widow/orphan and page break before. Send them through `hwpx_command` (one `request`) or `hwpx_bundle` (each item in `steps`); the CLI's JSON command/bundle surface accepts the same fields.

## How styles are read and changed

The document's HWPML2X text is both the source of every value and the proof of every change.

**Styles.** Its style list holds one style per entry, numbered from 0 in list order. That number is the index a paragraph names in `P/@Style`, and the index the official `Style` action applies. A style has:

- a kind: paragraph (`Para`) or character (`Char`);
- a name and, optionally, an English name;
- a next style;
- a form lock;
- a character shape and, for a paragraph style, a paragraph shape.

Shapes are compared by their content, never by their number: Hancom renumbers character shapes when it adds one, and a renumbered shape is not a changed style.

**Uses.** A paragraph style's use is every paragraph that names it, in the body, table cells, other lists such as text boxes, headers and footers. A text run names only its character shape, so the document records no use of a character style. None is reported for one.

**Native calls.** Apply uses the official `HAction.GetDefault`/`Execute` calls with a typed parameter set (HwpAutomation, ActionTable, ParameterSetTable). Both calls are documented BOOL, and only `True` is a success.

Create and change use the legacy action family instead, the way Hancom's own answers on its developer forum write it:

| Command | Action | Calls | Source |
|---|---|---|---|
| Apply | `Style` | `HAction.GetDefault`/`Execute` with `HParameterSet.HStyle`, whose `Apply` is the style index | ActionTable, ParameterSetTable; pinned pyhwpx 1.7.2 `set_style` |
| Create | `StyleAddNew` | `CreateAction`, `CreateSet`, `GetDefault(set)`, `SetItem` of `NameLocal`, `NameEng` and `Next`, the `CharShape` sub-set from `Item` set back with `SetItem("CharShape", sub-set)`, `Execute(set)` | forum topic 2505, post 2 (2025-05-20) |
| Change | `StyleDirectEdit` | first the style's whole character shape, `IHwpObject.CharShape` at its plain use, with the requested items written into it; then `CreateAction`, `CreateSet`, `GetDefault(set)`, `SetItem("NameLocal", <the style>)`, `SetItem("CharShape", <that set>)`, `Execute(set)` | forum topic 1552, post 4 (2024-06-11), for Hangul 2018 and later; the shape, HwpAutomation page 12 and ParameterSetTable sets 13 and 133; the set-back, forum topic 2354, post 5 (2025-03-27) |

What the sources say about these actions:

- **`Style`** is labelled 스타일 (글2005이하버전), for Hangul 2005 and earlier. `StyleEx`, 스타일 (글2007), takes the same set. Apply uses `Style`, as the pinned pyhwpx 1.7.2 `set_style` does.
- **`StyleAddNew`** and **`StyleDirectEdit`** are not in the ActionTable; Hancom's answers say so and give the code above. In `StyleDirectEdit`, `NameLocal` names the style to change.
- **The legacy family.** The manual (HwpAutomation, pages 61 to 64) declares `GetDefault`, `Execute` and `SetItem` void, `ItemExist` BOOL, and `CreateSet`, `CreateItemSet` and `Item` as returning a parameter set. `Item` answers 0 or an empty string for an item that does not exist.
  - The type library installed with Hancom 13.0.0.3457 (`HwpObject.tlb`, SHA-256 `047f64bd2714ea2eabb63d5771fea83a1f239a73393548f55d1771df116d9bf1`, 499,852 bytes) declares otherwise in part. On the action, `GetDefault` is VT_I4, `Execute` VT_BOOL and `CreateSet` a dispatch object. On the set, `SetItem` is VT_VOID, `ItemExist` VT_BOOL, `Item` a VARIANT and `CreateItemSet` a dispatch object.
  - The commands check the installed declarations.
- **Sub-sets are copies.** Hancom's answer in forum topic 2354, post 5 (2025-03-27): under Hangul Automation, a sub-set that `Item` or `CreateItemSet` gives is a copy, not a reference as in HwpCtrl. The filled sub-set is therefore set back on its parent with `SetItem`.
  - Topic 2505's `StyleAddNew` example does this; topic 1552's `StyleDirectEdit` example does not.
  - Both commands set the filled `CharShape` sub-set back once, before `Execute`.
- **A whole set, not a made one.** Topic 1552's `StyleDirectEdit` example makes its `CharShape` sub-set with `CreateItemSet`. Hancom's answer in forum topic 1044, post 6 (2023-11-09, about a `PageBorder` sub-set) says that a sub-set made with `CreateItemSet` needs every one of its child items set. To change only some, edit the sub-set `GetDefault` filled, which `Item` gives.
  - The create takes the `CharShape` sub-set `StyleAddNew`'s `GetDefault` filled (`ItemExist`, then `Item`), writes only the requested items into it and sets it back.
  - The change cannot: on Hancom 13.0.0.3457, `StyleDirectEdit`'s `GetDefault` held no `CharShape` sub-set for 바탕글 (see Native qualification). It writes the style's whole shape instead, read as below. Neither command makes a sub-set.
- **The change's whole shape.** The manual (HwpAutomation, page 12) describes `IHwpObject.CharShape`: a get gives the character shape of the selection, or of the caret when nothing is selected, as a `CharShape` parameter set. An item whose value differs within the selection is absent from it, and a put sets only the items the set holds.
  - The installed type library declares it DISPID 8, a dispatch object, with a get and a put. The command uses only the get.
  - The `CharShape` set (ParameterSetTable, set 13) has 67 items; `HCharShape` in the installed type library has the same 67 and spells `OutlineType` as `OutLineType`.
  - `StyleDirectEdit`'s set, `StyleItem` (set 133), holds `CharShape` as a set of that kind.
  - Forum topic 2354, post 2, quotes the official documentation's warning that no action is to be made while another awaits its `Execute`. The shape is read before `StyleDirectEdit` is made.
- **`HStyleItem`** cannot be handled from outside by any API (forum topic 1650, post 2, 2024-07-18). Create and change no longer use it, and no longer use `StyleAdd` or `StyleChangeToCurrentShape`. An older answer (topic 821, 2023) said adding a user style was not offered through the API; the 2024 and 2025 answers name the two actions above.

Every answer of the legacy family is checked before the next native call:

- **`GetDefault`:** exactly the builtin int that action's `GetDefault` answered natively, 1 for `StyleAddNew` and 1 for `StyleDirectEdit` (see Native qualification). That 1 is the call's observed answer, not a success: an edit succeeds only by `Execute`'s `True` and the proof.
- **`SetItem`:** `None`.
- **`ItemExist`:** a BOOL.
- **A set:** an object.
- **`Execute`:** exactly `True`, as the same `Execute` answered natively for this server's text and formatting commands.

The two families are never mixed: create and change read no `HAction` and pass no `HSet`.

## 1. List the styles

```json
{"op":"style_inventory"}
{"op":"style_inventory","name":"본문"}
```

The read-only result lists `styles` in index order, with `count`, `paragraph_styles`, `character_styles`, `duplicate_names` and `styles_hash`, a hash of the whole list. Each style has:

- `index`, `name`, `english_name` (null when the style has none) and `kind`: `paragraph` or `character`;
- `next_style`, with its index and name, and `lock_form`;
- `character`: font, size, bold, italic, underline and color, as the formatting commands name them, and `character_rest`, a digest of everything else in the shape;
- for a paragraph style, `paragraph` (alignment, indents, line spacing), `paragraph_rest`, and `numbering` (`{"kind":"outline","level":1}` for 개요 1), otherwise null;
- `hash`, which binds the style's index, kind, names, settings and both definitions;
- `uses`. A character style has `{"recorded":false}`. A paragraph style has:
  - `total` and `plain`. A use is plain when the paragraph shows exactly the style's definitions: its paragraph shape, and the character shape of every character, control and its last run.
  - `by_context`: counts for `body`, `table_cell`, `other`, `header` and `footer`.
  - `rows`: each use with its context, `paragraph_index` (the index the formatting commands take), or for a header or footer its `area` (such as `header:s0:both`) and `area_paragraph`, the first 80 characters of its text, and `plain`.
  - `rows_complete`.
  - `hash` over every use.

Up to 50 uses are listed per style, and up to 1000 with `name`; `total` and `hash` always cover every use. The document is refused as `style_model_unusable` when it has any of these:

- more than 1000 styles, or ids that are not the list order;
- a style with an unknown kind, without a name or with an undefined shape;
- a paragraph that names no paragraph style of the list;
- an inline mark (tab, line break, non-breaking or fixed-width space) with settings of its own.
 An inventory reads the document text once and writes nothing. A reply that is not a document text at all (not a string, empty, or not well-formed XML) is not this refusal: it is an unknown outcome, as below.

## 2. Apply a style by name

Copy the style's `name` and `hash` from the inventory, and give the target as the formatting commands take it: `match`, optionally with `through_match`, or `paragraph_index` with the paragraph's full `expected_text`; and `expected_page`.

```json
{"op":"style_apply_exact","style_name":"개요 1","expected_style_hash":"<inventory hash>","paragraph_index":3,"expected_text":"사업 개요","expected_page":1,"confirm_layout":true}
{"op":"style_apply_exact","style_name":"강조","expected_style_hash":"<inventory hash>","match":"반드시 지켜야 할 기한","expected_page":2,"confirm_layout":true}
```

**A paragraph style** goes to every paragraph the target touches, in the body or a table cell. Each paragraph takes the style's index and its paragraph shape. Its characters take the style's character shape, or, where they had a format of their own, may keep it. The result names which, per paragraph, in `characters`:

- `style`: every character has the style's character shape;
- `kept_direct`: characters that showed the old style's character shape have the new one, and the others kept theirs.

For a paragraph without a format of its own the two are the same.

**A character style** goes to the exact range: its characters take the style's character shape. Two things beside the range take it too: invisible controls Hancom joins to the block, as with `character_format_exact`, and the paragraph's last run when the range reaches it. The paragraph keeps its own style and paragraph shape.

**The result.**

- `style`: index, name, kind and hash.
- `target`: the paragraphs and the native block.
- `result.paragraphs`: for each target paragraph, `style_before`, `style_after` and the format before and after. For a character style this covers the range only.
- `result.native_actions` and `undo`.

A target already in the style, showing exactly its definitions (or a range already in the character style's shape), returns `changed:false` and `status:"already_set"` with no native call.

## 3. Create a style

A new paragraph style starts from an existing paragraph style's definitions, `based_on`, with the requested character properties over them. Copy `based_on`'s `hash` and the inventory's `styles_hash`:

```json
{"op":"style_create_exact","name":"보고서 제목","kind":"paragraph","based_on":"본문","expected_based_on_hash":"<hash>","expected_styles_hash":"<styles_hash>","next_style":"본문","size_pt":16,"bold":true,"confirm_layout":true}
```

- **Name:** `name` and `english_name` have 1 to 64 characters, with no control characters, unpaired surrogates or spaces at either end. The backend and the MCP models apply the same rule (`app/style_rules.py`). The English name is the name when it is omitted, and no style may already have the name.
- **Kind:** `kind` is `paragraph`. A character style is refused as `style_kind_unsupported` before any native style action, caret move or write: Hancom's `StyleAddNew` example creates a style without naming a kind.
- **Next style:** `next_style` names the paragraph style the next paragraph takes. A character style is refused as `style_kind_unsupported` before the caret moves. A new style takes itself when `next_style` is omitted. The command writes it as an index of the list after the action (see below).
- **Properties:** `font_family`, `size_pt`, `bold`, `italic`, `underline` and `text_color`, with the formatting commands' meanings and limits. None is required: without properties the new style has `based_on`'s definitions. Paragraph properties (`align`, the indents and line spacing) are refused as `style_variant_unsupported` before any native style action, caret move or write: the example sets only the character shape.

**How it is made.** The command makes the `StyleAddNew` action and its set, then puts the caret in the first body paragraph that uses `based_on` and shows exactly its definitions, so the defaults `GetDefault` fills from the caret are `based_on`'s. It writes the name, English name and next style.

The next style is written as `Next`, an index of the list as `StyleAddNew` leaves it. On Hancom 13.0.0.3457, in a native create based on 바탕글 at index 0, `StyleAddNew` put the new style right after the base, at index 1. Every old style, its next style included, and every paragraph's style kept its identity, and the new style's next style resolved as if the written `Next` were kept as given. So the command plans for the new style in the slot right after `based_on`:
- without `next_style`, `Next` is that slot, the new style itself;
- a requested next style before the slot keeps its index;
- one at or after the slot is one further.

For example, based on 바탕글 (0) with next style 본문 (1), `Next` is 2. If Hancom puts the new style anywhere else, the planned `Next` names another style or none, and the create holds (`new_style_wrong`, `next_style_dangling`). That slot is measured for one base at index 0 on one host; other bases are planned the same way, not measured.

For character properties it takes the `CharShape` sub-set those defaults hold (`ItemExist`, then `Item`), writes only the requested items into it and sets it back on the set. When the defaults hold no `CharShape` sub-set, the command is refused as `style_variant_unsupported`, with the caret given back: a sub-set made empty would not carry the base's other character properties. Then it executes the action and puts the caret back. When no plain body paragraph of `based_on` exists, the command is refused as `style_without_plain_body_use`.

**The result.** It returns the new style as the inventory lists it, `based_on`, the requested properties, `result.native_actions` (`["Execute:StyleAddNew"]`), `result.pattern_paragraph` (where the caret stood) and `next_binding`: the new style's hash and the new `styles_hash`. No paragraph changes, and the new style has no use until it is applied. The new style has the index Hancom gives it, which need not be the end of the list; the other styles, and the numbers that name them, may then have moved, so bind them again from the new inventory.

## 4. Change a style

Copy the style's `hash` and its `uses.total` and `uses.hash` from the inventory (read it with `name` to see every use). Give `all_uses: true` when the style has uses: every paragraph using the style takes the changed definitions, in the body, table cells, other lists, headers and footers.

```json
{"op":"style_update_exact","style_name":"본문","expected_style_hash":"<hash>","expected_uses":12,"expected_uses_hash":"<uses hash>","all_uses":true,"size_pt":11,"confirm_layout":true}
```

The command changes a paragraph style's character properties: `font_family`, `size_pt`, `bold`, `italic`, `underline` and `text_color`. The style keeps its index, kind, names, next style, lock and paragraph shape.

**How it is made.** The command puts the caret in the first body paragraph that shows the style exactly, with nothing selected. There it reads the style's whole character shape as `IHwpObject.CharShape`, before any action is made:

- **Whole:** every documented item of the `CharShape` set must be in it (`ItemExist`, either spelling of `OutlineType`).
- **The style's own:** every item the document text tells must have the style's value (`Item`): the size, colour, bold, italic, the underline's type, and for each of the 7 languages the font name, ratio, spacing, relative size and offset.

It writes only the requested items into that set, so every other character property stays the style's own. Then it makes the `StyleDirectEdit` action and its set, so whatever `GetDefault` fills the set's other items with comes from the same paragraph. It writes `NameLocal` as the style's own name: in this action `NameLocal` selects the style to change. It sets the shape on the set with `SetItem("CharShape", <the shape>)`, since under Automation the set is a copy, executes the action once and puts the caret back. It neither makes a sub-set nor puts `IHwpObject.CharShape`.

**When it is refused.**

- **A rename:** `new_name` and `new_english_name` are refused as `style_rename_unsupported` before any native style action, caret move or write. No documented mechanism renames a style, and `StyleDirectEdit`'s `NameLocal` only selects one.
- **Paragraph properties:** refused as `style_variant_unsupported` before any native style action, caret move or write; the example changes only the character shape.
- **A character style:** refused as `style_kind_unsupported`; the example changes a paragraph style.
- **A use with its own format:** when any paragraph using the style has a format of its own (`plain: false` in the inventory), the change is refused as `style_use_has_own_format`.
- **No plain body use:** a style that no body paragraph shows exactly (an unused style, or one used only in headers, footers or table cells) is refused as `style_without_plain_body_use`.
- **A shape that is not whole, or not the style's:** when the shape read at the style's use lacks a documented item, or an item the document text tells differs from it, the change is refused as `style_shape_unverified` before any action is made and before anything is written, with the caret given back. Items the document text does not tell are still compared after the action, by the proof.

**The result.**

- `style_before` and `style_after`.
- The requested changes.
- `result.native_actions` (`["Execute:StyleDirectEdit"]`) and `result.affected`: total, counts by context, and each use with its format before and after.
- `next_binding`: the new hash, use count and use hash.

A request whose values are already the style's returns `changed:false` and `status:"already_set"` with no native call.

**Still open.** Creating a character style, paragraph properties of a new or changed style, and renaming a style are product requirements these commands do not yet meet: each is refused, by its own code, until a documented mechanism is measured.

## Refusals

Each of these stops a command before any write. The document is unchanged, and is read again to prove it. When the command had already placed the caret or selected the block, the caret and any selection are given back first.

| Case | Code |
|---|---|
| No style has the name | `style_not_found` |
| Two or more styles have the name; or, for any edit, two styles of one kind share a name, so the proof could not tell them apart | `style_name_ambiguous` |
| The style changed since it was listed | `style_hash_mismatch` |
| The style list changed since it was listed (create) | `styles_hash_mismatch` |
| A character style to create or to change, or as `based_on` or `next_style` | `style_kind_unsupported` |
| Paragraph properties for a new or changed style, or character properties for a new style whose defaults hold no `CharShape` sub-set | `style_variant_unsupported` |
| The character shape read at the style's use to change it lacks a documented item, or is not the style's own where the document text tells | `style_shape_unverified` |
| `new_name` or `new_english_name` | `style_rename_unsupported` |
| The new name is taken | `style_name_taken` |
| No body paragraph shows the base style or the style exactly | `style_without_plain_body_use` |
| The style's uses are not as listed | `style_uses_mismatch` |
| The style has uses and `all_uses` is not true | `style_uses_unconfirmed` |
| A use has a format of its own | `style_use_has_own_format` |
| More than 1000 uses | `style_uses_too_many` |
| Apply's defaults could not be read into its parameter set (`GetDefault` returned `False`) | `style_action_unavailable` |
| The style list or a paragraph style cannot be read; or, for any edit, a style names a next style outside the list | `style_model_unusable` |
| A character style range reaching a numbered paragraph's end, whose mark takes its last run's shape | `target_changes_number_mark` |
| The document text and its paragraph lists disagree | `preservation_unprovable` |
| A native object, method or parameter set the wrapper object does not have at all, with no native code run to look it up | `native_unavailable` |
| `select_text` answered `False`, or the caret, the selection or the block's text is well-typed but somewhere else | `selection_unverified` |

The target refusals of the formatting commands apply to `style_apply_exact` as they are, among them `target_not_found`, `target_ambiguous`, `target_text_mismatch`, `target_in_header_footer`, `target_unsupported_context`, `target_crosses_control` and `target_wrong_page`. A request that breaks the request rules (names, hashes, kinds, properties, `expected_uses`, `all_uses`, `confirm_layout`) is rejected as invalid input before any native style action, caret move or write.

**Not refusals.** Once a command has placed the caret or selected the block, each of these is an unknown outcome at once:

- a native call that raises;
- an answer not of its documented type: apply's `GetDefault` other than `True` or `False`, a caret or selection that is not exact non-negative integers or moves between its reads, a `select_text` or search answer that is not a BOOL, or a parameter that cannot be set;
- apply's parameter set `HSet`, which `GetDefault` takes, read first: one that raises, is `None` or a plain value, or is absent. `GetDefault` is then not called;
- in the legacy family of create and change:
  - a `GetDefault` answer other than the exact builtin int observed for its action (1 for both): `None`, `True`, `False`, 0, -1, 2, any other int, an int subclass or any other object;
  - a `SetItem` answer other than `None`, the set-back of the `CharShape` sub-set included;
  - an `ItemExist` answer other than a BOOL;
  - a sub-set from `Item` that is not an object, or a sub-set without `SetItem`;
  - for a change, the read of `IHwpObject.CharShape`: a read that raises, is `None`, a plain value or absent; a set without `ItemExist`, `Item` or `SetItem`; an `ItemExist` other than a BOOL; or an `Item` that is not a str for a font name, or not an exact int within the values of its type for any other item. The types are those of ParameterSetTable set 13: `Height` PIT_I4 (a signed 32-bit value), `TextColor` PIT_UI4 (a 32-bit colour, read unsigned or signed: -2^31 to 2^32 - 1), `Bold`, `Italic`, `UnderlineType` and every ratio and relative size PIT_UI1 (0 to 255), every spacing and offset PIT_I1 (-128 to 127). A value of its type that is not the style's is the refusal `style_shape_unverified`;
  - for a change, its legacy family, which it reads after the caret moved and the shape was read: there even a `CreateAction`, `CreateSet`, `GetDefault`, `Execute` or `SetItem` absent without any lookup code holds, at `create_action.member`, `action.member` or `set.member`.

  An `Execute` answer other than exactly `True` is an unknown outcome after the action.

The session is held, and no further native call follows, not even giving the caret back.

**Where it stopped.** A held create or change, and a hold at the document text or a caret step, carries two fixed labels in the step's `failure_diagnostic`, and the MCP error details repeat them as `native_site` and `native_return`:

- `native_site`: where it stopped. Examples are `document.text`, `caret.place`, `caret.read`, `create_action.call`, `create_set.call`, `get_default.call`, `set_item.NameLocal`, `item_exist.CharShape`, `item.CharShape`, `subset.member`, `subset_set_item.Height` and `set_item.CharShape`, the set-back. For a change's shape: `char_shape.read`, `char_shape.member`, `char_shape.item_exist` and `char_shape.item`. `create_item_set.CharShape` is no longer a label: no command makes a sub-set.
- `native_return`: the exact builtin shape of the answer there, such as `none`, `true`, `false`, `int`, `str`, `object`, `raised`, `absent`, `empty` or `not_well_formed`.

Both come from fixed lists (`app/style_rules.py`), never from a value, a message or a type name, and no native call is made to find them. They describe the hold; they are no permission to close or retry.

**What the proof found.** When an edit holds because the document after its action is not as requested, the step carries `style_proof`. The MCP error details repeat it, and the command journal keeps it; the journal keeps no error message. The facts are measured before the edit holds, from the text already read, with no further native call:

- `schema` (`style-proof/v1`) and `command`;
- `old_styles`, `new_styles` and `matched`, the old styles found again by name and kind;
- `added`: for a create, the name and index of the new style of the requested name, when there is one;
- `problem_count`, with the first 20 problems in `problems` and `problems_cut` when there are more.

Each problem has a `predicate` and, as it applies, the style's `name`, its `old_index` and `new_index`, the `fields` that differ (`settings`, `character`, `paragraph`, `next`, `kind`, `english_name`, `lock`, `language`, `fonts`), the setting `attributes`, the `expected` and `found` style names, the paragraph's place (`where`, such as `paragraph 3` or `header:s0:both paragraph 0`) or the document `field`.

When a style's character shape or fonts are not as expected (`old_style_changed`, `new_style_wrong` or `changed_style_wrong` with `character` or `fonts`), the problem also says what of the shape differs. It is measured from the text the proof already read, with no further native call:

- `size_expected` and `size_found`: the size in points the shape should have and has;
- `character_items`: the first 32 differing items, each by a fixed label. A label is `attr_` and a character shape attribute (`attr_height`, `attr_textcolor`, `attr_shadecolor`), `bold`, `italic` or `underline`, `font_`, `font_type_` or `font_content_` and a language (`font_hangul`; `font_content_latin` when the font's whole content, `TYPEINFO` included, differs), or `child_` and another part of the shape (`child_ratio`, `child_charspacing`);
- `character_item_count`: how many items differ;
- `unrequested_changed`: whether any differing item is one the request did not name. For an old style, no item is requested.

A fault while measuring these leaves the problem, its fields and the hold as they are, without them.

The predicates are:

| Predicate | What failed |
|---|---|
| `old_style_missing`, `old_style_duplicated` | An old style is not in the list once by its name and kind. |
| `old_style_changed` | An old style changed its settings, a definition or the style its next style names. |
| `next_style_dangling` | A style names a next style outside the list. |
| `style_added` | A style was added that was not requested. |
| `new_style_missing`, `new_style_duplicated` | The requested style was not added once. |
| `new_style_wrong` | The new style is not as requested. |
| `changed_style_wrong` | The changed style is not as requested. |
| `paragraph_style` | A paragraph names another style than before, or the new style. |
| `paragraph_changed`, `paragraph_moved` | A paragraph outside the request changed or moved. |
| `target_paragraph` | A target paragraph or use is not as requested. |
| `document_changed` | Something else in the document changed. |

The facts carry style names of at most 64 characters, fixed labels, indexes and counts; never document text, a value, a path or an exception. The message names the first problems the same way and counts the rest. The facts reach the caller only when they are exactly of this shape, and a fault while projecting them leaves the hold as it is.

Before that point, and in the inventory, these are unknown outcomes at once as well, with the session held and no further native call, not even the read that would prove the document unchanged:

- **The document text:** a `get_text_file('HWPML2X')` reply that is not a string, is empty or is not well-formed XML, at the first read of a command, at the edit's own read or in the inventory. A well-formed document the style model cannot represent stays the refusal `style_model_unusable`.
- **The native capabilities and methods:** for apply, `HAction` with its `GetDefault` and `Execute` and `HParameterSet` with its `HStyle`; for create and change, `CreateAction`, the action it makes with its `CreateSet`, `GetDefault` and `Execute`, and the set `CreateSet` makes with the methods the command calls; and the methods the edit calls (`set_pos`, and `MoveDocBegin` and `find` for a table cell). The pinned pyhwpx 1.7.2 wrapper's `HAction` and `HParameterSet` are getters into the COM object (`core.py` lines 1248-1268 and 1302-1319), whose own members are dynamic. A read that raises, returns `None` or a plain value where an object is needed, or returns something that cannot be called where a method is needed, is an unknown outcome. An `AttributeError` is one too, unless the attribute is absent without any lookup code: no attribute, descriptor or class member by that name (`inspect.getattr_static`), and no `__getattr__` or `__getattribute__` of the object's classes. Only that absence, read before the edit moves the caret, is the refusal `native_unavailable`.

## Proof, history and saving

**The proof of an edit.** After the one action, the whole document text is compared with the state the request allows.

- **Every paragraph,** in the body, cells, other lists, headers and footers, is compared by:
  - the style it names, by name and kind; its paragraph shape; and every character and control with its data;
  - the whole content of each one's character shape, its fonts' whole content included (a font's `TYPEINFO` as well, not only its name and type);
  - the settings of the text run and `CHAR` holding each one, kept per character, so a run split or merge with the same settings is no change and a setting moved to other characters is one;
  - its last run, its other settings and its paragraph id.
  A paragraph the request does not reach must be exactly as it was. A paragraph it reaches must hold exactly what is described above for its command, and may take the new paragraph id Hancom gives an edited paragraph.
- **Every style** is found after the action by its name and kind, wherever it now is. An action may renumber the styles, and so the numbers that name them: a paragraph's style and a style's next style. A renumbering is no change.
  - **Compared:** each style's settings (its English name, lock and language included), its definitions' content, and the style its next style names, by name and kind, one step.
  - **Not compared:** the numbers. Nothing is matched by appearance: a style that looks the same under another name is another style.
  - **What may differ:** only the applied, created or changed style, and only as requested.
  - **A created style:** the one new style of the requested name, at whatever index, which no paragraph names yet.
- **Fonts of a new or changed style:** each keeps the content of the base style's font, except for a requested `font_family`. A requested font the document already lists must be that very font; only a font the document did not list may be new.
- **Headers and footers** are compared in full, except for their paragraphs' style references, which are compared by identity as above. While no style was renumbered, their full text is compared as well.
- **The rest:** the header and footer frames, the section and page settings, numbering, pictures and media, and the definition lists. Only the character, paragraph and font definition lists may gain or lose entries; applying a style may only drop definitions nothing uses any more.

Anything else stops as an unknown outcome: the session is held, and nothing is rolled back. So does a native read that raises, before or after the action.

**Undo.** The native Undo of these actions is not measured, so a style edit is not counted as an Undo step, and a following `undo` is refused with `undo_not_counted` before any native history call.

**Saving.** After an edit, call `hwpx_save` and keep the saved file it returns. Request `hwpx_proof` and keep the PDF and every page PNG. Then close, reopen the saved file and list the styles again to see the saved definitions and uses.

**Native qualification.** On Hancom 13.0.0.3457, these cases ran through the public MCP tools, each on a native-authored document with four click-here fields and the 23 built-in styles. The apply and the create ran with earlier versions of these commands, and the change with this version:

- **Apply:** `style_inventory` listed the styles, and `style_apply_exact` applied the existing paragraph style 개요 1 to a paragraph. The document was saved, the saved file copied out, closed, reopened and listed, rendered to a PDF and its page PNG, and closed.
- **Create:** `style_create_exact` created 검토용 문단, based on 바탕글 with next style 본문 and no character property. The new style was put at index 1, right after its base, with next style 본문 and no use. The document was saved, the saved file copied out, closed, reopened and listed, rendered to a one-page PDF and its PNG, and closed. Compared with the opened file, only the style list and the modification date changed. It covers one host, one base at index 0 and a new style without character properties.
- **Change:** with pyhwpx 1.7.2, `style_update_exact` changed 바탕글, the style at index 0, with its one plain body use, from 10 pt to 11 pt in one `StyleDirectEdit`. The inventory read it back at 11 pt, with every other style's entry and hash unchanged. The document was saved, the saved file copied out, closed, reopened and listed, with 바탕글 still at 11 pt, rendered to a one-page PDF and its PNG, where the paragraph's text takes four lines instead of three, and closed. Compared with the opened file, the saved package changed only in:
  - one new character shape: 바탕글's old one, with its height raised from 10 pt to 11 pt;
  - 바탕글 and the two text runs of its paragraph, which now name that shape;
  - that paragraph's line layout, which follows the larger size;
  - the modification date and the preview image.

  The other 22 styles, every old character shape, the fonts with their `TYPEINFO`, the paragraph's attributes, the 158-character text and the four field pairs with their values are unchanged.

The change covers one host, one style, one plain body use and the size only. Not covered by that run:

- **Other styles and uses:** other styles, other character properties, a style with more uses, and uses in headers, footers or table cells were not changed natively.
- **Saved proof:** the save reply's `saved_proof` was null, so `hwpx verify-saved` was not run. The saved package was checked by reading it.
- **Review coverage:** the review of the reopened file was `partial`. Its controls and page numbers were unverified, and its layout was not reported.
- **Undo:** the change was reported as one native editing action, which is not counted (see Undo).
- **The read shape:** that `IHwpObject.CharShape` held the 67 documented items with 바탕글's values, and the answers of `SetItem` and `Execute`, follow from the command's code path, which stops otherwise. The answers themselves were not kept.
- **The command journal:** it was not read before the guarded close removed the session's directory.
- **Exit paths:** whether the owner and worker processes made SDK calls on their exit paths is not recorded.

Other cases, also with earlier versions, held or were refused:

- **A create through `StyleAdd` with `HStyleItem`:** it held before its action, at a site that is not known.
- **A first create through `StyleAddNew`:** it held after `Execute` answered `True`, with one more style in the list. Its proof read the document text once after the action. It expected the new style at the end of the list and compared the old styles index by index, and it found styles 1 (본문) to 6 (개요 5) changed, the first six of its problems. Neither the text after the action nor its model was kept, so the new style's index and the list it held are not known. Nothing further was read after the hold. Whether the owner and worker processes made no SDK call on their exit paths after the hold is not established. The edit was discarded without saving.
- **A second create, with the same request:** it held after `Execute` answered `True` again. Its proof, comparing styles by identity, found 24 styles, all 23 old styles found again, and the new style at index 1, right after the base 바탕글. Its one problem: the new style's next style was the new style itself, not 본문. The set's `Next` 1, 본문's index before the action, named index 1 of the list after the insertion. Whether the stored number stayed 1 was not read separately. That edit was discarded without saving too. The command now plans `Next` for that slot, as described under Create.
- **A change of 바탕글, with its one plain body use, to 11 pt,** in three public calls (open, the inventory and the change): it held after `StyleDirectEdit`'s `Execute` answered `True`. Its proof found one problem, `changed_style_wrong` for 바탕글 with the field `character` only, and all 23 styles matched. Neither the text after the action nor its model was kept, so which character items differed, and whether the size became 11 pt, is not known. Nothing was called after the hold, and the edit was discarded without saving. That version made the change's `CharShape` sub-set with `CreateItemSet` and set only its size. Whether the made sub-set caused that result is not established. A held change now also names the differing items (see What the proof found).
- **The same change, with the version that edited the sub-set `GetDefault` filled,** in three public calls (open, the inventory, with the document's 23 styles and 바탕글 at 10 pt, and the change): it was refused as `style_variant_unsupported` before the edit. The session was neither held nor dirty, there was no `style_proof`, and the command journal recorded no possible mutation.
  - By the source path, that refusal is raised only where `ItemExist("CharShape")` answered `False` after `GetDefault` answered 1 and the `NameLocal` `SetItem` answered `None`. `Item`, the size write, the set-back and `Execute` were not reached. Those inner answers are inferred from the source, not read.
  - Why `StyleDirectEdit`'s defaults held no `CharShape` sub-set is not known. Nothing was called after the reply.
  - The change now writes the style's whole shape, read at its use, as described under Change a style.

`GetDefault` was also measured on its own, with no edit. Each measurement ran on its own fresh instance of Hancom 13.0.0.3457, on the native-authored seed:

- `StyleAddNew`'s `GetDefault` answered the exact builtin int 1 (2026-11-06T17:32:29Z);
- `StyleDirectEdit`'s answered 1 as well (18:00:23Z).

What that 1 means beyond the call's answer is not established. The tests replay actual HWPML2X text Hancom returned in earlier native runs: the inventory, and every command up to its first native call. They also cover the commands' logic over a fake that follows the official references, the installed declarations, the measured `GetDefault` answers and Hancom's examples, with copy sub-sets by default and HwpCtrl's references as an option. These are the points a native run decides beyond the measured cases above:

- **`StyleAddNew`:**
  - what `GetDefault` answers and fills from the caret: whether the set's defaults are the caret paragraph's style, and whether they hold a `CharShape` sub-set;
  - that `GetDefault` answers 1 within the edit, as measured, and `SetItem` and `Execute` as declared (`None`, and `True` for `Execute`);
  - where it puts the new style for a base other than 바탕글 at index 0, and whether every old style, every paragraph and every reference keeps its identity there;
  - whether the planned `Next` names the requested next style, or the new style itself when `next_style` is omitted;
  - whether the new style has the written names, keeps the caret paragraph's own style and opens no dialog.
- **`StyleDirectEdit`**, for Hangul 2018 and later: whether `IHwpObject.CharShape` at the style's plain use, with nothing selected, holds every documented item with the style's values; whether that set, with only the requested items changed and set on `StyleDirectEdit`'s set, changes only those; whether `NameLocal` selects the style; whether the set's other defaults leave the style's names and next style as they were; and whether every use takes the new definitions.
- **`Style`**, the action labelled for Hangul 2005 and earlier: which of the two outcomes Hancom gives a paragraph with a format of its own, and whether a character style over a block reaches only the block.

Each of these the proof checks after the action, so a different native behavior is an unknown outcome, never a reported success.
