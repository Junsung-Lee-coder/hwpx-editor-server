"""Command-bundle page-number and official-numbering operations for LocalCliService."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app import native_numbering, native_numbering_census
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_bundle_paragraphs import _NumberingTargetNotFound
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


def _plain_proof(value: Any, depth: int = 0) -> Any:
    """Copy exact primitives before any census lookup/equality can call foreign code."""
    if depth > 64:
        raise LocalCliRuntimeError('Numbering proof nesting is unproven.')
    kind = type(value)
    if value is None or kind is str or kind is int or kind is bool:
        return value
    if kind is dict:
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise LocalCliRuntimeError('Numbering proof key is unproven.')
            result[key] = _plain_proof(item, depth + 1)
        return result
    if kind is list:
        return [_plain_proof(item, depth + 1) for item in value]
    if kind is tuple:
        return tuple(_plain_proof(item, depth + 1) for item in value)
    raise LocalCliRuntimeError('Numbering proof primitive is unproven.')


class _NumberingSemanticRefusal(LocalCliRuntimeError):
    """Issued only after trusted semantic branch and complete restored-state proof."""


class _NumberingPreEditHold(LocalCliRuntimeError):
    """Trusted pre-navigation returned census is incomplete; custody is not issued-edit debt."""


def _guard_protection(protection: Callable[[], Any] | None,
                      on_incomplete: Callable[[], None] | None = None) -> dict[str, Any]:
    if protection is None:
        raise LocalCliRuntimeError('Numbering preservation reader is unavailable.')
    raw = _plain_proof(protection())
    required = {'header_footer', 'body_sha256', 'styles_sha256', 'numberings_sha256',
                'bindata_sha256', 'media_sha256', 'page_number_count', 'paragraphs',
                'definitions', 'frame', 'outline_shapes', 'page_restarts', 'page_restart_positions'}
    if type(raw) is not dict or set(raw) != required:
        raise LocalCliRuntimeError('Numbering preservation fields are incomplete.')
    census = native_numbering._validated_census(raw)  # noqa: SLF001
    if (not native_numbering._official_text_evidence_complete(census)  # noqa: SLF001
            or census['page_restarts'] is None or census['page_restart_positions'] is None):
        if on_incomplete is not None:
            on_incomplete()  # Only after the callback returned and exact data validated.
        raise LocalCliRuntimeError('Numbering preservation evidence is incomplete.')
    for paragraph in census['paragraphs']:
        if paragraph['section'] < 0 or paragraph['level'] < 0:
            raise LocalCliRuntimeError('Numbering paragraph state is unproven.')
    return raw


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

        phase, reason = 'preflight', 'preservation_unproven'
        issued_mutation = False
        guard_verified = False
        semantic_refused = False
        preedit_incomplete = False

        def incomplete() -> None:
            nonlocal preedit_incomplete
            if phase == 'preflight':
                preedit_incomplete = True

        def semantic_refusal() -> None:
            nonlocal semantic_refused
            semantic_refused = True

        def checked_protection() -> dict[str, Any]:
            return _guard_protection(protection, incomplete)

        def issued() -> None:
            nonlocal issued_mutation
            issued_mutation = True  # before the callback and actual native Execute
            if on_mutating is not None:
                on_mutating()

        def restore_proven_refusal() -> None:
            nonlocal phase, reason
            if not native_numbering._official_text_evidence_complete(before):  # noqa: SLF001
                raise LocalCliRuntimeError('Numbering finder preservation evidence is incomplete.')
            phase, reason = 'restore', 'restore_unproven'
            caret, selection = original
            native_numbering._set_pos(hwp, caret)  # noqa: SLF001
            if selection:
                if selection[0] != selection[3]:
                    raise LocalCliRuntimeError('Cross-list selection restoration is unproven.')
                native_numbering._require_motion_result(  # noqa: SLF001
                    hwp.select_text(selection[1], selection[2], selection[4], selection[5], selection[0]),
                )
            if native_numbering._read_target_state(hwp) != original:  # noqa: SLF001
                raise LocalCliRuntimeError('Numbering target restoration is unproven.')
            phase, reason = 'preservation', 'preservation_unproven'
            after = native_numbering._read_protection(checked_protection)  # noqa: SLF001
            if after != before:
                raise LocalCliRuntimeError('Numbering finder preservation is unproven.')
            text_after = native_numbering._read_text_hash(hwp)  # noqa: SLF001
            if text_after != text_before:
                raise LocalCliRuntimeError('Numbering finder text preservation is unproven.')
            controls_after = native_numbering._control_ids(hwp)  # noqa: SLF001
            if [c[1] for c in controls_after] != [c[1] for c in controls_before]:
                raise LocalCliRuntimeError('Numbering finder controls are unproven.')
            if native_numbering._read_target_state(hwp) != original:  # noqa: SLF001
                raise LocalCliRuntimeError('Numbering finder preservation is unproven.')

        try:
            original = native_numbering._read_target_state(hwp)  # noqa: SLF001
            if original is None:
                raise LocalCliRuntimeError('Numbering target state is unproven.')
            if original[1] and original[1][0] != original[1][3]:
                raise LocalCliRuntimeError('Cross-list selection restoration is unproven.')
            before = native_numbering._read_protection(checked_protection)  # noqa: SLF001
            if before is None:
                raise LocalCliRuntimeError('Numbering preservation evidence is incomplete.')
            native_numbering._require_complete(before)  # noqa: SLF001
            text_before, controls_before = native_numbering._preflight_census(hwp)  # noqa: SLF001
            phase, reason = 'pre_target', 'finder_failed'
            try:
                target = self._find_text_on_expected_page(
                    hwp, match=validated['match'], expected_page=validated['expected_page'], numbering_guard=True,
                )
            except _NumberingTargetNotFound as refusal:
                # Only the strict finder's exact-False exit is eligible. No
                # generic LocalCliRuntimeError/message-to-refusal conversion.
                if type(refusal) is not _NumberingTargetNotFound:
                    raise
                restore_proven_refusal()
                # This exact existing type is emitted only after positive
                # restoration/preservation; service never treats edited=False
                # or an arbitrary error's metadata as no-effect evidence.
                guard_verified = True
                raise native_numbering.NativeNumberingRefusal(
                    'No target on the expected page; original state and preservation verified.', code='target_not_found',
                ) from None
            target = _plain_proof(target)
            if (type(target) is not dict or type(target.get('page')) is not int
                    or target['page'] != validated['expected_page']):
                raise LocalCliRuntimeError('Numbering target page is unproven.')
            phase, reason = 'native', 'native_failed'
            try:
                result = native_numbering.apply_official_numbering_at_match(
                    hwp, find=validated['match'], level=validated['level'], protection=checked_protection, on_mutating=issued,
                    on_semantic_refusal=semantic_refusal,
                )
            except native_numbering.NativeNumberingRefusal as refusal:
                # Only the source's pure validated semantic branch sets this
                # hook, immediately before raising; SDK exceptions cannot do so.
                if not semantic_refused or issued_mutation or type(refusal) is not native_numbering.NativeNumberingRefusal:
                    raise
                restore_proven_refusal()
                # Preserve the mixin's public RuntimeError ABI and original
                # final caret-set boundary, with no untrusted exception data.
                if not original[1]:
                    native_numbering._set_pos(hwp, original[0])  # noqa: SLF001
                    if native_numbering._read_target_state(hwp) != original:  # noqa: SLF001
                        raise LocalCliRuntimeError('Numbering semantic refusal restoration is unproven.')
                guard_verified = True
                raise _NumberingSemanticRefusal('Numbering semantic preflight refused; state and preservation verified.') from refusal
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise  # Preserve control-flow identity; the caller retains custody.
            if guard_verified and not issued_mutation and (type(exc) is native_numbering.NativeNumberingRefusal
                                                          or type(exc) is _NumberingSemanticRefusal):
                raise  # trusted producer and completed proof, never class/code alone
            # Inner-native refusals may also wrap failed getters/nonexact
            # native returns. Their class/code alone is not restoration or
            # preservation authority; retain UNKNOWN with no further SDK.
            if issued_mutation:
                error = LocalCliMutationError(
                    'Native numbering failed; outcome is unproven.', mutation_may_have_persisted=True,
                    rollback={'attempted': False, 'succeeded': False},
                )
            elif preedit_incomplete and phase == 'preflight':
                error = _NumberingPreEditHold('Numbering preservation is incomplete before navigation or mutation.')
            else:
                error = LocalCliRuntimeError('Numbering guard failed; outcome is unproven.')
            error.numbering_phase = phase
            error.numbering_reason = reason
            raise error from exc
        # A complete native success retains the existing caret restoration.
        # Failed native calls never enter this block or perform later SDK work.
        try:
            native_numbering._set_pos(hwp, original[0])  # noqa: SLF001
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise  # Issued intent already protects custody; preserve signal identity.
            error = LocalCliRuntimeError('Numbering success caret restoration is unproven.')
            error.numbering_phase = 'restore'
            error.numbering_reason = 'restore_unproven'
            raise error from exc
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
