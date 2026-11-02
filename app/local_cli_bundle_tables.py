"""Command-bundle table operations for LocalCliService: every read is a fresh one, and a refusal gives back the caret and any selection it found."""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable

from app import native_numbering, native_numbering_census, native_table
from app.local_cli_bundle_numbering import _give_back, _guard_protection, _mutation_error, _no_change_proof, _runtime_error, _strict_target_state
from app.local_cli_runtime import LocalCliRuntimeError


_KEPT_STATE_DETAILS = 64


class LocalCliBundleTablesMixin:
    def _table_page_reader(self, hwp: Any) -> Callable[[], int]:
        return lambda: self._bundle_page_evidence(hwp, numbering_guard=True)['page']

    def _table_read_state(self, hwp: Any) -> Callable[[], dict[str, Any]]:
        def read() -> dict[str, Any]:
            try:
                # One native read for each: the model is taken from the very text the census proved usable.
                try:
                    source = hwp.get_text_file('HWPML2X', '')
                except native_numbering.NativeNumberingRefusal as exc:
                    # SDK exception identity is not a pure model refusal.
                    # Stop before parsing/restoration or another SDK call.
                    raise native_numbering.NativeNumberingReadFailure('Native table evidence read failed.', code='preservation_unprovable') from exc
                raw = _guard_protection(lambda: native_numbering_census.parse_hwpml2x_census(source))
                model = native_table.parse_table_model(source)
            except (LocalCliRuntimeError, native_numbering_census.CensusError) as exc:
                raise native_numbering.NativeNumberingRefusal('Document preservation evidence is unusable before the edit.', code='preservation_unprovable') from exc
            census = native_numbering._validated_census(raw)  # noqa: SLF001
            if census['paragraphs'] is None or census['definitions'] is None:
                raise native_numbering.NativeNumberingRefusal('Document preservation evidence is unusable before the edit.', code='preservation_unprovable')
            try:
                controls = [name for _control, name in native_numbering._control_ids(hwp)]  # noqa: SLF001
            except native_numbering.ControlEnumerationUnproven as exc:
                raise native_numbering.NativeNumberingRefusal('the document controls could not be enumerated', code='control_enumeration_unavailable') from exc
            return {'census': census, 'model': model, 'controls': controls}

        return read

    def _table_caret(self, hwp: Any) -> tuple[Any, Any]:
        return _strict_target_state(hwp)

    def _table_give_back(self, hwp: Any, original: tuple[Any, Any]) -> None:
        _give_back(hwp, original, 'The caret could not be given back after reading a table.')

    def _table_pages_then_back(self, hwp: Any, original: tuple[Any, Any], model: dict[str, Any], *, only: str | None = None) -> dict[str, int | None]:
        """Pages are read by visiting anchors. A refusal found on the way gives the caret back too; a raising read has no next call."""
        try:
            pages = native_table.table_pages(hwp, self._table_page_reader(hwp), only=only, skip=frozenset(item['inst_id'] for item in model['margin_tables']))
        except native_numbering.NativeNumberingReadFailure:
            raise  # Unknown: nothing is given back and no other SDK call follows.
        except native_numbering.NativeNumberingRefusal:
            self._table_give_back(hwp, original)
            raise
        self._table_give_back(hwp, original)
        return pages

    def _remember_table_states(self, result: dict[str, Any]) -> None:
        """What a later history mismatch can name, kept in this process and never in a reply. A few edits' worth: an older one simply cannot be named."""
        details = result.pop('_details', None)
        undo = result.get('undo') or {}
        if not details:
            return
        store = self.__dict__.setdefault('_table_state_details', OrderedDict())
        for sha, detail in zip((undo.get('pre_state_sha256'), undo.get('post_state_sha256')), details):
            if sha and detail is not None:
                store[sha] = detail
                store.move_to_end(sha)
        while len(store) > _KEPT_STATE_DETAILS:
            store.popitem(last=False)

    def _bundle_table_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        try:
            native_table.validate_inventory_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        original = self._table_caret(hwp)
        model = self._table_read_state(hwp)()['model']
        pages = self._table_pages_then_back(hwp, original, model)
        tables = [native_table.public_summary(table, pages.get(table['inst_id'])) for table in model['tables']]
        return {'schema_version': 'local-cli/table-inventory/v1', 'read_only': True, 'count': len(tables), 'tables': tables, 'warnings': []}

    def _bundle_table_read(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        try:
            request = native_table.validate_read_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        original = self._table_caret(hwp)
        model = self._table_read_state(hwp)()['model']
        table = native_table.resolve_table(model, request['table_id'])
        inst = table['inst_id']
        pages = self._table_pages_then_back(hwp, original, model, only=inst)
        return {'schema_version': 'local-cli/table-read/v1', 'read_only': True, **native_table.public_table(table, pages.get(inst), request['max_cells']), 'warnings': []}

    def _table_review_facts(self, hwp: Any, paragraphs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Every table's values for a review, and each paragraph row's table. What this cannot read it says it could not; a read that raises goes on raising, with no next call."""
        try:
            model = native_table.parse_table_model(hwp.get_text_file('HWPML2X', ''))
        except native_numbering_census.CensusError:
            return {'tables': None, 'tables_truncated': False, 'picture_payloads': None}
        native_table.tag_readback(paragraphs, model)
        if not model['tables'] and not model['margin_tables']:
            return {'tables': [], 'tables_truncated': False, 'picture_payloads': model['pictures']}
        facts: dict[str, Any] = {}
        pages: dict[str, int | None] = {}
        if model['tables']:
            try:
                original = native_numbering._read_target_state(hwp)  # noqa: SLF001
            except native_numbering.NativeNumberingReadFailure:
                raise
            except native_numbering.NativeNumberingRefusal:
                original = None
            if original is None:
                facts['tables_pages_reason'] = 'caret_unreadable'
            else:
                try:
                    pages = self._table_pages_then_back(hwp, original, model)
                except native_numbering.NativeNumberingReadFailure:
                    raise  # Unknown, so the review stops here and the session is held.
                except native_numbering.NativeNumberingRefusal as exc:
                    facts['tables_pages_reason'] = exc.code  # Refused with the caret proven back; a failed give-back raises instead.
        rows, truncated = native_table.review_rows(model, pages)
        return {'tables': rows, 'tables_truncated': truncated, 'picture_payloads': model['pictures'], **facts}

    def _table_mutation(self, hwp: Any, step: dict[str, Any], *, validate: Callable[[dict[str, Any]], dict[str, Any]], apply: Callable[..., dict[str, Any]], name: str, on_mutating: Callable[[], None] | None) -> dict[str, Any]:
        try:
            request = validate(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        original = self._table_caret(hwp)
        read = self._table_read_state(hwp)
        before = None

        def restored() -> None:
            # A refusal found the caret and selection somewhere; they go back, and only then is nothing proven changed.
            native_table._run(hwp, 'Cancel')  # noqa: SLF001
            self._table_give_back(hwp, original)
            if _no_change_proof(lambda: read()['census'], 'The refusal could not be proven to have changed nothing.') != before:
                raise LocalCliRuntimeError('The refusal could not be proven to have changed nothing.')

        def settle() -> None:
            # A caret in the body keeps its place. A caret in a cell may have no place after a structure edit, so it goes to the table's own paragraph.
            caret = original[0]
            if caret[0] == 0:
                native_numbering._set_pos(hwp, caret)  # noqa: SLF001
                return
            control = native_table.table_controls(hwp).get(request['table_id'].removeprefix('tbl:')) if 'table_id' in request else None
            if control is not None:
                hwp.set_pos_by_set(control.GetAnchorPos(0))

        try:
            before = read()['census']
            result = apply(hwp, request=request, read=read, page_of=self._table_page_reader(hwp), restore=settle, on_mutating=on_mutating)
        except native_numbering.NativeNumberingMutationError as exc:
            raise _mutation_error(exc) from exc
        except native_numbering.NativeNumberingReadFailure:
            raise  # Unknown: the service holds, and neither restoration nor any other SDK call follows.
        except native_numbering.NativeNumberingRefusal:
            if before is not None:
                restored()
            raise
        if result['result']['changed'] is False:
            restored()
        self._remember_table_states(result)
        told = result.get('after') or result.get('table') or {}
        warnings = native_table.layout_warnings(told.get('table_id') or '', told.get('page'), told.get('layout') or {})
        return {'schema_version': f'local-cli/{name}/v1', 'read_only': False, 'mutation': name, **result, 'warnings': [native_table.RENDER_PROOF_WARNING, *warnings]}

    def _bundle_table_edit_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        return self._table_mutation(hwp, step, validate=native_table.validate_edit_step, apply=native_table.apply_edit, name='table-edit-exact', on_mutating=on_mutating)

    def _bundle_table_style_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        return self._table_mutation(hwp, step, validate=native_table.validate_style_step, apply=native_table.apply_style, name='table-style-exact', on_mutating=on_mutating)

    def _bundle_table_width_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        return self._table_mutation(hwp, step, validate=native_table.validate_width_step, apply=native_table.apply_width, name='table-width-exact', on_mutating=on_mutating)

    def _bundle_table_create_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        return self._table_mutation(hwp, step, validate=native_table.validate_create_step, apply=native_table.apply_create, name='table-create-exact', on_mutating=on_mutating)
