"""N2 regressions: real service, real owning STA/custody, real public projection/CLI.
Only external SDK/COM/OS observations are synthetic. No native qualification.
"""
import base64,contextlib,copy,io,json,tempfile,unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
from app import local_cli_runtime as rt,native_numbering as nn,native_picture as picture
from app.local_cli_service import LocalCliService
from local_cli_v1 import main as cli
from tests import test_picture_mutation_contract as sdk
from tests.test_guarded_bundle_observations import real_owned

class Picture(sdk.PictureHwp):
    def __init__(self,damage=None,**kw):
        super().__init__(**kw);self.damage=damage;self.damage_completed=False;self.executed=False;self.post_returned=False;self.followons=[]
    def Execute(self,name,pset):
        result=super().Execute(name,pset);self.executed=True
        if self.damage:self.damage(self);self.damage_completed=True
        return result
    def get_text_file(self,*args):
        if self.post_returned:self.followons.append('GetTextFile')
        value=super().get_text_file(*args)
        if self.executed:self.post_returned=True
        return value
    def get_pos(self):
        if self.post_returned:self.followons.append('get_pos')
        return super().get_pos()

def pic(h):return sdk.first(h.root,'picture')
def target(h):return picture.parse_pictures(ET.tostring(h.root,encoding='unicode'))['pictures'][0]
def req(h,op,**kw):
    p=target(h)
    return dict(op=op,label='picture-n2',target_id=p['target_id'],expected_hash=p['hash'],confirm_layout=True,**kw)

@contextlib.contextmanager
def owned(h):
    # Restore real command-time runtime snapshots after the existing fake-only
    # constructor adapter. No snapshot or public projection stub during commands.
    snapshot=rt.snapshot_live_location
    with ExitStack() as cleanup:
        case=type('CleanupScope',(),{'addCleanup':lambda self,fn,*a,**kw:cleanup.callback(fn,*a,**kw)})()
        o=real_owned(case,h)
        o.service._public_bundle_steps=LocalCliService._public_bundle_steps.__get__(o.service)
        from tests.native_picture_typed_fakes import com_modules
        with patch.object(rt,'snapshot_live_location',snapshot),com_modules():yield o

def command(o,*steps):return o.service.command_bundle(steps=list(steps),session_id=o.sid)
def public_call(o,*steps):
    try:return command(o,*steps),None
    except rt.LocalCliRuntimeError as exc:return None,exc

def many(h,count):
    p=pic(h);parent=next(e for e in h.root.iter() if p in list(e))
    for i in range(1,count):
        q=copy.deepcopy(p);sdk.first(q,'shapeobject').set('InstId',str(800000+i));sdk.first(q,'shapecomponent').set('InstID',str(900000+i));parent.append(q)
    h.relink()

