"""N2, N3, N8, N9 at the real MCP boundary: a real client, adapter and PDF render over a fake backend. Not Hancom evidence."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest

from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, paragraph


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class OrdinaryDocumentReview(Base):
    async def review(self, **request):
        result = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0, **request}})
        self.assertFalse(result.is_error, result.structured_content)
        return result, result.structured_content['result']

    def plain(self):
        Backend.printed = [['Alpha', 'Beta'], ['second page']]
        Backend.readback_paragraphs = [paragraph('Alpha', 'NONE'), paragraph('Beta', 'NONE')]

    async def test_picture_free_document_compares_completely(self):
        self.plain()
        Backend.readback_evidence = {'bindata_sha256': 'absent'}
        _result, first = await self.review()
        self.assertEqual(first['review_status'], 'complete')
        _result, second = await self.review(compare_to=first['review_id'])
        self.assertEqual(second['comparison']['evidence']['bindata_sha256'], 'unchanged')
        self.assertEqual(second['review_status'], 'complete')
        self.assertNotIn('확인 못함', second['summary_text'])

    async def test_an_unreadable_inventory_is_still_not_complete(self):
        self.plain()
        Backend.readback_evidence = {'bindata_sha256': None}
        _result, first = await self.review()
        _result, second = await self.review(compare_to=first['review_id'])
        self.assertEqual(second['comparison']['evidence']['bindata_sha256'], 'unproven')
        self.assertEqual(second['review_status'], 'partial')

    async def test_list_kinds_reach_summary_json_and_markdown(self):
        Backend.printed = [['1: Number', 'o Bullet', 'Plain'], ['second page']]
        Backend.readback_paragraphs = [paragraph('Number', 'NUMBER'), paragraph('Bullet', 'BULLET'), paragraph('Plain', 'NONE')]
        result, value = await self.review()
        self.assertEqual(value['review_status'], 'partial')
        self.assertEqual(value['coverage']['list_numbering'], {'state': 'unverified', 'number': 1, 'bullet': 1, 'nested_outline': 0})
        self.assertIn('자동 번호 1개', value['summary_text'].splitlines()[0])
        self.assertIn('글머리표 1개', result.content[0].text)
        stored = json.loads(Path(value['json_path']).read_text(encoding='utf-8'))
        self.assertEqual(stored['coverage']['list_numbering']['number'], 1)
        markdown = Path(value['summary_path']).read_text(encoding='utf-8')
        self.assertIn('## 인쇄된 번호를 확인하지 않은 문단', markdown)
        self.assertIn('Bullet', markdown)

    async def test_a_paragraph_becoming_a_bullet_is_reported(self):
        self.plain()
        _result, first = await self.review()
        Backend.readback_paragraphs = [paragraph('Alpha', 'BULLET'), paragraph('Beta', 'NONE')]
        _result, second = await self.review(compare_to=first['review_id'])
        changes = second['comparison']['changes']
        self.assertEqual([(c['kind'], c['text'], c['after']) for c in changes], [('number_changed', 'Alpha', '글머리표')])
        self.assertIn('번호 변경 1건', second['summary_text'])
        self.assertEqual(second['review_status'], 'partial')


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class ArtifactRefusedForCustody(Base):
    """N9: a 409 is a custody refusal; only a 404 says the backend holds no saved copy."""

    async def test_close_is_refused_without_any_save_record(self):
        await self.opened()
        Backend.copy_mode = 'custody_mismatch'
        result = await self.close()
        self.assertEqual(result.structured_content['error']['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')
        self.assertEqual(result.structured_content['error']['details']['saved_output']['code'], 'ARTIFACT_CUSTODY_MISMATCH')
        self.assertEqual(self.close_posts(), [])
        self.assertEqual(self.published(), [])

    async def test_save_reports_the_custody_refusal(self):
        await self.opened()
        Backend.copy_mode = 'custody_mismatch'
        saved = await self.save()
        self.assertIs(saved['retrieved'], False)
        self.assertEqual(saved['code'], 'ARTIFACT_CUSTODY_MISMATCH')

    async def test_a_cleared_mismatch_recovers(self):
        await self.opened()
        Backend.copy_mode = 'custody_mismatch'
        self.assertEqual((await self.close()).structured_content['error']['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')
        Backend.copy_mode = 'ok'
        result = await self.close()
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(len(self.close_posts()), 1)
        self.assertEqual(len(self.published()), 1)

    async def test_a_plain_404_keeps_the_ordinary_close(self):
        Backend.copy_mode = 'no_artifact'
        result = await self.close()
        self.assertFalse(result.is_error, result.structured_content)
        self.assertEqual(len(self.close_posts()), 1)


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class SavedCopyAboveTheInputLimit(Base):
    """N8: what the guide says about a saved copy larger than the input limit."""

    async def test_it_is_kept_but_cannot_be_reopened(self):
        await self.opened()
        big = self.root / 'artifacts' / SID / 'saved' / '001-big.hwpx'
        big.parent.mkdir(parents=True, exist_ok=True)
        with big.open('wb') as stream:
            stream.truncate(25 * 1024 * 1024 + 1)
        result = await self.call('hwpx_open', {'request': {'source_path': str(big)}})
        self.assertEqual(result.structured_content['error']['code'], 'SOURCE_SIZE_LIMIT')
        self.assertTrue(big.is_file())


if __name__ == '__main__':
    unittest.main()
