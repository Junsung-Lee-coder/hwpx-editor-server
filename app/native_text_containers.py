"""Dedicated native text containers. HWPML is evidence only, never written.

Source contracts from Hancom's 2504 tables and pyhwpx 1.7.2. All variants
remain native-unqualified until an owned edit/save/reopen/render case passes.
Only one native list per owner and one plain paragraph are editable. A leading
native note/caption AUTONUM is retained outside the text range.
"""
from __future__ import annotations
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from app import native_format as nf, native_numbering as nn
from app import text_container_proof as proof
from app.native_numbering_census import (
    CensusError, MAX_HWPML_CHARS, _DOCTYPE, _MAX_DEPTH, _Resolver, _Unresolved, _attr, _name,
)

OPS = ('text_container_inventory', 'text_container_read', 'text_container_text_exact', 'text_box_create')
ENVELOPE = {'op', 'operation', 'label'}
# Observational labels only: never admission, restoration, retry or close authority.
# These are the existing source call/check names, plus the pure parser's phases.
NATIVE_READ_SITES = frozenset('container.' + site for site in (
    'raw_interface', 'GetTextFile', 'GetTextFile.block', 'GetPos', 'GetPos.recheck',
    'GetSelectedPos', 'SelectionMode', 'SelectionMode.recheck',
    'CurSelectedCtrl', 'CurSelectedCtrl.empty', 'selected.CtrlID',
    'selected.GetCtrlInstID', 'selected.identity', 'linear_state_required',
    'read_purity', 'insertion_state', 'constructor.state', 'constructor.identity',
    'constructor.Cancel', 'constructor.Front', 'constructor.TextBoxEdit',
    'constructor.preservation', 'ParentCtrl', 'ParentCtrl.CtrlID',
    'ParentCtrl.GetCtrlInstID', 'SetPos', 'SetPos.scan', 'SelectText',
    'HeadCtrl', 'control.CtrlID', 'control.Next', 'control.GetAnchorPos',
    'anchor.Item.List', 'anchor.Item.Para', 'anchor.Item.Pos', 'control.GetCtrlInstID',
    'binding.document', 'binding.census', 'binding.context', 'binding.stale',
    'binding.owner_list', 'binding.state',
    'state_consistency', 'caret_identity', 'scan_selection', 'selection_identity',
    'selection_complete_text', 'selection.parse', 'insertion_owner',
    'HAction', 'HParameterSet', 'HInsertText', 'HInsertText.HSet',
    'InsertText.GetDefault', 'InsertText.Text.put', 'InsertText.Text.get',
    'InsertText.Text', 'InsertText.Execute', 'HAction.delete', 'Delete',
    'HAction.create', 'HParameterSet.create', 'HShapeObject', 'HShapeObject.HSet',
    'TextBox.GetDefault', 'ShapeObject.item_mismatch', 'ShapeDrawLayOut',
    'CreateItemArray', 'CreatePt', 'CreatePt.SetItem', 'CreatePt.Count',
    'CreatePt.Item', 'CreatePt.count', 'CreatePt.coordinate',
    'CreateNumPt.put', 'CreateNumPt.get', 'creation_caret_drift',
    'pre_create_document_drift', 'pre_insert_document_drift',
    'complete_document_preservation', 'refusal_preservation',
    'TextBox.Execute', 'new_text_box_identity', 'creation_outside_preservation',
    'created_box_owner', 'created_box_text_and_preservation',
    'created_box_geometry', 'created_box_geometry.after_text',
    'document.input', 'document.xml', 'document.body', 'document.definitions',
    'document.outline', 'document.format', 'document.containers', 'document.identity',
)) | frozenset('container.ShapeObject.' + action + key
               for action in ('SetItem.', 'get.')
               for key in ('WidthRelTo', 'HeightRelTo', 'Width', 'Height',
                           'TreatAsChar', 'AdjustTextbox'))
NATIVE_READ_RETURNS = frozenset({
    'raised', 'invalid', 'none', 'true', 'false', 'empty_str', 'str',
    'int', 'float', 'tuple', 'list', 'object',
})


def _return_shape(value):
    """Cached exact-builtin shape only; never touch a returned object's callbacks."""
    if value is None:
        return 'none'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    kind = type(value)
    if kind is str:
        return 'str' if value else 'empty_str'
    for builtin, label in ((int, 'int'), (float, 'float'), (tuple, 'tuple'), (list, 'list')):
        if kind is builtin:
            return label
    return 'object'


_CTRLID_KEY = '_container_ctrlid_observation'
_CTRLID_SITES = ('control.CtrlID', 'ParentCtrl.CtrlID', 'selected.CtrlID')
CTRLID_DIAGNOSTIC_FIELDS = (
    'native_ctrlid_boundary', 'native_ctrlid_type', 'native_ctrlid_length',
    'native_ctrlid_primitive', 'native_ctrlid_sha256', 'native_ctrlid_returned',
)


_BINDING_KEY = '_container_binding_observation'
_BINDING_BRANCHES = ('full_signature', 'append_peer_count', 'append_text', 'append_signature', 'comparison_raised')
_BINDING_PHASES = ('direct', 'constructor.initial', 'constructor.positioned', 'constructor.selected',
                   'locate.initial', 'locate.refresh', 'owner.selection_identity',
                   'owner.selection_complete_text', 'owner.insertion_owner', 'owner.created_box_owner')
BINDING_DIAGNOSTIC_FIELDS = ('native_binding_branch', 'native_binding_phase', 'native_binding_expected',
                             'native_binding_observed', 'native_binding_xml_owner',
                             'native_binding_expected_hml_length', 'native_binding_observed_hml_length')


def _make_binding_observation(branch, phase, model, fresh, item, expected, observed, text, count):
    # Only inert references here. No digest/format/serialization/SDK or I/O.
    before = after = owner = planned = None
    for record, role in ((model, 0), (fresh, 1), (item, 2)):
        if type(record) is dict and len(record) <= 16:
            for key, value in dict.items(record):
                if type(key) is str:
                    if key == '_source_hml' and role == 0: before = value
                    elif key == '_source_hml' and role == 1: after = value
                    elif key == 'target_id' and role == 2: owner = value
                    elif key == '_binding_text' and role == 2: planned = value
    return (branch, phase, before, after, expected, observed, owner, planned, text, count)


def _cache_binding(error, *operands):
    try:
        observation = _make_binding_observation(*operands)
        data = BaseException.__dict__['__dict__'].__get__(error)
        if (type(data) is dict and len(data) <= 16
                and all(type(k) is str and k != _BINDING_KEY for k in dict.keys(data))):
            dict.__setitem__(data, _BINDING_KEY, observation)
    except BaseException:
        pass  # optional cache must never replace original first fault


def _binding_cached(error):
    if type(error) is not nn.NativeNumberingReadFailure and type(error) is not nn.NativeNumberingMutationError:
        return None
    data = BaseException.__dict__['__dict__'].__get__(error)
    observation = site = returned = None
    if type(data) is dict and len(data) <= 16:
        for key, value in dict.items(data):
            if type(key) is str:
                if key == _BINDING_KEY: observation = value
                elif key == 'native_site': site = value
                elif key == 'native_return': returned = value
    if type(site) is str and site == 'container.binding.document' and type(returned) is str and returned in ('invalid', 'raised'):
        return observation
    return None


