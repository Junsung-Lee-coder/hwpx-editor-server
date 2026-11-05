"""Native HWPX private-copy saved-package proof for existing-picture edits.

Strategy ``native_hwpx_package`` is selected BEFORE any native effect, and only
when an explicit profile is configured (``HWP_PICTURE_HWPX_PROFILE`` plus its
exact ``HWP_PICTURE_HWPX_PROFILE_SHA256``). Without one, the unchanged legacy
HWPML strict route runs. A failed legacy proof never falls back to this route.

Authority
- Before/after state: owned private copies written by ``SaveAs(path, "HWPX",
  "lock:false;export")`` on the owning STA inside ONE command epoch. Every
  member (unknown, script, empty and directory members included) and its ZIP
  container metadata are read completely and bound to the copy receipt.
- The before copy is the current in-memory document, including earlier valid
  unsaved edits; it is never the input file or the on-disk working copy.
- Expected after-state = before package + request-derived target deltas from
  the request and positively bound current values (typed native getters and
  the serialized before-state). The only after-state values admitted are the
  bounded replacement allocation (one reused or one new embedded PNG binding)
  and profile-declared volatile values.
- Every other member must be byte-identical with identical container metadata
  and order. Representation allowances (regenerated previews, volatile values,
  serialized caret mapping, container timestamps) come ONLY from profile data.

Limits
- Legacy HWPML BINDATA ``Size`` is neither read nor certified here; it remains
  unknown/unqualified. Raw HWPML is not claimed unchanged.
- Private copies are not a user Save or latest output and never discharge
  logical dirty debt or qualify history by themselves.
- The HWPML (SHAPEOBJECT InstId) and HWPX object identifiers are separate
  namespaces. The target is bridged by complete ordinal census plus physical
  frame/position/crop/dimension/media-payload facts, never by ID equality.
- Profile qualification status is reported literally; source never upgrades it.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

SCHEMA = 'hwpx-saved-proof-profile/v2'
STRATEGY = 'native_hwpx_package'
LEGACY = 'legacy_hwpml_strict'
BRIDGE = 'owpml_physical_ordinal/v1'
MATERIALIZATION = {'method': 'SaveAs', 'format': 'HWPX', 'args': 'lock:false;export'}
EDIT_OPS = ('picture_replace_exact', 'picture_crop_exact', 'picture_layout_exact')
# Hancom ParameterSetTable_2504 ShapeObject TextFlow 0..3 and OWPML (KS X 6101)
# hp:pic@textFlow enumeration. Same observed effective value only; no default.
FLOW = {0: 'BOTH_SIDES', 1: 'LEFT_ONLY', 2: 'RIGHT_ONLY', 3: 'LARGEST_ONLY'}
CONTAINER_VOLATILE = frozenset({'date_time', 'extra', 'create_version', 'extract_version', 'create_system'})
# Every profile field carries its own evidence status. 'observed' means actual
# native data answered it; 'not_run' means it is a declared candidate only.
EVIDENCE_FIELDS = ('materialization', 'bridge', 'export_native_dirty', 'container_volatile', 'regenerated_members',
                   'volatile_values', 'caret', 'replacement_manifest_attributes', 'public_save', 'history')
# A field a profile may omit; when declared it carries its own evidence row too.
OPTIONAL_EVIDENCE_FIELDS = ('line_reflow',)
EVIDENCE_NAMES = EVIDENCE_FIELDS + OPTIONAL_EVIDENCE_FIELDS
# Hancom's computed paragraph line layout: HWP 5.0 revision 1.3, 4.3.4 table 62
# (PARA_LINE_SEG, "cache information used to print each line of the paragraph")
# and OWPML hp:linesegarray/hp:lineseg (hancom-io/hwpx-owpml-model CLineSeg). A
# lineseg carries exactly these attributes; textpos and flags are UINT32, the
# others INT32.
LINESEG_ATTRIBUTES = ('textpos', 'vertpos', 'vertsize', 'textheight', 'baseline', 'spacing', 'horzpos', 'horzsize', 'flags')
LINESEG_UNSIGNED = frozenset({'textpos', 'flags'})
# The line-box metrics a changed picture layout may recompute in the target
# picture's own paragraph. textpos (line break), vertpos (vertical anchor),
# spacing and flags (segment structure) stay anchors, as do the line count and order.
REFLOW_FIELDS = ('baseline', 'horzpos', 'horzsize', 'textheight', 'vertsize')
REFLOW_SCOPE = 'target_paragraph_lines'
REFLOW_OPS = ('picture_layout_exact',)
REFLOW_CONTRACT = 'request_bound_line_reflow/v1'
MAX_REFLOW_LINES = 10000
_HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
_HS = '{http://www.hancom.co.kr/hwpml/2011/section}'
_DECIMAL = re.compile(r'(?:0|-?[1-9][0-9]{0,9})\Z')
# Typed volatile values: an exact UTC second timestamp ("2026-11-05T11:35:49Z")
# bound to the measured window of the native call that wrote that package.
VALUE_TYPES = {'utc_second_z': re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z\Z')}
# The only members a profile may declare as regenerated representation: the two
# fixed OWPML preview members, each with its own kind. No other member (scripts,
# settings, caret, META-INF, master pages, mimetype, version, body, style, media)
# can be waived, whatever the profile says.
PREVIEW_MEMBERS = {'Preview/PrvImage.png': 'png', 'Preview/PrvText.txt': 'text'}
# A regenerated text preview names its exact encoding; it is decoded strictly in it.
TEXT_ENCODINGS = ('utf-8', 'utf-16-le', 'utf-16-be')
MAX_DELTAS = 24
_INT = re.compile(r'(?:0|-?[1-9][0-9]{0,11})\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9._/-]{1,80}\Z')
_QNAME = re.compile(r'\{[^{}\s]{1,200}\}[A-Za-z_][A-Za-z0-9._-]{0,80}\Z')
_ATTR = re.compile(r'[A-Za-z_][A-Za-z0-9._-]{0,80}\Z')
_SECTION = re.compile(r'Contents/section(0|[1-9][0-9]{0,3})\.xml\Z')


class ProofUnavailable(ValueError):
    """Configuration/profile cannot support the strategy; raised before any effect."""


class PackageMismatch(ValueError):
    """A complete package comparison failed; carries bounded named deltas."""

    def __init__(self, message: str, deltas: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.deltas = list(deltas or [])[:MAX_DELTAS]


class BridgeUnproven(ValueError):
    """The HWPML target cannot be positively bridged to one HWPX picture."""


class XmlUnproved(ValueError):
    """A member parsed as XML declares a DOCTYPE/entity; it is never compared or digested."""


# --------------------------------------------------------------------------- profile


@dataclass(frozen=True)
class Profile:
    data: Mapping[str, Any]
    sha256: str
    source: str

    @property
    def qualified(self) -> bool:
        return self.data['qualification']['status'] == 'qualified'

    def evidence(self, field_name: str) -> str:
        return self.data['evidence'][field_name]['status']

    def native_qualified(self, used: tuple[str, ...]) -> bool:
        """Only a qualified profile whose every used field was observed natively."""
        return self.qualified and all(self.evidence(name) == 'observed' for name in used)

    def not_observed(self, used: tuple[str, ...]) -> list[str]:
        return [name for name in used if self.evidence(name) != 'observed']

    def identity(self) -> dict[str, Any]:
        q = self.data['qualification']
        return {'profile_id': self.data['profile_id'], 'profile_sha256': self.sha256,
                'qualification': q['status'], 'qualification_basis': q['basis'],
                'evidence_sha256': q['evidence_sha256'], 'source': self.source}

    def history(self, op: str) -> dict[str, int] | None:
        value = self.data['history'].get(op)
        return dict(value) if value is not None else None

    def reflow(self, op: str) -> tuple[str, ...] | None:
        """The declared recomputable line-box fields for this operation, or None (strict)."""
        rule = (self.data.get('line_reflow') or {}).get(op)
        return tuple(rule['fields']) if rule is not None else None


def _member_name(value: Any) -> str:
    if type(value) is not str or not 1 <= len(value) <= 200 or value.startswith('/') or '\\' in value:
        raise ProofUnavailable('profile member name is not canonical')
    if any(part in ('', '.', '..') for part in value.split('/')):
        raise ProofUnavailable('profile member name is not canonical')
    return value


def _exact_keys(raw: Any, keys: set[str], what: str) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != keys:
        raise ProofUnavailable(f'profile {what} must have exactly {sorted(keys)}')
    return raw


def _int_domain(raw: Any) -> list[int]:
    if type(raw) is not list or not 1 <= len(raw) <= 64 or len(set(raw)) != len(raw) or any(type(n) is not int or not 0 <= n < 2**31 for n in raw):
        raise ProofUnavailable('caret observed_domain lists exact non-negative integers')
    return raw


def validate_profile(raw: Any) -> dict[str, Any]:
    """Exact schema; any unknown or malformed field refuses the strategy."""
    keys = {'schema', 'profile_id', 'qualification', 'materialization', 'bridge', 'export_native_dirty',
            'container_volatile', 'regenerated_members', 'volatile_values', 'caret', 'replacement_manifest_attributes',
            'public_save', 'history', 'evidence'}
    optional = {name for name in OPTIONAL_EVIDENCE_FIELDS if type(raw) is dict and name in raw}
    data = _exact_keys(raw, keys | optional, 'document')
    if data['schema'] != SCHEMA:
        raise ProofUnavailable('profile schema is not ' + SCHEMA)
    if type(data['profile_id']) is not str or not _ID.fullmatch(data['profile_id']):
        raise ProofUnavailable('profile_id is not bounded')
    q = _exact_keys(data['qualification'], {'status', 'basis', 'evidence_sha256'}, 'qualification')
    if q['status'] not in ('qualified', 'unqualified') or type(q['basis']) is not str or not 1 <= len(q['basis']) <= 600:
        raise ProofUnavailable('profile qualification is malformed')
    if q['evidence_sha256'] is not None and (type(q['evidence_sha256']) is not str or not _HEX64.fullmatch(q['evidence_sha256'])):
        raise ProofUnavailable('profile evidence_sha256 is malformed')
    if q['status'] == 'qualified' and q['evidence_sha256'] is None:
        raise ProofUnavailable('a qualified profile names its evidence digest')
    if data['materialization'] != MATERIALIZATION or type(data['materialization']) is not dict:
        raise ProofUnavailable('profile materialization must be the selected SaveAs HWPX export candidate')
    if data['bridge'] != BRIDGE:
        raise ProofUnavailable('profile bridge must be ' + BRIDGE)
    if data['export_native_dirty'] not in ('unchanged', 'cleared'):
        raise ProofUnavailable('export_native_dirty must be unchanged or cleared')
    volatile = data['container_volatile']
    if type(volatile) is not list or len(set(volatile)) != len(volatile) or not set(volatile) <= CONTAINER_VOLATILE:
        raise ProofUnavailable('container_volatile names unknown container fields')
    regenerated = data['regenerated_members']
    if type(regenerated) is not dict:
        raise ProofUnavailable('regenerated_members is malformed')
    for name, rule in regenerated.items():
        _member_name(name)
        kind = PREVIEW_MEMBERS.get(name)
        if kind is None:
            raise ProofUnavailable('regenerated_members may name only ' + ', '.join(sorted(PREVIEW_MEMBERS)))
        keys = {'kind', 'when', 'same_pixel_size'} | ({'encoding'} if kind == 'text' else set())
        rule = _exact_keys(rule, keys, 'regenerated member ' + name)
        if rule['kind'] != kind or rule['when'] != 'rendering_request' or type(rule['same_pixel_size']) is not bool:
            raise ProofUnavailable(f'regenerated {name} is kind {kind}, when rendering_request, same_pixel_size Boolean')
        if kind == 'text' and (rule['same_pixel_size'] or rule['encoding'] not in TEXT_ENCODINGS):
            raise ProofUnavailable('a regenerated text preview names one of ' + ', '.join(TEXT_ENCODINGS) + ' and no pixel size')
    values = data['volatile_values']
    if type(values) is not list or len(values) > 16:
        raise ProofUnavailable('volatile_values is malformed')
    for item in values:
        item = _exact_keys(item, {'member', 'element', 'where', 'field', 'value_type', 'bound'}, 'volatile value')
        _member_name(item['member'])
        if _SECTION.fullmatch(item['member']) or item['member'].startswith('BinData/'):
            raise ProofUnavailable('document body/media cannot hold volatile values')
        if type(item['element']) is not str or not _QNAME.fullmatch(item['element']):
            raise ProofUnavailable('volatile element must be an expanded {uri}name')
        where = item['where']
        if type(where) is not dict or not 1 <= len(where) <= 3 or any(type(k) is not str or not _ATTR.fullmatch(k) or type(v) is not str or len(v) > 200 for k, v in where.items()):
            raise ProofUnavailable('volatile selector is malformed')
        fieldname = item['field']
        if type(fieldname) is not str or not (fieldname == 'text' or (fieldname.startswith('@') and _ATTR.fullmatch(fieldname[1:]))):
            raise ProofUnavailable('volatile field must be text or @attribute')
        if item['value_type'] not in VALUE_TYPES or item['bound'] != 'native_write_window':
            raise ProofUnavailable('volatile values are typed utc_second_z bound to the native write window')
    caret = data['caret']
    if caret is not None:
        caret = _exact_keys(caret, {'member', 'element', 'fields', 'observed_domain'}, 'caret')
        _member_name(caret['member'])
        if type(caret['element']) is not str or not _QNAME.fullmatch(caret['element']):
            raise ProofUnavailable('caret element must be an expanded {uri}name')
        fields = caret['fields']
        if type(fields) is not dict or set(fields) != {'List', 'Para', 'Pos'} or any(type(v) is not str or not _ATTR.fullmatch(v) for v in fields.values()) or len(set(fields.values())) != 3:
            raise ProofUnavailable('caret fields must map List/Para/Pos to three attributes')
        domain = _exact_keys(caret['observed_domain'], {'List', 'Para'}, 'caret observed_domain')
        _int_domain(domain['List'])
        _int_domain(domain['Para'])
    # The caret, volatile-value and regenerated selectors are disjoint, so no
    # allowance can absorb another one's expectation.
    selectors = [(v['member'], v['element'], tuple(sorted(v['where'].items())), v['field']) for v in values]
    if len(set(selectors)) != len(selectors):
        raise ProofUnavailable('volatile_values selectors must be distinct')
    for item in values:
        if item['member'] in regenerated:
            raise ProofUnavailable('a regenerated preview cannot also hold volatile values')
        if caret is not None and item['member'] == caret['member'] and item['element'] == caret['element']:
            raise ProofUnavailable('the caret element cannot also hold volatile values')
    if caret is not None and caret['member'] in regenerated:
        raise ProofUnavailable('the caret member cannot be regenerated representation')
    attributes = data['replacement_manifest_attributes']
    if (type(attributes) is not list or len(set(attributes)) != len(attributes) or not {'id', 'href', 'media-type'} <= set(attributes)
            or any(type(a) is not str or not _ATTR.fullmatch(a.replace('-', '_')) for a in attributes) or len(attributes) > 16):
        raise ProofUnavailable('replacement_manifest_attributes lists the qualified new-item attributes (id, href, media-type at least)')
    public = _exact_keys(data['public_save'], {'save_false_after_export_clear'}, 'public_save')
    if type(public['save_false_after_export_clear']) is not bool:
        raise ProofUnavailable('public_save flag must be a Boolean')
    history = data['history']
    if type(history) is not dict or not set(history) <= set(EDIT_OPS):
        raise ProofUnavailable('history names only picture edit operations')
    for value in history.values():
        value = _exact_keys(value, {'undo_native_steps', 'redo_native_steps'}, 'history kind')
        if any(type(n) is not int or not 1 <= n <= 4 for n in value.values()):
            raise ProofUnavailable('history native step counts must be integers 1..4')
    if 'line_reflow' in data:
        reflow = data['line_reflow']
        if type(reflow) is not dict or not reflow or not set(reflow) <= set(REFLOW_OPS):
            raise ProofUnavailable('line_reflow names only ' + ', '.join(REFLOW_OPS))
        for op, rule in reflow.items():
            rule = _exact_keys(rule, {'scope', 'fields'}, 'line_reflow ' + op)
            fields = rule['fields']
            if rule['scope'] != REFLOW_SCOPE:
                raise ProofUnavailable('line_reflow scope is ' + REFLOW_SCOPE)
            if (type(fields) is not list or not fields or len(set(fields)) != len(fields)
                    or any(type(f) is not str or f not in REFLOW_FIELDS for f in fields)):
                raise ProofUnavailable('line_reflow fields are a non-empty subset of ' + ', '.join(REFLOW_FIELDS)
                                       + '; textpos, vertpos, spacing and flags stay anchors')
    evidence = _exact_keys(data['evidence'], set(EVIDENCE_FIELDS) | optional, 'evidence')
    for name, row in evidence.items():
        row = _exact_keys(row, {'status', 'basis'}, 'evidence ' + name)
        if row['status'] not in ('observed', 'not_run') or type(row['basis']) is not str or not 1 <= len(row['basis']) <= 600:
            raise ProofUnavailable('evidence status is observed or not_run with a bounded basis')
    return data


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def load_profile(environ: Mapping[str, str] | None = None) -> Profile | None:
    """The configured profile, pinned by digest, or None for the legacy route.

    The file is read once per request, before any native effect. A configured
    but unreadable, unpinned, mismatched or malformed profile refuses; it never
    silently selects another strategy.
    """
    if environ is None:
        # The documented settings path (.env plus HWP_* environment), read fresh per request.
        try:
            from app.config import Settings
            settings = Settings()
        except Exception as exc:
            raise ProofUnavailable('server settings are unreadable') from exc
        env: Mapping[str, str] = {'HWP_PICTURE_HWPX_PROFILE': settings.picture_hwpx_profile,
                                  'HWP_PICTURE_HWPX_PROFILE_SHA256': settings.picture_hwpx_profile_sha256}
    else:
        env = environ
    raw_path = env.get('HWP_PICTURE_HWPX_PROFILE', '').strip()
    pin = env.get('HWP_PICTURE_HWPX_PROFILE_SHA256', '').strip().lower()
    if not raw_path and not pin:
        return None
    if not raw_path or not _HEX64.fullmatch(pin):
        raise ProofUnavailable('HWPX proof profile path and exact SHA-256 pin are both required')
    path = Path(raw_path)
    try:
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ProofUnavailable('HWPX proof profile must be an absolute regular file')
        data = path.read_bytes()
    except OSError as exc:
        raise ProofUnavailable('HWPX proof profile is unreadable') from exc
    if hashlib.sha256(data).hexdigest() != pin:
        raise ProofUnavailable('HWPX proof profile does not match its configured SHA-256')
    try:
        parsed = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProofUnavailable('HWPX proof profile is not JSON') from exc
    return Profile(validate_profile(parsed), pin, 'operator_configured_file')


# --------------------------------------------------------------------------- package state


@dataclass(frozen=True)
class PackageState:
    path: str
    sha256: str
    size_bytes: int
    order: tuple[str, ...]
    members: Mapping[str, bytes]
    container: Mapping[str, Mapping[str, Any]]
    archive_comment: bytes
    receipt: Mapping[str, Any] = field(default_factory=dict)
    epoch: Mapping[str, Any] = field(default_factory=dict)
    # Measured UTC (start, end) of the native call that wrote these bytes, or None.
    window: tuple[datetime, datetime] | None = None

    def summary(self) -> dict[str, Any]:
        return {'sha256': self.sha256, 'size_bytes': self.size_bytes, 'members': len(self.order)}


def read_state(path: str | Path, *, receipt: Mapping[str, Any] | None = None, epoch: Mapping[str, Any] | None = None,
               window: tuple[datetime, datetime] | None = None) -> PackageState:
    """Complete package bytes, re-hashed from the same bytes the reader returns."""
    from local_cli_v1.hwpx_reader import read_package_entries
    path = Path(path)
    data = path.read_bytes()
    entries = read_package_entries(path)
    if path.read_bytes() != data:
        raise PackageMismatch('package changed during complete read')
    digest = hashlib.sha256(data).hexdigest()
    if receipt is not None and receipt.get('sha256') not in (None, digest):
        raise PackageMismatch('package bytes differ from the private-copy receipt')
    if window is not None and (len(window) != 2 or any(type(t) is not datetime or t.tzinfo is None for t in window) or window[0] > window[1]):
        raise PackageMismatch('native write window is not an ordered pair of aware UTC times')
    return PackageState(str(path), digest, len(data), tuple(entries['order']), dict(entries['members']),
                        {k: dict(v) for k, v in entries['container'].items()}, entries['archive_comment'],
                        dict(receipt or {}), dict(epoch or {}), window)


def producer_text(raw: bytes, *, chars: int, sha256: str) -> str:
    """Exact decoder for a producer COM string persisted as UTF-8 bytes.

    Qualified on actual Hancom 13.0.0.3457 GetTextFile('HWPML2X') captures: the
    bytes are strict UTF-8 without BOM, their SHA-256 equals the producer
    witness ``sha256(str.encode('utf-8'))`` and the decoded length equals its
    ``chars``, although the text keeps its in-memory ``encoding="UTF-16"``
    declaration. The declaration is NOT a byte-decoding instruction for such
    persisted strings; bytes are never rewritten to satisfy a parser.
    """
    if type(raw) is not bytes or raw.startswith((b'\xef\xbb\xbf', b'\xff\xfe', b'\xfe\xff')):
        raise ValueError('producer string bytes must be BOM-free UTF-8 bytes')
    if type(chars) is not int or type(sha256) is not str or not _HEX64.fullmatch(sha256):
        raise ValueError('producer witness chars/sha256 are malformed')
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('producer string bytes differ from the witness digest')
    text = raw.decode('utf-8', errors='strict')
    if len(text) != chars or text.encode('utf-8') != raw:
        raise ValueError('producer string length or round trip differs from the witness')
    return text


# --------------------------------------------------------------------------- XML helpers


def _local(tag: Any) -> str:
    return tag.rsplit('}', 1)[-1] if type(tag) is str else ''


def _outer(data: bytes) -> tuple[bytes, bytes]:
    """Exact bytes before the root start tag and after the root end tag.

    The prefix carries any BOM, the XML declaration and every comment, PI and
    whitespace before the root; the suffix carries the whole epilog. Both are
    compared byte for byte, so nothing outside the root element is lost to the
    element-tree parser. Any DOCTYPE or entity declaration refuses.
    """
    from xml.parsers import expat
    parser = expat.ParserCreate()
    marks: dict[str, Any] = {'depth': 0, 'start': None, 'end': None}

    def refuse(*_args: Any) -> None:
        raise XmlUnproved('XML member declares a DOCTYPE or entity')

    def start(_name: str, _attrs: Any) -> None:
        if marks['depth'] == 0 and marks['start'] is None:
            marks['start'] = parser.CurrentByteIndex
        marks['depth'] += 1

    def end(_name: str) -> None:
        marks['depth'] -= 1

    def outside(*_args: Any) -> None:
        if marks['depth'] == 0 and marks['start'] is not None and marks['end'] is None:
            marks['end'] = parser.CurrentByteIndex

    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.UnparsedEntityDeclHandler = refuse
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CommentHandler = outside
    parser.ProcessingInstructionHandler = outside
    # Every byte without a specific handler (whitespace in the epilog included).
    parser.DefaultHandler = outside
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise XmlUnproved('XML member is not one well-formed document') from exc
    if marks['start'] is None:
        raise XmlUnproved('XML member has no root element')
    end_at = len(data) if marks['end'] is None else marks['end']
    return data[:marks['start']], data[end_at:]


def _parse(data: bytes) -> ET.Element:
    _outer(data)
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    return ET.fromstring(data, parser=parser)


def _prolog(data: bytes) -> bytes:
    """Exact leading bytes through the XML declaration (BOM included), or b''."""
    for bom, encoding in ((b'\xff\xfe', 'utf-16-le'), (b'\xfe\xff', 'utf-16-be'), (b'\xef\xbb\xbf', 'utf-8'), (b'', 'utf-8')):
        start, end_marker = (bom + '<?xml'.encode(encoding)), '?>'.encode(encoding)
        if data.startswith(start):
            end = data.find(end_marker, len(start))
            return data[:end + len(end_marker)] if end >= 0 else data
    return b''


def _path_of(root: ET.Element, element: ET.Element) -> tuple[int, ...]:
    for path, node in _paths(root, _local(element.tag)):
        if node is element:
            return path
    raise PackageMismatch('element path is unproved')


def _namespaces(data: bytes) -> list[tuple[str, str]]:
    _outer(data)
    return [value for _event, value in ET.iterparse(io.BytesIO(data), events=('start-ns',))]


def _difference(expected: ET.Element, actual: ET.Element, path: str = '') -> str | None:
    """First infoset difference: expanded names, attributes, text/tail, comments, PIs and order."""
    here = f'{path}/{_local(expected.tag) or str(expected.tag)}'
    if expected.tag is not actual.tag and expected.tag != actual.tag:
        return here + ':tag'
    if dict(expected.attrib) != dict(actual.attrib):
        keys = sorted(set(expected.attrib) | set(actual.attrib), key=str)
        changed = [k for k in keys if expected.attrib.get(k) != actual.attrib.get(k)]
        return here + ':@' + ','.join(_local(k) for k in changed[:6])
    if (expected.text or None) != (actual.text or None):
        return here + ':text'
    if (expected.tail or None) != (actual.tail or None):
        return here + ':tail'
    if len(expected) != len(actual):
        return here + ':children'
    for index, (a, b) in enumerate(zip(expected, actual)):
        found = _difference(a, b, f'{here}[{index}]')
        if found:
            return found
    return None


def _canonical_tree(element: ET.Element) -> list[Any]:
    tag = element.tag if type(element.tag) is str else ('#comment' if element.tag is ET.Comment else '#pi')
    return [tag, sorted(element.attrib.items()), element.text or None, element.tail or None,
            [_canonical_tree(child) for child in element]]


def _one_child(element: ET.Element, local: str) -> ET.Element:
    found = [child for child in element if _local(child.tag) == local]
    if len(found) != 1:
        raise BridgeUnproven('exact HWPX picture child is unproved: ' + local)
    return found[0]


def _exact_attr(element: ET.Element, name: str) -> str:
    if [k for k in element.attrib if type(k) is str and k.casefold() == name.casefold()] != [name]:
        raise BridgeUnproven('exact HWPX attribute is unproved: ' + name)
    return element.attrib[name]


def _at_path(root: ET.Element, path: tuple[int, ...]) -> ET.Element:
    node = root
    for index in path:
        children = list(node)
        if not 0 <= index < len(children):
            raise PackageMismatch('target picture path no longer exists')
        node = children[index]
    return node


def _paths(root: ET.Element, local: str) -> list[tuple[tuple[int, ...], ET.Element]]:
    found: list[tuple[tuple[int, ...], ET.Element]] = []

    def visit(node: ET.Element, path: tuple[int, ...]) -> None:
        if len(path) > 400:
            raise BridgeUnproven('HWPX nesting is too deep')
        if _local(node.tag) == local:
            found.append((path, node))
        for index, child in enumerate(node):
            visit(child, path + (index,))
    visit(root, ())
    return found


# --------------------------------------------------------------------------- manifest / bridge


def _manifest(state: PackageState) -> dict[str, Any]:
    data = state.members.get('Contents/content.hpf')
    if data is None:
        raise BridgeUnproven('HWPX manifest Contents/content.hpf is missing')
    root = _parse(data)
    items: dict[str, dict[str, Any]] = {}
    for element in root.iter():
        if _local(element.tag) != 'item':
            continue
        ident, href = element.get('id'), element.get('href')
        if type(ident) is not str or type(href) is not str or not ident or not href or ident in items:
            raise BridgeUnproven('HWPX manifest item identity is ambiguous')
        items[ident] = {'href': href, 'element': element}
    hrefs = [value['href'] for value in items.values()]
    if len(set(hrefs)) != len(hrefs):
        raise BridgeUnproven('HWPX manifest aliases one member')
    spine = [e.get('idref') for e in root.iter() if _local(e.tag) == 'itemref']
    sections = []
    for ident in spine:
        if ident in items and _SECTION.fullmatch(items[ident]['href']):
            sections.append(items[ident]['href'])
    present = [name for name in state.order if _SECTION.fullmatch(name)]
    if not sections or len(set(sections)) != len(sections) or sorted(sections) != sorted(present):
        raise BridgeUnproven('HWPX section spine does not cover exactly the section members')
    return {'root': root, 'items': items, 'sections': sections}


def _media_sha(state: PackageState, manifest: dict[str, Any], ref: str) -> tuple[str, str]:
    item = manifest['items'].get(ref)
    if item is None or item['href'] not in state.members:
        raise BridgeUnproven('HWPX picture media reference is unresolved')
    return item['href'], hashlib.sha256(state.members[item['href']]).hexdigest()


def _hwpx_fingerprint(state: PackageState, manifest: dict[str, Any], pic: ET.Element) -> dict[str, Any]:
    size, pos = _one_child(pic, 'sz'), _one_child(pic, 'pos')
    clip, dim, img = _one_child(pic, 'imgClip'), _one_child(pic, 'imgDim'), _one_child(pic, 'img')
    as_char = _exact_attr(pos, 'treatAsChar')
    if as_char not in ('0', '1'):
        raise BridgeUnproven('HWPX treatAsChar is not an exact OWPML Boolean')
    href, sha = _media_sha(state, manifest, _exact_attr(img, 'binaryItemIDRef'))
    facts = {'width': _exact_attr(size, 'width'), 'height': _exact_attr(size, 'height'),
             'inline': as_char == '1', 'horz': _exact_attr(pos, 'horzOffset'), 'vert': _exact_attr(pos, 'vertOffset'),
             'clip': tuple(_exact_attr(clip, k) for k in ('left', 'top', 'right', 'bottom')),
             'dim': (_exact_attr(dim, 'dimwidth'), _exact_attr(dim, 'dimheight')), 'media': sha}
    for key in ('width', 'height', 'horz', 'vert'):
        if not _INT.fullmatch(facts[key]):
            raise BridgeUnproven('HWPX picture number is not an exact integer')
    if not all(_INT.fullmatch(v) for v in facts['clip'] + facts['dim']):
        raise BridgeUnproven('HWPX picture crop/dimension is not exact')
    return {**facts, 'href': href}


def _hwpml_fingerprint(picture: Mapping[str, Any]) -> dict[str, Any]:
    frame, place = picture.get('frame') or {}, picture.get('placement') or {}
    clip, dim, media = picture.get('crop') or {}, picture.get('source_dimensions') or {}, picture.get('media') or []
    if len(media) != 1 or not media[0].get('payload_sha256') or place.get('TreatAsChar') not in ('true', 'false'):
        raise BridgeUnproven('HWPML picture facts are not exact for the bridge')
    return {'width': frame.get('Width'), 'height': frame.get('Height'), 'inline': place['TreatAsChar'] == 'true',
            'horz': place.get('HorzOffset'), 'vert': place.get('VertOffset'),
            'clip': tuple(clip.get(k) for k in ('Left', 'Top', 'Right', 'Bottom')),
            'dim': (dim.get('Width'), dim.get('Height')), 'media': media[0]['payload_sha256']}


@dataclass(frozen=True)
class Bridge:
    ordinal: int
    count: int
    section: str
    path: tuple[int, ...]
    media_ref: str
    media_member: str

    def public(self) -> dict[str, Any]:
        return {'method': BRIDGE, 'ordinal': self.ordinal, 'pictures': self.count, 'section_member': self.section,
                'identity_namespaces': 'HWPML SHAPEOBJECT InstId and HWPX hp:pic ids are not compared'}


def bridge(model: Mapping[str, Any], target: Mapping[str, Any], state: PackageState, request: Mapping[str, Any]) -> Bridge:
    """Complete ordinal census with physical facts for EVERY picture; no ID equality."""
    manifest = _manifest(state)
    found: list[tuple[str, tuple[int, ...], ET.Element]] = []
    for section in manifest['sections']:
        root = _parse(state.members[section])
        found.extend((section, path, pic) for path, pic in _paths(root, 'pic'))
    pictures = list(model['pictures'])
    if len(found) != len(pictures):
        raise BridgeUnproven('HWPML and HWPX picture censuses differ in size')
    ordinal = None
    for index, ((section, path, pic), record) in enumerate(zip(found, pictures)):
        hwpx = _hwpx_fingerprint(state, manifest, pic)
        legacy = _hwpml_fingerprint(record)
        if {k: v for k, v in hwpx.items() if k != 'href'} != legacy:
            raise BridgeUnproven(f'picture {index} physical facts differ between HWPML and HWPX')
        if record is target or record.get('_path') == target.get('_path'):
            if ordinal is not None:
                raise BridgeUnproven('target matches more than one census position')
            ordinal = index
    if ordinal is None:
        raise BridgeUnproven('target is absent from the HWPX census')
    section, path, pic = found[ordinal]
    ref = _exact_attr(_one_child(pic, 'img'), 'binaryItemIDRef')
    op = request['op']
    pos = _one_child(pic, 'pos')
    if op == 'picture_layout_exact':
        _exact_attr(pos, 'treatAsChar')
        if request['mode'] == 'square':
            _exact_attr(pic, 'textWrap')
        for public, native in (('horizontal_offset_mm', 'horzOffset'), ('vertical_offset_mm', 'vertOffset')):
            if public in request:
                _exact_attr(pos, native)
    elif op == 'picture_crop_exact':
        for key in ('left', 'top', 'right', 'bottom'):
            _exact_attr(_one_child(pic, 'imgClip'), key)
    return Bridge(ordinal, len(found), section, path, ref, manifest['items'][ref]['href'])


def check_flow(state: PackageState, link: Bridge, flow_prestate: Any) -> None:
    """Existing serialized HWPX textFlow must agree with the observed getter value."""
    pic = _at_path(_parse(state.members[link.section]), link.path)
    aliases = [k for k in pic.attrib if type(k) is str and k.casefold() == 'textflow']
    if aliases and aliases != ['textFlow']:
        raise ValueError('HWPX textFlow serialization case conflict')
    current = pic.get('textFlow')
    if current is not None and current != FLOW[flow_prestate.observed]:
        raise ValueError('current HWPX textFlow conflicts with the observed TextFlow')


# --------------------------------------------------------------------------- caret


def caret_values(state: PackageState, profile: Profile) -> tuple[int, int, int]:
    spec = profile.data['caret']
    if spec is None:
        raise ProofUnavailable('profile has no serialized caret mapping')
    data = state.members.get(spec['member'])
    if data is None:
        raise ProofUnavailable('serialized caret member is missing')
    try:
        root = _parse(data)
    except (ET.ParseError, XmlUnproved) as exc:
        raise ProofUnavailable('serialized caret member is not proved XML') from exc
    found = [e for e in root.iter() if e.tag == spec['element']]
    if len(found) != 1:
        raise ProofUnavailable('serialized caret element is not unique')
    values = []
    for key in ('List', 'Para', 'Pos'):
        raw = found[0].get(spec['fields'][key])
        if type(raw) is not str or not _INT.fullmatch(raw) or raw.startswith('-'):
            raise ProofUnavailable('serialized caret field is not an exact integer')
        values.append(int(raw))
    return tuple(values)


def caret_in_domain(profile: Profile, position: tuple[int, int, int]) -> bool:
    """Only List/Para values natively observed under this mapping are admitted."""
    spec = profile.data['caret']
    domain = spec['observed_domain'] if spec is not None else None
    return domain is not None and position[0] in domain['List'] and position[1] in domain['Para']


def replacement_admissible(state: PackageState, link: 'Bridge', profile: Profile) -> list[str]:
    """Attributes of the bound manifest item whose meaning for a NEW item is unqualified."""
    item = _manifest(state)['items'][link.media_ref]['element']
    allowed = set(profile.data['replacement_manifest_attributes'])
    return sorted(key for key in item.attrib if key not in allowed)


# --------------------------------------------------------------------------- expectation


@dataclass
class Expectation:
    trees: dict[str, ET.Element]
    added: dict[str, Any] | None = None
    reasons: dict[str, str] = field(default_factory=dict)
    # True only for a changed picture edit: the one condition under which a
    # profile-declared preview member may be regenerated rather than identical.
    rendering_request: bool = False
    # Report of the request-bound derived line reflow admitted for this edit, or None.
    reflow: dict[str, Any] | None = None


# --------------------------------------------------------------------------- request-bound line reflow


class _LineCacheInvalid(ValueError):
    """A paragraph line cache or section geometry is outside the reflow contract."""


def _line_rows(paragraph: ET.Element) -> tuple[int, list[dict[str, int]]]:
    """The paragraph's one exact OWPML hp:linesegarray: its child index and typed rows."""
    arrays = [(index, child) for index, child in enumerate(paragraph) if child.tag == _HP + 'linesegarray']
    if len(arrays) != 1:
        raise _LineCacheInvalid('the paragraph does not hold exactly one hp:linesegarray')
    index, array = arrays[0]
    if array.attrib or array.text or array.tail:
        raise _LineCacheInvalid('hp:linesegarray carries attributes or text')
    rows: list[dict[str, int]] = []
    for segment in array:
        if segment.tag != _HP + 'lineseg':
            raise _LineCacheInvalid('hp:linesegarray holds a node other than hp:lineseg')
        if len(segment) or segment.text or segment.tail:
            raise _LineCacheInvalid('hp:lineseg carries children or text')
        if set(segment.attrib) != set(LINESEG_ATTRIBUTES):
            raise _LineCacheInvalid('hp:lineseg does not carry exactly the nine OWPML attributes')
        row = {}
        for name in LINESEG_ATTRIBUTES:
            raw = segment.attrib[name]
            if not _DECIMAL.fullmatch(raw):
                raise _LineCacheInvalid(f'hp:lineseg {name} is not an exact decimal integer')
            low, high = (0, 2**32 - 1) if name in LINESEG_UNSIGNED else (-2**31, 2**31 - 1)
            if not low <= int(raw) <= high:
                raise _LineCacheInvalid(f'hp:lineseg {name} is outside its OWPML integer type')
            row[name] = int(raw)
        rows.append(row)
    if not 1 <= len(rows) <= MAX_REFLOW_LINES:
        raise _LineCacheInvalid('the paragraph line count is outside 1..%d' % MAX_REFLOW_LINES)
    return index, rows


