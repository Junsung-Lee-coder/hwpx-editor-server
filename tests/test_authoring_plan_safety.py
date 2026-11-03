"""Compiled authoring plans become job instructions only through typed, bounded operations.

The helper used by confirm approve, /author-and-convert and /author-and-convert-from-path refuses a plan that
carries an operation MCP never exposes in queued jobs (native_action) or one the queued runtime refuses
(page_number, official_numbering), whatever whitespace surrounds the op name.
"""
from __future__ import annotations

import unittest

from app import api_server, edit_ops


def plan(*operations):
    return {'compile_readiness': {'status': 'ready'},
            'edit_plan': {'operations': list(operations), 'validation': {}, 'instruction_metadata': {}}}


SAFE = {'op': 'replace_text_safe', 'find': '가', 'replace': '나', 'apply': 'first'}


class ForbiddenCompiledOperations(unittest.TestCase):
    def test_forbidden_ops_are_refused_before_any_instructions_are_written(self):
        for op in ('native_action', ' native_action ', 'page_number', 'official_numbering'):
            with self.subTest(op=op):
                with self.assertRaises(edit_ops.EditOperationError) as caught:
                    api_server._build_authoring_instruction_payload(plan(SAFE, {'op': op, 'action': 'FileSaveAs'}))
                self.assertIn(op.strip(), str(caught.exception))

    def test_bounded_ops_still_pass(self):
        payload = api_server._build_authoring_instruction_payload(plan(SAFE))
        self.assertEqual([o['op'] for o in payload['operations']], ['replace_text_safe'])

    def test_parity_reason_states_compiled_plans_cannot_carry_native_action(self):
        from hwpx_mcp import parity
        reason = parity.QUEUED_OPS['native_action']['reason']
        self.assertNotIn('may still contain', reason)
        self.assertIn('compiled authoring plans', reason)


if __name__ == '__main__':
    unittest.main()
