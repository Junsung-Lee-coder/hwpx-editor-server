"""Existing-picture bindings and native operations; HWPML2X is read, never rewritten.

Native action authority: Hancom PictureChange and ShapeObject/DrawImageAttr.
Qualification of actual mutating variants is recorded separately from this reader.
"""
from __future__ import annotations

import hashlib
import base64
import copy
import io
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, NoReturn

from app import hwpx_saved_proof
from app import native_numbering as nn
from app import picture_preservation, picture_public, picture_request_proof
from app.native_numbering_census import (
    MAX_HWPML_CHARS, _DOCTYPE, _MAX_DEPTH, _Resolver, _attr, _name, payload_identity,
)

OPS = ('picture_inventory', 'picture_replace_exact', 'picture_crop_exact', 'picture_layout_exact')
ENVELOPE = {'op', 'operation', 'label'}
BINDING = {'target_id', 'expected_hash', 'confirm_layout'}
FIELDS = {
    'picture_inventory': ENVELOPE,
    'picture_replace_exact': ENVELOPE | BINDING | {'source_path'},
    'picture_crop_exact': ENVELOPE | BINDING | {'left', 'top', 'right', 'bottom'},
    'picture_layout_exact': ENVELOPE | BINDING | {'mode', 'horizontal_offset_mm', 'vertical_offset_mm'},
}


def _number(value: Any, name: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise nn.NativeNumberingInputError(f'{name} must be a finite number in {low}..{high}, not bool')
    return float(value)


def validate_step(step: Mapping[str, Any]) -> dict[str, Any]:
    op = step.get('op', step.get('operation'))
    if op not in FIELDS:
        raise nn.NativeNumberingInputError('unknown existing-picture operation')
    nn._reject_unknown_keys(step, tuple(FIELDS[op]), op)
    if op == 'picture_inventory':
        return {'op': op}
    target = step.get('target_id')
    expected = step.get('expected_hash')
    if type(target) is not str or not re.fullmatch(r'picture:[0-9]{1,20}', target):
        raise nn.NativeNumberingInputError('target_id must be the exact picture:<SHAPEOBJECT InstId> from picture_inventory')
    if type(expected) is not str or not re.fullmatch(r'sha256:[0-9a-f]{24}', expected):
        raise nn.NativeNumberingInputError('expected_hash must be the exact picture_inventory hash')
    if step.get('confirm_layout') is not True:
        raise nn.NativeNumberingInputError('confirm_layout=true is required')
    request = {'op': op, 'target_id': target, 'expected_hash': expected}
    if op == 'picture_replace_exact':
        source = step.get('source_path')
        if type(source) is not str or not 1 <= len(source) <= 4096 or '\x00' in source:
            raise nn.NativeNumberingInputError('source_path must be an absolute supported image path')
        request['source_path'] = source
    elif op == 'picture_crop_exact':
        for key in ('left', 'top', 'right', 'bottom'):
            request[key] = _number(step.get(key), key, 0, 1)
        if request['left'] >= request['right'] or request['top'] >= request['bottom']:
            raise nn.NativeNumberingInputError('normalized crop requires left<right and top<bottom')
    else:
        mode = step.get('mode')
        if type(mode) is not str or mode not in ('inline', 'square', 'top_bottom', 'behind', 'in_front'):
            raise nn.NativeNumberingInputError('mode must be inline, square, top_bottom, behind or in_front')
        request['mode'] = mode
        for key in ('horizontal_offset_mm', 'vertical_offset_mm'):
            if key in step:
                request[key] = _number(step[key], key, -300, 300)
        if mode == 'inline' and any(request.get(key, 0) != 0 for key in ('horizontal_offset_mm', 'vertical_offset_mm')):
            raise nn.NativeNumberingInputError('inline pictures do not take floating offsets')
    return request


def _child(element: ET.Element, name: str) -> ET.Element | None:
    found = [e for e in element if _name(e) == name]
    return found[0] if len(found) == 1 else None


def _facts(element: ET.Element | None) -> dict[str, str] | None:
    return dict(element.attrib) if element is not None else None


def _ungrouped_component(shape, component):
    """Require native SHAPECOMPONENT.GroupLevel; never infer it from SHAPEOBJECT."""
    if shape is None or component is None or component.get('GroupLevel') != '0':
        return False
    for attributes in (shape, component):
        for key in attributes:
            if key.rsplit('}', 1)[-1].casefold() == 'grouplevel' and key != 'GroupLevel':
                return False
    # A misplaced shadow cannot override or conflict with the authoritative field.
    return 'GroupLevel' not in shape or shape['GroupLevel'] == '0'


def parse_pictures(text: str) -> dict[str, Any]:
    """Exact HWPML picture identity, frame, crop, layout, caption and media mapping.

    SHAPEOBJECT InstId and SHAPECOMPONENT InstID are kept distinct. A malformed
    picture remains visible as unsupported, never silently omitted or guessed.
    """
    if type(text) is not str or len(text) > MAX_HWPML_CHARS or _DOCTYPE.search(text):
        raise nn.NativeNumberingReadFailure('picture preservation source is not usable HWPML2X', code='picture_state_unproven')
    try:
        root = ET.fromstring(text)
        body = next(e for e in root.iter() if _name(e) == 'body')
        resolver = _Resolver(root, body)
    except Exception as exc:
        raise nn.NativeNumberingReadFailure('picture preservation source cannot be resolved', code='picture_state_unproven') from exc
    items = [e for e in root.iter() if _name(e) == 'binitem']
    data = {(_attr(e, 'id') or '').strip(): payload_identity(e.text)
            for e in root.iter() if _name(e) == 'bindata'}
    pictures = []

    def visit(element: ET.Element, ancestry: tuple[ET.Element, ...], path: tuple[int, ...]) -> None:
        if len(ancestry) > _MAX_DEPTH:
            raise nn.NativeNumberingReadFailure('picture source nesting is too deep', code='picture_state_unproven')
        if _name(element) == 'picture':
            shape = _child(element, 'shapeobject')
            component = _child(element, 'shapecomponent')
            ident = shape.get('InstId') if shape is not None else None
            images = [e for e in element.iter() if _name(e) == 'image']
            media = []
            for image in images:
                raw_index = _attr(image, 'binitem')
                number = int(raw_index) if raw_index and raw_index.isdigit() else 0
                item = items[number-1] if 1 <= number <= len(items) else None
                reference = (_attr(item, 'bindata') or _attr(item, 'id')) if item is not None else None
                media.append({'bin_item': raw_index, 'member_id': reference,
                              'payload_sha256': data.get((reference or '').strip())})
            caption = _child(shape, 'caption') if shape is not None else None
            if caption is None:
                caption = _child(element, 'caption')
            reasons = []
            if not ident or not ident.isdigit():
                reasons.append('picture_identity_unproven')
            if (any(_name(e) in {'container', 'group', 'header', 'footer', 'footnote', 'endnote'} for e in ancestry)
                    or not _ungrouped_component(_facts(shape), _facts(component))):
                reasons.append('picture_context_unqualified')
            if len(media) != 1 or not media[0]['payload_sha256']:
                reasons.append('picture_media_unproven')
            try:
                resolved = resolver.digest(element)
            except Exception as exc:
                raise nn.NativeNumberingReadFailure('picture references cannot be resolved', code='picture_state_unproven') from exc
            binding = {'native_shapeobject_instid': ident, 'path': path,
                       'picture': resolved, 'media': media}
            digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:24]
            pictures.append({
                'target_id': 'picture:' + ident if ident else None,
                'hash': 'sha256:' + digest,
                'native_shapeobject_instid': ident,
                'native_component_instid': component.get('InstID') if component is not None else None,
                'context': 'table_cell' if any(_name(e) == 'cell' for e in ancestry) else 'body',
                'frame': _facts(_child(shape, 'size')) if shape is not None else None,
                'placement': _facts(_child(shape, 'position')) if shape is not None else None,
                'shape': _facts(shape), 'component': _facts(component),
                'image_rect': _facts(_child(element, 'imagerect')),
                'crop': _facts(_child(element, 'imageclip')),
                'source_dimensions': _facts(_child(element, 'imagedim')),
                'media': media,
                'caption': {'present': caption is not None,
                            'digest': resolver.digest(caption) if caption is not None else None,
                            'text': ''.join(caption.itertext()) if caption is not None else None},
                'supported': not reasons, 'refusal_reasons': reasons,
                '_element': element, '_path': path,
            })
        for index, child in enumerate(element):
            visit(child, ancestry + (element,), path + (index,))
    visit(body, (), ())
    identifiers = [p['target_id'] for p in pictures if p['target_id'] is not None]
    for picture in pictures:
        if picture['target_id'] is not None and identifiers.count(picture['target_id']) != 1:
            picture['supported'] = False
            picture['refusal_reasons'].append('picture_identity_ambiguous')
    return {'root': root, 'body': body, 'resolver': resolver, 'pictures': pictures}


