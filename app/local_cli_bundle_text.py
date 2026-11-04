"""Command-bundle text replacement and table cell writes for LocalCliService: every read is a fresh one, and a refusal gives back the caret and any selection it found."""
from __future__ import annotations

from typing import Any, Callable

from app import native_format, native_numbering, native_numbering_census, native_table, native_text_edit
from app.local_cli_bundle_numbering import (
    _RENDER_PROOF_WARNING,
    _give_back,
    _guard_protection,
    _mutation_error,
    _no_change_proof,
    _runtime_error,
    _strict_target_state,
)
from app.local_cli_runtime import LocalCliRuntimeError


class LocalCliBundleTextMixin:
    def _text_read_state(self, hwp: Any) -> Callable[[], dict[str, Any]]:
        def read() -> dict[str, Any]:
            try:
                # One native read: the census, the character model and the table model all come from the very same text.
                source = native_numbering._sdk_read(lambda: hwp.get_text_file('HWPML2X', ''))  # noqa: SLF001
                raw = _guard_protection(lambda: native_numbering_census.parse_hwpml2x_census(source))
                model = native_format.parse_format_model(source)
                tables = native_table.parse_table_model(source)
            except (LocalCliRuntimeError, native_numbering_census.CensusError) as exc:
                raise native_numbering.NativeNumberingRefusal('Document preservation evidence is unusable before the edit.', code='preservation_unprovable') from exc
            census = native_numbering._validated_census(raw)  # noqa: SLF001
            if census['paragraphs'] is None or census['definitions'] is None:
                raise native_numbering.NativeNumberingRefusal('Document preservation evidence is unusable before the edit.', code='preservation_unprovable')
            return {'census': census, 'model': model, 'tables': tables}

        return read

    def _bundle_text_replace_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        """One exact range of existing text, found again from the document and replaced in the format of the replaced text."""
        try:
            request = native_text_edit.validate_replace_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        original = _strict_target_state(hwp)
        read = self._text_read_state(hwp)

        def restored() -> None:
            _give_back(hwp, original, 'Text replacement refusal restoration is unproven.')
            if _no_change_proof(lambda: read()['census'], 'Text replacement refusal restoration is unproven.') != before:
                raise LocalCliRuntimeError('Text replacement refusal restoration is unproven.')

        before = None
        try:
            before = read()['census']
            result = native_text_edit.apply_replace(hwp, request=request, read=read, on_mutating=on_mutating,
                                                    page_of=lambda: self._bundle_page_evidence(hwp, numbering_guard=True)['page'])
        except native_numbering.NativeNumberingMutationError as exc:
            raise _mutation_error(exc) from exc
        except native_numbering.NativeNumberingReadFailure:
            raise  # Unknown: the service holds, and neither restoration nor any other SDK call follows.
        except native_numbering.NativeNumberingRefusal:
            if before is not None:
                restored()
            raise
        return {'schema_version': 'local-cli/text-replace-exact/v1', 'read_only': False, 'mutation': 'text-replace-exact', **result, 'warnings': [_RENDER_PROOF_WARNING]}

    def _bundle_table_cell_text_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        """The text of whole cells of one bound table, each cell proven before the first write."""
        return self._table_mutation(hwp, step, validate=native_text_edit.validate_cell_text_step, apply=native_text_edit.apply_cell_text,
                                    name='table-cell-text-exact', on_mutating=on_mutating)
