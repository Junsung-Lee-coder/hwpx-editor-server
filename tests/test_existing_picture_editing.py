"""Existing pictures at public schema/service boundaries; synthetic SDK, not native proof."""
from __future__ import annotations

import unittest
from pydantic import ValidationError
from hwpx_mcp.schema import Command
from tests.native_header_footer_fakes import report_document
from tests.test_native_paragraph_edit import Flow

SID = 'a' * 32
BINDING = {'target_id': 'picture:12345', 'expected_hash': 'sha256:' + 'a' * 24,
           'confirm_layout': True}
REPLACE = {'op': 'picture_replace_exact', **BINDING,
           'source_path': '/owned/new-picture.png'}
CROP = {'op': 'picture_crop_exact', **BINDING,
        'left': 0.25, 'top': 0.0, 'right': 0.75, 'bottom': 1.0}
LAYOUT = {'op': 'picture_layout_exact', **BINDING,
          'mode': 'square', 'horizontal_offset_mm': 15.0,
          'vertical_offset_mm': 0.0}


class Requests(unittest.TestCase):
    def test_inventory_read_uncertainty_issues_no_later_sdk_snapshot(self):
        hwp = report_document()
        latched = False
        after = []
        original_pos = hwp.get_pos

        def read(*args):
            nonlocal latched
            latched = True
            raise RuntimeError('injected entered preservation read failure')

        def position():
            if latched:
                after.append('get_pos')
            return original_pos()

        hwp.get_text_file = read
        hwp.get_pos = position
        result = Flow(hwp).step(op='picture_inventory')
        self.assertFalse(result['ok'])
        self.assertEqual(after, [], result)

    def test_inventory_is_a_real_read_only_service_operation_even_for_no_pictures(self):
        hwp = report_document()
        old = hwp.get_pos()
        outcome = Flow(hwp).step(op='picture_inventory')
        self.assertTrue(outcome['ok'], outcome)
        result = outcome['steps'][0]['result']
        self.assertEqual(result['pictures'], [])
        self.assertTrue(result['read_only'])
        self.assertEqual(hwp.get_pos(), old)

    def test_public_mcp_accepts_inventory(self):
        request = {'op': 'picture_inventory'}
        result = Command.model_validate({'session_id': SID, 'request': request})
        self.assertEqual(result.model_dump()['request']['op'], request['op'])

    def test_public_mcp_accepts_replace(self):
        result = Command.model_validate({'session_id': SID, 'request': REPLACE})
        self.assertEqual(result.model_dump()['request']['source_path'], REPLACE['source_path'])

    def test_public_mcp_accepts_finite_normalized_crop(self):
        result = Command.model_validate({'session_id': SID, 'request': CROP})
        self.assertEqual(result.model_dump()['request']['left'], 0.25)

    def test_public_mcp_accepts_layout_without_any_implicit_resize(self):
        result = Command.model_validate({'session_id': SID, 'request': LAYOUT})
        self.assertNotIn('width', result.model_dump()['request'])
        self.assertNotIn('height', result.model_dump()['request'])

    def test_public_mcp_rejects_nonfinite_bool_and_invalid_crop_extents(self):
        for field, value in [('left', float('nan')), ('right', float('inf')),
                             ('top', True), ('left', -0.1), ('right', 1.1),
                             ('left', 0.75), ('bottom', 0.0)]:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError):
                    Command.model_validate({'session_id': SID, 'request': {**CROP, field: value}})

    def test_public_mcp_rejects_implicit_or_unsupported_layout_and_resize(self):
        for fields in [{'mode': 'tight'}, {'mode': 'through'}, {'width': 40.0},
                       {'mode': 'square', 'horizontal_offset_mm': True},
                       {'mode': 'square', 'vertical_offset_mm': float('nan')}]:
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    Command.model_validate({'session_id': SID, 'request': {**LAYOUT, **fields}})


if __name__ == '__main__':
    unittest.main()
