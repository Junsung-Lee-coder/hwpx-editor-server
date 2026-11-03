"""MCP job schemas accept what the backend accepts, and reject what it would refuse or silently ignore.

- Placeholder keys and table names follow the backend's token rules (canonicalize_placeholder_token and
  canonicalize_placeholder_segment); the MCP decision is compared with the real backend functions.
- confirm_action takes only the fields its chosen action reads in /jobs/{job_id}/confirm-action.
- instructions.metadata is described as input the worker acts on, not as record-only.
"""
from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from app import template_engine
from hwpx_mcp import job_schema, schema

KEYS = ['NAME', '[[BR::NAME]]', '[[BR::A_B::C]]', 'BR::NAME', '담당자 NAME', '이름1', 'a-b', 'x/y',
        '이름', '담당자', ' ', '[[BR::]]', '---', '[[BR::이름]]', '___']
JOB = 'b' * 32


def backend_accepts_key(key):
    try:
        template_engine.canonicalize_placeholder_token(key)
        return True
    except template_engine.TemplateEngineError:
        return False


def backend_accepts_segment(name):
    try:
        template_engine.canonicalize_placeholder_segment(name)
        return True
    except template_engine.TemplateEngineError:
        return False


def mcp_accepts(request):
    try:
        schema.MODELS['hwpx_job'].model_validate({'request': request})
        return True
    except ValidationError:
        return False


def fills(**value):
    return {'action': 'compile_placeholder_fills', 'source_path': 'C:/in/t.hwpx', 'placeholder_fills': value}


class PlaceholderGrammar(unittest.TestCase):
    def test_placeholder_keys_match_the_backend_token_rule(self):
        for key in KEYS:
            with self.subTest(key=key):
                self.assertEqual(mcp_accepts(fills(placeholders={key: 'v'})), backend_accepts_key(key))

    def test_table_names_match_the_backend_segment_rule(self):
        for name in KEYS:
            if not name.strip():
                continue
            with self.subTest(name=name):
                want = backend_accepts_segment(name)
                self.assertEqual(mcp_accepts(fills(tables=[{'name': name, 'cells': {'r1c1': 'v'}}])), want)
                self.assertEqual(mcp_accepts(fills(table_cells=[{'table': name, 'row': 1, 'col': 1, 'value': 'v'}])), want)


class ConfirmActionFields(unittest.TestCase):
    READS = {'inspect_more': set(), 'abort': set(), 'approve': {'enqueue_convert'},
             'regenerate': {'authoring_markdown', 'validation', 'cleanup_placeholders', 'policy_override'}}
    VALUES = {'authoring_markdown': '# 개요\n\n본문\n', 'validation': {'preserve_page_count': True},
              'cleanup_placeholders': 'none', 'policy_override': {'decision_override': 'confirm'}, 'enqueue_convert': True}

    def test_each_action_takes_only_the_fields_the_backend_reads(self):
        for confirm, reads in self.READS.items():
            for field, value in self.VALUES.items():
                with self.subTest(confirm=confirm, field=field):
                    request = {'action': 'confirm_action', 'job_id': JOB, 'confirm': confirm, field: value}
                    if confirm == 'regenerate' and field != 'authoring_markdown':
                        request['authoring_markdown'] = self.VALUES['authoring_markdown']
                    self.assertEqual(mcp_accepts(request), field in reads, request)


class MetadataDescription(unittest.TestCase):
    def test_metadata_is_described_as_worker_input(self):
        text = json.dumps(job_schema.EditInstructions.model_json_schema(), ensure_ascii=False)
        field = job_schema.EditInstructions.model_fields['metadata'].description or ''
        self.assertNotIn('Record-only', field)
        for key in ('post_serialization_proof', 'compile_warning_badges', 'request_id'):
            self.assertIn(key, field)
        self.assertIn('metadata', text)


if __name__ == '__main__':
    unittest.main()
