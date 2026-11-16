# Exact textbox body-caret navigation

`text_container_body_caret` is an additive guarded command-bundle operation, exposed through `hwpx_command`, `hwpx_bundle`, and the existing `/local-cli/command-bundle` backend. It is not an arbitrary SDK/coordinate transport.

Use only a fresh inventory identity/hash and a complete `text_container_read` result:

- `op`: `text_container_body_caret`
- `target_id`: the exact opaque textbox identity
- `expected_hash`: its full current binding hash
- `expected_text`: complete exact current text, not a preview
- `confirm_navigation`: exact JSON boolean `true`

Only an eligible editable body-owned textbox and an already unselected caret are accepted. The caret may initially be inside that textbox. Captions, footnotes, unsupported owners, selections, stale/ambiguous bindings, malformed values and SDK faults refuse without a fallback.

The backend reuses the complete native owner census and independent `GetAnchorPos(0)` coordinates. It verifies that this observed native anchor is the exact uniquely bound body owner and calls the existing typed `SetPos` leaf once. It never derives a destination from caller offsets, searches for substitute text, cancels a selection, selects another object or rewrites XML. It immediately proves the exact native position, selection/mode and complete document preservation, and revalidates the target/hash/full text and native identity before success.

Successful data uses `local-cli/text-containers/v1` with:

- `anchor_basis`: `native_GetAnchorPos_exact_owner_census`
- `body_anchor`: three exact nonnegative integer LONGs with listID 0
- `cursor`: the same position, `has_selection=false`, `selected_pos=[]`
- `container`: complete unchanged target/hash/text
- `result`: `changed=true`, `status=positioned`, `navigation_only=true`, `content_changed=false`
- `preservation`: `complete_document_unchanged_navigation_only`

`changed` and dirty intent denote navigation/serialized-caret save debt, not a claim of content mutation. A later read-only bundle's dirty flag cannot discharge a caller's navigation/edit debt. Preserve session custody and positively verified save outcomes before any close.

Before dependent editing, immediately run an existing guarded bundle containing `where` followed by `text_container_read` of the same target/hash. The read operation selects the established guarded lane so `where` produces its strict native cursor schema, not a legacy context snapshot. Require the exact returned body anchor and unselected position, then full target/hash/text agreement. Stop on the first failed or malformed result; no retry, alternate selector, restore or close follows it. The existing guarded `where -> text_container_text_exact -> where` edit and complete saved-package audit remain mandatory.

A limited native observation covers this navigation for one existing singleton inline horizontal 40 × 20 mm text box, together with inventory, complete read, whole-text replacement changing one digit on one line without changing its length, and ordinary Save; its scope and limits are stated under [Qualification and persistence](NATIVE_TEXT_CONTAINERS.md#qualification-and-persistence). That observation does not establish full native qualification of this operation: the returned container keeps `native_qualification: not_run`, and the operation's history policy refuses Undo. Portable source/serializer/MCP/model tests do not establish real Hancom navigation success or a native body anchor for any particular saved input. The complete preservation criteria and native identity/no-selection guards remain unchanged.
