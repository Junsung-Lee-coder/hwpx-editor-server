"""The backend saved-package proof at the real MCP boundary: official SDK, sockets, a fake REST backend.

Only the backend is fake. This shows the adapter keeps exactly the bytes the
backend proved and refuses close otherwise; it is not Hancom evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mcp_tests.test_repair_saved_output_and_review import SID, Backend, Base, hwpx_bytes


def proof_for(data: bytes) -> dict:
    return {'basis': 'saved_package_equals_verified_private_copy', 'saved_package_sha256': hashlib.sha256(data).hexdigest(),
            'members_manifest_sha256': '0' * 64, 'members': 2, 'profile_id': 'synthetic-portable-test/v2',
            'qualification': 'unqualified', 'native_qualified': False, 'evidence_used': ['materialization', 'public_save'],
            'evidence_not_observed': ['materialization', 'public_save'], 'save_decision': 'verified_save_false',
            'legacy_hwpml_bindata_size': 'unknown_unqualified_not_compared'}


class SavedProofAtTheMcpBoundary(Base):
    async def test_matching_proof_is_published_with_the_copy_and_close_proceeds(self):
        await self.opened()
        Backend.save_extra = {'saved_proof': proof_for(Backend.saved)}
        result = await self.call('hwpx_save', {'session_id': SID})
        self.assertFalse(result.is_error, result.structured_content)
        body = result.structured_content['result']
        self.assertEqual(body['saved_proof'], proof_for(Backend.saved), 'backend proof reaches the client unaltered')
        saved = body['saved_output']
        self.assertTrue(saved['retrieved'])
        self.assertEqual(saved['saved_proof']['saved_package_sha256'], hashlib.sha256(Backend.saved).hexdigest())
        self.assertFalse(saved['saved_proof']['native_qualified'])
        self.assertEqual(Path(saved['path']).read_bytes(), Backend.saved)
        closed = await self.close()
        self.assertFalse(closed.is_error, closed.structured_content)
        self.assertEqual(len(self.close_posts()), 1)

    async def test_bytes_that_differ_from_the_proof_are_never_kept_and_close_is_refused(self):
        await self.opened()
        Backend.save_extra = {'saved_proof': proof_for(hwpx_bytes(b'other'))}
        saved = await self.save()
        self.assertFalse(saved['retrieved'])
        self.assertEqual(saved['code'], 'ARTIFACT_IDENTITY_MISMATCH')
        self.assertEqual(self.published(), [])
        record = json.loads((self.root / 'artifacts' / SID / 'saved-proof.json').read_text(encoding='utf-8'))
        self.assertEqual(record['saved_package_sha256'], proof_for(hwpx_bytes(b'other'))['saved_package_sha256'])
        closed = await self.close()
        self.assertTrue(closed.is_error)
        self.assertEqual(closed.structured_content['error']['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')
        self.assertEqual(self.close_posts(), [], 'close-time capture is bound to the same proof')
        self.assertEqual(self.published(), [])

    async def test_close_time_capture_after_a_failed_copy_is_still_bound_to_the_proof(self):
        await self.opened()
        Backend.save_extra = {'saved_proof': proof_for(Backend.saved)}
        Backend.copy_mode = 'status500'
        saved = await self.save()
        self.assertFalse(saved['retrieved'])
        Backend.copy_mode = 'ok'
        closed = await self.close()
        self.assertFalse(closed.is_error, closed.structured_content)
        self.assertEqual(Path(closed.structured_content['result']['saved_output']['path']).read_bytes(), Backend.saved)
        self.assertEqual(closed.structured_content['result']['saved_output']['saved_proof']['saved_package_sha256'],
                         hashlib.sha256(Backend.saved).hexdigest())

    async def test_present_but_unusable_proofs_are_refused_at_save_and_at_close(self):
        good = hashlib.sha256(Backend.saved).hexdigest()
        for value in ({'basis': 'saved_package_equals_verified_private_copy'}, {'saved_package_sha256': None},
                      {'saved_package_sha256': good.upper()}, {'saved_package_sha256': good[:63]}, {'saved_package_sha256': 'g' * 64},
                      'not-a-proof', ['saved_package_sha256'], 7):
            with self.subTest(value=value):
                await self.asyncSetUp()
                await self.opened()
                Backend.save_extra = {'saved_proof': value}
                result = await self.call('hwpx_save', {'session_id': SID})
                self.assertFalse(result.is_error, result.structured_content)
                saved = result.structured_content['result']['saved_output']
                self.assertFalse(saved['retrieved'])
                self.assertEqual(saved['code'], 'ARTIFACT_IDENTITY_MISMATCH')
                self.assertEqual(self.published(), [])
                closed = await self.close()
                self.assertTrue(closed.is_error)
                self.assertEqual(closed.structured_content['error']['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')
                self.assertEqual(self.close_posts(), [], 'a present proof is never absence at close either')
                self.assertEqual(self.published(), [])

    async def test_a_save_without_a_proof_keeps_the_existing_copy_contract(self):
        await self.opened()
        Backend.save_extra = {'saved_proof': proof_for(hwpx_bytes(b'other'))}
        await self.save()
        Backend.save_extra = {}
        saved = await self.save()
        self.assertTrue(saved['retrieved'])
        self.assertNotIn('saved_proof', saved)
        self.assertFalse((self.root / 'artifacts' / SID / 'saved-proof.json').exists(), 'a later unproved save clears the stale proof')
