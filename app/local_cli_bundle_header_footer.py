"""Command-bundle headers and footers for LocalCliService: the inventory, and exact text and format edits of one existing area.

Every read is a fresh one; a refusal gives back the caret and any selection it found, and proves the document unchanged.
"""
from __future__ import annotations

from typing import Any, Callable

from app import native_format, native_header_footer, native_numbering, native_numbering_census
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


class LocalCliBundleHeaderFooterMixin:
    def _header_footer_read_state(self, hwp: Any) -> Callable[[], dict[str, Any]]:
        def read() -> dict[str, Any]:
            try:
                # One native read: the census, the body format model and the areas all come from the very same text.
                source = native_numbering._sdk_read(lambda: hwp.get_text_file('HWPML2X', ''))  # noqa: SLF001
                raw = _guard_protection(lambda: native_numbering_census.parse_hwpml2x_census(source))
                model = native_format.parse_format_model(source)
                areas = native_header_footer.parse_areas(source)
            except (LocalCliRuntimeError, native_numbering_census.CensusError) as exc:
                raise native_numbering.NativeNumberingRefusal('Document preservation evidence is unusable before the edit.', code='preservation_unprovable') from exc
            census = native_numbering._validated_census(raw)  # noqa: SLF001
            if census['paragraphs'] is None or census['definitions'] is None:
                raise native_numbering.NativeNumberingRefusal('Document preservation evidence is unusable before the edit.', code='preservation_unprovable')
            return {'census': census, 'model': model, 'areas': areas}

        return read

    def _bundle_header_footer_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        """Every existing header and footer with its section, page scope, text, format, fields and hash: a read of the document text only."""
        try:
            native_header_footer.validate_inventory_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        try:
            source = native_numbering._sdk_read(lambda: hwp.get_text_file('HWPML2X', ''))  # noqa: SLF001
            model = native_header_footer.parse_areas(source)
        except native_numbering_census.CensusError as exc:
            raise native_numbering.NativeNumberingRefusal('The document text could not be read as headers and footers.', code='preservation_unprovable') from exc
        return {'schema_version': 'local-cli/header-footer-inventory/v1', 'read_only': True, **native_header_footer.inventory(model), 'warnings': []}

    def _header_footer_mutation(self, hwp: Any, step: dict[str, Any], *, validate: Callable[[dict[str, Any]], dict[str, Any]], apply: Callable[..., dict[str, Any]],
                                name: str, on_mutating: Callable[[], None] | None) -> dict[str, Any]:
        message = f'{name} refusal restoration is unproven.'
        try:
            request = validate(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        original = _strict_target_state(hwp)
        read = self._header_footer_read_state(hwp)

        def restored() -> None:
            _give_back(hwp, original, message)
            if _no_change_proof(lambda: read()['census'], message) != before:
                raise LocalCliRuntimeError(message)

        before = None
        try:
            before = read()['census']
            result = apply(hwp, request=request, read=read, on_mutating=on_mutating)
        except native_numbering.NativeNumberingMutationError as exc:
            raise _mutation_error(exc) from exc
        except native_numbering.NativeNumberingReadFailure:
            raise  # Unknown: the service holds, and neither restoration nor any other SDK call follows.
        except native_numbering.NativeNumberingRefusal:
            if before is not None:
                restored()
            raise
        return {'schema_version': f'local-cli/{name}/v1', 'read_only': False, 'mutation': name, **result, 'warnings': [_RENDER_PROOF_WARNING]}

    def _bundle_header_footer_text_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        """One exact range of the text of one existing header or footer, replaced in the format of the replaced text."""
        return self._header_footer_mutation(hwp, step, validate=native_header_footer.validate_text_step, apply=native_header_footer.apply_text,
                                            name='header-footer-text-exact', on_mutating=on_mutating)

    def _bundle_header_footer_format_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        """Character format of exact text and paragraph format of its paragraph inside one existing header or footer."""
        return self._header_footer_mutation(hwp, step, validate=native_header_footer.validate_format_step, apply=native_header_footer.apply_format,
                                            name='header-footer-format-exact', on_mutating=on_mutating)