def _binding_payload(observation):
    """Existing PRIVATE journal payload AFTER owning HOLD; exact primitives only."""
    if type(observation) is not tuple or len(observation) != 10: return None
    branch, phase, before, after, expected, observed, owner, planned, text, count = observation
    if type(branch) is not str or branch not in _BINDING_BRANCHES: return None
    if type(phase) is not str or phase not in _BINDING_PHASES: return None
    for value in (before, after, planned, text):
        if value is not None and (type(value) is not str or len(value) > MAX_HWPML_CHARS): return None
    for value in (expected, observed):
        if value is not None and (type(value) is not str or len(value) != 31
                                  or str.__getitem__(value, slice(0, 7)) != 'sha256:'
                                  or any(c not in '0123456789abcdef' for c in str.__getitem__(value, slice(7, None)))): return None
    if owner is not None and (type(owner) is not str or len(owner) > 40
                             or re.fullmatch(r'(text_box|caption|footnote):[0-9]{1,10}', owner) is None): return None
    if count is not None and (type(count) is not int or not 0 <= count <= 2048): return None
    return {'schema': 'container-binding-comparison/v1', 'branch': branch, 'phase': phase,
            'expected_hml': before, 'observed_hml': after, 'expected_signature': expected,
            'observed_signature': observed, 'xml_owner': owner, 'expected_text': planned,
            'observed_text': text, 'observed_peer_count': count}


def _private_binding(error):
    return _binding_payload(_binding_cached(error))


def _project_binding(observation):
    """No raw documents/text in public diagnostics; optional digest only post-HOLD."""
    p = _binding_payload(observation)
    if p is None: return None
    branch = p['branch']
    expected = 'unavailable' if p['expected_signature'] is None else 'logical_sha256:' + p['expected_signature']
    observed = 'unavailable' if p['observed_signature'] is None else 'logical_sha256:' + p['observed_signature']
    if branch == 'append_peer_count':
        expected, observed = 'count:1', 'unavailable' if p['observed_peer_count'] is None else 'count:' + str(p['observed_peer_count'])
    elif branch == 'append_text':
        expected = 'unavailable' if p['expected_text'] is None else 'text_utf16le_sha256:' + hashlib.sha256(p['expected_text'].encode('utf-16le', 'surrogatepass')).hexdigest()
        observed = 'unavailable' if p['observed_text'] is None else 'text_utf16le_sha256:' + hashlib.sha256(p['observed_text'].encode('utf-16le', 'surrogatepass')).hexdigest()
    return (branch, p['phase'], expected, observed, p['xml_owner'] or 'unavailable',
            'NA' if p['expected_hml'] is None else str(len(p['expected_hml'])),
            'NA' if p['observed_hml'] is None else str(len(p['observed_hml'])))


class _CtrlIDObservation(tuple):
    """Finite inert capture BEFORE validation; last slot retains raw, never projected.

    No formatting/digest/I/O here. Exact builtins only; opaque/subclass values
    are never asked for a length, type name, repr, bool, equality or attribute.
    """
    __slots__ = ()

    def __new__(cls, site, value=None, returned=False):
        tag, length, primitive, digest_input = 'unreturned', None, None, None
        if returned is True:
            kind = type(value)
            tag = 'opaque'
            if value is None:
                tag = 'none'
            elif kind is str or kind is bytes:
                tag = 'str' if kind is str else 'bytes'
                length = len(value)
                if length <= 8:
                    primitive = value
                if length <= 4096:
                    digest_input = value
            elif kind is bool:
                tag, primitive = 'bool', value
            elif kind is int:
                tag = 'int'
                if -(2**31) <= value < 2**31:
                    primitive = value
            elif kind is float:
                tag, primitive = 'float', value
            else:
                for builtin, label in ((tuple, 'tuple'), (list, 'list'), (dict, 'dict')):
                    if kind is builtin:
                        tag, length = label, len(value)
                        break
        return tuple.__new__(cls, (site, returned, tag, length, primitive, digest_input, value))


def _project_ctrlid(observation):
    """Bounded SDK-identifier metadata AFTER owning HOLD; never reads retained raw."""
    if type(observation) is not _CtrlIDObservation or tuple.__len__(observation) != 7:
        return None
    site, returned, tag, length, primitive, digest_input = tuple.__getitem__(observation, slice(0, 6))
    if (type(site) is not str or site not in _CTRLID_SITES or type(returned) is not bool
            or type(tag) is not str or tag not in ('unreturned', 'none', 'str', 'bytes', 'bool', 'int', 'float', 'tuple', 'list', 'dict', 'opaque')):
        return None
    if length is not None and not (type(length) is int and 0 <= length < 2**63):
        return None
    value, digest = 'omitted', 'unavailable'
    if tag == 'none' and returned is True:
        value = 'none'
    elif tag == 'str' and type(primitive) is str and len(primitive) <= 8:
        value = 'utf16le_hex:' + primitive.encode('utf-16le', 'surrogatepass').hex()
    elif tag == 'bytes' and type(primitive) is bytes and len(primitive) <= 8:
        value = 'bytes_hex:' + primitive.hex()
    elif tag == 'bool' and type(primitive) is bool:
        value = 'true' if primitive is True else 'false'
    elif tag == 'int' and type(primitive) is int and -(2**31) <= primitive < 2**31:
        value = 'int32:' + str(primitive)
    elif tag == 'float' and type(primitive) is float:
        import struct
        value = 'float64be:' + struct.pack('>d', primitive).hex()
    if tag == 'str' and type(digest_input) is str and len(digest_input) <= 4096:
        digest = 'utf16le:' + hashlib.sha256(digest_input.encode('utf-16le', 'surrogatepass')).hexdigest()
    elif tag == 'bytes' and type(digest_input) is bytes and len(digest_input) <= 4096:
        digest = 'bytes:' + hashlib.sha256(digest_input).hexdigest()
    return site, tag, 'NA' if length is None else str(length), value, digest, 'true' if returned is True else 'false'


def _read_failure(message, site, returned):
    error = nn.NativeNumberingReadFailure(message, code='container_state_unproven')
    data = BaseException.__dict__['__dict__'].__get__(error)
    dict.__setitem__(data, 'native_site', 'container.' + site)
    dict.__setitem__(data, 'native_return', returned)
    return error


def _mutation_failure(message, cause):
    """Carry the owned first read's cached labels, not its objects or cause chain."""
    error = nn.NativeNumberingMutationError(message)
    try:
        if type(cause) is nn.NativeNumberingReadFailure:
            data = BaseException.__dict__['__dict__'].__get__(cause)
            site = returned = None
            observation = binding_observation = None
            if type(data) is dict and len(data) <= 16:
                for key, value in dict.items(data):
                    if type(key) is str and key == _BINDING_KEY and type(value) is tuple:
                        binding_observation = value
                    if type(key) is str and key == _CTRLID_KEY and type(value) is _CtrlIDObservation:
                        observation = value
                    if type(key) is str and type(value) is str:
                        if key == 'native_site':
                            site = value
                        elif key == 'native_return':
                            returned = value
            if (type(site) is str and type(returned) is str
                    and site in NATIVE_READ_SITES and returned in NATIVE_READ_RETURNS):
                target = BaseException.__dict__['__dict__'].__get__(error)
                dict.__setitem__(target, 'native_site', site)
                dict.__setitem__(target, 'native_return', returned)
                if observation is not None:
                    dict.__setitem__(target, _CTRLID_KEY, observation)
                if binding_observation is not None:
                    dict.__setitem__(target, _BINDING_KEY, binding_observation)
    except BaseException:
        # Optional diagnostics cannot replace the original read/cause or its debt.
        pass
    return error


BINDING = {'target_id', 'expected_hash'}
FIELDS = {
    'text_container_inventory': ENVELOPE,
    'text_container_read': ENVELOPE | BINDING,
    'text_container_text_exact': ENVELOPE | BINDING | {'expected_text', 'new_text', 'confirm_layout'},
    'text_box_create': ENVELOPE | {'text', 'width_mm', 'height_mm', 'expected_pos', 'confirm_layout'},
}
KINDS = {'text_box': 'gso', 'caption': 'gso', 'footnote': 'fn'}


