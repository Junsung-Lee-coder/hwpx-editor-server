# Read and fill existing named fields

These commands work on the document's own data fields: click-here fields (누름틀), and fields named on table cells or text boxes. They are not the `[[BR::NAME]]` text placeholders of the authoring templates (fill those with `/compile-placeholder-fills` or `/fill-placeholders-and-convert`, MCP `hwpx_job`), and not the temporary cell-address fields the table commands use inside. Send them through `hwpx_command` (one `request`) or `hwpx_bundle` (each item in `steps`); the CLI's JSON command/bundle surface accepts the same fields.

They follow the official desktop automation contract (HwpAutomation, `GetFieldList`, `GetFieldText`, `PutFieldText`, `FieldExist`): a field is addressed as `name{{n}}`, `n` counting the fields of one name from 0 in document order.

## 1. List the fields

```json
{"op":"field_inventory"}
{"op":"field_inventory","name":"성명"}
```

The read-only result lists `fields` in document order. Each row has:

- `name`, cut at 200 characters with `name_truncated`;
- `occurrence` (from 1) and `occurrences` (how many fields have this name);
- `native_address` (`성명{{0}}`);
- `kind`: `click_here`, `cell`, `text_box`, `mixed` (one name used for fields of several kinds) or `unclassified`;
- `text`, the current value as the field list reads it: a tab is `\t`, a paragraph break `\r\n`. The manual marks the end of a field's text with 0x02; on Hancom 13.0.0.3457 a read of one field returned no 0x02 after its text. The command accepts both: one trailing 0x02 is not part of the text;
- `empty` and `readable`;
- `fillable`, with `fill_refusal` when it is not;
- `hash`, which binds the field's name, occurrence, count, kind and full text.

`count` is the number of fields listed or omitted, `listed` the number of rows in `fields`, and `document_field_count` and `names` give the document's totals. At most 1000 rows are listed: further fields are counted in `coverage.rows_omitted`, with `truncated: true`. Field texts are read for the first 1000 fields in document order whose names can be addressed; a later field is listed with `readable: false` and `fill_refusal: "field_census_budget"`. `coverage.complete` is false when any listed or omitted field was not read. With `name` only that name's fields are listed and read, within the same first 1000 fields; the whole list, counts and kinds are still checked.

- **Empty field:** reported as `text: ""` and `empty: true`.
- **Unreadable value:** reported as `readable: false`, with `text` and `empty` null and `fill_refusal` naming why; it is never shown as empty.
- **Unaddressable name:** a name a request cannot give (one holding `{{`, `}}`, a control character or an unpaired surrogate, or longer than 200 characters) cannot be addressed by occurrence; it is listed with `fill_refusal: "field_name_unaddressable"`, but its text is not read.
- **Unnamed field:** the field list does not return fields without a name, so they are not listed.
- **Inconsistent list:** a list that is not text, or whose numbered and counted forms disagree, is refused as `field_census_unusable`.
- **No field API:** if the native field object or one of its methods is missing, the inventory is refused as `native_unavailable`.

Text previews stop at 2000 characters (`text_truncated`).

An inventory refusal comes from reads only: nothing was written and no state check is run. A field read that raises an error holds the session as an unknown outcome.

## 2. Fill one click-here field

Copy `name` and `hash` from the inventory:

```json
{"op":"field_text_exact","name":"성명","expected_hash":"<inventory hash>","new_text":"김철수","confirm_layout":true}
```

**Repeated names.** When a name occurs more than once, also give `occurrence` and `expected_occurrences` as the inventory reports them:

```json
{"op":"field_text_exact","name":"서명","occurrence":2,"expected_occurrences":2,"expected_hash":"<inventory hash>","new_text":"박병삼","confirm_layout":true}
```

The command always writes the field's own `name{{occurrence-1}}`. A bare name would make the official `PutFieldText` fill every field of that name, and a missing field would be ignored without an error. So one fill writes exactly one field, and a repeated name without `occurrence` is refused as `field_occurrence_required`.

**The new text.** `new_text` goes in on one line and keeps tabs. It may be empty, which clears the field, and holds at most 2000 characters, with no other control character and no unpaired surrogate. The backend and the MCP models apply the same rules to `new_text` and to the name (`app/field_rules.py`). The filled text must be in one character format: the field's own, as the official contract describes (`format_source: "field"`), or the one the whole previous text had (`"previous_text"`). A cleared field gives `"none"`. A value equal to the current one returns `changed:false` and `status:"already_set"` with no native call.

**The result.**

