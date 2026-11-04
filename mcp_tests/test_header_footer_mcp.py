"""Existing headers and footers at the real MCP boundary: official SDK client, adapter and sockets over a fake REST backend. Not Hancom evidence."""
from __future__ import annotations

import json

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, VERSION

BINDING = {'kind': 'header', 'section': 0, 'apply_to': 'both', 'expected_area_hash': 'sha256:' + 'c' * 24, 'confirm_layout': True}
INVENTORY = {'op': 'header_footer_inventory'}
TEXT = {'op': 'header_footer_text_exact', **BINDING, 'old_text': 'SAMPLE', 'new_text': 'UPDATED'}
TEXT_ODD = {'op': 'header_footer_text_exact', 'kind': 'header', 'section': 1, 'apply_to': 'odd', 'expected_area_hash': 'sha256:' + 'd' * 24, 'confirm_layout': True,
            'paragraph_index': 0, 'expected_text': 'ODD HEADER 2025', 'old_text': '2025', 'new_text': '2026'}
FORMAT = {'op': 'header_footer_format_exact', **BINDING, 'match': 'REPORT', 'bold': True, 'text_color': '#C00000', 'align': 'center'}


class HeaderFooterCommands(Base):
    async def command(self, request):
        return await self.call('hwpx_command', {'session_id': SID, 'request': request})

    async def test_tool_description_and_schema_offer_the_commands(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                tools = await client.list_tools()
        for name in ('hwpx_command', 'hwpx_bundle'):
            tool = next(item for item in tools.tools if item.name == name)
            rendered = json.dumps(tool.input_schema)
            for field in ('header_footer_inventory', 'header_footer_text_exact', 'header_footer_format_exact', 'expected_area_hash', 'apply_to'):
                self.assertIn(field, rendered, name)
        command = next(item for item in tools.tools if item.name == 'hwpx_command')
        for name in ('header_footer_inventory', 'header_footer_text_exact', 'header_footer_format_exact', 'area_inherited'):
            self.assertIn(name, command.description)

    async def test_each_command_reaches_the_backend_as_one_exact_step(self):
        for request in (INVENTORY, TEXT, TEXT_ODD, FORMAT):
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertFalse(result.is_error, result.structured_content)
                body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
                self.assertEqual(len(body['steps']), 1)
                step = body['steps'][0]
                self.assertEqual({key: step[key] for key in request}, request)

    async def test_a_refusal_reaches_the_caller_by_name_and_says_nothing_changed(self):
        for code in ('area_inherited', 'area_not_found', 'area_ambiguous', 'area_hash_mismatch', 'area_unlocatable', 'target_crosses_control'):
            with self.subTest(code=code):
                Backend.bundle_reply = {'ok': False, 'steps': [{'ok': False, 'refusal_code': code, 'mutation_may_have_persisted': False}]}
                result = await self.command(TEXT)
                self.assertTrue(result.is_error)
                details = result.structured_content['error']['details']
                self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), (code, True, False))

    async def test_an_unknown_outcome_reaches_the_caller_as_held(self):
        Backend.bundle_reply = {'ok': False, 'native_custody_held': True, 'steps': [{'ok': False, 'mutation_may_have_persisted': True, 'native_custody_held': True}]}
        details = (await self.command(FORMAT)).structured_content['error']['details']
        self.assertEqual((details['stopped_before_edit'], details['session_held']), (False, True))

    async def test_invalid_requests_never_reach_the_backend(self):
        for request in ({**TEXT, 'apply_to': 'first'}, {**TEXT, 'kind': 'body'}, {**TEXT, 'section': -1}, {**TEXT, 'expected_area_hash': 'sha256:abc'},
                        {**TEXT, 'expected_page': 1}, {**TEXT, 'new_text': 'two\nlines'}, {**TEXT, 'confirm_layout': False},
                        {key: value for key, value in TEXT.items() if key != 'expected_area_hash'}, {**FORMAT, 'through_match': 'X'},
                        {key: value for key, value in FORMAT.items() if key not in {'bold', 'text_color', 'align'}}, {**FORMAT, 'size_pt': 10.05},
                        {**INVENTORY, 'section': 0}):
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual([item for item in Backend.calls if item[1] == '/local-cli/command-bundle'], [])
