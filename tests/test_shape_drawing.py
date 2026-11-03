"""Drawn shapes with the characterized drawing contract (S1, second stage).

The contract comes from an owned Hancom Office 2024 characterization (each request saved, reopened and rendered):
after GetDefault and the frame items (Width, Height, TreatAsChar), the drawing goes in through the typed
ShapeDrawLayOut subset (CreateItemArray('CreatePt', 2n), each coordinate, CreateNumPt n) and the outline through the
typed ShapeDrawLineAttr subset (style 1 = solid, Width 85 = 0.3 mm, Color 0, Alpha 0, OutLineStyle 0). Every
prepared value is read back through its typed getter and must be exactly the int that was set before the single
Execute. Afterwards the new control's HWPML2X must show the requested frame, finite transforms, the drawn geometry
(rectangle corners, ellipse centre and axes, line endpoints scaled to the request) and a visible solid outline.

``tests/fixtures/native_shapes`` holds the new drawing object exactly as Hancom read it back in that run. The
verifier accepts each of them, refuses the one drawn without an outline and refuses every degenerate alteration.
The synthetic drawing SDK below reproduces those readbacks for the same prepared values (checked here), and draws
nothing visible when the drawing subsets are missing, as Hancom does.
"""
from __future__ import annotations

import copy
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from app.object_insert import ObjectInsertError, evaluate_insert, mm_to_hwpunit, normalize_step, parse_hwpml
from tests.test_bundle_step_local_refusal import owner_for
from tests.test_deferred_step_intent import OBJECT_READS, _Checks, counted, modifies, run
from tests.shape_forms import OUTLINE, drawn_xml, points_for
from tests.test_object_insert_exact import BEFORE, _char, _doc, _FakeHAction, _FakeHwp, _Params

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'native_shapes'
INDEX = json.loads((FIXTURES / 'INDEX.json').read_text(encoding='utf-8'))['cases']
TAGS = {'rectangle': 'RECTANGLE', 'ellipse': 'ELLIPSE', 'line': 'LINE'}
ACTIONS = {'rectangle': 'DrawObjCreatorRectangle', 'ellipse': 'DrawObjCreatorEllipse', 'line': 'DrawObjCreatorLine'}

# The characterized requests (mm), as the public step gives them.
REQUESTS = {
    'rectangle': {'width_mm': 30, 'height_mm': 12},
    'ellipse': {'width_mm': 40, 'height_mm': 20},
    'line_h': {'width_mm': 40, 'height_mm': 0},
    'line_v': {'width_mm': 0, 'height_mm': 30},
}


# ------------------------------------------------------------------ synthetic drawing SDK


class _Array:
    """HArray made by CreateItemArray: Count, Item(i), SetItem(i, value)."""

    def __init__(self, count: int, hwp: Any) -> None:
        self.values: list[Any] = [0] * count
        self.hwp = hwp

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.values)

    def Item(self, index: int) -> Any:  # noqa: N802
        value = self.values[index]
        return float(value) if 'item_float' in self.hwp.drawing_faults else value

    def SetItem(self, index: int, value: Any) -> None:  # noqa: N802
        if 'array_drops_last' in self.hwp.drawing_faults and index == len(self.values) - 1:
            return
        self.values[index] = value


class _Layout:
    """Typed HDrawLayOut subset."""

    def __init__(self, hwp: Any) -> None:
        object.__setattr__(self, 'hwp', hwp)
        object.__setattr__(self, '_num', 0)
        object.__setattr__(self, 'CreatePt', None)

    def CreateItemArray(self, name: str, count: int) -> None:  # noqa: N802
        assert name == 'CreatePt', name
        extra = 1 if 'count_long' in self.hwp.drawing_faults else 0
        object.__setattr__(self, 'CreatePt', _Array(count + extra, self.hwp))

    def __getattr__(self, name: str) -> Any:
        if name == 'CreateNumPt':
            return self._num
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name != 'CreateNumPt':
            raise AttributeError(f'HDrawLayOut has no settable {name}')
        if 'numpt_ignored' not in self.hwp.drawing_faults:
            object.__setattr__(self, '_num', value)


