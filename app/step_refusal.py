"""Strict state reads and typed clean refusals for the deferred-intent bundle engines.

layout_exact, object_insert_exact and paragraph_rehome_exact publish mutation intent at their first native mutator
instead of before invocation. Before that point they may refuse cleanly, but only:

- from values read through the strict readers below (any exception or malformed value is a technical
  failure that holds the session; an unreadable getter is never "normal", "False" or "different"), and
- with a code from the op's fixed set, through a LocalCliStepRefusal object the engine itself built and
  handed to the bundle's trusted registration callback after restoring and re-proving the caret.

The bundle accepts a refusal as clean only when the exception raised out of the step is that exact registered
object and no mutator was issued. Anything else keeps today's hold.
"""

from __future__ import annotations

from typing import Any, Callable

from app.local_cli_runtime import LocalCliRuntimeError

STEP_REFUSAL_CODES: dict[str, frozenset[str]] = {
    'layout_exact': frozenset({
        'invalid_request', 'caret_moved', 'editor_not_normal', 'caret_in_table', 'stale_expected_before',
        'nothing_to_change', 'layout_unsupported', 'not_section_start', 'marker_mismatch', 'proof_budget',
    }),
    'object_insert_exact': frozenset({
        'invalid_request', 'caret_moved', 'editor_not_normal', 'selection_required', 'selection_text_mismatch',
        'existing_header_footer', 'bookmark_exists', 'unverifiable_layout', 'proof_budget',
    }),
    'paragraph_rehome_exact': frozenset({'invalid_request', 'insert_before_delete', 'not_adjacent', 'different_list'}),
}
DEFERRED_INTENT_OPS = frozenset(STEP_REFUSAL_CODES)


