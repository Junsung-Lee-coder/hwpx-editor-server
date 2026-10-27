"""Native Hancom page-number insertion and official-document outline numbering.

Both operations only drive documented HAction/HParameterSet calls:

* page numbers: ``HAction.GetDefault/Execute('PageNumPos')`` with ``HParameterSet.HPageNumPos``
* outline numbering: ``HAction.GetDefault/Execute('OutlineNumber')`` with ``HParameterSet.HSecDef``
  (``OutlineShape`` NumberingShape, ``ApplyTo=2`` current section) to set or verify the four-level
  ``1. -> 가. -> 1) -> 가)`` definition (only when it differs), and one
  ``HAction.Execute('ParagraphShape')`` with ``HParaShape.HeadingType=1`` (outline) and ``Level`` for the
  paragraph. The ParaShape.HeadingType/Level fields are documented; whether one native undo reverts the
  paragraph change is NOT proven here, and the define-then-number path is two native actions.

Nothing here edits XML/ZIP content or writes number characters into text. A ``protection`` reader
(a zero-argument callable returning a census v2 snapshot) is read before and after every mutating call.
Mandatory preservation evidence that is missing or unresolvable before the call refuses with zero
mutation (``preservation_unprovable``); missing evidence, or any change outside a small exact allowed
delta (page numbers: the new page-number control only; official numbering: the target paragraph's
numbering and, when needed, the requested four-level outline definition) after the call raises
``NativeNumberingMutationError``, which is never rolled back and never reported as a normal success.
"""
from __future__ import annotations

import builtins
import hashlib
from typing import Any, Callable, Mapping

from app.edit_ops import EditOperationError


class NativeNumberingInputError(EditOperationError):
    """The request is malformed; the document was never touched."""

    mutation_state = 'none'


