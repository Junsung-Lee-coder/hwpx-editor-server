"""Fixed, value-free labels for ONE failed public open; never native evidence.

No exception formatting, traceback, args, HRESULT, path or document reads. The
first capsule survives the runtime Future/wrapper and service error boundary.
"""
STAGES = frozenset({
    'open_dispatch',
    'session_layout',
    'upload_write', 'working_copy', 'interactive_record', 'runtime_open',
    'binding_commit', 'runtime_session_create', 'runtime_initialize',
    'runtime_sdk_import', 'runtime_preflight', 'runtime_process_baseline',
    'runtime_constructor', 'runtime_owner_binding', 'runtime_post_constructor',
    'runtime_watchdog', 'runtime_document_open', 'runtime_ready_report',
    'runtime_location', 'runtime_start_result',
})
CATEGORIES = frozenset({'permission_error','file_not_found','os_error','type_error',
                       'value_error','attribute_error','import_error','timeout_error',
                       'runtime_error','other'})
_KEY = '_local_cli_open_failure'
_EXCEPTION_DICT = BaseException.__dict__['__dict__']


class _Failure(tuple):
    __slots__ = ()

    def __new__(cls, stage, category, prior=None):
        # Retain the old mapping, including a replaced foreign slot, so the
        # metadata update cannot eagerly run its/value's destructor. No registry.
        return tuple.__new__(cls, (stage, category, prior))


def public_failure(value):
    """Inspect three builtin items before any key/value hashing or equality."""
    if type(value) is not dict or dict.__len__(value) != 3:
        return None
    fields = {}
    for key, item in dict.items(value):
        if type(key) is not str or type(item) is not str:
            return None
        if key not in ('code', 'stage', 'exception_class'):
            return None
        fields[key] = item  # fresh exact dict; only exact string keys/values
    if (len(fields) != 3 or fields['code'] != 'open_failed'
            or fields['stage'] not in STAGES or fields['exception_class'] not in CATEGORIES):
        return None
    return fields


def _exception_items(exc):
    if not issubclass(type(exc), BaseException):
        return None
    state = _EXCEPTION_DICT.__get__(exc)
    # BaseException permits a dict subclass. Use only builtin entry iteration,
    # bounded by its stored entry count, not its methods or keyed lookups.
    if not issubclass(type(state), dict):
        return None
    return state, tuple(dict.items(state))


def _read_items(items):
    for key, value in items:
        if type(key) is str and key == _KEY and type(value) is _Failure:
            if tuple.__len__(value) == 3:
                return public_failure({'code': 'open_failed', 'stage': tuple.__getitem__(value, 0),
                                       'exception_class': tuple.__getitem__(value, 1)})
    return None


def failure_from_exception(exc):
    try:
        snapshot = _exception_items(exc)
        return _read_items(snapshot[1]) if snapshot is not None else None
    except BaseException:
        # Optional metadata failure must not replace the primary operation.
        return None


def record_failure(exc, stage, *, origin=None):
    try:
        snapshot = _exception_items(exc)
        if snapshot is None:
            return
        prior, items = snapshot
        if _read_items(items) is not None:
            return  # preserve the first validated capsule, even with foreign siblings
        # Do not purge foreign attributes, rebuild/hash their keys or insert into
        # their live dictionary. Metadata is unavailable on this unsafe shape.
        if any(type(key) is not str for key, value in items):
            return
        first = failure_from_exception(origin if origin is not None else
                                       BaseException.__dict__['__context__'].__get__(exc))
        if first is not None:
            stage, category = first['stage'], first['exception_class']
        else:
            if type(stage) is not str or stage not in STAGES:
                return
            source = exc if origin is None else origin
            category = 'other'
            for cls, label in ((PermissionError,'permission_error'),(FileNotFoundError,'file_not_found'),
                               (TimeoutError,'timeout_error'),(OSError,'os_error'),(TypeError,'type_error'),
                               (ValueError,'value_error'),(AttributeError,'attribute_error'),
                               (ImportError,'import_error'),(RuntimeError,'runtime_error')):
                if issubclass(type(source), cls):
                    category=label;break
        # A private snapshot cannot gain a foreign colliding key between checking
        # and storing. Preserve every original attribute and prior reference;
        # never dispatch subclass setitem or drop an unknown value on replacement.
        state = {key: value for key, value in items}
        state[_KEY] = _Failure(stage, category, prior)
        _EXCEPTION_DICT.__set__(exc, state)
    except BaseException:
        return  # no callback was authorized; metadata is not Future/outcome authority
