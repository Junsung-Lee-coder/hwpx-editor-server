"""Real MCP sockets -> real service/packages -> model SDK. NOT native evidence.

Run with both pinned portable backend and MCP dependencies available. Backend
session admission and SDK are test-owned; command validation, dispatch, public
projection and adapter transport are the real product implementations.
"""
import json
from types import MethodType
from unittest.mock import patch
from app.local_cli_service import LocalCliService
from tests.native_text_containers_fakes import ContainersHwp, container
from tests.native_format_fakes import paragraph
from tests.test_native_paragraph_edit import Flow
from tests.test_native_text_container_repairs import outline_pair, root_read_hook
from mcp_tests.test_repair_saved_output_and_review import Base, Backend, SID


class NativeTextContainerSeam(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.hwp = ContainersHwp()
        self.flow = Flow(self.hwp)
        self.flow.binding['session_id'] = SID
        # Do not replace the public projection with the workflow fixture's stub.
        self.flow.service._public_bundle_steps = MethodType(LocalCliService._public_bundle_steps, self.flow.service)
        flow = self.flow
        original = Backend.do_POST
        def post(handler):
            if handler.path != '/local-cli/command-bundle':
                return original(handler)
            payload = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
            with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
                result = flow.service.command_bundle(**payload)
            return handler.reply(result)
        self.patcher = patch.object(Backend, 'do_POST', post)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def command(self, request):
        return await self.call('hwpx_command', {'session_id': SID, 'request': request})

    async def item(self):
        result = await self.command({'op': 'text_container_inventory'})
        self.assertFalse(result.is_error, result.structured_content)
        return result.structured_content['result']['steps'][0]['result']['containers'][0]

    async def test_inventory_read_and_complete_replacement_all_three_kinds(self):
        for kind in ('text_box', 'caption', 'footnote'):
            with self.subTest(kind=kind):
                self.hwp.body[-1]['atoms'][-1]['container_kind'] = kind
                self.hwp._number_cells(self.hwp.body)
                item = await self.item()
                read = await self.command({'op': 'text_container_read', 'target_id': item['target_id'], 'expected_hash': item['hash']})
                self.assertFalse(read.is_error, read.structured_content)
                result = await self.command({'op': 'text_container_text_exact', 'target_id': item['target_id'], 'expected_hash': item['hash'],
                                             'expected_text': item['text'], 'new_text': 'MCP caller text ' + kind, 'confirm_layout': True})
                self.assertFalse(result.is_error, result.structured_content)
                self.assertEqual((await self.item())['text'], 'MCP caller text ' + kind)

    async def test_creation_through_bundle_and_public_result(self):
        self.hwp.cursor = (0, 1, 0)
        result = await self.call('hwpx_bundle', {'session_id': SID, 'request': {'steps': [
            {'op': 'text_box_create', 'text': 'MCP created text', 'width_mm': 40, 'height_mm': 20,
             'expected_pos': [0, 1, 0], 'confirm_layout': True}]}})
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(result.structured_content['result']['steps'][0]['result']['container']['text'], 'MCP created text')

    async def test_partial_text_refusal_is_not_a_generic_success(self):
        item = await self.item()
        result = await self.command({'op': 'text_container_text_exact', 'target_id': item['target_id'], 'expected_hash': item['hash'],
                                     'expected_text': 'Box', 'new_text': 'Bad', 'confirm_layout': True})
        self.assertTrue(result.is_error, result.structured_content)
        self.assertEqual(result.structured_content['error']['details']['reason'], 'container_text_mismatch')
        self.assertEqual(self.hwp.executes(), [])

    async def test_entered_malformed_read_stops_at_the_service_seam(self):
        def broken(fmt, option):
            self.hwp.calls.append(('FAULT',))
            return '<broken'
        self.hwp.GetTextFile = broken
        result = await self.command({'op': 'text_container_inventory'})
        self.assertTrue(result.is_error, result.structured_content)
        self.assertEqual(self.hwp.calls[self.hwp.calls.index(('FAULT',))+1:], [])

    async def test_grouped_owner_inventory_and_mcp_mutation_admission_agree(self):
        root_read_hook(self.hwp, lambda root:next(e for e in root.iter() if e.tag=='SHAPECOMPONENT').set('GroupLevel','1'))
        item=await self.item()
        self.assertFalse(item['editable']);self.assertEqual(item['refusal_code'],'container_grouped_owner')
        self.hwp.calls.clear()
        result=await self.command({'op':'text_container_text_exact','target_id':item['target_id'],'expected_hash':item['hash'],
                                   'expected_text':item['text'],'new_text':'Must not write','confirm_layout':True})
        self.assertTrue(result.is_error,result.structured_content)
        self.assertEqual(result.structured_content['error']['details']['reason'],'container_grouped_owner')
        self.assertFalse(any(c[0] in ('GetPos','set_pos','Execute') for c in self.hwp.calls))

    async def test_outline_rebinding_is_not_certified_preserved_through_mcp(self):
        outline_pair(self.hwp,True)
        item=await self.item()
        result=await self.command({'op':'text_container_text_exact','target_id':item['target_id'],'expected_hash':item['hash'],
                                   'expected_text':item['text'],'new_text':'MCP replacement','confirm_layout':True})
        self.assertTrue(result.is_error,result.structured_content)

    async def test_creation_wrong_anchor_stops_at_first_post_creation_read(self):
        self.hwp.cursor=(0,1,0)
        execute=self.hwp.HAction.Execute
        def wrong(name, pset):
            if name=='DrawObjCreatorTextBox':self.hwp.cursor=(0,2,0)
            return execute(name,pset)
        self.hwp.HAction.Execute=wrong
        root_read_hook(self.hwp,lambda root:None,post_creation=True)
        result=await self.command({'op':'text_box_create','text':'MCP text','width_mm':40,'height_mm':20,
                                   'expected_pos':[0,1,0],'confirm_layout':True})
        self.assertTrue(result.is_error,result.structured_content)
        self.assertEqual(self.hwp.calls[self.hwp.calls.index(('POST_CREATION_EVIDENCE',))+1:],[])

    async def test_collapsed_actual_geometry_stops_before_more_sdk(self):
        self.hwp.cursor=(0,1,0)
        root_read_hook(self.hwp,lambda root:next(e for e in root.iter() if e.tag=='SCAMATRIX').set('E1','0'),post_creation=True)
        result=await self.command({'op':'text_box_create','text':'MCP text','width_mm':40,'height_mm':20,
                                   'expected_pos':[0,1,0],'confirm_layout':True})
        self.assertTrue(result.is_error,result.structured_content)
        self.assertEqual(self.hwp.calls[self.hwp.calls.index(('POST_CREATION_EVIDENCE',))+1:],[])

    async def test_out_of_long_selected_position_has_no_follow_on(self):
        self.hwp.cursor=(0,1,0)
        def malformed():
            self.hwp.calls.append(('SELECTION_FAULT',))
            return (True,0,0,0,0,0,2**31)
        self.hwp.GetSelectedPos=malformed
        result=await self.command({'op':'text_box_create','text':'MCP text','width_mm':40,'height_mm':20,
                                   'expected_pos':[0,1,0],'confirm_layout':True})
        self.assertTrue(result.is_error,result.structured_content)
        self.assertEqual(self.hwp.calls[self.hwp.calls.index(('SELECTION_FAULT',))+1:],[])
