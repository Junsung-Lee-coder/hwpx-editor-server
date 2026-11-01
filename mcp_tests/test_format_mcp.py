"""Formatting commands and their review at the real MCP boundary: official SDK client, adapter and sockets over a fake REST backend. Not Hancom evidence."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, VERSION, paragraph

CHARACTER = {'op': 'character_format_exact', 'match': '표적', 'expected_page': 1, 'number_mark': 'follow', 'bold': True, 'size_pt': 14.0, 'text_color': '#C00000', 'font_family': '굴림', 'italic': False, 'underline': True, 'confirm_layout': True}
PARAGRAPH = {'op': 'paragraph_format_exact', 'paragraph_index': 2, 'expected_text': '', 'expected_page': 1, 'align': 'center', 'left_indent_pt': 10.0, 'right_indent_pt': 5.0, 'first_line_indent_pt': -2.5,
             'line_spacing_type': 'percent', 'line_spacing_value': 180, 'confirm_layout': True}
FORMAT = {'runs': [], 'runs_truncated': False, 'paragraph': {'align': 'justify', 'left_indent_pt': 0.0, 'right_indent_pt': 0.0, 'first_line_indent_pt': 0.0, 'line_spacing_type': 'percent', 'line_spacing_value': 160}}
MARK = {'font_family': '바탕', 'size_pt': 10.0, 'bold': False, 'italic': False, 'underline': False, 'text_color': '#000000'}


def run(text, **changes):
    return {'text': text, 'length': len(text), 'font_family': '바탕', 'size_pt': 10.0, 'bold': False, 'italic': False, 'underline': False, 'text_color': '#000000', **changes}


class FormattingCommands(Base):
    async def command(self, request):
        return await self.call('hwpx_command', {'session_id': SID, 'request': request})

    async def test_tool_description_and_schema_offer_the_commands(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                tools = await client.list_tools()
        tool = next(item for item in tools.tools if item.name == 'hwpx_command')
        self.assertIn('character', tool.description.casefold())
        rendered = json.dumps(tool.input_schema)
        for name in ('character_format_exact', 'paragraph_format_exact', 'size_pt', 'text_color', 'first_line_indent_pt', 'line_spacing_type', 'through_match', 'paragraph_index'):
            self.assertIn(name, rendered)

    async def test_both_commands_reach_the_backend_as_one_exact_step(self):
        for request in (CHARACTER, PARAGRAPH):
            with self.subTest(op=request['op']):
                Backend.calls = []
                result = await self.command(request)
                self.assertFalse(result.is_error, result.structured_content)
                body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
                step = body['steps'][0]
                self.assertEqual({key: step[key] for key in request}, request)
                self.assertEqual(len(body['steps']), 1)

    async def test_a_refusal_reaches_the_caller_by_name_and_says_nothing_changed(self):
        Backend.bundle_reply = {'ok': False, 'steps': [{'ok': False, 'refusal_code': 'target_ambiguous', 'mutation_may_have_persisted': False}]}
        result = await self.command(CHARACTER)
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), ('target_ambiguous', True, False))

    async def test_a_wrong_page_refusal_names_the_page(self):
        Backend.bundle_reply = {'ok': False, 'steps': [{'ok': False, 'refusal_code': 'target_wrong_page', 'found_page': 2, 'mutation_may_have_persisted': False}]}
        details = (await self.command(CHARACTER)).structured_content['error']['details']
        self.assertEqual((details['reason'], details['found_page']), ('target_wrong_page', 2))

    async def test_an_unknown_outcome_reaches_the_caller_as_held(self):
        Backend.bundle_reply = {'ok': False, 'native_custody_held': True, 'steps': [{'ok': False, 'mutation_may_have_persisted': True, 'native_custody_held': True}]}
        details = (await self.command(PARAGRAPH)).structured_content['error']['details']
        self.assertEqual((details['stopped_before_edit'], details['session_held']), (False, True))

    async def test_invalid_requests_never_reach_the_backend(self):
        for request in ({**CHARACTER, 'text_color': 'red'}, {**CHARACTER, 'method': 'SelectAll'}, {**CHARACTER, 'confirm_layout': False},
                        {key: value for key, value in PARAGRAPH.items() if key != 'expected_text'}, {**PARAGRAPH, 'align': 'distribute'}):
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual([item for item in Backend.calls if item[1] == '/local-cli/command-bundle'], [])


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class FormattingReview(Base):
    async def test_the_review_shows_changed_ranges_and_values(self):
        Backend.printed = [['Alpha Beta'], ['second page']]
        Backend.readback_paragraphs = [{**paragraph('Alpha Beta', 'NONE'), 'index': 0, 'context': 'body', 'format': {**FORMAT, 'runs': [run('Alpha Beta')]}}]
        first = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        self.assertFalse(first.is_error, first.structured_content)
        Backend.readback_paragraphs = [{**paragraph('Alpha Beta', 'NONE'), 'index': 0, 'context': 'body', 'content_sha256': 'changed',
                                        'format': {**FORMAT, 'runs': [run('Alpha '), run('Beta', bold=True, text_color='#C00000')]}}]
        result = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        value = result.structured_content['result']
        self.assertEqual(value['comparison']['changes'][0]['ranges'], [{'text': 'Beta', 'changes': {'bold': {'before': False, 'after': True}, 'text_color': {'before': '#000000', 'after': '#C00000'}}}])
        self.assertIn('Beta', result.content[0].text)
        markdown = Path(value['summary_path']).read_text(encoding='utf-8')
        self.assertIn('| [0] Alpha Beta | Beta | 글자색 | #000000 | #C00000 |', markdown)


    async def test_a_number_added_after_the_first_review_is_told_as_new_in_the_reply_and_the_file(self):
        Backend.printed = [['Alpha'], ['second page']]
        Backend.readback_paragraphs = [{**paragraph('Alpha', 'NONE'), 'index': 0, 'context': 'body', 'format': {**FORMAT, 'runs': [run('Alpha')]}}]
        first = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        self.assertFalse(first.is_error, first.structured_content)
        Backend.printed = [['1. Alpha'], ['second page']]
        Backend.readback_paragraphs = [{**paragraph('Alpha', 'OUTLINE'), 'index': 0, 'context': 'body', 'content_sha256': 'numbered',
                                        'format': {**FORMAT, 'runs': [run('Alpha')], 'mark': {'kind': 'outline', 'source': 'paragraph_end', **MARK}}}]
        result = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        value = result.structured_content['result']
        self.assertEqual(value['coverage']['number_mark'], {'state': 'new_only', 'numbered': 1, 'compared': 0, 'changed': 0, 'not_compared': 0, 'new': 1})
        self.assertEqual(value['review_status'], 'partial')
        self.assertIn('새 번호 문단 1개', result.content[0].text)
        self.assertNotIn('비교해', result.content[0].text)
        markdown = Path(value['summary_path']).read_text(encoding='utf-8')
        self.assertIn('새 번호 1개', markdown)
        self.assertNotIn('비교했고', markdown)

    def test_a_refusal_gives_its_reason_in_the_first_fields_a_caller_reads(self):
        from hwpx_mcp.server import rejection_summary
        summary = rejection_summary({'ok': False, 'steps': [{'ok': False, 'refusal_code': 'target_changes_number_mark', 'refusal_message': "a range reaching a numbered paragraph's end", 'mutation_may_have_persisted': False}]})
        self.assertEqual(summary['reason'], 'target_changes_number_mark')
        self.assertEqual(summary['message'], "a range reaching a numbered paragraph's end")
        self.assertIs(summary['stopped_before_edit'], True)


if __name__ == '__main__':
    unittest.main()