def _section_geometry(root: ET.Element) -> dict[str, int]:
    """Text width and paper height of the one evidenced section variant (single column, WIDELY, no gutter)."""
    pages = [e for e in root.iter() if e.tag == _HP + 'pagePr']
    if len(pages) != 1:
        raise _LineCacheInvalid('the section does not hold exactly one hp:pagePr')
    margins = [c for c in pages[0] if c.tag == _HP + 'margin']
    if len(margins) != 1:
        raise _LineCacheInvalid('hp:pagePr does not hold exactly one hp:margin')
    if pages[0].get('landscape') != 'WIDELY':
        raise _LineCacheInvalid('only the observed WIDELY page orientation is covered')
    columns = [e for e in root.iter() if e.tag == _HP + 'colPr']
    if not columns or any(c.get('colCount') != '1' for c in columns):
        raise _LineCacheInvalid('only single-column sections are covered')
    values = {}
    for element, name in ((pages[0], 'width'), (pages[0], 'height'), (margins[0], 'left'), (margins[0], 'right'), (margins[0], 'gutter')):
        raw = element.get(name)
        if type(raw) is not str or not _DECIMAL.fullmatch(raw) or raw.startswith('-'):
            raise _LineCacheInvalid(f'page {name} is not an exact non-negative integer')
        values[name] = int(raw)
    if values['gutter'] != 0:
        raise _LineCacheInvalid('only a zero gutter is covered')
    width = values['width'] - values['left'] - values['right']
    if width <= 0 or values['height'] <= 0:
        raise _LineCacheInvalid('the section text area is empty')
    return {'text_width': width, 'paper_height': values['height']}


