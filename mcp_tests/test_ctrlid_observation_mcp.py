"""Public MCP -> HTTP -> actual owning service CtrlID diagnostic contracts."""
import json
from unittest.mock import patch
from app import native_text_containers as nt
from tests.test_ctrlid_observation import model,retain,CREATE,ContainersHwp
from mcp_tests.test_repair_saved_output_and_review import Base,Backend,SID
class CtrlIDObservationMCP(Base):
 async def asyncSetUp(self):
  await super().asyncSetUp();self.model=None;old=Backend.do_POST
  def post(handler):
   if handler.path!='/local-cli/command-bundle':return old(handler)
   payload=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
   return handler.reply(self.model.own.service.command_bundle(**payload))
  p=patch.object(Backend,'do_POST',post);p.start();self.addCleanup(p.stop)
 async def test_empty_case_and_malformed_ctrlid_observation_reaches_public_wire(self):
  for label,value,kind in [('empty','','str'),('case','GSO','str'),('none',None,'none')]:
   with self.subTest(label=label):
    self.model,events,head=model(self,value,sid=SID)
    with self.model.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)):reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
    retain(self,label,self.model,reply.structured_content,events);self.assertTrue(reply.is_error)
    data=reply.structured_content['error']['details']['backend'];d=data['steps'][0]['failure_diagnostic']
    if label=='empty':
     self.assertEqual(d['native_site'],'container.binding.census');self.assertNotIn('native_ctrlid_boundary',d);self.assertEqual(events[-1],('Next',))
    else:
     self.assertEqual(d.get('native_ctrlid_boundary'),'control.CtrlID');self.assertEqual(d.get('native_ctrlid_type'),kind);self.assertEqual(events[-1],('RETURN_CTRLID',))
    self.assertTrue(data['dirty']);self.assertTrue(self.model.own.session._native_custody_held)
    self.assertEqual(self.model.own.cleanup_events(),[])
 async def test_publication_failure_does_not_replace_failure_or_unsettle_owner(self):
  self.model,events,head=model(self,'GSO',sid=SID);published=[]
  def fail(*a):published.append(self.model.own.session._native_custody_held);raise MemoryError('optional publication')
  with self.model.patches(),patch.object(ContainersHwp,'HeadCtrl',property(head)),patch.object(nt,'_project_ctrlid',fail,create=True):
   reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
  retain(self,'publication',self.model,reply.structured_content,events);self.assertTrue(reply.is_error);self.assertEqual(published,[True,True])
  data=reply.structured_content['error']['details']['backend'];d=data['steps'][0]['failure_diagnostic']
  self.assertEqual(d['native_site'],'container.control.CtrlID');self.assertEqual(d['native_return'],'invalid')
  self.assertEqual((d['causal_class'],d['cause_class']),('NativeNumberingMutationError','NativeNumberingReadFailure'))
  self.assertTrue(self.model.own.session._native_custody_held);self.assertEqual(self.model.own.cleanup_events(),[])
