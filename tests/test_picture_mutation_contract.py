"""Public existing-picture edits; synthetic SDK semantics, never native proof.

The HWPML template is an authenticated pre-edit snapshot of a NEW owned fixture.
Only this fake mutates its in-memory XML; the product must use Hancom actions.
"""
from pathlib import Path
import base64,copy,hashlib,tempfile,unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from app import native_picture as picture
from tests.native_header_footer_fakes import report_document
from tests.test_native_paragraph_edit import Flow as BaseFlow

FIXTURE=Path(__file__).parent/'fixtures/picture_native_preedit.hwpml2x'
def local(e):return e.tag.rsplit('}',1)[-1].lower()
def first(e,name):return next(x for x in e.iter() if local(x)==name)

class Flow(BaseFlow):
    def __init__(self,hwp):
        super().__init__(hwp)
        execute=self.service._execute_live
        def scoped(*,handler,**kwargs):
            def inject(handle):
                handle.session_root=hwp.asset_root
                from tests.native_picture_typed_fakes import com_modules
                with com_modules():return handler(handle)
            return execute(handler=inject,**kwargs)
        self.service._execute_live=scoped

class PSet:
    def __init__(self,hwp):
        self.hwp=hwp
        dims=picture.parse_pictures(hwp.get_text_file())['pictures'][0]['source_dimensions']
        self.data={'OriginalSizeX':int(dims['Width']),'OriginalSizeY':int(dims['Height'])}
    def SetItem(self,key,value):
        self.hwp.events.append('SetItem:'+key); self.data[key]=value
        return self.hwp.setter_result
    def Item(self,key):self.hwp.events.append('Item:'+key);return self.data.get(key)
    def CreateItemSet(self,key,kind):
        self.hwp.events.append('CreateItemSet:'+key); self.data[key]=PSet(self.hwp);return self.data[key]

class Ctrl:
    CtrlID='gso'
    def __init__(self,hwp,ident):self.owner=hwp;self.ident=ident;self.Next=None
    def GetCtrlInstID(self):self.owner.events.append('GetCtrlInstID');return self.ident
    def GetAnchorPos(self,mode):
        if type(mode) is not int or mode!=0:raise TypeError('documented anchor mode')
        return Anchor(self.owner.ctrls.index(self)+1)

class Anchor:
    def __init__(self,pos):self.values={'List':0,'Para':0,'Pos':pos}
    def Item(self,key):return self.values[key]