def _line_bounds(rows: list[dict[str, int]], geometry: Mapping[str, int]) -> None:
    """Relationships every observed native line satisfies; a line outside them is not admitted."""
    for row in rows:
        if any(row[name] < 0 for name in REFLOW_FIELDS):
            raise _LineCacheInvalid('a line-box metric is negative')
        if row['baseline'] > row['vertsize'] or row['textheight'] > row['vertsize']:
            raise _LineCacheInvalid('the baseline or text height lies outside the line height')
        if row['horzpos'] + row['horzsize'] > geometry['text_width']:
            raise _LineCacheInvalid('the line extends beyond the section text width')
        if row['vertsize'] > geometry['paper_height']:
            raise _LineCacheInvalid('the line is taller than the paper')


@dataclass(frozen=True)
class ReflowScope:
    """Bound from the before state and request only, before any effect.

    paragraph_child_index is the paragraph's child index in the section root
    (comments and other nodes count), never its hp:p id attribute.
    """
    trigger: str
    section: str
    paragraph_child_index: int
    paragraph_id: str | None
    array_index: int
    before_rows: tuple[Mapping[str, int], ...]
    fields: tuple[str, ...]
    text_width: int
    paper_height: int

    def public(self) -> dict[str, Any]:
        return {'contract': REFLOW_CONTRACT, 'trigger': self.trigger, 'section_member': self.section,
                'paragraph_child_index': self.paragraph_child_index, 'paragraph_id': self.paragraph_id,
                'lines': len(self.before_rows), 'fields': list(self.fields), 'text_width': self.text_width}


