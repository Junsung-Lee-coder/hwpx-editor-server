"""SYNTHETIC external SaveAs/Save/Undo/Redo for portable tests; never native evidence.

The fake SDK serializes ITS OWN in-memory HWPML state into an OWPML-shaped
HWPX package (KS X 6101 element names), so the real product reader, bridge
and comparator run on complete package bytes. Values, IDs and layout are this
fake's choices, not Hancom output. HWPX object ids are deliberately different
numbers from HWPML InstId values: the two namespaces are never equal here.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from tests import test_picture_mutation_contract as sdk

HP = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
HC = 'http://www.hancom.co.kr/hwpml/2011/core'
HS = 'http://www.hancom.co.kr/hwpml/2011/section'
HH = 'http://www.hancom.co.kr/hwpml/2011/head'
HA = 'http://www.hancom.co.kr/hwpml/2011/app'
OPF = 'http://www.idpf.org/2007/opf/'
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
WRAP = {'TopAndBottom': 'TOP_AND_BOTTOM', 'Square': 'SQUARE', 'BehindText': 'BEHIND_TEXT', 'InFrontOfText': 'IN_FRONT_OF_TEXT'}
FLOW = {'BothSides': 'BOTH_SIDES', 'LeftOnly': 'LEFT_ONLY', 'RightOnly': 'RIGHT_ONLY', 'LargestOnly': 'LARGEST_ONLY'}

EVIDENCE_FIELDS = ('materialization', 'bridge', 'export_native_dirty', 'container_volatile', 'regenerated_members',
                   'volatile_values', 'caret', 'replacement_manifest_attributes', 'public_save', 'history')
PROFILE = {
    'schema': 'hwpx-saved-proof-profile/v2',
    'profile_id': 'synthetic-portable-test/v2',
    'qualification': {'status': 'unqualified', 'basis': 'SYNTHETIC portable test fixture; not native qualification',
                      'evidence_sha256': None},
    'materialization': {'method': 'SaveAs', 'format': 'HWPX', 'args': 'lock:false;export'},
    'bridge': 'owpml_physical_ordinal/v1',
    'export_native_dirty': 'cleared',
    'container_volatile': [],
    'regenerated_members': {'Preview/PrvImage.png': {'kind': 'png', 'when': 'rendering_request', 'same_pixel_size': True}},
    'volatile_values': [{'member': 'Contents/content.hpf', 'element': '{%s}meta' % OPF,
                         'where': {'name': 'ModifiedDate', 'content': 'text'}, 'field': 'text',
                         'value_type': 'utc_second_z', 'bound': 'native_write_window'}],
    'caret': {'member': 'settings.xml', 'element': '{%s}CaretPosition' % HA,
              'fields': {'List': 'listIDRef', 'Para': 'paraIDRef', 'Pos': 'pos'},
              'observed_domain': {'List': [0], 'Para': [0]}},
    'replacement_manifest_attributes': ['id', 'href', 'media-type', 'isEmbeded'],
    'public_save': {'save_false_after_export_clear': True},
    'history': {'picture_layout_exact': {'undo_native_steps': 1, 'redo_native_steps': 1}},
    # SYNTHETIC: nothing in this test profile was observed natively.
    'evidence': {name: {'status': 'not_run', 'basis': 'SYNTHETIC portable test fixture'} for name in EVIDENCE_FIELDS},
}


def profile_env(directory: Path, data: dict | None = None, *, pin: str | None = None) -> dict[str, str]:
    """Write a profile file and return the documented configuration environment."""
    import hashlib
    body = json.dumps(PROFILE if data is None else data, sort_keys=True).encode()
    path = Path(directory) / 'hwpx-profile.json'
    path.write_bytes(body)
    return {'HWP_PICTURE_HWPX_PROFILE': str(path),
            'HWP_PICTURE_HWPX_PROFILE_SHA256': pin or hashlib.sha256(body).hexdigest()}


def _attrs(mapping) -> str:
    return ''.join(f' {k}={quoteattr(str(v))}' for k, v in mapping.items())


def _generic(element, ns='hp') -> str:
    text = escape(element.text or '') if (element.text or '').strip() else ''
    inner = ''.join(_node(child) for child in element)
    return f'<{ns}:x hwpml={quoteattr(element.tag)}{_attrs(element.attrib)}>{text}{inner}</{ns}:x>'


def _child(element, name):
    found = [c for c in element if c.tag == name]
    return found[0] if len(found) == 1 else None


def _pic(picture) -> str:
    shape, comp = _child(picture, 'SHAPEOBJECT'), _child(picture, 'SHAPECOMPONENT')
    size, pos = _child(shape, 'SIZE'), _child(shape, 'POSITION')
    attrs = {'id': str(int(shape.get('InstId')) + 7), 'zOrder': shape.get('ZOrder', '0'),
             'textWrap': WRAP.get(shape.get('TextWrap'), 'X_' + str(shape.get('TextWrap'))),
             'lock': '1' if shape.get('Lock') == 'true' else '0', 'groupLevel': comp.get('GroupLevel', ''),
             'instid': str(int(comp.get('InstID')) + 3)}
    if 'TextFlow' in shape.attrib:
        attrs['textFlow'] = FLOW.get(shape.get('TextFlow'), 'X_' + shape.get('TextFlow'))
    for key, value in shape.attrib.items():
        if key not in ('InstId', 'ZOrder', 'TextWrap', 'Lock', 'TextFlow'):
            attrs['x' + key] = value
    for key, value in picture.attrib.items():
        attrs['p' + key] = value
    sz = {'width': size.get('Width'), 'height': size.get('Height'),
          **{'x' + k: v for k, v in size.attrib.items() if k not in ('Width', 'Height')}}
    ps = {'treatAsChar': '1' if pos.get('TreatAsChar') == 'true' else '0', 'horzOffset': pos.get('HorzOffset'),
          'vertOffset': pos.get('VertOffset'),
          **{'x' + k: v for k, v in pos.attrib.items() if k not in ('TreatAsChar', 'HorzOffset', 'VertOffset')}}
    parts = [f'<hp:sz{_attrs(sz)}/>', f'<hp:pos{_attrs(ps)}/>']
    parts += [_node(c) for c in shape if c.tag not in ('SIZE', 'POSITION')]
    parts += [_node(c) for c in comp]
    for child in picture:
        if child.tag in ('SHAPEOBJECT', 'SHAPECOMPONENT'):
            continue
        if child.tag == 'IMAGECLIP':
            parts.append('<hp:imgClip' + _attrs({'left': child.get('Left'), 'right': child.get('Right'),
                                                 'top': child.get('Top'), 'bottom': child.get('Bottom')}) + '/>')
        elif child.tag == 'IMAGEDIM':
            parts.append('<hp:imgDim' + _attrs({'dimwidth': child.get('Width'), 'dimheight': child.get('Height')}) + '/>')
        elif child.tag == 'IMAGE':
            parts.append('<hc:img' + _attrs({'binaryItemIDRef': 'image' + child.get('BinItem'), 'bright': child.get('Bright'),
                                             'contrast': child.get('Contrast'), 'effect': child.get('Effect'),
                                             'alpha': child.get('Alpha')}) + '/>')
        else:
            parts.append(_node(child))
    return f'<hp:pic{_attrs(attrs)}>' + ''.join(parts) + '</hp:pic>'


# SYNTHETIC opt-in line-layout model (tests/line_cache_fakes.py): while a fake that models
# Hancom's computed line cache serializes, this returns (prefix, suffix) XML for one HWPML P.
_PARAGRAPH_HOOK = None


def _node(element) -> str:
    if element.tag == 'P':
        prefix, suffix = _PARAGRAPH_HOOK(element) if _PARAGRAPH_HOOK is not None else ('', '')
        return ('<hp:p' + _attrs({'paraPrIDRef': element.get('ParaShape', ''), 'styleIDRef': element.get('Style', '')}) + '>'
                + prefix + ''.join(_node(c) for c in element) + suffix + '</hp:p>')
    if element.tag == 'TEXT':
        return '<hp:run' + _attrs({'charPrIDRef': element.get('CharShape', '')}) + '>' + escape(element.text or '') + ''.join(_node(c) for c in element) + '</hp:run>'
    if element.tag == 'CHAR':
        return '<hp:t>' + escape(element.text or '') + ''.join(_node(c) for c in element) + '</hp:t>' + escape(element.tail or '')
    if element.tag == 'PICTURE':
        return _pic(element)
    return _generic(element)


def _png(seed: int) -> bytes:
    from PIL import Image
    data = io.BytesIO()
    Image.new('RGB', (4, 3), (seed % 256, 10, 20)).save(data, format='PNG')
    return data.getvalue()


def _stamp(moment) -> str:
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def members(fake) -> list[tuple[str, bytes]]:
    global _PARAGRAPH_HOOK
    previous, _PARAGRAPH_HOOK = _PARAGRAPH_HOOK, getattr(fake, 'paragraph_lines', None)
    try:
        return _members(fake)
    finally:
        _PARAGRAPH_HOOK = previous


def _members(fake) -> list[tuple[str, bytes]]:
    root = fake.root
    head = next(e for e in root if e.tag == 'HEAD')
    body = next(e for e in root if e.tag == 'BODY')
    tail = next(e for e in root if e.tag == 'TAIL')
    items = [e for e in root.iter() if e.tag == 'BINITEM']
    data = {e.get('Id'): e for e in root.iter() if e.tag == 'BINDATA'}
    caret = next(e for e in root.iter() if e.tag == 'CARETPOS')
    sections = [e for e in body if e.tag == 'SECTION']
    head_copy = copy.deepcopy(head)
    for parent in list(head_copy.iter()):
        for child in list(parent):
            if child.tag in ('CARETPOS', 'BINDATALIST'):
                parent.remove(child)
    out = [('mimetype', b'application/hwp+zip'),
           ('version.xml', (DECL + f'<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" major="5" minor="1"/>').encode())]
    images = ''.join(f'<opf:item id="image{i}" href="BinData/image{i}.png" media-type="image/png" isEmbeded="1"/>'
                     for i in range(1, len(items) + 1))
    sections_items = ''.join(f'<opf:item id="section{i}" href="Contents/section{i}.xml" media-type="application/xml"/>'
                             for i in range(len(sections)))
    spine = ''.join(f'<opf:itemref idref="section{i}" linear="yes"/>' for i in range(len(sections)))
    hpf = (DECL + f'<opf:package xmlns:opf="{OPF}" version="" unique-identifier="" id=""><opf:metadata><opf:title/>'
           f'<opf:meta name="CreatedDate" content="text">2026-11-05T00:00:00Z</opf:meta>'
           f'<opf:meta name="ModifiedDate" content="text">{_stamp(fake.clock())}</opf:meta></opf:metadata>'
           f'<opf:manifest><opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>{images}{sections_items}'
           f'<opf:item id="settings" href="settings.xml" media-type="application/xml"/></opf:manifest>'
           f'<opf:spine><opf:itemref idref="header" linear="yes"/>{spine}</opf:spine></opf:package>')
    out.append(('Contents/content.hpf', hpf.encode()))
    out.append(('Contents/header.xml', (DECL + f'<hh:head xmlns:hh="{HH}" xmlns:hp="{HP}" version="1.31">'
                                        + ''.join(_node(c) for c in head_copy) + '</hh:head>').encode()))
    for index, section in enumerate(sections):
        xml = (DECL + f'<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}" xmlns:hc="{HC}"><!-- retained comment -->'
               + ''.join(_node(c) for c in section) + '</hs:sec>')
        out.append((f'Contents/section{index}.xml', xml.encode()))
    for index, item in enumerate(items, start=1):
        payload = data[item.get('BinData')].text
        out.append((f'BinData/image{index}.png', base64.b64decode(''.join((payload or '').split()))))
    out.append(('settings.xml', (DECL + f'<ha:HWPApplicationSetting xmlns:ha="{HA}"><ha:CaretPosition'
                                 + _attrs({'listIDRef': caret.get('List'), 'paraIDRef': caret.get('Para'), 'pos': caret.get('Pos')})
                                 + '/></ha:HWPApplicationSetting>').encode()))
    out.append(('Scripts/headerScripts', ''.join(_node(c) for c in tail if c.tag == 'SCRIPTCODE').encode() or b'//'))
    out.append(('Custom/unknown.bin', b'\x00unknown-member\xff'))
    out.append(('Custom/empty/', b''))
    # As observed natively: the preview is a function of content, identical for identical content.
    body_digest = int(hashlib.sha256(b''.join(d for n, d in out if n.startswith('Contents/section'))).hexdigest()[:6], 16)
    out.append(('Preview/PrvImage.png', _png(body_digest)))
    out.append(('Preview/PrvText.txt', 'Picture editing report 2025'.encode('utf-8')))
    out.append(('META-INF/container.xml', (DECL + '<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container"><ocf:rootfiles><ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/></ocf:rootfiles></ocf:container>').encode()))
    out.append(('META-INF/manifest.xml', (DECL + '<odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>').encode()))
    return out


def write_package(fake, path: Path, *, damage=None) -> None:
    fake.stamp += 1
    entries = members(fake)
    if damage is not None:
        entries = damage(entries)
    stamp = (2026, 11, 5, 10, 0, 0) if not fake.vary_time else (2026, 11, 5, 10, min(59, fake.stamp), 0)
    with zipfile.ZipFile(path, 'x') as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_STORED if name in ('mimetype', 'Custom/empty/') else zipfile.ZIP_DEFLATED
            if name.endswith('/'):
                info.external_attr = 0o40755 << 16 | 0x10
            archive.writestr(info, data)


class PackagePictureHwp(sdk.PictureHwp):
    """The existing synthetic picture SDK plus SaveAs/Save/IsModified/Path/Undo/Redo."""

    def __init__(self, *, export_clears=True, vary_time=False, shared=False, size_drift=False):
        super().__init__(shared=shared)
        self.path = None
        self.modified = False
        self.export_clears = export_clears
        self.vary_time = vary_time
        self.size_drift = size_drift
        self.stamp = 0
        self.clock = lambda: datetime.now(timezone.utc)  # ModifiedDate source; tests may script it
        self.save_as_result = True
        self.save_result = True
        self.after_execute_package_damage = None
        self.saved_package_damage = None
        self.executed = False
        self.undo_snapshots: list = []
        self.redo_snapshots: list = []
        self.undo_damage = None
        self.undo_result = None  # SYNTHETIC: a non-None value is returned by Undo/Redo with no state change
        self.saveas_hooks = {}  # SYNTHETIC: {call number: fn(fake)} run right after that SaveAs wrote its package
        self.after_failure = []

    # Raw Automation properties/methods read by the product's private-copy protocol.
    @property
    def Path(self):
        self.events.append('Path')
        return self.path

    @property
    def IsModified(self):
        self.events.append('IsModified')
        return self.modified

    @IsModified.setter
    def IsModified(self, value):
        self.modified = value

    def SaveAs(self, path, fmt, arg=None):
        self.events.append(('SaveAs', fmt, arg))
        if (fmt, arg) == ('PDF', None):
            # SYNTHETIC rendered PDF export: an artifact file, no document or flag change.
            Path(path).write_bytes(b'%PDF-1.4\n% SYNTHETIC fake export\n%%EOF\n')
            return True
        if (fmt, arg) != ('HWPX', 'lock:false;export'):
            raise TypeError('unexpected SaveAs variant')
        damage = self.after_execute_package_damage if self.executed else None
        write_package(self, Path(path), damage=damage)
        hook = self.saveas_hooks.get(len([e for e in self.events if isinstance(e, tuple) and e[0] == 'SaveAs']))
        if hook is not None:
            hook(self)
        if self.export_clears:
            self.modified = False
        return self.save_as_result

    def Save(self, save_if_dirty=True):
        self.events.append(('Save', save_if_dirty))
        if save_if_dirty and not self.modified:
            return True  # documented dirty-only form writes nothing
        target = Path(self.path)
        if target.exists():
            target.unlink()
        write_package(self, target, damage=self.saved_package_damage)
        self.modified = False
        return self.save_result

    def save(self, save_if_dirty=True):  # locked pyhwpx 1.7.2 forwarding body shape
        return self.Save(save_if_dirty=save_if_dirty)

    def get_text_file(self, *args):
        text = super().get_text_file(*args)
        if self.size_drift and self.executed:
            # Legacy HWPML BINDATA Size drift with identical payload, as observed natively.
            self.stamp += 1
            text = text.replace('Size="1431"', f'Size="{1431 + self.stamp}"', 1)
        return text

    def Execute(self, name, pset):
        snapshot = copy.deepcopy(self.root)
        result = super().Execute(name, pset)
        self.executed = True
        self.modified = True
        self.undo_snapshots.append(snapshot)
        self.redo_snapshots.clear()
        return result

    def _move(self, delta):
        # SYNTHETIC caret move: native position and the serialized CARETPOS move together.
        caret = next(e for e in self.root.iter() if e.tag == 'CARETPOS')
        self.native_position = (self.native_position[0], self.native_position[1], max(0, self.native_position[2] + delta))
        caret.set('Pos', str(self.native_position[2]))
        self.events.append('Move:%+d' % delta)
        return True

    def MoveRight(self):
        return self._move(1)

    def MoveLeft(self):
        return self._move(-1)

    def Run(self, name):
        if name in ('Undo', 'Redo') and self.undo_result is not None:
            self.events.append('Run:' + name)
            return self.undo_result
        if name in ('Undo', 'Redo'):
            self.events.append('Run:' + name)
            source, target = (self.undo_snapshots, self.redo_snapshots) if name == 'Undo' else (self.redo_snapshots, self.undo_snapshots)
            if not source:
                return False
            target.append(copy.deepcopy(self.root))
            self.root = source.pop()
            if self.undo_damage is not None:
                self.undo_damage(self)
            self.selected = None
            self.relink()
            self.modified = True
            return True
        return super().Run(name)