def _valid_raw_selection(value):
    """Validate the raw BOOL + six LONG outputs at the FIRST read boundary.

    Canonical no-block (False, None x6) remains admitted. No bool, float,
    negative or arbitrary-size Python int may reach a later SDK getter.
    This is local to this interface; numbering's deliberate policy is unchanged.
    """
    if type(value) not in (tuple, list) or len(value) != 7 or type(value[0]) is not bool:
        return False
    if value[0] is False and all(x is None for x in value[1:]):
        return True
    if not all(type(x) is int and 0 <= x < 2**31 for x in value[1:]):
        return False
    return nn._parse_selected_pos(value) is not None


class LocalCliBundleTextContainersMixin:
    def _bundle_text_container(self, hwp, step, on_mutating=None):
        return {'schema_version': 'local-cli/text-containers/v1',
                **execute(hwp, step, on_mutating=on_mutating)}


def _hash(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def _refuse(code, message):
    raise nn.NativeNumberingRefusal(message, code=code)


def validate_step(step):
    op = step.get('op')
    if op not in FIELDS or set(step) - FIELDS[op]:
        raise nn.NativeNumberingInputError('Unknown text container operation or fields.')
    result = dict(step)
    if op in ('text_container_read', 'text_container_text_exact'):
        if type(step.get('target_id')) is not str or not re.fullmatch(r'(text_box|caption|footnote):[0-9]{1,10}', step['target_id']):
            raise nn.NativeNumberingInputError('target_id must be the exact inventory identity.')
        if int(step['target_id'].split(':')[1]) >= 2**32:
            raise nn.NativeNumberingInputError('target_id native owner is outside UI4 range.')
        if type(step.get('expected_hash')) is not str or not re.fullmatch(r'sha256:[0-9a-f]{24}', step['expected_hash']):
            raise nn.NativeNumberingInputError('expected_hash must be the fresh inventory hash.')
    if op in ('text_container_text_exact', 'text_box_create'):
        if step.get('confirm_layout') is not True:
            raise nn.NativeNumberingInputError('confirm_layout must be true.')
        for key in (('expected_text', 'new_text') if op == 'text_container_text_exact' else ('text',)):
            text = step.get(key)
            limit = 20000 if key == 'expected_text' else 2000
            if type(text) is not str or not 1 <= len(text) <= limit or any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or 0xd800 <= ord(c) <= 0xdfff for c in text):
                raise nn.NativeNumberingInputError(key + ' must be complete, nonempty single-line text within its bound.')
    if op == 'text_box_create':
        pos = step.get('expected_pos')
        if type(pos) not in (list, tuple) or len(pos) != 3 or any(type(x) is not int or not 0 <= x < 2**31 for x in pos):
            raise nn.NativeNumberingInputError('expected_pos requires three exact nonnegative LONGs.')
        for key in ('width_mm', 'height_mm'):
            value = step.get(key)
            if type(value) not in (int, float) or not 5 <= value <= 200:
                raise nn.NativeNumberingInputError(key + ' must be between 5 and 200 mm.')
            result[key.replace('_mm', '_hu')] = round(value * 7200 / 25.4)
    return result


def _identity(owner):
    # SHAPECOMPONENT InstID is deliberately NOT an alias for SHAPEOBJECT InstId.
    shape = [e for e in owner if _name(e) == 'shapeobject']
    if len(shape) > 1:
        raise CensusError('Ambiguous SHAPEOBJECT identity.')
    source = shape[0] if shape else owner if _name(owner) == 'footnote' else None
    values = [v for k, v in source.attrib.items() if k.lower() == 'instid'] if source is not None else []
    if not values:
        return None
    if len(values) != 1 or re.fullmatch(r'[0-9]{1,10}', values[0]) is None or int(values[0]) >= 2**32:
        raise CensusError('Malformed native owner identity.')
    return values[0]


def parse(text, *, _phase=None):
    # _Native.read owns this exact builtin list. No SDK/reporting callback runs here.
    if _phase is not None:
        _phase[0] = 'document.input'
    if type(text) is not str or not text or len(text) > MAX_HWPML_CHARS or _DOCTYPE.search(text):
        raise CensusError('Unusable HWPML2X container evidence.')
    if _phase is not None:
        _phase[0] = 'document.xml'
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CensusError('Malformed HWPML2X container evidence.') from exc
    parents = {}
    def visit(e, depth=0):
        if depth > _MAX_DEPTH:
            raise CensusError('Container evidence exceeds nesting bound.')
        for child in e:
            parents[child] = e
            visit(child, depth + 1)
    visit(root)
    if _phase is not None:
        _phase[0] = 'document.body'
    bodies = [e for e in root if _name(e) == 'body']
    if len(bodies) != 1:
        raise CensusError('Container evidence has no unique BODY.')
    body = bodies[0]
    if _phase is not None:
        _phase[0] = 'document.definitions'
    resolver = _Resolver(root, body)
    # Numbering's shared resolver deliberately masks OutlineShape. This family
    # is not numbering: retain exact binding identity AND resolved meaning.
    # Qualify it during the entered document read, not after another SDK call.
    if _phase is not None:
        _phase[0] = 'document.outline'
    outline_references = {}
    for node in root.iter():
        pairs = [(k, v) for k, v in node.attrib.items() if k.rsplit('}', 1)[-1].lower() == 'outlineshape']
        if pairs or _name(node) == 'secdef':
            ref = proof.attribute(node, 'OutlineShape', '1')  # HWPML table 79 default
            outline_references[node] = [pairs, ref, resolver.definition('numberinglist', ref)]
    if _phase is not None:
        _phase[0] = 'document.format'
    chars, paras, _, _ = nf._shape_tables(root, resolver)
    if _phase is not None:
        _phase[0] = 'document.containers'
    containers = []
    for e in body.iter():
        tag = _name(e)
        if tag not in ('drawtext', 'caption', 'footnote'):
            continue
        kind = {'drawtext': 'text_box', 'caption': 'caption', 'footnote': 'footnote'}[tag]
        owner = e
        if kind != 'footnote':
            while owner in parents and _name(owner) not in ('rectangle', 'ellipse', 'polygon', 'picture', 'table'):
                owner = parents[owner]
        ident = _identity(owner)
        lists = [x for x in e if _name(x) == 'paralist']
        ps = [x for x in lists[0] if _name(x) == 'p'] if len(lists) == 1 else []
        rows = []
        for p in ps:
            rows.append({'element': p, 'atoms': nf._atoms(p, chars, resolver), 'shape': paras.get((_attr(p, 'parashape') or '').strip())})
        words = '\n'.join(''.join(a['char'] for a in row['atoms'] if 'char' in a) for row in rows)
        ancestors = []
        parent = parents.get(owner)
        while parent is not None and parent is not body:
            ancestors.append(_name(parent))
            parent = parents.get(parent)
        grouping = proof.grouping_problem(owner, kind) if kind == 'footnote' or _name(owner) in ('rectangle', 'ellipse', 'polygon', 'picture') else None
        unsupported = None
        if not ident:
            unsupported = 'container_identity_unavailable'
        elif any(x not in ('text', 'p', 'section') for x in ancestors):
            unsupported = 'container_nested_owner'
        elif grouping:
            unsupported = grouping
        elif sum(_name(x) == 'paralist' for x in owner.iter()) != 1:
            unsupported = 'container_owner_shared'
        elif len(rows) != 1 or not rows[0]['shape']:
            unsupported = 'container_multi_paragraph'
        else:
            atoms = rows[0]['atoms']
            controls = [a for a in atoms if 'char' not in a]
            prefix = 1 if kind in ('caption', 'footnote') and controls and atoms[0] is controls[0] and controls[0].get('control') == 'autonum' else 0
            if len(controls) != prefix or any(a.get('width') != 1 for a in atoms[prefix:]):
                unsupported = 'container_has_controls'
            elif not words or len(words) > 20000:
                unsupported = 'container_text_unsupported'
            elif any(a['shape'] != atoms[prefix]['shape'] for a in atoms[prefix:]):
                unsupported = 'container_mixed_format'
        try:
            digest = resolver.digest(owner)
        except _Unresolved as exc:
            raise CensusError('Container owner references do not resolve.') from exc
        containers.append({'kind': kind, 'target_id': f'{kind}:{ident}' if ident else None, 'native_id': ident,
                           'hash': _hash([kind, digest]), 'text': words, 'editable': unsupported is None,
                           'refusal_code': unsupported, '_owner': owner, '_rows': rows})
        if len(containers) > 2048:
            raise CensusError('Container count exceeds bound.')
    if _phase is not None:
        _phase[0] = 'document.identity'
    ids = [x['target_id'] for x in containers if x['target_id']]
    if len(ids) != len(set(ids)):
        raise CensusError('Duplicate container identities.')
    return {'root': root, 'body': body, 'resolver': resolver, 'containers': containers, '_source_hml': text,
            'chars': chars, 'outline_references': outline_references,
            'body_paragraphs': [p for section in body if _name(section) == 'section' for p in section if _name(p) == 'p'],
            'list_bound': sum(_name(x) == 'paralist' for x in body.iter()) + 16}


