"""columns apply_to from_caret_new at the real MCP boundary: official SDK client, adapter and sockets over fake REST backends.

The schema keeps the recognized value so the request reaches the backend, which refuses it before any SDK call with
columns_from_caret_new_unsupported; the adapter reports that refusal by name, as stopped before any edit and not held. The
tool schema names the refusal instead of offering the value as a normal edit, and hwpx_capabilities lists the variant as
unsupported with that code and nothing to undo. Not Hancom evidence.
"""
from __future__ import annotations

import json
import unittest

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from mcp_tests import test_backend_parity_mcp as parity_transport  # the module, so its test class is not collected here again
from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, VERSION

CODE = 'columns_from_caret_new_unsupported'
STEP = {'op': 'layout_exact', 'kind': 'columns', 'expected_pos': [0, 3, 2], 'count': 2, 'gap_mm': 8.0, 'same_width': True,
        'apply_to': 'from_caret_new', 'confirm_layout': True}


class ColumnsRefusalCommand(Base):
    async def test_the_request_reaches_the_backend_once_and_its_refusal_reaches_the_caller_by_name(self):
        Backend.calls = []
        Backend.bundle_reply = {'ok': False, 'semantic_ok': False, 'session_id': SID, 'dirty': False,
                                'steps': [{'index': 1, 'op': 'layout_exact', 'ok': False, 'refusal_code': CODE, 'mutation_may_have_persisted': False,
                                           'error': f'LocalCliStepRefusal: Refused before any edit ({CODE}); the document is unchanged.'}]}
        try:
            result = await self.call('hwpx_command', {'session_id': SID, 'request': STEP})
        finally:
            Backend.bundle_reply = {'ok': True, 'steps': []}
        bundles = [item for item in Backend.calls if item[1] == '/local-cli/command-bundle']
        self.assertEqual(len(bundles), 1)
        self.assertEqual(len(bundles[0][2]['steps']), 1)
        self.assertEqual({key: bundles[0][2]['steps'][0][key] for key in STEP}, STEP)
        self.assertTrue(result.is_error)
        details = result.structured_content['error']['details']
        self.assertEqual((details['reason'], details['stopped_before_edit'], details['session_held']), (CODE, True, False))

    async def test_the_tool_schema_names_the_refusal_instead_of_offering_the_value(self):
        async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.token}, timeout=30) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=VERSION) as client:
                tools = await client.list_tools()
        rendered = json.dumps(next(item for item in tools.tools if item.name == 'hwpx_command').input_schema)
        self.assertIn(CODE, rendered)
        self.assertNotIn('columns: current_section or from_caret_new', rendered)


class ColumnsRefusalCapability(unittest.IsolatedAsyncioTestCase):
    """The parity transport's adapter and fake backend, without re-running its own tests."""

    setUpClass = classmethod(parity_transport.ParityTransport.setUpClass.__func__)
    tearDownClass = classmethod(parity_transport.ParityTransport.tearDownClass.__func__)
    asyncSetUp = parity_transport.ParityTransport.asyncSetUp
    call = parity_transport.ParityTransport.call

    async def test_capabilities_list_the_variant_as_unsupported_with_the_code(self):
        result = await self.call('hwpx_capabilities', {})
        self.assertFalse(result.is_error, result.structured_content)
        rows = {row['backend']: row for row in result.structured_content['result']['matrix']['bundle_ops']}
        layout = {row['variant']: row for row in rows['layout_exact']['variants']}
        row = layout['columns:from_caret_new']
        self.assertEqual((row['native'], row.get('refusal_code'), row.get('undo')), ('unsupported', CODE, 'not_applicable'))
        self.assertNotIn('evidence', row)
        self.assertEqual((layout['columns:current_section']['native'], layout['columns:current_section']['undo']), ('qualified', 'counted'))


if __name__ == '__main__':
    unittest.main()
