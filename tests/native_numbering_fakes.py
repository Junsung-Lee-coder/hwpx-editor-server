"""Mock Hancom objects for the page-number and official-numbering tests.

These fakes model the documented call shapes only (HAction.GetDefault/Execute/Run,
HParameterSet.HPageNumPos / HParaShape, HeadCtrl->Next control chains and the
toggle behaviour of PutOutlineNumber). They are not Hancom evidence.
"""
from __future__ import annotations

from typing import Any


class FakeProps:
    def __init__(self, values: dict[str, Any] | None, *, readable: bool = True) -> None:
        self._values = dict(values or {})
        self._readable = readable

    def Item(self, name: str) -> Any:  # noqa: N802 - mirrors the COM member name
        if not self._readable:
            raise RuntimeError('Properties are not readable')
        return self._values[name]


class FakeCtrl:
    def __init__(self, ctrl_id: str, props: FakeProps | None = None) -> None:
        self.CtrlID = ctrl_id
        self.Properties = props
        self.Next: FakeCtrl | None = None


def chain(controls: list[FakeCtrl]) -> FakeCtrl | None:
    for left, right in zip(controls, controls[1:]):
        left.Next = right
    if controls:
        controls[-1].Next = None
    return controls[0] if controls else None


class _PageNumPosSet:
    def __init__(self) -> None:
        self.HSet = self
        self.DrawPos: Any = None
        self.NumberFormat: Any = None
        self.NewNumber: Any = None
        self.SideChar: Any = None


class _ParaShapeSet:
    def __init__(self) -> None:
        self.HSet = self
        self.HeadingType: Any = None
        self.Level: Any = None


class _OutlineShapeSet:
    pass


class _SecDefSet:
    def __init__(self) -> None:
        self.HSet = self
        self.ApplyClass: Any = None
        self.ApplyTo: Any = None
        self.OutlineShape = _OutlineShapeSet()


class _ParameterRoot:
    def __init__(self) -> None:
        self.HPageNumPos = _PageNumPosSet()
        self.HParaShape = _ParaShapeSet()
        self.HSecDef = _SecDefSet()


# Hancom's stock outline: 1. -> 가. -> 1) -> 가) (the first four levels are the official-document system).
OFFICIAL_SHAPE_LEVELS = (('^1.', 0), ('^2.', 8), ('^3)', 0), ('^4)', 8))
STOCK_TAIL_LEVELS = (('(^5)', 0), ('(^6)', 8), ('^7', 1))


def shape_items(levels: tuple[tuple[str, int], ...]) -> dict[str, Any]:
    items: dict[str, Any] = {}
    for index, (text, number_format) in enumerate(levels):
        items[f'StrFormatLevel{index}'] = text
        items[f'NumFormatLevel{index}'] = number_format
    return items


def stock_shape() -> dict[str, Any]:
    return shape_items(OFFICIAL_SHAPE_LEVELS + STOCK_TAIL_LEVELS)


