"""Replay of the ACTUAL native h01 public square layout through the real product caller.

What is real: the before/after private HWPX copies (every member byte, member
order and ZIP container field), the HWPML2X producer strings returned before
and after Execute, the typed TextFlow/TreatAsChar/TextWrap values, the original
caret, the target anchor and the IsModified transitions (tests/fixtures/native_h01).

What is SYNTHETIC and labelled so: (1) re-dating content.hpf ModifiedDate to the
replay's own write time, because the product binds that value to the measured
window of the call that wrote it; (2) the public Save(False) and any copy taken
after it, which no native run produced for a picture edit (the replay writes the
actual after package). This is not native evidence of a saved picture edit.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import io
import json
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import hwpx_saved_proof as proof
from tests import hwpx_package_fakes as fakes
from tests import native_archive
from tests import test_picture_mutation_contract as sdk

FIXTURE = Path(__file__).parent / 'fixtures' / 'native_h01'
MANIFEST = json.loads((FIXTURE / 'MANIFEST.json').read_text(encoding='utf-8'))
H01 = MANIFEST['h01']
TEST_PROFILE = Path(__file__).parent.parent / 'docs' / 'profiles' / 'hwpx-saved-proof-h01-reflow-test.json'
G01_PROFILE = FIXTURE / 'g01-derived-method-profile.json'


def producer_string(name: str) -> str:
    witness = H01['producer_strings'][name]
    return proof.producer_text((FIXTURE / name).read_bytes(), chars=witness['chars'], sha256=witness['sha256'])


def package_bytes(name: str) -> bytes:
    """The exact native archive bytes, decoded from the lossless envelope and SHA-verified."""
    envelope = json.loads((FIXTURE / name).read_text(encoding='utf-8'))
    data = base64.b64decode(envelope['base64'], validate=True)
    if hashlib.sha256(data).hexdigest() != envelope['sha256'] or envelope['sha256'] != MANIFEST['files'][name]['decodes_to_sha256']:
        raise AssertionError('native package envelope does not decode to its recorded digest: ' + name)
    return data


_MATERIALIZED: dict[str, Path] = {}


def package_path(name: str) -> Path:
    if name not in _MATERIALIZED:
        folder = Path(tempfile.mkdtemp(prefix='native-h01-'))
        atexit.register(shutil.rmtree, folder, True)
        path = folder / name.replace('.package.json', '.hwpx')
        path.write_bytes(package_bytes(name))
        _MATERIALIZED[name] = path
    return _MATERIALIZED[name]


def window(name: str) -> tuple[datetime, datetime]:
    start, end = H01['copies'][name]['write_window_utc']
    return datetime.fromisoformat(start), datetime.fromisoformat(end)


def state(name: str) -> proof.PackageState:
    """The actual package with its actual native write window; nothing rewritten."""
    return proof.read_state(package_path(name), window=window(name))


def members(name: str) -> list[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(io.BytesIO(package_bytes(name))) as archive:
        return [(info, archive.read(info)) for info in archive.infolist()]


write = native_archive.write



def redate(data: bytes, moment: datetime) -> bytes:
    stamp = moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ').encode()
    new, count = re.subn(rb'(<opf:meta name="ModifiedDate" content="text">)[^<]*(</opf:meta>)', rb'\g<1>' + stamp + rb'\g<2>', data)
    if count != 1:
        raise AssertionError('real content.hpf ModifiedDate is not unique')
    return new


def section_edit(old: bytes, new: bytes, member: str = 'Contents/section0.xml'):
    """A test-local adversary on the actual bytes; the anchor must exist exactly once."""
    def edit(entries):
        out = []
        for info, data in entries:
            if info.filename == member:
                if data.count(old) != 1:
                    raise AssertionError('damage anchor is not unique in the real member ' + member)
                data = data.replace(old, new, 1)
            out.append((info, data))
        return out
    return edit


def request() -> dict:
    return dict(H01['validated_request'])


class _AnchoredCtrl(sdk.Ctrl):
    def GetAnchorPos(self, mode):
        if type(mode) is not int or mode != 0:
            raise TypeError('documented anchor mode')
        anchor = sdk.Anchor(0)
        anchor.values = dict(zip(('List', 'Para', 'Pos'), H01['anchor']))
        return anchor


class NativeH01ReplayHwp(fakes.PackagePictureHwp):
    """Existing synthetic typed SDK, rooted on the ACTUAL h01 HWPML string, returning the actual responses."""

    def __init__(self):
        super().__init__(export_clears=True)
        self.root = ET.fromstring(producer_string('parse-002.hwpml2x'))
        self.native_position = tuple(H01['copies']['before.package.json']['native_before']['position'])
        self.modified = self.initial_modified = H01['copies']['before.package.json']['native_before']['modified']
        self.after_entries_damage = None   # adversary on the copy written after Execute
        self.saved_entries_damage = None   # adversary on the public Save(False) output
        self.later_copy_damage = None      # adversary on any copy written after the after copy
        self.history_copy_damage = None    # adversary on a copy written after Undo
        self.copies_written = 0
        self.relink()

    def relink(self):
        super().relink()
        anchored = [_AnchoredCtrl(self, c.ident) for c in self.ctrls]
        # Keep the native control chain order; only the picture anchors replay the recorded value.
        chain, node = [], self.HeadCtrl
        while node is not None:
            chain.append(node)
            node = node.Next
        replaced = [anchored[self.ctrls.index(c)] if c in self.ctrls else c for c in chain]
        for a, b in zip(replaced, replaced[1:]):
            a.Next = b
        if replaced:
            replaced[-1].Next = None
        self.ctrls, self.HeadCtrl = anchored, (replaced[0] if replaced else None)

    def Run(self, name):
        if name == 'SelectCtrlFront':
            self.events.append('Run:' + name)
            self.selected = next(c for c in self.ctrls if self.GetPos() == tuple(H01['anchor']))
            return True
        return super().Run(name)

    def get_text_file(self, *args):
        super().get_text_file(*args)  # keeps the existing event and latch accounting
        return producer_string('parse-003.hwpml2x' if self.executed and not self._undone() else 'parse-002.hwpml2x')

    def _undone(self):
        return bool(self.redo_snapshots) and not self.undo_snapshots

    def _package(self, moment: datetime):
        name = 'after.package.json' if self.executed and not self._undone() else 'before.package.json'
        entries = [(info, redate(data, moment) if info.filename == 'Contents/content.hpf' else data) for info, data in members(name)]
        return name, entries

    def SaveAs(self, path, fmt, arg=None):
        self.events.append(('SaveAs', fmt, arg))
        if (fmt, arg) != ('HWPX', 'lock:false;export'):
            raise TypeError('unexpected SaveAs variant')
        name, entries = self._package(self.clock())
        self.copies_written += 1
        damage = None
        if self._undone() and self.history_copy_damage is not None:
            damage = self.history_copy_damage
        elif name == 'after.package.json':
            damage = self.after_entries_damage if self.copies_written == 2 else self.later_copy_damage
        write(Path(path), damage(entries) if damage else entries)
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
        _name, entries = self._package(self.clock())
        write(target, self.saved_entries_damage(entries) if self.saved_entries_damage else entries)
        self.modified = False
        return self.save_result


def write_input(path: Path) -> None:
    """The working copy on disk: the actual before package bytes, unchanged."""
    path.write_bytes(package_bytes('before.package.json'))