class _LineAttr:
    """Typed HDrawLineAttr subset (lowercase ``style``, as the type library names it)."""

    NAMES = ('style', 'Width', 'Color', 'Alpha', 'OutLineStyle')

    def __init__(self, hwp: Any) -> None:
        object.__setattr__(self, 'hwp', hwp)
        object.__setattr__(self, 'values', {name: 0 for name in self.NAMES})

    def __getattr__(self, name: str) -> Any:
        if name in self.NAMES:
            return self.values[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name not in self.NAMES:
            raise AttributeError(f'HDrawLineAttr has no {name}')
        if name == 'style' and 'style_ignored' in self.hwp.drawing_faults:
            return
        self.values[name] = value


class _ShapeSet:
    """HShapeObject: the frame items through HSet.SetItem and typed getters, and the two typed drawing subsets.

    As in the installed type library, the typed HSet has SetItem only (no Item/ItemExist). GetDefault replaces both
    subsets, so values written into a subset fetched before GetDefault are lost.
    """

    def __init__(self, hwp: Any) -> None:
        self.hwp = hwp
        self.HSet = self
        self.reset()

    def reset(self) -> None:
        self.frame: dict[str, Any] = {'Width': 0, 'Height': 0, 'TreatAsChar': 0}
        self.ShapeDrawLayOut = _Layout(self.hwp)
        self.ShapeDrawLineAttr = _LineAttr(self.hwp)

    @property
    def items(self) -> dict[str, Any]:
        return dict(self.frame)

    def SetItem(self, name: str, value: Any) -> None:  # noqa: N802
        self.frame[name] = value

    @property
    def Width(self) -> Any:  # noqa: N802
        return self.frame['Width']

    @property
    def Height(self) -> Any:  # noqa: N802
        return self.frame['Height']

    @property
    def TreatAsChar(self) -> Any:  # noqa: N802
        value = self.frame['TreatAsChar']
        return bool(value) if 'treat_bool' in self.hwp.drawing_faults else value

    def prepared(self) -> dict[str, Any]:
        layout, line = self.ShapeDrawLayOut, self.ShapeDrawLineAttr
        return {**self.frame, 'CreateNumPt': layout.CreateNumPt,
                'CreatePt': list(layout.CreatePt.values) if layout.CreatePt is not None else None, 'line': dict(line.values)}


class _DrawingParams(_Params):
    def __getattr__(self, name: str) -> Any:
        if name == 'HShapeObject':
            return self.hwp.shape_set
        return super().__getattr__(name)


class _DrawingHAction(_FakeHAction):
    def GetDefault(self, name: str, hset: Any) -> Any:  # noqa: N802
        if isinstance(hset, _ShapeSet):
            self.hwp.log.append(f'GetDefault({name})')
            hset.reset()
            return False if 'get_default_false' in self.hwp.drawing_faults else True
        return super().GetDefault(name, hset)

    def Execute(self, name: str, hset: Any) -> Any:  # noqa: N802
        if isinstance(hset, _ShapeSet):
            self.hwp.executed.append(hset.prepared())
            result = self.hwp.native(name, hset.items)
            return 1 if 'execute_int' in self.hwp.drawing_faults else result
        return super().Execute(name, hset)


class _DrawingHwp(_FakeHwp):
    """The object SDK fake with Hancom's drawing subsets.

    Drawing faults: ``array_drops_last``, ``item_float``, ``count_long``, ``numpt_ignored``, ``style_ignored``,
    ``treat_bool`` and ``get_default_false`` corrupt preparation or its readback; ``execute_int`` returns 1 instead of
    True; ``ignores_line_attr`` / ``ignores_points`` draw as Hancom does without that subset; ``stays_selected``
    keeps the new object selected after Cancel.
    """

    def __init__(self, *drawing_faults: str, **faults: Any) -> None:
        super().__init__(**faults)
        self.drawing_faults = frozenset(drawing_faults)
        self.executed: list[dict[str, Any]] = []
        self.shape_set = _ShapeSet(self)
        self.HAction = _DrawingHAction(self)
        self.HParameterSet = _DrawingParams(self)
        self.selection_mode = 0

    @property
    def SelectionMode(self) -> int:  # noqa: N802
        return self.selection_mode

    def native(self, name: str, items: dict[str, Any] | None) -> bool:
        if name.startswith('DrawObjCreator') and name not in self.noop:
            self.log.append(f'Execute({name})')
            tag = name.removeprefix('DrawObjCreator').upper()
            self.put(drawn_xml(tag, self.shape_set.prepared(), self.drawing_faults))
            self.selection_mode = 4  # Hancom leaves the new object selected
            return True
        if name == 'Cancel':
            self.log.append('Cancel')
            if 'stays_selected' not in self.drawing_faults:
                self.selection_mode = 0
            return True
        return super().native(name, items)


def drawing_doc(*drawing_faults: str) -> _DrawingHwp:
    hwp = counted(_DrawingHwp(*drawing_faults), OBJECT_READS)
    modifies(hwp, 'native', lambda action, *rest: action not in hwp.noop and action != 'Cancel')
    hwp.fault_mark = None
    return hwp


def shape_step(hwp: Any, name: str, inline: bool) -> dict[str, Any]:
    kind = 'line' if name.startswith('line') else name
    step = {'op': 'object_insert_exact', 'kind': kind, 'expected_pos': list(hwp.caret), 'confirm_mutation': True,
            'treat_as_char': inline, **REQUESTS[name]}
    return step


# ------------------------------------------------------------------ helpers over the pinned readbacks


def canonical(element: ET.Element) -> Any:
    """Tags, attributes and text, without per-instance ids and the unused arc fields of a plain ellipse."""
    attrib = {key: value for key, value in element.attrib.items() if key not in ('InstId', 'InstID', 'ZOrder')}
    if element.tag == 'ELLIPSE' and attrib.get('HasArcProperty') == 'false':
        attrib = {key: value for key, value in attrib.items() if not key.startswith(('Start', 'End'))}
    return (element.tag, sorted(attrib.items()), (element.text or '').strip(), [canonical(child) for child in element])


def fixture(name: str) -> str:
    return (FIXTURES / f'{name}.xml').read_text(encoding='utf-8')


def fixture_plan(name: str) -> dict[str, Any]:
    case = INDEX[name]
    base = name.split('_floating')[0].split('_inline')[0]
    # The recorded-macro ellipse (5669 x 5669 HWPUNIT) is the 20 mm circle.
    request = {'width_mm': 20, 'height_mm': 20} if name.endswith('recorded_equal') else REQUESTS[base]
    step = {'kind': 'line' if base.startswith('line') else base, 'expected_pos': [0, 0, 0], 'confirm_mutation': True,
            'treat_as_char': case['treat_as_char'], **request}
    plan = normalize_step(step)
    assert (plan['width_hu'], plan['height_hu']) == (case['width_hu'], case['height_hu']), (name, plan)
    return plan


def placed(snippet: str) -> str:
    return _doc(snippet + _char('Hello world'), _char('Second'))


def verdict(plan: dict[str, Any], snippet: str) -> dict[str, Any]:
    return evaluate_insert(plan, parse_hwpml(BEFORE), parse_hwpml(placed(snippet)))


def altered(snippet: str, path: str, attr: str, value: str | None) -> str:
    root = ET.fromstring(snippet)
    target = root if path == '.' else root.find(path)
    assert target is not None, path
    if value is None:
        target.attrib.pop(attr)
    else:
        assert attr in target.attrib or attr in ('Color',), (path, attr)
        target.set(attr, value)
    return ET.tostring(root, encoding='unicode')


QUALIFIED = ('rectangle_inline', 'rectangle_floating', 'ellipse_inline', 'ellipse_floating', 'line_h_inline', 'line_h_floating',
             'line_v_inline', 'line_v_floating')


class HancomAuthoredReadbacks(unittest.TestCase):
    def test_the_synthetic_sdk_reproduces_each_hancom_readback_for_the_same_prepared_values(self) -> None:
        for name, case in INDEX.items():
            with self.subTest(name):
                state = {'Width': case['width_hu'], 'Height': case['height_hu'], 'TreatAsChar': int(case['treat_as_char']),
                         'CreateNumPt': len(case['points']) // 2, 'CreatePt': case['points'],
                         'line': {name_: case['line_attr'].get(name_, 0) for name_ in OUTLINE}}
                if name.startswith('line'):
                    self.assertEqual(state['CreateNumPt'], 2)
                self.assertEqual(canonical(ET.fromstring(drawn_xml(case['tag'], state))), canonical(ET.fromstring(fixture(name))))

    def test_the_characterized_layouts_match_the_pinned_requests(self) -> None:
        for name in QUALIFIED:
            with self.subTest(name):
                case, base = INDEX[name], name.rsplit('_', 1)[0]
                self.assertEqual(points_for(base, case['width_hu'], case['height_hu'])[1], case['points'])
                self.assertEqual(case['line_attr'], OUTLINE)

    def test_each_hancom_drawn_shape_is_verified(self) -> None:
        for name in QUALIFIED + ('ellipse_floating_recorded_equal',):
            with self.subTest(name):
                result = verdict(fixture_plan(name), fixture(name))
                self.assertTrue(result['ok'], result)

    def test_a_shape_drawn_without_an_outline_is_not_verified(self) -> None:
        result = verdict(fixture_plan('rectangle_floating_no_line_attr'), fixture('rectangle_floating_no_line_attr'))
        self.assertFalse(result['ok'])
        self.assertIn('outline', ' '.join(result['reasons']))

    def test_editing_only_the_outline_or_frame_of_an_undrawn_object_is_not_verified(self) -> None:
        for name in ('rectangle', 'ellipse', 'line_h', 'line_v'):
            for inline in (True, False):
                with self.subTest(name=name, inline=inline):
                    hwp = _DrawingHwp()
                    step = shape_step(hwp, name, inline)
                    plan = normalize_step(step)
                    tag = TAGS[plan['kind']]
                    undrawn = {'Width': plan['width_hu'], 'Height': plan['height_hu'], 'TreatAsChar': int(inline),
                               'CreateNumPt': 0, 'CreatePt': None, 'line': dict(OUTLINE)}
                    result = verdict(plan, drawn_xml(tag, undrawn))
                    self.assertFalse(result['ok'], result)

    def test_every_degenerate_field_is_refused(self) -> None:
        rect, ellipse, line_h, line_v = 'rectangle_floating', 'ellipse_floating', 'line_h_floating', 'line_v_inline'
        component = './DRAWINGOBJECT/SHAPECOMPONENT'
        sca = component + '/RENDERINGINFO/SCAMATRIX'
        cases = (
            (rect, './SHAPEOBJECT/SIZE', 'Width', '8505'),
            (rect, './SHAPEOBJECT/SIZE', 'Height', '0'),
            (rect, './SHAPEOBJECT/SIZE', 'Width', '8504.0'),
            (rect, './SHAPEOBJECT/POSITION', 'TreatAsChar', 'true'),
            (rect, './DRAWINGOBJECT/LINESHAPE', 'Style', 'None'),
            (rect, './DRAWINGOBJECT/LINESHAPE', 'Width', '0'),
            (rect, './DRAWINGOBJECT/LINESHAPE', 'Width', None),
            (rect, './DRAWINGOBJECT/LINESHAPE', 'Alpha', '255'),
            (rect, './DRAWINGOBJECT/LINESHAPE', 'Color', '16777215'),
            (rect, '.', 'X1', '0'),
            (rect, '.', 'Y2', '0'),
            (rect, '.', 'X2', 'x'),
            (rect, component, 'OriWidth', '0'),
            (rect, component, 'OriHeight', '3401'),
            (rect, sca, 'E1', 'inf'),
            (rect, sca, 'E5', '0.000000'),
            (rect, sca, 'E3', '12.000000'),
            (rect, component + '/RENDERINGINFO/TRANSMATRIX', 'E5', '-nan(ind)'),
            (rect, component + '/RENDERINGINFO/ROTMATRIX', 'E2', '1.000000'),
            (rect, component, 'HorzFlip', 'true'),
            (ellipse, component, 'CurWidth', '0'),
            (ellipse, component, 'CurHeight', None),
            (ellipse, component, 'OriWidth', '0'),
            (ellipse, sca, 'E1', 'nan'),
            (ellipse, sca, 'E5', '2.000000'),
            (ellipse, '.', 'Axis1X', '5669'),
            (ellipse, '.', 'Axis2Y', '2834'),
            (ellipse, '.', 'CenterX', '0'),
            (ellipse, '.', 'HasArcProperty', 'true'),
            (line_h, component, 'CurWidth', '0'),
            (line_h, sca, 'E1', '0.000000'),
            (line_h, sca, 'E1', 'inf'),
            (line_h, sca, 'E5', '1.000000'),
            (line_h, sca, 'E5', '-nan(ind)'),
            (line_h, '.', 'EndX', '0'),
            (line_h, '.', 'IsReverseHV', 'true'),
            (line_h, './DRAWINGOBJECT/LINESHAPE', 'Style', 'None'),
            (line_v, sca, 'E5', '0.000000'),
            (line_v, sca, 'E1', '0.010000'),
            (line_v, component, 'CurHeight', '1'),
            (line_v, '.', 'EndY', '0'),
            (line_v, './SHAPEOBJECT/SIZE', 'Width', '1'),
        )
        for name, path, attr, value in cases:
            with self.subTest(name=name, path=path, attr=attr, value=value):
                self.assertTrue(verdict(fixture_plan(name), fixture(name))['ok'])
                result = verdict(fixture_plan(name), altered(fixture(name), path, attr, value))
                self.assertFalse(result['ok'], result)

    def test_missing_drawing_parts_are_refused(self) -> None:
        for name, path in (('rectangle_inline', './DRAWINGOBJECT/LINESHAPE'), ('ellipse_inline', './DRAWINGOBJECT/SHAPECOMPONENT/RENDERINGINFO'),
                           ('line_v_floating', './DRAWINGOBJECT/SHAPECOMPONENT/RENDERINGINFO/SCAMATRIX'), ('line_h_inline', './DRAWINGOBJECT')):
            with self.subTest(name=name, path=path):
                root = ET.fromstring(fixture(name))
                parent_path, _, child = path.rpartition('/')
                parent = root.find(parent_path) if parent_path != '.' else root
                parent.remove(parent.find(child))
                result = verdict(fixture_plan(name), ET.tostring(root, encoding='unicode'))
                self.assertFalse(result['ok'], result)
        doubled = ET.fromstring(fixture('rectangle_inline'))
        doubled.find('./DRAWINGOBJECT').append(copy.deepcopy(doubled.find('./DRAWINGOBJECT/LINESHAPE')))
        self.assertFalse(verdict(fixture_plan('rectangle_inline'), ET.tostring(doubled, encoding='unicode'))['ok'])


class DrawingRequests(unittest.TestCase):
    def step(self, **fields: Any) -> dict[str, Any]:
        return {'kind': 'rectangle', 'expected_pos': [0, 0, 0], 'confirm_mutation': True, 'width_mm': 30, 'height_mm': 12, **fields}

    def test_a_positive_size_that_rounds_below_a_drawable_extent_is_rejected(self) -> None:
        for fields in ({'width_mm': 0.001}, {'height_mm': 0.0017}, {'kind': 'line', 'width_mm': 0.001, 'height_mm': 0},
                       {'kind': 'line', 'width_mm': 0, 'height_mm': 0.001}, {'kind': 'ellipse', 'height_mm': 0.004},
                       {'kind': 'ellipse', 'width_mm': 0.005}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ObjectInsertError, 'HWPUNIT'):
                normalize_step(self.step(**fields))

    def test_the_smallest_drawable_sizes_keep_their_exact_extent(self) -> None:
        line = normalize_step(self.step(kind='line', width_mm=0.0018, height_mm=0))
        self.assertEqual((line['width_hu'], line['height_hu']), (1, 0))
        ellipse = normalize_step(self.step(kind='ellipse', width_mm=0.0071, height_mm=0.0071))
        self.assertEqual((ellipse['width_hu'], ellipse['height_hu']), (2, 2))
        tiny = {'Width': 1, 'Height': 0, 'TreatAsChar': 1, 'CreateNumPt': 2, 'CreatePt': [0, 0, 1, 0], 'line': dict(OUTLINE)}
        self.assertTrue(verdict(line, drawn_xml('LINE', tiny))['ok'])
        collapsed = dict(tiny, Width=0, CreatePt=[0, 0, 0, 0])
        self.assertFalse(verdict(line, drawn_xml('LINE', collapsed))['ok'])
        self.assertEqual(mm_to_hwpunit(0.0018), 1)


# ------------------------------------------------------------------ through the public bundle


class DrawingThroughTheBundle(_Checks):
    def expected_state(self, name: str, inline: bool) -> dict[str, Any]:
        plan = normalize_step(shape_step(_DrawingHwp(), name, inline))
        n, points = points_for(name, plan['width_hu'], plan['height_hu'])
        return {'Width': plan['width_hu'], 'Height': plan['height_hu'], 'TreatAsChar': 1 if inline else 0,
                'CreateNumPt': n, 'CreatePt': points, 'line': dict(OUTLINE)}

    def test_each_characterized_variant_is_drawn_once_and_verified(self) -> None:
        for name in REQUESTS:
            for inline in (True, False):
                with self.subTest(name=name, inline=inline):
                    hwp = drawing_doc()
                    owner = owner_for(self, hwp)
                    step = shape_step(hwp, name, inline)
                    result = run(owner, step)
                    self.assertTrue(result['ok'], result)
                    action = ACTIONS[step['kind']]
                    self.assertEqual(hwp.log, [f'GetDefault({action})', f'Execute({action})', 'Cancel'])
                    self.assertEqual(hwp.executed, [self.expected_state(name, inline)])
                    prepared = hwp.executed[0]
                    values = [prepared[key] for key in ('Width', 'Height', 'TreatAsChar', 'CreateNumPt')]
                    values += prepared['CreatePt'] + list(prepared['line'].values())
                    self.assertEqual({type(value) for value in values}, {int})
                    self.assertIn(f'<{TAGS[step["kind"]]} ', hwp.xml())
                    self.assertIn('Style="Solid"', hwp.xml())
                    self.assertEqual(hwp.selection_mode, 0)
                    self.assertTrue(owner.store['working_copy_dirty'])

    def test_a_preparation_that_does_not_read_back_exactly_never_executes(self) -> None:
        for fault in ('array_drops_last', 'item_float', 'count_long', 'numpt_ignored', 'style_ignored', 'treat_bool', 'get_default_false'):
            for name in ('rectangle', 'line_v'):
                with self.subTest(fault=fault, name=name):
                    hwp = drawing_doc(fault)
                    owner = owner_for(self, hwp)
                    xml = hwp.xml()
                    result = run(owner, shape_step(hwp, name, True))
                    self.assertEqual(hwp.executed, [])
                    self.assertFalse(any(entry.startswith('Execute') for entry in hwp.log), hwp.log)
                    self.assertEqual(hwp.xml(), xml)
                    self.assert_held(owner, hwp, result, mutated=False)

    def test_failures_after_the_draw_hold_the_session(self) -> None:
        cases = (
            ('execute_int', 'ellipse', 'native', 'native_action_failed'),
            ('execute_int', 'line_h', 'native', 'native_action_failed'),
            ('ignores_line_attr', 'ellipse', 'verify', 'shape_outline_not_visible'),
            ('ignores_line_attr', 'line_h', 'verify', 'shape_outline_not_visible'),
            ('ignores_points', 'rectangle', 'verify', 'shape_geometry_mismatch'),
            ('ignores_points', 'ellipse', 'verify', 'shape_transform_invalid'),
            ('ignores_points', 'line_v', 'verify', 'shape_transform_invalid'),
            ('stays_selected', 'ellipse', 'native', 'shape_not_released'),
            ('stays_selected', 'line_h', 'native', 'shape_not_released'),
        )
        for fault, name, phase, reason in cases:
            with self.subTest(fault=fault, name=name):
                hwp = drawing_doc(fault)
                owner = owner_for(self, hwp)
                result = run(owner, shape_step(hwp, name, False))
                self.assertEqual(len(hwp.executed), 1)
                self.assert_held(owner, hwp, result, mutated=True, phase=phase)
                self.assertEqual(result['steps'][-1]['failure_diagnostic'].get('step_reason'), reason, result['steps'][-1])


if __name__ == '__main__':
    unittest.main()
