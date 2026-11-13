"""Read-side container admission/geometry. Never edits XML or calls an SDK.

HWPML revision 1.2 tables 96, 104, 116--119, 134 are authoritative for
GroupLevel=0, absolute SIZE and horizontal PARALIST defaults. Geometry follows
those tables and the retained native_shapes rectangle readback, NOT a render
claim. Missing transform values have no guessed identity default.
"""
import math
import re
from app.native_numbering_census import CensusError, _name


def attribute(element, name, default=None):
    values = [(k, v) for k, v in element.attrib.items() if k.rsplit('}', 1)[-1].lower() == name.lower()]
    if not values:
        return default
    if len(values) != 1 or values[0][0] != name:
        raise CensusError('Ambiguous or noncanonical container attribute: ' + name)
    return values[0][1]


def one(parent, tag):
    children = [e for e in parent if _name(e) == tag.lower()]
    if len(children) != 1:
        raise CensusError('Container child is missing or ambiguous: ' + tag)
    return children[0]


def number(element, name, *, default=None, integer=False):
    value = attribute(element, name, default)
    if value is None:
        raise CensusError('Required container numeric evidence is absent: ' + name)
    if integer:
        if re.fullmatch(r'-?(0|[1-9][0-9]*)', value) is None:
            raise CensusError('Container integer is malformed: ' + name)
        parsed = int(value)
        if not -2**31 <= parsed < 2**31:
            raise CensusError('Container integer exceeds LONG: ' + name)
        return parsed
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as exc:
        raise CensusError('Container number is malformed: ' + name) from exc
    if not math.isfinite(parsed):
        raise CensusError('Container number is nonfinite: ' + name)
    return parsed


def component(owner):
    if _name(owner) == 'picture':
        return one(owner, 'SHAPECOMPONENT')
    return one(one(owner, 'DRAWINGOBJECT'), 'SHAPECOMPONENT')


def grouping_problem(owner, kind):
    # Footnotes are not drawing components; never invent SHAPECOMPONENT for fn.
    if kind == 'footnote':
        return None
    part = component(owner)
    level = number(part, 'GroupLevel', default='0', integer=True)
    if level < 0:
        raise CensusError('Negative component GroupLevel.')
    # SHAPEOBJECT is not the authority. A shadow may not contradict it.
    shape = one(owner, 'SHAPEOBJECT')
    shadow = attribute(shape, 'GroupLevel')
    if shadow is not None and (not re.fullmatch(r'0|[1-9][0-9]*', shadow) or int(shadow) != level):
        raise CensusError('Conflicting component/shape grouping evidence.')
    return 'container_grouped_owner' if level else None


def prove_created_geometry(owner, width, height):
    """Exact prepared rectangle and effective noncollapsed horizontal frame.

    CurWidth/Height may both be absent in retained Hancom rectangle readback;
    then extents are derived from the explicit original canvas and matrices,
    not from a guessed current-size default. If present both must agree.
    """
    if _name(owner) != 'rectangle' or grouping_problem(owner, 'text_box'):
        raise CensusError('Created text box is not an ungrouped rectangle.')
    shape = one(owner, 'SHAPEOBJECT')
    size, position = one(shape, 'SIZE'), one(shape, 'POSITION')
    if (number(size, 'Width', integer=True), number(size, 'Height', integer=True)) != (width, height):
        raise CensusError('Created text box SIZE differs from requested HWPUNIT size.')
    if attribute(size, 'WidthRelTo', 'Absolute') != 'Absolute' or attribute(size, 'HeightRelTo', 'Absolute') != 'Absolute':
        raise CensusError('Created text box SIZE is not absolute HWPUNIT.')
    if attribute(position, 'TreatAsChar', 'false') != 'true':
        raise CensusError('Created text box is not inline.')
    points = [(number(owner, 'X'+str(i), integer=True), number(owner, 'Y'+str(i), integer=True)) for i in range(4)]
    if points != [(0, 0), (width, 0), (width, height), (0, height)] or width <= 0 or height <= 0:
        raise CensusError('Created rectangle points do not span the requested frame.')
    part = component(owner)
    original = (number(part, 'OriWidth', integer=True), number(part, 'OriHeight', integer=True))
    if original != (width, height):
        raise CensusError('Created rectangle original extents differ from request.')
    for field in ('HorzFlip', 'VertFlip'):
        if attribute(part, field, 'false') != 'false':
            raise CensusError('Created text box is flipped or malformed.')
    if number(part, 'XPos', default='0', integer=True) != 0 or number(part, 'YPos', default='0', integer=True) != 0:
        raise CensusError('Created component has an unexpected group offset.')
    render = one(part, 'RENDERINGINFO')
    identity = [1., 0., 0., 0., 1., 0.]
    matrices = {}
    for tag in ('TRANSMATRIX', 'SCAMATRIX', 'ROTMATRIX'):
        matrix = one(render, tag)
        matrices[tag] = [number(matrix, 'E'+str(i)) for i in range(1, 7)]
    # This narrow creation request is unrotated and unscaled, not an arbitrary
    # drawing transform. Nonfinite/singular/zero-scale transforms cannot pass.
    if any(matrix != identity for matrix in matrices.values()):
        raise CensusError('Created text box has a collapsed or unsupported effective transform.')
    current = [attribute(part, key) for key in ('CurWidth', 'CurHeight')]
    if any(value is not None for value in current):
        if (number(part, 'CurWidth', integer=True), number(part, 'CurHeight', integer=True)) != (width, height):
            raise CensusError('Created text box current extents differ from request.')
    rotation = [e for e in part if _name(e) == 'rotationinfo']
    if len(rotation) > 1 or (rotation and number(rotation[0], 'Angle', default='0') != 0):
        raise CensusError('Created text box is rotated or has ambiguous rotation.')
    drawtext = one(one(owner, 'DRAWINGOBJECT'), 'DRAWTEXT')
    paragraphs = one(drawtext, 'PARALIST')
    if attribute(paragraphs, 'TextDirection', '0') != '0':
        raise CensusError('Created text box is not horizontal.')
    # Genuine unlinked constructor HWPML emits the exact pair ('0', '0').
    # No integer coercion/default: partial, noncanonical and nonzero links stay
    # unsupported. This does not change any geometry/extent/transform proof.
    links = tuple(attribute(paragraphs, key) for key in ('LinkListID', 'LinkListIDNext'))
    if links != ('0', '0') and any(value not in (None, '') for value in links):
        raise CensusError('Linked text-box creation is unsupported.')
