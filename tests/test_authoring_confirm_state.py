"""The authoring confirm state and approval gate for shared-table conflicts.

/compile-authoring builds the confirm state after compiling; confirm actions recheck the same gate before
offering or accepting approval. A compiled plan whose readiness lists shared-table conflict groups (authored
or emitted) must never offer or accept approval, and a plan without them must.
"""
from __future__ import annotations

import unittest

from app import api_server

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


if __name__ == '__main__':
    unittest.main()