def public_inventory(model: Mapping[str, Any]) -> dict[str, Any]:
    if len(model['pictures']) > picture_public.MAX_PICTURES:
        _refuse('Picture inventory supports at most 1000 pictures per document; nothing was edited.', 'picture_inventory_limit')
    pictures = [{key: value for key, value in picture.items() if not key.startswith('_')}
                for picture in model['pictures']]
    return {'schema_version': 'local-cli/picture-inventory/v1', 'read_only': True,
            'pictures': pictures, 'complete': True, 'total_count': len(pictures),
            'edit_contract': {'implemented_contexts': ['body_without_native_caption'],
                              'replacement': 'regular backend-host PNG, same decoded aspect ratio',
                              'crop': 'normalized source bounds 0..1, positive native-unit extent, unchanged frame/payload',
                              'layout_modes': ['inline', 'square'],
                              'native_edit_qualified': False, 'native_history_qualified': False,
                              'history': 'changed picture steps refuse Undo/Redo before native dispatch; no-op companions preserve earlier history',
                              'unqualified_contexts': ['table_cell', 'native_caption', 'group', 'header_footer', 'other_nested'],
                              'reason': 'native edit/Undo/Redo/save/reopen qualification is incomplete'},
            'identity_namespace': 'HWPML SHAPEOBJECT InstId; never SHAPECOMPONENT InstID',
            'qualification': 'inventory is structural discovery; mutating variant qualification is separate'}


def inventory(hwp: Any, step: Mapping[str, Any]) -> dict[str, Any]:
    validate_step(step)
    text = nn._sdk_read(lambda: hwp.get_text_file('HWPML2X', ''))
    return public_inventory(parse_pictures(text))