class LocalCliStepRefusal(LocalCliRuntimeError):
    """Refused from strict source-origin evidence before the step's first native mutator; nothing changed."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class StepControl:
    """What a deferred-intent engine receives from the bundle: the intent callback and the refusal registration."""

    def __init__(self, op: str, *, on_mutating: Callable[[], None] | None = None,
                 on_refusal: Callable[[BaseException], None] | None = None) -> None:
        self.op = op
        self.on_mutating = on_mutating
        self.on_refusal = on_refusal
        self.started = False
        self.pending: LocalCliStepRefusal | None = None

    def begin_mutation(self) -> None:
        """Call immediately before the first native mutating call."""
        if not self.started and self.on_mutating is not None:
            self.on_mutating()
        self.started = True

    def refuse(self, code: str, message: str) -> LocalCliStepRefusal:
        """Build (not yet register) a clean refusal. Only valid before the first mutator."""
        if self.started:
            raise LocalCliRuntimeError(f'{self.op}: internal error: a refusal was built after a native mutator')
        if code not in STEP_REFUSAL_CODES[self.op]:
            raise LocalCliRuntimeError(f'{self.op}: internal error: unknown refusal code')
        refusal = LocalCliStepRefusal(message, code=code)
        self.pending = refusal
        return refusal

    def is_pending(self, exc: BaseException) -> bool:
        return self.pending is not None and exc is self.pending and not self.started

    def register(self, exc: LocalCliStepRefusal) -> None:
        """After the caret was restored and re-proved: hand the exact object to the bundle."""
        if self.on_refusal is not None and self.is_pending(exc):
            self.on_refusal(exc)


def _technical(op: str, what: str, exc: BaseException | None = None) -> LocalCliRuntimeError:
    error = LocalCliRuntimeError(f'{op}: {what} could not be read; the state is unknown')
    if exc is not None:
        error.__cause__ = exc
    return error


def _method(hwp: Any, *names: str) -> Callable[..., Any]:
    for name in names:
        value = getattr(hwp, name, None)
        if callable(value):
            return value
    raise LocalCliRuntimeError(f'needs one of: {", ".join(names)}')


def _index(value: Any) -> bool:
    return type(value) is int and value >= 0


def strict_pos(hwp: Any, op: str) -> tuple[int, int, int]:
    try:
        value = _method(hwp, 'get_pos', 'GetPos')()
    except Exception as exc:
        raise _technical(op, 'the caret', exc) from exc
    if (type(value) is not tuple and type(value) is not list) or len(value) != 3 or not all(_index(item) for item in value):
        raise _technical(op, 'the caret')
    return int(value[0]), int(value[1]), int(value[2])


def strict_selection(hwp: Any, op: str) -> tuple[int, int, int, int, int, int] | None:
    """None = no selection; six exact indexes = (slist, spara, spos, elist, epara, epos)."""
    try:
        value = _method(hwp, 'get_selected_pos', 'GetSelectedPos')()
    except Exception as exc:
        raise _technical(op, 'the selection', exc) from exc
    if (type(value) is not tuple and type(value) is not list) or len(value) != 7 or type(value[0]) is not bool:
        raise _technical(op, 'the selection')
    if value[0] is False:
        return None
    if not all(_index(item) for item in value[1:]):
        raise _technical(op, 'the selection')
    return tuple(int(item) for item in value[1:])  # type: ignore[return-value]


def strict_selection_mode(hwp: Any, op: str) -> int:
    try:
        raw = getattr(hwp, 'SelectionMode')
        value = raw() if callable(raw) else raw
    except Exception as exc:
        raise _technical(op, 'the selection mode', exc) from exc
    if type(value) is not int or value < 0:
        raise _technical(op, 'the selection mode')
    return value


def strict_is_cell(hwp: Any, op: str) -> bool:
    try:
        value = _method(hwp, 'is_cell')()
    except Exception as exc:
        raise _technical(op, 'the table-cell state', exc) from exc
    if type(value) is not bool:
        raise _technical(op, 'the table-cell state')
    return value


def strict_state(hwp: Any, op: str, *, need_cell: bool = False) -> dict[str, Any]:
    """Exact caret, selection and mode; normal = mode 0 and no selection."""
    pos = strict_pos(hwp, op)
    selection = strict_selection(hwp, op)
    mode = strict_selection_mode(hwp, op)
    state: dict[str, Any] = {'pos': pos, 'selection': selection, 'selection_mode': mode,
                             'normal': mode == 0 and selection is None, 'selected': selection is not None}
    if need_cell:
        state['is_cell'] = strict_is_cell(hwp, op)
    return state


def restore_caret(hwp: Any, op: str, pos: tuple[int, int, int]) -> None:
    """Put the caret back and prove it; any doubt is technical."""
    try:
        result = _method(hwp, 'set_pos', 'SetPos')(*pos)
    except Exception as exc:
        raise _technical(op, 'the restored caret', exc) from exc
    if result is not None and result is not True:
        raise _technical(op, 'the restored caret')
    if strict_pos(hwp, op) != tuple(pos):
        raise _technical(op, 'the restored caret')


# Fixed phases and reasons a post-mutation (or legacy engine) failure may carry into the journal and the public
# failure_diagnostic. Reasons are the engines' fixed labels; anything unlisted is dropped, never echoed.
_PHASES = frozenset({'native', 'readback', 'verify', 'after_mutation',
                     'pre_navigation', 'navigation', 'split_action', 'post_inventory',
                     'exact_line_proof', 'find_target', 'select', 'delete', 'post_count', 'next_guard'})
_EXTRA_REASONS = frozenset({'native_action_failed', 'native_outcome_unknown', 'readback_after_failed', 'typing_failed',
                            'close_return_failed', 'other', 'failed', 'text_changed'})


def step_diagnostic_allowed(phase: str, reason: str) -> bool:
    from app.layout_ops import REASON_CODES as LAYOUT_REASONS
    from app.object_insert import REASON_CODES as OBJECT_REASONS
    return phase in _PHASES and (reason in LAYOUT_REASONS or reason in OBJECT_REASONS or reason in _EXTRA_REASONS)