def reflow_scope(state: PackageState, link: 'Bridge', request: Mapping[str, Any], profile: Profile) -> ReflowScope | None:
    """The target picture's own paragraph line cache a declared layout request may recompute.

    None when the profile declares no reflow for this operation (strict comparison).
    A declared reflow whose target paragraph, line cache or section geometry is
    outside the contract refuses before any effect.
    """
    fields = profile.reflow(request['op'])
    if fields is None:
        return None
    root = _parse(state.members[link.section])
    if root.tag != _HS + 'sec' or len(link.path) != 3:
        raise ProofUnavailable('the target picture is not in a run of a top-level body paragraph')
    paragraph, run, pic = _at_path(root, link.path[:1]), _at_path(root, link.path[:2]), _at_path(root, link.path)
    if (paragraph.tag, run.tag, pic.tag) != (_HP + 'p', _HP + 'run', _HP + 'pic'):
        raise ProofUnavailable('the target picture is not in a run of a top-level body paragraph')
    try:
        geometry = _section_geometry(root)
        index, rows = _line_rows(paragraph)
        _line_bounds(rows, geometry)
    except _LineCacheInvalid as exc:
        raise ProofUnavailable('the target paragraph line cache is outside ' + REFLOW_CONTRACT + ': ' + str(exc)) from exc
    ident = paragraph.get('id')
    return ReflowScope(request['op'], link.section, link.path[0], ident if type(ident) is str and re.fullmatch(r'[0-9]{1,10}', ident) else None,
                       index, tuple(rows), fields, geometry['text_width'], geometry['paper_height'])