class _Calls:
    """First-failure latch; optional case-owned witness, never private repr."""
    def __init__(self, witness=None):
        self.failed = False
        self.issued = False
        self.witness = witness
        self.events = []

    def event(self, name, stage, value=None, arguments=None):
        row = {'phase': name, 'stage': stage}
        if arguments is not None:
            # Only source-constructed public IDs, exact coordinates and action names.
            row['arguments'] = dict(arguments)
        if stage == 'returned' or stage == 'invalid_return':
            if value is None:
                row['returned'] = {'builtin': 'NoneType', 'value': None}
            elif any(type(value) is known for known in (bool, int, float)):
                row['returned'] = {'builtin': type(value).__name__, 'value': value}
            elif type(value) is str:
                row['returned'] = {'builtin': 'str', 'chars': len(value),
                                   'sha256': hashlib.sha256(value.encode()).hexdigest()}
            elif any(type(value) is k for k in (tuple, list)) and len(value) == 3 and all(type(x) is int for x in value):
                row['returned'] = {'builtin': type(value).__name__, 'value': list(value)}
            else:
                row['returned'] = {'builtin': False, 'opaque': True}
        self.events.append(row)
        if self.witness is not None:
            try:
                self.witness(dict(row))
            except BaseException:
                self.failed = True
                raise

    def call(self, name, fn, expected, *, arguments=None, native_type=None) -> Any:
        if self.failed:
            raise nn.NativeNumberingReadFailure('Picture SDK boundary already failed.', code='picture_state_unproven')
        self.event(name, 'before', arguments=arguments)
        try:
            value = nn._sdk_read(fn)
        except BaseException:
            self.failed = True
            self.event(name, 'exception')
            raise
        t = type(value)
        valid = ((expected == 'str' and t is str) or
                 (expected == 'id' and t is str and value.isascii() and value.isdigit()) or
                 (expected == 'int' and t is int) or
                 (expected == 'uint16' and t is int and 0 <= value < 2**16) or
                 (expected == 'flag16' and t is int and value in (0, 1)) or
                 (expected == 'flow16' and t is int and value in (0, 1, 2, 3)) or
                 (expected == 'uint32' and t is int and 0 <= value < 2**32) or
                 (expected == 'sint32' and t is int and -2**31 <= value < 2**31) or
                 (expected == 'native_type' and native_type is not None and t is native_type) or
                 (expected == 'coordinate' and t is int and 0 <= value < 2**31) or
                 (expected == 'body_list' and t is int and value == 0) or
                 (expected == 'position' and any(t is k for k in (tuple, list)) and
                  len(value) == 3 and all(type(x) is int and 0 <= x < 2**31 for x in value)) or
                 (expected == 'true' and value is True) or
                 (expected == 'void' and value is None) or
                 (expected == 'none' and value is None) or
                 (expected == 'object' and value is not None and all(t is not known for known in (str, int, bool, float, list, dict, tuple))) or
                 (expected == 'object_or_none' and (value is None or all(t is not known for known in (str, int, bool, float, list, dict, tuple)))))
        if not valid:
            self.failed = True
            self.event(name, 'invalid_return', value)
            raise nn.NativeNumberingReadFailure('Picture SDK returned an unproved value at ' + name,
                                                code='picture_state_unproven')
        self.event(name, 'returned', value)
        return value


def _refuse(message, code) -> NoReturn:
    raise nn.NativeNumberingRefusal(message, code=code)


def _target(model, request):
    found = [p for p in model['pictures'] if p['target_id'] == request['target_id']]
    if len(found) != 1:
        _refuse('The exact picture is missing or ambiguous.', 'picture_target_unproven')
    target = found[0]
    if target['hash'] != request['expected_hash']:
        _refuse('The picture frame, content or media changed. Rediscover it.', 'picture_binding_stale')
    if not target['supported']:
        _refuse('This structural picture context is not supported.', 'picture_context_unqualified')
    if target['context'] != 'body' or target['caption']['present']:
        _refuse('Body pictures without native captions are the implemented edit context; nested/caption contexts require native qualification.',
                'picture_context_unqualified')
    return target


def _outside(model, target):
    """Analysis copy only; never serialize it into Hancom or a document."""
    body = copy.deepcopy(model['body'])
    path = target['_path']
    parent = body
    for index in path[:-1]:
        parent = list(parent)[index]
    parent.remove(list(parent)[path[-1]])
    return model['resolver'].digest(body)


def _head_without_media(model):
    heads = [e for e in model['root'] if _name(e) == 'head']
    if len(heads) != 1:
        raise nn.NativeNumberingReadFailure('Complete HEAD preservation is unavailable.', code='picture_state_unproven')
    head = copy.deepcopy(heads[0])
    for parent in list(head.iter()):
        for child in list(parent):
            if _name(child) == 'binitem':
                parent.remove(child)
        if _name(parent) == 'bindatalist':
            parent.attrib.pop('Count', None)
    return ET.tostring(head, encoding='unicode')


