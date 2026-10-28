"""Command-bundle page-number and official-numbering operations for LocalCliService."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app import native_numbering, native_numbering_census
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError

_RENDER_PROOF_WARNING = (
    'Rendered proof of every page is required before accepting the working copy; '
    'this operation only reports native call results.'
)


def _runtime_error(exc: Exception) -> LocalCliRuntimeError:
    return LocalCliRuntimeError(str(exc))


def _mutation_error(exc: native_numbering.NativeNumberingMutationError) -> LocalCliMutationError:
    return LocalCliMutationError(
        str(exc),
        mutation_may_have_persisted=bool(exc.mutation_may_have_persisted),
        rollback=dict(exc.rollback),
    )


_NUMBERING_OPS = frozenset({'page_number_apply', 'official_numbering_apply'})


class LocalCliBundleNumberingMixin:
    """Command-bundle page-number and official-numbering operations for LocalCliService."""

    def _hold_unknown_numbering(self, handle: Any, step: dict[str, Any], exc: BaseException) -> bool:
        """Ask the owning runtime to retain its session after a numbering mutator was reached with an unknown outcome.

        Returns False when the step is not a numbering step or no runtime owner offers a hold, in which case the
        caller keeps its previous behavior (an ordinary error is reported; a control-flow signal propagates).
        """
        if step.get('op') not in _NUMBERING_OPS:
            return False
        hold = getattr(handle, 'hold_native_custody', None)
        if not callable(hold):
            return False
        try:
            hold(
                'Numbering reached a native mutating call and ended noncomplete; '
                'its outcome is unknown, so the live session is held and not closed, retried or recovered.',
                exc,
            )
        except Exception:
            return False
        return True

    def _numbering_protection_reader(self, handle: Any) -> Callable[[], dict[str, Any]]:
        """Zero-argument reader of the live document's in-memory HWPML2X census; it never saves or rebinds the document."""
        return native_numbering_census.live_protection_reader(handle.hwp)

    def _bundle_page_number_apply(
        self, hwp: Any, step: dict[str, Any], protection: Callable[[], Any] | None = None, on_mutating: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        try:
            request = native_numbering.validate_page_number_step(step)
            result = native_numbering.apply_page_number(hwp, request, protection=protection, on_mutating=on_mutating)
        except native_numbering.NativeNumberingMutationError as exc:
            raise _mutation_error(exc) from exc
        except native_numbering.EditOperationError as exc:
            raise _runtime_error(exc) from exc
        return {
            'schema_version': 'local-cli/page-number-apply/v1',
            'read_only': False,
            'mutation': 'page-number-apply',
            'result': result,
            'warnings': [_RENDER_PROOF_WARNING, *result.get('warnings', [])],
        }

    def _bundle_official_numbering_apply(
        self, hwp: Any, step: dict[str, Any], protection: Callable[[], Any] | None = None, on_mutating: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        try:
            validated = native_numbering.validate_official_numbering_step(step)
        except native_numbering.NativeNumberingInputError as exc:
            raise _runtime_error(exc) from exc

        original_pos = native_numbering._get_pos(hwp)  # noqa: SLF001
        restore_caret = True
        def issued() -> None:
            nonlocal restore_caret
            restore_caret = False
            if on_mutating is not None:
                on_mutating()
        try:
            target = self._find_text_on_expected_page(
                hwp,
                match=validated['match'],
                expected_page=validated['expected_page'],
            )
            if target.get('page') is None:
                raise LocalCliRuntimeError('official_numbering_apply cannot prove the target page')
            try:
                result = native_numbering.apply_official_numbering_at_match(
                    hwp,
                    find=validated['match'],
                    level=validated['level'],
                    protection=protection,
                    on_mutating=issued,
                )
            except native_numbering.NativeNumberingMutationError as exc:
                raise _mutation_error(exc) from exc
            except native_numbering.EditOperationError as exc:
                raise _runtime_error(exc) from exc
            restore_caret = True  # only a complete successful native result
        finally:
            if restore_caret and original_pos is not None:
                try:
                    native_numbering._set_pos(hwp, original_pos)  # noqa: SLF001
                except Exception:
                    pass
        return {
            'schema_version': 'local-cli/official-numbering-apply/v1',
            'read_only': False,
            'mutation': 'official-numbering-apply',
            'match': validated['match'],
            'expected_page': validated['expected_page'],
            'level': validated['level'],
            'target': {'page': target.get('page'), 'finder': target},
            'result': result,
            'numbering_format_enforced': result['numbering_format_enforced'],
            'numbering_format': result['numbering_format'],
            'warnings': [_RENDER_PROOF_WARNING],
        }
