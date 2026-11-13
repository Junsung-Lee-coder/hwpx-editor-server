"""Coherent R1--R5 regressions through real service + public projection.

SDK geometry/position evidence is modeled, not native qualification. The R1
oracle is the review's corrected distinct-Id 1/2 + Count=2 + real Outline-use
pair; no duplicated/missing-ID helper is used.
"""
from types import MethodType
import unittest
import xml.etree.ElementTree as E
from app.local_cli_service import LocalCliService
from tests.native_text_containers_fakes import ContainersHwp, container
from tests.native_format_fakes import paragraph, shape
from tests.test_native_paragraph_edit import Flow
from tests.test_native_text_containers import listing

CREATE = {'op':'text_box_create','text':'Caller supplied text','width_mm':40,'height_mm':20,
          'expected_pos':[0,1,0],'confirm_layout':True}


def flow(hwp):
    result = Flow(hwp)
    result.service._public_bundle_steps = MethodType(LocalCliService._public_bundle_steps, result.service)
    return result


def edit_request(item):
    return {'op':'text_container_text_exact','target_id':item['target_id'],'expected_hash':item['hash'],
            'expected_text':item['text'],'new_text':'Replacement text','confirm_layout':True}


def creation_doc():
    hwp = ContainersHwp([paragraph('Body sentinel'),paragraph('Anchor'),paragraph('Tail sentinel')])
    hwp.cursor = (0,1,0)
    return hwp


def root_read_hook(hwp, change, *, post_creation=False):
    original = hwp.GetTextFile
    captured = []
    def read(fmt, option):
        text = original(fmt, option)
        if option or (post_creation and ('Execute','DrawObjCreatorTextBox') not in hwp.calls):
            return text
        root = E.fromstring(text)
        change(root)
        text = E.tostring(root, encoding='unicode')
        captured.append(text)
        if post_creation:
            hwp.calls.append(('POST_CREATION_EVIDENCE',))
        return text
    hwp.GetTextFile = read
    return captured


def outline_pair(hwp, corrupt):
    hwp.body[0]['shape'] = shape(HeadingType='Outline')
    def change(root):
        nums = next(e for e in root.iter() if e.tag == 'NUMBERINGLIST')
        assert len(nums) == 1 and nums[0].get('Id') == '1'
        added = E.fromstring(E.tostring(nums[0])); added.set('Id','2')
        next(e for e in added if e.tag == 'PARAHEAD').set('Start','7')
        nums.append(added); nums.set('Count','2')
        next(e for e in root.iter() if e.tag == 'SECDEF').set('OutlineShape','2' if corrupt and ('Run','Delete') in hwp.calls else '1')
        ids = [e.get('Id') for e in nums]
        assert ids == ['1','2'] and len(set(ids)) == 2 and nums.get('Count') == '2'
        section = root.find('BODY/SECTION')
        p = section.find('P')
        para = next(e for e in root.iter() if e.tag == 'PARASHAPE' and e.get('Id') == p.get('ParaShape'))
        assert para.get('HeadingType') == 'Outline'
    return root_read_hook(hwp, change)


class OutlinePreservation(unittest.TestCase):
    def check(self, corrupt):
        hwp = ContainersHwp(); captured = outline_pair(hwp, corrupt)
        item = listing(hwp)[0]
        reply = flow(hwp).step(**edit_request(item))
        before, after = E.fromstring(captured[0]), E.fromstring(captured[-1])
        self.assertEqual(E.tostring(next(e for e in before.iter() if e.tag=='NUMBERINGLIST')),
                         E.tostring(next(e for e in after.iter() if e.tag=='NUMBERINGLIST')))
        self.assertEqual(next(e for e in before.iter() if e.tag=='SECDEF').get('OutlineShape'),'1')
        self.assertEqual(next(e for e in after.iter() if e.tag=='SECDEF').get('OutlineShape'),'2' if corrupt else '1')
        self.assertEqual(reply['ok'], not corrupt, reply)
        if corrupt:
            self.assertTrue(reply['steps'][0]['mutation_may_have_persisted'], reply)
    def test_valid_outline_use_unchanged(self):
        self.check(False)
    def test_distinct_existing_outline_binding_change_holds(self):
        self.check(True)