def _selection_universe(model, target):
    """Complete parsed control census, not a document-wide singleton whitelist.

    Cardinality comes from a verified empty selection followed by one ordinary
    Front action on an unlocked, ungrouped body target. CurSelectedCtrl alone
    and SelectionMode=4 alone never prove cardinality. Every native control is
    walked, and all drawing IDs must agree with the full parsed object census.
    Unrelated body/head/media proofs remain complete and request-bound.
    """
    if any(_name(e) != 'section' for e in model['body']):
        _refuse('The BODY must contain only HWPML SECTION elements.',
                'picture_context_unqualified')
    if ((target['shape'] or {}).get('Lock') not in ('false', '0') or
            not _ungrouped_component(target['shape'], target['component'])):
        _refuse('The target must be unlocked and ungrouped.', 'picture_context_unqualified')
    control_tags = {'secdef':'secd', 'coldef':'cold', 'table':'tbl',
        'header':'head', 'footer':'foot', 'footnote':'fn', 'endnote':'en',
        'autonum':'atno', 'newnum':'nwno', 'pagenumctrl':'pgct',
        'pagehiding':'pghd', 'pagenumpos':'pgnp', 'bookmark':'bokm',
        'indexmark':'idxm', 'dutmal':'tdut'}
    counts = {kind: 0 for kind in control_tags.values()}
    parents = {child: parent for parent in model['body'].iter() for child in parent}
    ids = set()
    for element in model['body'].iter():
        name = _name(element)
        if name in control_tags:
            counts[control_tags[name]] += 1
        if name not in ('shapeobject', 'shapecomponent'):
            continue
        parent = parents.get(element)
        if parent is None or _name(parent) in ('body','section','p','text','char'):
            _refuse('An orphan native shape makes the parsed control census ambiguous.',
                    'picture_context_unqualified')
        if name == 'shapeobject' and _name(parent) != 'table':
            ident = element.get('InstId')
            if type(ident) is not str or not ident.isascii() or not ident.isdigit() or ident in ids:
                _refuse('The complete drawing identity census is ambiguous.', 'picture_context_unqualified')
            ids.add(ident)
    counts['gso'] = len(ids)
    return {'counts': counts, 'gso_ids': ids}


def _proof(calls, condition, phase):
    if not condition:
        calls.failed = True
        calls.event(phase, 'invalid_proof')
        raise nn.NativeNumberingReadFailure('Picture identity/anchor proof failed at ' + phase,
                                            code='picture_state_unproven')


def _native_target(com, target, calls):
    """Observe actual GetCtrlInstID first; never select an XML ID by assumption.

    A fresh same-control equality with SHAPEOBJECT InstId is required on every
    call. SHAPECOMPONENT InstID is a separate namespace, never an OR alias.
    """
    ctrl = calls.call('HeadCtrl', lambda: com.HeadCtrl, 'object_or_none')
    found = []
    seen = set()
    expected = target['_selection_universe']
    counts = {kind: 0 for kind in expected['counts']}
    for index in range(2048):
        if ctrl is None:
            break
        current = ctrl
        kind = calls.call(f'control[{index}].CtrlID', lambda: current.CtrlID, 'str')
        _proof(calls, kind in counts, f'control[{index}].CtrlID')
        counts[kind] += 1
        _proof(calls, counts[kind] <= expected['counts'][kind], f'control[{index}].CtrlID')
        if kind == 'gso':
            native_id = calls.call(f'control[{index}].GetCtrlInstID', lambda: current.GetCtrlInstID(), 'id')
            _proof(calls, native_id not in seen, 'native.duplicate_control_identity')
            seen.add(native_id)
            if native_id == target['native_shapeobject_instid']:
                found.append((current, native_id))
        ctrl = calls.call(f'control[{index}].Next', lambda: current.Next, 'object_or_none')
    else:
        _proof(calls, False, 'native.complete_control_census_bound')
    if len(found) != 1:
        _refuse('No unique current native GetCtrlInstID mapping exists for this picture.', 'picture_native_mapping_unqualified')
    _proof(calls, counts == expected['counts'] and seen == expected['gso_ids'],
           'native.complete_control_universe')
    return found[0]


def _selection(com, native_id, calls):
    mode = calls.call('selected.SelectionMode', lambda: com.SelectionMode, 'int')
    _proof(calls, mode == 4, 'selected.SelectionMode.object')
    selected = calls.call('CurSelectedCtrl', lambda: com.CurSelectedCtrl, 'object')
    actual = calls.call('selected.GetCtrlInstID', lambda: selected.GetCtrlInstID(), 'id')
    _proof(calls, actual == native_id, 'selected.GetCtrlInstID')
    kind = calls.call('selected.CtrlID', lambda: selected.CtrlID, 'str')
    _proof(calls, kind == 'gso', 'selected.CtrlID')
    calls.call('ParentCtrl.body', lambda: com.ParentCtrl, 'none')


def _clear_selection(com, action, calls):
    mode = calls.call('preselection.SelectionMode', lambda: com.SelectionMode, 'int')
    _proof(calls, mode in (0,1,2,3,4,17,18,19,20), 'preselection.SelectionMode.known')
    if mode != 0:
        calls.call('HAction.Run.Cancel', lambda: action.Run('Cancel'), 'true',
                   arguments={'action':'Cancel'})
        mode = calls.call('cleared.SelectionMode', lambda: com.SelectionMode, 'int')
    _proof(calls, mode == 0, 'cleared.SelectionMode.none')
    calls.call('cleared.CurSelectedCtrl', lambda: com.CurSelectedCtrl, 'none')


