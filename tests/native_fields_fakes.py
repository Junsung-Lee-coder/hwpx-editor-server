"""A document with existing named fields, for the field commands. Source behavior only; not Hancom evidence.

The raw COM object (``hwp.hwp``) follows the official desktop contract of HwpAutomation_2504 pp. 16-19:
- GetFieldList(number, option) returns the names joined by 0x02 (none after the last): number 0 plain, 1 with
  ``{{n}}`` counted from 0 per name in document order, 2 with ``{{count}}`` once per name; option 0 every field,
  1 cell fields, 2 click-here fields, 8 text box fields;
- GetFieldText(fieldlist) returns BSTR text; a bare name means its first field and ``name{{n}}`` the n-th; a missing
  field or one without text gives an empty string; a tab is 0x09;
- PutFieldText(fieldlist, textlist) is void: a bare name writes EVERY field of that name, ``name{{n}}`` only the n-th,
  and a missing field is ignored; the filled text takes the field's own character shape;
- FieldExist(field) is BOOL.
The HWPML2X the fake writes for a click-here field is a FIELDBEGIN and a FIELDEND control directly around its text, with
the attribute names Hancom 13.0.0.3457 wrote in an actual GetTextFile('HWPML2X') of four click-here fields
(tests/fixtures/native_fields): Type "Clickhere", a FieldId shared by every field (not an identity), a unique InstId on
the begin control only, and a FIELDEND carrying no InstId at all, so a begin and its end pair only by their order in the
text. The attribute values here are the fake's own. The product compares those controls only as the format model's
atoms (control kind, shape and full-attribute digest), never by attribute names.
Each call to the raw COM object is also logged, in order with the document reads, in ``FieldsHwp.calls`` as
('com', method, *args), and every control walk as ('HeadCtrl',).
``FieldsHwp.walk_fault`` makes the live control walk (HeadCtrl, Next, CtrlID) unreadable in one of three SYNTHETIC ways
the product refuses to read further: an empty CtrlID ('empty'), a CtrlID that is not text ('not_text'), or a chain that
never ends ('endless'). On Hancom 13.0.0.3457 a fill of an actual click-here field stopped at this walk
(control_enumeration_unavailable); the value the native walk returned was not retained, so none of these is that value.
"""
from __future__ import annotations

import re
from typing import Any
from xml.sax.saxutils import quoteattr

from tests.native_format_fakes import char
from tests.native_header_footer_fakes import HeaderFooterHwp

SEPARATOR = '\x02'
_NUMBERED = re.compile(r'(.*)\{\{(\d+)\}\}', re.S)


FIELD_ID = '7000001'


def field_begin_xml(name: str, inst: int, *, dirty: str = 'true') -> str:
    return (f'<FIELDBEGIN Command="Clickhere:set:0:" Dirty="{dirty}" Editable="true" FieldId="{FIELD_ID}" InstId="{inst}" Name={quoteattr(name)} '
            'Property="9" Type="Clickhere"/>')


FIELD_END_XML = f'<FIELDEND Editable="true" FieldId="{FIELD_ID}" Property="9" Type="Clickhere"/>'


