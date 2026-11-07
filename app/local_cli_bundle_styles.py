"""Command-bundle named styles for LocalCliService: the inventory, applying a style by name, creating one and changing one.

Every read is a fresh one. The inventory only reads the document text; its refusal writes nothing and runs no state check.
A refusal of an edit proves the document unchanged by reading it again, and once the edit has placed the caret or selected a
block it first gives back the caret and any selection it found. A technical fault from that point on is not a refusal: it is
raised as an unknown outcome, and nothing is given back or read again (native_styles, the native boundary). So is, at any
point, a document text that is not a str, is empty or is not well-formed XML (native_styles.document_text), and a native
capability that cannot be read.
"""
from __future__ import annotations

from typing import Any, Callable

from app import native_format, native_numbering, native_numbering_census, native_styles
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

_UNUSABLE = 'Document preservation evidence is unusable; nothing was written.'


class LocalCliBundleStylesMixin:
    def _styles_read_state(self, hwp: Any) -> Callable[[], dict[str, Any]]:
        def read() -> dict[str, Any]:
            # One native read: the census, the character model and the style model all come from the very same text.
            source = native_styles.document_text(native_numbering._sdk_read(lambda: hwp.get_text_file('HWPML2X', '')))  # noqa: SLF001
            try:
                styles = native_styles.parse_style_model(source)
            except native_styles.StyleModelUnusable as exc:
                raise native_numbering.NativeNumberingRefusal(f'The style list or a paragraph style is not usable ({exc}).', code='style_model_unusable') from exc
            try:
                raw = _guard_protection(lambda: native_numbering_census.parse_hwpml2x_census(source))
                model = native_format.parse_format_model(source)
            except (LocalCliRuntimeError, native_numbering_census.CensusError) as exc:
                raise native_numbering.NativeNumberingRefusal(_UNUSABLE, code='preservation_unprovable') from exc
            census = native_numbering._validated_census(raw)  # noqa: SLF001
            if census['paragraphs'] is None or census['definitions'] is None:
                raise native_numbering.NativeNumberingRefusal(_UNUSABLE, code='preservation_unprovable')
            texts = [item['text'] for item in census['paragraphs']]
            if texts != [item['text'] for item in model['paragraphs']] or texts != [item['text'] for item in styles['paragraphs'] if item['area'] is None]:
                # The proof compares these lists by one index: they must be the same list.
                raise native_numbering.NativeNumberingRefusal('The document text and its paragraph lists disagree; nothing was written.', code='preservation_unprovable')
            return {'census': census, 'model': model, 'styles': styles}

        return read

    def _bundle_style_inventory(self, hwp: Any, step: dict[str, Any]) -> dict[str, Any]:
        """Every named style with its identity, kind, definitions, hash and use: one read of the document text."""
        try:
            request = native_styles.validate_inventory_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        source = native_styles.document_text(native_numbering._sdk_read(lambda: hwp.get_text_file('HWPML2X', '')))  # noqa: SLF001
        try:
            model = native_styles.parse_style_model(source)
        except native_styles.StyleModelUnusable as exc:
            raise native_numbering.NativeNumberingRefusal(f'The style list or a paragraph style is not usable ({exc}).', code='style_model_unusable') from exc
        return {'schema_version': 'local-cli/style-inventory/v1', 'read_only': True, **native_styles.inventory(model, request), 'warnings': []}

    def _style_edit(self, hwp: Any, step: dict[str, Any], *, validate: Callable[[dict[str, Any]], dict[str, Any]],
                    edit: Callable[..., dict[str, Any]], schema: str, mutation: str, on_mutating: Callable[[], None] | None) -> dict[str, Any]:
        message = 'Style edit refusal restoration is unproven.'
        try:
            request = validate(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc
        original = _strict_target_state(hwp)
        read = self._styles_read_state(hwp)
        progress = {'entered': False}

        def restored() -> None:
            if progress['entered']:
                _give_back(hwp, original, message)
            if _no_change_proof(lambda: read()['census'], message) != before:
                raise LocalCliRuntimeError(message)

        before = None
        try:
            before = read()['census']
            result = edit(request=request, read=read, restore=original[0], on_mutating=on_mutating, progress=progress)
        except native_numbering.NativeNumberingMutationError as exc:
            error = _mutation_error(exc)
            # The proof's bounded facts, measured before the edit was held (native_styles._not_as_requested); the service checks them.
            proof = vars(exc).get('style_proof')
            if type(proof) is dict:
                error.style_proof = proof
            raise error from exc
        except native_numbering.NativeNumberingReadFailure:
            raise  # Unknown: the service holds, and neither restoration nor any other SDK call follows.
        except native_numbering.NativeNumberingRefusal:
            if before is not None:
                restored()
            raise
        if result['result']['changed'] is False:
            restored()
        return {'schema_version': schema, 'read_only': False, 'mutation': mutation, **result, 'warnings': [_RENDER_PROOF_WARNING]}

    def _bundle_style_apply_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        """One existing style, bound by name and hash, applied to an exact paragraph or range by one Style action."""
        return self._style_edit(
            hwp, step, validate=native_styles.validate_apply_step,
            edit=lambda **kw: native_styles.apply_style(hwp, page_of=lambda: self._bundle_page_evidence(hwp, numbering_guard=True)['page'], **kw),
            schema='local-cli/style-apply-exact/v1', mutation='style-apply-exact', on_mutating=on_mutating)

    def _bundle_style_create_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        """One new paragraph style from an existing paragraph style's definitions and the requested character properties, by one StyleAddNew."""
        return self._style_edit(hwp, step, validate=native_styles.validate_create_step, edit=lambda **kw: native_styles.create_style(hwp, **kw),
                                schema='local-cli/style-create-exact/v1', mutation='style-create-exact', on_mutating=on_mutating)

    def _bundle_style_update_exact(self, hwp: Any, step: dict[str, Any], on_mutating: Callable[[], None] | None = None) -> dict[str, Any]:
        """One existing paragraph style's character properties changed, with every paragraph using it bound, by one StyleDirectEdit."""
        return self._style_edit(hwp, step, validate=native_styles.validate_update_step, edit=lambda **kw: native_styles.update_style(hwp, **kw),
                                schema='local-cli/style-update-exact/v1', mutation='style-update-exact', on_mutating=on_mutating)