def public(item, *, complete=False):
    words = item['text']
    full = complete or len(words) <= 2000
    return {k: item[k] for k in ('kind', 'target_id', 'hash', 'editable', 'refusal_code')} | {
        'text': words if full else None, 'text_preview': words[:300], 'text_complete': full, 'text_chars': len(words),
        'native_qualification': 'not_run'}


def _target(model, step):
    matches = [x for x in model['containers'] if x['target_id'] == step['target_id']]
    if len(matches) != 1:
        _refuse('container_not_found', 'No unique container with this exact identity.')
    item = matches[0]
    if item['hash'] != step['expected_hash']:
        _refuse('container_hash_mismatch', 'Container changed since inventory.')
    return item


class _Native:
    """One exact raw interface, a first-fault latch, no alias or post-fault call."""
    def __init__(self, hwp):
        self.failed = False
        self.issued = False
        self._ctrlid_observation = None
        self._last_call = None
        self.com = self.call('raw_interface', lambda: hwp.hwp, 'object')

    def _observe_ctrlid(self, site, value=None, returned=False):
        try:
            self._ctrlid_observation = _CtrlIDObservation(site, value, returned)
        except BaseException:
            # Optional metadata must not change healthy acceptance or replace
            # the original technical fault. The call frame still retains raw.
            self._ctrlid_observation = None

    def _failure(self, message, site, returned):
        error = _read_failure(message, site, returned)
        observation = self._ctrlid_observation
        try:
            if type(observation) is _CtrlIDObservation:
                boundary = tuple.__getitem__(observation, 0)
                if (type(boundary) is str and boundary == self._last_call
                        and (site == boundary or (site == 'selected.identity' and boundary == 'selected.CtrlID'))):
                    data = BaseException.__dict__['__dict__'].__get__(error)
                    if (type(data) is dict and dict.__len__(data) <= 16
                            and all(type(key) is str and key != _CTRLID_KEY for key in dict.keys(data))):
                        # Never hash against foreign keys or replace/destruct an
                        # existing slot. Metadata is optional; first fault is not.
                        dict.__setitem__(data, _CTRLID_KEY, observation)
        except BaseException:
            pass  # preserve the original error/cause and failure latch
        return error

    def hold(self, site, returned='invalid'):
        self.failed = True
        raise self._failure('Text container boundary unproved at ' + site, site, returned)

    def call(self, site, fn, expected):
        if self.failed:
            self.hold(site)
        self._last_call = site
        ctrlid = type(site) is str and site in _CTRLID_SITES
        if ctrlid:
            self._observe_ctrlid(site)
        try:
            value = fn()
        except Exception as exc:
            self.failed = True
            raise self._failure('Text container SDK failed at ' + site, site, 'raised') from exc
        if ctrlid:
            self._observe_ctrlid(site, value, True)
        t = type(value)
        valid = ((expected == 'object' and value is not None and t not in (str, int, float, bool, list, tuple, dict)) or
                 (expected == 'optional_object' and (value is None or t not in (str, int, float, bool, list, tuple, dict))) or
                 (expected == 'true' and value is True) or (expected == 'void' and value is None) or
                 (expected == 'str' and t is str) or
                 (expected == 'coordinate' and t is int and 0 <= value < 2**31) or
                 (expected == 'ui1' and t is int and 0 <= value < 2**8) or
                 (expected == 'ui2' and t is int and 0 <= value < 2**16) or
                 (expected == 'id' and t is str and re.fullmatch(r'[0-9]{1,10}', value) is not None and int(value) < 2**32) or
                 (expected == 'position' and t in (tuple, list) and len(value) == 3 and all(type(x) is int and 0 <= x < 2**31 for x in value)) or
                 (expected == 'selection' and _valid_raw_selection(value)) or
                 (expected == 'mode' and t is int and value in (0, 1, 2, 3, 4, 17, 18, 19, 20)))
        if not valid:
            self.hold(site, _return_shape(value))
        return value

    def _selected_identity(self, mode):
        ctrl = self.call('CurSelectedCtrl', lambda: self.com.CurSelectedCtrl, 'optional_object')
        if mode not in (4, 20):
            if ctrl is not None:
                self.hold('state_consistency')
            return None
        if ctrl is None:
            self.hold('selected.identity')
        kind = self.call('selected.CtrlID', lambda: ctrl.CtrlID, 'str')
        if kind not in ('gso', 'fn'):
            self.hold('selected.identity')
        return kind, self.call('selected.GetCtrlInstID', lambda: ctrl.GetCtrlInstID(), 'id')

    def witness(self):
        """Complete immutable observation, NOT a linear range/cardinality proof.

        Keep strict bits, all raw selection operands and observed object identity.
        Unknown type/bits/member faults stop before any later recheck or action.
        """
        pos = tuple(self.call('GetPos', lambda: self.com.GetPos(), 'position'))
        raw = tuple(self.call('GetSelectedPos', lambda: self.com.GetSelectedPos(), 'selection'))
        mode = self.call('SelectionMode', lambda: self.com.SelectionMode, 'mode')
        if (mode == 0 and raw[0] is not False) or (mode in (1, 17) and raw[0] is not True):
            self.hold('state_consistency')
        selected = self._selected_identity(mode)
        if tuple(self.call('GetPos.recheck', lambda: self.com.GetPos(), 'position')) != pos:
            self.hold('state_consistency')
        if self.call('SelectionMode.recheck', lambda: self.com.SelectionMode, 'mode') != mode:
            self.hold('state_consistency')
        if self._selected_identity(mode) != selected:
            self.hold('state_consistency')
        return pos, raw, mode, selected

    def state(self):
        """Explicit linear projection for range/intent proofs; never purity equality."""
        pos, raw, mode, selected = self.witness()
        if mode not in (0, 1, 17) or selected is not None:
            self.hold('linear_state_required')
        return pos, nn._parse_selected_pos(raw)

    def at(self, pos):
        self.call('SetPos', lambda: self.com.SetPos(*pos), 'true')
        if self.state() != (tuple(pos), ()):
            self.hold('caret_identity')

    def owner(self, wanted=None):
        ctrl = self.call('ParentCtrl', lambda: self.com.ParentCtrl, 'optional_object')
        if ctrl is None:
            return None, None
        kind = self.call('ParentCtrl.CtrlID', lambda: ctrl.CtrlID, 'str')
        if not kind or (kind.lower() in KINDS.values() and kind not in KINDS.values()):
            self.hold('ParentCtrl.CtrlID')
        # Filter before the target-only getter, never after its failed result.
        if kind not in KINDS.values() or (wanted is not None and kind != wanted):
            return kind, None
        return kind, self.call('ParentCtrl.GetCtrlInstID', lambda: ctrl.GetCtrlInstID(), 'id')

    def read(self):
        source = self.call('GetTextFile', lambda: self.com.GetTextFile('HWPML2X', ''), 'str')
        phase = ['document.input']
        try:
            return parse(source, _phase=phase)
        except Exception as exc:
            self.failed = True
            raise _read_failure('Entered container document evidence is unusable.', phase[0], 'raised') from exc

    def read_unchanged(self, expected, site):
        before = self.witness()
        model = self.read()
        # Complete domain proof precedes ANY outgoing SDK state query.
        if _signature(model) != _signature(expected):
            self.hold(site)
        if self.witness() != before:
            self.hold('read_purity')
        return model

    def select(self, lid, first, last, item):
        self.at((lid, 0, first))
        _require_owner(self, item, 'selection_identity')
        self.call('SelectText', lambda: self.com.SelectText(0, first, 0, last), 'true')
        pos, sel = self.state()
        if sel != (lid, 0, first, lid, 0, last) or pos[0] != lid:
            self.hold('selection_identity')
        _require_owner(self, item, 'selection_identity')
        before_block = self.witness()
        source = self.call('GetTextFile.block', lambda: self.com.GetTextFile('HWPML2X', 'saveblock:true'), 'str')
        try:
            block = nf.parse_format_model(source)
            paragraphs = block['paragraphs']
            words = [p['text'] for p in paragraphs]
        except Exception as exc:
            self.failed = True
            raise _read_failure('Entered selection evidence is unusable.', 'selection.parse', 'raised') from exc
        if words != [item['text']]:
            self.hold('selection_complete_text')
        if self.witness() != before_block:
            self.hold('selection_complete_text')
        _require_owner(self, item, 'selection_complete_text')

    def insert(self, text, on_mutating):
        before = self.witness()
        if before[2] != 0 or before[1][0] is not False or before[3] is not None:
            self.hold('insertion_state')
        action = self.call('HAction', lambda: self.com.HAction, 'object')
        params = self.call('HParameterSet', lambda: self.com.HParameterSet, 'object')
        pset = self.call('HInsertText', lambda: params.HInsertText, 'object')
        hset = self.call('HInsertText.HSet', lambda: pset.HSet, 'object')
        self.call('InsertText.GetDefault', lambda: action.GetDefault('InsertText', hset), 'true')
        self.call('InsertText.Text.put', lambda: setattr(pset, 'Text', text), 'void')
        if self.call('InsertText.Text.get', lambda: pset.Text, 'str') != text:
            self.hold('InsertText.Text')
        if self.witness() != before:
            self.hold('insertion_state')
        if on_mutating:
            on_mutating()
        self.issued = True
        self.call('InsertText.Execute', lambda: action.Execute('InsertText', hset), 'true')


