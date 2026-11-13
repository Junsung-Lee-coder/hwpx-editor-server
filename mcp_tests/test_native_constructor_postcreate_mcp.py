"""Actual native constructor format through real MCP/HTTP/service/owner, SDK model."""
import json
from unittest.mock import patch
from mcp_tests.test_repair_saved_output_and_review import Base,Backend,SID
from tests.test_native_constructor_postcreate import actual_owned,CREATE,retain,owner
class NativeConstructorMCP(Base):
 async def asyncSetUp(self):
  await super().asyncSetUp();self.owned=None;old=Backend.do_POST
  def post(handler):
   if handler.path!='/local-cli/command-bundle':return old(handler)
   payload=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
   return handler.reply(self.owned.service.command_bundle(**payload))
  p=patch.object(Backend,'do_POST',post);p.start();self.addCleanup(p.stop)
 async def test_native_format_complete_create_reaches_requested_text_via_public_MCP(self):
  self.owned,trace,state=actual_owned(self,session_id=SID)
  reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
  retain(self,self.owned,state,reply.structured_content)
  self.assertFalse(reply.is_error,reply.structured_content)
  self.assertIn(CREATE['text'],json.dumps(reply.structured_content))
  self.assertEqual(''.join(c.text or '' for c in owner(state['root']).findall('.//CHAR')),CREATE['text'])
  self.assertFalse(self.owned.session._native_custody_held)
 async def test_wrong_geometry_remains_dirty_held_and_no_public_follow_on(self):
  self.owned,trace,state=actual_owned(self,'wrong_size',session_id=SID)
  reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
  retain(self,self.owned,state,reply.structured_content)
  self.assertTrue(reply.is_error);self.assertEqual(reply.structured_content['error']['code'],'BACKEND_REJECTED')
  self.assertTrue(self.owned.session._native_custody_held);self.assertEqual([c[1] for c in self.owned.hwp.executes()],['DrawObjCreatorTextBox'])
  self.assertEqual(self.owned.hwp.calls[-1][0],'GetTextFile')