def _apply_reflow(scope: ReflowScope, expected: ET.Element, actual: ET.Element) -> dict[str, Any]:
    """Admit only the declared metrics of the scoped paragraph's lines; everything else stays compared exactly."""
    def refuse(reason: str, **extra: Any) -> PackageMismatch:
        return PackageMismatch('derived line reflow is outside ' + REFLOW_CONTRACT,
                               [{'kind': 'line_reflow', 'reason': reason, 'member': scope.section, **extra}])
    expected_paragraph = _at_path(expected, (scope.paragraph_child_index,))
    try:
        actual_paragraph = _at_path(actual, (scope.paragraph_child_index,))
    except PackageMismatch:
        raise refuse('paragraph') from None
    if actual_paragraph.tag != expected_paragraph.tag:
        raise refuse('paragraph')
    try:
        index, rows = _line_rows(actual_paragraph)
        _line_bounds(rows, {'text_width': scope.text_width, 'paper_height': scope.paper_height})
    except _LineCacheInvalid as exc:
        raise refuse('malformed', detail=str(exc)[:120]) from None
    if index != scope.array_index or len(rows) != len(scope.before_rows):
        raise refuse('cardinality')
    segments = list(list(expected_paragraph)[scope.array_index])
    changed_lines, changed = 0, set()
    for before, after, segment in zip(scope.before_rows, rows, segments):
        for name in LINESEG_ATTRIBUTES:
            if name not in scope.fields and after[name] != before[name]:
                raise refuse('anchor', field=name)
        delta = [name for name in scope.fields if after[name] != before[name]]
        for name in delta:
            segment.set(name, str(after[name]))
        if delta:
            changed_lines += 1
            changed.update(delta)
    return {**scope.public(), 'changed_lines': changed_lines, 'changed_fields': [f for f in REFLOW_FIELDS if f in changed]}