def click_here(name: str, text: str = '', *, cs: dict[str, Any] | None = None, inst: int = 1, text_cs: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The atoms of one click-here field: its begin control, its text (in text_cs when given) and its end control."""
    shape = dict(cs or char())
    inner = dict(text_cs or shape)
    begin = {'ctrl': '%clk', 'xml': field_begin_xml(name, inst), 'cs': dict(shape), 'field': name}
    end = {'ctrl': 'fend', 'xml': FIELD_END_XML, 'cs': dict(shape), 'field_end': True}
    return [begin, *({'c': c, 'cs': dict(inner)} for c in text), end]


def cell_field(paragraph: dict[str, Any], name: str) -> dict[str, Any]:
    """A table cell paragraph whose cell carries a field name (a cell field, not a click-here)."""
    return {**paragraph, 'cell_field': name}


def text_box_field(paragraph: dict[str, Any], name: str) -> dict[str, Any]:
    """A text box paragraph whose text box carries a field name (a text box field, GetFieldList option 8)."""
    return {**paragraph, 'text_box_field': name}


_WHOLE_PARAGRAPH = {'cell_field': 'cell', 'text_box_field': 'text_box'}


class FieldCom:
    """The raw COM object behind the pyhwpx wrapper, for the field methods only."""

    def __init__(self, owner: 'FieldsHwp') -> None:
        self.owner = owner
        self.calls: list[tuple] = []
        self.text_end = ''
        self.put_result: Any = None
        self.put_side_effect = None
        self.overrides: dict[str, Any] = {}
        self.raises: dict[str, BaseException] = {}

    # The fields in document order.
    def fields(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def visit(paragraphs: list[dict[str, Any]]) -> None:
            for para in paragraphs:
                for key, kind in _WHOLE_PARAGRAPH.items():
                    if para.get(key) is not None:
                        found.append({'name': para[key], 'kind': kind, 'para': para})
                for index, atom in enumerate(para['atoms']):
                    if 'ctrl' in atom and atom.get('field') is not None:
                        found.append({'name': atom['field'], 'kind': 'click_here', 'para': para, 'begin': index})
                    for key in ('cells', 'drawtext', 'caption'):
                        visit(atom.get(key, []))
                    visit(atom.get('paras', []))

        visit(self.owner.body)
        return found

    @staticmethod
    def _end(field: dict[str, Any]) -> int:
        atoms = field['para']['atoms']
        return next(index for index in range(field['begin'] + 1, len(atoms)) if atoms[index].get('field_end'))

    def text_of(self, field: dict[str, Any]) -> str:
        if field['kind'] in _WHOLE_PARAGRAPH.values():
            return ''.join(atom.get('c', '') for atom in field['para']['atoms'])
        atoms = field['para']['atoms'][field['begin'] + 1:self._end(field)]
        return ''.join(atom.get('c', '') for atom in atoms)  # a control inside the field text is not text

    def _log(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        self.owner.calls.append(('com', name, *args))

    def _hook(self, name: str, *args: Any) -> Any:
        self._log(name, *args)
        if name in self.raises:
            raise self.raises[name]
        return self.overrides.get(name, _ABSENT)

    @staticmethod
    def _spec(spec: str) -> tuple[str, int | None]:
        match = _NUMBERED.fullmatch(spec)
        return (match.group(1), int(match.group(2))) if match else (spec, None)

    def _matching(self, spec: str) -> list[dict[str, Any]]:
        name, number = self._spec(spec)
        same = [field for field in self.fields() if field['name'] == name]
        if number is None:
            return same
        return same[number:number + 1]

    # Official methods.
    def GetFieldList(self, number: int = 0, option: int = 0) -> Any:  # noqa: N802
        override = self._hook('GetFieldList', number, option)
        if override is not _ABSENT:
            return override(number, option) if callable(override) else override
        return self.listed(number, option)

    def listed(self, number: int, option: int) -> str:
        """What GetFieldList answers for this document, without any override (for an override that changes one form only)."""
        kinds = {0: None, 1: 'cell', 2: 'click_here', 8: 'text_box'}
        if option not in kinds:
            raise AssertionError(f'unexpected GetFieldList option {option}')
        fields = [field for field in self.fields() if kinds[option] is None or field['kind'] == kinds[option]]
        if number == 0:
            items = [field['name'] for field in fields]
        elif number == 1:
            seen: dict[str, int] = {}
            items = []
            for field in fields:
                items.append(f"{field['name']}{{{{{seen.get(field['name'], 0)}}}}}")
                seen[field['name']] = seen.get(field['name'], 0) + 1
        elif number == 2:
            counts: dict[str, int] = {}
            for field in fields:
                counts[field['name']] = counts.get(field['name'], 0) + 1
            items = [f'{name}{{{{{count}}}}}' for name, count in counts.items()]
        else:
            raise AssertionError(f'unexpected GetFieldList number {number}')
        return SEPARATOR.join(items)

    def GetFieldText(self, fieldlist: str) -> Any:  # noqa: N802
        override = self._hook('GetFieldText', fieldlist)
        if override is not _ABSENT:
            return override(fieldlist) if callable(override) else override
        out = []
        for spec in fieldlist.split(SEPARATOR):
            name, number = self._spec(spec)
            same = [field for field in self.fields() if field['name'] == name]
            index = number or 0
            out.append((self.text_of(same[index]) if index < len(same) else '') + self.text_end)
        return ''.join(out)

    def FieldExist(self, field: str) -> Any:  # noqa: N802
        override = self._hook('FieldExist', field)
        if override is not _ABSENT:
            return override(field) if callable(override) else override
        return any(item['name'] == field for item in self.fields())

    def PutFieldText(self, fieldlist: str, textlist: str) -> Any:  # noqa: N802
        self._log('PutFieldText', fieldlist, textlist)
        if 'PutFieldText' in self.raises:
            raise self.raises['PutFieldText']
        specs, texts = fieldlist.split(SEPARATOR), textlist.split(SEPARATOR)
        assert len(specs) == len(texts), 'PutFieldText takes as many texts as fields'
        for spec, text in zip(specs, texts):
            for field in self._matching(spec):
                self._write(field, text)
        if self.put_side_effect is not None:
            self.put_side_effect(self.owner)
        return self.put_result

    def _write(self, field: dict[str, Any], text: str) -> None:
        para = field['para']
        if field['kind'] in _WHOLE_PARAGRAPH.values():
            shape = para['atoms'][0]['cs'] if para['atoms'] else char()
            para['atoms'] = [{'c': c, 'cs': dict(shape)} for c in text]
            return
        begin, end = field['begin'], self._end(field)
        shape = para['atoms'][begin]['cs']
        para['atoms'][begin + 1:end] = [{'c': c, 'cs': dict(shape)} for c in text]


_ABSENT = object()


class FieldsHwp(HeaderFooterHwp):
    """A text document whose paragraphs may hold click-here fields; its raw COM object answers the field methods."""

    def __init__(self, body: list[dict[str, Any]], *, areas: list[dict[str, Any]] | None = None, **options: Any) -> None:
        super().__init__(body, areas=list(areas or []), **options)
        self.hwp = FieldCom(self)

    walk_fault: str | None = None

    @property
    def HeadCtrl(self) -> Any:  # noqa: N802
        self.calls.append(('HeadCtrl',))
        head = super().HeadCtrl
        if self.walk_fault is None or head is None:
            return head
        controls = [head]
        while controls[-1].Next is not None:
            controls.append(controls[-1].Next)
        if self.walk_fault == 'endless':
            controls[-1].Next = head
        else:
            target = next((ctrl for ctrl in controls if ctrl.CtrlID == 'fend'), controls[-1])
            target.CtrlID = {'empty': '', 'not_text': None}[self.walk_fault]
        return head

    def field_texts(self) -> list[tuple[str, str]]:
        return [(field['name'], self.hwp.text_of(field)) for field in self.hwp.fields()]

    def puts(self) -> list[tuple]:
        return [call for call in self.hwp.calls if call[0] == 'PutFieldText']

    def calls_after_write(self) -> list[tuple]:
        """Every logged document read, control walk and COM call after the first PutFieldText."""
        index = next(number for number, call in enumerate(self.calls) if call[:2] == ('com', 'PutFieldText'))
        return self.calls[index + 1:]
