"""cell_replace's anchor is text inside the target table: the backend moves the caret to it and resolves the
cell within the table the caret is then in (app/local_cli_service.py cell_replace). A sentence near the table
leaves the caret outside it and the backend refuses."""
from __future__ import annotations

import json
import unittest


class CellReplaceAnchor(unittest.TestCase):
    def test_anchor_is_described_as_text_inside_the_target_table(self):
        from hwpx_mcp.operations import CellReplaceOp
        anchor = CellReplaceOp.model_fields['anchor'].description or ''
        self.assertNotIn('near the table', anchor)
        self.assertIn('inside the target table', anchor)
        cell = CellReplaceOp.model_fields['cell'].description or ''
        self.assertIn('already be inside', cell)
        text = json.dumps(CellReplaceOp.model_json_schema(), ensure_ascii=False)
        self.assertIn('inside the target table', text)


if __name__ == '__main__':
    unittest.main()
