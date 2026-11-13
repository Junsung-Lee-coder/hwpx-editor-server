"""Public MCP/HTTP/owner constructor object-state -> singleton text entry contracts."""
import json
from unittest.mock import patch
from mcp_tests.test_repair_saved_output_and_review import Base,Backend,SID
from tests.test_constructor_state_contract import Lifecycle,CREATE
class ConstructorStateMCP(Base):
 async def asyncSetUp(self):
  await super().asyncSetUp();self.model=None;old=Backend.do_POST
  def post(handler):
   if handler.path!='/local-cli/command-bundle':return old(handler)
   body=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
   return handler.reply(self.model.own.service.command_bundle(**body))
  p=patch.object(Backend,'do_POST',post);p.start();self.addCleanup(p.stop)
 async def test_normal_object_and_strict_object_constructor_text_entry(self):
  for mode in (0,4,20):
   with self.subTest(mode=mode):
    self.model=Lifecycle(self,mode,sid=SID)
    with self.model.patches():reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
    self.model.retain(self,str(mode),reply.structured_content);self.assertFalse(reply.is_error,reply.structured_content)
    self.assertTrue(self.model.entered);self.assertFalse(self.model.own.session._native_custody_held)
    self.assertIn(CREATE['text'],''.join(self.model.data['root'].itertext()))
 async def test_unknown_state_and_failed_entry_never_fall_back_or_release(self):
  for kind in ('unknown','entry_false'):
   with self.subTest(kind=kind):
    self.model=Lifecycle(self,36 if kind=='unknown' else 4,sid=SID);original=self.model.Run
    def run(name):
     result=original(name)
     return False if name=='ShapeObjTextBoxEdit' else result
    if kind=='entry_false':self.model.Run=run
    with self.model.patches():reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
    self.model.retain(self,kind,reply.structured_content);self.assertTrue(reply.is_error,reply.structured_content)
    self.assertTrue(self.model.own.session._native_custody_held);self.assertEqual(self.model.own.cleanup_events(),[])
    self.assertEqual([x[1] for x in self.model.h.executes()],['DrawObjCreatorTextBox'])
    if kind=='unknown':self.assertEqual([x for x in self.model.h.calls if x[0]=='Run'],[])
