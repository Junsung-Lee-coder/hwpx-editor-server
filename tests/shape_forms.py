"""Hancom 2024 HWPML2X forms of a new drawing object, for the synthetic SDKs and verifier tests.

``drawn_xml`` returns the new object as Hancom read it back in the owned characterization run for the same prepared
values (tests/test_shape_drawing.py checks it against every pinned readback in tests/fixtures/native_shapes). Without
the drawing subsets it returns what Hancom drew then: no points, non-finite or zero scales, no outline.
"""
from __future__ import annotations

from typing import Any

OUTLINE = {'style': 1, 'Width': 85, 'Color': 0, 'Alpha': 0, 'OutLineStyle': 0}
COMMENT = {'RECTANGLE': '사각형입니다.', 'ELLIPSE': '타원입니다.', 'LINE': '선입니다.'}


def points_for(name: str, width: int, height: int) -> tuple[int, list[int]]:
    """CreateNumPt and CreatePt values of the characterized layouts (independent of the engine)."""
    if name == 'rectangle':
        return 4, [0, 0, width, 0, width, height, 0, height]
    if name == 'ellipse':
        return 3, [width // 2, height // 2, width, height // 2, width // 2, 0]
    return 2, [0, 0, width, 0] if name == 'line_h' else [0, 0, 0, height]


def _f(value: float) -> str:
    return f'{value:.6f}'


def drawn_xml(tag: str, state: dict[str, Any], faults: frozenset[str] = frozenset()) -> str:
    """The new drawing object as Hancom 2024 reads it back for the prepared ``state`` (InstId/ZOrder fixed)."""
    width, height, treat = state['Width'], state['Height'], bool(state['TreatAsChar'])
    points = state['CreatePt'] if state['CreatePt'] is not None and state['CreateNumPt'] else None
    if 'ignores_points' in faults:
        points = None
    line = dict(state['line'])
    if 'ignores_line_attr' in faults:
        line = {name: 0 for name in line}
    identity = (1, 0, 0, 0, 1, 0)
    cur: tuple[int, int] | None = (width, height)
    if tag == 'RECTANGLE':
        xs, ys = (points[0::2], points[1::2]) if points else ([0] * 4, [0] * 4)
        head = f'<RECTANGLE Ratio="0" ' + ' '.join(f'X{i}="{x}"' for i, x in enumerate(xs)) + ' ' + ' '.join(f'Y{i}="{y}"' for i, y in enumerate(ys)) + '>'
        ori, sca, cur = (width, height), identity, (None if points else (0, 0))
        center = (width // 2, height // 2)
    elif tag == 'ELLIPSE':
        cx, cy, a1x, a1y, a2x, a2y = points if points else [0] * 6
        ori = (2 * (a1x - cx), 2 * (cy - a2y))
        sca = (width / ori[0] if ori[0] else float('inf'), 0, 0, 0, height / ori[1] if ori[1] else float('inf'), 0)
        head = (f'<ELLIPSE ArcType="Normal" Axis1X="{a1x}" Axis1Y="{a1y}" Axis2X="{a2x}" Axis2Y="{a2y}" CenterX="{cx}" CenterY="{cy}" '
                'End1X="0" End1Y="0" End2X="0" End2Y="0" HasArcProperty="false" IntervalDirty="false" Start1X="0" Start1Y="0" Start2X="0" Start2Y="0">')
        center = (width // 2, height // 2)
    else:
        ori = (100, 100)
        if points:
            sca = ((points[2] - points[0]) / 100, 0, 0, 0, (points[3] - points[1]) / 100, 0)
        else:
            sca = (float('inf'), 0, 0, 0, float('nan'), 0)
        head = '<LINE EndX="100" EndY="100" IsReverseHV="false" StartX="0" StartY="0">'
        center = (width // 2, height // 2)
    matrix = lambda name, e: f'<{name} ' + ' '.join(f'E{i + 1}="{_f(v)}"' for i, v in enumerate(e)) + '/>'
    cur_attrs = f'CurHeight="{cur[1]}" CurWidth="{cur[0]}" ' if cur is not None else ''
    if line['style']:
        ends = ' HeadSize="SmallSmall"' if tag == 'LINE' else ''
        tails = ' TailSize="SmallSmall"' if tag == 'LINE' else ''
        color = f' Color="{line["Color"]}"' if line['Color'] else ''
        lineshape = f'<LINESHAPE Alpha="{line["Alpha"]}"{color} EndCap="Round"{ends} Style="Solid"{tails} Width="{line["Width"]}"/>'
    else:
        lineshape = '<LINESHAPE Alpha="0" EndCap="Round" Style="None"/>'
    return (
        f'{head}<SHAPEOBJECT InstId="1" Lock="false" NumberingType="None" TextWrap="TopAndBottom" ZOrder="1">'
        f'<SIZE Height="{height}" HeightRelTo="Absolute" Protect="false" Width="{width}" WidthRelTo="Absolute"/>'
        '<POSITION AffectLSpacing="false" AllowOverlap="false" FlowWithText="true" HoldAnchorAndSO="false" HorzAlign="Left" '
        f'HorzOffset="0" HorzRelTo="Column" TreatAsChar="{"true" if treat else "false"}" VertAlign="Top" VertOffset="0" VertRelTo="Para"/>'
        f'<OUTSIDEMARGIN Bottom="0" Left="0" Right="0" Top="0"/><SHAPECOMMENT>{COMMENT[tag]}</SHAPECOMMENT></SHAPEOBJECT>'
        f'<DRAWINGOBJECT><SHAPECOMPONENT {cur_attrs}GroupLevel="0" HorzFlip="false" InstID="1" OriHeight="{ori[1]}" OriWidth="{ori[0]}" '
        f'VertFlip="false" XPos="0" YPos="0"><ROTATIONINFO Angle="0" CenterX="{center[0]}" CenterY="{center[1]}" Rotate="1"/>'
        f'<RENDERINGINFO>{matrix("TRANSMATRIX", identity)}{matrix("SCAMATRIX", sca)}{matrix("ROTMATRIX", identity)}</RENDERINGINFO>'
        f'</SHAPECOMPONENT>{lineshape}<SHADOW Alpha="0" Color="11711154" OffsetX="0" OffsetY="0" Type="0"/></DRAWINGOBJECT></{tag}>'
    )
