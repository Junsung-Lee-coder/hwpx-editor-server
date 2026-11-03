"""list_items: a count the backend cannot type is refused before the live runtime, and MCP offers only what works.

render_numbered_list_text joins items with a paragraph break and insert_text_at_caret refuses any text with a
line break, so count >= 2 can never succeed. The service now refuses it (HTTP 400) before loading the session
binding or entering the owning worker; count == 1 still runs. MCP accepts count 1 only and the parity row
names the unsupported variant.
"""
from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.local_cli_service import LocalCliService, LocalCliServiceError


class _Stub:
    def __init__(self):
        self.entered = []

    def _load_active_binding(self, **kwargs):
        self.entered.append('binding')
        return {'session_id': 's'}

    def _execute_live(self, **kwargs):
        self.entered.append('runtime')
        return {'mode': 'numbered_text', 'location': {}}

    def _update_live_binding(self, binding, **kwargs):
        return binding

    def _record_local_cli_command(self, *args, **kwargs):
        pass

    def _compact_state_payload(self, **kwargs):
        return {}


class ServiceRefusesUntypableCounts(unittest.TestCase):
    def test_count_two_or_more_is_a_400_before_the_runtime(self):
        for count in (2, 3, 100):
            with self.subTest(count=count):
                stub = _Stub()
                with self.assertRaises(LocalCliServiceError) as caught:
                    LocalCliService.list_items(stub, count=count)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertEqual(stub.entered, [])

    def test_count_one_still_runs(self):
        stub = _Stub()
        result = LocalCliService.list_items(stub, count=1)
        self.assertTrue(result['ok'])
        self.assertEqual(stub.entered, ['binding', 'runtime'])


class McpOffersOnlyCountOne(unittest.TestCase):
    def test_schema(self):
        from hwpx_mcp import schema
        model = schema.MODELS['hwpx_command']
        model.model_validate({'session_id': 'a' * 32, 'request': {'op': 'list_items', 'count': 1}})
        for count in (2, 5):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                model.model_validate({'session_id': 'a' * 32, 'request': {'op': 'list_items', 'count': count}})

    def test_parity_row_names_the_unsupported_variant(self):
        from hwpx_mcp import parity
        row = parity.REST_ROUTES['POST /local-cli/list']
        self.assertEqual(row['status'], 'exposed')
        self.assertIn('count>1', row.get('backend_unsupported_variants', {}))


if __name__ == '__main__':
    unittest.main()
