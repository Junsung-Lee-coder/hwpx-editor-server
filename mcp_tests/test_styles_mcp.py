"""Named styles at the real MCP boundary: official SDK client, adapter and sockets over a fake REST backend.

The backend replies are the real backend's own command-bundle bodies for these commands, kept in
tests/fixtures/native_styles/backend_replies.json and checked against the backend by tests/test_native_styles_wire.py.
Not Hancom evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx2
import httpx2._content
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, VERSION

REPLIES = json.loads((Path(__file__).resolve().parents[1] / 'tests' / 'fixtures' / 'native_styles' / 'backend_replies.json').read_text(encoding='utf-8'))
HASH = 'sha256:' + 'a' * 24
INVENTORY = {'op': 'style_inventory'}
ONE = {'op': 'style_inventory', 'name': '본문'}
APPLY = {'op': 'style_apply_exact', 'style_name': '개요 1', 'expected_style_hash': HASH, 'paragraph_index': 3, 'expected_text': '바탕 둘째 문단',
         'expected_page': 1, 'confirm_layout': True}
APPLY_RANGE = {'op': 'style_apply_exact', 'style_name': '강조', 'expected_style_hash': HASH, 'match': '첫 문단', 'through_match': '둘째', 'expected_page': 2,
               'confirm_layout': True}
CREATE = {'op': 'style_create_exact', 'name': '강조 본문', 'kind': 'paragraph', 'based_on': '본문', 'expected_based_on_hash': HASH,
          'expected_styles_hash': HASH, 'size_pt': 14, 'bold': True, 'align': 'center', 'confirm_layout': True}
CREATE_CHARACTER = {'op': 'style_create_exact', 'name': '빨강', 'english_name': 'Red', 'kind': 'character', 'based_on': '바탕글', 'expected_based_on_hash': HASH,
                    'expected_styles_hash': HASH, 'text_color': '#FF0000', 'confirm_layout': True}
UPDATE = {'op': 'style_update_exact', 'style_name': '본문', 'expected_style_hash': HASH, 'expected_uses': 4, 'expected_uses_hash': HASH, 'all_uses': True,
          'size_pt': 11, 'line_spacing_type': 'percent', 'line_spacing_value': 180, 'confirm_layout': True}
UPDATE_SIZE = {'op': 'style_update_exact', 'style_name': '본문', 'expected_style_hash': HASH, 'expected_uses': 4, 'expected_uses_hash': HASH, 'all_uses': True,
               'size_pt': 11, 'confirm_layout': True}
RENAME = {'op': 'style_update_exact', 'style_name': '본문', 'expected_style_hash': HASH, 'expected_uses': 0, 'expected_uses_hash': HASH, 'new_name': '본문 새이름',
          'confirm_layout': True}


class StyleCommands(Base):
    async def command(self, request):
        return await self.call('hwpx_command', {'session_id': SID, 'request': request})

    async def test_tool_description_and_schema_offer_the_commands(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                tools = await client.list_tools()
        for name in ('hwpx_command', 'hwpx_bundle'):
            tool = next(item for item in tools.tools if item.name == name)
            rendered = json.dumps(tool.input_schema)
            for field in ('style_inventory', 'style_apply_exact', 'style_create_exact', 'style_update_exact', 'expected_style_hash', 'expected_uses_hash',
                          'all_uses', 'based_on', 'expected_styles_hash'):
                self.assertIn(field, rendered, name)
        command = next(item for item in tools.tools if item.name == 'hwpx_command')
        for name in ('style_inventory', 'style_apply_exact', 'style_create_exact', 'style_update_exact', 'all_uses=true'):
            self.assertIn(name, command.description)

    async def test_each_command_reaches_the_backend_as_one_exact_step(self):
        for request in (INVENTORY, ONE, APPLY, APPLY_RANGE, CREATE, CREATE_CHARACTER, UPDATE, RENAME):
            with self.subTest(request=request['op']):
                Backend.calls = []
                Backend.bundle_reply = {'ok': True, 'steps': []}
                result = await self.command(request)
                self.assertFalse(result.is_error, result.structured_content)
                body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
                self.assertEqual(len(body['steps']), 1)
                step = body['steps'][0]
                self.assertEqual({key: step[key] for key in request}, request)

    async def test_the_backends_own_replies_reach_the_caller(self):
        for name in ('inventory', 'apply', 'create', 'update'):
            with self.subTest(reply=name):
                Backend.bundle_reply = REPLIES[name]
                result = await self.command(INVENTORY if name == 'inventory' else APPLY)
                self.assertFalse(result.is_error, result.structured_content)
                rendered = json.dumps(result.structured_content, ensure_ascii=False)
                expected = REPLIES[name]['steps'][0]['result']
                for key in {'inventory': ('styles_hash', 'by_context'), 'apply': ('style_after', 'characters'), 'create': ('next_binding', 'expected_styles_hash'),
                            'update': ('affected', 'expected_uses_hash')}[name]:
                    self.assertIn(key, rendered)
                self.assertIn(json.dumps(expected['schema_version']), rendered)

    async def test_a_backend_refusal_reaches_the_caller_by_name_and_says_nothing_changed(self):
        Backend.bundle_reply = REPLIES['refusal']
        result = await self.command(APPLY)
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), ('style_hash_mismatch', True, False))
        for code in ('style_not_found', 'style_name_ambiguous', 'style_kind_unsupported', 'style_name_taken', 'styles_hash_mismatch', 'style_without_plain_body_use',
                     'style_action_unavailable', 'style_uses_unconfirmed', 'style_uses_mismatch', 'style_use_has_own_format',
                     'style_uses_too_many', 'style_model_unusable', 'style_variant_unsupported', 'style_rename_unsupported', 'style_shape_unverified'):
            with self.subTest(code=code):
                Backend.bundle_reply = {'ok': False, 'steps': [{'ok': False, 'refusal_code': code, 'mutation_may_have_persisted': False}]}
                details = (await self.command(UPDATE)).structured_content['error']['details']
                self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), (code, True, False))

    async def test_a_backend_unknown_outcome_reaches_the_caller_as_held(self):
        Backend.bundle_reply = REPLIES['held']
        details = (await self.command(UPDATE)).structured_content['error']['details']
        self.assertEqual((details['stopped_before_edit'], details['session_held']), (False, True))

    async def test_the_backends_next_style_refusal_reaches_the_caller_by_name(self):
        Backend.bundle_reply = REPLIES['next_style_refusal']
        result = await self.command({**CREATE, 'next_style': '쪽 번호'})
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), ('style_kind_unsupported', True, False))
        self.assertIn('next_style', details['message'])

    async def test_the_backends_technical_hold_reaches_the_caller_held_and_without_a_refusal_code(self):
        Backend.bundle_reply = REPLIES['technical_hold']
        result = await self.command(APPLY)
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertTrue(details['session_held'])
        self.assertNotIn('reason', details, 'a technical fault is never reported as a refusal')
        self.assertTrue(details['stopped_before_edit'], 'it held before the action was issued')

    async def test_the_backends_entry_holds_reach_the_caller_held_and_without_a_refusal_code(self):
        for name, request in (('capability_hold', APPLY), ('document_text_hold', INVENTORY)):
            with self.subTest(reply=name):
                Backend.bundle_reply = REPLIES[name]
                result = await self.command(request)
                self.assertTrue(result.is_error)
                details = result.structured_content['error']['details']
                self.assertTrue(details['session_held'])
                self.assertNotIn('reason', details, 'an unreadable capability or document text is never reported as a refusal')
                self.assertTrue(details['stopped_before_edit'])

    async def test_the_backends_variant_and_rename_refusals_reach_the_caller_by_name(self):
        for name, request, code in (('variant_refusal', {**CREATE, 'align': 'center'}, 'style_variant_unsupported'),
                                    ('rename_refusal', RENAME, 'style_rename_unsupported')):
            with self.subTest(reply=name):
                Backend.bundle_reply = REPLIES[name]
                result = await self.command(request)
                self.assertTrue(result.is_error)
                details = result.structured_content['error']['details']
                self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), (code, True, False))
                self.assertTrue(details['message'])

    async def test_the_backends_legacy_hold_reaches_the_caller_with_its_fixed_site_and_no_message(self):
        """GetDefault answering True, or an int other than the observed 1, holds the create at get_default.call."""
        for name, shape in (('legacy_hold', 'true'), ('get_default_hold', 'int')):
            with self.subTest(reply=name):
                Backend.bundle_reply = REPLIES[name]
                result = await self.command(CREATE)
                self.assertTrue(result.is_error)
                details = result.structured_content['error']['details']
                self.assertTrue(details['session_held'])
                self.assertNotIn('reason', details, 'a technical hold is never a refusal')
                self.assertNotIn('message', details)
                self.assertEqual((details['native_site'], details['native_return']), ('get_default.call', shape))
                self.assertTrue(details['stopped_before_edit'])

    async def test_a_held_proof_reaches_the_caller_with_the_predicate_it_broke(self):
        Backend.bundle_reply = REPLIES['proof_hold']
        result = await self.command(CREATE)
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual((details['session_held'], details['stopped_before_edit']), (True, False))
        self.assertNotIn('reason', details, 'an unproven outcome is never a refusal')
        self.assertIn("the new style's next style is 강조, not 개요 1", details['message'])
        self.assertEqual((details['style_proof']['old_styles'], details['style_proof']['new_styles'], details['style_proof']['added']),
                         (5, 6, {'name': '보고서 본문', 'index': 5}))
        self.assertEqual(details['style_proof']['problems'], [{'predicate': 'new_style_wrong', 'name': '보고서 본문', 'new_index': 5, 'fields': ['next'],
                                                               'expected': '개요 1', 'found': '강조'}])

    async def test_proof_facts_outside_their_fixed_shape_never_reach_the_caller_summary(self):
        changes = (lambda facts: facts.update(schema='other'), lambda facts: facts['problems'][0].update(predicate='PRIVATE'),
                   lambda facts: facts['problems'][0].update(name='x' * 65), lambda facts: facts.update(extra=1),
                   lambda facts: facts['problems'][0].update(where='paragraph text'), lambda facts: facts.update(problem_count=0))
        for number, change in enumerate(changes):
            with self.subTest(change=number):
                reply = json.loads(json.dumps(REPLIES['proof_hold']))
                change(reply['steps'][0]['style_proof'])
                Backend.bundle_reply = reply
                details = (await self.command(CREATE)).structured_content['error']['details']
                self.assertNotIn('style_proof', details)
                self.assertTrue(details['session_held'])

    async def test_a_held_update_reaches_the_caller_with_what_differed_in_its_character_shape(self):
        Backend.bundle_reply = REPLIES['update_proof_hold']
        result = await self.command(UPDATE_SIZE)
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual((details['session_held'], details['stopped_before_edit']), (True, False))
        self.assertNotIn('reason', details, 'an unproven outcome is never a refusal')
        self.assertIn("the style's character shape is not the old one with the requested properties", details['message'])
        self.assertEqual(details['style_proof']['problems'], [{'predicate': 'changed_style_wrong', 'name': '본문', 'old_index': 1, 'new_index': 1,
                                                               'fields': ['character'], 'size_expected': 11.0, 'size_found': 11.0,
                                                               'character_items': ['attr_shadecolor'], 'character_item_count': 1, 'unrequested_changed': True}])

    async def test_the_backends_shape_refusal_reaches_the_caller_by_name(self):
        Backend.bundle_reply = REPLIES['shape_refusal']
        result = await self.command(UPDATE_SIZE)
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), ('style_shape_unverified', True, False))
        self.assertIn('nothing was written', details['message'])

    async def test_character_facts_outside_their_fixed_shape_never_reach_the_caller_summary(self):
        changes = ({'size_expected': '11'}, {'size_found': -1}, {'character_items': ['child ratio']}, {'character_items': []},
                   {'character_item_count': 0}, {'unrequested_changed': 'yes'}, {'character_items': ['italic', 'italic'], 'character_item_count': 2})
        for number, change in enumerate(changes):
            with self.subTest(change=number):
                reply = json.loads(json.dumps(REPLIES['update_proof_hold']))
                reply['steps'][0]['style_proof']['problems'][0].update(change)
                Backend.bundle_reply = reply
                details = (await self.command(UPDATE_SIZE)).structured_content['error']['details']
                self.assertNotIn('style_proof', details)
                self.assertTrue(details['session_held'])

    async def test_a_site_label_outside_the_allowlist_never_reaches_the_caller_summary(self):
        for site, returned in (('PRIVATE_SITE', 'true'), ('get_default.call', 'PRIVATE'), (1, 'true')):
            with self.subTest(site=site, returned=returned):
                reply = json.loads(json.dumps(REPLIES['legacy_hold']))
                reply['steps'][0]['failure_diagnostic'].update(native_site=site, native_return=returned)
                Backend.bundle_reply = reply
                details = (await self.command(CREATE)).structured_content['error']['details']
                self.assertNotIn('native_site', details)
                self.assertNotIn('native_return', details)

    async def test_the_backends_absent_capability_refusal_reaches_the_caller_by_name(self):
        Backend.bundle_reply = REPLIES['absent_refusal']
        result = await self.command(APPLY)
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), ('native_unavailable', True, False))

    async def test_invalid_requests_never_reach_the_backend(self):
        bad = (
            {**APPLY, 'confirm_layout': False}, {key: value for key, value in APPLY.items() if key != 'expected_style_hash'}, {**APPLY, 'expected_style_hash': 'sha256:abc'},
            {**APPLY, 'style_name': ''}, {**APPLY, 'style_name': ' 본문'}, {**APPLY, 'style_name': 'tab\tname'}, {**APPLY, 'style_name': 'x' * 65},
            {**APPLY, 'bold': True}, {key: value for key, value in APPLY.items() if key != 'expected_page'},
            {**CREATE, 'kind': 'table'}, {**CREATE_CHARACTER, 'align': 'center'}, {**CREATE_CHARACTER, 'next_style': '본문'}, {**CREATE, 'size_pt': 10.05},
            {key: value for key, value in CREATE.items() if key != 'expected_styles_hash'}, {**CREATE, 'name': '새\x01것'},
            {key: value for key, value in UPDATE.items() if key not in {'size_pt', 'line_spacing_type', 'line_spacing_value'}},
            {**UPDATE, 'expected_uses': -1}, {**UPDATE, 'all_uses': 'yes'}, {**UPDATE, 'based_on': '바탕글'}, {**UPDATE, 'line_spacing_value': None},
            {**INVENTORY, 'kind': 'paragraph'}, {**INVENTORY, 'name': ''},
        )
        for request in bad:
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual([item for item in Backend.calls if item[1] == '/local-cli/command-bundle'], [])

    async def test_a_lone_surrogate_in_a_style_name_never_reaches_the_backend(self):
        real = httpx2._content.json_dumps

        def escaping(value, **options):
            return real(value, **{**options, 'ensure_ascii': True})
        for request in ({**INVENTORY, 'name': '\ud800'}, {**APPLY, 'style_name': 'a\udfffb'}, {**CREATE, 'name': '새\ud800'}, {**UPDATE, 'new_name': '\ude00'}):
            with self.subTest(request=ascii(request)):
                Backend.calls = []
                with patch.object(httpx2._content, 'json_dumps', escaping):
                    result = await self.command(request)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual([item for item in Backend.calls if item[1] == '/local-cli/command-bundle'], [])
        Backend.calls = []
        Backend.bundle_reply = {'ok': True, 'steps': []}
        with patch.object(httpx2._content, 'json_dumps', escaping):
            result = await self.command({**INVENTORY, 'name': '본문\U0001F600'})
        self.assertFalse(result.is_error, result.structured_content)

    async def test_a_bundle_carries_the_inventory_and_an_edit_in_order(self):
        Backend.calls = []
        Backend.bundle_reply = {'ok': True, 'steps': []}
        result = await self.call('hwpx_bundle', {'session_id': SID, 'request': {'steps': [INVENTORY, APPLY]}})
        self.assertFalse(result.is_error, result.structured_content)
        body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
        self.assertEqual([step['op'] for step in body['steps']], ['style_inventory', 'style_apply_exact'])