def _document_view_caret(model):
    """HWPML table15 view location, NOT authored document/owner identity.

    Only the unique exact Desktop path with normal native-coordinate strings
    qualifies. Unsupported shape keeps the original complete signature.
    """
    root = model['root']
    if root.tag != 'HWPML':
        return None
    heads = [e for e in root if e.tag == 'HEAD']
    if len(heads) != 1:
        return None
    settings = [e for e in heads[0] if e.tag == 'DOCSETTING']
    if len(settings) != 1:
        return None
    positions = [e for e in settings[0] if e.tag == 'CARETPOS']
    if len(positions) != 1:
        return None
    node = positions[0]
    if len(node) or node.text not in (None, '') or set(node.attrib) != {'List', 'Para', 'Pos'}:
        return None
    for field in ('List', 'Para', 'Pos'):
        value = node.attrib.get(field)
        if (type(value) is not str or re.fullmatch(r'(?:0|[1-9][0-9]{0,9})', value) is None
                or int(value) >= 2**31):
            return None
    return node


def _signature(model, *, mask=None, remove=None, element=None, remove_shell=None):
    """Complete logical document, excluding only qualified serialized view coords.

    Native TEXT/CHAR splits normalize as before. Every non-target character,
    control subtree, format/definition/style/summary/numbering/media token stays.
    Live caret/selection/mode/owner purity remains separate mandatory witness.
    """
    resolver = model['resolver']
    view_caret = _document_view_caret(model)
    def shell(e):
        clone = ET.Element(e.tag, e.attrib)
        clone.text = e.text
        if e is view_caret:
            # Only the qualified leaf loses its three view-coordinate VALUES.
            # Domain membership is a structural shell slot, never XML data.
            for field in ('List', 'Para', 'Pos'):
                clone.attrib.pop(field)
        result = [resolver.digest(clone), model['outline_references'].get(e)]
        if e is view_caret:
            result.append(['qualified-runtime-caret-domain', 1])
        # Every raw/unqualified shell has exactly two slots. No XML attribute,
        # text, tag or sentinel can forge the qualified three-slot shell.
        return result
    def walk(e):
        if e is remove or e is remove_shell:
            return None
        if _name(e) != 'p':
            return [shell(e), [(walk(x), x.tail or '') for x in e if x is not remove and x is not remove_shell]]
        content, end = [], None
        for run in e:
            if _name(run) != 'text':
                content.append(walk(run))
                continue
            end = shell(run)
            for char in run:
                if char is remove or char is remove_shell:
                    continue
                if _name(char) == 'char' and not list(char):
                    content.extend([end, sorted(char.attrib.items()), c] for c in char.text or '')
                    if not char.text:
                        content.append(['empty_char', end, sorted(char.attrib.items())])
                else:
                    content.append([end, walk(char)])
                if char.tail:
                    content.append(['tail', char.tail])
            if not list(run):
                content.append(['empty_run', end])
            if run.tail:
                content.append(['run_tail', run.tail])
        if e is mask:
            # Preserve prefix controls, all format/CHAR attributes and the end run;
            # the exact new text is independently verified before masking.
            seen = set()
            masked = []
            for token in content:
                if len(token) == 3 and type(token[2]) is str:
                    key = json.dumps(token[:2], sort_keys=True)
                    if key not in seen:
                        masked.append([*token[:2], '<complete-text>'])
                        seen.add(key)
                else:
                    masked.append(token)
            content = masked
        return [shell(e), content, end]
    return _hash(walk(model['root'] if element is None else element))


