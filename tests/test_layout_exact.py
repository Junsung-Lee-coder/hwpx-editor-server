from __future__ import annotations

import unittest
from typing import Any

from app.command_packages.runtime import get_command_package_registry
from app.layout_ops import (
    HWPUNIT_PER_MM,
    LayoutError,
    body_token_reasons,
    compare_documents,
    hwpunit_to_mm,
    mm_to_hwpunit,
    normalize_request,
    parse_layout_xml,
    plan_columns,
    plan_hanging_indent,
    plan_page_setup,
    plan_section_delete,
    strict_normal,
    text_area_mm,
    verify_hanging_indent,
    verify_page_setup,
)
from app.local_cli_layout import LocalCliLayoutMixin
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service_support import LocalCliMutationError

A4 = {'PaperWidth': 59528, 'PaperHeight': 84188, 'Landscape': 0, 'GutterType': 0, 'TopMargin': 5668, 'BottomMargin': 4252,
      'LeftMargin': 8504, 'RightMargin': 8504, 'HeaderLen': 4252, 'FooterLen': 4252, 'GutterLen': 0}
COLDEF = {'Count': 1, 'SameSize': 1, 'SameGap': 0, 'Type': 0, 'Layout': 0, 'LineType': 0, 'LineWidth': 0, 'LineColor': 0}
SHAPE = {'AlignType': 0, 'LineSpacingType': 0, 'LineSpacing': 160, 'Indentation': 0, 'LeftMargin': 0, 'RightMargin': 0,
         'PrevSpacing': 0, 'NextSpacing': 0, 'KeepWithNext': 0}
_MARGIN_ATTRS = {'LeftMargin': 'Left', 'RightMargin': 'Right', 'TopMargin': 'Top', 'BottomMargin': 'Bottom',
                 'HeaderLen': 'Header', 'FooterLen': 'Footer', 'GutterLen': 'Gutter'}


class _Error(Exception):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


# ------------------------------------------------------------------ fake Hancom


class _HSet:
    def __init__(self) -> None:
        self.items: dict[str, Any] = {}

    def SetItem(self, name: str, value: Any) -> None:  # noqa: N802 - Hancom API name
        self.items[name] = value

    def Item(self, name: str) -> Any:  # noqa: N802
        return self.items.get(name)


