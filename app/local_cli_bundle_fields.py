"""Command-bundle native named fields for LocalCliService: the inventory by name and occurrence, and filling one existing click-here field.

Every read is a fresh one. The inventory only reads the field list and field texts; its refusal writes nothing and runs
no state check. A fill refusal gives back the caret and any selection it found, and proves the document unchanged.
"""
from __future__ import annotations

from typing import Any, Callable

from app import native_fields, native_format, native_header_footer, native_numbering, native_numbering_census, native_table
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


class LocalCliBundleFieldsMixin:
    def _fields_read_state(self, hwp: Any) -> Callable[[], dict[str, Any]]:
        def read() -> dict[str, Any]:
            try:
                # One native read: the census, the character model, the tables and the headers and footers all come from the very same text.
                source = native_numbering._sdk_read(lambda: hwp.get_text_file('HWPML2X', ''))  # noqa: SLF001
                raw = _guard_protection(lambda: native_numbering_census.parse_hwpml2x_census(source))
                model = native_format.parse_format_model(source)
                tables = native_table.parse_table_model(source)
                areas = native_header_footer.parse_areas(source, paragraph_digests=True)
            except (LocalCliRuntimeError, native_numbering_census.CensusError) as exc:
                raise native_numbering.NativeNumberingReadFailure('Document preservation evidence is unusable; no further native call is permitted.', code='preservation_unprovable') from exc
            census = native_numbering._validated_census(raw)  # noqa: SLF001
            if census['paragraphs'] is None or census['definitions'] is None:
                raise native_numbering.NativeNumberingReadFailure('Document preservation evidence is unusable; no further native call is permitted.', code='preservation_unprovable')
            return {'census': census, 'model': model, 'tables': tables, 'areas': areas}

        return read

    def _bundle_field_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        """Every existing named field with its occurrence, kind, text and hash: reads of the field list and field texts only."""
        try:
            request = native_fields.validate_inventory_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        return {'schema_version': 'local-cli/field-inventory/v1', 'read_only': True, **native_fields.inventory(hwp, request), 'warnings': []}

    def _bundle_field_text_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        """One existing click-here field, bound by name, occurrence and hash, filled with new text by one PutFieldText of its own occurrence."""
        message = 'Field fill refusal restoration is unproven.'
        try:
            request = native_fields.validate_fill_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        original = _strict_target_state(hwp)
        read = self._fields_read_state(hwp)

        def restored() -> None:
            _give_back(hwp, original, message)
            if _no_change_proof(lambda: read()['census'], message) != before:
                raise LocalCliRuntimeError(message)

        before = None
        try:
            before = read()['census']
            result = native_fields.apply_fill(hwp, request=request, read=read, on_mutating=on_mutating)
        except native_numbering.NativeNumberingMutationError as exc:
            raise _mutation_error(exc) from exc
        except native_numbering.NativeNumberingReadFailure:
            raise  # Unknown: the service holds, and neither restoration nor any other SDK call follows.
        except native_numbering.NativeNumberingRefusal:
            if before is not None:
                restored()
            raise
        return {'schema_version': 'local-cli/field-text-exact/v1', 'read_only': False, 'mutation': 'field-text-exact', **result, 'warnings': [_RENDER_PROOF_WARNING]}
