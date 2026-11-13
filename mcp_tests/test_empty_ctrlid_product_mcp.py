"""Public MCP/HTTP/owner product slice; SDK is declared model, not native proof."""
import json
from unittest.mock import patch
from tests.test_constructor_state_contract import Lifecycle,CREATE
from tests.test_empty_ctrlid_product import create_head,record,ContainersHwp
from mcp_tests.test_repair_saved_output_and_review import Base,Backend,SID
class EmptyCensusMCP(Base):
 async def asyncSetUp(self):
  await super().asyncSetUp();self.model=None;old=Backend.do_POST
  def post(handler):
   if handler.path!='/local-cli/command-bundle':return old(handler)
   payload=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])));return handler.reply(self.model.own.service.command_bundle(**payload))
  p=patch.object(Backend,'do_POST',post);p.start();self.addCleanup(p.stop)
 async def test_public_create_with_empty_node_inventory_and_read_requested_text(self):
  for where in (0,2):
   with self.subTest(where=where):
    m=self.model=Lifecycle(self,sid=SID);events=[]
    with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events,where))):
     reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}});record(self,str(where),m,reply.structured_content,events)
     self.assertFalse(reply.is_error,reply.structured_content);self.assertFalse(m.own.session._native_custody_held)
     inv=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[{'op':'text_container_inventory'}]}});self.assertFalse(inv.is_error,inv.structured_content)
     data=inv.structured_content['result']['steps'][0]['result'];item=data['containers'][0]
     read=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[{'op':'text_container_read','target_id':item['target_id'],'expected_hash':item['hash']}]}})
     self.assertFalse(read.is_error,read.structured_content);self.assertIn(CREATE['text'],json.dumps(read.structured_content));self.assertEqual(m.own.cleanup_events(),[])
 async def test_public_empty_required_target_refuses_without_text_cleanup_or_unholding(self):
  m=self.model=Lifecycle(self,sid=SID);events=[]
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events,omit=True))):reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
  self.assertTrue(reply.is_error);data=reply.structured_content['error']['details']['backend'];self.assertTrue(data['dirty']);self.assertTrue(m.own.session._native_custody_held)
  self.assertEqual(data['steps'][0]['failure_diagnostic']['native_site'],'container.binding.census');self.assertEqual([c[1] for c in m.h.executes()],['DrawObjCreatorTextBox']);self.assertEqual(m.own.cleanup_events(),[])