class PictureHwp:
    def __init__(self,shared=False):
        self.base=report_document();self.root=ET.fromstring(FIXTURE.read_text(encoding='utf-8'))
        self.asset_root=Path(tempfile.mkdtemp(prefix='picture-owned-'))
        self.events=[];self.selected=None;self.setter_result=None;self.native_id_override=None
        self.action_result=True;self.post_body_damage=False;self.read_failure=False;self.latched=False
        self.after_latch=[];self.hwp=self;self.ParentCtrl=None;self.HAction=self
        from tests.native_picture_typed_fakes import Parameters,Ole
        self.HParameterSet=Parameters(self);self._oleobj_=Ole('HAction')
        # Synthetic external state matches the retained native serialized caret.
        self.native_position=tuple(int(first(self.root,'caretpos').get(k)) for k in ('List','Para','Pos'))
        self.effective_text_flow=0  # Explicit synthetic observed state, not a product default.
        if shared:
            p=first(self.root,'picture'); q=copy.deepcopy(p)
            first(q,'shapeobject').set('InstId','999999');first(q,'shapecomponent').set('InstID','888888')
            parent=next(e for e in self.root.iter() if p in list(e));parent.append(q)
        self.relink()
    def relink(self):
        ctrls=[Ctrl(self,first(p,'shapeobject').get('InstId')) for p in self.root.iter() if local(p)=='picture']
        prefix=[]
        for e in self.root.iter():
            kind={'secdef':'secd','coldef':'cold','header':'head','footer':'foot','table':'tbl'}.get(local(e))
            if kind:
                c=Ctrl(self,'0');c.CtrlID=kind;prefix.append(c)
        all_ctrls=prefix+ctrls
        for a,b in zip(all_ctrls,all_ctrls[1:]):a.Next=b
        self.ctrls=ctrls;self.HeadCtrl=all_ctrls[0] if all_ctrls else None
    def __getattr__(self,name):return getattr(self.base,name)
    def get_pos(self):
        if self.latched:self.after_latch.append('get_pos')
        return self.GetPos()
    def GetPos(self):return self.native_position
    def SetPosBySet(self,anchor):
        self.native_position=tuple(anchor.Item(k) for k in ('List','Para','Pos'))
        for key,value in zip(('List','Para','Pos'),self.native_position):
            first(self.root,'caretpos').set(key,str(value))
        return True
    @property
    def SelectionMode(self):
        return 4 if self.selected is not None or getattr(self,'native_selected',set()) else 0
    def Run(self,name):
        self.events.append('Run:'+name)
        if name=='Cancel':
            self.selected=None
            if hasattr(self,'native_selected'):self.native_selected.clear()
            return True
        if name=='SelectCtrlFront':
            self.selected=next(c for c in self.ctrls if self.GetPos()==(0,0,self.ctrls.index(c)+1))
            if hasattr(self,'native_selected'):self.native_selected={self.selected.ident}
            return True
        return self.base.HAction.Run(name)
    def get_text_file(self,*args):
        if self.latched:self.after_latch.append('GetTextFile')
        self.events.append('GetTextFile')
        if self.read_failure:self.latched=True;raise RuntimeError('injected required read')
        return ET.tostring(self.root,encoding='unicode')
    def SelectCtrl(self,ident,option):
        self.events.append('SelectCtrl:'+ident)
        self.selected=next((c for c in self.ctrls if c.ident==ident),None)
        return self.selected is not None
    @property
    def CurSelectedCtrl(self):
        self.events.append('CurSelectedCtrl')
        if self.native_id_override and self.selected:return Ctrl(self,self.native_id_override)
        return self.selected
    def CreateAction(self,name):
        self.events.append('CreateAction:'+name)
        return SimpleNamespace(CreateSet=lambda:PSet(self))
    def GetDefault(self,name,pset):
        from tests.native_picture_typed_fakes import default
        return default(self,name,pset)
    def Execute(self,name,pset):
        from tests.native_picture_typed_fakes import execution_values
        pset=execution_values(self,name,pset)
        self.events.append('Execute:'+name)
        pic=next(p for p in self.root.iter() if local(p)=='picture' and first(p,'shapeobject').get('InstId')==self.selected.ident)
        if name=='PictureChange':
            # Allocate a new member. Never mutate the one shared by another control.
            item=next(e for e in self.root.iter() if local(e)=='binitem')
            parent=next(e for e in self.root.iter() if item in list(e))
            new=copy.deepcopy(item); index=len([e for e in self.root.iter() if local(e)=='binitem'])+1
            new.set('BinData',str(index));new.set('Id',str(index));parent.append(new)
            parent.set('Count',str(len([e for e in parent if local(e)=='binitem'])))
            data=first(self.root,'bindata'); dataparent=next(e for e in self.root.iter() if data in list(e))
            added=copy.deepcopy(data);added.set('Id',str(index));added.text=base64.b64encode(Path(pset.data['PicturePath']).read_bytes()).decode();dataparent.append(added)
            first(pic,'image').set('BinItem',str(index))
        else:
            shape=first(pic,'shapeobject');pos=first(shape,'position')
            if 'ShapeDrawImageAttr' in pset.data and any(k.startswith('Skip') for k in pset.data['ShapeDrawImageAttr'].data):
                d=pset.data['ShapeDrawImageAttr'].data;clip=first(pic,'imageclip')
                for key,value in {'Left':d['SkipLeft'],'Top':d['SkipTop'],'Right':d['OriginalSizeX']-d['SkipRight'],'Bottom':d['OriginalSizeY']-d['SkipBottom']}.items():clip.set(key,str(value))
            # SYNTHETIC SDK model: Execute applies every value that was put on the set
            # GetDefault loaded; an unput value keeps its current value. A test may
            # inject a native side effect with an inline request.
            if 'TreatAsChar' in pset.data:
                pos.set('TreatAsChar','true' if pset.data['TreatAsChar'] else 'false')
            if 'TextWrap' in pset.data:
                shape.set('TextWrap','Square' if pset.data['TextWrap']==0 else 'TopAndBottom')
            if 'TreatAsChar' in pset.data and getattr(self,'unrequested_wrap',None):
                shape.set('TextWrap',self.unrequested_wrap)
            for key in ('HorzOffset','VertOffset'):
                if key in pset.data:pos.set(key,str(pset.data[key]))
        if self.post_body_damage:
            t=next(e for e in self.root.iter() if local(e)=='char' and e.text);t.text='UNRELATED DAMAGE'
        return self.action_result

