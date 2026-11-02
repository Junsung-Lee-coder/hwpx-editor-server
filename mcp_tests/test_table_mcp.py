"""Table commands and their review at the real MCP boundary: official SDK client, adapter and sockets over a fake REST backend. Not Hancom evidence."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, VERSION

BOUND = {'table_id': 'tbl:1011', 'expected_hash': 'sha256:' + 'a' * 24, 'expected_page': 1, 'expected_rows': 5, 'expected_cols': 4, 'confirm_layout': True}
REQUESTS = [
    {'op': 'table_inventory'},
    {'op': 'table_read', 'table_id': 'tbl:1011', 'max_cells': 40},
    {'op': 'table_edit_exact', 'action': 'insert_row_below', 'row': 2, 'count': 2, **BOUND},
    {'op': 'table_edit_exact', 'action': 'delete_col', 'col': 2, 'expected_removed_cells': [{'address': 'B1', 'text': 'B1'}, {'address': 'B2', 'text': '그림', 'objects': ['picture']}], **BOUND},
    {'op': 'table_edit_exact', 'action': 'merge_cells', 'cell': 'A1', 'end_cell': 'B2', **BOUND},
    {'op': 'table_edit_exact', 'action': 'split_cell', 'cell': 'A1', 'split_rows': 2, 'split_cols': 2, **BOUND},
    {'op': 'table_edit_exact', 'action': 'unmerge_cell', 'cell': 'A1', **BOUND},
    {'op': 'table_style_exact', 'scope': 'range', 'cell': 'B2', 'end_cell': 'C3', 'fill': '#FFF2CC', 'borders': [{'edges': ['outline'], 'line': 'double', 'width_mm': 0.5, 'color': '#112233'}, {'edges': ['inner_horizontal'], 'line': 'none'}], **BOUND},
    {'op': 'table_style_exact', 'scope': 'table', 'fill': 'none', **BOUND},
    {'op': 'table_create_exact', 'match': '기준 문단', 'expected_page': 1, 'rows': 2, 'cols': 3, 'cells': [['a', 'b', 'c'], ['d', 'e', 'f']], 'confirm_layout': True},
]


def live_cell(address, row, col, text, **fields):
    line = {'line': 'solid', 'width_mm': 0.12, 'color': '#000000'}
    return {'address': address, 'row': row, 'col': col, 'row_span': 1, 'col_span': 1, 'width': 9000, 'text': text, 'objects': [], 'content': f'c:{text}',
            'border': {side: dict(line) for side in ('left', 'right', 'top', 'bottom')}, 'fill': {'kind': 'none'}, 'rest': 'r', **fields}


def live_table(cells, rows=1, cols=2):
    return {'table_id': 'tbl:1011', 'index': 0, 'page': 1, 'rows': rows, 'cols': cols, 'cells': cells, 'before_text': '앞', 'state_sha256': json.dumps(cells, sort_keys=True), 'nested_in': None}


class TableCommands(Base):
    async def command(self, request):
        return await self.call('hwpx_command', {'session_id': SID, 'request': request})

    async def test_the_tool_offers_every_table_command(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                tools = await client.list_tools()
        tool = next(item for item in tools.tools if item.name == 'hwpx_command')
        rendered = json.dumps(tool.input_schema)
        for name in ('table_inventory', 'table_read', 'table_edit_exact', 'table_style_exact', 'table_create_exact', 'unmerge_cell', 'inner_horizontal', 'width_mm', 'expected_removed_cells'):
            self.assertIn(name, rendered)
        for word in ('border', 'fill', 'merge', 'split'):
            self.assertIn(word, tool.description.casefold())

    async def test_each_command_reaches_the_backend_as_one_exact_step(self):
        for request in REQUESTS:
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertFalse(result.is_error, result.structured_content)
                body = next(item[2] for item in Backend.calls if item[1] == '/local-cli/command-bundle')
                self.assertEqual(len(body['steps']), 1)
                step = body['steps'][0]
                self.assertEqual({key: step[key] for key in request}, request)

    async def test_a_refusal_names_its_reason_and_that_nothing_changed(self):
        Backend.bundle_reply = {'ok': False, 'steps': [{'ok': False, 'refusal_code': 'range_cuts_merged_cell', 'refusal_message': 'B1 is part of the merged cell A1', 'mutation_may_have_persisted': False}]}
        details = (await self.command(REQUESTS[4])).structured_content['error']['details']
        self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), ('range_cuts_merged_cell', True, False))
        self.assertIn('A1', details['message'])

    async def test_an_unknown_outcome_reaches_the_caller_as_held(self):
        Backend.bundle_reply = {'ok': False, 'native_custody_held': True, 'steps': [{'ok': False, 'mutation_may_have_persisted': True, 'native_custody_held': True}]}
        details = (await self.command(REQUESTS[7])).structured_content['error']['details']
        self.assertEqual((details['stopped_before_edit'], details['session_held']), (False, True))

    async def test_invalid_requests_never_reach_the_backend(self):
        for request in ({**REQUESTS[2], 'count': 99}, {**REQUESTS[7], 'fill': 'yellow'}, {**REQUESTS[8], 'method': 'CellFill'}, {**REQUESTS[2], 'confirm_layout': False},
                        {'op': 'table_edit_exact', 'action': 'TableMergeCell', 'cell': 'A1', **BOUND}, {**REQUESTS[8], 'borders': [{'edges': ['outline'], 'line': 'wave', 'width_mm': 0.12, 'color': '#000000'}]}):
            with self.subTest(request=request):
                Backend.calls = []
                result = await self.command(request)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INVALID_ARGUMENTS')
                self.assertEqual([item for item in Backend.calls if item[1] == '/local-cli/command-bundle'], [])


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class TableReview(Base):
    async def test_the_review_names_the_table_cells_and_values(self):
        Backend.printed = [['A1 B1'], ['second page']]
        Backend.readback_tables = [live_table([live_cell('A1', 1, 1, 'A1'), live_cell('B1', 1, 2, 'B1')])]
        first = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        self.assertFalse(first.is_error, first.structured_content)
        Backend.readback_tables = [live_table([live_cell('A1', 1, 1, 'A1', fill={'kind': 'solid', 'color': '#FFF2CC'}), live_cell('B1', 1, 2, 'B1')])]
        result = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        value = result.structured_content['result']
        self.assertEqual(value['comparison']['tables']['changed'][0]['fills'], [{'cell': 'A1', 'before': {'kind': 'none'}, 'after': {'kind': 'solid', 'color': '#FFF2CC'}}])
        self.assertNotIn('tables', {key for key in value if key == 'tables'})
        self.assertIn('tbl:1011', result.content[0].text)
        markdown = Path(value['summary_path']).read_text(encoding='utf-8')
        self.assertIn('## 표 변경', markdown)
        self.assertIn('#FFF2CC', markdown)
        stored = json.loads(Path(value['json_path']).read_text(encoding='utf-8'))
        self.assertEqual(stored['tables'][0]['table_id'], 'tbl:1011')


if __name__ == '__main__':
    unittest.main()
