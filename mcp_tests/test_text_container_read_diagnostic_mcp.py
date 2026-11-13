"""Official MCP sockets -> backend HTTP -> real service/owner, synthetic SDK only."""
import json
from unittest.mock import patch
from mcp_tests.test_repair_saved_output_and_review import Base, Backend, SID
from tests.test_text_container_read_diagnostic import CREATE, owned_probe, assert_held


class ReadDiagnosticMCP(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.owned = self.probe = None
        original = Backend.do_POST
        def post(handler):
            if handler.path != '/local-cli/command-bundle':
                return original(handler)
            payload=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
            return handler.reply(self.owned.service.command_bundle(**payload))
        self.patcher=patch.object(Backend,'do_POST',post)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def check(self, kind, site, returned):
        self.owned,self.probe=owned_probe(self,kind,session_id=SID)
        reply=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':[CREATE]}})
        self.assertTrue(reply.is_error,reply.structured_content)
        error=reply.structured_content['error']
        self.assertEqual(error['code'],'BACKEND_REJECTED')
        # Adapter's retained backend result carries the same bounded failure capsule.
        def find(node):
            if type(node) is dict:
                if type(node.get('steps')) is list and node['steps'] and 'failure_diagnostic' in node['steps'][0]: return node
                for value in node.values():
                    found=find(value)
                    if found is not None:return found
            elif type(node) is list:
                for value in node:
                    found=find(value)
                    if found is not None:return found
            return None
        result=find(error['details'])
        self.assertIsNotNone(result,error)
        facts=assert_held(self,self.owned,self.probe,result)
        self.assertEqual((facts.get('native_site'),facts.get('native_return')),(site,returned))

    async def test_raw_native_raise_keeps_custody_and_site_through_bundle(self):
        await self.check('raised','container.GetTextFile','raised')
    async def test_invalid_raw_contract_is_not_parser_failure(self):
        await self.check('none','container.GetTextFile','none')
    async def test_outline_parser_failure_is_not_raw_getter_failure(self):
        await self.check('outline','container.document.outline','raised')
