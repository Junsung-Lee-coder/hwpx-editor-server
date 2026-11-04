"""Existing-text replacement and table cell writes at the real MCP boundary: official SDK client, adapter and sockets over a fake REST backend. Not Hancom evidence."""
from __future__ import annotations

import json

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, VERSION

REPLACE = {'op': 'text_replace_exact', 'match': 'Annual Plan 2025', 'old_text': '2025', 'new_text': '2026', 'expected_page': 1, 'confirm_layout': True}
REPLACE_OCCURRENCE = {'op': 'text_replace_exact', 'old_text': 'draft', 'new_text': 'final', 'occurrence': 2, 'expected_occurrences': 3, 'format_from': 'last_character',
                      'expected_page': 1, 'confirm_layout': True}
REPLACE_PARAGRAPH = {'op': 'text_replace_exact', 'paragraph_index': 4, 'expected_text': 'Status: draft', 'old_text': 'draft', 'new_text': 'final', 'expected_page': 1, 'confirm_layout': True}
CELLS = {'op': 'table_cell_text_exact', 'table_id': 'tbl:1011', 'expected_hash': 'sha256:' + 'b' * 24, 'expected_page': 1, 'expected_rows': 3, 'expected_cols': 3,
         'cells': [{'address': 'B2', 'expected_text': '1,200', 'text': '1,350'}, {'address': 'C2', 'expected_text': 'Planned', 'text': 'Revised'}], 'confirm_layout': True}


class EditingCommands(Base):
    async def command(self, request):
        return await self.call('hwpx_command', {'session_id': SID, 'request': request})

    async def test_tool_description_and_schema_offer_the_commands(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                tools = await client.list_tools()
        tool = next(item for item in tools.tools if item.name == 'hwpx_command')
        self.assertIn('text_replace_exact', tool.description)
        self.assertIn('table_cell_text_exact', tool.description)
        rendered = json.dumps(tool.input_schema)
        for name in ('text_replace_exact', 'table_cell_text_exact', 'old_text', 'new_text', 'expected_occurrences', 'format_from', 'last_character'):
            self.assertIn(name, rendered)

    async def test_each_command_reaches_the_backend_as_one_exact_step(self):
        for request in (REPLACE, REPLACE_OCCURRENCE, REPLACE_PARAGRAPH, CELLS):
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertFalse(result.is_error, result.structured_content)
                body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
                self.assertEqual(len(body['steps']), 1)
                step = body['steps'][0]
                self.assertEqual({key: step[key] for key in request}, request)

    async def test_a_refusal_reaches_the_caller_by_name_and_says_nothing_changed(self):
        for code in ('target_ambiguous', 'target_count_mismatch', 'target_mixed_format', 'cell_text_mismatch', 'cell_multi_paragraph'):
            with self.subTest(code=code):
                Backend.bundle_reply = {'ok': False, 'steps': [{'ok': False, 'refusal_code': code, 'mutation_may_have_persisted': False}]}
                result = await self.command(CELLS if code.startswith('cell') else REPLACE)
                self.assertTrue(result.is_error)
                details = result.structured_content['error']['details']
                self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), (code, True, False))

    async def test_an_unknown_outcome_reaches_the_caller_as_held(self):
        Backend.bundle_reply = {'ok': False, 'native_custody_held': True, 'steps': [{'ok': False, 'mutation_may_have_persisted': True, 'native_custody_held': True}]}
        details = (await self.command(REPLACE)).structured_content['error']['details']
        self.assertEqual((details['stopped_before_edit'], details['session_held']), (False, True))

    async def test_invalid_requests_never_reach_the_backend(self):
        for request in ({**REPLACE, 'new_text': 'two\nlines'}, {**REPLACE, 'confirm_layout': False}, {**REPLACE, 'occurrence': 2},
                        {**REPLACE, 'format_from': 'first_character'}, {key: value for key, value in REPLACE.items() if key != 'old_text'},
                        {**REPLACE_PARAGRAPH, 'match': 'x'}, {**CELLS, 'cells': []}, {**CELLS, 'cells': [{'address': 'B2', 'text': 'x'}]},
                        {**CELLS, 'cells': [{'address': 'B2', 'expected_text': '1,200', 'text': ''}]}, {**CELLS, 'method': 'SelectAll'}):
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual([item for item in Backend.calls if item[1] == '/local-cli/command-bundle'], [])
