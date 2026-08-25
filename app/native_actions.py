from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class NativeActionError(RuntimeError):
    pass


def _call_first(obj: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        method = getattr(obj, name, None)
        if callable(method):
            return method(*args, **kwargs)
    raise NativeActionError(f'none of the methods are available: {names!r}')


def _has_any(obj: Any, *names: str) -> bool:
    return any(callable(getattr(obj, name, None)) for name in names)


def _optional_call_first(obj: Any, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any | None:
    for name in names:
        method = getattr(obj, name, None)
        if callable(method):
            return method(*args, **kwargs)
    return None


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable_value(item) for key, item in value.items()}
    return str(value)


def _safe_set_parameter(pset: Any, key: str, value: Any) -> bool:
    for name in ('SetItem', 'set_item'):
        method = getattr(pset, name, None)
        if callable(method):
            method(key, value)
            return True

    if isinstance(pset, dict):
        pset[key] = value
        return True

    try:
        setattr(pset, key, value)
        return True
    except Exception:
        return False


def get_native_capabilities(hwp: Any) -> dict[str, Any]:
    haction = getattr(hwp, 'HAction', None)
    in_cell = None
    if _has_any(hwp, 'is_cell'):
        try:
            in_cell = bool(_optional_call_first(hwp, ('is_cell',)))
        except Exception:
            in_cell = None

    cursor_pos = None
    cursor_pos_error = None
    if _has_any(hwp, 'GetPos', 'get_pos'):
        try:
            cursor_pos = _jsonable_value(_call_first(hwp, ('GetPos', 'get_pos')))
        except Exception as exc:
            cursor_pos_error = str(exc)

    current_field_name = None
    if _has_any(hwp, 'GetCurFieldName'):
        try:
            current_field_name = _jsonable_value(_call_first(hwp, ('GetCurFieldName',)))
        except Exception:
            current_field_name = None

    selection_mode = None
    selection_mode_method = getattr(hwp, 'SelectionMode', None)
    if callable(selection_mode_method):
        try:
            selection_mode = _jsonable_value(selection_mode_method())
        except Exception:
            selection_mode = None
    elif selection_mode_method is not None:
        selection_mode = _jsonable_value(selection_mode_method)

    cell_addr = None
    if in_cell and _has_any(hwp, 'get_cell_addr'):
        try:
            cell_addr = _jsonable_value(_call_first(hwp, ('get_cell_addr',)))
        except Exception:
            cell_addr = None

    return {
        'create_action': _has_any(hwp, 'CreateAction') or _has_any(haction, 'CreateAction'),
        'create_set_on_hwp': _has_any(hwp, 'CreateSet'),
        'haction_run': _has_any(haction, 'Run'),
        'haction_execute': _has_any(haction, 'Execute'),
        'haction_get_default': _has_any(haction, 'GetDefault'),
        'hparameter_set': hasattr(hwp, 'HParameterSet'),
        'set_para': _has_any(hwp, 'set_para'),
        'set_parashape': _has_any(hwp, 'set_parashape'),
        'get_parashape': _has_any(hwp, 'get_parashape'),
        'set_font': _has_any(hwp, 'set_font'),
        'head_type': callable(getattr(hwp, 'HeadType', None)),
        'cursor': {
            'get_pos': _has_any(hwp, 'GetPos', 'get_pos'),
            'set_pos': _has_any(hwp, 'SetPos', 'set_pos'),
            'get_selected_pos': _has_any(hwp, 'GetSelectedPos', 'get_selected_pos'),
            'move_pos': _has_any(hwp, 'MovePos', 'move_pos'),
            'selection_mode': callable(getattr(hwp, 'SelectionMode', None)) or getattr(hwp, 'SelectionMode', None) is not None,
            'sample_pos': cursor_pos,
            'sample_pos_error': cursor_pos_error,
            'current_field_name': current_field_name,
            'current_selection_mode': selection_mode,
        },
        'table': {
            'is_cell': _has_any(hwp, 'is_cell'),
            'get_cell_addr': _has_any(hwp, 'get_cell_addr'),
            'get_into_nth_table': _has_any(hwp, 'get_into_nth_table'),
            'create_table': _has_any(hwp, 'create_table'),
            'cell_fill': _has_any(hwp, 'cell_fill'),
            'table_to_df': _has_any(hwp, 'table_to_df'),
            'navigation_actions': {
                'left': _has_any(hwp, 'TableLeftCell'),
                'right': _has_any(hwp, 'TableRightCell'),
                'up': _has_any(hwp, 'TableUpperCell'),
                'down': _has_any(hwp, 'TableLowerCell'),
            },
            'sample_is_cell': in_cell,
            'sample_cell_addr': cell_addr,
        },
    }


@dataclass
class NativeActionResult:
    action: str
    mode: str
    succeeded: bool
    strategy: str = ''
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class NativeActionRunner:
    def __init__(self, hwp: Any):
        self.hwp = hwp
        self.haction = getattr(hwp, 'HAction', None)

    def capabilities(self) -> dict[str, Any]:
        return get_native_capabilities(self.hwp)

    def run(self, action: str) -> NativeActionResult:
        if not _has_any(self.haction, 'Run'):
            return NativeActionResult(action=action, mode='run', succeeded=False, error='HAction.Run is unavailable')
        try:
            raw = _call_first(self.haction, ('Run',), action)
            succeeded = True if raw is None else bool(raw)
            return NativeActionResult(action=action, mode='run', succeeded=succeeded, strategy='HAction.Run', details={'raw_result': raw})
        except Exception as exc:
            return NativeActionResult(action=action, mode='run', succeeded=False, strategy='HAction.Run', error=str(exc))

    def execute(self, action: str, *, parameters: dict[str, Any] | None = None, set_name: str | None = None) -> NativeActionResult:
        parameters = parameters or {}
        try:
            action_obj = self._create_action(action)
            pset, pset_strategy = self._create_parameter_set(action_obj, set_name=set_name)

            get_default_strategy = ''
            try:
                get_default_strategy = self._apply_get_default(action, action_obj, pset)
            except Exception:
                get_default_strategy = ''

            unset_keys: list[str] = []
            for key, value in parameters.items():
                if not _safe_set_parameter(pset, key, value):
                    unset_keys.append(key)

            execute_strategy, raw = self._execute_action(action, action_obj, pset)
            succeeded = True if raw is None else bool(raw)
            return NativeActionResult(
                action=action,
                mode='execute',
                succeeded=succeeded,
                strategy=execute_strategy,
                details={
                    'create_set_strategy': pset_strategy,
                    'get_default_strategy': get_default_strategy,
                    'unset_parameter_keys': unset_keys,
                    'parameter_keys': sorted(parameters.keys()),
                    'raw_result': raw,
                },
            )
        except Exception as exc:
            return NativeActionResult(action=action, mode='execute', succeeded=False, error=str(exc))

    def auto(self, action: str, *, parameters: dict[str, Any] | None = None, set_name: str | None = None) -> NativeActionResult:
        if parameters:
            executed = self.execute(action, parameters=parameters, set_name=set_name)
            if executed.succeeded:
                return executed
        ran = self.run(action)
        if ran.succeeded:
            return ran
        if not parameters:
            return self.execute(action, parameters=parameters, set_name=set_name)
        return executed

    def _create_action(self, action: str) -> Any:
        creator = getattr(self.hwp, 'CreateAction', None)
        if callable(creator):
            return creator(action)
        if _has_any(self.haction, 'CreateAction'):
            return _call_first(self.haction, ('CreateAction',), action)
        raise NativeActionError('CreateAction is unavailable')

    def _create_parameter_set(self, action_obj: Any, *, set_name: str | None = None) -> tuple[Any, str]:
        creator = getattr(action_obj, 'CreateSet', None)
        if callable(creator):
            return creator(), 'action.CreateSet'

        if set_name:
            hparameter_set = getattr(self.hwp, 'HParameterSet', None)
            if hparameter_set is not None and hasattr(hparameter_set, set_name):
                return getattr(hparameter_set, set_name), f'HParameterSet.{set_name}'

            create_set = getattr(self.hwp, 'CreateSet', None)
            if callable(create_set):
                return create_set(set_name), 'hwp.CreateSet'

        raise NativeActionError('CreateSet/HParameterSet is unavailable for this action')

    def _apply_get_default(self, action: str, action_obj: Any, pset: Any) -> str:
        action_get_default = getattr(action_obj, 'GetDefault', None)
        if callable(action_get_default):
            action_get_default(pset)
            return 'action.GetDefault'

        if _has_any(self.haction, 'GetDefault'):
            _call_first(self.haction, ('GetDefault',), action, pset)
            return 'HAction.GetDefault'

        raise NativeActionError('GetDefault is unavailable')

    def _execute_action(self, action: str, action_obj: Any, pset: Any) -> tuple[str, Any]:
        action_execute = getattr(action_obj, 'Execute', None)
        if callable(action_execute):
            return 'action.Execute', action_execute(pset)

        if _has_any(self.haction, 'Execute'):
            return 'HAction.Execute', _call_first(self.haction, ('Execute',), action, pset)

        raise NativeActionError('Execute is unavailable')
