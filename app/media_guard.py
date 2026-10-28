"""Picture and embedded-media inventory for checking a generic native action (issue #3).

`hwpx action <name>` runs one Hancom action by name. A deletion whose live
selection covers only caption text can still remove the adjacent inline picture
and its BinData entry while the action reports success. The service reads the
whole-document HWPML2X before and after the action and compares the inventory
built here:

* picture and OLE controls in BODY (``PICTURE``, ``OLE``);
* every ``BinItem`` reference, by element;
* the BINDATALIST entries;
* the BINDATASTORAGE payloads, bound by their text and attributes. The stored
  ``Size`` attribute is ignored because native readbacks report different
  values for the same unchanged payload.

Pure functions; anything that cannot be read raises MediaGuardError.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any

MEDIA_CONTROL_TAGS = ('PICTURE', 'OLE')
REFERENCE_ATTR = 'binitem'
UNSTABLE_STORAGE_ATTRS = frozenset({'size'})
MAX_READBACK_CHARS = 16 * 1024 * 1024
_DOCTYPE = re.compile(r'<!\s*(?:DOCTYPE|ENTITY)', re.IGNORECASE)


class MediaGuardError(ValueError):
    """The readback cannot be turned into a media inventory."""


def _name(element: ET.Element) -> str:
    return element.tag.rsplit('}', 1)[-1].upper()


def _attrs(element: ET.Element, *, skip: frozenset[str] = frozenset()) -> tuple[tuple[str, str], ...]:
    pairs = ((key.rsplit('}', 1)[-1].lower(), value) for key, value in element.attrib.items())
    return tuple(sorted((key, value) for key, value in pairs if key not in skip))


def media_inventory(xml_text: Any) -> dict[str, Any]:
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise MediaGuardError('document readback returned no HWPML text')
    if len(xml_text) > MAX_READBACK_CHARS:
        raise MediaGuardError(f'document readback is {len(xml_text)} characters, above the {MAX_READBACK_CHARS} limit')
    if _DOCTYPE.search(xml_text):
        raise MediaGuardError('document readback declares a DOCTYPE or ENTITY')
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise MediaGuardError(f'document readback is not well-formed HWPML: {exc}') from exc
    body = next((element for element in root.iter() if _name(element) == 'BODY'), None)
    if body is None:
        raise MediaGuardError('document readback has no BODY')

    controls = Counter(_name(element) for element in body.iter() if _name(element) in MEDIA_CONTROL_TAGS)
    references: Counter[tuple[str, str]] = Counter()
    items: list[tuple[tuple[str, str], ...]] = []
    payloads: list[tuple[tuple[tuple[str, str], ...], str]] = []
    for element in root.iter():
        name = _name(element)
        for key, value in element.attrib.items():
            if key.rsplit('}', 1)[-1].lower() == REFERENCE_ATTR:
                references[(name, value.strip())] += 1
        if name == 'BINITEM':
            items.append(_attrs(element))
        elif name == 'BINDATASTORAGE':
            for stored in element:
                if _name(stored) == 'BINDATA':
                    text = stored.text or ''
                    payloads.append((_attrs(stored, skip=UNSTABLE_STORAGE_ATTRS), hashlib.sha256(text.encode('utf-8')).hexdigest()))
    return {
        'controls': {tag: controls.get(tag, 0) for tag in MEDIA_CONTROL_TAGS},
        'references': sorted(references.items()),
        'bindata_items': sorted(items),
        'bindata_payloads': sorted(payloads),
    }


def public_counts(inventory: dict[str, Any]) -> dict[str, int]:
    return {
        'picture_controls': inventory['controls']['PICTURE'],
        'ole_controls': inventory['controls']['OLE'],
        'binitem_references': sum(count for _key, count in inventory['references']),
        'bindata_items': len(inventory['bindata_items']),
        'bindata_payloads': len(inventory['bindata_payloads']),
    }


def media_change_reasons(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for tag, label in (('PICTURE', 'picture controls'), ('OLE', 'OLE controls')):
        if before['controls'][tag] != after['controls'][tag]:
            reasons.append(f'{label} {before["controls"][tag]} -> {after["controls"][tag]}')
    if before['references'] != after['references']:
        reasons.append('BinItem references changed')
    if before['bindata_items'] != after['bindata_items']:
        reasons.append(f'BinData list changed ({len(before["bindata_items"])} -> {len(after["bindata_items"])} entries)')
    if before['bindata_payloads'] != after['bindata_payloads']:
        reasons.append(f'BinData payloads changed ({len(before["bindata_payloads"])} -> {len(after["bindata_payloads"])})')
    return reasons