class NativeNumberingRefusal(EditOperationError):
    """A preflight guard refused the request before any native mutation call."""

    mutation_state = 'none'

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class NativeNumberingMutationError(EditOperationError):
    """A native mutation call was issued and the final document state is not proven."""

    mutation_state = 'unknown'
    mutation_may_have_persisted = True
    fixed_reason: dict[str, Any] | None = None

    def __init__(self, message: str, *, fixed_reason: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.rollback = {'attempted': False, 'succeeded': False}
        if type(fixed_reason) is dict:
            self.fixed_reason = fixed_reason


PAGE_NUMBER_REQUEST_KEYS = (
    'position',
    'start_number',
    'side_char',
    'prefix',
    'suffix',
)
OFFICIAL_NUMBERING_REQUEST_KEYS = ('level',)

# Official-document system 1. -> 가. -> 1) -> 가). NumFormat codes come from the documented
# PageNumPos/NumberingShape number-format domain (0 = digit, 8 = Hangul syllable); StrFormat uses the
# documented ^N level codes. The native Level of each entry is 0-based.
OFFICIAL_OUTLINE_LEVELS = (
    {'level': 1, 'native_level': 0, 'str_format': '^1.', 'num_format': 0},
    {'level': 2, 'native_level': 1, 'str_format': '^2.', 'num_format': 8},
    {'level': 3, 'native_level': 2, 'str_format': '^3)', 'num_format': 0},
    {'level': 4, 'native_level': 3, 'str_format': '^4)', 'num_format': 8},
)
# SecDef ApplyTo 2 (current section) requires the matching ApplyClass flag 0x04 (ParameterSetTable_2504); the
# smallest compatible class is that flag alone.
_OUTLINE_APPLY_TO_CURRENT_SECTION = 2
_OUTLINE_APPLY_CLASS = 1 << _OUTLINE_APPLY_TO_CURRENT_SECTION
_PAGE_PROP_LIMITS = {'DrawPos': 255, 'NewNumber': 65535, 'SideChar': 65535, 'NumberFormat': 255}
_CENSUS_HEADINGS = {'NONE', 'OUTLINE', 'NUMBER', 'BULLET'}

PAGE_NUMBER_POSITIONS = {
    'top-left': 1,
    'top-center': 2,
    'top-right': 3,
    'bottom-left': 4,
    'bottom-center': 5,
    'bottom-right': 6,
}
SIDE_CHAR_CODES = {'none': 0, 'dash': 45}
START_NUMBER_MIN = 1
START_NUMBER_MAX = 65535
OFFICIAL_LEVEL_MIN = 1
OFFICIAL_LEVEL_MAX = 4

_DIGIT_NUMBER_FORMAT = 0
_HEADING_NONE = 0
_HEADING_OUTLINE = 1
_MAX_CONTROLS = 4096
_MAX_FIND_HITS = 10000

_PREFIX_SUFFIX_LIMIT = (
    'page numbers support only the native SideChar decoration (none or dash); '
    'Hancom PageNumPos has no usable arbitrary prefix/suffix text, so prefix/suffix other than '
    "both '' or both '-' is refused"
)


def _require_mapping(payload: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise NativeNumberingInputError(f'{what} request must be an object')
    return payload


def _reject_unknown_keys(payload: Mapping[str, Any], allowed: tuple[str, ...], what: str) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise NativeNumberingInputError(
            f'{what} request has unsupported field(s): {", ".join(unknown)}. Allowed: {", ".join(allowed)}'
        )


def _exact_int(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NativeNumberingInputError(f'{name} must be an integer from {low} to {high}')
    if value < low or value > high:
        raise NativeNumberingInputError(f'{name} must be an integer from {low} to {high}')
    return value


def normalize_page_number_request(payload: Any) -> dict[str, Any]:
    data = _require_mapping(payload, 'page number')
    _reject_unknown_keys(data, PAGE_NUMBER_REQUEST_KEYS, 'page number')

    position = data.get('position')
    if not isinstance(position, str) or position not in PAGE_NUMBER_POSITIONS:
        raise NativeNumberingInputError(
            f'position is required and must be one of: {", ".join(PAGE_NUMBER_POSITIONS)}'
        )

    start_number = 1
    if 'start_number' in data:
        start_number = _exact_int(data['start_number'], 'start_number', START_NUMBER_MIN, START_NUMBER_MAX)

    side_char: str | None = None
    if 'side_char' in data:
        raw_side = data['side_char']
        if not isinstance(raw_side, str) or raw_side not in SIDE_CHAR_CODES:
            raise NativeNumberingInputError(f'side_char must be one of: {", ".join(SIDE_CHAR_CODES)}')
        side_char = raw_side

    if 'prefix' in data or 'suffix' in data:
        prefix = data.get('prefix')
        suffix = data.get('suffix')
        if not isinstance(prefix, str) or not isinstance(suffix, str):
            raise NativeNumberingInputError(f'prefix and suffix must be given together as strings; {_PREFIX_SUFFIX_LIMIT}')
        if (prefix, suffix) == ('', ''):
            derived = 'none'
        elif (prefix, suffix) == ('-', '-'):
            derived = 'dash'
        else:
            raise NativeNumberingInputError(_PREFIX_SUFFIX_LIMIT)
        if side_char is not None and side_char != derived:
            raise NativeNumberingInputError(
                f'side_char={side_char!r} conflicts with prefix/suffix, which mean side_char={derived!r}'
            )
        side_char = derived
    if side_char is None:
        side_char = 'none'

    return {
        'position': position,
        'draw_pos': PAGE_NUMBER_POSITIONS[position],
        'start_number': start_number,
        'side_char': side_char,
        'side_char_code': SIDE_CHAR_CODES[side_char],
        'number_format': _DIGIT_NUMBER_FORMAT,
    }


def normalize_official_numbering_request(payload: Any) -> dict[str, Any]:
    data = _require_mapping(payload, 'official numbering')
    _reject_unknown_keys(data, OFFICIAL_NUMBERING_REQUEST_KEYS, 'official numbering')
    if 'level' not in data:
        raise NativeNumberingInputError(
            f'level is required and must be an integer from {OFFICIAL_LEVEL_MIN} to {OFFICIAL_LEVEL_MAX}'
        )
    level = _exact_int(data['level'], 'level', OFFICIAL_LEVEL_MIN, OFFICIAL_LEVEL_MAX)
    return {
        'level': level,
        'native_level': level - 1,
    }


def _action_confirmed(result: Any) -> bool:
    """HAction.GetDefault/Execute are documented BOOL (HwpAutomation_2504); only the bool True is a success."""
    return result is True


def _status_text(result: Any) -> str:
    return 'False' if result is False else f'a non-BOOL {type(result).__name__} value'


def _require_haction(hwp: Any, *methods: str) -> Any:
    haction = getattr(hwp, 'HAction', None)
    if haction is None:
        raise NativeNumberingRefusal('native HAction is unavailable on this machine', code='native_unavailable')
    for method in methods:
        if not callable(getattr(haction, method, None)):
            raise NativeNumberingRefusal(f'native HAction.{method} is unavailable on this machine', code='native_unavailable')
    if getattr(hwp, 'HParameterSet', None) is None:
        raise NativeNumberingRefusal('native HParameterSet is unavailable on this machine', code='native_unavailable')
    return haction


def _read_text_hash(hwp: Any) -> str:
    reader = getattr(hwp, 'get_text_file', None)
    if not callable(reader):
        raise RuntimeError('get_text_file is unavailable')
    text = reader('UNICODE', '')
    if not isinstance(text, str):
        raise RuntimeError('get_text_file returned a non-text value')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _control_ids(hwp: Any) -> list[tuple[Any, str]]:
    head = hwp.HeadCtrl
    controls: list[tuple[Any, str]] = []
    ctrl = head
    for _ in range(_MAX_CONTROLS):
        if ctrl is None:
            return controls
        controls.append((ctrl, str(ctrl.CtrlID)))
        ctrl = ctrl.Next
    raise RuntimeError(f'control enumeration exceeded {_MAX_CONTROLS} controls')


def _preflight_census(hwp: Any) -> tuple[str, list[tuple[Any, str]]]:
    try:
        text_hash = _read_text_hash(hwp)
    except Exception as exc:
        raise NativeNumberingRefusal(
            f'document text could not be read back before editing: exception of class {_exception_class_label(exc)}',
            code='text_readback_unavailable',
        ) from exc
    try:
        controls = _control_ids(hwp)
    except Exception as exc:
        raise NativeNumberingRefusal(
            f'main-list control enumeration is unavailable: exception of class {_exception_class_label(exc)}',
            code='control_enumeration_unavailable',
        ) from exc
    return text_hash, controls


def _exact_index(value: Any) -> bool:
    return type(value) is int and value >= 0


def _get_pos(hwp: Any) -> tuple[int, int, int] | None:
    """Caret as (list, para, pos); only three exact non-negative ints count, nothing is coerced."""
    getter = getattr(hwp, 'get_pos', None) or getattr(hwp, 'GetPos', None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(_exact_index(item) for item in value):
        return (value[0], value[1], value[2])
    return None


def _parse_selected_pos(value: Any) -> tuple[int, ...] | None:
    """() = no selection, six exact ints = (slist, spara, spos, elist, epara, epos), None = not canonical."""
    if not isinstance(value, (list, tuple)) or len(value) != 7 or type(value[0]) is not bool:
        return None
    if not value[0]:
        return ()
    coordinates = tuple(value[1:])
    if not all(_exact_index(item) for item in coordinates):
        return None
    return coordinates


def _get_selection(hwp: Any) -> tuple[int, ...] | None:
    getter = getattr(hwp, 'get_selected_pos', None) or getattr(hwp, 'GetSelectedPos', None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception:
        return None
    return _parse_selected_pos(value)


_TargetState = tuple[tuple[int, int, int], tuple[int, ...]]


def _read_target_state(hwp: Any) -> _TargetState | None:
    caret = _get_pos(hwp)
    selection = _get_selection(hwp)
    # One additional get_pos SDK expression after the selection getter, so a caret the getter moved or invalidated is
    # seen at this boundary; it assumes neither that the getters are side-effect free nor that the read costs no time.
    settled = _get_pos(hwp)
    if caret is None or selection is None or settled != caret:
        return None
    return caret, selection


def _anchor_target(hwp: Any, expected: tuple[int, int] | None = None) -> _TargetState:
    """Record the caret/selection that identifies the target paragraph, before any getter or census can move it."""
    state = _read_target_state(hwp)
    if state is None:
        raise NativeNumberingRefusal(
            'the caret and selection could not be read as exact non-negative integers, so the target paragraph cannot be proven',
            code='target_unlocatable',
        )
    caret, selection = state
    if expected is not None and (caret[0], caret[1]) != expected:
        raise NativeNumberingRefusal('the caret is not on the target paragraph after positioning', code='target_unlocatable')
    if selection and (selection[0], selection[1], selection[3], selection[4]) != (caret[0], caret[1], caret[0], caret[1]):
        raise NativeNumberingRefusal('a selection outside the target paragraph is still active', code='target_unlocatable')
    return state


def _target_guard(hwp: Any, anchor: _TargetState) -> Callable[[bool, str], None]:
    """Boundary check run immediately before a dependent native mutator; a drifted caret is never recaptured."""

    def guard(mutated: bool, boundary: str) -> None:
        if _read_target_state(hwp) == anchor:
            return
        if mutated:
            reason = _fixed_reason(lambda: _official_reason(['target_moved_after_define']))
            raise NativeNumberingMutationError(
                f'the target caret/selection changed or could not be read {boundary} after the outline definition was written; '
                'the paragraph write was not attempted, the caret was not recaptured and nothing was rolled back',
                fixed_reason=reason,
            )
        raise NativeNumberingRefusal(
            f'the target caret/selection changed or could not be read {boundary}; refusing before any mutation',
            code='target_moved',
        )

    return guard


def _set_pos(hwp: Any, pos: tuple[int, int, int]) -> None:
    setter = getattr(hwp, 'set_pos', None) or getattr(hwp, 'SetPos', None)
    if not callable(setter):
        raise RuntimeError('set_pos is unavailable')
    setter(pos[0], pos[1], pos[2])


def _page_number_props(
    ctrl: Any, *, _diagnostic: list[tuple[str, str, Any]] | None = None, allow_unset_new_number: bool = False
) -> dict[str, int | None] | None:
    """Read pgnp settings; keep the existing UI1/UI2 validation and getter sequence.

    With ``allow_unset_new_number`` a NewNumber that the native getter returned as exactly None is kept as None
    (never coerced); every other value, and every other property, is validated as before.
    """
    stage, name = 'Properties', 'Properties'
    try:
        props = ctrl.Properties
        stage = 'Item'
        values = {}
        for name in _PAGE_PROP_LIMITS:
            values[name] = props.Item(name)
    except Exception as exc:
        if _diagnostic is not None:
            try:
                _diagnostic.append((stage, name, exc))
            except BaseException:
                pass  # Diagnostic failure must not change the unreadable outcome.
        return None
    for name, value in values.items():
        if allow_unset_new_number and name == 'NewNumber' and value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _PAGE_PROP_LIMITS[name]:
            if _diagnostic is not None:
                try:
                    _diagnostic.append(('validate', name, value))
                except BaseException:
                    pass
            return None
    return values


def _page_number_diagnostic_value(value: Any) -> str:
    """Bound exact primitives only; never format a wrapper, subclass or nested value."""
    kind = type(value)
    if kind is str:
        return ascii(value[:32])[:64]
    if kind is int:
        return str(value) if -2147483648 <= value <= 4294967295 else '<integer-out-of-display-range>'
    if kind is bool:
        return 'True' if value else 'False'
    if value is None:
        return 'None'
    if kind is float:
        return float.hex(value)
    return '<noncanonical>'


def _page_number_readback_detail(observations: list[tuple[str, str, Any]]) -> str:
    """No SDK access/cause traversal; bypass exception and metaclass attribute hooks.

    Only the first two already-returned exception args are inspected (not nested
    COM excepinfo). This is bounded diagnostic text, never native proof or fallback.
    """
    stage, field, value = observations[0]
    name = type.__dict__['__name__'].__get__(type(value))
    label = ascii(name[:32])[1:-1][:48] if type(name) is str else '<noncanonical>'
    prefix = f'page-number readback stage={stage} field={field} type={label} failure='
    if stage == 'validate':
        return (prefix + 'invalid_integer value=' + _page_number_diagnostic_value(value))[:220]
    args = BaseException.__dict__['args'].__get__(value)
    detail = prefix + 'exception'
    if type(args) is tuple:
        for index, arg in enumerate(args[:2]):
            detail += f' arg{index}=' + _page_number_diagnostic_value(arg)
    return detail[:220]


def _validated_census(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise RuntimeError('protection census must be an object')
    census: dict[str, Any] = {}
    header_footer = raw.get('header_footer')
    if header_footer is not None and (not isinstance(header_footer, list) or not all(isinstance(item, str) for item in header_footer)):
        raise RuntimeError('protection census header_footer must be a list of digests')
    census['header_footer'] = list(header_footer) if header_footer is not None else None
    for field in ('body_sha256', 'styles_sha256', 'numberings_sha256', 'bindata_sha256', 'media_sha256'):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise RuntimeError(f'protection census {field} must be a digest string')
        census[field] = value
    count = raw.get('page_number_count')
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
        raise RuntimeError('protection census page_number_count must be a non-negative integer')
    census['page_number_count'] = count
    paragraphs = raw.get('paragraphs')
    if paragraphs is not None:
        if not isinstance(paragraphs, list):
            raise RuntimeError('protection census paragraphs must be a list')
        checked: list[dict[str, Any]] = []
        for item in paragraphs:
            if not isinstance(item, Mapping):
                raise RuntimeError('protection census paragraph entries must be objects')
            section, level = item.get('section'), item.get('level')
            if (
                isinstance(section, bool) or not isinstance(section, int)
                or isinstance(level, bool) or not isinstance(level, int)
                or not isinstance(item.get('text'), str)
                or not isinstance(item.get('text_sha256'), str)
                or item.get('heading') not in _CENSUS_HEADINGS
            ):
                raise RuntimeError('protection census paragraph entry is malformed')
            if 'restart_stripped_sha256' in item and not isinstance(item['restart_stripped_sha256'], str):
                raise RuntimeError('protection census paragraph restart_stripped_sha256 must be a digest string')
            if item.get('inst_id') is not None and not isinstance(item['inst_id'], str):
                raise RuntimeError('protection census paragraph inst_id must be a string')
            if 'inst_ids' in item and not (isinstance(item['inst_ids'], list) and all(isinstance(value, str) for value in item['inst_ids'])):
                raise RuntimeError('protection census paragraph inst_ids must be a list of strings')
            if item.get('shares_outline_numbering') is not None and not isinstance(item['shares_outline_numbering'], bool):
                raise RuntimeError('protection census paragraph shares_outline_numbering must be a boolean')
            checked.append(dict(item))
        paragraphs = checked
    census['paragraphs'] = paragraphs
    census['page_restarts'] = _validated_page_restarts(raw.get('page_restarts'))
    census['page_restart_positions'] = _validated_restart_positions(raw.get('page_restart_positions'), census['page_restarts'])
    census['definitions'] = _validated_definitions(raw.get('definitions'))
    frame = raw.get('frame')
    if frame is not None and (
        not isinstance(frame, Mapping) or not all(isinstance(frame.get(key), str) for key in ('sha256', 'stripped_sha256'))
    ):
        raise RuntimeError('protection census frame must hold sha256 and stripped_sha256 digests')
    census['frame'] = dict(frame) if frame is not None else None
    shapes = raw.get('outline_shapes')
    if shapes is not None and (not isinstance(shapes, list) or not all(isinstance(item, str) for item in shapes)):
        raise RuntimeError('protection census outline_shapes must be a list of digests')
    census['outline_shapes'] = list(shapes) if shapes is not None else None
    return census


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validated_page_restarts(raw: Any) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise RuntimeError('protection census page_restarts must be a list')
    checked: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise RuntimeError('protection census page_restarts entries must be objects')
        attrs, path = item.get('attrs'), item.get('path')
        if (
            item.get('location') not in {'body', 'outside'}
            or not all(item.get(key) is None or _is_plain_int(item.get(key)) for key in ('section', 'paragraph'))
            or not isinstance(path, list)
            or not all(isinstance(step, str) for step in path)
            or not isinstance(attrs, list)
            or not all(isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair) for pair in attrs)
            or not _is_plain_int(item.get('children'))
            or not isinstance(item.get('text'), str)
            or not isinstance(item.get('tail'), str)
        ):
            raise RuntimeError('protection census page_restarts entry is malformed')
        checked.append(
            {
                'location': item['location'],
                'section': item.get('section'),
                'paragraph': item.get('paragraph'),
                'path': list(path),
                'attrs': [list(pair) for pair in attrs],
                'children': item['children'],
                'text': item['text'],
                'tail': item['tail'],
            }
        )
    return checked


def _validated_restart_positions(raw: Any, restarts: list[dict[str, Any]] | None) -> list[list[int]] | None:
    if raw is None:
        return None
    if (
        restarts is None
        or not isinstance(raw, list)
        or len(raw) != len(restarts)
        or not all(isinstance(item, list) and all(_is_plain_int(step) for step in item) for item in raw)
    ):
        raise RuntimeError('protection census page_restart_positions must hold one list of integers per page restart')
    return [list(item) for item in raw]


def _validated_definitions(raw: Any) -> dict[str, list[tuple[str, str]]] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise RuntimeError('protection census definitions must be an object')
    definitions: dict[str, list[tuple[str, str]]] = {}
    for name, items in raw.items():
        if not isinstance(name, str) or not isinstance(items, list):
            raise RuntimeError('protection census definitions must map list names to item lists')
        entries: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get('sha'), str) or not isinstance(item.get('shell'), str):
                raise RuntimeError('protection census definition items must carry sha and shell digests')
            entries.append((item['sha'], item['shell']))
        definitions[name] = entries
    return definitions


_MANDATORY_DEFINITION_LISTS = ('charshapelist', 'parashapelist', 'stylelist')
_PAGE_NUMBER_COMPARED = (
    'header_footer', 'body_sha256', 'styles_sha256', 'numberings_sha256', 'bindata_sha256',
    'definitions', 'outline_shapes', 'frame', 'paragraphs',
)
_OFFICIAL_COMPARED = (
    'header_footer', 'body_sha256', 'styles_sha256', 'bindata_sha256', 'media_sha256', 'definitions', 'outline_shapes', 'frame', 'paragraphs',
)
_MAX_REPORTED_PROBLEMS = 5


def _read_protection(protection: Callable[[], Any] | None) -> dict[str, Any] | None:
    if protection is None:
        return None
    return _validated_census(protection())


def _incomplete(census: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    for field, label in (
        ('paragraphs', 'per-paragraph structure digests'),
        ('header_footer', 'header/footer digests'),
        ('frame', 'body frame digest'),
        ('outline_shapes', 'section outline definition references'),
    ):
        if census.get(field) is None:
            reasons.append(f'{label} could not be resolved')
    for item in census.get('paragraphs') or []:
        if not all(isinstance(item.get(key), str) for key in ('sha256', 'masked_sha256', 'stripped_sha256')):
            reasons.append('paragraph structure digests are missing')
            break
    definitions = census.get('definitions')
    if definitions is None:
        reasons.append('style, shape and numbering definitions could not be resolved')
    else:
        for name in _MANDATORY_DEFINITION_LISTS:
            if not definitions.get(name):
                reasons.append(f'{name} is missing, empty or not recognized')
    return reasons


def _require_complete(census: Mapping[str, Any]) -> None:
    reasons = _incomplete(census)
    if reasons:
        raise NativeNumberingRefusal(
            'mandatory document preservation evidence is unknown before the edit (' + '; '.join(reasons)
            + '), so the edit cannot be proven to leave the rest of the document unchanged; refusing before any mutation',
            code='preservation_unprovable',
        )


def _official_text_evidence_complete(census: Mapping[str, Any]) -> bool:
    """Additional proof required to replace UNICODE-export equality, not a general census waiver."""
    if _incomplete(census):
        return False
    digests = [census.get(field) for field in ('body_sha256', 'styles_sha256', 'numberings_sha256', 'media_sha256')]
    digests.extend(census['header_footer'])
    digests.extend(census['outline_shapes'])
    digests.extend(census['frame'][key] for key in ('sha256', 'stripped_sha256'))
    for paragraph in census['paragraphs']:
        digests.extend(paragraph.get(key) for key in ('text_sha256', 'sha256', 'masked_sha256', 'stripped_sha256'))
    for entries in census['definitions'].values():
        for sha, shell in entries:
            digests.extend((sha, shell))
    return all(type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value) for value in digests)


def _count(entries: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for entry in entries:
        counts[entry] = counts.get(entry, 0) + 1
    return counts


def _multiset_delta(before: list[tuple[str, str]], after: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    left, right = _count(before), _count(after)
    removed = [entry for entry, number in left.items() for _ in range(max(number - right.get(entry, 0), 0))]
    added = [entry for entry, number in right.items() for _ in range(max(number - left.get(entry, 0), 0))]
    return removed, added


def _same_paragraph_count(before: Mapping[str, Any], after: Mapping[str, Any], problems: list[str]) -> bool:
    if len(before['paragraphs']) != len(after['paragraphs']):
        problems.append('the paragraph count changed')
        return False
    return True


def _raw_field_problems(before: Mapping[str, Any], after: Mapping[str, Any], fields: tuple[str, ...]) -> list[str]:
    return [f'{field} changed' for field in fields if before.get(field) != after.get(field)]


def _has_restart_evidence(census: Mapping[str, Any]) -> bool:
    return (
        census.get('page_restarts') is not None
        and census.get('page_restart_positions') is not None
        and all(isinstance(item.get('restart_stripped_sha256'), str) for item in census.get('paragraphs') or [])
    )


def _first_paragraph_start_conflicts(restarts: list[dict[str, Any]], request_start: int | None) -> list[str]:
    if request_start is None:
        return []
    problems: list[str] = []
    for entry in restarts:
        if entry['location'] != 'body' or entry['section'] != 0 or entry['paragraph'] != 0:
            continue
        number = next((value for name, value in entry['attrs'] if name == 'number'), '').strip()
        if not (number.isascii() and number.isdigit()) or int(number) != request_start:
            problems.append(
                f'a page restart in the first paragraph of the first section sets the start to {number!r}, '
                f'which contradicts the requested start {request_start}'
            )
    return problems


def _page_restart_problems(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    before_positions: list[list[int]],
    after_positions: list[list[int]],
    request_start: int | None,
    require_restart: bool,
) -> list[str]:
    if after == before:
        problems = _first_paragraph_start_conflicts(after, request_start)
        if after_positions != before_positions:
            problems.append('an existing page restart moved relative to the content around it')
        if require_restart:
            problems.append('no new page restart appeared in the document after the native call to confirm the requested start')
        return problems
    if before:
        return ['existing page restart(s) changed or cannot be told apart from the new one']
    if request_start is None or len(after) != 1:
        return [f'{len(after)} page restart(s) appeared and none can be bound to the request']
    entry = after[0]
    attrs = entry['attrs']
    bound = (
        entry['location'] == 'body'
        and entry['section'] == 0
        and entry['paragraph'] == 0
        and entry['path'] == ['section', 'p', 'text']
        and len(attrs) == 2
        and attrs[0] == ['number', str(request_start)]
        and attrs[1][0] == 'numbertype'
        and attrs[1][1].strip().lower() == 'page'
        and entry['children'] == 0
        and entry['text'] == ''
        and entry['tail'] == ''
    )
    return [] if bound else ['the new page restart does not match the requested start in the first paragraph of the single section']


def _page_number_delta_problems(
    before: Mapping[str, Any], after: Mapping[str, Any], *, request_start: int | None = None, require_restart: bool = False
) -> list[str]:
    problems = _raw_field_problems(
        before, after, ('header_footer', 'body_sha256', 'styles_sha256', 'numberings_sha256', 'bindata_sha256', 'definitions', 'outline_shapes')
    )
    restart_evidence = _has_restart_evidence(before) and _has_restart_evidence(after)
    digest_key = 'restart_stripped_sha256' if restart_evidence else 'stripped_sha256'
    if restart_evidence:
        problems.extend(
            _page_restart_problems(
                before['page_restarts'],
                after['page_restarts'],
                before_positions=before['page_restart_positions'],
                after_positions=after['page_restart_positions'],
                request_start=request_start,
                require_restart=require_restart,
            )
        )
    elif require_restart:
        problems.append('page restart evidence is not available in the document census, so the start number cannot be confirmed')
    if before['frame']['stripped_sha256'] != after['frame']['stripped_sha256']:
        problems.append('the document body frame changed')
    if _same_paragraph_count(before, after, problems):
        changed = [
            index
            for index, (left, right) in enumerate(zip(before['paragraphs'], after['paragraphs']))
            if left[digest_key] != right[digest_key] or left['text_sha256'] != right['text_sha256']
        ]
        if changed:
            problems.append(f'paragraph(s) {changed[:_MAX_REPORTED_PROBLEMS]} changed beyond the new page number element')
    return problems


_NATIVE_INST_ID_MAX = 4294967295


def _is_native_inst_id(value: Any) -> bool:
    return (
        isinstance(value, str) and value.isascii() and value.isdigit() and str(int(value)) == value and int(value) <= _NATIVE_INST_ID_MAX
    )


def _paragraph_inst_ids(item: Mapping[str, Any]) -> list[str]:
    ids = item.get('inst_ids')
    if ids is not None:
        return list(ids)
    single = item.get('inst_id')
    return [] if single is None else [single]


_UNDO_RESTORED_INST_ID_FLAG = 0x80000000


def _target_inst_id_problems(
    before: Mapping[str, Any], after: Mapping[str, Any], *, target_index: int, target_written: bool
) -> list[str]:
    """The target may gain one new native paragraph identifier from the ParagraphShape write; nothing else may change it.

    A paragraph restored by Undo carries its identifier with bit 31 set, and a ParagraphShape write on it
    allocates a newer identifier of the same native sequence with bit 31 clear; that replacement is admitted.
    A ParagraphShape write that moves an outline paragraph holding an identifier (bit 31 clear) to another
    outline level likewise replaces it with a newer, previously unused identifier of the same sequence; that
    replacement is admitted too.
    """
    left = _paragraph_inst_ids(before['paragraphs'][target_index])
    right = _paragraph_inst_ids(after['paragraphs'][target_index])
    if left == right:
        return []
    others = [_paragraph_inst_ids(item) for index, item in enumerate(after['paragraphs']) if index != target_index]
    unambiguous = all(len(ids) <= 1 for ids in others)
    if target_written and not left and len(right) == 1 and _is_native_inst_id(right[0]) and unambiguous and not any(right[0] in ids for ids in others):
        return []
    if (
        target_written and len(left) == 1 and len(right) == 1 and _is_native_inst_id(left[0]) and _is_native_inst_id(right[0])
        and int(left[0]) & _UNDO_RESTORED_INST_ID_FLAG and int(left[0]) - _UNDO_RESTORED_INST_ID_FLAG < int(right[0]) < _UNDO_RESTORED_INST_ID_FLAG
        and unambiguous and not any(right[0] in ids for ids in others)
    ):
        return []
    was, now = before['paragraphs'][target_index], after['paragraphs'][target_index]
    if (
        target_written and len(left) == 1 and len(right) == 1 and _is_native_inst_id(left[0]) and _is_native_inst_id(right[0])
        and not int(left[0]) & _UNDO_RESTORED_INST_ID_FLAG and int(left[0]) < int(right[0]) < _UNDO_RESTORED_INST_ID_FLAG
        and was.get('heading') == now.get('heading') == 'OUTLINE' and was.get('level') != now.get('level')
        and unambiguous and not any(right[0] in ids for ids in others)
        and not any(right[0] in _paragraph_inst_ids(item) for item in before['paragraphs'])
    ):
        return []
    return ['the target paragraph identifier changed beyond one new native assignment']


def _official_delta_problems(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    target_index: int,
    native_level: int,
    define: bool,
    target_written: bool = False,
) -> list[str]:
    problems = _raw_field_problems(before, after, ('header_footer', 'body_sha256', 'styles_sha256', 'bindata_sha256', 'media_sha256'))
    if not define:
        problems.extend(_raw_field_problems(before, after, ('numberings_sha256',)))
    if before['frame']['sha256'] != after['frame']['sha256']:
        problems.append('the document body frame changed')
    if _same_paragraph_count(before, after, problems):
        for index, (left, right) in enumerate(zip(before['paragraphs'], after['paragraphs'])):
            if left['text_sha256'] != right['text_sha256']:
                problems.append(f'paragraph {index} text changed')
            elif index == target_index:
                if left['masked_sha256'] != right['masked_sha256']:
                    problems.append('the target paragraph changed beyond its numbering')
                problems.extend(_target_inst_id_problems(before, after, target_index=target_index, target_written=target_written))
                if right['heading'] != 'OUTLINE' or right['level'] != native_level:
                    problems.append('the target paragraph is not an outline paragraph at the requested level')
            elif left['sha256'] != right['sha256']:
                problems.append(f'paragraph {index} outside the target changed')
    problems.extend(_official_definition_problems(before['definitions'], after['definitions'], define=define))
    shapes_before, shapes_after = before['outline_shapes'], after['outline_shapes']
    if len(shapes_before) != len(shapes_after):
        problems.append('the section outline definition references changed in number')
    else:
        differing = sum(1 for left, right in zip(shapes_before, shapes_after) if left != right)
        if differing > (1 if define else 0):
            problems.append('section outline definition references changed beyond the requested definition')
    return problems


def _official_definition_problems(
    before: Mapping[str, list[tuple[str, str]]], after: Mapping[str, list[tuple[str, str]]], *, define: bool
) -> list[str]:
    problems: list[str] = []
    for name in sorted(set(before) | set(after)):
        removed, added = _multiset_delta(before.get(name, []), after.get(name, []))
        if not removed and not added:
            continue
        if name == 'parashapelist':
            if removed or len(added) > 1:
                problems.append(f'{name} lost entries or gained more than the target paragraph shape')
        elif name == 'numberinglist' and define:
            replaced_in_place = len(removed) == 1 and len(added) == 1 and removed[0][1] == added[0][1]
            added_only = not removed and len(added) <= 1
            if not (replaced_in_place or added_only):
                problems.append(f'{name} changed beyond the requested four-level outline definition')
        else:
            problems.append(f'{name} changed')
    return problems


def _expected_page_number_props(request: Mapping[str, Any]) -> dict[str, int]:
    return {
        'DrawPos': request['draw_pos'],
        'NewNumber': request['start_number'],
        'SideChar': request['side_char_code'],
        'NumberFormat': request['number_format'],
    }


def _restore_caret(hwp: Any, pos: tuple[int, int, int] | None, warnings: list[str]) -> None:
    if pos is None:
        warnings.append('caret position could not be read before the edit, so it was not restored')
        return
    try:
        _set_pos(hwp, pos)
    except Exception as exc:
        warnings.append(f'caret could not be restored to {list(pos)}: exception of class {_exception_class_label(exc)}')


_TYPE_NAME = type.__dict__['__name__'].__get__
_TYPE_MRO = type.__dict__['__mro__'].__get__
_BUILTIN_EXCEPTIONS = tuple(obj for obj in vars(builtins).values() if type(obj) is type and issubclass(obj, BaseException))


def _exception_class_label(exc: BaseException) -> str:
    """Fixed label of an exception's class: a builtin exception name, 'subclass-of:<name>' or 'OTHER'.

    Read with builtin descriptors and identity only, so a hostile exception's __class__, metaclass, repr/str or attributes are never called.
    """
    try:
        cls = type(exc)
        for base in _TYPE_MRO(cls):
            for entry in _BUILTIN_EXCEPTIONS:
                if base is entry:
                    name = _TYPE_NAME(entry)
                    return name if base is cls else 'subclass-of:' + name
    except BaseException:  # noqa: BLE001 - a diagnostic label can never decide the error that is raised
        pass
    return 'OTHER'


_FIXED_REASON_SCHEMA = 'page-number-fixed-reason/1'


def _fixed_primitive(value: Any) -> Any:
    """Exact bool/None/bounded int pass through; every other returned value becomes a fixed type label."""
    kind = type(value)
    if value is None or kind is bool:
        return value
    if kind is int:
        return value if -2147483648 <= value <= 4294967295 else 'int-out-of-range'
    if kind is str:
        return 'str'
    if kind is float:
        return 'float'
    return 'noncanonical'


def _fixed_reason(build: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
    try:
        return build()
    except Exception:  # noqa: BLE001 - fixed metadata can never change which typed error is raised
        return None


def _census_reason(codes: list[str], pgnp_count: int) -> dict[str, Any]:
    reason: dict[str, Any] = {'schema': _FIXED_REASON_SCHEMA, 'judgements': list(codes)}
    if 'pgnp_count_not_one' in codes:
        reason['pgnp_count'] = pgnp_count
    return reason


def _unavailable_reason(observations: list[tuple[str, str, Any]]) -> dict[str, Any]:
    stage, field, value = observations[0]
    reason: dict[str, Any] = {'schema': _FIXED_REASON_SCHEMA, 'judgements': ['readback_unavailable'], 'stage': stage, 'field': field}
    if stage == 'validate':
        reason['observed'] = _fixed_primitive(value)
    else:
        reason['exception_class'] = _exception_class_label(value)
    return reason


def _mismatch_reason(code: str, values: dict[str, Any]) -> dict[str, Any]:
    return {'schema': _FIXED_REASON_SCHEMA, 'judgements': [code], 'values': {name: _fixed_primitive(value) for name, value in values.items()}}


_OFFICIAL_FIXED_REASON_SCHEMA = 'official-numbering-fixed-reason/1'


def _official_reason(codes: list[str], *, exception: BaseException | None = None, **values: Any) -> dict[str, Any]:
    """Fixed judgement codes chosen where the product adjudicates; values are already-returned primitives, never text."""
    reason: dict[str, Any] = {'schema': _OFFICIAL_FIXED_REASON_SCHEMA, 'judgements': list(codes)}
    if values:
        reason['values'] = {name: _fixed_primitive(value) for name, value in values.items()}
    if exception is not None:
        reason['exception_class'] = _exception_class_label(exception)
    return reason


def apply_page_number(hwp: Any, request: Any, *, protection: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Insert one page number control.

    Once ``PageNumPos`` was issued, an ordinary failure anywhere afterwards (including raw readback, census or
    comparison errors) is raised as ``NativeNumberingMutationError`` with the original as ``__cause__``; a
    ``BaseException`` (for example ``KeyboardInterrupt``/``SystemExit``) propagates unchanged, tagged as unknown.
    """
    reach = _MutationReach()
    try:
        return _apply_page_number(hwp, request, protection=protection, reach=reach)
    except NativeNumberingMutationError:
        raise
    except Exception as exc:
        if not reach.reached:
            raise
        raise NativeNumberingMutationError(
            f'page number insertion failed with an exception of class {_exception_class_label(exc)} after the native PageNumPos call was issued; '
            'the document state is not proven and nothing was rolled back'
        ) from exc
    except BaseException as exc:
        if reach.reached:
            _mark_noncomplete(exc)
        raise


def _apply_page_number(hwp: Any, request: Any, *, protection: Callable[[], Any] | None, reach: '_MutationReach') -> dict[str, Any]:
    req = normalize_page_number_request(request)
    haction = _require_haction(hwp, 'GetDefault', 'Execute')
    page_set_root = getattr(hwp.HParameterSet, 'HPageNumPos', None)
    if page_set_root is None or getattr(page_set_root, 'HSet', None) is None:
        raise NativeNumberingRefusal('native HParameterSet.HPageNumPos is unavailable on this machine', code='native_unavailable')

    text_hash, controls = _preflight_census(hwp)
    ids = [ctrl_id for _ctrl, ctrl_id in controls]
    if ids.count('secd') != 1:
        raise NativeNumberingRefusal(
            f'page numbers are applied only to documents with exactly one section; found {ids.count("secd")} section controls',
            code='section_count_unprovable',
        )

    existing = [ctrl for ctrl, ctrl_id in controls if ctrl_id == 'pgnp']
    expected = _expected_page_number_props(req)
    if len(existing) > 1:
        raise NativeNumberingRefusal(
            f'document already has {len(existing)} page number controls; refusing to add or rewrite any',
            code='existing_page_number',
        )
    if len(existing) == 1:
        current = _page_number_props(existing[0])
        if current is not None and current == expected:
            return _page_number_result(
                req,
                changed=False,
                status='already_present_identical',
                readback={'available': True, 'values': current},
                warnings=[],
                protection_info={'checked': False, 'compared': [], 'unproven': []},
            )
        detail = 'its settings could not be read' if current is None else f'it differs from the request ({current})'
        raise NativeNumberingRefusal(
            f'document already has a page number control and {detail}; refusing to duplicate or overwrite it',
            code='existing_page_number',
        )

    header_footer_count = sum(1 for ctrl_id in ids if ctrl_id in {'head', 'foot'})
    if header_footer_count and protection is None:
        raise NativeNumberingRefusal(
            'document has a header or footer and no document protection reader is available, so its content cannot '
            'be proven unchanged by PageNumPos; refusing before any mutation',
            code='header_footer_unprovable',
        )
    before_census: dict[str, Any] | None = None
    if protection is not None:
        try:
            before_census = _read_protection(protection)
        except Exception as exc:
            raise NativeNumberingRefusal(
                f'document protection census could not be read before editing: exception of class {_exception_class_label(exc)}',
                code='protection_unavailable',
            ) from exc
        header_footer = before_census['header_footer']
        if header_footer_count or header_footer:
            if header_footer is None or len(header_footer) != header_footer_count:
                raise NativeNumberingRefusal(
                    f'header/footer content could not be fingerprinted for the {header_footer_count} head/foot control(s) found; '
                    'refusing before any mutation',
                    code='header_footer_unprovable',
                )
        _require_complete(before_census)

    mover = getattr(hwp, 'MoveDocBegin', None)
    if not callable(mover):
        raise NativeNumberingRefusal('MoveDocBegin is unavailable on this machine', code='native_unavailable')

    warnings: list[str] = []
    caret = _get_pos(hwp)
    try:
        mover()
        hset = hwp.HParameterSet.HPageNumPos
        defaults_result = haction.GetDefault('PageNumPos', hset.HSet)
    except Exception as exc:
        _restore_caret(hwp, caret, warnings)
        raise NativeNumberingRefusal(
            f'native PageNumPos defaults could not be read: exception of class {_exception_class_label(exc)}',
            code='native_defaults_unavailable',
        ) from exc
    if not _action_confirmed(defaults_result):
        _restore_caret(hwp, caret, warnings)
        raise NativeNumberingRefusal(
            f'native PageNumPos defaults could not be read (GetDefault returned {_status_text(defaults_result)})',
            code='native_defaults_unavailable',
        )

    try:
        hset.DrawPos = req['draw_pos']
        hset.NumberFormat = req['number_format']
        hset.NewNumber = req['start_number']
        hset.SideChar = req['side_char_code']
    except Exception as exc:
        _restore_caret(hwp, caret, warnings)
        raise NativeNumberingRefusal(
            f'native PageNumPos parameters could not be set: exception of class {_exception_class_label(exc)}',
            code='native_parameters_unavailable',
        ) from exc

    # From here the outcome may be unknown, so no failure path below moves the caret.
    reach.reached = True
    try:
        execute_result = haction.Execute('PageNumPos', hset.HSet)
    except Exception as exc:
        raise NativeNumberingMutationError(
            f'native PageNumPos raised an exception of class {_exception_class_label(exc)}; '
            'the document may or may not have been changed and was not rolled back'
        ) from exc
    if not _action_confirmed(execute_result):
        raise NativeNumberingMutationError(
            f'native PageNumPos returned {_status_text(execute_result)} instead of the documented BOOL True; '
            'the document may or may not have been changed and was not rolled back'
        )

    try:
        after_hash, after_controls = _preflight_census(hwp)
    except NativeNumberingRefusal as exc:
        raise NativeNumberingMutationError(
            f'page number outcome could not be verified after the native call: preflight census refused ({exc.code})'
        ) from exc
    after_ids = [ctrl_id for _ctrl, ctrl_id in after_controls]
    problems: list[str] = []
    codes: list[str] = []
    if after_hash != text_hash:
        problems.append('document text changed')
        codes.append('document_text_changed')
    new_pgnp = [ctrl for ctrl, ctrl_id in after_controls if ctrl_id == 'pgnp']
    if len(new_pgnp) != 1:
        problems.append(f'expected exactly one pgnp control after insertion but found {len(new_pgnp)}')
        codes.append('pgnp_count_not_one')
    other_ids = [ctrl_id for ctrl_id in after_ids if ctrl_id != 'pgnp']
    restart_control = False
    if other_ids != ids:
        # Exactly one new nwno beside the unchanged others may be the requested restart, and only with document-level evidence.
        restart_control = (
            before_census is not None
            and 'nwno' not in ids
            and other_ids.count('nwno') == 1
            and [ctrl_id for ctrl_id in other_ids if ctrl_id != 'nwno'] == ids
        )
        if not restart_control:
            problems.append('the set of other controls changed')
            codes.append('other_controls_changed')
    if problems:
        raise NativeNumberingMutationError(
            'page number outcome is not as requested: ' + '; '.join(problems) + '; the document was not rolled back',
            fixed_reason=_fixed_reason(lambda: _census_reason(codes, len(new_pgnp))),
        )

    readback_diagnostic: list[tuple[str, str, Any]] = []
    values = _page_number_props(new_pgnp[0], _diagnostic=readback_diagnostic, allow_unset_new_number=True)
    if values is None:
        error = NativeNumberingMutationError(
            'the inserted page number control settings could not be read back as documented integers, so the final '
            'state is unknown; the document was not rolled back',
            fixed_reason=_fixed_reason(lambda: _unavailable_reason(readback_diagnostic)),
        )
        try:
            error.args = (_page_number_readback_detail(readback_diagnostic) + '; ' + error.args[0],)
        except BaseException:
            pass  # The typed mutation-unknown error already exists; diagnostics cannot release it.
        raise error
    new_number_unset = values['NewNumber'] is None
    if new_number_unset:
        if any(values[name] != expected[name] for name in values if name != 'NewNumber'):
            raise NativeNumberingMutationError(
                f'inserted page number control reads back {values}, not the requested {expected}; the document was not rolled back',
                fixed_reason=_fixed_reason(lambda: _mismatch_reason('readback_values_differ_new_number_unset', values)),
            )
        if before_census is None:
            raise NativeNumberingMutationError(
                'the inserted page number control returned no NewNumber and no document protection evidence is available to confirm '
                'the requested start number, so the final state is unknown; the document was not rolled back'
            )
    elif values != expected:
        raise NativeNumberingMutationError(
            f'inserted page number control reads back {values}, not the requested {expected}; the document was not rolled back',
            fixed_reason=_fixed_reason(lambda: _mismatch_reason('readback_values_differ', values)),
        )

    readback: dict[str, Any] = {'available': True, 'values': values}
    protection_info: dict[str, Any] = {'checked': False, 'compared': [], 'unproven': []}
    if protection is not None and before_census is not None:
        try:
            after_census = _read_protection(protection)
        except Exception as exc:
            raise NativeNumberingMutationError(
                'document protection census could not be read after the native call: '
                f'exception of class {_exception_class_label(exc)}; the document was not rolled back'
            ) from exc
        lost = _incomplete(after_census)
        census_problems = (
            ['preservation evidence is incomplete after the native call (' + '; '.join(lost) + ')']
            if lost
            else _page_number_delta_problems(
                before_census, after_census, request_start=req['start_number'], require_restart=new_number_unset or restart_control
            )
        )
        if census_problems:
            raise NativeNumberingMutationError(
                'document state outside the new page number is not proven unchanged: '
                + '; '.join(census_problems)
                + '; the document was not rolled back'
            )
        protection_info = {
            'checked': True,
            'compared': list(_PAGE_NUMBER_COMPARED),
            'unproven': [],
            'page_number_proof': (
                'control_chain+hwpml2x_page_restart' if new_number_unset or restart_control else 'control_chain'
            ),
        }
        if new_number_unset:
            readback = {
                'available': True,
                'values': {**values, 'NewNumber': req['start_number']},
                'new_number_source': 'hwpml2x_page_restart',
            }

    _restore_caret(hwp, caret, warnings)
    return _page_number_result(
        req, changed=True, status='inserted', readback=readback, warnings=warnings, protection_info=protection_info
    )


def _page_number_result(
    req: Mapping[str, Any],
    *,
    changed: bool,
    status: str,
    readback: dict[str, Any],
    warnings: list[str],
    protection_info: dict[str, Any],
) -> dict[str, Any]:
    return {
        'changed': changed,
        'status': status,
        'position': req['position'],
        'start_number': req['start_number'],
        'side_char': req['side_char'],
        'number_format': 'digit',
        'readback': readback,
        'protection': protection_info,
        'warnings': list(warnings),
    }


def _read_paragraph_shape(hwp: Any, haction: Any) -> tuple[Any, int, int | None]:
    hset = hwp.HParameterSet.HParaShape
    try:
        result = haction.GetDefault('ParagraphShape', hset.HSet)
    except Exception as exc:
        raise NativeNumberingRefusal(
            f'current paragraph shape could not be read: exception of class {_exception_class_label(exc)}',
            code='paragraph_shape_unreadable',
        ) from exc
    if not _action_confirmed(result):
        raise NativeNumberingRefusal(
            f'current paragraph shape could not be read (GetDefault returned {_status_text(result)})', code='paragraph_shape_unreadable'
        )
    heading = getattr(hset, 'HeadingType', None)
    if isinstance(heading, bool) or not isinstance(heading, int) or heading not in {0, 1, 2, 3}:
        raise NativeNumberingRefusal('current paragraph HeadingType could not be read', code='paragraph_shape_unreadable')
    level = getattr(hset, 'Level', None)
    if isinstance(level, bool) or not isinstance(level, int):
        level = None
    return hset, heading, level


def _outline_hset(hwp: Any) -> Any:
    root = getattr(hwp.HParameterSet, 'HSecDef', None)
    if root is None or getattr(root, 'HSet', None) is None:
        raise NativeNumberingRefusal('native HParameterSet.HSecDef is unavailable on this machine', code='native_unavailable')
    return root


def _read_outline_definition(hwp: Any, haction: Any) -> list[dict[str, Any]]:
    """Read the first four outline levels of the caret section; only documented str/UI1 values count."""
    hset = _outline_hset(hwp)
    try:
        result = haction.GetDefault('OutlineNumber', hset.HSet)
    except Exception as exc:
        raise NativeNumberingRefusal(
            f'the section outline definition could not be read: exception of class {_exception_class_label(exc)}',
            code='outline_definition_unreadable',
        ) from exc
    if not _action_confirmed(result):
        raise NativeNumberingRefusal(
            f'the section outline definition could not be read (GetDefault returned {_status_text(result)})',
            code='outline_definition_unreadable',
        )
    shape = getattr(hset, 'OutlineShape', None)
    levels: list[dict[str, Any]] = []
    for spec in OFFICIAL_OUTLINE_LEVELS:
        index = spec['native_level']
        text = getattr(shape, f'StrFormatLevel{index}', None)
        number_format = getattr(shape, f'NumFormatLevel{index}', None)
        if (
            not isinstance(text, str)
            or isinstance(number_format, bool)
            or not isinstance(number_format, int)
            or number_format < 0
            or number_format > 255
        ):
            raise NativeNumberingRefusal(
                f'outline level {spec["level"]} StrFormatLevel{index}/NumFormatLevel{index} could not be read as str/integer values',
                code='outline_definition_unreadable',
            )
        levels.append({'level': spec['level'], 'str_format': text, 'num_format': number_format})
    return levels


def _outline_matches_official(levels: list[dict[str, Any]]) -> bool:
    return all(
        read['str_format'] == spec['str_format'] and read['num_format'] == spec['num_format']
        for read, spec in zip(levels, OFFICIAL_OUTLINE_LEVELS)
    )


def _census_hits(paragraphs: list[dict[str, Any]] | None, find: str | None, match_case: bool) -> list[int]:
    if paragraphs is None or not isinstance(find, str) or not find:
        return []
    needle = find if match_case else find.casefold()
    return [index for index, item in enumerate(paragraphs) if needle in (item['text'] if match_case else item['text'].casefold())]


def _prove_outline_scope(census: dict[str, Any] | None, target_find: str | None, match_case: bool = True) -> None:
    """Refuse (before mutation) unless redefining the caret section's outline cannot touch another outline paragraph."""
    paragraphs = census['paragraphs'] if census is not None else None
    if paragraphs is None or not target_find:
        raise NativeNumberingRefusal(
            'the section outline definition differs from the official 1. 가. 1) 가) levels and the paragraphs using outline '
            'numbering in this section cannot be inspected, so redefining it is refused',
            code='outline_usage_unprovable',
        )
    hits = _census_hits(paragraphs, target_find, match_case)
    if len(hits) != 1:
        raise NativeNumberingRefusal(
            f'the target paragraph is not uniquely identifiable in the document census ({len(hits)} candidates)',
            code='outline_usage_unprovable',
        )
    target = paragraphs[hits[0]]
    others = [item for item in paragraphs if item is not target and item['section'] == target['section'] and item['heading'] == 'OUTLINE']
    if others:
        raise NativeNumberingRefusal(
            f'the section already uses a different outline definition for {len(others)} other outline paragraph(s); '
            'redefining it would change their numbers, so it is refused',
            code='foreign_outline_definition',
        )
    shared = [item for item in paragraphs if item is not target and item.get('shares_outline_numbering') is True]
    if shared:
        raise NativeNumberingRefusal(
            f'{len(shared)} numbered paragraph(s) use the numbering definition that the section outline uses; '
            'redefining the outline would change their numbers, so it is refused',
            code='foreign_outline_definition',
        )
    unknown = [
        item for item in paragraphs
        if item is not target and item['heading'] == 'NUMBER' and item.get('shares_outline_numbering') is not False
    ]
    if unknown:
        raise NativeNumberingRefusal(
            f'{len(unknown)} numbered paragraph(s) have no readable numbering reference, so it cannot be shown that they '
            'do not use the section outline definition; redefining it is refused',
            code='outline_usage_unprovable',
        )
    elsewhere = [item for item in paragraphs if item['section'] != target['section'] and item['heading'] == 'OUTLINE']
    if elsewhere:
        raise NativeNumberingRefusal(
            f'{len(elsewhere)} outline paragraph(s) lie in another section, and nothing shows that they use a different '
            'outline definition than the target section; redefining it is refused',
            code='outline_usage_unprovable',
        )
    shapes = [shape for shape in census.get('outline_shapes') or () if shape != 'none']
    if len(shapes) != len(set(shapes)):
        raise NativeNumberingRefusal(
            'more than one section references the same outline definition, so redefining it would change another section; '
            'it is refused',
            code='outline_usage_unprovable',
        )


class _MutationReach:
    """Set just before a mutating native call is issued; from then on the document state is unproven."""

    reached = False


def _mark_noncomplete(exc: BaseException) -> None:
    """Tag a control-flow exit (not an Exception) that escaped after a mutating call, keeping the exit unchanged."""
    for name, value in (
        ('mutation_state', 'unknown'),
        ('mutation_may_have_persisted', True),
        ('rollback', {'attempted': False, 'succeeded': False}),
    ):
        try:
            setattr(exc, name, value)
        except Exception:
            pass


def _define_official_outline(hwp: Any, haction: Any, guard: Callable[[bool, str], None], reach: _MutationReach) -> None:
    hset = _outline_hset(hwp)
    # GetDefault already filled the remaining OutlineShape levels, which are written back unchanged.
    def set_item(name: str, value: Any) -> None:
        setter = getattr(hset.HSet, 'SetItem', None)
        if callable(setter) and hset.HSet is not hset:
            setter(name, value)
        else:
            setattr(hset, name, value)

    try:
        set_item('ApplyClass', _OUTLINE_APPLY_CLASS)
        set_item('ApplyTo', _OUTLINE_APPLY_TO_CURRENT_SECTION)
        for spec in OFFICIAL_OUTLINE_LEVELS:
            index = spec['native_level']
            setattr(hset.OutlineShape, f'StrFormatLevel{index}', spec['str_format'])
            setattr(hset.OutlineShape, f'NumFormatLevel{index}', spec['num_format'])
    except Exception as exc:
        raise NativeNumberingRefusal(f'native outline parameters could not be set: {exc}', code='native_parameters_unavailable') from exc
    guard(False, 'before the outline definition')
    reach.reached = True
    try:
        result = haction.Execute('OutlineNumber', hset.HSet)
    except Exception as exc:
        reason = _fixed_reason(lambda: _official_reason(['define_execute_raised'], exception=exc))
        raise NativeNumberingMutationError(
            f'native OutlineNumber raised {exc!r}; the section outline definition may or may not have changed and was not rolled back',
            fixed_reason=reason,
        ) from exc
    if not _action_confirmed(result):
        reason = _fixed_reason(lambda: _official_reason(['define_not_confirmed'], observed=result))
        raise NativeNumberingMutationError(
            f'native OutlineNumber returned {_status_text(result)} instead of the documented BOOL True; '
            'the section outline definition may or may not have changed and was not rolled back',
            fixed_reason=reason,
        )
    try:
        levels = _read_outline_definition(hwp, haction)
    except NativeNumberingRefusal as exc:
        reason = _fixed_reason(lambda: _official_reason(['define_readback_unverified']))
        raise NativeNumberingMutationError(
            f'the section outline definition could not be verified after OutlineNumber: {exc}; nothing was rolled back', fixed_reason=reason
        ) from exc
    if not _outline_matches_official(levels):
        reason = _fixed_reason(lambda: _official_reason(['define_readback_not_official']))
        raise NativeNumberingMutationError(
            f'the section outline definition reads back {levels} after OutlineNumber, not the official levels; nothing was rolled back',
            fixed_reason=reason,
        )


def apply_official_numbering(
    hwp: Any,
    request: Any,
    *,
    protection: Callable[[], Any] | None = None,
    target_find: str | None = None,
    target_match_case: bool = True,
    target_anchor: _TargetState | None = None,
) -> dict[str, Any]:
    """Set/verify the official 4-level outline definition, then number the paragraph holding the caret.

    Once a mutating native call was issued, an ordinary failure anywhere afterwards (including raw readback,
    census or comparison errors) is raised as ``NativeNumberingMutationError`` with the original as ``__cause__``;
    a ``BaseException`` (for example ``KeyboardInterrupt``/``SystemExit``) propagates unchanged, tagged as unknown.
    """
    reach = _MutationReach()
    try:
        return _apply_official_numbering(
            hwp,
            request,
            protection=protection,
            target_find=target_find,
            target_match_case=target_match_case,
            target_anchor=target_anchor,
            reach=reach,
        )
    except NativeNumberingMutationError:
        raise
    except Exception as exc:
        if not reach.reached:
            raise
        reason = _fixed_reason(lambda: _official_reason(['unexpected_exception_after_mutation'], exception=exc))
        raise NativeNumberingMutationError(
            f'outline numbering failed with {exc!r} after a native mutating call was issued; '
            'the document state is not proven and nothing was rolled back',
            fixed_reason=reason,
        ) from exc
    except BaseException as exc:
        if reach.reached:
            _mark_noncomplete(exc)
        raise


def _apply_official_numbering(
    hwp: Any,
    request: Any,
    *,
    protection: Callable[[], Any] | None,
    target_find: str | None,
    target_match_case: bool,
    target_anchor: _TargetState | None,
    reach: _MutationReach,
) -> dict[str, Any]:
    req = normalize_official_numbering_request(request)
    haction = _require_haction(hwp, 'GetDefault', 'Execute')
    native_level = req['native_level']

    anchor = target_anchor if target_anchor is not None else _anchor_target(hwp)
    guard = _target_guard(hwp, anchor)
    text_hash, controls = _preflight_census(hwp)
    ids = [ctrl_id for _ctrl, ctrl_id in controls]
    before_census: dict[str, Any] | None = None
    target_index: int | None = None
    if protection is not None:
        try:
            before_census = _read_protection(protection)
        except Exception as exc:
            raise NativeNumberingRefusal(
                f'document protection census could not be read before editing: {exc}', code='protection_unavailable'
            ) from exc
        _require_complete(before_census)
        hits = _census_hits(before_census['paragraphs'], target_find, target_match_case)
        if len(hits) != 1:
            raise NativeNumberingRefusal(
                f'the target paragraph is not uniquely identifiable in the document census ({len(hits)} candidates), '
                'so its numbering change cannot be told apart from other paragraphs; refusing before any mutation',
                code='target_unidentifiable',
            )
        target_index = hits[0]
    guard(False, 'after the document census')
    definition = _read_outline_definition(hwp, haction)
    format_matches = _outline_matches_official(definition)
    _hset, heading, level = _read_paragraph_shape(hwp, haction)

    if heading in {2, 3}:
        kind = 'paragraph number' if heading == 2 else 'bullet'
        raise NativeNumberingRefusal(
            f'target paragraph already has a native {kind} list; outline numbering would replace it, so it is refused',
            code='existing_number_or_bullet',
        )
    if heading == _HEADING_OUTLINE:
        if level is None:
            raise NativeNumberingRefusal('target paragraph is an outline paragraph whose level could not be read', code='paragraph_shape_unreadable')
        if level == native_level and format_matches:
            guard(False, 'while reading the existing numbering')
            return _official_result(
                req, changed=False, status='already_applied', actions=[], format_status='verified_existing', definition=definition
            )

    actions: list[str] = []
    defined = False
    status = 'level_changed' if heading == _HEADING_OUTLINE else 'applied'
    format_status = 'verified_existing'
    if not format_matches:
        _prove_outline_scope(before_census, target_find, target_match_case)
        actions.append('Execute:OutlineNumber(ApplyTo=current section,levels 1-4)')
        _define_official_outline(hwp, haction, guard, reach)
        defined = True
        format_status = 'defined_in_current_section'
        status = 'format_defined' if heading == _HEADING_OUTLINE and level == native_level else status
        try:
            _hset, redefined_heading, redefined_level = _read_paragraph_shape(hwp, haction)
        except NativeNumberingRefusal as exc:
            reason = _fixed_reason(lambda: _official_reason(['paragraph_unverified_after_define']))
            raise NativeNumberingMutationError(
                f'paragraph state could not be verified after the outline definition: {exc}; nothing was rolled back', fixed_reason=reason
            ) from exc
        if redefined_heading != heading or redefined_level != level:
            reason = _fixed_reason(
                lambda: _official_reason(
                    ['paragraph_changed_during_define'],
                    heading_before=heading,
                    level_before=level,
                    heading_after=redefined_heading,
                    level_after=redefined_level,
                )
            )
            raise NativeNumberingMutationError(
                f'the paragraph changed from HeadingType={heading}/Level={level} to {redefined_heading}/{redefined_level} '
                'while its outline was defined; nothing was rolled back',
                fixed_reason=reason,
            )
    make_outline = heading == _HEADING_NONE
    paragraph_written = make_outline or level != native_level
    if paragraph_written:
        actions.append(
            f'Execute:ParagraphShape(HeadingType={_HEADING_OUTLINE},Level={native_level})'
            if make_outline
            else f'Execute:ParagraphShape(Level={native_level})'
        )
        try:
            if make_outline:
                _hset.HeadingType = _HEADING_OUTLINE
            _hset.Level = native_level
        except Exception as exc:
            reason = _fixed_reason(lambda: _official_reason(['paragraph_shape_set_raised'], exception=exc))
            raise NativeNumberingMutationError(
                f'native ParagraphShape raised {exc!r}; the paragraph may or may not have been changed and nothing was rolled back',
                fixed_reason=reason,
            ) from exc
        guard(defined, 'before the paragraph write')
        reach.reached = True
        try:
            execute_result = haction.Execute('ParagraphShape', _hset.HSet)
        except Exception as exc:
            reason = _fixed_reason(lambda: _official_reason(['paragraph_shape_execute_raised'], exception=exc))
            raise NativeNumberingMutationError(
                f'native ParagraphShape raised {exc!r}; the paragraph may or may not have been changed and nothing was rolled back',
                fixed_reason=reason,
            ) from exc
        if not _action_confirmed(execute_result):
            reason = _fixed_reason(lambda: _official_reason(['paragraph_shape_not_confirmed'], observed=execute_result))
            raise NativeNumberingMutationError(
                f'native ParagraphShape returned {_status_text(execute_result)} instead of the documented BOOL True; '
                'the paragraph may or may not have been changed and nothing was rolled back',
                fixed_reason=reason,
            )

    try:
        _hset, final_heading, final_level = _read_paragraph_shape(hwp, haction)
        final_hash, final_controls = _preflight_census(hwp)
        final_definition = _read_outline_definition(hwp, haction)
    except NativeNumberingRefusal as exc:
        reason = _fixed_reason(lambda: _official_reason(['outcome_unverified']))
        raise NativeNumberingMutationError(
            f'outline numbering outcome could not be verified: {exc}; nothing was rolled back', fixed_reason=reason
        ) from exc
    problems: list[str] = []
    codes: list[str] = []
    values: dict[str, Any] = {}
    if not _outline_matches_official(final_definition):
        problems.append('the section outline definition is not the official 1. 가. 1) 가) levels')
        codes.append('outline_definition_not_official')
    census_text_preserved = False
    protection_info: dict[str, Any] = {'checked': False, 'compared': [], 'unproven': []}
    if before_census is not None and protection is not None:
        try:
            after_census = _read_protection(protection)
        except Exception as exc:
            reason = _fixed_reason(lambda: _official_reason(['protection_read_after_failed'], exception=exc))
            raise NativeNumberingMutationError(
                f'document protection census could not be read after the native call: {exc}; nothing was rolled back',
                fixed_reason=reason,
            ) from exc
        lost = _incomplete(after_census)
        if lost:
            problems.append('preservation evidence is incomplete after the native call (' + '; '.join(lost) + ')')
            codes.append('preservation_evidence_incomplete')
        else:
            assert target_index is not None
            delta = _official_delta_problems(
                before_census,
                after_census,
                target_index=target_index,
                native_level=native_level,
                define=not format_matches,
                target_written=paragraph_written,
            )
            problems.extend(delta)
            if delta:
                codes.append('census_delta_mismatch')
            # UNICODE is a native plain-text export, not a literal-content oracle
            # after numbering. Admit its delta only on the demonstrated paragraph-
            # only path with complete, target-specific structural preservation.
            # Definition-changing paths and absent/legacy evidence stay strict.
            census_text_preserved = (
                not defined and not delta
                and _official_text_evidence_complete(before_census)
                and _official_text_evidence_complete(after_census)
            )
        protection_info = {'checked': True, 'compared': list(_OFFICIAL_COMPARED), 'unproven': []}
    if final_heading != _HEADING_OUTLINE:
        problems.append(f'HeadingType is {final_heading}')
        codes.append('heading_type_mismatch')
        values['final_heading'] = final_heading
    if final_level != native_level:
        problems.append(f'native level reads {final_level} instead of {native_level}')
        codes.append('level_mismatch')
        values.update(final_level=final_level, native_level=native_level)
    if final_hash != text_hash and not census_text_preserved:
        problems.append('document text changed without complete structural preservation proof')
        codes.append('text_hash_mismatch')
    if [ctrl_id for _ctrl, ctrl_id in final_controls] != ids:
        problems.append('the main-list control sequence changed')
        codes.append('control_sequence_mismatch')
    if problems:
        reason = _fixed_reason(lambda: _official_reason(codes, **values))
        raise NativeNumberingMutationError(
            'outline numbering outcome is not as requested: ' + '; '.join(problems) + '; nothing was rolled back',
            fixed_reason=reason,
        )
    return _official_result(
        req, changed=True, status=status, actions=actions, format_status=format_status, definition=final_definition, protection_info=protection_info
    )


def _official_result(
    req: Mapping[str, Any],
    *,
    changed: bool,
    status: str,
    actions: list[str],
    format_status: str,
    definition: list[dict[str, Any]],
    protection_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        'changed': changed,
        'status': status,
        'level': req['level'],
        'native_level': req['native_level'],
        'numbering_format_enforced': True,
        'numbering_format': {'status': format_status, 'levels': [dict(item) for item in definition]},
        'protection': protection_info if protection_info is not None else {'checked': False, 'compared': [], 'unproven': []},
        'native_actions': list(actions),
    }


def _locate_unique_body_paragraph(hwp: Any, find: str, *, match_case: bool) -> tuple[int, int]:
    mover = getattr(hwp, 'MoveDocBegin', None)
    finder = getattr(hwp, 'find', None)
    selected_getter = getattr(hwp, 'get_selected_pos', None) or getattr(hwp, 'GetSelectedPos', None)
    if not callable(mover) or not callable(finder) or not callable(selected_getter):
        raise NativeNumberingRefusal('text search is unavailable on this machine', code='native_unavailable')
    advance = getattr(hwp, 'MoveRight', None)
    if not callable(advance):
        raise NativeNumberingRefusal('MoveRight is unavailable on this machine', code='native_unavailable')

    found: list[tuple[int, int]] = []
    mover()
    for _ in range(_MAX_FIND_HITS):
        if not finder(find, direction='Forward', MatchCase=1 if match_case else 0, WholeWordOnly=0):
            break
        try:
            selected = _parse_selected_pos(selected_getter())
        except Exception:
            selected = None
        if not selected:
            raise NativeNumberingRefusal('a text match could not be located by position', code='target_unlocatable')
        slist, spara, elist, epara = selected[0], selected[1], selected[3], selected[4]
        if slist != elist or spara != epara:
            raise NativeNumberingRefusal('the text match spans more than one paragraph', code='target_unlocatable')
        key = (slist, spara)
        if key not in found:
            found.append(key)
        advance()
    else:
        raise NativeNumberingRefusal('text search did not terminate', code='target_unlocatable')

    if not found:
        raise NativeNumberingRefusal(f'target text {find!r} was not found', code='target_not_found')
    if len(found) > 1:
        raise NativeNumberingRefusal(
            f'target text {find!r} appears in {len(found)} paragraphs; refusing to guess which one to number',
            code='target_ambiguous',
        )
    if found[0][0] != 0:
        raise NativeNumberingRefusal('the target paragraph is not in the main body text list (for example a table cell)', code='target_not_in_body')
    return found[0]


def apply_official_numbering_at_match(
    hwp: Any,
    *,
    find: str,
    level: Any,
    match_case: bool = True,
    protection: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    req = normalize_official_numbering_request({'level': level})
    if not isinstance(find, str) or find == '' or '\n' in find or '\r' in find:
        raise NativeNumberingInputError('find must be a non-empty single-line string')
    if not isinstance(match_case, bool):
        raise NativeNumberingInputError('match_case must be true or false')
    _require_haction(hwp, 'GetDefault', 'Execute')

    list_id, para = _locate_unique_body_paragraph(hwp, find, match_case=match_case)
    try:
        _set_pos(hwp, (list_id, para, 0))
    except Exception as exc:
        raise NativeNumberingRefusal(f'the caret could not be moved to the target paragraph: {exc}', code='target_unlocatable') from exc
    anchor = _anchor_target(hwp, expected=(list_id, para))

    result = apply_official_numbering(
        hwp,
        {'level': req['level']},
        protection=protection,
        target_find=find,
        target_match_case=match_case,
        target_anchor=anchor,
    )
    result['matches'] = 1
    return result


def page_number_request_from_op(op: Mapping[str, Any]) -> dict[str, Any]:
    return {key: op[key] for key in PAGE_NUMBER_REQUEST_KEYS if key in op}


STEP_MATCH_MAX_CHARS = 500


def _require_confirm_layout(step: Mapping[str, Any], name: str) -> None:
    if step.get('confirm_layout') is not True:
        raise NativeNumberingInputError(f'{name} requires confirm_layout=true')


_STEP_ENVELOPE_KEYS = ('op', 'operation', 'label', 'confirm_layout')


def validate_page_number_step(step: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a page_number_apply bundle step and return the explicit native request."""
    _reject_unknown_keys(step, PAGE_NUMBER_REQUEST_KEYS + _STEP_ENVELOPE_KEYS, 'page_number_apply step')
    _require_confirm_layout(step, 'page_number_apply')
    request = page_number_request_from_op(step)
    normalize_page_number_request(request)
    return request


def validate_official_numbering_step(step: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an official_numbering_apply bundle step; returns match, expected_page and level."""
    _reject_unknown_keys(step, ('match', 'expected_page', 'level') + _STEP_ENVELOPE_KEYS, 'official_numbering_apply step')
    _require_confirm_layout(step, 'official_numbering_apply')
    match = step.get('match')
    if not isinstance(match, str) or not match.strip():
        raise NativeNumberingInputError('official_numbering_apply requires non-empty match text')
    match = match.strip()
    if len(match) > STEP_MATCH_MAX_CHARS:
        raise NativeNumberingInputError(f'match is too long (max {STEP_MATCH_MAX_CHARS} characters)')
    if '\n' in match or '\r' in match:
        raise NativeNumberingInputError('match must be a single line of text')
    expected_page = step.get('expected_page')
    if isinstance(expected_page, bool) or not isinstance(expected_page, int) or expected_page < 1:
        raise NativeNumberingInputError('expected_page must be a positive integer')
    request = normalize_official_numbering_request({'level': step.get('level')} if 'level' in step else {})
    return {'match': match, 'expected_page': expected_page, 'level': request['level']}