class PublicMutations(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        from PIL import Image
        self.image=Path(self.tmp.name)/'new.png';Image.new('RGB',(300,150),'purple').save(self.image)
    def request(self,hwp,op,**fields):
        info=Flow(hwp).step(op='picture_inventory')
        self.assertTrue(info['ok'],info)
        target=info['steps'][0]['result']['pictures'][0]
        return {'op':op,'target_id':target['target_id'],'expected_hash':target['hash'],'confirm_layout':True,**fields}
    def test_public_replace_reaches_native_action_and_preserves_shared_other_picture(self):
        hwp=PictureHwp(shared=True);before=picture.parse_pictures(hwp.get_text_file())['pictures']
        req=self.request(hwp,'picture_replace_exact',source_path=str(self.image))
        out=Flow(hwp).step(**req);self.assertTrue(out['ok'],out)
        after=picture.parse_pictures(hwp.get_text_file())['pictures']
        self.assertEqual(before[1]['media'],after[1]['media'])
        self.assertEqual(before[0]['frame'],after[0]['frame'])
        self.assertNotEqual(before[0]['media'],after[0]['media'])
        self.assertIn('Execute:PictureChange',hwp.events)
    def test_public_crop_uses_native_metadata_not_payload_reencoding_or_resize(self):
        hwp=PictureHwp();before=picture.parse_pictures(hwp.get_text_file())['pictures'][0]
        req=self.request(hwp,'picture_crop_exact',left=.25,top=0.,right=.75,bottom=1.)
        out=Flow(hwp).step(**req);self.assertTrue(out['ok'],out)
        after=picture.parse_pictures(hwp.get_text_file())['pictures'][0]
        self.assertEqual(before['media'],after['media']);self.assertEqual(before['frame'],after['frame'])
        self.assertEqual(int(after['crop']['Left']),5625)
        self.assertEqual(int(after['crop']['Right']),16875)
        self.assertIn('Execute:ShapeObjDialog',hwp.events)
    def test_public_layout_keeps_frame_media_and_crop(self):
        hwp=PictureHwp();before=picture.parse_pictures(hwp.get_text_file())['pictures'][0]
        req=self.request(hwp,'picture_layout_exact',mode='square',horizontal_offset_mm=15.)
        out=Flow(hwp).step(**req);self.assertTrue(out['ok'],out)
        after=picture.parse_pictures(hwp.get_text_file())['pictures'][0]
        for key in ('media','frame','crop'):self.assertEqual(before[key],after[key])
        self.assertEqual(after['shape']['TextWrap'],'Square');self.assertEqual(after['placement']['TreatAsChar'],'false')
    def test_stale_binding_never_selects_or_executes(self):
        hwp=PictureHwp();req=self.request(hwp,'picture_crop_exact',left=.25,top=0.,right=.75,bottom=1.)
        req['expected_hash']='sha256:'+'0'*24
        out=Flow(hwp).step(**req);self.assertFalse(out['ok'])
        self.assertFalse(any(e.startswith(('SelectCtrl','Execute')) for e in hwp.events))
    def test_entered_read_failure_has_no_post_latch_snapshot(self):
        hwp=PictureHwp();req=self.request(hwp,'picture_crop_exact',left=.25,top=0.,right=.75,bottom=1.)
        hwp.read_failure=True
        out=Flow(hwp).step(**req);self.assertFalse(out['ok'])
        self.assertEqual(hwp.after_latch,[])
    def test_unrelated_change_after_execute_is_not_success(self):
        hwp=PictureHwp();req=self.request(hwp,'picture_layout_exact',mode='square')
        hwp.post_body_damage=True
        out=Flow(hwp).step(**req);self.assertFalse(out['ok'])
    def test_setitem_void_contract_refuses_nonvoid_without_execute(self):
        hwp=PictureHwp();req=self.request(hwp,'picture_layout_exact',mode='square')
        hwp.setter_result=True
        out=Flow(hwp).step(**req);self.assertFalse(out['ok'])
        self.assertFalse(any(e.startswith('Execute') for e in hwp.events))

    def test_current_full_crop_noop_issues_no_selection_or_action(self):
        hwp=PictureHwp();req=self.request(hwp,'picture_crop_exact',left=0.,top=0.,right=1.,bottom=1.)
        out=Flow(hwp).step(**req);self.assertTrue(out['ok'],out)
        self.assertFalse(out['steps'][0]['dirty'])
        self.assertFalse(any(x.startswith(('SelectCtrl','Execute')) for x in hwp.events))
    def test_existing_inline_noop_preserves_prior_history(self):
        hwp=PictureHwp();flow=Flow(hwp)
        req=self.request(hwp,'picture_layout_exact',mode='inline')
        flow.binding['pending_logical_undo_count']=3
        out=flow.step(**req);self.assertTrue(out['ok'],out)
        self.assertFalse(out['steps'][0]['dirty']);self.assertEqual(flow.binding['pending_logical_undo_count'],3)
    def test_single_dirty_picture_history_is_not_inferred_from_one_execute(self):
        hwp=PictureHwp();flow=Flow(hwp)
        req=self.request(hwp,'picture_layout_exact',mode='square')
        out=flow.step(**req);self.assertTrue(out['ok'],out)
        self.assertIsNone(flow.binding.get('pending_logical_undo_count'))
        self.assertIn('picture_layout_exact',flow.binding.get('logical_undo_unverified',''))
    def test_foreign_opaque_type_validation_never_invokes_metaclass_equality(self):
        hwp=PictureHwp();req=self.request(hwp,'picture_layout_exact',mode='square');touched=[]
        class Meta(type):
            def __eq__(self,other):touched.append('metaclass');return False
        class Foreign(metaclass=Meta):pass
        hwp.HeadCtrl=Foreign()
        out=Flow(hwp).step(**req);self.assertFalse(out['ok'])
        self.assertEqual(touched,[])

class DiscoveryContract(unittest.TestCase):
    def test_public_parity_lists_all_picture_ops_without_native_history_claim(self):
        from hwpx_mcp import parity
        rows={r['backend']:r for r in parity.matrix()['bundle_ops']}
        for op in ('picture_inventory','picture_replace_exact','picture_crop_exact','picture_layout_exact'):
            self.assertIn(op,rows)
            self.assertEqual(rows[op]['status'],'exposed')
        for op in ('picture_replace_exact','picture_crop_exact','picture_layout_exact'):
            variants=rows[op]['variants']
            self.assertTrue(variants)
            self.assertTrue(all(r['native']!='qualified' for r in variants))
            self.assertTrue(all(r.get('undo')=='not_counted' for r in variants if r['native']=='not_qualified'))
    def test_picture_mcp_defaults_supply_the_required_backend_label(self):
        from hwpx_mcp import schema
        for cls in (schema.PictureInventoryStep,schema.PictureReplaceStep,schema.PictureCropStep,schema.PictureLayoutStep):
            op=cls.model_fields['op'].annotation.__args__[0]
            fields={'op':op} if op=='picture_inventory' else {'op':op,'target_id':'picture:12345','expected_hash':'sha256:'+'a'*24,'confirm_layout':True}
            if op=='picture_replace_exact':fields['source_path']='/owned/new.png'
            if op=='picture_crop_exact':fields.update(left=.25,top=.25,right=.75,bottom=.75)
            if op=='picture_layout_exact':fields['mode']='square'
            dumped=cls.model_validate(fields).model_dump(mode='json',exclude_none=True)
            self.assertIs(type(dumped.get('label')),str)
            self.assertTrue(dumped['label'])
    def test_inventory_separates_structural_eligibility_and_qualified_editing(self):
        hwp=PictureHwp()
        out=Flow(hwp).step(op='picture_inventory')
        contract=out['steps'][0]['result']['edit_contract']
        self.assertFalse(contract['native_edit_qualified'])
        self.assertEqual(contract['implemented_contexts'],['body_without_native_caption'])
        self.assertFalse(contract['native_history_qualified'])

if __name__=='__main__':unittest.main()
