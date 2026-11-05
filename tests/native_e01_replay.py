"""Replay of the ACTUAL native e01 capture through the real product caller.

What is real: the HWPML2X producer string (tests/fixtures/native_e01/*.before.hwpml2x,
decoded by the qualified producer decoder) and every HWPX member byte, member
order and ZIP container field of the native copies, plus the observed native
IsModified transitions and caret.

What is SYNTHETIC and labelled so: (1) re-dating content.hpf ModifiedDate to
the replay's own write time, because the product binds that value to the
measured window of the call that wrote it; (2) the picture-edit after-state,
which no native run produced: the requested hp:pos/hp:pic attributes, the
serialized caret at the replayed anchor and a re-rendered preview. Every other
byte is copied from the real package. This is not native evidence of an edit.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import io
import shutil
import tempfile
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import hwpx_saved_proof as proof
from tests import hwpx_package_fakes as fakes
from tests import native_archive
from tests import test_picture_mutation_contract as sdk

FIXTURE = Path(__file__).parent / 'fixtures' / 'native_e01'
MANIFEST = json.loads((FIXTURE / 'MANIFEST.json').read_text(encoding='utf-8'))
EPOCHS = {row['epoch']: row for row in MANIFEST['epochs']}
CANDIDATE = Path(__file__).parent.parent / 'docs' / 'profiles' / 'hwpx-saved-proof-e01-candidate.json'


def producer_string(epoch: str) -> str:
    row = EPOCHS[epoch]
    return proof.producer_text((FIXTURE / row['producer_string']).read_bytes(), **row['producer_witness'])


def window(epoch: str) -> tuple[datetime, datetime]:
    start, end = EPOCHS[epoch]['write_window_utc']
    return datetime.fromisoformat(start), datetime.fromisoformat(end)


def package_bytes(epoch: str) -> bytes:
    """The exact native archive bytes, decoded from the lossless envelope and SHA-verified."""
    envelope = json.loads((FIXTURE / EPOCHS[epoch]['package']).read_text(encoding='utf-8'))
    data = base64.b64decode(envelope['base64'], validate=True)
    if hashlib.sha256(data).hexdigest() != envelope['sha256'] or envelope['sha256'] != MANIFEST['files'][EPOCHS[epoch]['package']]['decodes_to_sha256']:
        raise AssertionError('native package envelope does not decode to its recorded digest: ' + epoch)
    return data


_MATERIALIZED: dict[str, Path] = {}


def package_path(epoch: str) -> Path:
    """The exact native archive as a file (the product reader takes paths); temporary, per process."""
    if epoch not in _MATERIALIZED:
        folder = Path(tempfile.mkdtemp(prefix='native-e01-'))
        atexit.register(shutil.rmtree, folder, True)
        path = folder / (epoch + '.hwpx')
        path.write_bytes(package_bytes(epoch))
        _MATERIALIZED[epoch] = path
    return _MATERIALIZED[epoch]


def real_members(epoch: str) -> list[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(io.BytesIO(package_bytes(epoch))) as archive:
        return [(info, archive.read(info)) for info in archive.infolist()]


def _redate(data: bytes, moment: datetime) -> bytes:
    stamp = moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ').encode()
    new, count = re.subn(rb'(<opf:meta name="ModifiedDate" content="text">)[^<]*(</opf:meta>)', rb'\g<1>' + stamp + rb'\g<2>', data)
    if count != 1:
        raise AssertionError('real content.hpf ModifiedDate is not unique')
    return new


def _preview(seed: int) -> bytes:
    from PIL import Image
    data = io.BytesIO()
    Image.new('RGB', (724, 1024), (seed % 256, 200, 200)).save(data, format='PNG')
    return data.getvalue()


# Keeps every container field the product reads, flag_bits included (tests/native_archive.py).
write = native_archive.write


def edited(entries, hwpml_root: ET.Element, moment: datetime, *, rendering_seed: int = 1):
    """SYNTHETIC after-state: requested picture attributes, caret and preview only."""
    shape = sdk.first(hwpml_root, 'shapeobject')
    pos = sdk.first(shape, 'position')
    caret = sdk.first(hwpml_root, 'caretpos')
    out = []
    for info, data in entries:
        if info.filename == 'Contents/section0.xml':
            text = data.decode('utf-8')
            start, end = text.index('<hp:pic '), text.index('</hp:pic>')
            pic = text[start:end]
            pic = pic.replace('treatAsChar="1"', 'treatAsChar="%s"' % ('1' if pos.get('TreatAsChar') == 'true' else '0'), 1)
            wrap = {'TopAndBottom': 'TOP_AND_BOTTOM', 'Square': 'SQUARE'}[shape.get('TextWrap')]
            pic = re.sub(r'textWrap="[A-Z_]+"', 'textWrap="%s"' % wrap, pic, count=1)
            for native, attr in (('HorzOffset', 'horzOffset'), ('VertOffset', 'vertOffset')):
                pic = re.sub(r'(<hp:pos [^>]*?)%s="-?[0-9]+"' % attr, r'\g<1>%s="%s"' % (attr, pos.get(native)), pic, count=1)
            data = (text[:start] + pic + text[end:]).encode('utf-8')
        elif info.filename == 'settings.xml':
            data = re.sub(rb'listIDRef="[0-9]+" paraIDRef="[0-9]+" pos="[0-9]+"',
                          b'listIDRef="%s" paraIDRef="%s" pos="%s"' % tuple(caret.get(k).encode() for k in ('List', 'Para', 'Pos')), data)
        elif info.filename == 'Preview/PrvImage.png':
            data = _preview(rendering_seed)
        elif info.filename == 'Contents/content.hpf':
            data = _redate(data, moment)
        out.append((info, data))
    return out


class NativeReplayHwp(fakes.PackagePictureHwp):
    """Existing synthetic typed SDK, rooted on the ACTUAL e01 HWPML string and HWPX bytes."""

    def __init__(self, *, epoch: str = 'clean', package_epoch: str | None = None):
        super().__init__(export_clears=True)
        self.root = ET.fromstring(producer_string(epoch))
        self.package_epoch = package_epoch or epoch
        observed = EPOCHS[epoch]
        self.native_position = tuple(observed['caret_before_after'][0])
        self.modified = self.initial_modified = observed['native_modified'][0]
        self.after_entries_damage = None
        self.saved_entries_damage = None
        self.relink()

    def _entries(self, moment: datetime, damage=None):
        entries = [(info, _redate(data, moment) if info.filename == 'Contents/content.hpf' else data)
                   for info, data in real_members(self.package_epoch)]
        if self.executed:
            entries = edited(entries, self.root, moment)
        return damage(entries) if damage is not None else entries

    def SaveAs(self, path, fmt, arg):
        self.events.append(('SaveAs', fmt, arg))
        if (fmt, arg) != ('HWPX', 'lock:false;export'):
            raise TypeError('unexpected SaveAs variant')
        write(Path(path), self._entries(self.clock(), self.after_entries_damage if self.executed else None))
        if self.export_clears:
            self.modified = False  # observed natively: false->false, true->false
        return self.save_as_result

    def Save(self, save_if_dirty=True):
        self.events.append(('Save', save_if_dirty))
        if save_if_dirty and not self.modified:
            return True
        target = Path(self.path)
        if target.exists():
            target.unlink()
        write(target, self._entries(self.clock(), self.saved_entries_damage))
        self.modified = False
        return self.save_result


def write_input(path: Path, epoch: str = 'clean') -> None:
    """The working copy on disk: the real package bytes, unchanged."""
    path.write_bytes(package_bytes(epoch))