def _body_control_anchors(model):
    """Actual XML element identity -> native body coordinate, never an ID alias."""
    shapes = {'rectangle', 'ellipse', 'arc', 'polygon', 'curve', 'line',
              'connectline', 'picture', 'ole', 'container'}
    found = {}
    for index, paragraph in enumerate(model['body_paragraphs']):
        offset = 0
        for run in paragraph:
            if _name(run) != 'text':
                continue
            for child in run:
                kind = 'gso' if _name(child) in shapes else 'fn' if _name(child) == 'footnote' else None
                if kind is not None:
                    key = (kind, (0, index, offset))
                    if key in found:
                        raise CensusError('Ambiguous body control anchor.')
                    found[key] = child
                # Read-only temporary parent. ElementTree append does not detach
                # the actual child or modify/serialize the original document.
                part = ET.Element('P')
                text = ET.SubElement(part, 'TEXT', dict(run.attrib))
                text.append(child)
                for atom in nf._atoms(part, model['chars'], model['resolver']):
                    width = atom.get('width')
                    if type(width) is not int or width < 0:
                        raise CensusError('Unresolved control anchor width.')
                    offset += width
    return found


def _binding(native, model, item, *, phase='direct'):
    """One upfront HeadCtrl -> exact anchor -> observed native instance route.

    Neither HWPML SHAPEOBJECT InstId nor SHAPECOMPONENT InstID supplies a native
    selector. The complete same-kind control census is fresh on each binding.
    An empty kind is unclassified, never a terminator/selector: exclusion needs
    a closed homogeneous BODY owner universe plus full document/read purity.
    """
    wanted = KINDS[item['kind']]
    try:
        anchors = _body_control_anchors(model)
        points = [point for (kind, point), node in anchors.items()
                  if kind == wanted and node is item['_owner']]
        if len(points) != 1:
            raise CensusError('No unique parsed owner anchor.')
        expected = {point for kind, point in anchors if kind == wanted}
        # Independent serialized owner coverage, not the empty node's identity.
        # A mixed/nested universe cannot authorize this narrow empty-kind route.
        target_tags = {'rectangle', 'ellipse', 'arc', 'polygon', 'curve', 'line',
                       'connectline', 'picture', 'ole', 'container', 'footnote'}
        required_nodes = {node for node in model['body'].iter() if _name(node) in target_tags}
        closed_body = required_nodes == {node for (kind, point), node in anchors.items() if kind == wanted}
    except Exception as exc:
        native.failed = True
        raise _read_failure('Container identity document is unproven.', 'binding.document', 'raised') from exc
    # A complete census is a read-only native boundary, including upfront and
    # scan-refresh callers. Capture before its first getter; never in a finally.
    state = native.witness()
    ctrl = native.call('HeadCtrl', lambda: native.com.HeadCtrl, 'optional_object')
    rows, ids, positions = [], set(), set()
    empty_seen, other_target = False, False
    for _ in range(2048):
        if ctrl is None:
            break
        current = ctrl
        kind = native.call('control.CtrlID', lambda: current.CtrlID, 'str')
        if kind.lower() in KINDS.values() and kind not in KINDS.values():
            native.hold('control.CtrlID')
        empty_seen = empty_seen or kind == ''
        other_target = other_target or (kind in KINDS.values() and kind != wanted)
        if kind == wanted:
            anchor = native.call('control.GetAnchorPos', lambda: current.GetAnchorPos(0), 'object')
            point = tuple(native.call('anchor.Item.' + key, lambda k=key: anchor.Item(k), 'coordinate')
                          for key in ('List', 'Para', 'Pos'))
            ident = native.call('control.GetCtrlInstID', lambda: current.GetCtrlInstID(), 'id')
            if ident in ids or point in positions:
                native.hold('binding.census')
            ids.add(ident)
            positions.add(point)
            rows.append((point, ident))
        ctrl = native.call('control.Next', lambda: current.Next, 'optional_object')
    else:
        native.hold('binding.census')
    if {point for point in positions if point[0] == 0} != expected:
        native.hold('binding.census')
    matches = [ident for point, ident in rows if point == points[0]]
    if len(matches) != 1:
        native.hold('binding.census')
    if empty_seen and (not closed_body or other_target or positions != expected or len(rows) != len(expected)):
        native.hold('binding.census')
    # Only the healthy path reaches these SDK reads. Earlier faults/invalid
    # values already latched failure and must not be followed by a state query.
    if native.witness() != state:
        native.hold('binding.state')
    if empty_seen:
        # Census purity FIRST: a subsequent read must not erase getter drift.
        # Preserve all foreign content as well as the positively bound targets.
        fresh = native.read()
        branch, expected_signature, observed_signature = 'full_signature', None, None
        observed_text = peer_count = None
        try:
            observed_signature = _signature(fresh)
            expected_signature = _signature(model)
            unchanged = observed_signature == expected_signature
            # Preserve original short circuit/evaluation order, not a new proof.
            if '_binding_text' in item:
                peers = [x for x in fresh['containers'] if x['target_id'] == item['target_id'] and x['kind'] == item['kind']]
                branch, peer_count = 'append_peer_count', len(peers)
                expected_signature = observed_signature = None
                unchanged = False
                if len(peers) == 1:
                    branch, observed_text = 'append_text', peers[0]['text']
                    if peers[0]['text'] == item['_binding_text']:
                        branch = 'append_signature'
                        observed_signature = _signature(fresh, mask=peers[0]['_rows'][0]['element'])
                        expected_signature = _signature(model, mask=item['_rows'][0]['element'])
                        unchanged = observed_signature == expected_signature
        except Exception as exc:
            native.failed = True
            error = _read_failure('Empty-kind census document proof failed.', 'binding.document', 'raised')
            try:
                _cache_binding(error, 'comparison_raised', phase, model, fresh, item, expected_signature, observed_signature, observed_text, peer_count)
            except BaseException:
                pass
            raise error from exc
        if not unchanged:
            try:
                native.hold('binding.document')
            except nn.NativeNumberingReadFailure as error:
                try:
                    _cache_binding(error, branch, phase, model, fresh, item, expected_signature, observed_signature, observed_text, peer_count)
                except BaseException:
                    pass
                raise
        # Only the healthy domain-proof path may issue this outgoing witness.
        if native.witness() != state:
            native.hold('binding.state')
    return wanted, points[0], matches[0], tuple(sorted(rows))


def _require_owner(native, item, site):
    binding = item.get('_native_binding')
    if binding is None:
        native.hold('binding.context')
    model, expected = binding
    original = native.witness()
    if native.owner(expected[0]) != (expected[0], expected[2]):
        native.hold(site)
    if _binding(native, model, item, phase='owner.' + site) != expected:
        native.hold('binding.stale')
    if native.witness() != original:
        native.hold('binding.state')


def _locate(native, model, item):
    if model['list_bound'] > 20000:
        _refuse('container_unlocatable', 'Native list bound exceeds supported budget.')
    binding = _binding(native, model, item, phase='locate.initial')
    item['_native_binding'] = (model, binding)
    matches = []
    for lid in range(1, model['list_bound'] + 1):
        # Native SetPos may accept a nonexistent list without moving. This is not
        # a fault; only an in-range exact returned coordinate is used.
        native.call('SetPos.scan', lambda: native.com.SetPos(lid, 0, 0), 'true')
        pos, selection = native.state()
        if selection:
            native.hold('scan_selection')
        if pos != (lid, 0, 0):
            continue
        if native.owner(binding[0]) == (binding[0], binding[2]):
            matches.append(lid)
    if len(matches) != 1:
        native.hold('binding.owner_list')
    if _binding(native, model, item, phase='locate.refresh') != binding:
        native.hold('binding.stale')
    return matches[0]


