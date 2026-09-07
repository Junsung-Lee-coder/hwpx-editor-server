from __future__ import annotations

import unittest

from pydantic import ValidationError

from hwpx_mcp.schema import CellFormatStep


_BASE = {
    'op': 'cell_format_exact',
    'section_anchor': 'native proof page 1',
    'target_id': 'ctrl/1/tbl/1',
    'expected_hash': 'sha256:' + 'a' * 24,
    'expected_page': 1,
    'page_from': 1,
    'page_to': 1,
    'confirm_layout': True,
}


class McpSchemaTests(unittest.TestCase):
    def test_accepts_guarded_cell_margin_selector(self) -> None:
        step = CellFormatStep.model_validate({**_BASE, 'cell_margin_mm': 7.0})
        self.assertEqual(step.cell_margin_mm, 7.0)
        self.assertIsNone(step.vertical_align)

    def test_requires_exactly_one_cell_format_selector(self) -> None:
        with self.assertRaises(ValidationError):
            CellFormatStep.model_validate(_BASE)
        with self.assertRaises(ValidationError):
            CellFormatStep.model_validate({**_BASE, 'vertical_align': 'center', 'cell_margin_mm': 7.0})

    def test_rejects_non_positive_margin(self) -> None:
        with self.assertRaises(ValidationError):
            CellFormatStep.model_validate({**_BASE, 'cell_margin_mm': 0.0})


if __name__ == '__main__':
    unittest.main()