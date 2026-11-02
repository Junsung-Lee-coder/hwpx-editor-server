"""Command-bundle table operations for LocalCliService: every read is a fresh one, and a refusal gives back the caret and any selection it found."""
from __future__ import annotations

from typing import Any, Callable

from app import native_numbering, native_numbering_census, native_table
from app.local_cli_bundle_numbering import _guard_protection, _mutation_error, _runtime_error
from app.local_cli_runtime import LocalCliRuntimeError


class LocalCliBundleTablesMixin:
    def _table_page_reader(self, hwp: Any) -> Callable[[], int]:
        return lambda: self._bundle_page_evidence(hwp, numbering_guard=True)['page']

    def _table_read_state(self, hwp: Any) -> Callable[[], dict[str, Any]]:
        def read() -> dict[str, Any]:
            try:
                # One native read for each: the model is taken from the very text the census proved usable.
                source = hwp.get_text_file('HWPML2X', '')
                raw = _guard_protection(lambda: native_numbering_census.parse_hwpml2x_census(source))
                model = native_table.parse_table_model(source)
            except (LocalCliRuntimeError, native_numbering_census.CensusError) as exc:
                raise native_numbering.NativeNumberingRefusal('Document preservation evidence is unusable before the edit.', code='preservation_unprovable') from exc
            census = native_numbering._validated_census(raw)  # noqa: SLF001
            if census['paragraphs'] is None or census['definitions'] is None:
                raise native_numbering.NativeNumberingRefusal('Document preservation evidence is unusable before the edit.', code='preservation_unprovable')
            return {'census': census, 'model': model, 'controls': [name for _control, name in native_numbering._control_ids(hwp)]}  # noqa: SLF001

        return read

    def _table_caret(self, hwp: Any) -> tuple[Any, Any]:
        original = native_numbering._read_target_state(hwp)  # noqa: SLF001
        if original is None:
            raise native_numbering.NativeNumberingRefusal('The caret or selection could not be read as exact positions.', code='target_unlocatable')
        return original

    def _table_give_back(self, hwp: Any, original: tuple[Any, Any]) -> None:
        caret, selection = original
        native_numbering._set_pos(hwp, caret)  # noqa: SLF001
        if selection:
            if selection[0] != selection[3]:
                raise LocalCliRuntimeError('Cross-list selection restoration is unproven.')
            native_numbering._require_motion_result(hwp.select_text(selection[1], selection[2], selection[4], selection[5], selection[0]))  # noqa: SLF001
        if native_numbering._read_target_state(hwp) != original:  # noqa: SLF001
            raise LocalCliRuntimeError('The caret could not be given back after reading a table.')

    def _bundle_table_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        try:
            native_table.validate_inventory_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        original = self._table_caret(hwp)
        model = self._table_read_state(hwp)()['model']
        pages = native_table.table_pages(hwp, self._table_page_reader(hwp))
        self._table_give_back(hwp, original)
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
        pages = native_table.table_pages(hwp, self._table_page_reader(hwp), only=inst)
        self._table_give_back(hwp, original)
        return {'schema_version': 'local-cli/table-read/v1', 'read_only': True, **native_table.public_table(table, pages.get(inst), request['max_cells']), 'warnings': []}

    def _table_review_facts(self, hwp: Any) -> dict[str, Any]:
        """Every table's values for a review. A document this cannot read as tables says so and stays readable; the caret goes back."""
        try:
            model = native_table.parse_table_model(hwp.get_text_file('HWPML2X', ''))
        except native_numbering_census.CensusError:
            return {'tables': None, 'tables_truncated': False}
        if not model['tables']:
            return {'tables': [], 'tables_truncated': False}
        original = native_numbering._read_target_state(hwp)  # noqa: SLF001
        pages = native_table.table_pages(hwp, self._table_page_reader(hwp)) if original is not None else {}
        if original is not None:
            self._table_give_back(hwp, original)
        rows, truncated = native_table.review_rows(model, pages)
        return {'tables': rows, 'tables_truncated': truncated}

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
            if read()['census'] != before:
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
        return {'schema_version': f'local-cli/{name}/v1', 'read_only': False, 'mutation': name, **result, 'warnings': [native_table.RENDER_PROOF_WARNING]}

    def _bundle_table_edit_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        return self._table_mutation(hwp, step, validate=native_table.validate_edit_step, apply=native_table.apply_edit, name='table-edit-exact', on_mutating=on_mutating)

    def _bundle_table_style_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        return self._table_mutation(hwp, step, validate=native_table.validate_style_step, apply=native_table.apply_style, name='table-style-exact', on_mutating=on_mutating)

    def _bundle_table_create_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        return self._table_mutation(hwp, step, validate=native_table.validate_create_step, apply=native_table.apply_create, name='table-create-exact', on_mutating=on_mutating)