class FakeHwp:
    """Document with a flat control chain, flat text and a paragraph list."""

    def __init__(
        self,
        *,
        paragraphs: list[dict[str, Any]] | None = None,
        controls: list[FakeCtrl] | None = None,
    ) -> None:
        self.paragraphs = paragraphs if paragraphs is not None else [{'text': 'body', 'heading_type': 0, 'level': 0}]
        self.controls = controls if controls is not None else [FakeCtrl('secd')]
        self.cursor = (0, 0, 0)
        self.selection_para: int | None = None
        self.search_from = 0
        self.calls: list[tuple[Any, ...]] = []
        self.HParameterSet = _ParameterRoot()
        self.HAction = _FakeHAction(self)
        self.text_available = True
        self.enumeration_available = True
        self.fail_set_pos = False
        self.page_number_defaults: dict[str, Any] = {}
        self.outline_shape: dict[str, Any] = stock_shape()
        self.other_section_shape: dict[str, Any] = stock_shape()
        self.outline_mode = 'apply'
        self.outline_readback = 'ok'
        self.outline_unreadable = False
        self.package_state: dict[str, Any] = {
            'header_footer': [],
            'styles': 'styles-0',
            'numberings': 'numberings-0',
            'bindata': ['bin-0'],
            'charshapes': 'charshapes-0',
            'parashapes': 'parashapes-0',
            'frame': 'frame-0',
        }
        self.census_error: Exception | None = None

    def census(self) -> dict[str, Any]:
        """Document-level protection census (v2) as the live HWPML2X reader would report it."""
        if self.census_error is not None:
            raise self.census_error
        headings = {0: 'NONE', 1: 'OUTLINE', 2: 'NUMBER', 3: 'BULLET'}
        import hashlib

        def digest(value: str) -> str:
            return hashlib.sha256(value.encode('utf-8')).hexdigest()

        def entry(value: str, shell: str | None = None) -> dict[str, str]:
            sha = digest(value)
            return {'sha': sha, 'shell': digest(shell) if shell is not None else sha}

        def heads(shape: dict[str, Any]) -> str:
            return '|'.join(f'{shape.get(f"StrFormatLevel{i}")}:{shape.get(f"NumFormatLevel{i}")}' for i in range(4))

        state = self.package_state
        numbering_shell = f'{state["numberings"]}'
        paragraphs = []
        for p in self.paragraphs:
            section = int(p.get('section', 0))
            fmt = str(p.get('format', 'fmt-0'))
            text = str(p['text'])
            paragraphs.append(
                {
                    'section': section,
                    'text_sha256': digest(text),
                    'text': text,
                    'heading': headings[p['heading_type']],
                    'level': int(p['level']),
                    'sha256': digest(f'{text}|{p["heading_type"]}|{p["level"]}|{section}|{fmt}'),
                    'masked_sha256': digest(f'{text}|{section}|{fmt}'),
                    'stripped_sha256': digest(f'{text}|{p["heading_type"]}|{p["level"]}|{section}|{fmt}'),
                }
            )
        return {
            'header_footer': [digest(str(item)) for item in state['header_footer']],
            'body_sha256': digest('\n'.join(str(p['text']) for p in self.paragraphs)),
            'styles_sha256': digest(str(state['styles'])),
            'numberings_sha256': digest(str(state['numberings'])),
            'bindata_sha256': digest('|'.join(state['bindata'])),
            'page_number_count': sum(1 for ctrl in self.controls if ctrl.CtrlID == 'pgnp'),
            'paragraphs': paragraphs,
            'definitions': {
                'charshapelist': [entry(str(state['charshapes']))],
                'parashapelist': [entry(str(state['parashapes']))],
                'stylelist': [entry(str(state['styles']))],
                'numberinglist': [entry(f'{numbering_shell}#{heads(self.outline_shape)}', numbering_shell)],
                'bindatalist': [entry(str(item)) for item in state['bindata']],
            },
            'frame': {'sha256': digest(str(state['frame'])), 'stripped_sha256': digest(str(state['frame']))},
            'outline_shapes': [digest(heads(self.outline_shape)), digest(heads(self.other_section_shape))],
        }

    @property
    def HeadCtrl(self) -> FakeCtrl | None:  # noqa: N802
        if not self.enumeration_available:
            raise AttributeError('HeadCtrl')
        return chain(self.controls)

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> str:
        if not self.text_available:
            raise RuntimeError('GetTextFile unavailable')
        self.calls.append(('get_text_file', fmt, option))
        return '\r\n'.join(str(item['text']) for item in self.paragraphs)

    def get_pos(self) -> tuple[int, int, int]:
        return self.cursor

    def set_pos(self, list_id: int, para: int, pos: int) -> None:
        if self.fail_set_pos:
            raise RuntimeError('SetPos failed')
        self.calls.append(('set_pos', list_id, para, pos))
        self.cursor = (list_id, para, pos)

    def MoveDocBegin(self) -> None:  # noqa: N802
        self.calls.append(('MoveDocBegin',))
        self.cursor = (0, 0, 0)
        self.search_from = 0
        self.selection_para = None

    def MoveRight(self) -> None:  # noqa: N802
        self.calls.append(('MoveRight',))
        if self.selection_para is not None:
            self.search_from = self.selection_para + 1
        self.selection_para = None

    def find(self, text: str, direction: str = 'Forward', **_kwargs: Any) -> bool:
        for index in range(self.search_from, len(self.paragraphs)):
            if text in str(self.paragraphs[index]['text']):
                self.cursor = (0, index, 0)
                self.selection_para = index
                return True
        self.selection_para = None
        return False

    def get_selected_pos(self) -> tuple[Any, ...]:
        if self.selection_para is None:
            return (False, 0, 0, 0, 0, 0, 0)
        para = self.selection_para
        return (True, 0, para, 0, 0, para, len(str(self.paragraphs[para]['text'])))

    def select_text(self, spara: int, spos: int, epara: int, epos: int, list_id: int = 0) -> None:
        self.calls.append(('select_text', spara, spos, epara, epos, list_id))
        self.selection_para = spara
        self.cursor = (list_id, spara, 0)

    def current_paragraph(self) -> dict[str, Any]:
        return self.paragraphs[self.cursor[1]]