def _set(element: ET.Element, name: str, value: str) -> None:
    _exact_attr(element, name)
    element.set(name, value)


def expect(before: PackageState, after: PackageState, link: Bridge, request: Mapping[str, Any], *,
           settings: Mapping[str, int], expected_crop: Mapping[str, int] | None, flow_prestate: Any,
           ui_plan: Any, profile: Profile, source: Mapping[str, Any] | None,
           reflow: ReflowScope | None = None) -> Expectation:
    """Expected complete after-state from request and positively bound prestate.

    ``reflow`` (bound before effect by reflow_scope) admits only its declared
    line-box metrics of the target paragraph; it never applies to another
    operation, paragraph or member, and never to saved or history comparisons.
    """
    trees: dict[str, ET.Element] = {}
    section = _parse(before.members[link.section])
    pic = _at_path(section, link.path)
    op = request['op']
    added = None
    report = None
    if reflow is not None and (reflow.trigger != op or reflow.section != link.section or reflow.paragraph_child_index != link.path[0]):
        raise ValueError('the line reflow scope is not bound to this request target')
    if op == 'picture_layout_exact':
        pos = _one_child(pic, 'pos')
        _set(pos, 'treatAsChar', '1' if settings['TreatAsChar'] else '0')
        if 'TextWrap' in settings:
            _set(pic, 'textWrap', 'SQUARE')
        for native, name in (('HorzOffset', 'horzOffset'), ('VertOffset', 'vertOffset')):
            if native in settings:
                _set(pos, name, str(settings[native]))
        actual_section = _parse(after.members[link.section])
        actual_pic = _at_path(actual_section, link.path)
        if (flow_prestate is not None and flow_prestate.may_materialize and 'textFlow' not in pic.attrib
                and 'textFlow' in actual_pic.attrib):
            # From the pre-Execute typed getter only, never from the after-copy value.
            pic.set('textFlow', FLOW[flow_prestate.observed])
        if reflow is not None:
            report = _apply_reflow(reflow, section, actual_section)
    elif op == 'picture_crop_exact':
        clip = _one_child(pic, 'imgClip')
        for public, name in (('Left', 'left'), ('Top', 'top'), ('Right', 'right'), ('Bottom', 'bottom')):
            _set(clip, name, str(expected_crop[public]))
    elif op == 'picture_replace_exact':
        added = _replacement(before, after, link, source, trees, profile)
        _set(_one_child(pic, 'img'), 'binaryItemIDRef', added['ref'])
    else:
        raise ValueError('unknown picture operation')
    trees[link.section] = section
    if ui_plan is not None and ui_plan.original != ui_plan.expected:
        spec = profile.data['caret']
        if spec is None:
            raise ProofUnavailable('a caret move needs the profile caret mapping')
        tree = trees.get(spec['member']) or _parse(before.members[spec['member']])
        element = [e for e in tree.iter() if e.tag == spec['element']]
        if len(element) != 1:
            raise PackageMismatch('serialized caret element is not unique')
        for key, value in zip(('List', 'Para', 'Pos'), ui_plan.expected):
            _set(element[0], spec['fields'][key], str(value))
        trees[spec['member']] = tree
    return Expectation(trees, added if added and added['new'] else None,
                       {'replacement': 'reused' if added and not added['new'] else 'allocated' if added else 'none'},
                       rendering_request=True, reflow=report)


