"""Per-variant native qualification in the parity contract (hwpx_capabilities returns hwpx_mcp.parity.matrix()).

layout_exact and object_insert_exact each run several distinct variants. Their matrix rows carry one native status per
variant instead of one status for the whole op: qualified (reference saved-output evidence on this source),
qualified_unaffected (evidence from an earlier source whose path did not change), not_qualified (the backend runs it
without such evidence) or unsupported (refused before any edit, with the refusal code named). The statuses describe
the reference qualification host, not every installed host, and carry no private paths, tokens or work labels.
"""
from __future__ import annotations

import json
import re
import typing
import unittest

from app.step_refusal import STEP_REFUSAL_CODES
from hwpx_mcp import parity
from hwpx_mcp.operations import LayoutStep, ObjectInsertStep

STATUSES = {'qualified', 'qualified_unaffected', 'not_qualified', 'unsupported'}


def kinds(model):
    return set(typing.get_args(model.model_fields['kind'].annotation))


def rows():
    return {row['backend']: row for row in parity.matrix()['bundle_ops']}


class VariantRows(unittest.TestCase):
    def variants(self, op):
        row = rows()[op]
        self.assertIn('variants', row, row)
        return row['variants']

    def test_every_kind_has_its_own_variant_rows(self):
        for op, model in (('layout_exact', LayoutStep), ('object_insert_exact', ObjectInsertStep)):
            with self.subTest(op):
                variants = self.variants(op)
                self.assertEqual({row['kind'] for row in variants}, kinds(model))
                names = [row['variant'] for row in variants]
                self.assertEqual(len(names), len(set(names)), names)
                for row in variants:
                    self.assertIn(row['native'], STATUSES, row)
                    self.assertTrue(row['variant'].startswith(row['kind']), row)
                    self.assertTrue(row.get('scope'), row)
                    if row['native'] == 'unsupported':
                        self.assertIn(row.get('refusal_code'), STEP_REFUSAL_CODES[op], row)
                    else:
                        self.assertNotIn('refusal_code', row, row)

    def test_the_statuses_are_per_variant_not_per_op(self):
        layout = {row['variant']: row['native'] for row in self.variants('layout_exact')}
        self.assertEqual(layout['section_insert:inside_paragraph'], 'qualified')
        self.assertEqual(layout['section_insert:paragraph_start_or_end'], 'unsupported')
        self.assertEqual(layout['page_setup:landscape'], 'qualified')
        self.assertEqual(layout['columns:from_caret_new'], 'not_qualified')
        objects = {row['variant']: row['native'] for row in self.variants('object_insert_exact')}
        self.assertEqual(objects['memo'], 'qualified')
        # Prepared with only its frame size, Hancom saves a shape with no outline and no drawn geometry; until a variant
        # has a qualified drawing contract it is refused before any edit, never reported as inserted.
        shape_rows = {row['variant']: row for row in self.variants('object_insert_exact')}
        for shape in ('line', 'rectangle', 'ellipse'):
            for place in ('inline', 'floating'):
                row = shape_rows[f'{shape}:{place}']
                self.assertEqual((row['native'], row.get('refusal_code')), ('unsupported', 'unverifiable_layout'), row)
        self.assertEqual(objects['header:even'], 'not_qualified')
        self.assertNotIn('native', rows()['layout_exact'])

    def test_the_reference_is_described_without_private_labels(self):
        matrix = parity.matrix()
        self.assertIn('native_reference', matrix)
        self.assertIn('Hancom Office 2024', matrix['native_reference'])
        text = json.dumps(matrix, ensure_ascii=False)
        for pattern in (r'[A-Za-z]:\\\\', r'/home/', r'AppData', r'hwpx-mcp-parity', r'\bowner\b', r'\b[NFKT]\d{1,2}\b', r'\bc0\d[a-z]\b', r'\bd\d{2}\b', r'campaign'):
            self.assertIsNone(re.search(pattern, text), pattern)


if __name__ == '__main__':
    unittest.main()