class N2Contract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        from PIL import Image
        self.png=Path(self.tmp.name)/'replacement.png';Image.new('RGB',(300,150),'purple').save(self.png)
    def request(self,h,op):
        if op=='picture_replace_exact':return req(h,op,source_path=str(self.png))
        if op=='picture_crop_exact':return req(h,op,left=.25,top=.25,right=.75,bottom=.75)
        return req(h,op,mode='square')
    def assert_held(self,o):
        self.assertTrue(o.session._thread.is_alive());self.assertFalse(o.session._closed.is_set())
        self.assertTrue(o.session._quarantined);self.assertTrue(o.session.has_unreconciled_reconciliation())
        self.assertIs(o.manager._sessions.get(o.sid),o.session);self.assertEqual(o.cleanup_events(),[])
    def test_ordinary_positive_each_operation_uses_real_service_and_owner(self):
        for op in ('picture_replace_exact','picture_crop_exact','picture_layout_exact'):
            with self.subTest(op=op):
                h=Picture(shared=True)
                with owned(h) as o:
                    out=command(o,self.request(h,op));self.assertTrue(out['ok'],out)
                    self.assertFalse(o.session._quarantined)
                    self.assertEqual(len([x for x in h.events if x.startswith('Execute:')]),1)
    def test_requested_success_plus_unrequested_target_delta_is_held_all_families(self):
        damages={
            'nested_rotation':lambda h:sdk.first(pic(h),'rotationinfo').set('Angle','30'),
            'nested_render_matrix':lambda h:sdk.first(pic(h),'transmatrix').set('E1','2'),
            'image_alpha':lambda h:sdk.first(pic(h),'image').set('Alpha','255'),
            'image_rectangle_point':lambda h:sdk.first(pic(h),'imagerect').set('X0','4711'),
        }
        for op in ('picture_replace_exact','picture_crop_exact','picture_layout_exact'):
            for name,damage in damages.items():
                with self.subTest(op=op,damage=name):
                    h=Picture(damage=damage,shared=True)
                    with owned(h) as o:
                        out,error=public_call(o,self.request(h,op))
                        self.assertTrue(h.executed,'fault must follow a valid requested Execute')
                        self.assertTrue(h.damage_completed,'damage injection must complete, not fail during setup')
                        self.assertFalse(out is not None and out.get('ok') is True,(op,name,out))
                        self.assert_held(o);self.assertEqual(h.followons,[])
    def test_full_real_public_inventory_preserves_layout_and_reaches_101st_binding(self):
        h=Picture();many(h,101)
        with owned(h) as o:
            out=command(o,dict(op='picture_inventory',label='inventory'))
            self.assertTrue(out['ok'],out);r=out['steps'][0]['result']
            self.assertTrue(all(type(p) is dict for p in r['pictures']))
            self.assertEqual(len(r['pictures']),101)
            self.assertEqual(r['total_count'],101);self.assertTrue(r['complete'])
            self.assertEqual(r['pictures'][0]['shape']['TextWrap'],'TopAndBottom')
            self.assertEqual(r['pictures'][0]['placement']['FlowWithText'],'true')
            last=r['pictures'][-1]
            edit=dict(op='picture_layout_exact',label='last',target_id=last['target_id'],expected_hash=last['hash'],confirm_layout=True,mode='square')
            self.assertTrue(command(o,edit)['ok'])
    def test_inventory_over_documented_bound_is_a_clean_explicit_refusal(self):
        h=Picture();many(h,1001)
        with owned(h) as o:
            out=command(o,dict(op='picture_inventory',label='too-many'))
            self.assertFalse(out['ok']);self.assertEqual(out['steps'][0]['refusal_code'],'picture_inventory_limit')
            self.assertFalse(o.session._quarantined);self.assertFalse(h.executed)
    def test_outside_shared_payload_body_and_other_caption_damage_holds(self):
        def shared(h):sdk.first(h.root,'bindata').text=base64.b64encode(b'UNRELATED-CORRUPTION').decode()
        def body(h):next(e for e in h.root.iter() if sdk.local(e)=='char' and e.text).text='UNREQUESTED BODY CHANGE'
        def caption(h):
            second=[p for p in h.root.iter() if sdk.local(p)=='picture'][1]
            ET.SubElement(second,'CAPTION').set('Width','1234')
        for op in ('picture_replace_exact','picture_crop_exact','picture_layout_exact'):
            for damage in (shared,body,caption):
                with self.subTest(op=op,damage=damage.__name__):
                    h=Picture(shared=True,damage=damage)
                    with owned(h) as o:
                        out,error=public_call(o,self.request(h,op))
                        self.assertTrue(h.damage_completed)
                        self.assertFalse(out is not None and out.get('ok') is True)
                        self.assert_held(o);self.assertEqual(h.followons,[])
    def test_real_service_to_CLI_consumer_does_not_drop_last_binding(self):
        h=Picture();many(h,101)
        path=Path(self.tmp.name)/'bundle.json';path.write_text(json.dumps({'steps':[{'op':'picture_inventory','label':'inventory'}]}))
        with owned(h) as o:
            def transport(base,path,payload):
                self.assertEqual(path,'/local-cli/command-bundle')
                return json.loads(json.dumps(o.service.command_bundle(**payload)))
            stdout=io.StringIO();stderr=io.StringIO()
            with patch.object(cli,'load_state',return_value={'base_url':'http://synthetic.invalid','session_id':o.sid}),patch.object(cli,'post_json',side_effect=transport),contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                status=cli.main(['bundle','--json',str(path)])
            self.assertEqual(status,0,stderr.getvalue());value=json.loads(stdout.getvalue());encoded=json.dumps(value)
            self.assertIn('picture:800100',encoded);self.assertIn('TopAndBottom',encoded);self.assertIn('FlowWithText',encoded)
    def test_explicit_public_picture_projection_does_not_expose_paths_or_secrets(self):
        h=Picture();private='/private/not-for-public/secret-image.png';token='synthetic-private-value-7'
        sdk.first(pic(h),'shapecomponent').set('Name',private)
        sdk.first(pic(h),'shapeobject').set('UnexpectedCredential',token)
        sdk.first(pic(h),'shapeobject').set('TextWrap',private)
        with owned(h) as o:
            out=command(o,dict(op='picture_inventory',label='inventory'))
            body=json.dumps(out);self.assertNotIn(private,body);self.assertNotIn(token,body)
    def test_actual_clean_picture_refusals_keep_exact_public_code_and_usable_session(self):
        def case(name):
            h=Picture();r=self.request(h,'picture_layout_exact')
            if name=='picture_binding_stale':r['expected_hash']='sha256:'+'0'*24
            elif name=='picture_target_unproven':r['target_id']='picture:7654321'
            elif name=='picture_context_unqualified':
                p=pic(h);parent=next(e for e in h.root.iter() if p in list(e));parent.remove(p);cell=ET.SubElement(parent,'CELL');cell.append(p);r=self.request(h,'picture_layout_exact')
            elif name=='picture_layout_unqualified':r['mode']='behind'
            elif name=='picture_crop_empty':r=req(h,'picture_crop_exact',left=0.,top=0.,right=1e-10,bottom=1e-10)
            elif name=='picture_native_mapping_unqualified':h.ctrls[0].ident=sdk.first(pic(h),'shapecomponent').get('InstID')
            elif name=='picture_source_unreadable':r=req(h,'picture_replace_exact',source_path=str(Path(self.tmp.name)/'missing.png'))
            elif name=='picture_source_unsupported':r=req(h,'picture_replace_exact',source_path=str(Path(self.tmp.name)/'wrong.jpg'))
            elif name=='picture_aspect_unqualified':
                from PIL import Image
                square=Path(self.tmp.name)/'square.png';Image.new('RGB',(10,10),'blue').save(square);r=req(h,'picture_replace_exact',source_path=str(square))
            elif name=='picture_asset_root_unproven':r=req(h,'picture_replace_exact',source_path=str(self.png))
            return h,r
        names=('picture_binding_stale','picture_target_unproven','picture_context_unqualified','picture_layout_unqualified','picture_crop_empty','picture_native_mapping_unqualified','picture_source_unreadable','picture_source_unsupported','picture_aspect_unqualified','picture_asset_root_unproven')
        for name in names:
            with self.subTest(code=name):
                h,r=case(name)
                with owned(h) as o:
                    if name=='picture_asset_root_unproven':(o.root/'picture-assets').symlink_to(self.tmp.name,target_is_directory=True)
                    out=command(o,r);self.assertFalse(out['ok']);step=out['steps'][0]
                    self.assertEqual(step['refusal_code'],name);self.assertTrue(step.get('refusal_message'))
                    self.assertFalse(o.session._quarantined);self.assertFalse(h.executed)
                    self.assertTrue(command(o,dict(op='picture_inventory',label='still-usable'))['ok'])
    def test_entered_exact_refusal_shaped_SDK_error_stays_UNKNOWN_held_no_followons(self):
        for name in ('picture_binding_stale','picture_context_unqualified','picture_source_unreadable','picture_native_mapping_unqualified'):
            with self.subTest(code=name):
                h=Picture();request=self.request(h,'picture_layout_exact');entered=[];after=[]
                def bad(*args):entered.append(True);raise nn.NativeNumberingRefusal('synthetic misleading code',code=name)
                h.get_text_file=bad
                with owned(h) as o:
                    out,error=public_call(o,request);self.assertTrue(entered)
                    self.assertFalse(out is not None and out.get('ok') is True)
                    if out is not None:self.assertEqual(out['steps'][0].get('refusal_code','UNKNOWN'),'UNKNOWN')
                    self.assert_held(o)

if __name__=='__main__':unittest.main()
