"""Public MCP product path over declared SDK/native-authored format, not native proof."""
import json
from unittest.mock import patch
from tests.test_document_caret_projection import CaretSerialization
from tests.test_empty_ctrlid_product import create_head,ContainersHwp
from tests.test_constructor_state_contract import CREATE
from mcp_tests.test_repair_saved_output_and_review import Base,Backend,SID
class DocumentCaretMCP(Base):
 async def asyncSetUp(self):
  await super().asyncSetUp();self.model=None;old=Backend.do_POST
  def post(h):
   if h.path!='/local-cli/command-bundle':return old(h)
   payload=json.loads(h.rfile.read(int(h.headers['Content-Length'])));return h.reply(self.model.own.service.command_bundle(**payload))
  p=patch.object(Backend,'do_POST',post);p.start();self.addCleanup(p.stop)
 async def test_public_Create_then_inventory_read_exact_requested_text_with_view_serialization(self):
  m=self.model=CaretSerialization(self,sid=SID);events=[]
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events))):
   r=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}});self.assertFalse(r.is_error,r.structured_content)
   inv=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[{'op':'text_container_inventory'}]}});self.assertFalse(inv.is_error,inv.structured_content)
   item=inv.structured_content['result']['steps'][0]['result']['containers'][0]
   read=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[{'op':'text_container_read','target_id':item['target_id'],'expected_hash':item['hash']}]}})
   self.assertFalse(read.is_error,read.structured_content);self.assertIn(CREATE['text'],json.dumps(read.structured_content));self.assertFalse(m.own.session._native_custody_held);self.assertEqual(m.own.cleanup_events(),[])
 async def test_public_read_purity_is_not_bypassed_by_view_projection(self):
  m=self.model=CaretSerialization(self,sid=SID,move=lambda m:setattr(m.h,'cursor',(m.h.cursor[0],0,1)));events=[]
  with m.patches(),patch.object(ContainersHwp,'HeadCtrl',property(create_head(m,events))):r=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
  self.assertTrue(m.applied);self.assertTrue(r.is_error);self.assertTrue(m.own.session._native_custody_held);self.assertEqual(m.own.cleanup_events(),[])
  self.assertEqual([c[1] for c in m.h.executes()],['DrawObjCreatorTextBox'])
