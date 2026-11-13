"""MCP -> HTTP -> real owner post-mutation debt/diagnostic regressions, SDK models."""
import json
from unittest.mock import patch
from mcp_tests.test_repair_saved_output_and_review import Base, Backend, SID
from tests.test_container_postmutation_failure import scenario, CREATE, debt, retained

class PostMutationMCP(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp();self.owned=None
        original=Backend.do_POST
        def post(handler):
            if handler.path!='/local-cli/command-bundle':return original(handler)
            payload=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
            return handler.reply(self.owned.service.command_bundle(**payload))
        patcher=patch.object(Backend,'do_POST',post);patcher.start();self.addCleanup(patcher.stop)
    async def check(self,kind,site):
        self.owned,trace,state=scenario(self,kind,session_id=SID)
        reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
        self.assertTrue(reply.is_error,reply.structured_content)
        error=reply.structured_content['error'];self.assertEqual(error['code'],'BACKEND_REJECTED')
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
            return None
        result=find(error['details']);self.assertIsNotNone(result,error)
        retained(self,self.owned,trace,state,result)
        facts=debt(self,self.owned,trace,state,result)
        self.assertEqual((facts['causal_class'],facts['cause_class']),('NativeNumberingMutationError','NativeNumberingReadFailure'))
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),(site,'raised'))
    async def test_post_execute_read_is_dirty_held_not_pristine(self):await self.check('after_create_read','container.GetTextFile')
    async def test_after_text_read_retains_first_capsule(self):await self.check('after_text_read','container.GetTextFile')
    async def test_post_execute_geometry_proof_retains_grouped_label(self):await self.check('geometry','container.created_box_geometry')