def _pixels(data: bytes) -> str:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        if image.format != 'PNG' or image.width * image.height > 20_000_000:
            raise PackageMismatch('replacement member is not a bounded PNG')
        image.load()
        return hashlib.sha256(image.convert('RGBA').tobytes()).hexdigest()


def _alias(name: str) -> str:
    return unicodedata.normalize('NFC', name).casefold()


def _replacement(before: PackageState, after: PackageState, link: Bridge, source: Mapping[str, Any] | None,
                 trees: dict[str, ET.Element], profile: Profile) -> dict[str, Any]:
    """Bounded allocation: reuse one unchanged identical asset, or append exactly one PNG binding."""
    if source is None:
        raise ValueError('replacement source proof is missing')
    after_pic = _at_path(_parse(after.members[link.section]), link.path)
    ref = _exact_attr(_one_child(after_pic, 'img'), 'binaryItemIDRef')
    if ref == link.media_ref:
        raise PackageMismatch('replacement left the original media binding')
    old, new = _manifest(before), _manifest(after)
    if ref in old['items']:
        member = old['items'][ref]['href']
        if _pixels(before.members[member]) != source['pixels']:
            raise PackageMismatch('reused media binding is not the requested image')
        return {'new': False, 'ref': ref, 'member': member}
    added = [name for name in after.order if name not in before.members]
    if len(added) != 1 or new['items'].get(ref, {}).get('href') != added[0]:
        raise PackageMismatch('replacement did not allocate exactly one target-bound member')
    name = added[0]
    if not name.startswith('BinData/') or _alias(name) in {_alias(n) for n in before.order}:
        raise PackageMismatch('replacement member name is unqualified or aliases an existing member')
    if _pixels(after.members[name]) != source['pixels']:
        raise PackageMismatch('replacement member pixels differ from the requested image')
    template = old['items'][link.media_ref]['element']
    item = new['items'][ref]['element']
    if item.tag != template.tag or set(item.attrib) != set(template.attrib) or len(item) or (item.text or '').strip():
        raise PackageMismatch('replacement manifest item shape differs from the original binding')
    if not set(item.attrib) <= set(profile.data['replacement_manifest_attributes']):
        raise PackageMismatch('replacement manifest item carries an attribute whose new-item meaning is unqualified')
    for key, value in item.attrib.items():
        expected = {'id': ref, 'href': name}.get(key, template.attrib[key])
        if key == 'media-type':
            expected = 'image/png'
        if value != expected:
            raise PackageMismatch('replacement manifest item has an unrequested attribute value')
    parent_after = [p for p in new['root'].iter() if item in list(p)]
    parent_before = [p for p in old['root'].iter() if template in list(p)]
    if len(parent_after) != 1 or len(parent_before) != 1:
        raise PackageMismatch('replacement manifest parent is unproved')
    expected_root = copy.deepcopy(old['root'])
    path = _path_of(old['root'], parent_before[0])
    if _path_of(new['root'], parent_after[0]) != path:
        raise PackageMismatch('replacement manifest item moved to another container')
    # Existing items keep their order; only the one validated binding is inserted.
    _at_path(expected_root, path).insert(list(parent_after[0]).index(item), copy.deepcopy(item))
    trees['Contents/content.hpf'] = expected_root
    return {'new': True, 'ref': ref, 'member': name, 'pixels': source['pixels']}


# --------------------------------------------------------------------------- comparison


def _volatile_specs(profile: Profile, member: str) -> list[Mapping[str, Any]]:
    return [v for v in profile.data['volatile_values'] if v['member'] == member]


def _apply_volatile(expected: ET.Element, actual: ET.Element, specs: list[Mapping[str, Any]], *, blank: bool = False) -> None:
    for spec in specs:
        def select(root: ET.Element) -> list[ET.Element]:
            return [e for e in root.iter() if e.tag == spec['element'] and all(e.get(k) == v for k, v in spec['where'].items())]
        mine, theirs = select(expected), select(actual)
        if len(mine) != 1 or len(theirs) != 1:
            raise PackageMismatch('profile volatile value is not exactly one element', [{'member': spec['member'], 'field': spec['field']}])
        if spec['field'] == 'text':
            mine[0].text = None if blank else theirs[0].text
            if blank:
                theirs[0].text = None
        else:
            key = spec['field'][1:]
            if key not in mine[0].attrib or key not in theirs[0].attrib:
                raise PackageMismatch('profile volatile attribute is absent', [{'member': spec['member'], 'field': spec['field']}])
            mine[0].set(key, '' if blank else theirs[0].attrib[key])
            if blank:
                theirs[0].set(key, '')


def _png_size(data: bytes) -> tuple[int, int] | None:
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or image.width * image.height > 20_000_000:
                return None
            image.load()
            return image.size
    except Exception:
        return None


def _regenerated_valid(rule: Mapping[str, Any], data: bytes) -> bool:
    if rule['kind'] == 'png':
        return _png_size(data) is not None
    if len(data) > 1_000_000:
        return False
    # Strictly the declared encoding; no alternative decoding is tried.
    try:
        data.decode(rule['encoding'], errors='strict')
    except UnicodeDecodeError:
        return False
    return True


def _utc_second(value: Any, value_type: str) -> datetime | None:
    match = VALUE_TYPES[value_type].fullmatch(value) if type(value) is str else None
    if match is None:
        return None
    try:
        return datetime(*(int(g) for g in match.groups()), tzinfo=timezone.utc)
    except ValueError:
        return None


def _in_window(moment: datetime, window: tuple[datetime, datetime] | None) -> bool:
    if window is None:
        return False
    start = window[0].astimezone(timezone.utc).replace(microsecond=0)
    end = window[1].astimezone(timezone.utc)
    if end.microsecond:
        end = end.replace(microsecond=0) + timedelta(seconds=1)
    return start <= moment <= end


def _typed_volatile(spec: Mapping[str, Any], b_tree: ET.Element, a_tree: ET.Element,
                    before: PackageState, after: PackageState) -> dict[str, Any] | None:
    """Each side's value must be an exact UTC second inside the window of the call that wrote it."""
    def value(tree: ET.Element) -> Any:
        found = [e for e in tree.iter() if e.tag == spec['element'] and all(e.get(k) == v for k, v in spec['where'].items())]
        if len(found) != 1:
            return None
        return found[0].text if spec['field'] == 'text' else found[0].get(spec['field'][1:])
    old, new = _utc_second(value(b_tree), spec['value_type']), _utc_second(value(a_tree), spec['value_type'])
    if old is None or new is None:
        return {'kind': 'volatile_type', 'member': spec['member'], 'field': spec['field']}
    if not _in_window(old, before.window) or not _in_window(new, after.window):
        return {'kind': 'volatile_window', 'member': spec['member'], 'field': spec['field']}
    if new < old:
        return {'kind': 'volatile_order', 'member': spec['member'], 'field': spec['field']}
    return None


