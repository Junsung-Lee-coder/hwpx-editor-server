"""Model-only raw SDK for source regressions; NOT observed Hancom behavior."""
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from tests.native_format_fakes import char, paragraph, textbox
from tests.native_header_footer_fakes import HeaderFooterHwp
from tests.native_text_fakes import _TextHAction, _atom_index


def container(kind='text_box', text='Box text', ident='101'):
    return {**textbox(paragraph(text)), 'container_kind': kind, 'native_id': ident, 'width': 11339, 'height': 5669}


class Ctrl:
    def __init__(self, kind, ident):
        self.CtrlID, self.ident = kind, ident
    def GetCtrlInstID(self):
        # Section/other controls do not promise a target-selector instance ID.
        return self.ident if self.CtrlID in ('gso', 'fn') else ''


class InsertSet:
    def __init__(self):
        self.HSet = self
        self.Text = ''
    @property
    def items(self):
        return {'Text': self.Text}


class Array:
    def __init__(self, count):
        self.values = [0] * count
        self.Count = count
    def SetItem(self, index, value):
        self.values[index] = value
    def Item(self, index):
        return self.values[index]


class Layout:
    def CreateItemArray(self, name, count):
        self.CreatePt = Array(count)


class Shape:
    def __init__(self):
        self.HSet = self
        self.ShapeDrawLayOut = Layout()
    def SetItem(self, name, value):
        setattr(self, name, value)


class Actions(_TextHAction):
    def GetDefault(self, name, hset):
        self.hwp.calls.append(('GetDefault', name))
        return True
    def Execute(self, name, hset):
        if name != 'DrawObjCreatorTextBox':
            return super().Execute(name, hset)
        self.hwp.calls.append(('Execute', name))
        item = container(text='', ident='999')
        self.hwp._created_anchor = self.hwp.cursor
        item['width'], item['height'] = hset.Width, hset.Height
        item['width_rel_to'] = {0:'Paper',1:'Page',2:'Column',3:'Para',4:'Absolute'}[hset.WidthRelTo]
        item['height_rel_to'] = {0:'Paper',1:'Page',2:'Absolute'}[hset.HeightRelTo]
        # Returned drawing points come from the actual prepared array, not just
        # declared SIZE. This is a model contract, never a native observation.
        item['points'] = list(hset.ShapeDrawLayOut.CreatePt.values)
        item['point_count'] = hset.ShapeDrawLayOut.CreateNumPt
        para = self.hwp.body[self.hwp.cursor[1]]
        para['atoms'].insert(_atom_index(para, self.hwp.cursor[2]), {'cs': char(), **item})
        self.hwp._number_cells(self.hwp.body)
        # The new control is not necessarily the last native list: an existing
        # later body drawing may own that list. Bind this SDK model's own newly
        # generated instance, independently of production _binding/list search.
        created_id = getattr(self.hwp, 'native_ids', {}).get('999', '999')
        created_lists = [lid for lid, ctrl in self.hwp.owners.items()
                         if ctrl is not None and ctrl.CtrlID == 'gso' and ctrl.GetCtrlInstID() == created_id]
        assert len(created_lists) == 1
        self.hwp._created_list = created_lists[0]
        self.hwp._selected_container = self.hwp.owners[self.hwp._created_list]
        self.hwp.cursor = (self.hwp.cursor[0], self.hwp.cursor[1], self.hwp.cursor[2] + 8)
        self.hwp.IsModified = True
        return True

    def Run(self, name):
        if name in ('Cancel', 'SelectCtrlFront', 'ShapeObjTextBoxEdit'):
            return self.hwp._container_run(name)
        return super().Run(name)