def execute(hwp, step, *, on_mutating=None):
    request = validate_step(step)
    native = _Native(hwp)
    before = native.read()
    op = step['op']
    if op == 'text_container_inventory':
        return {'read_only': True, 'containers': [public(x) for x in before['containers']], 'warnings': []}
    item = _target(before, request) if op != 'text_box_create' else None
    if op == 'text_container_read':
        return {'read_only': True, 'container': public(item, complete=True), 'warnings': []}
    if op == 'text_container_text_exact':
        # Pure entered-document admission comes BEFORE native caret/navigation.
        # A grouped owner is not visited merely to restore a caret never moved.
        if not item['editable']:
            _refuse(item['refusal_code'], 'Container variant is not supported for exact text editing.')
        if item['text'] != request['expected_text']:
            _refuse('container_text_mismatch', 'expected_text must equal the entire current container text.')
    entry_witness = native.witness()
    original = (entry_witness[0], nn._parse_selected_pos(entry_witness[1]))
    if entry_witness[2] != 0 or original[1] or entry_witness[3] is not None:
        _refuse('container_selection_present', 'A plain unselected caret is required.')
    if original[0][0] != 0:
        _refuse('container_wrong_caret', 'Start this operation at a plain body caret.')
    try:
        if op == 'text_box_create':
            return _create(native, before, request, original, on_mutating, entry_witness)
        if item['text'] == request['new_text']:
            return {'read_only': False, 'container': public(item), 'result': {'changed': False}, 'warnings': []}
        lid = _locate(native, before, item)
        atoms = item['_rows'][0]['atoms']
        first = next(i for i, a in enumerate(atoms) if 'char' in a)
        start = sum(a['width'] for a in atoms[:first])
        last = sum(a['width'] for a in atoms)
        native.select(lid, start, last, item)
        native.at((lid, 0, last))
        _require_owner(native, item, 'insertion_owner')
        native.read_unchanged(before, 'pre_insert_document_drift')
        if native.state() != ((lid, 0, last), ()):
            native.hold('pre_insert_document_drift')
        native.insert(request['new_text'], on_mutating)
        item['_binding_text'] = item['text'] + request['new_text']
        native.select(lid, start, last, item)
        action = native.call('HAction.delete', lambda: native.com.HAction, 'object')
        native.call('Delete', lambda: action.Run('Delete'), 'true')
        post_read_state = native.witness()
        after = native.read()
        later = _target(after, {**request, 'expected_hash': next((x['hash'] for x in after['containers'] if x['target_id'] == item['target_id']), '')})
        if later['text'] != request['new_text'] or not later['editable'] or _signature(before, mask=item['_rows'][0]['element']) != _signature(after, mask=later['_rows'][0]['element']):
            native.hold('complete_document_preservation')
        if native.witness() != post_read_state:
            native.hold('read_purity')
        later['_native_binding'] = (after, item['_native_binding'][1])
        _require_owner(native, later, 'insertion_owner')
        native.at(original[0])
        return {'read_only': False, 'container': public(later), 'result': {'changed': True, 'status': 'replaced'},
                'preservation': 'complete_resolved_document_except_target_text', 'warnings': ['Native save/reopen and every-page render qualification required.']}
    except nn.NativeNumberingReadFailure as exc:
        if native.issued:
            raise _mutation_failure('Native container mutation outcome is unproven; first SDK fault retained and no follow-on call issued.', exc) from exc
        raise
    except nn.NativeNumberingRefusal as exc:
        if native.failed or native.issued:
            raise _mutation_failure('Native container mutation outcome is unproven; no follow-on call issued.', exc) from exc
        native.at(original[0])
        native.read_unchanged(before, 'refusal_preservation')
        raise
    except Exception as exc:
        if native.issued:
            raise _mutation_failure('Native container mutation outcome is unproven; no further SDK call or rollback was attempted.', exc) from exc
        raise


def _creation_anchor(model, position):
    """Request-bound pristine body paragraph plus exact native atom boundary."""
    lid, index, offset = position
    paragraphs = model['body_paragraphs']
    if lid != 0 or index >= len(paragraphs):
        _refuse('container_wrong_caret', 'Creation paragraph is not a proven body paragraph.')
    paragraph = paragraphs[index]
    atoms = nf._atoms(paragraph, model['chars'], model['resolver'])
    boundaries, current = {0}, 0
    for atom in atoms:
        if type(atom.get('width')) is not int or atom['width'] < 0:
            _refuse('container_wrong_caret', 'Creation native offset cannot be resolved.')
        current += atom['width']
        boundaries.add(current)
    if offset not in boundaries:
        _refuse('container_wrong_caret', 'Creation offset is not an exact native text/control boundary.')
    # At a proven body END only, native creation may add a terminal empty CHAR
    # after the new owner. Preserve an already existing empty marker exactly.
    run = paragraph[-1] if len(paragraph) and _name(paragraph[-1]) == 'text' else None
    existing_empty = (run is not None and len(run) and _name(run[-1]) == 'char'
                      and not (run[-1].text or list(run[-1])))
    return {'position': tuple(position), 'paragraph_signature': _signature(model, element=paragraph),
            'new_end_char': offset == current and run is not None and not existing_empty}


def _created_end_shell(model, owner, anchor):
    """One exact constructor-only zero-width terminal CHAR, never a global mask.

    Position and the full pristine paragraph are still proved independently.
    Only the same last TEXT run, immediate owner successor, no attributes/text/
    children/tail and no trailing siblings qualify. Run/end formatting survives.
    """
    if not anchor.get('new_end_char'):
        return None
    index = anchor['position'][1]
    paragraphs = model['body_paragraphs']
    if index >= len(paragraphs):
        return None
    paragraph = paragraphs[index]
    if not len(paragraph) or _name(paragraph[-1]) != 'text':
        return None
    run = paragraph[-1]
    if len(run) < 2 or run[-2] is not owner or run.tail:
        return None
    end = run[-1]
    if _name(end) != 'char' or end.attrib or end.text or len(end) or end.tail:
        return None
    return end


def _prove_created_anchor(model, owner, anchor):
    """Locate the new exact owner in body atoms, without SDK navigation.

    Removing an owner proves outside content, NOT its insertion position. Both
    the native offset and complete pristine paragraph are independently bound.
    """
    digest = nf._object_digest(owner, model['resolver'])
    matches = []
    for index, paragraph in enumerate(model['body_paragraphs']):
        offset = 0
        for atom in nf._atoms(paragraph, model['chars'], model['resolver']):
            if atom.get('control') == 'rectangle' and atom.get('digest') == digest:
                matches.append((index, offset, paragraph))
            if type(atom.get('width')) is not int or atom['width'] < 0:
                raise CensusError('Created owner native location has an unresolved width.')
            offset += atom['width']
    if digest is None or len(matches) != 1:
        raise CensusError('Created owner has no unique body atom location.')
    index, offset, paragraph = matches[0]
    end_shell = _created_end_shell(model, owner, anchor)
    if (0, index, offset) != anchor['position'] or _signature(model, element=paragraph, remove=owner, remove_shell=end_shell) != anchor['paragraph_signature']:
        raise CensusError('Created owner is not at the exact pristine request anchor.')
    return end_shell


