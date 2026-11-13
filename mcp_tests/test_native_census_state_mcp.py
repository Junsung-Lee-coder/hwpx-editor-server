"""I1 through public MCP -> loopback HTTP -> actual service/owning composition."""
import json
from unittest.mock import patch
from mcp_tests.test_repair_saved_output_and_review import Base,Backend,SID
from tests.native_text_containers_fakes import ContainersHwp
from tests.test_native_census_state import manual,watched,check_held

class CensusStateMCP(Base):
 async def asyncSetUp(self):
  await super().asyncSetUp();self.owned=None;original=Backend.do_POST
  def post(handler):
   if handler.path!='/local-cli/command-bundle':return original(handler)
   body=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
   return handler.reply(self.owned.service.command_bundle(**body))
  p=patch.object(Backend,'do_POST',post);p.start();self.addCleanup(p.stop)
 async def check(self,phase,effect,create=False):
  self.owned,request,state,events,head=manual(self,phase,effect,create=create,session_id=SID)
  with watched(self.owned.hwp,head):reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[request]}})
  self.assertTrue(reply.is_error,reply.structured_content)
  self.assertEqual(reply.structured_content['error']['code'],'BACKEND_REJECTED')
  def find(node):
   if type(node) is dict:
    if type(node.get('steps')) is list and node['steps'] and 'failure_diagnostic' in node['steps'][0]:return node
    for value in node.values():
     answer=find(value)
     if answer is not None:return answer
   elif type(node) is list:
    for value in node:
     answer=find(value)
     if answer is not None:return answer
  result=find(reply.structured_content);self.assertIsNotNone(result)
  check_held(self,self.owned,result,None,events,created=create,label='MCP-'+str(phase)+'-'+effect)
 async def test_initial_and_scan_refresh_state_drift_is_public_technical_hold(self):
  for phase in (1,2):
   for effect in ('caret','selection'):
    with self.subTest(phase=phase,effect=effect):await self.check(phase,effect)
 async def test_post_create_state_drift_retains_public_dirty_unknown_debt(self):
  for effect in ('caret','selection'):
   with self.subTest(effect=effect):await self.check(1,effect,True)