class ContainersHwp(HeaderFooterHwp):
    @property
    def SelectionMode(self):
        return 4 if getattr(self, '_selected_container', None) is not None else super().SelectionMode

    @property
    def CurSelectedCtrl(self):
        return getattr(self, '_selected_container', None)

    def _container_run(self, name):
        self.calls.append(('Run', name))
        if name == 'Cancel':
            self._selected_container = None
            self.selection = None
        elif name == 'SelectCtrlFront':
            assert self.SelectionMode == 0 and self.CurSelectedCtrl is None
            assert self.cursor == self._created_anchor
            self._selected_container = self.owners[self._created_list]
        elif name == 'ShapeObjTextBoxEdit':
            assert self.SelectionMode == 4 and self.CurSelectedCtrl is self.owners[self._created_list]
            self._selected_container = None
            self.selection = None
            self.cursor = (self._created_list, 0, 0)
        else:
            raise AssertionError('Undeclared container action')
        return True

    def __init__(self, body=None):
        super().__init__(body or [paragraph('Body sentinel'), paragraph('Anchor'), paragraph('Tail sentinel', container())], areas=[])
        self.hwp = self
        self.HParameterSet.HInsertText = InsertSet()
        self.HParameterSet.HShapeObject = Shape()
        self.HAction = Actions(self)
        self._number_cells(self.body)

    def _number_cells(self, paragraphs):
        if getattr(self, '_building', False) or paragraphs is not self.body:
            return
        self.lists, self.owners = {0: self.body, 1: [paragraph('')]}, {1: Ctrl('secd', '1')}
        def visit(items):
            for p in items:
                for a in p['atoms']:
                    if 'drawtext' in a:
                        lid = len(self.lists)
                        self.lists[lid] = a['drawtext']
                        ident = a.get('native_id', '101')
                        self.owners[lid] = Ctrl('fn' if a.get('container_kind') == 'footnote' else 'gso', getattr(self, 'native_ids', {}).get(ident, ident))
                        visit(a['drawtext'])
        visit(self.body)

    def GetTextFile(self, fmt, option):
        source = super().GetTextFile(fmt, option)
        if not option:
            self._identity_xml = source
        return source

    @property
    def HeadCtrl(self):
        from tests.native_container_identity_models import chain
        return chain(self)

    @property
    def ParentCtrl(self):
        self.calls.append(('ParentCtrl',))
        return self.owners.get(self.cursor[0])

    def GetPos(self):
        self.calls.append(('GetPos',))
        return self.get_pos()
    def GetSelectedPos(self):
        self.calls.append(('GetSelectedPos',))
        return self.get_selected_pos()
    def SetPos(self, lid, para, pos):
        return self.set_pos(lid, para, pos)
    def SelectText(self, spara, spos, epara, epos):
        return self.select_text(spara, spos, epara, epos, self.cursor[0])

    def _p(self, p, chars, paras, atoms=None):
        root = ET.fromstring(super()._p(p, chars, paras, atoms=atoms))
        for a in (atoms if atoms is not None else p['atoms']):
            if 'drawtext' not in a:
                continue
            rect = next(e for e in root.iter('RECTANGLE') if e.find('SHAPEOBJECT') is None)
            ident = a.get('native_id', '101')
            kind = a.get('container_kind', 'text_box')
            draw = rect.find('DRAWINGOBJECT')
            text = draw.find('DRAWTEXT')
            lists = text.find('PARALIST')
            if kind == 'footnote':
                rect.tag = 'FOOTNOTE'
                rect.attrib = {'InstId': ident, 'Number': '1'}
                rect[:] = [lists]
                continue
            width, height = a.get('width', 11339), a.get('height', 5669)
            points = a.get('points', [0, 0, width, 0, width, height, 0, height])
            for i in range(min(a.get('point_count', 4), len(points)//2, 4)):
                rect.set('X'+str(i), str(points[i*2]))
                rect.set('Y'+str(i), str(points[i*2+1]))
            # Authenticated saved-native constructor DATA explicitly emits Lock=false.
            frame = ET.Element('SHAPEOBJECT', {'InstId': ident, 'Lock': 'false'})
            ET.SubElement(frame, 'SIZE', {'Width':str(width), 'Height':str(height), 'WidthRelTo':a.get('width_rel_to','Absolute'), 'HeightRelTo':a.get('height_rel_to','Absolute')})
            ET.SubElement(frame, 'POSITION', {'TreatAsChar':'true'})
            rect.insert(0, frame)
            comp = ET.Element('SHAPECOMPONENT', {'GroupLevel':'0', 'InstID':str(int(ident)+1000), 'OriWidth':str(width), 'OriHeight':str(height),
                                               'CurWidth':str(width), 'CurHeight':str(height)})
            render = ET.SubElement(comp, 'RENDERINGINFO')
            for tag in ('TRANSMATRIX', 'SCAMATRIX', 'ROTMATRIX'):
                ET.SubElement(render, tag, {f'E{i}':str(value) for i,value in enumerate((1,0,0,0,1,0),1)})
            draw.insert(0, comp)
            lists.set('TextDirection', '0')
            if kind == 'caption':
                draw.remove(text)
                text.tag = 'CAPTION'
                frame.append(text)
        return ET.tostring(root, encoding='unicode')