class RawSelectionRepresentation(unittest.TestCase):
    def test_canonical_no_block_positive(self):
        hwp = creation_doc()
        raw = hwp.GetSelectedPos
        hwp.GetSelectedPos = lambda: raw() if hwp.selection is not None else (False, None, None, None, None, None, None)
        reply = flow(hwp).step(**CREATE)
        self.assertTrue(reply['ok'], reply)

    def test_each_coordinate_and_malformed_shape_latches_at_first_read(self):
        values = [None, (), (1,0,0,0,0,0,0), (True,0,0,0,0,0)]
        for index in range(1,7):
            for bad in (2**31, 2**32, -1, True, 0., None, '0'):
                value = [True,0,0,0,0,0,0]; value[index] = bad
                values.append(tuple(value))
        for value in values:
            with self.subTest(value=value):
                hwp = creation_doc()
                def malformed():
                    hwp.calls.append(('SELECTION_FAULT',))
                    return value
                hwp.GetSelectedPos = malformed
                reply = flow(hwp).step(**CREATE)
                self.assertFalse(reply['ok'], reply)
                self.assertEqual(hwp.calls[hwp.calls.index(('SELECTION_FAULT',))+1:], [])
                self.assertEqual(hwp.executes(), [])


class ExactCreationLocation(unittest.TestCase):
    def test_eligible_zero_and_nonzero_native_offsets(self):
        for offset in (0,2):
            hwp = creation_doc(); hwp.cursor=(0,1,offset)
            reply = flow(hwp).step(**{**CREATE,'expected_pos':[0,1,offset]})
            self.assertTrue(reply['ok'], reply)
            self.assertIn('drawtext',hwp.body[1]['atoms'][offset])
            self.assertEqual(hwp.body[1]['atoms'][offset]['points'],[0,0,11339,0,11339,5669,0,5669])

    def test_foreign_paragraph_or_offset_holds_before_navigation_or_text(self):
        for requested, actual in (((0,1,0),(0,2,0)), ((0,1,2),(0,1,3))):
            hwp = creation_doc(); hwp.cursor=requested
            execute = hwp.HAction.Execute
            def misplaced(name, pset):
                if name=='DrawObjCreatorTextBox': hwp.cursor=actual
                return execute(name,pset)
            hwp.HAction.Execute=misplaced
            root_read_hook(hwp, lambda root:None, post_creation=True)
            reply=flow(hwp).step(**{**CREATE,'expected_pos':list(requested)})
            self.assertFalse(reply['ok'],reply)
            self.assertIn('drawtext',hwp.body[actual[1]]['atoms'][actual[2]])
            self.assertEqual(''.join(a.get('c','') for a in hwp.body[1]['atoms']),'Anchor')
            self.assertEqual(hwp.calls[hwp.calls.index(('POST_CREATION_EVIDENCE',))+1:],[])
            self.assertFalse(any(c[:2]==('Execute','InsertText') for c in hwp.calls))


class ActualCreationGeometry(unittest.TestCase):
    def check(self, mutate, positive=False):
        hwp=creation_doc()
        def change(root):
            rect=next(e for e in root.iter() if e.tag=='RECTANGLE')
            mutate(rect)
        root_read_hook(hwp,change,post_creation=True)
        reply=flow(hwp).step(**CREATE)
        self.assertEqual(reply['ok'],positive,reply)
        if not positive:
            self.assertEqual(hwp.calls[hwp.calls.index(('POST_CREATION_EVIDENCE',))+1:],[])
            self.assertFalse(any(c[:2]==('Execute','InsertText') for c in hwp.calls))
        return reply

    def test_honest_prepared_array_noncollapsed_geometry_positive(self):
        self.check(lambda rect:None,True)

    def test_actual_collapsed_nonfinite_malformed_or_nonhorizontal_geometry(self):
        def coincident(rect):
            for index in range(4): rect.set('X'+str(index),'0');rect.set('Y'+str(index),'0')
        cases=[coincident,
               lambda r:r.find('DRAWINGOBJECT/SHAPECOMPONENT').set('CurWidth','0'),
               lambda r:r.find('DRAWINGOBJECT/SHAPECOMPONENT').set('CurHeight','0'),
               lambda r:r.find('DRAWINGOBJECT/SHAPECOMPONENT').set('OriWidth','0'),
               lambda r:r.find('.//SCAMATRIX').set('E1','0'),
               lambda r:r.find('.//SCAMATRIX').set('E5','NaN'),
               lambda r:r.find('.//SCAMATRIX').set('E2','Infinity'),
               lambda r:r.find('.//ROTMATRIX').set('E1','bad'),
               lambda r:r.set('X2','NaN'),
               lambda r:r.set('Y2','2147483648'),
               lambda r:r.find('SHAPEOBJECT/SIZE').set('WidthRelTo','Page'),
               lambda r:r.find('SHAPEOBJECT/SIZE').set('HeightRelTo','Page'),
               lambda r:r.find('.//PARALIST').set('TextDirection','1'),
               lambda r:r.find('.//RENDERINGINFO').remove(r.find('.//TRANSMATRIX'))]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index): self.check(mutate)

    def test_source_grounded_default_omissions_and_derived_extents(self):
        def omit(rect):
            comp=rect.find('DRAWINGOBJECT/SHAPECOMPONENT')
            for key in ('GroupLevel','CurWidth','CurHeight'):comp.attrib.pop(key)
            size=rect.find('SHAPEOBJECT/SIZE')
            for key in ('WidthRelTo','HeightRelTo'):size.attrib.pop(key)
            rect.find('.//PARALIST').attrib.pop('TextDirection')
        self.check(omit,True)


