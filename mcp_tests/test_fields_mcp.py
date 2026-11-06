"""Existing named fields at the real MCP boundary: official SDK client, adapter and sockets over a fake REST backend. Not Hancom evidence."""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx2
import httpx2._content
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, VERSION

INVENTORY = {'op': 'field_inventory'}
ONE_NAME = {'op': 'field_inventory', 'name': '성명'}
FILL = {'op': 'field_text_exact', 'name': '성명', 'expected_hash': 'sha256:' + 'e' * 24, 'confirm_layout': True, 'new_text': '김철수'}
FILL_SECOND = {'op': 'field_text_exact', 'name': '서명', 'occurrence': 2, 'expected_occurrences': 2, 'expected_hash': 'sha256:' + 'f' * 24,
               'confirm_layout': True, 'new_text': '박\t병삼'}
CLEAR = {**FILL, 'new_text': ''}


class FieldCommands(Base):
    async def command(self, request):
        return await self.call('hwpx_command', {'session_id': SID, 'request': request})

    async def test_tool_description_and_schema_offer_the_commands(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                tools = await client.list_tools()
        for name in ('hwpx_command', 'hwpx_bundle'):
            tool = next(item for item in tools.tools if item.name == name)
            rendered = json.dumps(tool.input_schema)
            for field in ('field_inventory', 'field_text_exact', 'expected_occurrences', 'new_text'):
                self.assertIn(field, rendered, name)
        command = next(item for item in tools.tools if item.name == 'hwpx_command')
        for name in ('field_inventory', 'field_text_exact', 'field_occurrence_required', '[[BR::NAME]]'):
            self.assertIn(name, command.description)

    async def test_each_command_reaches_the_backend_as_one_exact_step(self):
        for request in (INVENTORY, ONE_NAME, FILL, FILL_SECOND, CLEAR):
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertFalse(result.is_error, result.structured_content)
                body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
                self.assertEqual(len(body['steps']), 1)
                step = body['steps'][0]
                self.assertEqual({key: step[key] for key in request}, request)
                self.assertNotIn('expected_page', step)

    async def test_a_bundle_carries_the_inventory_and_the_fill_in_order(self):
        Backend.calls = []
        result = await self.call('hwpx_bundle', {'session_id': SID, 'request': {'steps': [INVENTORY, FILL]}})
        self.assertFalse(result.is_error, result.structured_content)
        body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
        self.assertEqual([step['op'] for step in body['steps']], ['field_inventory', 'field_text_exact'])

    async def test_a_refusal_reaches_the_caller_by_name_and_says_nothing_changed(self):
        for code in ('field_not_found', 'field_occurrence_required', 'field_count_mismatch', 'field_hash_mismatch', 'field_kind_unsupported',
                     'field_multi_paragraph', 'field_text_unprovable', 'field_census_unusable'):
            with self.subTest(code=code):
                Backend.bundle_reply = {'ok': False, 'steps': [{'ok': False, 'refusal_code': code, 'mutation_may_have_persisted': False}]}
                result = await self.command(FILL)
                self.assertTrue(result.is_error)
                details = result.structured_content['error']['details']
                self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), (code, True, False))

    async def test_an_unknown_outcome_reaches_the_caller_as_held(self):
        Backend.bundle_reply = {'ok': False, 'native_custody_held': True, 'steps': [{'ok': False, 'mutation_may_have_persisted': True, 'native_custody_held': True}]}
        details = (await self.command(FILL)).structured_content['error']['details']
        self.assertEqual((details['stopped_before_edit'], details['session_held']), (False, True))

    async def test_invalid_requests_never_reach_the_backend(self):
        bad = ({**FILL, 'new_text': 'two\nlines'}, {**FILL, 'new_text': 'cr\r'}, {**FILL, 'new_text': 'x' * 2001}, {**FILL, 'new_text': 'a\x02b'},
               {**FILL, 'confirm_layout': False}, {key: value for key, value in FILL.items() if key != 'confirm_layout'},
               {key: value for key, value in FILL.items() if key != 'expected_hash'}, {**FILL, 'expected_hash': 'sha256:abc'}, {**FILL, 'name': ''},
               {**FILL, 'name': 'a{{0}}'}, {**FILL, 'name': 'tab\tname'}, {**FILL, 'occurrence': 1}, {**FILL, 'expected_occurrences': 2},
               {**FILL_SECOND, 'occurrence': 3}, {**FILL_SECOND, 'occurrence': 0}, {**FILL, 'expected_page': 1}, {**FILL, 'new_text': 3},
               {**INVENTORY, 'occurrence': 1}, {**INVENTORY, 'name': 'a}}'})
        for request in bad:
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual([item for item in Backend.calls if item[1] == '/local-cli/command-bundle'], [])

    async def test_a_lone_surrogate_in_new_text_or_name_never_reaches_the_backend(self):
        """The official client cannot encode a lone surrogate at all (its JSON body is UTF-8); a client that escapes it in JSON text
        reaches the real server, which refuses it, in new_text or in a name, before the backend, as the backend refuses it itself."""
        real = httpx2._content.json_dumps

        def escaping(value, **options):
            return real(value, **{**options, 'ensure_ascii': True})
        requests = [{**FILL, 'new_text': text} for text in ('\ud800', 'a\udfffb', '\ude00\ud83d')]
        requests += [{**INVENTORY, 'name': '\ud800'}, {**FILL, 'name': 'a\udfffb'}, {**FILL_SECOND, 'name': '\ude00\ud83d'}]
        for request in requests:
            with self.subTest(request=ascii(request)):
                Backend.calls = []
                with patch.object(httpx2._content, 'json_dumps', escaping):
                    result = await self.command(request)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual([item for item in Backend.calls if item[1] == '/local-cli/command-bundle'], [])
        Backend.calls = []
        with patch.object(httpx2._content, 'json_dumps', escaping):
            result = await self.command({**FILL, 'new_text': '\U0001F600'})
        self.assertFalse(result.is_error, result.structured_content)
        body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
        self.assertEqual(body['steps'][0]['new_text'], '\U0001F600', 'a character outside the BMP is not a lone surrogate')
        Backend.calls = []
        with patch.object(httpx2._content, 'json_dumps', escaping):
            result = await self.command({**INVENTORY, 'name': '성명\U0001F600'})
        self.assertFalse(result.is_error, result.structured_content)
        body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
        self.assertEqual(body['steps'][0]['name'], '성명\U0001F600', 'a name with a character outside the BMP is valid')