class _FakeHAction:
    def __init__(self, owner: FakeHwp) -> None:
        self.owner = owner
        self.get_default_result: bool | None = True
        self.execute_result: bool | None = True
        self.run_result: bool | None = True
        self.execute_raises: Exception | None = None
        self.run_raises: Exception | None = None
        self.page_number_mode = 'insert'
        self.run_mode = 'toggle'
        self.shape_mode = 'apply'
        self.execute_text_edit: str | None = None
        self.shape_execute_level_offset = 0
        self.heading_unreadable = False

    def GetDefault(self, name: str, hset: Any) -> bool | None:  # noqa: N802
        self.owner.calls.append(('GetDefault', name))
        if self.get_default_result is False:
            return False
        if name == 'ParagraphShape' and not self.heading_unreadable:
            para = self.owner.current_paragraph()
            hset.HeadingType = para['heading_type']
            hset.Level = para['level']
        if name == 'PageNumPos':
            for key, value in self.owner.page_number_defaults.items():
                setattr(hset, key, value)
        if name == 'OutlineNumber':
            if self.owner.outline_unreadable:
                return False
            shape = hset.OutlineShape
            for key, value in self.owner.outline_shape.items():
                if self.owner.outline_readback == 'missing' and key == 'NumFormatLevel1':
                    continue
                if self.owner.outline_readback == 'bool' and key == 'NumFormatLevel1':
                    value = True
                if self.owner.outline_readback == 'str' and key == 'NumFormatLevel1':
                    value = str(value)
                if self.owner.outline_readback == 'float' and key == 'NumFormatLevel1':
                    value = float(value)
                setattr(shape, key, value)
        return self.get_default_result

    def Execute(self, name: str, hset: Any) -> bool | None:  # noqa: N802
        owner = self.owner
        owner.calls.append(('Execute', name, {k: getattr(hset, k) for k in vars(hset) if k != 'HSet'}))
        if self.execute_raises is not None:
            raise self.execute_raises
        if self.execute_result is False:
            return False
        if name == 'PageNumPos':
            values = {
                'DrawPos': hset.DrawPos,
                'NewNumber': hset.NewNumber,
                'SideChar': hset.SideChar,
                'NumberFormat': hset.NumberFormat,
            }
            if self.page_number_mode == 'insert':
                owner.controls.append(FakeCtrl('pgnp', FakeProps(values)))
            elif self.page_number_mode == 'insert-unreadable':
                owner.controls.append(FakeCtrl('pgnp', FakeProps(values, readable=False)))
            elif self.page_number_mode == 'duplicate':
                owner.controls.append(FakeCtrl('pgnp', FakeProps(values)))
                owner.controls.append(FakeCtrl('pgnp', FakeProps(values)))
            elif self.page_number_mode == 'wrong-values':
                owner.controls.append(FakeCtrl('pgnp', FakeProps({**values, 'DrawPos': 1})))
            elif self.page_number_mode == 'noop':
                pass
            if self.execute_text_edit is not None:
                owner.paragraphs[0]['text'] = self.execute_text_edit
        if name == 'OutlineNumber':
            requested = {k: v for k, v in vars(hset.OutlineShape).items()}
            if self.owner.outline_mode == 'apply':
                if hset.ApplyTo == 2:
                    owner.outline_shape.update(requested)
                elif hset.ApplyTo == 3:
                    owner.outline_shape.update(requested)
                    owner.other_section_shape.update(requested)
            elif self.owner.outline_mode == 'partial':
                owner.outline_shape.update({k: v for k, v in requested.items() if k.startswith('StrFormat')})
        if name == 'ParagraphShape' and self.shape_mode != 'noop':
            para = owner.current_paragraph()
            if hset.HeadingType is not None:
                para['heading_type'] = int(hset.HeadingType)
            para['level'] = int(hset.Level) + self.shape_execute_level_offset
            if self.shape_mode == 'prefix-text':
                para['text'] = '1. ' + str(para['text'])
            if self.execute_text_edit is not None:
                para['text'] = self.execute_text_edit
        return self.execute_result

    def Run(self, name: str) -> bool | None:  # noqa: N802
        owner = self.owner
        owner.calls.append(('Run', name))
        if self.run_raises is not None:
            raise self.run_raises
        if self.run_result is False:
            return False
        if name == 'PutOutlineNumber':
            para = owner.current_paragraph()
            if self.run_mode == 'toggle':
                para['heading_type'] = 0 if para['heading_type'] == 1 else 1
            elif self.run_mode == 'noop':
                pass
            elif self.run_mode == 'prefix-text':
                para['heading_type'] = 1
                para['text'] = '1. ' + str(para['text'])
            if self.execute_text_edit is not None:
                para['text'] = self.execute_text_edit
        return self.run_result
