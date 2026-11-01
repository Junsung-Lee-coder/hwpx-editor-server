"""R2, R3 at the real MCP boundary: a real client and adapter over a fake backend. Not Hancom evidence."""
from __future__ import annotations

import shutil
import unittest

from hwpx_mcp.server import rejection_summary
from mcp_tests.test_repair_saved_output_and_review import Backend, Base, SID, paragraph

HELD = 'native_custody_held: The live local CLI session holds a native command whose outcome is unknown; it is retained and is not closed, discarded or released by this runtime.'


class RejectionLabels(unittest.TestCase):
    def test_the_backend_label_is_what_reports_a_hold(self):
        summary = rejection_summary({'detail': HELD})
        self.assertIs(summary['session_held'], True)
        self.assertEqual(summary['reason'], 'native_custody_held')

    def test_wording_alone_never_reports_a_hold(self):
        for detail in ('Something left the live session held elsewhere.', 'Local CLI native command left the live session held: its outcome is unknown.',
                       'save_required: the document has unsaved changes; call save first. It was not closed.'):
            with self.subTest(detail=detail):
                self.assertIs(rejection_summary({'detail': detail})['session_held'], False)


@unittest.skipUnless(shutil.which('pdftoppm') and shutil.which('pdftotext'), 'Poppler is not on PATH')
class CloseAtTheBoundary(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        Backend.held = False

    async def test_a_held_close_reports_the_hold(self):
        await self.opened()
        Backend.close_mode = 'held'
        Backend.held = True
        result = await self.close()
        error = result.structured_content['error']
        self.assertEqual(error['code'], 'BACKEND_REJECTED')
        self.assertIs(error['details']['session_held'], True)
        self.assertEqual(error['details']['reason'], 'native_custody_held')

    async def test_a_save_refusal_stays_recoverable(self):
        await self.opened()
        Backend.dirty = True
        Backend.close_mode = 'save_required'
        error = (await self.close()).structured_content['error']
        self.assertEqual(error['code'], 'SAVE_REQUIRED')
        self.assertIs(error['details']['session_held'], False)

    async def test_an_incomplete_copy_stays_recoverable(self):
        await self.opened()
        Backend.copy_mode = 'status500'
        error = (await self.close()).structured_content['error']
        self.assertEqual(error['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')
        self.assertIs(error['details']['session_held'], False)

    async def test_an_incomplete_copy_of_a_held_session_reports_the_hold(self):
        await self.opened()
        Backend.copy_mode = 'status500'
        Backend.held = True
        error = (await self.close()).structured_content['error']
        self.assertEqual(error['code'], 'SAVED_OUTPUT_NOT_RETRIEVED')
        self.assertIs(error['details']['session_held'], True)
        self.assertEqual(self.close_posts(), [])

    async def test_the_truncated_list_coverage_reaches_the_result(self):
        Backend.printed = [['Alpha', 'Beta'], ['second page']]
        Backend.readback_paragraphs = [paragraph('Alpha', 'NONE'), paragraph('Beta', 'NONE')]
        Backend.readback_truncated = True
        result = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        value = result.structured_content['result']
        self.assertEqual(value['coverage']['list_numbering']['state'], 'partial')
        self.assertEqual(value['review_status'], 'partial')

    async def test_unavailable_structure_reaches_the_result_as_unverified(self):
        Backend.readback_mode = 'unavailable'
        result = await self.call('hwpx_proof', {'session_id': SID, 'request': {'kind': 'review', 'inline_images': 0}})
        coverage = result.structured_content['result']['coverage']['list_numbering']
        self.assertEqual(coverage, {'state': 'unverified', 'number': None, 'bullet': None, 'nested_outline': None})


if __name__ == '__main__':
    unittest.main()
