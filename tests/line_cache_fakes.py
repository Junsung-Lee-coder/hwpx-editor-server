"""SYNTHETIC external SDK model of Hancom's computed line-layout cache (hp:linesegarray).

Only the fake SDK is modelled here; the product neither predicts nor computes these
numbers. The model translates the fixture's own HWPML page definition into the
section's hp:secPr/hp:pagePr/hp:margin/hp:colPr exactly as the actual h01 copy maps
them (Landscape 0 -> WIDELY, same numbers), writes one hp:linesegarray per paragraph,
and at Execute recomputes ONLY the target picture's own paragraph unless a test asks
for a broader native-like reflow. Not native evidence.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET

from tests import hwpx_package_fakes as fakes

ATTRS = ('textpos', 'vertpos', 'vertsize', 'textheight', 'baseline', 'spacing', 'horzpos', 'horzsize', 'flags')
SINGLE_SEGMENT = 393216  # bits 17 and 18: first and last segment of the line (as in every observed native line)


def _path(root: ET.Element, target: ET.Element) -> tuple[int, ...]:
    def visit(node, path):
        if node is target:
            return path
        for index, child in enumerate(node):
            found = visit(child, path + (index,))
            if found is not None:
                return found
        return None
    found = visit(root, ())
    if found is None:
        raise AssertionError('paragraph is not in the fake document')
    return found


def _first(element: ET.Element, tag: str) -> ET.Element | None:
    return next((e for e in element.iter() if e.tag == tag), None)


class LineCacheHwp(fakes.PackagePictureHwp):
    """PackagePictureHwp plus a SYNTHETIC per-paragraph line cache and section page geometry."""

    def __init__(self, *, hwpml: ET.Element | None = None, lines_per_paragraph: dict | None = None, **kw):
        super().__init__(**kw)
        if hwpml is not None:
            self.root = hwpml
            self.native_position = tuple(int(_first(self.root, 'CARETPOS').get(k)) for k in ('List', 'Para', 'Pos'))
            self.relink()
        self.lines_per_paragraph = dict(lines_per_paragraph or {})  # {top-level paragraph path: line count}
        self.reflow_target = True        # recompute the target paragraph's lines at Execute
        self.reflow_following = False    # adversary: also move every later paragraph vertically
        self.reflow_damage = None        # adversary: fn(fake, target_key) after the recomputation
        self.lines = self._initial_lines()
        self.line_undo: list = []
        self.line_redo: list = []

    # ------------------------------------------------------------------ synthetic layout model
    def _sections(self):
        body = next(e for e in self.root if e.tag == 'BODY')
        return [s for s in body if s.tag == 'SECTION']

    def text_width(self) -> int:
        page, margin = _first(self.root, 'PAGEDEF'), _first(self.root, 'PAGEMARGIN')
        return int(page.get('Width')) - int(margin.get('Left')) - int(margin.get('Right')) - int(margin.get('Gutter'))

    def _pictures(self, paragraph: ET.Element):
        return [e for e in paragraph.iter() if e.tag == 'PICTURE']

    def _layout(self, paragraph: ET.Element, key, first_vertpos: int) -> list[dict]:
        width = self.text_width()
        height, left = 1000, 0
        for pic in self._pictures(paragraph):
            shape = _first(pic, 'SHAPEOBJECT')
            size, pos = _first(shape, 'SIZE'), _first(shape, 'POSITION')
            if pos.get('TreatAsChar') == 'true':
                height = max(height, int(size.get('Height')))
            elif shape.get('TextWrap') == 'Square':
                left = max(left, int(size.get('Width')) + int(pos.get('HorzOffset')))
        count = self.lines_per_paragraph.get(key, 1)
        rows, vertpos = [], first_vertpos
        for index in range(count):
            line_height = height if index == 0 else 1000
            rows.append({'textpos': index * 12, 'vertpos': vertpos, 'vertsize': line_height, 'textheight': line_height,
                         'baseline': line_height * 85 // 100, 'spacing': 600, 'horzpos': left, 'horzsize': width - left,
                         'flags': SINGLE_SEGMENT})
            vertpos += line_height + 600
        return rows

    def _initial_lines(self) -> dict:
        lines = {}
        for section in self._sections():
            vertpos = 0
            for paragraph in section:
                key = _path(self.root, paragraph)
                lines[key] = self._layout(paragraph, key, vertpos)
                vertpos = lines[key][-1]['vertpos'] + lines[key][-1]['vertsize'] + lines[key][-1]['spacing']
        return lines

    def _target_paragraph_key(self):
        pic = next(p for p in self.root.iter() if p.tag == 'PICTURE' and _first(p, 'SHAPEOBJECT').get('InstId') == self.selected.ident)
        for section in self._sections():
            for paragraph in section:
                if any(e is pic for e in paragraph.iter()):
                    return _path(self.root, paragraph), section
        raise AssertionError('target picture has no top-level paragraph')

    # ------------------------------------------------------------------ serialization hook
    def paragraph_lines(self, element: ET.Element) -> tuple[str, str]:
        key = _path(self.root, element)
        prefix = ''
        if any(e.tag == 'SECDEF' for e in element.iter()) and len(key) == 3:
            page, margin, col = _first(self.root, 'PAGEDEF'), _first(self.root, 'PAGEMARGIN'), _first(self.root, 'COLDEF')
            landscape = {'0': 'WIDELY', '1': 'NARROWLY'}[page.get('Landscape')]
            gutter = {'LeftOnly': 'LEFT_ONLY', 'LeftRight': 'LEFT_RIGHT', 'TopBottom': 'TOP_BOTTOM'}[page.get('GutterType')]
            prefix = ('<hp:run charPrIDRef="0"><hp:secPr id="" textDirection="HORIZONTAL">'
                      f'<hp:pagePr landscape="{landscape}" width="{page.get("Width")}" height="{page.get("Height")}" gutterType="{gutter}">'
                      f'<hp:margin header="{margin.get("Header")}" footer="{margin.get("Footer")}" gutter="{margin.get("Gutter")}" '
                      f'left="{margin.get("Left")}" right="{margin.get("Right")}" top="{margin.get("Top")}" bottom="{margin.get("Bottom")}"/>'
                      '</hp:pagePr></hp:secPr><hp:ctrl>'
                      f'<hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="{col.get("Count")}" sameSz="1" sameGap="0"/>'
                      '</hp:ctrl></hp:run>')
        rows = self.lines.get(key)
        if rows is None:  # nested paragraph (header/footer): its own stable cache
            rows = self.lines[key] = self._layout(element, key, 0)
        segs = ''.join('<hp:lineseg' + ''.join(f' {a}="{row[a]}"' for a in ATTRS) + '/>' for row in rows)
        return prefix, f'<hp:linesegarray>{segs}</hp:linesegarray>'

    # ------------------------------------------------------------------ native effects
    def Execute(self, name, pset):
        before = copy.deepcopy(self.lines)
        result = super().Execute(name, pset)
        self.line_undo.append(before)
        self.line_redo.clear()
        key, section = self._target_paragraph_key()
        if self.reflow_target:
            old = self.lines[key]
            self.lines[key] = self._layout(self._element(key), key, old[0]['vertpos'])
            if self.reflow_following:
                delta = (self.lines[key][-1]['vertpos'] + self.lines[key][-1]['vertsize']) - (old[-1]['vertpos'] + old[-1]['vertsize'])
                later = False
                for paragraph in section:
                    other = _path(self.root, paragraph)
                    if later:
                        for row in self.lines[other]:
                            row['vertpos'] += delta
                    later = later or other == key
        if self.reflow_damage is not None:
            self.reflow_damage(self, key)
        return result

    def _element(self, key):
        node = self.root
        for index in key:
            node = list(node)[index]
        return node

    def Run(self, name):
        if name in ('Undo', 'Redo') and self.undo_result is None:
            source, target = (self.line_undo, self.line_redo) if name == 'Undo' else (self.line_redo, self.line_undo)
            if source:
                target.append(copy.deepcopy(self.lines))
                self.lines = source.pop()
        return super().Run(name)
