"""MCP sockets/backend HTTP/real service+owner, installed-name DATA-backed SDK model."""
import json
from unittest.mock import patch
from mcp_tests.test_repair_saved_output_and_review import Base, Backend, SID
from tests.test_shapeobject_installed_accessor import CREATE, strict_owned, assert_read_hold, retain

class InstalledAccessorMCP(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owned=None
        original=Backend.do_POST
        def post(handler):
            if handler.path!='/local-cli/command-bundle':return original(handler)
            payload=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
            return handler.reply(self.owned.service.command_bundle(**payload))
        patcher=patch.object(Backend,'do_POST',post);patcher.start();self.addCleanup(patcher.stop)
    async def test_typed_healthy_creation_through_public_bundle(self):
        self.owned,shape,trace=strict_owned(self,session_id=SID)
        reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
        self.assertFalse(reply.is_error,reply.structured_content)
        result=reply.structured_content['result'];retain(self,self.owned,trace,result)
        self.assertTrue(result['ok'],result)
        self.assertEqual(shape.AdjustTextbox,1)
        self.assertFalse(self.owned.session._native_custody_held)
    async def test_readraise_before_execute_keeps_correct_source05_capsule(self):
        self.owned,shape,trace=strict_owned(self,session_id=SID,faults={'AdjustTextbox':RuntimeError('private synthetic')})
        reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
        self.assertTrue(reply.is_error,reply.structured_content)
        error=reply.structured_content['error'];self.assertEqual(error['code'],'BACKEND_REJECTED')
        def find(node):
            if type(node) is dict:
                if type(node.get('steps')) is list and node['steps'] and 'failure_diagnostic' in node['steps'][0]:return node
                for value in node.values():
                    found=find(value)
                    if found is not None:return found
            elif type(node) is list:
                for value in node:
                    found=find(value)
                    if found is not None:return found
            return None
        result=find(error['details']);self.assertIsNotNone(result,error)
        retain(self,self.owned,trace,result)
        facts=assert_read_hold(self,self.owned,trace,result)
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),('container.ShapeObject.get.AdjustTextbox','raised'))