class _Obj:
    """Attribute bag standing in for a parameter set (or its PageDef sub-set)."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _PSets:
    def __init__(self) -> None:
        self.HSecDef = _Obj(HSet=_HSet(), PageDef=_Obj())
        self.HColDef = _Obj(HSet=_HSet())
        self.HParaShape = _Obj(HSet=_HSet())


class _FakeHAction:
    def __init__(self, hwp: _FakeLayoutHwp) -> None:
        self.hwp = hwp

    def GetDefault(self, name: str, hset: _HSet) -> bool:  # noqa: N802
        return self.hwp.get_default(name, hset)

    def Execute(self, name: str, hset: _HSet) -> bool:  # noqa: N802
        return self.hwp.drift_after(name, self.hwp.execute(name, hset))

    def Run(self, name: str) -> bool:  # noqa: N802
        return self.hwp.drift_after(name, self.hwp.run(name))


class _FakeLayoutHwp:
    """Fake pyhwpx Hwp over a sectioned document.

    Paragraphs are numbered document-wide in list 0 (``para`` in get_pos).
    Fault knobs: ``raising`` action names raise; ``noop`` names report success
    but change nothing; ``extra_page_change`` makes PageSetup also move
    RightMargin; ``page_applies_to_all`` ignores ApplyTo 2; ``coldef_ignores_gap``
    drops SameGap; ``break_eats_char`` makes BreakSection drop a character;
    ``indent_model`` ('indent_only' | 'margin_shift' | 'positive' | 'shape_drift');
    ``snapshot_unknown``; ``selected`` leaves a selection.
    """

    MUTATING = {'PageSetup', 'MultiColumn', 'BreakSection', 'DeleteBack', 'ParagraphShapeIndentAtCaret'}

    def __init__(self, sections: list[list[str]] | None = None, **faults: Any) -> None:
        sections = sections or [['1. first item that wraps', 'second']]
        self.sections = [
            {'pagedef': dict(A4), 'coldefs': [dict(COLDEF)], 'paras': [{'text': text, 'shape': dict(SHAPE)} for text in paras]}
            for paras in sections
        ]
        self.caret = (0, 0, 0)
        self.selection: tuple[int, int, int, int] | None = None
        self.log: list[str] = []
        self.raising: set[str] = set(faults.get('raising', ()))
        self.noop: set[str] = set(faults.get('noop', ()))
        self.faults = faults
        self.snapshot_unknown = bool(faults.get('snapshot_unknown'))
        self.in_cell = bool(faults.get('in_cell'))
        self.executed_apply_to: list[Any] = []
        self.HParameterSet = _PSets()
        self.HAction = _FakeHAction(self)

    # --- document geometry
    def _flat(self) -> list[tuple[int, dict[str, Any]]]:
        return [(index, para) for index, section in enumerate(self.sections) for para in section['paras']]

    def _section_of(self, para: int) -> int:
        return self._flat()[para][0]

    def _para(self, para: int) -> dict[str, Any]:
        return self._flat()[para][1]

    # --- caret / selection
    def get_pos(self) -> tuple[int, int, int]:
        return self.caret

    def set_pos(self, list_id: int, para: int, pos: int) -> bool:
        self.caret = (list_id, para, pos)
        self.selection = None
        return True

    def select_text(self, spara: int, spos: int, epara: int, epos: int, slist: int = 0) -> bool:
        self.selection = (spara, spos, epara, epos)
        return True

    def get_selected_text(self, keep_select: bool = True) -> str:
        assert self.selection is not None
        spara, spos, _epara, epos = self.selection
        text = self._para(spara)['text']
        result = text[spos:] + '\r\n' if epos == -1 else text[spos:epos]
        if not keep_select:
            self.selection = None
        return result

    def KeyIndicator(self) -> tuple[Any, ...]:  # noqa: N802
        return (True, len(self.sections), self._section_of(self.caret[1]) + 1, 1, 1, 1, 1, False, '')

    # --- readback
    def GetTextFile(self, fmt: str, option: str) -> str:  # noqa: N802
        assert (fmt, option) == ('HWPML2X', '')
        out = []
        for section in self.sections:
            pagedef = section['pagedef']
            margin = ' '.join(f'{attr}="{pagedef[item]}"' for item, attr in _MARGIN_ATTRS.items())
            head = (f'<SECDEF><PAGEDEF Landscape="{pagedef["Landscape"]}" Width="{pagedef["PaperWidth"]}" '
                    f'Height="{pagedef["PaperHeight"]}" GutterType="{pagedef["GutterType"]}"><PAGEMARGIN {margin}/></PAGEDEF></SECDEF>')
            paras = []
            for index, para in enumerate(section['paras']):
                controls = head if index == 0 else ''
                if index == 0:
                    controls += ''.join(f'<COLDEF Count="{c["Count"]}" SameSize="{c["SameSize"]}" SameGap="{c["SameGap"]}"/>' for c in section['coldefs'])
                shape_id = abs(hash(tuple(sorted(para['shape'].items())))) % 10**6
                paras.append(f'<P ParaShape="{shape_id}" Style="{para.get("style", "0")}"><TEXT>{controls}<CHAR>{para["text"]}</CHAR></TEXT></P>')
            out.append(f'<SECTION>{"".join(paras)}</SECTION>')
        return f'<?xml version="1.0" encoding="UTF-16" standalone="no"?><HWPML><HEAD/><BODY>{"".join(out)}</BODY></HWPML>'

    # --- actions
    def get_default(self, name: str, hset: _HSet) -> bool:
        self.log.append(f'GetDefault({name})')
        hset.items.clear()
        psets = self.HParameterSet
        section = self.sections[self._section_of(self.caret[1])]
        if name == 'PageSetup':
            assert hset is psets.HSecDef.HSet
            psets.HSecDef.PageDef.__dict__.update(section['pagedef'])
        elif name == 'MultiColumn':
            assert hset is psets.HColDef.HSet
            psets.HColDef.__dict__.update(section['coldefs'][-1])
        elif name == 'ParagraphShape':
            assert hset is psets.HParaShape.HSet
            psets.HParaShape.__dict__.update(self._para(self.caret[1])['shape'])
        return True

    def execute(self, name: str, hset: _HSet) -> bool:
        self.log.append(name)
        self.executed_apply_to.append(hset.items.get('ApplyTo'))
        if name in self.raising:
            raise RuntimeError(f'COM error in {name}')
        if name in self.noop:
            return True
        psets = self.HParameterSet
        section_index = self._section_of(self.caret[1])
        apply_to = hset.items.get('ApplyTo')
        if name == 'PageSetup':
            values = {item: getattr(psets.HSecDef.PageDef, item) for item in A4}
            if self.faults.get('extra_page_change'):
                values['RightMargin'] += 100
            targets = self.sections if apply_to == 3 or self.faults.get('page_applies_to_all') else [self.sections[section_index]]
            for section in targets:
                section['pagedef'] = dict(values)
            return True
        if name == 'MultiColumn':
            values = {item: getattr(psets.HColDef, item) for item in COLDEF}
            if self.faults.get('coldef_ignores_gap'):
                values['SameGap'] = self.sections[section_index]['coldefs'][-1]['SameGap']
            targets = self.sections if self.faults.get('coldef_applies_to_all') else [self.sections[section_index]]
            for section in targets:
                coldefs = section['coldefs']
                if apply_to == 6:
                    coldefs.append(dict(values))
                else:
                    coldefs[-1] = dict(values)
            return True
        raise AssertionError(f'unexpected Execute({name})')

    def run(self, name: str) -> bool:
        self.log.append(name)
        if name in self.raising:
            raise RuntimeError(f'COM error in {name}')
        if name in self.noop:
            return True
        _list, para, pos = self.caret
        section_index = self._section_of(para)
        section = self.sections[section_index]
        first = sum(len(s['paras']) for s in self.sections[:section_index])
        local = para - first
        if name == 'BreakSection':
            current = section['paras'][local]
            head, tail = current['text'][:pos], current['text'][pos:]
            if self.faults.get('break_eats_char'):
                tail = tail[1:]
            current['text'] = head
            moved = [{'text': tail, 'shape': dict(current['shape'])}] + section['paras'][local + 1:]
            section['paras'] = section['paras'][:local + 1]
            new = {'pagedef': dict(section['pagedef']), 'coldefs': [dict(section['coldefs'][-1])], 'paras': moved}
            self.sections.insert(section_index + 1, new)
            self.caret = (0, para + 1, 0)
            return True
        if name == 'DeleteBack':
            if local == 0 and pos == 0 and section_index > 0:
                previous = self.sections[section_index - 1]
                last = previous['paras'][-1]
                self.caret = (0, para - 1, len(last['text']))
                last['text'] += section['paras'][0]['text']
                previous['paras'].extend(section['paras'][1:])
                del self.sections[section_index]
            elif pos > 0:
                current = section['paras'][local]
                current['text'] = current['text'][:pos - 1] + current['text'][pos:]
                self.caret = (0, para, pos - 1)
            return True
        if name == 'ParagraphShapeIndentAtCaret':
            shape = section['paras'][local]['shape']
            amount = pos * 1000
            model = self.faults.get('indent_model', 'indent_only')
            if model == 'positive':
                shape['Indentation'] = amount
            else:
                shape['Indentation'] = -amount
            if model == 'margin_shift':
                shape['LeftMargin'] += amount
            if model == 'shape_drift':
                shape['LineSpacing'] += 10
            if self.faults.get('indent_moves_caret'):
                self.caret = (0, para + 1, 0)
            return True
        if name == 'MoveLeft':
            if pos > 0:
                self.caret = (0, para, pos - 1)
            elif para > 0:
                self.caret = (0, para - 1, len(self._para(para - 1)['text']))
            return True
        if name == 'Cancel':
            self.selection = None
            return True
        raise AssertionError(f'unexpected Run({name})')

    def drift_after(self, name: str, result: bool) -> bool:
        """``drift`` fault: a mutating action also restyles the last paragraph away from the caret.

        Only the body-token comparison sees a paragraph Style change (text,
        controls and ParaShape ids stay the same).
        """
        if self.faults.get('drift') and name in self.MUTATING:
            flat = self._flat()
            index = next(i for i in range(len(flat) - 1, -1, -1) if i != self.caret[1])
            flat[index][1]['style'] = '9'
        return result

    def mutations(self) -> list[str]:
        return [entry for entry in self.log if entry in self.MUTATING]


class _Service(LocalCliLayoutMixin):
    def __init__(self, hwp: _FakeLayoutHwp) -> None:
        self.hwp = hwp

    def _bundle_compact_snapshot(self, _hwp: Any) -> dict[str, Any]:
        if self.hwp.snapshot_unknown:
            return {'is_cell': False, 'selection_mode': None}
        return {'pos': self.hwp.caret, 'is_cell': self.hwp.in_cell, 'has_selection': self.hwp.selection is not None, 'selection_mode': 0}


def _page_step(**overrides: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        'op': 'layout_exact', 'kind': 'page_setup', 'expected_pos': [0, 0, 0], 'confirm_layout': True,
        'margin_left_mm': 20.0, 'expected_before': {'margin_left_mm': 30.0},
    }
    step.update(overrides)
    return step


def _run(hwp: _FakeLayoutHwp, step: dict[str, Any]) -> dict[str, Any]:
    return _Service(hwp)._bundle_layout_exact(hwp, step)


# ------------------------------------------------------------------ pure tests


class UnitTests(unittest.TestCase):
    def test_mm_conversion(self) -> None:
        self.assertAlmostEqual(HWPUNIT_PER_MM, 283.4645669, places=6)
        self.assertEqual(mm_to_hwpunit(210), 59528)
        self.assertEqual(mm_to_hwpunit(297), 84189)
        self.assertEqual(mm_to_hwpunit(30), 8504)
        self.assertAlmostEqual(hwpunit_to_mm(8504), 30.0, places=2)

    def test_text_area_uses_orientation_and_gutter(self) -> None:
        width, height = text_area_mm(A4)
        self.assertAlmostEqual(width, 150.0, places=1)
        landscape = {**A4, 'Landscape': 1}
        self.assertGreater(text_area_mm(landscape)[0], width)
        self.assertLess(text_area_mm({**A4, 'GutterType': 2, 'GutterLen': 2835})[1], height)

    def test_strict_normal(self) -> None:
        self.assertTrue(strict_normal({'has_selection': False, 'selection_mode': 0}))
        for snapshot in ({'has_selection': None, 'selection_mode': 0}, {'has_selection': False, 'selection_mode': None},
                         {'has_selection': False, 'selection_mode': False}, {'has_selection': False, 'selection_mode': 1},
                         {'error': 'x'}):
            self.assertFalse(strict_normal(snapshot))


class RequestTests(unittest.TestCase):
    def test_valid_kinds(self) -> None:
        self.assertEqual(normalize_request(_page_step())['targets'], {'margin_left_mm': 20.0})
        cols = normalize_request({'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2, 'gap_mm': 8})
        self.assertEqual((cols['same_width'], cols['apply_to']), (True, 'current_section'))
        hang = normalize_request({'kind': 'hanging_indent', 'expected_pos': [0, 1, 0], 'confirm_layout': True, 'marker_text': '1. '})
        self.assertEqual(hang['marker_text'], '1. ')

    def test_rejections(self) -> None:
        cases = [
            (_page_step(kind='nope'), 'kind must be'),
            (_page_step(confirm_layout=None), 'confirm_layout'),
            (_page_step(expected_pos=[0, 0]), 'expected_pos'),
            (_page_step(expected_pos=[0, True, 0]), 'expected_pos'),
            (_page_step(expected_before=None), 'expected_before'),
            (_page_step(expected_before={'margin_top_mm': 1}), 'missing: margin_left_mm'),
            (_page_step(margin_left_mm=301), '0..300'),
            (_page_step(paper_width_mm=5, expected_before={'margin_left_mm': 30, 'paper_width_mm': 210}), '10..1000'),
            (_page_step(landscape=1, expected_before={'margin_left_mm': 30, 'landscape': False}), 'landscape'),
            (_page_step(apply_to='from_caret_new'), 'apply_to'),
            (_page_step(count=2), 'does not take: count'),
            ({'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 11}, '1..10'),
            ({'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 1, 'gap_mm': 5}, 'count >= 2'),
            ({'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2, 'gap_mm': 5, 'same_width': False}, 'same_width'),
            ({'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2, 'margin_left_mm': 5}, 'does not take'),
            ({'kind': 'section_insert', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'marker_text': 'x'}, 'does not take'),
            ({'kind': 'section_delete', 'expected_pos': [0, 3, 0], 'confirm_layout': True, 'section_index': 1}, '>= 2'),
            ({'kind': 'section_delete', 'expected_pos': [0, 3, 2], 'confirm_layout': True, 'section_index': 2}, 'pos 0'),
            ({'kind': 'hanging_indent', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'marker_text': ''}, '1..40'),
            ({'kind': 'hanging_indent', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'marker_text': 'x' * 41}, '1..40'),
            ({'kind': 'hanging_indent', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'marker_text': 'a\n'}, 'line breaks'),
        ]
        for step, pattern in cases:
            with self.subTest(step=step), self.assertRaisesRegex(LayoutError, pattern):
                normalize_request(step)


class ReadbackTests(unittest.TestCase):
    def test_parse_sections_pagedefs_coldefs_text(self) -> None:
        doc = parse_layout_xml(_FakeLayoutHwp([['a b', 'c'], ['d']]).GetTextFile('HWPML2X', ''))
        self.assertEqual((doc['section_count'], doc['secdef_count'], doc['section_elements']), (2, 2, 2))
        self.assertEqual(len(doc['pagedefs']), 2)
        self.assertEqual(len(doc['coldefs']), 2)
        self.assertEqual(doc['text'], 'a bcd')
        self.assertEqual(doc['controls'], {})
        self.assertEqual(doc['paragraph_count'], 3)

    def test_paragraph_split_keeps_text(self) -> None:
        one = parse_layout_xml('<HWPML><BODY><SECTION><P><TEXT><CHAR>ab cd</CHAR></TEXT></P></SECTION></BODY></HWPML>')
        two = parse_layout_xml('<HWPML><BODY><SECTION><P><TEXT><CHAR>ab </CHAR></TEXT></P><P><TEXT><CHAR>cd</CHAR></TEXT></P></SECTION></BODY></HWPML>')
        self.assertEqual(one['text'], two['text'])
        self.assertIsNone(one['pagedefs'])

    def test_rejects_bad_readback(self) -> None:
        for xml in (None, '', '<HWPML', '<HWPML/>', '<HWPML><BODY/></HWPML>'):
            with self.subTest(xml=xml), self.assertRaises(LayoutError):
                parse_layout_xml(xml)

    def test_compare_documents_guards_head_tail_and_embedded_objects(self) -> None:
        def doc(head: str = '<HEAD SecCnt="1"><CHARSHAPELIST Count="1"><CHARSHAPE Id="0" Height="1000"/></CHARSHAPELIST></HEAD>',
                picture: str = '<PICTURE><IMAGE BinItem="1"/></PICTURE>', tail: str = '<TAIL><BINDATASTORAGE>QUJD</BINDATASTORAGE></TAIL>',
                shape: str = '1') -> dict[str, Any]:
            return parse_layout_xml(f'<HWPML>{head}<BODY><SECTION><SECDEF/><P ParaShape="{shape}"><TEXT><CHAR>x</CHAR>{picture}</TEXT></P></SECTION></BODY>{tail}</HWPML>')
        base = doc()
        grown = doc(head='<HEAD SecCnt="1"><CHARSHAPELIST Count="2"><CHARSHAPE Id="0" Height="1000"/><CHARSHAPE Id="1" Height="9"/></CHARSHAPELIST></HEAD>')
        self.assertEqual(compare_documents(base, grown), [])
        self.assertEqual(compare_documents(base, doc(shape='2'), paragraphs='one_shape'), [])
        cases = (
            (doc(head='<HEAD SecCnt="1"><CHARSHAPELIST Count="1"><CHARSHAPE Id="0" Height="1200"/></CHARSHAPELIST></HEAD>'), 'existing HEAD entry changed'),
            (doc(tail='<TAIL/>'), 'TAIL changed'),
            (doc(picture='<PICTURE><IMAGE BinItem="2"/></PICTURE>'), 'embedded object'),
            (doc(head='<HEAD SecCnt="2"><CHARSHAPELIST Count="1"><CHARSHAPE Id="0" Height="1000"/></CHARSHAPELIST></HEAD>'), 'SecCnt'),
        )
        for after, reason in cases:
            with self.subTest(reason=reason):
                self.assertIn(reason, ' '.join(compare_documents(base, after)))

    def test_column_sections_need_provable_secdef_order(self) -> None:
        good = parse_layout_xml('<HWPML><BODY><SECTION><P><TEXT><SECDEF/><COLDEF Count="1"/><CHAR>a</CHAR></TEXT></P></SECTION>'
                                '<SECTION><P><TEXT><SECDEF/><COLDEF Count="2"/><CHAR>b</CHAR></TEXT></P></SECTION></BODY></HWPML>')
        self.assertEqual(len(good['coldefs_by_section']), 2)
        for body in (
            '<SECTION><P><TEXT><COLDEF Count="1"/><SECDEF/><CHAR>a</CHAR></TEXT></P></SECTION><SECTION><P><TEXT><SECDEF/><CHAR>b</CHAR></TEXT></P></SECTION>',
            '<SECTION><P><TEXT><SECDEF/><COLDEF/><SECDEF/><COLDEF/><CHAR>a</CHAR></TEXT></P></SECTION>',
        ):
            with self.subTest(body=body):
                doc = parse_layout_xml(f'<HWPML><BODY>{body}</BODY></HWPML>')
                self.assertIsNone(doc['coldefs_by_section'])
                request = normalize_request({'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2})
                with self.assertRaisesRegex(LayoutError, 'cannot be proven'):
                    plan_columns(request, COLDEF, doc)

    def test_body_tokens_allow_only_the_requested_change(self) -> None:
        def doc(*, secdef: str = '<SECDEF TextDirection="0"><PAGEDEF Width="100"/></SECDEF>', coldef: str = '<COLDEF Count="1"/>',
                p1: str = 'ParaShape="1" Style="0"', run: str = '<TEXT CharShape="0"><CHAR>a  b</CHAR><PICTURE Id="p"/><CHAR>c</CHAR></TEXT>',
                note: str = '<FOOTNOTE Number="1"><PARALIST><P><TEXT><CHAR>n</CHAR></TEXT></P></PARALIST></FOOTNOTE>') -> dict[str, Any]:
            return parse_layout_xml(f'<HWPML><HEAD/><BODY><SECTION><P ParaShape="1"><TEXT>{secdef}{coldef}<CHAR>x</CHAR></TEXT></P>'
                                    f'<P {p1}>{run}</P><P ParaShape="1"><TEXT><CHAR>y</CHAR>{note}</TEXT></P></SECTION></BODY></HWPML>')
        base = doc()
        allowed = (
            ('page_setup', doc(secdef='<SECDEF TextDirection="0"><PAGEDEF Width="200"/></SECDEF>')),
            ('columns', doc(coldef='<COLDEF Count="2"/>')),
            ('columns', doc(coldef='<COLDEF Count="1"/><COLDEF Count="2"/>')),
            ('hanging_indent', doc(p1='ParaShape="9" Style="0"')),
        )
        for rule, after in allowed:
            with self.subTest(allowed=rule):
                self.assertEqual(body_token_reasons(rule, base, after), [])
        unrequested = (
            ('whitespace count', doc(run='<TEXT CharShape="0"><CHAR>a b</CHAR><PICTURE Id="p"/><CHAR>c</CHAR></TEXT>')),
            ('existing run CharShape', doc(run='<TEXT CharShape="5"><CHAR>a  b</CHAR><PICTURE Id="p"/><CHAR>c</CHAR></TEXT>')),
            ('paragraph Style', doc(p1='ParaShape="1" Style="3"')),
            ('paragraph PageBreak', doc(p1='ParaShape="1" Style="0" PageBreak="true"')),
            ('picture position', doc(run='<TEXT CharShape="0"><CHAR>a</CHAR><PICTURE Id="p"/><CHAR>  bc</CHAR></TEXT>')),
            ('SECDEF attribute', doc(secdef='<SECDEF TextDirection="1"><PAGEDEF Width="100"/></SECDEF>')),
            ('footnote attribute', doc(note='<FOOTNOTE Number="1" NumberShape="2"><PARALIST><P><TEXT><CHAR>n</CHAR></TEXT></P></PARALIST></FOOTNOTE>')),
            ('paragraph split', doc(run='<TEXT CharShape="0"><CHAR>a </CHAR></TEXT></P><P ParaShape="1" Style="0"><TEXT CharShape="0"><CHAR> b</CHAR><PICTURE Id="p"/><CHAR>c</CHAR></TEXT>')),
        )
        for label, after in unrequested:
            for rule in ('page_setup', 'columns', 'hanging_indent'):
                with self.subTest(label=label, rule=rule):
                    self.assertTrue(body_token_reasons(rule, base, after))
        with self.subTest('hanging_indent plus Style'):
            self.assertTrue(body_token_reasons('hanging_indent', base, doc(p1='ParaShape="9" Style="3"')))

    def test_section_break_tokens(self) -> None:
        def section(body: str, secdef: str = '<SECDEF><PAGEDEF Width="1"/></SECDEF><COLDEF Count="1"/>') -> str:
            return f'<SECTION><P ParaShape="1"><TEXT>{secdef}{body}</TEXT></P></SECTION>'
        one = parse_layout_xml(f'<HWPML><BODY>{section("<CHAR>ab</CHAR>")}</BODY></HWPML>')
        two = parse_layout_xml(f'<HWPML><BODY>{section("<CHAR>a</CHAR>")}{section("<CHAR>b</CHAR>")}</BODY></HWPML>')
        self.assertEqual(body_token_reasons('section_insert', one, two), [])
        self.assertEqual(body_token_reasons('section_delete', two, one), [])
        second = section('<CHAR>b</CHAR>').replace('ParaShape="1"', 'ParaShape="1" PageBreak="true"')
        restyled = parse_layout_xml('<HWPML><BODY>' + section('<CHAR>a</CHAR>') + second + '</BODY></HWPML>')
        self.assertTrue(body_token_reasons('section_insert', one, restyled))
        eaten = parse_layout_xml(f'<HWPML><BODY>{section("<CHAR>a</CHAR>")}{section("<CHAR>c</CHAR>")}</BODY></HWPML>')
        self.assertTrue(body_token_reasons('section_insert', one, eaten))
        extra = parse_layout_xml('<HWPML><BODY>' + section('<CHAR>a</CHAR>') + section('<BOOKMARK Name="x"/><CHAR>b</CHAR>') + '</BODY></HWPML>')
        self.assertTrue(body_token_reasons('section_insert', one, extra))

    def test_compare_documents_detects_controls_and_shapes(self) -> None:
        base = parse_layout_xml('<HWPML><BODY><SECTION><P ParaShape="1"><TEXT><CHAR>x</CHAR></TEXT></P><P ParaShape="1"><TEXT><CHAR>y</CHAR></TEXT></P></SECTION></BODY></HWPML>')
        table = parse_layout_xml('<HWPML><BODY><SECTION><P ParaShape="1"><TEXT><CHAR>x</CHAR><TABLE/></TEXT></P><P ParaShape="1"><TEXT><CHAR>y</CHAR></TEXT></P></SECTION></BODY></HWPML>')
        shapes = parse_layout_xml('<HWPML><BODY><SECTION><P ParaShape="2"><TEXT><CHAR>x</CHAR></TEXT></P><P ParaShape="3"><TEXT><CHAR>y</CHAR></TEXT></P></SECTION></BODY></HWPML>')
        self.assertEqual(compare_documents(base, base), [])
        self.assertTrue(any('controls' in reason for reason in compare_documents(base, table)))
        self.assertTrue(compare_documents(base, shapes, paragraphs='one_shape'))
        self.assertEqual(compare_documents(base, shapes, paragraphs='free'), [])


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.doc = parse_layout_xml(_FakeLayoutHwp().GetTextFile('HWPML2X', ''))
        self.two = parse_layout_xml(_FakeLayoutHwp([['a'], ['b']]).GetTextFile('HWPML2X', ''))

    def test_page_plan_and_stale_view(self) -> None:
        plan = plan_page_setup(normalize_request(_page_step()), A4, self.doc)
        self.assertEqual(plan['targets'], {'LeftMargin': mm_to_hwpunit(20)})
        with self.assertRaisesRegex(LayoutError, 'changed since it was probed'):
            plan_page_setup(normalize_request(_page_step(expected_before={'margin_left_mm': 25})), A4, self.doc)
        with self.assertRaisesRegex(LayoutError, 'nothing to change'):
            plan_page_setup(normalize_request(_page_step(margin_left_mm=30.05)), A4, self.doc)
        with self.assertRaisesRegex(LayoutError, 'text area'):
            plan_page_setup(normalize_request(_page_step(margin_left_mm=200, margin_right_mm=30,
                                                         expected_before={'margin_left_mm': 30, 'margin_right_mm': 30})), A4, self.doc)
        with self.assertRaisesRegex(LayoutError, 'no numeric'):
            plan_page_setup(normalize_request(_page_step()), {**A4, 'GutterType': None}, self.doc)

    def test_page_plan_multi_section_needs_pagedefs(self) -> None:
        no_pagedefs = {**self.two, 'pagedefs': None}
        with self.assertRaisesRegex(LayoutError, 'PAGEDEF per section'):
            plan_page_setup(normalize_request(_page_step()), A4, no_pagedefs)
        mixed = {**self.two, 'pagedefs': ['a', 'b']}
        with self.assertRaisesRegex(LayoutError, 'different page setups'):
            plan_page_setup(normalize_request(_page_step(apply_to='whole_document')), A4, mixed)

    def test_page_verify_requires_exact_fields(self) -> None:
        plan = plan_page_setup(normalize_request(_page_step()), A4, self.doc)
        good = {**A4, 'LeftMargin': mm_to_hwpunit(20)}
        doc_after = {**self.doc, 'pagedefs': ['changed']}
        self.assertTrue(verify_page_setup(plan, good, self.doc, doc_after, caret_section=1)['ok'])
        self.assertFalse(verify_page_setup(plan, {**good, 'LeftMargin': mm_to_hwpunit(20.2)}, self.doc, doc_after, caret_section=1)['ok'])
        self.assertFalse(verify_page_setup(plan, {**good, 'TopMargin': 1}, self.doc, doc_after, caret_section=1)['ok'])
        self.assertFalse(verify_page_setup(plan, good, self.doc, self.doc, caret_section=1)['ok'])

    def test_column_plan(self) -> None:
        request = normalize_request({'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2, 'gap_mm': 8,
                                     'expected_before': {'count': 1}})
        self.assertEqual(plan_columns(request, COLDEF, self.doc)['targets'], {'Count': 2, 'SameSize': 1, 'SameGap': mm_to_hwpunit(8)})
        with self.assertRaisesRegex(LayoutError, 'since it was probed'):
            plan_columns(request, {**COLDEF, 'Count': 3}, self.doc)
        same = normalize_request({'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 1})
        with self.assertRaisesRegex(LayoutError, 'nothing to change'):
            plan_columns(same, COLDEF, self.doc)

    def test_section_delete_plan(self) -> None:
        request = normalize_request({'kind': 'section_delete', 'expected_pos': [0, 1, 0], 'confirm_layout': True, 'section_index': 3})
        with self.assertRaisesRegex(LayoutError, 'past the last'):
            plan_section_delete(request, self.two, key_indicator_sections=2)
        request['section_index'] = 2
        with self.assertRaisesRegex(LayoutError, 'KeyIndicator reports'):
            plan_section_delete(request, self.two, key_indicator_sections=3)
        with self.assertRaisesRegex(LayoutError, 'single section'):
            plan_section_delete(request, self.doc, key_indicator_sections=None)

    def test_hanging_plan_and_verify(self) -> None:
        request = normalize_request({'kind': 'hanging_indent', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'marker_text': '1. '})
        with self.assertRaisesRegex(LayoutError, 'does not start with'):
            plan_hanging_indent(request, '2. x', SHAPE)
        plan = plan_hanging_indent(request, '1. x\r\n', SHAPE)
        self.assertEqual(plan['marker_pos'], (0, 0, 3))
        ok = verify_hanging_indent(plan, {**SHAPE, 'Indentation': -3000}, 'a', 'a', self.doc, self.doc)
        self.assertEqual((ok['ok'], ok['model']), (True, 'indent_only'))
        shift = verify_hanging_indent(plan, {**SHAPE, 'Indentation': -3000, 'LeftMargin': 3000}, 'a', 'a', self.doc, self.doc)
        self.assertEqual((shift['ok'], shift['model']), (True, 'margin_shift'))
        for after in ({**SHAPE, 'Indentation': 3000}, {**SHAPE, 'Indentation': -3000, 'LeftMargin': 10},
                      {**SHAPE, 'Indentation': -3000, 'AlignType': 1}):
            self.assertFalse(verify_hanging_indent(plan, after, 'a', 'a', self.doc, self.doc)['ok'])
        self.assertFalse(verify_hanging_indent(plan, {**SHAPE, 'Indentation': -3000}, 'a', 'b', self.doc, self.doc)['ok'])


# ------------------------------------------------------------------ package validation


class PackageValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.package = get_command_package_registry().get('layout_exact')

    def _validate(self, step: dict[str, Any]) -> dict[str, Any]:
        return self.package.validate(service=None, index=0, step=step, error_type=_Error)

    def test_manifest(self) -> None:
        self.assertFalse(self.package.read_only)
        for key in ('op', 'operation', 'label', 'kind', 'expected_pos', 'expected_before', 'confirm_layout', 'marker_text', 'section_index'):
            self.assertIn(key, self.package.allowed_keys)

    def test_accepts_and_normalizes(self) -> None:
        step = self._validate({'op': 'layout_exact', 'kind': 'columns', 'expected_pos': (0, 2, 0), 'count': 2, 'confirm_layout': True})
        self.assertEqual((step['expected_pos'], step['apply_to'], step['same_width']), ([0, 2, 0], 'current_section', True))
        self._validate(_page_step())

    def test_rejects_with_400(self) -> None:
        for step in (_page_step(confirm_layout=False), _page_step(bogus=1), _page_step(marker_text='1.'),
                     {'op': 'layout_exact', 'kind': 'section_insert', 'confirm_layout': True}):
            with self.subTest(step=step), self.assertRaises(_Error) as ctx:
                self._validate(step)
            self.assertEqual(ctx.exception.status_code, 400)


# ------------------------------------------------------------------ service flows


class PageSetupFlowTests(unittest.TestCase):
    def test_success_current_section(self) -> None:
        hwp = _FakeLayoutHwp([['a'], ['b']])
        result = _run(hwp, _page_step())
        self.assertTrue(result['succeeded'])
        self.assertEqual(hwp.mutations(), ['PageSetup'])
        self.assertEqual(hwp.sections[0]['pagedef']['LeftMargin'], mm_to_hwpunit(20))
        self.assertEqual(hwp.sections[1]['pagedef'], A4)
        self.assertEqual(hwp.executed_apply_to, [2])

    def test_success_whole_document_landscape(self) -> None:
        hwp = _FakeLayoutHwp([['a'], ['b']])
        step = _page_step(landscape=True, expected_before={'margin_left_mm': 30, 'landscape': False}, apply_to='whole_document')
        _run(hwp, step)
        self.assertEqual(hwp.executed_apply_to, [3])
        self.assertTrue(all(s['pagedef']['Landscape'] == 1 and s['pagedef']['LeftMargin'] == mm_to_hwpunit(20) for s in hwp.sections))

    def test_stale_expected_before_refused_without_native_action(self) -> None:
        hwp = _FakeLayoutHwp()
        with self.assertRaisesRegex(LocalCliRuntimeError, 'changed since it was probed') as ctx:
            _run(hwp, _page_step(expected_before={'margin_left_mm': 25}))
        self.assertNotIsInstance(ctx.exception, LocalCliMutationError)
        self.assertEqual(hwp.mutations(), [])

    def test_readback_mismatch_is_mutation_error(self) -> None:
        for faults in ({'extra_page_change': True}, {'noop': {'PageSetup'}}):
            hwp = _FakeLayoutHwp([['a'], ['b']], **faults)
            with self.subTest(faults=faults), self.assertRaises(LocalCliMutationError) as ctx:
                _run(hwp, _page_step())
            self.assertTrue(ctx.exception.mutation_may_have_persisted)

    def test_wrong_scope_is_mutation_error(self) -> None:
        hwp = _FakeLayoutHwp([['a'], ['b']], page_applies_to_all=True)
        with self.assertRaisesRegex(LocalCliMutationError, 'changed 2 sections'):
            _run(hwp, _page_step())

    def test_raising_execute_not_retried(self) -> None:
        hwp = _FakeLayoutHwp(raising={'PageSetup'})
        with self.assertRaisesRegex(LocalCliMutationError, 'outcome_unknown'):
            _run(hwp, _page_step())
        self.assertEqual(hwp.mutations(), ['PageSetup'])

    def test_caret_mismatch_refused(self) -> None:
        hwp = _FakeLayoutHwp([['a', 'b']])
        with self.assertRaisesRegex(LocalCliRuntimeError, 'caret is at'):
            _run(hwp, _page_step(expected_pos=[0, 1, 0]))
        self.assertEqual(hwp.mutations(), [])

    def test_selection_or_unknown_state_refused(self) -> None:
        for hwp in (_FakeLayoutHwp(snapshot_unknown=True), _FakeLayoutHwp()):
            if not hwp.snapshot_unknown:
                hwp.selection = (0, 0, 0, 1)
            with self.assertRaisesRegex(LocalCliRuntimeError, 'normal edit state'):
                _run(hwp, _page_step())
            self.assertEqual(hwp.mutations(), [])


class ColumnFlowTests(unittest.TestCase):
    def _step(self, **overrides: Any) -> dict[str, Any]:
        step = {'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2, 'gap_mm': 8.0}
        step.update(overrides)
        return step

    def test_success_current_and_new(self) -> None:
        hwp = _FakeLayoutHwp()
        result = _run(hwp, self._step())
        self.assertEqual(result['verification']['after']['Count'], 2)
        self.assertEqual(hwp.sections[0]['coldefs'], [{**COLDEF, 'Count': 2, 'SameGap': mm_to_hwpunit(8)}])
        hwp = _FakeLayoutHwp()
        _run(hwp, self._step(apply_to='from_caret_new', count=3))
        self.assertEqual(len(hwp.sections[0]['coldefs']), 2)
        self.assertEqual(hwp.executed_apply_to, [6])

    def test_readback_mismatch(self) -> None:
        hwp = _FakeLayoutHwp(coldef_ignores_gap=True)
        with self.assertRaisesRegex(LocalCliMutationError, 'SameGap'):
            _run(hwp, self._step())

    def test_multi_section_change_must_stay_in_the_caret_section(self) -> None:
        sections = [['one'], ['two'], ['three']]
        hwp = _FakeLayoutHwp(sections)
        hwp.caret = (0, 1, 0)
        result = _run(hwp, self._step(expected_pos=[0, 1, 0]))
        self.assertTrue(result['verification']['ok'])
        self.assertEqual([len(s['coldefs']) and s['coldefs'][-1]['Count'] for s in hwp.sections], [1, 2, 1])
        for apply_to in ('current_section', 'from_caret_new'):
            with self.subTest(apply_to=apply_to):
                hwp = _FakeLayoutHwp(sections, coldef_applies_to_all=True)
                hwp.caret = (0, 1, 0)
                with self.assertRaisesRegex(LocalCliMutationError, r'sections \[1, 2, 3\], expected only section 2'):
                    _run(hwp, self._step(expected_pos=[0, 1, 0], apply_to=apply_to))

    def test_multi_section_without_caret_section_is_refused_before_mutation(self) -> None:
        hwp = _FakeLayoutHwp([['one'], ['two']])
        hwp.KeyIndicator = lambda: None  # type: ignore[method-assign]
        with self.assertRaisesRegex(LocalCliRuntimeError, 'cannot be scoped'):
            _run(hwp, self._step())
        self.assertEqual(hwp.mutations(), [])

    def test_stale_expected_before(self) -> None:
        hwp = _FakeLayoutHwp()
        with self.assertRaisesRegex(LocalCliRuntimeError, 'since it was probed'):
            _run(hwp, self._step(expected_before={'count': 2}))
        self.assertEqual(hwp.mutations(), [])

    def test_raising_not_retried(self) -> None:
        hwp = _FakeLayoutHwp(raising={'MultiColumn'})
        with self.assertRaises(LocalCliMutationError):
            _run(hwp, self._step())
        self.assertEqual(hwp.mutations(), ['MultiColumn'])


class BodyDriftFlowTests(unittest.TestCase):
    """Each kind's real verifier path must reject an unrequested body change (here a paragraph Style)."""

    def test_every_kind_rejects_style_drift_elsewhere(self) -> None:
        cases = (
            ('page_setup', [['a'], ['b']], (0, 0, 0), _page_step()),
            ('columns', None, (0, 0, 0), {'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2}),
            ('section_insert', [['hello world', 'next']], (0, 0, 6), {'kind': 'section_insert', 'expected_pos': [0, 0, 6], 'confirm_layout': True}),
            ('section_delete', [['a', 'b'], ['c', 'd'], ['e']], (0, 2, 0),
             {'kind': 'section_delete', 'expected_pos': [0, 2, 0], 'confirm_layout': True, 'section_index': 2}),
            ('hanging_indent', None, (0, 0, 0), {'kind': 'hanging_indent', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'marker_text': '1. '}),
        )
        for kind, sections, caret, step in cases:
            with self.subTest(kind=kind):
                clean = _FakeLayoutHwp(sections)
                clean.caret = caret
                self.assertTrue(_run(clean, dict(step))['succeeded'])
                hwp = _FakeLayoutHwp(sections, drift=True)
                hwp.caret = caret
                with self.assertRaisesRegex(LocalCliMutationError, 'body changed|section break'):
                    _run(hwp, dict(step))


class SectionFlowTests(unittest.TestCase):
    def test_insert_success(self) -> None:
        hwp = _FakeLayoutHwp([['hello world', 'next']])
        hwp.caret = (0, 0, 6)
        result = _run(hwp, {'kind': 'section_insert', 'expected_pos': [0, 0, 6], 'confirm_layout': True})
        self.assertEqual((result['document_before']['section_count'], result['document_after']['section_count']), (1, 2))
        self.assertEqual(hwp.mutations(), ['BreakSection'])

    def test_insert_text_change_detected(self) -> None:
        hwp = _FakeLayoutHwp([['hello world']], break_eats_char=True)
        hwp.caret = (0, 0, 6)
        with self.assertRaisesRegex(LocalCliMutationError, 'text changed'):
            _run(hwp, {'kind': 'section_insert', 'expected_pos': [0, 0, 6], 'confirm_layout': True})

    def test_insert_noop_and_raise(self) -> None:
        hwp = _FakeLayoutHwp(noop={'BreakSection'})
        with self.assertRaisesRegex(LocalCliMutationError, 'SECDEF count'):
            _run(hwp, {'kind': 'section_insert', 'expected_pos': [0, 0, 0], 'confirm_layout': True})
        hwp = _FakeLayoutHwp(raising={'BreakSection'})
        with self.assertRaises(LocalCliMutationError):
            _run(hwp, {'kind': 'section_insert', 'expected_pos': [0, 0, 0], 'confirm_layout': True})
        self.assertEqual(hwp.mutations(), ['BreakSection'])

    def test_insert_refused_in_table_cell(self) -> None:
        hwp = _FakeLayoutHwp(in_cell=True)
        with self.assertRaisesRegex(LocalCliRuntimeError, 'not a table cell'):
            _run(hwp, {'kind': 'section_insert', 'expected_pos': [0, 0, 0], 'confirm_layout': True})
        self.assertEqual(hwp.mutations(), [])

    def test_insert_caret_mismatch(self) -> None:
        hwp = _FakeLayoutHwp()
        with self.assertRaisesRegex(LocalCliRuntimeError, 'caret is at'):
            _run(hwp, {'kind': 'section_insert', 'expected_pos': [0, 0, 3], 'confirm_layout': True})
        self.assertEqual(hwp.mutations(), [])

    def _delete_step(self, para: int = 2, index: int = 2) -> dict[str, Any]:
        return {'kind': 'section_delete', 'expected_pos': [0, para, 0], 'confirm_layout': True, 'section_index': index}

    def test_delete_success(self) -> None:
        hwp = _FakeLayoutHwp([['a', 'b'], ['c', 'd'], ['e']])
        hwp.caret = (0, 2, 0)
        result = _run(hwp, self._delete_step())
        self.assertEqual(len(hwp.sections), 2)
        self.assertEqual(result['section_start_proof']['section_after_move_left'], 1)
        self.assertEqual(hwp.mutations(), ['DeleteBack'])

    def test_delete_refuses_when_not_at_section_start(self) -> None:
        hwp = _FakeLayoutHwp([['a', 'b'], ['c', 'd']])
        hwp.caret = (0, 3, 0)  # start of 'd', inside section 2 but not its start
        with self.assertRaisesRegex(LocalCliRuntimeError, 'not provably at the start'):
            _run(hwp, self._delete_step(para=3))
        self.assertEqual(hwp.mutations(), [])
        self.assertEqual(hwp.caret, (0, 3, 0))
        hwp.caret = (0, 1, 0)
        with self.assertRaisesRegex(LocalCliRuntimeError, 'section 1, expected 2'):
            _run(hwp, self._delete_step(para=1))

    def test_delete_noop_and_raise(self) -> None:
        hwp = _FakeLayoutHwp([['a'], ['b']], noop={'DeleteBack'})
        hwp.caret = (0, 1, 0)
        with self.assertRaisesRegex(LocalCliMutationError, 'SECDEF count'):
            _run(hwp, self._delete_step(para=1))
        hwp = _FakeLayoutHwp([['a'], ['b']], raising={'DeleteBack'})
        hwp.caret = (0, 1, 0)
        with self.assertRaises(LocalCliMutationError):
            _run(hwp, self._delete_step(para=1))
        self.assertEqual(hwp.mutations(), ['DeleteBack'])


class HangingIndentFlowTests(unittest.TestCase):
    def _step(self, marker: str = '1. ', pos: tuple[int, int, int] = (0, 0, 0)) -> dict[str, Any]:
        return {'kind': 'hanging_indent', 'expected_pos': list(pos), 'confirm_layout': True, 'marker_text': marker}

    def test_success_both_models(self) -> None:
        for model in ('indent_only', 'margin_shift'):
            hwp = _FakeLayoutHwp(indent_model=model)
            result = _run(hwp, self._step())
            self.assertEqual(result['verification']['model'], model)
            self.assertEqual(hwp.sections[0]['paras'][0]['shape']['Indentation'], -3000)
            self.assertEqual(hwp.sections[0]['paras'][1]['shape'], SHAPE)
            self.assertEqual(hwp.caret, (0, 0, 0))
            self.assertEqual(hwp.mutations(), ['ParagraphShapeIndentAtCaret'])

    def test_marker_mismatch_refused(self) -> None:
        hwp = _FakeLayoutHwp()
        with self.assertRaisesRegex(LocalCliRuntimeError, 'does not start with'):
            _run(hwp, self._step(marker='2. '))
        self.assertEqual(hwp.mutations(), [])

    def test_readback_mismatch(self) -> None:
        for model, pattern in (('positive', 'negative'), ('shape_drift', 'LineSpacing')):
            hwp = _FakeLayoutHwp(indent_model=model)
            with self.subTest(model=model), self.assertRaisesRegex(LocalCliMutationError, pattern):
                _run(hwp, self._step())

    def test_caret_moved_by_action_is_mutation_error(self) -> None:
        hwp = _FakeLayoutHwp(indent_moves_caret=True)
        with self.assertRaisesRegex(LocalCliMutationError, 'caret moved'):
            _run(hwp, self._step())

    def test_raising_not_retried(self) -> None:
        hwp = _FakeLayoutHwp(raising={'ParagraphShapeIndentAtCaret'})
        with self.assertRaises(LocalCliMutationError):
            _run(hwp, self._step())
        self.assertEqual(hwp.mutations(), ['ParagraphShapeIndentAtCaret'])

    def test_caret_mismatch_refused(self) -> None:
        hwp = _FakeLayoutHwp()
        with self.assertRaisesRegex(LocalCliRuntimeError, 'caret is at'):
            _run(hwp, self._step(pos=(0, 1, 0)))
        self.assertEqual(hwp.mutations(), [])

    def test_marker_span_must_read_back_as_marker(self) -> None:
        hwp = _FakeLayoutHwp()
        original = hwp.get_selected_text

        def shifted(keep_select: bool = True) -> str:
            if hwp.selection and hwp.selection[3] != -1:
                hwp.selection = None
                return 'xx'
            return original(keep_select=keep_select)

        hwp.get_selected_text = shifted  # type: ignore[method-assign]
        with self.assertRaisesRegex(LocalCliRuntimeError, 'not marker_text'):
            _run(hwp, self._step())
        self.assertEqual(hwp.mutations(), [])

    def test_unknown_state_refused(self) -> None:
        hwp = _FakeLayoutHwp(snapshot_unknown=True)
        with self.assertRaisesRegex(LocalCliRuntimeError, 'normal edit state'):
            _run(hwp, self._step())
        self.assertEqual(hwp.mutations(), [])


if __name__ == '__main__':
    unittest.main()