def _enter_created(native, model, item, action):
    """Planned empty -> one Front -> text edit, never a failed-read fallback.

    Prior constructor selection is NOT a cardinality oracle. Clear only a
    positively known object state, then construct and prove a new singleton.
    """
    incoming = native.witness()
    if incoming[2] not in (0, 4, 20):
        native.hold('constructor.state')
    try:
        unlocked = proof.attribute(proof.one(item['_owner'], 'SHAPEOBJECT'), 'Lock') in ('false', '0')
        ungrouped = proof.grouping_problem(item['_owner'], item['kind']) is None
    except Exception as exc:
        native.failed = True
        raise _read_failure('Created object selection context is unproven.', 'constructor.identity', 'raised') from exc
    # New EMPTY constructors intentionally do not meet nonempty exact-edit
    # admission yet. Preserve every other parsed owner/list/format restriction.
    ready = item['editable'] or (item['text'] == '' and item['refusal_code'] == 'container_text_unsupported')
    if not unlocked or not ungrouped or not ready:
        native.hold('constructor.identity')
    binding = _binding(native, model, item, phase='constructor.initial')
    item['_native_binding'] = (model, binding)
    if incoming[2] in (4, 20) and incoming[3] != ('gso', binding[2]):
        native.hold('constructor.identity')
    if native.witness() != incoming:
        native.hold('binding.state')

    def empty():
        state = native.witness()
        if state[2] != 0 or state[1][0] is not False or state[3] is not None:
            native.hold('constructor.state')
        native.call('CurSelectedCtrl.empty', lambda: native.com.CurSelectedCtrl, 'void')
        if native.witness() != state:
            native.hold('constructor.state')
        return state

    if incoming[2] in (4, 20):
        native.call('constructor.Cancel', lambda: action.Run('Cancel'), 'true')
    empty()
    native.read_unchanged(model, 'constructor.preservation')
    native.at(binding[1])
    empty()
    if _binding(native, model, item, phase='constructor.positioned') != binding:
        native.hold('binding.stale')
    # Empty selection + ordinary Front on an unlocked/ungrouped unique owner
    # establishes singleton cardinality. Mode4/CurSelectedCtrl alone does not.
    native.call('constructor.Front', lambda: action.Run('SelectCtrlFront'), 'true')
    selected = native.witness()
    if selected[2] != 4 or selected[3] != ('gso', binding[2]):
        native.hold('constructor.identity')
    if native.owner() != (None, None):
        native.hold('constructor.identity')
    if _binding(native, model, item, phase='constructor.selected') != binding or native.witness() != selected:
        native.hold('binding.stale')
    # HAction.Run is BOOL(BSTR), not the VOID IHwpObject.Run wrapper.
    native.call('constructor.TextBoxEdit', lambda: action.Run('ShapeObjTextBoxEdit'), 'true')
    entered = empty()
    if entered[0][0] == 0 or entered[0][1:] != (0, 0):
        native.hold('constructor.identity')
    _require_owner(native, item, 'created_box_owner')
    if native.witness() != entered:
        native.hold('constructor.state')
    native.read_unchanged(model, 'constructor.preservation')


def _create(native, before, request, original, on_mutating, entry_witness):
    if tuple(request['expected_pos']) != original[0] or original[0][0] != 0 or native.owner() != (None, None):
        _refuse('container_wrong_caret', 'Creation requires the exact current body caret.')
    anchor = _creation_anchor(before, original[0])
    com = native.com
    action = native.call('HAction.create', lambda: com.HAction, 'object')
    params = native.call('HParameterSet.create', lambda: com.HParameterSet, 'object')
    pset = native.call('HShapeObject', lambda: params.HShapeObject, 'object')
    hset = native.call('HShapeObject.HSet', lambda: pset.HSet, 'object')
    name = 'DrawObjCreatorTextBox'
    native.call('TextBox.GetDefault', lambda: action.GetDefault(name, hset), 'true')
    # Inline horizontal rectangle only; no floating/group/linked/vertical claims.
    # ShapeObject table 105: WidthRelTo=4 and HeightRelTo=2 mean absolute
    # HWPUNIT. Do not inherit percentage modes from GetDefault.
    items = {'WidthRelTo': 4, 'HeightRelTo': 2, 'Width': request['width_hu'], 'Height': request['height_hu'], 'TreatAsChar': 1, 'AdjustTextbox': 1}
    for key, value in items.items():
        native.call('ShapeObject.SetItem.' + key, lambda: hset.SetItem(key, value), 'void')
        got = native.call('ShapeObject.get.' + key, lambda: getattr(pset, key), 'coordinate' if key in ('Width', 'Height') else 'ui2')
        if got != value:
            native.hold('ShapeObject.item_mismatch')
    layout = native.call('ShapeDrawLayOut', lambda: pset.ShapeDrawLayOut, 'object')
    w, h = request['width_hu'], request['height_hu']
    points = [0, 0, w, 0, w, h, 0, h]
    native.call('CreateItemArray', lambda: layout.CreateItemArray('CreatePt', 8), 'void')
    array = native.call('CreatePt', lambda: layout.CreatePt, 'object')
    for i, value in enumerate(points):
        native.call('CreatePt.SetItem', lambda: array.SetItem(i, value), 'void')
    native.call('CreateNumPt.put', lambda: setattr(layout, 'CreateNumPt', 4), 'void')
    # All typed array/count values are exact, bounded integers before Execute.
    if native.call('CreateNumPt.get', lambda: layout.CreateNumPt, 'coordinate') != 4 or native.call('CreatePt.Count', lambda: array.Count, 'coordinate') != 8:
        native.hold('CreatePt.count')
    for i, value in enumerate(points):
        if native.call('CreatePt.Item', lambda: array.Item(i), 'coordinate') != value:
            native.hold('CreatePt.coordinate')
    if native.witness() != entry_witness:
        native.hold('creation_caret_drift')
    if _signature(native.read()) != _signature(before) or native.witness() != entry_witness:
        native.hold('pre_create_document_drift')
    if on_mutating:
        on_mutating()
    native.issued = True
    native.call('TextBox.Execute', lambda: action.Execute(name, hset), 'true')
    made = native.read()
    old_ids = {x['target_id'] for x in before['containers']}
    new = [x for x in made['containers'] if x['target_id'] not in old_ids]
    if len(new) != 1 or new[0]['kind'] != 'text_box' or new[0]['text'] != '' or not new[0]['native_id']:
        native.hold('new_text_box_identity')
    item = new[0]
    # FIRST post-Execute evidence: no _locate, restoration, text insertion or
    # other SDK call until location AND actual usable drawing geometry pass.
    try:
        end_shell = _prove_created_anchor(made, item['_owner'], anchor)
        proof.prove_created_geometry(item['_owner'], w, h)
    except Exception as exc:
        native.failed = True
        raise _read_failure('Created owner anchor or actual geometry is unproven.', 'created_box_geometry', 'raised') from exc
    if _signature(before) != _signature(made, remove=item['_owner'], remove_shell=end_shell):
        native.hold('creation_outside_preservation')
    _enter_created(native, made, item, action)
    _require_owner(native, item, 'created_box_owner')
    native.insert(request['text'], on_mutating)
    post_read_state = native.witness()
    after = native.read()
    later = next((x for x in after['containers'] if x['target_id'] == item['target_id']), None)
    end_shell = _created_end_shell(after, later['_owner'], anchor) if later is not None else None
    if later is None or later['text'] != request['text'] or not later['editable'] or _signature(before) != _signature(after, remove=later['_owner'], remove_shell=end_shell):
        native.hold('created_box_text_and_preservation')
    # Content insertion may not change the already proven frame or location.
    try:
        _prove_created_anchor(after, later['_owner'], anchor)
        proof.prove_created_geometry(later['_owner'], w, h)
    except Exception as exc:
        native.failed = True
        raise _read_failure('Created owner geometry or anchor drifted during text insertion.', 'created_box_geometry.after_text', 'raised') from exc
    if native.witness() != post_read_state:
        native.hold('read_purity')
    later['_native_binding'] = (after, item['_native_binding'][1])
    _require_owner(native, later, 'created_box_owner')
    # Insertion changes the body offset; keep the proven new text caret instead
    # of pretending the old body coordinate still denotes the old character.
    return {'read_only': False, 'container': public(later), 'result': {'changed': True, 'status': 'created'},
            'preservation': 'complete_resolved_document_except_new_inline_box', 'warnings': ['Native save/reopen and every-page render qualification required.']}
