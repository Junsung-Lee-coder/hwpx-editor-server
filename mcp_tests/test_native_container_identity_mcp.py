"""Public MCP/HTTP/owner identity-contract regression, declared SDK models."""
import json
from unittest.mock import patch
from mcp_tests.test_repair_saved_output_and_review import Base,Backend,SID
from tests.test_native_container_identity import setup,keep
from tests.test_native_constructor_postcreate import actual_owned,CREATE
class ContainerIdentityMCP(Base):
 async def asyncSetUp(self):
  await super().asyncSetUp();self.owned=None;old=Backend.do_POST
  def post(handler):
   if handler.path!='/local-cli/command-bundle':return old(handler)
   body=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
   return handler.reply(self.owned.service.command_bundle(**body))
  p=patch.object(Backend,'do_POST',post);p.start();self.addCleanup(p.stop)
 async def test_all_target_pairs_edit_with_native_IDs_independent_of_XML(self):
  for kind in ('text_box','caption','footnote'):
   with self.subTest(kind=kind):
    self.owned,trace,request=setup(self,kind,session_id=SID)
    reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[request]}})
    keep(self,self.owned,reply.structured_content);self.assertFalse(reply.is_error,reply.structured_content)
    self.assertIn('Updated container',json.dumps(reply.structured_content));self.assertFalse(self.owned.session._native_custody_held)
 async def test_constructor_create_skips_foreign_section_target_getter(self):
  self.owned,trace,state=actual_owned(self,session_id=SID)
  reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
  keep(self,self.owned,reply.structured_content);self.assertFalse(reply.is_error,reply.structured_content)
  self.assertIn(CREATE['text'],json.dumps(reply.structured_content))
