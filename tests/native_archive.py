"""Lossless rewrite of an actual native HWPX archive for replays and test-local adversaries."""
from __future__ import annotations

import zipfile
from pathlib import Path


def write(path: Path, entries) -> None:
    """Rewrite an archive keeping every container field the product reads, flag_bits included.

    zipfile always writes flag_bits 0; the native archives carry the deflate
    option bits (0x06), so they are restored in both the local and the central
    header. Any other differing flag bit is an error, never silently rewritten.
    """
    entries = list(entries)
    with zipfile.ZipFile(path, 'x') as archive:
        for info, data in entries:
            copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            for key in ('compress_type', 'external_attr', 'create_system', 'create_version', 'extract_version', 'extra', 'comment', 'internal_attr'):
                setattr(copy, key, getattr(info, key))
            archive.writestr(copy, data)
    wanted = {info.filename: info.flag_bits for info, _data in entries}
    raw = bytearray(path.read_bytes())

    def patch(offset: int, name: str) -> None:
        written = int.from_bytes(raw[offset:offset + 2], 'little')
        if written & ~0x06 != wanted[name] & ~0x06:
            raise AssertionError('rewritten archive flag bits differ beyond the deflate option: ' + name)
        raw[offset:offset + 2] = ((written & ~0x06) | (wanted[name] & 0x06)).to_bytes(2, 'little')
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        for info in infos:
            patch(info.header_offset + 6, info.filename)
    end = raw.rfind(b'PK\x05\x06')
    count, start = int.from_bytes(raw[end + 10:end + 12], 'little'), int.from_bytes(raw[end + 16:end + 20], 'little')
    cursor = start
    for _ in range(count):
        if raw[cursor:cursor + 4] != b'PK\x01\x02':
            raise AssertionError('central directory record expected')
        n, m, k = (int.from_bytes(raw[cursor + o:cursor + o + 2], 'little') for o in (28, 30, 32))
        patch(cursor + 8, raw[cursor + 46:cursor + 46 + n].decode('utf-8'))
        cursor += 46 + n + m + k
    path.write_bytes(bytes(raw))