class GroupScopeAdmission(unittest.TestCase):
    def grouped(self, change):
        hwp=ContainersHwp()
        root_read_hook(hwp,lambda root:change(next(e for e in root.iter() if e.tag=='RECTANGLE')))
        return hwp

    def test_nonzero_group_level_inventory_and_backend_refuse_without_navigation(self):
        hwp=self.grouped(lambda r:r.find('DRAWINGOBJECT/SHAPECOMPONENT').set('GroupLevel','1'))
        item=listing(hwp)[0]
        self.assertFalse(item['editable']);self.assertEqual(item['refusal_code'],'container_grouped_owner')
        hwp.calls.clear();reply=flow(hwp).step(**edit_request(item))
        self.assertFalse(reply['ok'],reply)
        self.assertEqual(reply['steps'][0]['refusal_code'],'container_grouped_owner')
        self.assertFalse(any(c[0] in ('set_pos','GetPos','Execute') for c in hwp.calls),hwp.calls)

    def test_container_ancestry_is_not_overridden_by_child_group_level_zero(self):
        hwp=ContainersHwp()
        def grouped(root):
            rect=next(e for e in root.iter() if e.tag=='RECTANGLE')
            parent=next(e for e in root.iter() if rect in list(e))
            index=list(parent).index(rect)
            group=E.Element('CONTAINER');parent[index]=group;group.append(rect)
        root_read_hook(hwp,grouped)
        item=listing(hwp)[0]
        self.assertFalse(item['editable']);self.assertEqual(item['refusal_code'],'container_nested_owner')
        hwp.calls.clear();reply=flow(hwp).step(**edit_request(item))
        self.assertFalse(reply['ok'],reply)
        self.assertFalse(any(c[0] in ('set_pos','GetPos','Execute') for c in hwp.calls))

    def test_malformed_conflicting_or_ambiguous_grouping_stops_at_document_read(self):
        cases=[lambda r:r.find('DRAWINGOBJECT/SHAPECOMPONENT').set('GroupLevel','-1'),
               lambda r:r.find('DRAWINGOBJECT/SHAPECOMPONENT').set('GroupLevel','bad'),
               lambda r:r.find('DRAWINGOBJECT/SHAPECOMPONENT').set('groupLevel','1'),
               lambda r:r.find('SHAPEOBJECT').set('GroupLevel','1'),
               lambda r:r.find('DRAWINGOBJECT').append(E.Element('SHAPECOMPONENT',{'GroupLevel':'0'}))]
        for index, change in enumerate(cases):
            with self.subTest(case=index):
                hwp=self.grouped(change)
                reply=flow(hwp).step(op='text_container_inventory')
                self.assertFalse(reply['ok'],reply)
                self.assertEqual(hwp.calls,[('get_text_file','HWPML2X','')])

    def test_ungrouped_default_omission_and_footnote_specific_positives(self):
        for kind in ('text_box','caption','footnote'):
            hwp=ContainersHwp([paragraph('Body',container(kind))])
            if kind!='footnote':
                root_read_hook(hwp,lambda root:next(e for e in root.iter() if e.tag=='SHAPECOMPONENT').attrib.pop('GroupLevel'))
            item=listing(hwp)[0]
            self.assertTrue(item['editable'])
            reply=flow(hwp).step(**edit_request(item))
            self.assertTrue(reply['ok'],reply)