def compare(before: PackageState, after: PackageState, expectation: Expectation, profile: Profile) -> dict[str, Any]:
    """Complete member/container comparison of after vs expected; returns counts or raises."""
    deltas: list[dict[str, Any]] = []
    added = expectation.added
    added_name = added['member'] if added else None
    order = tuple(name for name in after.order if name != added_name)
    if order != before.order:
        missing = [n for n in before.order if n not in after.members][:8]
        extra = [n for n in after.order if n not in before.members and n != added_name][:8]
        deltas.append({'kind': 'member_set_or_order', 'missing': missing, 'unexpected': extra})
        raise PackageMismatch('package member set or order changed', deltas)
    if after.archive_comment != before.archive_comment:
        raise PackageMismatch('archive comment changed', [{'kind': 'archive_comment'}])
    volatile_fields = set(profile.data['container_volatile'])
    regenerated = profile.data['regenerated_members']
    counts = {'byte_identical': 0, 'infoset_compared': 0, 'regenerated_not_compared': 0, 'added_media': 0}
    for name in before.order:
        b_meta, a_meta = before.container[name], after.container[name]
        changed = sorted(k for k in b_meta if k not in volatile_fields and b_meta[k] != a_meta.get(k))
        if changed:
            deltas.append({'kind': 'container', 'member': name, 'fields': changed})
            continue
        b, a = before.members[name], after.members[name]
        if name in regenerated and expectation.rendering_request and a != b:
            rule = regenerated[name]
            if not (_regenerated_valid(rule, b) and _regenerated_valid(rule, a)):
                deltas.append({'kind': 'regenerated_invalid', 'member': name})
            elif rule['same_pixel_size'] and _png_size(b) != _png_size(a):
                deltas.append({'kind': 'regenerated_pixel_size', 'member': name})
            else:
                counts['regenerated_not_compared'] += 1
            continue
        specs = _volatile_specs(profile, name)
        if name in expectation.trees or specs:
            try:
                if _prolog(a) != _prolog(b):
                    deltas.append({'kind': 'xml_declaration', 'member': name})
                    continue
                # The whole document: every byte before the root start tag and after its end tag.
                if _outer(a) != _outer(b):
                    deltas.append({'kind': 'xml_outer', 'member': name})
                    continue
                if _namespaces(a) != _namespaces(b):
                    deltas.append({'kind': 'namespace_declarations', 'member': name})
                    continue
                expected = copy.deepcopy(expectation.trees[name]) if name in expectation.trees else _parse(b)
                actual = _parse(a)
                typed = [found for spec in specs if (found := _typed_volatile(spec, _parse(b), actual, before, after))]
                if typed:
                    deltas.extend(typed)
                    continue
                _apply_volatile(expected, actual, specs)
            except PackageMismatch as exc:
                deltas.extend(exc.deltas or [{'kind': 'volatile', 'member': name}])
                continue
            except (ET.ParseError, ValueError) as exc:
                deltas.append({'kind': 'xml_unproved', 'member': name, 'detail': str(exc)[:120]})
                continue
            found = _difference(expected, actual)
            if found:
                deltas.append({'kind': 'xml_delta', 'member': name, 'at': found[:240]})
            else:
                counts['infoset_compared'] += 1
            continue
        if a != b:
            deltas.append({'kind': 'bytes', 'member': name})
        else:
            counts['byte_identical'] += 1
    if added:
        counts['added_media'] = 1
    if deltas:
        raise PackageMismatch('unrequested complete package delta', deltas)
    return counts


def state_digest(state: PackageState, profile: Profile) -> str:
    """Semantic digest under the profile's representation allowances (for history)."""
    rows: list[Any] = [list(state.order), base64.b64encode(state.archive_comment).decode()]
    volatile_fields = set(profile.data['container_volatile'])
    caret = profile.data['caret']
    for name in state.order:
        meta = {k: (base64.b64encode(v).decode() if type(v) is bytes else v) for k, v in state.container[name].items() if k not in volatile_fields}
        data = state.members[name]
        specs = _volatile_specs(profile, name)
        # Previews are compared: actual data shows identical previews for identical content.
        if specs or (caret is not None and name == caret['member']):
            tree, other = _parse(data), _parse(data)
            _apply_volatile(tree, other, specs, blank=True)
            if caret is not None and name == caret['member']:
                for e in tree.iter():
                    if e.tag == caret['element']:
                        for attr in caret['fields'].values():
                            if attr in e.attrib:
                                e.set(attr, '')
            prefix, suffix = _outer(data)
            body = [prefix.hex(), suffix.hex(), _namespaces(data), _canonical_tree(tree)]
        else:
            body: Any = hashlib.sha256(data).hexdigest()
        rows.append([name, meta, body])
    return hashlib.sha256(_canonical_json(rows)).hexdigest()


def members_manifest(state: PackageState) -> str:
    """Digest of the ordered (name, sha256(bytes)) member list; recomputable offline from the file."""
    return hashlib.sha256(_canonical_json([[name, hashlib.sha256(state.members[name]).hexdigest()]
                                           for name in state.order])).hexdigest()


def verify_external(path: str | Path, proof: Mapping[str, Any]) -> dict[str, Any]:
    """Re-read an external saved HWPX completely and bind it to a recorded saved proof.

    Static: no Hancom call. It proves the external bytes are exactly the saved
    package the backend compared with its verified state, member by member.
    """
    state = read_state(path)
    manifest = members_manifest(state)
    checks = {'package_sha256': state.sha256 == proof.get('saved_package_sha256'),
              'members_manifest_sha256': manifest == proof.get('members_manifest_sha256'),
              'members': len(state.order) == proof.get('members')}
    return {'verified': all(checks.values()), 'checks': checks, 'package_sha256': state.sha256,
            'members_manifest_sha256': manifest, 'members': len(state.order),
            'basis': 'external_bytes_equal_backend_saved_package' if all(checks.values()) else 'mismatch'}


def prove_saved(verified: PackageState, saved: PackageState, profile: Profile) -> dict[str, Any]:
    """A saved file must equal the last verified in-memory state under profile allowances only."""
    counts = compare(verified, saved, Expectation({}), profile)
    return {'basis': 'saved_package_equals_verified_private_copy', 'verified_package_sha256': verified.sha256,
            'saved_package_sha256': saved.sha256, 'saved_size_bytes': saved.size_bytes,
            'members': len(saved.order), **counts}


# Why a recorded private copy is the in-memory state a public save is proved against:
# - verified_edit: the after copy of a verified picture edit;
# - before_copy: the before copy of a picture edit (earlier unsaved edits included);
# - history_match: a declared picture Undo/Redo reached its recorded state;
# - history_not_applied: a first exact-False step left the recorded origin state;
# - history_mismatch_reached: a declared step reached a DIFFERENT state, reported as
#   a named mismatch with history stopped; that reached state is what a save writes;
# - save_fresh_copy: a copy taken by the save itself, bound by state_digest to the
#   last recorded package after only non-content commands staled it.
BASELINES = ('verified_edit', 'before_copy', 'history_match', 'history_not_applied',
             'history_mismatch_reached', 'save_fresh_copy')
SAVE_EVIDENCE = ('materialization', 'container_volatile', 'volatile_values')
EDIT_EVIDENCE = ('materialization', 'bridge', 'export_native_dirty', 'container_volatile', 'volatile_values', 'caret')


def edit_evidence(op: str, profile: Profile, counts: Mapping[str, int], *, reflow: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    used = list(EDIT_EVIDENCE)
    if counts.get('regenerated_not_compared'):
        used.append('regenerated_members')
    if reflow is not None and reflow.get('changed_lines'):
        used.append('line_reflow')
    if op == 'picture_replace_exact':
        used.append('replacement_manifest_attributes')
    if profile.history(op) is not None:
        used.append('history')
    return tuple(used)


def public_proof(profile: Profile, before: PackageState, after: PackageState, link: Bridge,
                 counts: Mapping[str, int], expectation: Expectation, used: tuple[str, ...] = EDIT_EVIDENCE) -> dict[str, Any]:
    return {'strategy': STRATEGY, **profile.identity(), 'native_qualified': profile.native_qualified(used),
            'evidence_used': list(used), 'evidence_not_observed': profile.not_observed(used),
            'before_package_sha256': before.sha256, 'after_package_sha256': after.sha256,
            'members': len(after.order), **dict(counts), 'replacement': expectation.reasons.get('replacement', 'none'),
            # Derived line reflow is reported on its own, apart from the unchanged formatting.
            **({'derived_line_reflow': dict(expectation.reflow)} if expectation.reflow is not None else {}),
            'bridge': link.public(), 'epoch': dict(after.epoch),
            'legacy_hwpml_bindata_size': 'unknown_unqualified_not_compared',
            'private_copy_is_user_save': False, 'logical_debt_discharged': False}


def private_copy_path(root: Path, epoch: Mapping[str, Any], label: str) -> Path:
    if label not in ('before', 'after', 'history', 'save'):
        raise ValueError('private copy label')
    folder = Path(root) / 'private-proof' / f"{int(epoch['sequence']):06d}-{epoch['nonce']}"
    return folder / f"{int(epoch['index']):02d}-{label}.hwpx"


def is_canonical_member(name: str) -> bool:
    return bool(name) and not name.startswith('/') and '..' not in PurePosixPath(name).parts