- `field`: name, occurrence, native address and kind, with `hash_before` and `hash_after`.
- `result`:
  - `text_before`, and `text_after` read back from the field list after the fill, each cut at 2000 characters with `text_before_truncated` and `text_after_truncated`;
  - `native_calls`;
  - `location`: whether the field stands in the body, a table cell, another object or a header or footer, and its paragraph index;
  - `paragraph_before` and `paragraph_after`, cut the same way, with `paragraph_before_truncated` and `paragraph_after_truncated`;
  - `format_source`;
  - `siblings_kept`.
- `next_binding`: the fields for the next fill of the same field.

## Refusals

Each of these stops a fill before any write, and the document is unchanged:

| Case | Code |
|---|---|
| No field of this name, or `FieldExist` answers false | `field_not_found` |
| Repeated name without `occurrence` | `field_occurrence_required` |
| `expected_occurrences` is not the current count | `field_count_mismatch` |
| The field changed since it was listed | `field_hash_mismatch` |
| A cell or text box field, one name for several kinds, or a name the kind lists do not cover | `field_kind_unsupported` (a cell's whole text: `table_cell_text_exact`) |
| The field holds more than one paragraph | `field_multi_paragraph` |
| The field's text could not be read as one value | `field_text_unreadable` |
| The field is not among the first 1000 the census reads | `field_census_budget` |
| The field's name cannot be addressed by occurrence (an inventory `fill_refusal`; a request cannot give such a name) | `field_name_unaddressable` |
| The field list is not usable, or `FieldExist` does not answer with a BOOL | `field_census_unusable` |
| No field in the document text holds the text the field list reads | `field_text_unprovable` |
| The document text and its paragraph list disagree, so the fill could not be proven | `preservation_unprovable` |
| The caret holds a selection | `selection_unverified` |
| The native field object, `FieldExist` or `PutFieldText` is missing | `native_unavailable` |

A fill refusal gives the caret back and proves the document unchanged. The inventory refuses with `field_census_unusable` or `native_unavailable` (see above). A request that breaks the field rules (name, `new_text`, `occurrence` with `expected_occurrences`, `expected_hash`, `confirm_layout`) is rejected as invalid input before any native call.

## Proof, history and saving

**The proof of a fill.**

- `PutFieldText` is documented as void, so any returned value other than none is an unknown outcome; it is never coerced to success.
- The field list is read again. The filled field must hold the new text, and every other field must read as before, with the same names, counts and kinds. If it does not, the outcome is unknown at once, and no further native call follows.
- The document's own text is compared before and after. Every definition, control, table, header, footer and paragraph is as it was, except that one paragraph holds the new text in place of the old between that one field's begin and end controls, with those controls unchanged, in one character format: the field's own or the whole previous text's. A character format or font that only the old text used may be gone. The controls are compared as that text holds them, with their data; the fill does not walk the live control list, in which Hancom gives a field's end an empty control identifier.
- For a field in a header or footer, that whole area is compared as well: its frame and every paragraph in it, those of its text boxes, tables and other lists included, and the tables in it. Only the field's own paragraph may differ, as described above; it keeps its own settings and may take the new paragraph id Hancom gives an edited paragraph.

Anything else stops as an unknown outcome: the session is held, and nothing is rolled back. So does a native read that raises, before or after the write.

**Undo.** The native Undo of `PutFieldText` is not measured. So a fill is not counted as an Undo step, and a following `undo` is refused with `undo_not_counted` before any native call.

**Saving.** After filling, call `hwpx_save` and keep the saved file it returns. Request `hwpx_proof` and keep the PDF and every page PNG. Then close, reopen the saved file and list the fields again to see the saved values.

**Native qualification.** No fill has been completed against Hancom yet. An actual HWPML2X text of a native-authored document with four click-here fields was captured before any fill (Hancom 13.0.0.3457, `tests/fixtures/native_fields`). Each field is a `FIELDBEGIN` with its own `InstId`, its text and a `FIELDEND`. All the begins and ends carry one shared `FieldId`, and an end has no `InstId`, so a begin pairs with its end only by their order in the text; neither id is used as an identity. In an offline replay of that text and of the field values read on the same open, the inventory lists the four fields, and a fill of one of them passes every check before the write and writes only its own `name{{n}}`. The replay derives from the observed list the list forms and the `FieldExist` answer the native session did not read (`GetFieldList` options 0, 1 and 8, and the counted lists), and uses a fake caret and selection. On Hancom 13.0.0.3457 itself, `field_inventory` of that document listed the four fields with the same texts and hashes as the replay. No fill of an existing value, after state, save, reopen or rendering has been captured natively. If a fill renews any data of the field's own begin or end control, the proof stops it as an unknown outcome until a native before/after pair has measured that.
