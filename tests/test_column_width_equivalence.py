"""Legacy table_column_width_exact is reached through table_width_exact.

The legacy engine refuses any request without four whole-document SHA-256 guards (working-copy preimage, cell
inventory, document text, BinData manifest) and positive counts, and no public read returns the inventory or BinData
hashes, so no MCP caller could build a request it accepts. table_width_exact sets the same thing (each column width of
one table in millimetres, verified after the edit) with column_widths_mm, so MCP classifies the legacy op as
equivalent and does not accept it.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from pydantic import ValidationError

from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service import LocalCliService
from hwpx_mcp import parity
from hwpx_mcp.schema import Command
from tests.native_table_fakes import grid
from tests.test_native_table import binding
from tests.test_table_repairs import SESSION, Width, width_doc

# The request MCP used to accept for this op (no guards).
OLD_MCP_SAMPLE = {'op': 'table_column_width_exact', 'section_anchor': '1. 일정', 'target_id': 'ctrl/3/tbl/1', 'expected_hash': 'sha256:' + 'a' * 24,
                  'expected_page': 1, 'expected_div0_count': 0, 'expected_rows': 2, 'expected_cols': 2, 'expected_total_width_mm': 150.0,
                  'expected_table_height_mm': 20.0, 'requested_widths_mm': [50.0, 100.0], 'confirm_layout': True}


class ColumnWidthEquivalence(unittest.TestCase):
    def test_the_contract_row_names_table_width_exact(self):
        row = parity.BUNDLE_OPS['table_column_width_exact']
        self.assertEqual(row['status'], 'equivalent')
        self.assertEqual(row['mcp'], 'hwpx_command:table_width_exact')
        self.assertIn('column_widths_mm', row['reason'])

    def test_mcp_does_not_accept_the_legacy_op(self):
        with self.assertRaises(ValidationError):
            Command.model_validate({'session_id': SESSION, 'request': OLD_MCP_SAMPLE})

    def test_the_legacy_engine_refuses_the_old_mcp_request_before_any_table_read(self):
        service = LocalCliService.__new__(LocalCliService)
        handle = NS(hwp=NS(), working_copy_path='unused.hwpx')
        reads = []
        with patch.object(LocalCliService, '_bundle_resolve_control_target', lambda self, hwp, step, **kw: {'target_ctrl': object(), 'target_id': step['target_id']}), \
                patch.object(LocalCliService, '_bundle_sha256_file', lambda self, path: reads.append(path)):
            with self.assertRaisesRegex(LocalCliRuntimeError, 'requires a full SHA-256 value for expected_preimage_sha256'):
                service._bundle_table_column_width_exact(handle, dict(OLD_MCP_SAMPLE))
        self.assertEqual(reads, [])

    def run_width(self, total_mm, widths_mm):
        total = round(total_mm * 7200 / 25.4)
        hwp = width_doc(grid('2001', [['가', '나'], ['다', '라']], widths=[total // 2, total - total // 2]))
        Command.model_validate({'session_id': SESSION, 'request': {'op': 'table_width_exact', **binding(hwp, '2001'), 'column_widths_mm': widths_mm}})
        return hwp, Width().width(hwp, column_widths_mm=widths_mm)

    def widths(self, hwp):
        return {cell.address: cell.width for cell in hwp.by_inst('2001').ordered()}

    def test_table_width_exact_sets_a_legacy_style_request(self):
        # A legacy request keeps the total (here 140 mm inside the 148 mm text); the same widths as column_widths_mm.
        hwp, outcome = self.run_width(140.0, [40.0, 100.0])
        self.assertTrue(outcome['ok'], outcome)
        hu = [round(mm * 7200 / 25.4) for mm in (40.0, 100.0)]
        self.assertEqual(self.widths(hwp), {'A1': hu[0], 'B1': hu[1], 'A2': hu[0], 'B2': hu[1]})

    def test_widths_wider_than_the_text_are_a_clean_refusal(self):
        # The difference from the legacy engine: a total wider than the text is refused before any edit, not held.
        hwp, outcome = self.run_width(150.0, [50.0, 100.0])
        before = self.widths(hwp)
        self.assertFalse(outcome['ok'])
        step = outcome['steps'][0]
        self.assertEqual(step['refusal_code'], 'table_would_overflow')
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertNotIn('native_custody_held', step)
        self.assertEqual(self.widths(hwp), before)

if __name__ == '__main__':
    unittest.main()
