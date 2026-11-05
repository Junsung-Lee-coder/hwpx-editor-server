"""Picture schemas and dispatch over the real MCP SDK; HTTP backend is deliberately fake.
This does not qualify a native picture edit or its saved/rendered appearance.
"""
import json
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, VERSION

BINDING={'target_id':'picture:12345','expected_hash':'sha256:'+'a'*24,'confirm_layout':True}
REQUESTS=[{'op':'picture_inventory'},
          {'op':'picture_replace_exact',**BINDING,'source_path':'/owned/new.png'},
          {'op':'picture_crop_exact',**BINDING,'left':0.25,'top':0.25,'right':0.75,'bottom':0.75},
          {'op':'picture_layout_exact',**BINDING,'mode':'square','horizontal_offset_mm':15.0},
          {'op':'picture_layout_exact',**BINDING,'mode':'inline'}]

class PictureCommands(Base):
    async def test_tool_schema_and_description_offer_existing_picture_operations(self):
        async with httpx2.AsyncClient(headers={'Authorization':'Bearer '+self.token},timeout=30) as http:
            async with Client(streamable_http_client(self.url,http_client=http),mode=VERSION) as client:
                tools=await client.list_tools()
        command=next(t for t in tools.tools if t.name=='hwpx_command')
        for name in ('hwpx_command','hwpx_bundle'):
            schema=json.dumps(next(t for t in tools.tools if t.name==name).input_schema)
            for request in REQUESTS:self.assertIn(request['op'],schema)
        for request in REQUESTS:self.assertIn(request['op'],command.description)
        self.assertIn('unqualified',command.description)
    async def test_every_picture_request_reaches_backend_without_field_loss(self):
        for request in REQUESTS:
            with self.subTest(op=request['op']):
                Backend.calls=[]
                result=await self.call('hwpx_command',{'session_id':SID,'request':request})
                self.assertFalse(result.is_error,result.structured_content)
                body=next(item[2] for item in Backend.calls if item[1]=='/local-cli/command-bundle')
                self.assertEqual(len(body['steps']),1)
                self.assertEqual({k:body['steps'][0][k] for k in request},request)
    async def test_picture_bundle_reaches_backend_in_order(self):
        Backend.calls=[]
        result=await self.call('hwpx_bundle',{'session_id':SID,'request':{'steps':REQUESTS}})
        self.assertFalse(result.is_error,result.structured_content)
        body=next(item[2] for item in Backend.calls if item[1]=='/local-cli/command-bundle')
        self.assertEqual(len(body['steps']),len(REQUESTS))
        for actual,request in zip(body['steps'],REQUESTS):
            self.assertEqual({k:actual[k] for k in request},request)
