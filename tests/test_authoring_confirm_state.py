"""The authoring confirm state and approval gate for shared-table conflicts.

/compile-authoring builds the confirm state after compiling; confirm actions recheck the same gate before
offering or accepting approval. A compiled plan whose readiness lists shared-table conflict groups (authored
or emitted) must never offer or accept approval, and a plan without them must.
"""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from app import api_server, edit_ops, template_engine

JOB = '0' * 32


def compiled(status='ready', groups=(), emitted=()):
    return {
        'confirm_policy': {'decision': 'confirm', 'decision_reason': 'test'},
        'approval_packet': {},
        'compile_readiness': {'status': status, 'shared_table_conflict_groups': list(groups), 'emitted_shared_table_collision_groups': list(emitted)},
    }


GROUP = {'shared_table_target_key': 'tbl:7', 'section_keys': ['개요', '계획'], 'claim_count': 2,
         'summary': 'canonical shared-table key tbl:7 is claimed by 개요, 계획'}


class SharedTableConflictGate(unittest.TestCase):
    def test_ready_plan_without_conflicts_offers_approval(self):
        state = api_server._default_confirm_state(JOB, compiled())
        self.assertEqual(state['available_actions'][0], 'approve')
        self.assertIsNone(api_server._shared_table_conflict_blocker(compiled()))

    def test_authored_conflict_groups_block_approval_with_their_summary(self):
        plan = compiled(groups=[GROUP])
        blocker = api_server._shared_table_conflict_blocker(plan)
        self.assertIsInstance(blocker, str)
        self.assertIn('canonical shared-table key tbl:7 is claimed by 개요, 계획', blocker)
        self.assertNotIn('approve', api_server._default_confirm_state(JOB, plan)['available_actions'])

    def test_emitted_collision_groups_block_approval(self):
        plan = compiled(emitted=[{'shared_table_target_key': 'tbl:9', 'operations': [1, 2]}])
        self.assertTrue(api_server._shared_table_conflict_blocker(plan))
        self.assertNotIn('approve', api_server._default_confirm_state(JOB, plan)['available_actions'])

    def test_plan_not_ready_never_offers_approval(self):
        state = api_server._default_confirm_state(JOB, compiled(status='needs_exact_target_confirmation'))
        self.assertEqual(state['available_actions'], ['inspect_more', 'regenerate', 'abort'])

    def test_missing_or_malformed_readiness_has_no_blocker(self):
        for plan in ({}, {'compile_readiness': None}, {'compile_readiness': {'shared_table_conflict_groups': None}}, None):
            with self.subTest(plan=plan):
                self.assertIsNone(api_server._shared_table_conflict_blocker(plan))


HS = 'http://www.hancom.co.kr/hwpml/2011/section'
HP = 'http://www.hancom.co.kr/hwpml/2011/paragraph'


def _template(path):
    para = '<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>{}</hp:t></hp:run></hp:p>'
    body = ''.join(para.format(t) for t in ('시험 문서', '1. 개요', '담당자 [[BR::NAME]] 기준 본문입니다.', '2. 추진 계획', '추진 계획 본문입니다.'))
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/hwp+zip')
        z.writestr('Contents/section0.xml', f'<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">{body}</hs:sec>')
    return path


class AuthoringInstructionPayload(unittest.TestCase):
    """Approve, /author-and-convert and /author-and-convert-from-path write the compiled plan as job instructions."""

    def test_payload_is_the_normalized_edit_plan_with_metadata_and_tracking(self):
        plan = {
            'template_version': 'tpl', 'inspect_snapshot_id': 'inspect:abc', 'template_fingerprint': 'tfp:tpl:abc',
            'compile_readiness': {'status': 'ready'},
            'edit_plan': {
                'operations': [{'op': 'replace_text_safe', 'find': '가', 'replace': '나', 'apply': 'first'}],
                'validation': {'preserve_page_count': True},
                'instruction_metadata': {'compile_warning_badges': [], 'allowed_section_keys': ['개요']},
                'failure_reason': None,
            },
        }
        payload = api_server._build_authoring_instruction_payload(plan)
        self.assertEqual(payload, edit_ops.normalize_instruction_payload({
            'operations': plan['edit_plan']['operations'], 'validation': plan['edit_plan']['validation'],
            'metadata': plan['edit_plan']['instruction_metadata'], 'template_version': 'tpl',
            'inspect_snapshot_id': 'inspect:abc', 'template_fingerprint': 'tfp:tpl:abc', 'compile_status': 'ready'}))
        self.assertEqual(payload['metadata'], {'compile_warning_badges': [], 'allowed_section_keys': ['개요']})

    def test_plan_without_edit_plan_is_refused(self):
        for plan in ({}, {'edit_plan': None}, None):
            with self.subTest(plan=plan), self.assertRaises(edit_ops.EditOperationError):
                api_server._build_authoring_instruction_payload(plan)

    def test_compiled_authoring_plan_becomes_runnable_instructions(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = _template(Path(tmp) / 'template.hwpx')
            first = template_engine.compile_authoring_payload(source, '# 개요\n\n작성 본문 문단입니다.\n')
            self.assertEqual(first['compile_readiness']['status'], 'needs_exact_target_confirmation')
            target = first['target_recommendations'][0]['recommended_resolved_target_id']
            plan = template_engine.compile_authoring_payload(source, f'# 개요\n@resolved_target_id: {target}\n\n작성 본문 문단입니다.\n')
            self.assertEqual(plan['compile_readiness']['status'], 'ready')
            self.assertEqual(api_server._default_confirm_state(JOB, plan)['available_actions'][0], 'approve')
            payload = api_server._build_authoring_instruction_payload(plan)
            self.assertEqual([op['op'] for op in payload['operations']], [op['op'] for op in plan['edit_plan']['operations']])
            self.assertEqual(payload['inspect_snapshot_id'], plan['inspect_snapshot_id'])
            self.assertEqual(edit_ops.normalize_instruction_payload(payload), payload)


if __name__ == '__main__':
    unittest.main()