def _image_file(source: str, asset_root: Any):
    from PIL import Image
    path = Path(source)
    if not path.is_absolute() or path.suffix.lower() != '.png' or path.is_symlink():
        _refuse('Replacement requires an absolute regular PNG path on the backend host.', 'picture_source_unsupported')
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size < 8 or stat.st_size > 20_000_000:
            _refuse('Replacement image file size is unsupported.', 'picture_source_unsupported')
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or image.width * image.height > 20_000_000:
                _refuse('Replacement image is not a supported bounded PNG.', 'picture_source_unsupported')
            image.load()
            pixels = hashlib.sha256(image.convert('RGBA').tobytes()).hexdigest()
            dimensions = image.size
    except nn.NativeNumberingRefusal:
        raise
    except Exception as exc:
        raise nn.NativeNumberingRefusal('Replacement PNG is missing or unreadable.', code='picture_source_unreadable') from exc
    if asset_root is None or not Path(asset_root).is_absolute() or not Path(asset_root).is_dir():
        _refuse('An owned live-session asset root is required.', 'picture_asset_root_unproven')
    folder = Path(asset_root) / 'picture-assets'
    if folder.is_symlink() or getattr(folder, 'is_junction', lambda: False)():
        _refuse('Replacement asset root is linked.', 'picture_asset_root_unproven')
    folder.mkdir(exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    staged = folder / (digest + '.png')
    if staged.exists():
        if staged.is_symlink() or hashlib.sha256(staged.read_bytes()).hexdigest() != digest:
            _refuse('Existing staged image identity is unproved.', 'picture_asset_root_unproven')
    else:
        with staged.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {'path': str(staged), 'sha256': digest, 'pixels': pixels, 'dimensions': dimensions}


def _pixels(model, target):
    from PIL import Image
    member = target['media'][0]['member_id']
    data = [e for e in model['root'].iter() if _name(e) == 'bindata' and (_attr(e, 'id') or '').strip() == member]
    if len(data) != 1:
        raise nn.NativeNumberingReadFailure('Exact picture payload is unavailable.', code='picture_state_unproven')
    try:
        encoded = base64.b64decode(''.join((data[0].text or '').split()), validate=True)
        with Image.open(io.BytesIO(encoded)) as image:
            if image.width * image.height > 20_000_000:
                raise ValueError('image bound')
            return hashlib.sha256(image.convert('RGBA').tobytes()).hexdigest(), image.size
    except Exception as exc:
        raise nn.NativeNumberingReadFailure('Embedded picture pixels cannot be proven.', code='picture_state_unproven') from exc


def _strategy(saved_proof):
    """Select the preservation authority BEFORE any SDK effect; never after a failed proof."""
    try:
        profile = hwpx_saved_proof.load_profile()
    except hwpx_saved_proof.ProofUnavailable as exc:
        _refuse('The configured HWPX saved-proof profile is unusable: ' + str(exc)[:160] + '; nothing was edited.',
                'picture_saved_proof_unqualified')
    if profile is None:
        return None
    if (profile.data['export_native_dirty'] == 'cleared'
            and profile.data['public_save']['save_false_after_export_clear'] is not True):
        # Every changed edit would leave the native flag False with logical debt and no
        # public save could complete it: refuse instead of admitting an unsaveable edit.
        _refuse('The configured HWPX profile clears the native modified flag on export but declares no completing '
                'public save; nothing was edited.', 'picture_saved_proof_unqualified')
    if saved_proof is None:
        _refuse('The HWPX saved-proof strategy needs the owning live session private-copy custody; nothing was edited.',
                'picture_saved_proof_unqualified')
    if Path(saved_proof.working_copy_path).suffix.lower() != '.hwpx':
        _refuse('The HWPX saved-proof strategy needs an .hwpx working copy so the public save can be verified; nothing was edited.',
                'picture_saved_proof_unqualified')
    if profile.data['caret'] is None:
        # A changed picture edit selects its anchor; the move must be serialized-mapped.
        _refuse('The configured HWPX profile has no serialized caret mapping for the target-anchor move; nothing was edited.',
                'picture_saved_proof_unqualified')
    return profile


def edit(hwp: Any, step: Mapping[str, Any], *, mark_mutating=None, asset_root=None, witness=None,
         saved_proof=None) -> dict[str, Any]:
    """Three native edit implementations; preservation authority chosen upfront.

    Without a configured profile the unchanged legacy HWPML strict comparison is
    the authority. With one, complete native HWPX private copies before and
    after the action are. This is not a native qualification badge.
    """
    request = validate_step(step)
    op = request['op']
    if op == 'picture_inventory':
        return inventory(hwp, step)
    if op == 'picture_layout_exact' and request['mode'] not in ('inline', 'square'):
        _refuse('Only inline and square have an implemented layout contract.', 'picture_layout_unqualified')
    profile = _strategy(saved_proof)
    strategy = hwpx_saved_proof.STRATEGY if profile is not None else hwpx_saved_proof.LEGACY
    source = _image_file(request['source_path'], asset_root) if op == 'picture_replace_exact' else None
    calls = _Calls(witness)
    calls.event('proof.strategy', 'declared_before_effect', arguments={'strategy': strategy,
                **({'profile_sha256': profile.sha256, 'profile_id': profile.data['profile_id']} if profile else {})})
    text = calls.call('before.GetTextFile.HWPML2X', lambda: hwp.get_text_file('HWPML2X', ''), 'str')
    before = parse_pictures(text)
    target = _target(before, request)
    noop = False
    if op == 'picture_crop_exact':
        try:
            sx, sy = int(target['source_dimensions']['Width']), int(target['source_dimensions']['Height'])
            clip = {'Left': round(sx * request['left']), 'Top': round(sy * request['top']),
                    'Right': round(sx * request['right']), 'Bottom': round(sy * request['bottom'])}
            if sx <= 0 or sy <= 0:
                raise ValueError('source dimensions')
            if clip['Left'] >= clip['Right'] or clip['Top'] >= clip['Bottom']:
                _refuse('Crop has no visible extent in native source units.', 'picture_crop_empty')
            noop = all(int(target['crop'][k]) == v for k, v in clip.items())
        except nn.NativeNumberingRefusal:
            raise
        except Exception as exc:
            raise nn.NativeNumberingReadFailure('Current crop is unproved.', code='picture_state_unproven') from exc
    elif op == 'picture_layout_exact':
        placement = target['placement'] or {}
        inline = placement.get('TreatAsChar') == 'true'
        noop = (inline if request['mode'] == 'inline' else
                not inline and target['shape'].get('TextWrap') == 'Square')
        for public, native in (('horizontal_offset_mm', 'HorzOffset'), ('vertical_offset_mm', 'VertOffset')):
            if public in request:
                noop = noop and placement.get(native) == str(round(request[public] * 7200 / 25.4))
    if noop:
        # No native copy, selection or action: conditional no-call semantics.
        return {'ok': True, 'applied': False, 'changed': False, 'native_actions_issued': 0,
                'reason': 'already_requested_state', 'target_id': target['target_id'], 'hash': target['hash'],
                'history_qualified': True, 'history_reason': 'no_native_edit', 'preservation_strategy': strategy}
    if source is not None:
        current_pixels, dimensions = _pixels(before, target)
        if current_pixels == source['pixels']:
            return {'ok': True, 'applied': False, 'changed': False, 'native_actions_issued': 0,
                    'reason': 'same_decoded_pixels', 'target_id': target['target_id'], 'hash': target['hash'],
                    'preservation_strategy': strategy}
        if dimensions[0] * source['dimensions'][1] != dimensions[1] * source['dimensions'][0]:
            _refuse('Replacement aspect ratio must match the current source image.', 'picture_aspect_unqualified')
    outside = _outside(before, target)
    universe = _selection_universe(before, target)
    target = {**target, '_selection_universe': universe}
    com = hwp.hwp
    before_package = link = reflow = None
    if profile is not None:
        # Current in-memory document INCLUDING earlier unsaved edits, as a complete
        # owned private copy; never the input file or the on-disk working copy.
        before_package = calls.call('proof.hwpx.before', lambda: saved_proof.copy('before', profile), 'object')
        # The copy IS the current in-memory state until the next mutating intent:
        # a later refusal must not leave an export-cleared flag without a save proof.
        calls.call('proof.hwpx.record_current', lambda: saved_proof.record(before_package, profile, 'before_copy'), 'none')
        try:
            link = hwpx_saved_proof.bridge(before, target, before_package, request)
        except (hwpx_saved_proof.BridgeUnproven, hwpx_saved_proof.PackageMismatch, hwpx_saved_proof.XmlUnproved, ET.ParseError) as exc:
            _refuse('The HWPML target cannot be bridged to exactly one HWPX picture by physical facts ('
                    + str(exc)[:160] + '); nothing was selected or edited.', 'picture_saved_bridge_unproven')
        try:
            # Bound from the before package and the request only, before any effect.
            reflow = hwpx_saved_proof.reflow_scope(before_package, link, request, profile)
        except hwpx_saved_proof.ProofUnavailable as exc:
            _refuse('The target paragraph line layout cache cannot be bound for a declared reflow ('
                    + str(exc)[:160] + '); nothing was selected or edited.', 'picture_saved_proof_unqualified')
        if reflow is not None:
            calls.event('proof.hwpx.line_reflow', 'declared_before_effect', arguments=reflow.public())
        if op == 'picture_replace_exact':
            unknown = hwpx_saved_proof.replacement_admissible(before_package, link, profile)
            if unknown:
                # Actual native manifests carry e.g. hashkey, whose value for a NEW item is not derivable.
                _refuse('A new replacement manifest item cannot be proved: attribute(s) ' + ', '.join(unknown)[:120]
                        + ' have no qualified new-item meaning in the configured profile; nothing was selected or edited.',
                        'picture_saved_proof_unqualified')
    original_position = calls.call('GetPos.original', lambda: com.GetPos(), 'position')
    serialized_position = calls.call('proof.ui.original_serialized',
        lambda: picture_request_proof.caret(before['root'])[1], 'position')
    _proof(calls, tuple(original_position) == serialized_position,
           'proof.ui.original_native_serialized_equal')
    if profile is not None:
        copied = tuple(before_package.receipt['native_after']['position'])
        try:
            mapped = hwpx_saved_proof.caret_values(before_package, profile)
        except hwpx_saved_proof.ProofUnavailable as exc:
            _refuse('The HWPX serialized caret is not exactly mapped (' + str(exc)[:120] + '); nothing was selected or edited.',
                    'picture_saved_bridge_unproven')
        if copied != tuple(original_position) or mapped != tuple(original_position):
            _refuse('The HWPX serialized caret does not equal the native caret; nothing was selected or edited.',
                    'picture_saved_bridge_unproven')
        if not hwpx_saved_proof.caret_in_domain(profile, tuple(original_position)):
            _refuse('The caret list/paragraph is outside the natively observed caret serialization domain; nothing was selected or edited.',
                    'picture_saved_proof_unqualified')
    raw_target, native_id = _native_target(com, target, calls)
    from app import picture_parameters as pp
    action = pp.action(calls, com)
    _clear_selection(com, action, calls)
    # Independent upfront route. No SelectCtrl/FindCtrl/Reverse retry is permitted.
    anchor = calls.call('target.GetAnchorPos(0)', lambda: raw_target.GetAnchorPos(0), 'object')
    coordinates = []
    for key in ('List', 'Para', 'Pos'):
        coordinates.append(calls.call('anchor.Item.' + key, lambda k=key: anchor.Item(k),
                                      'body_list' if key == 'List' else 'coordinate'))
    current_id = calls.call('target.GetCtrlInstID.after_anchor', lambda: raw_target.GetCtrlInstID(), 'id')
    _proof(calls, current_id == native_id, 'target.GetCtrlInstID.after_anchor')
    if profile is not None and not hwpx_saved_proof.caret_in_domain(profile, tuple(coordinates)):
        # Before any caret move or edit; a pre-existing selection may have been cleared above.
        _refuse('The target anchor list/paragraph is outside the natively observed caret serialization domain; '
                'nothing was edited and the caret was not moved.', 'picture_saved_proof_unqualified')
    binding = {'native_id': native_id, 'anchor': coordinates}
    ui_plan = calls.call('proof.ui.plan',
        lambda: picture_request_proof.plan(before, request, original_position, coordinates), 'object')
    expected_before = ui_plan.expected_model(before)
    header = _head_without_media(expected_before)
    calls.event('proof.ui.plan', 'declared_before_effect', arguments={
        'contract': 'selected_target_anchor', 'original': list(ui_plan.original),
        'expected': list(ui_plan.expected), 'target_id': ui_plan.target_id,
        'request_sha256': ui_plan.request_sha256})
    calls.call('SetPosBySet.target_anchor', lambda: com.SetPosBySet(anchor), 'true', arguments=binding)
    position = calls.call('GetPos.target_anchor', lambda: com.GetPos(), 'position')
    _proof(calls, list(position) == coordinates, 'GetPos.target_anchor')
    calls.call('HAction.Run.SelectCtrlFront', lambda: action.Run('SelectCtrlFront'), 'true',
               arguments={**binding, 'action': 'SelectCtrlFront'})
    _selection(com, native_id, calls)
    name = 'PictureChange' if source is not None else 'ShapeObjDialog'
    params = pp.Parameters(calls, com, source is not None)
    calls.call('HAction.GetDefault.' + name, lambda: action.GetDefault(name, params.hset), 'true',
               arguments={'action': name, 'family': params.family + '.HSet'})
    _selection(com, native_id, calls)
    expected_crop = None
    flow_prestate = None
    if op == 'picture_layout_exact':
        current_flow = params.read(params.root, 'HShapeObject', 'TextFlow')
        def prestate():
            observed = picture_request_proof.flow_prestate(target, request, current_flow)
            if link is not None:
                # The HWPX serialization of the same picture must agree too.
                hwpx_saved_proof.check_flow(before_package, link, observed)
            return observed
        flow_prestate = calls.call('proof.TextFlow.prestate', prestate, 'object')
        calls.event('proof.TextFlow.prestate', 'declared_before_put', arguments={
            'target_id': target['target_id'], 'observed_before': current_flow,
            'serialized_before': flow_prestate.serialized,
            'expected_name': flow_prestate.name,
            'may_materialize': flow_prestate.may_materialize})
    if source is not None:
        settings = {'PicturePath': source['path'], 'PictureEmbed': 1}
    else:
        settings = {}
        if op == 'picture_crop_exact':
            image = params.image()
            sx = params.read(image, 'HDrawImageAttr', 'OriginalSizeX')
            sy = params.read(image, 'HDrawImageAttr', 'OriginalSizeY')
            _proof(calls, sx > 0 and sy > 0 and sx == int(target['source_dimensions']['Width']) and
                   sy == int(target['source_dimensions']['Height']), 'native.crop_source_dimensions')
            expected_crop = {'Left': round(sx * request['left']), 'Top': round(sy * request['top']),
                             'Right': round(sx * request['right']), 'Bottom': round(sy * request['bottom'])}
            if expected_crop['Left'] >= expected_crop['Right'] or expected_crop['Top'] >= expected_crop['Bottom']:
                raise nn.NativeNumberingReadFailure('Native crop has no visible extent.', code='picture_state_unproven')
            skips = {'SkipLeft': expected_crop['Left'], 'SkipTop': expected_crop['Top'],
                     'SkipRight': sx - expected_crop['Right'], 'SkipBottom': sy - expected_crop['Bottom']}
            for key, value in skips.items():
                params.put(image, 'HDrawImageAttr', key, value)
        else:
            settings.update(TreatAsChar=1 if request['mode'] == 'inline' else 0)
            if request['mode'] == 'square':
                settings['TextWrap'] = 0
            for public, native in (('horizontal_offset_mm', 'HorzOffset'), ('vertical_offset_mm', 'VertOffset')):
                if public in request:
                    settings[native] = round(request[public] * 7200 / 25.4)
    for key, value in settings.items():
        params.put(params.root, params.family, key, value)
    position = calls.call('GetPos.preExecute', lambda: com.GetPos(), 'position')
    _proof(calls, tuple(position) == ui_plan.expected, 'GetPos.preExecute')
    if flow_prestate is not None:
        current_flow = params.read(params.root, 'HShapeObject', 'TextFlow')
        _proof(calls, current_flow == flow_prestate.observed, 'proof.TextFlow.preExecute')
    _selection(com, native_id, calls)
    if mark_mutating is not None:
        mark_mutating()
    calls.issued = True
    try:
        calls.call('HAction.Execute.' + name, lambda: action.Execute(name, params.hset), 'true',
                   arguments={'action': name, 'family': params.family + '.HSet'})
        post = calls.call('after.GetTextFile.HWPML2X', lambda: hwp.get_text_file('HWPML2X', ''), 'str')
        after = parse_pictures(post)
        ui_result = ui_plan.verify(after)
        matches = [p for p in after['pictures'] if p['target_id'] == target['target_id']]
        if len(matches) != 1:
            raise ValueError('target identity renewal is not qualified')
        final = matches[0]
        if _outside(after, final) != outside or _head_without_media(after) != header:
            raise ValueError('unrelated body, styles, definitions or controls changed')
        if final['frame'] != target['frame'] or final['caption'] != target['caption']:
            raise ValueError('frame or caption changed')
        if source is not None:
            if final['placement'] != target['placement'] or final['shape'] != target['shape']:
                raise ValueError('replacement changed placement or shape identity')
            if _pixels(after, final)[0] != source['pixels']:
                raise ValueError('replacement decoded pixels differ')
        elif final['media'] != target['media']:
            raise ValueError('crop/layout changed the source payload or reference')
        elif expected_crop is not None:
            if any(int(final['crop'][k]) != v for k, v in expected_crop.items()):
                raise ValueError('persisted crop differs')
            if final['placement'] != target['placement'] or final['shape'] != target['shape']:
                raise ValueError('crop changed placement')
        else:
            if final['crop'] != target['crop'] or final['component'] != target['component'] or final['image_rect'] != target['image_rect']:
                raise ValueError('layout changed image geometry')
            if final['placement']['TreatAsChar'] != ('true' if request['mode'] == 'inline' else 'false'):
                raise ValueError('persisted inline placement differs')
            if request['mode'] == 'square' and final['shape'].get('TextWrap') != 'Square':
                raise ValueError('persisted square wrap differs')
            for public, native in (('horizontal_offset_mm', 'HorzOffset'), ('vertical_offset_mm', 'VertOffset')):
                if public in request and int(final['placement'][native]) != settings[native]:
                    raise ValueError('persisted placement offset differs')
        # Complete target and request-bound media allocation, not a collection
        # of selected attribute projections. This performs no native calls.
        picture_preservation.prove(expected_before, after, target, final, request, expected_crop,
                                   flow_prestate=flow_prestate, legacy_media=profile is None)
        saved = None
        if profile is not None:
            after_package = calls.call('proof.hwpx.after', lambda: saved_proof.copy('after', profile), 'object')
            if {k: after_package.epoch.get(k) for k in ('session_id', 'sequence', 'command_id', 'nonce')} != \
                    {k: before_package.epoch.get(k) for k in ('session_id', 'sequence', 'command_id', 'nonce')}:
                raise ValueError('private copies belong to different operation epochs')
            expectation = hwpx_saved_proof.expect(
                before_package, after_package, link, request, settings=settings, expected_crop=expected_crop,
                flow_prestate=flow_prestate, ui_plan=ui_plan, profile=profile, source=source, reflow=reflow)
            counts = hwpx_saved_proof.compare(before_package, after_package, expectation, profile)
            saved = hwpx_saved_proof.public_proof(profile, before_package, after_package, link, counts, expectation,
                                                  hwpx_saved_proof.edit_evidence(op, profile, counts, reflow=expectation.reflow))
            history = profile.history(op)
            undo = ({'hwpx_undo': 'profile_declared', 'native_editing_actions': 1,
                     'undo_native_steps': history['undo_native_steps'], 'redo_native_steps': history['redo_native_steps'],
                     'state_kind': 'hwpx_package', 'profile_sha256': profile.sha256,
                     'pre_state_sha256': hwpx_saved_proof.state_digest(before_package, profile),
                     'post_state_sha256': hwpx_saved_proof.state_digest(after_package, profile)}
                    if history is not None else None)
            # Only now may save/history treat this exact package as the current state.
            calls.call('proof.hwpx.record_verified', lambda: saved_proof.record(after_package, profile, 'verified_edit'), 'none')
    except BaseException as exc:
        calls.failed = True
        raise nn.NativeNumberingMutationError('Picture edit was entered; final preservation is unproved.') from exc
    result = {'ok': True, 'applied': True, 'changed': True, 'target_id': final['target_id'], 'hash': final['hash'],
              'native_GetCtrlInstID': native_id, 'native_action': name, 'native_actions_issued': 1,
              'history_qualified': False, 'history_reason': 'native_undo_redo_not_qualified',
              'undo_native_actions': None, 'frame': final['frame'], 'crop': final['crop'],
              'placement': final['placement'], 'media': final['media'], 'native_call_witnesses': calls.events,
              'ui_position': ui_result,
              'effective_text_flow': flow_prestate.report(final) if flow_prestate is not None else None,
              'preservation': {'unrelated_body_styles_controls': True, 'other_picture_payloads_references': True,
                               'frame': True, 'caption': True, 'complete_target_allowed_delta': True,
                               'head_tail_media': True},
              'preservation_strategy': strategy,
              'qualification': 'implementation; final native MCP edit/history/save/reopen qualification still required'}
    if saved is not None:
        # HWPML HEAD/TAIL media were not the authority; the complete package was.
        result['preservation'] = {**result['preservation'], 'head_tail_media': False, 'complete_hwpx_package': True}
        result['saved_proof'] = saved
        if undo is not None:
            result.update(undo=undo, history_reason='profile_declared_history',
                          history_qualified=profile.qualified, undo_native_actions=undo['undo_native_steps'])
    return result
