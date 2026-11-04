"""Per-variant undo in the parity contract, the same tables the engines count undo with.

Every variant row of layout_exact and object_insert_exact says whether `hwpx_command undo` counts it ('counted', with the
measured number of native Undo steps and the native cases it rests on), refuses it before any native history call
('not_counted', with the reason), or has nothing to undo because the variant is refused before any edit
('not_applicable'). The engines decide from the same product tables, so the contract cannot claim a counted undo the
backend would not give. Portable; not Hancom evidence.
"""
from __future__ import annotations

import unittest

from app import layout_ops, object_insert
from hwpx_mcp import parity

UNDO = {'counted', 'not_counted', 'not_applicable'}


def variants(op):
    return {row['variant']: row for row in next(row for row in parity.matrix()['bundle_ops'] if row['backend'] == op)['variants']}


class UndoPerVariant(unittest.TestCase):
    def test_every_insertion_and_layout_variant_states_its_undo(self):
        for op, table in (('object_insert_exact', object_insert.UNDO_STEPS), ('layout_exact', layout_ops.UNDO_STEPS)):
            for name, row in variants(op).items():
                with self.subTest(op=op, variant=name):
                    self.assertIn(row.get('undo'), UNDO, row)
                    if row['undo'] == 'counted':
                        self.assertEqual(row.get('undo_steps'), table.get(name), row)
                        self.assertIsInstance(row.get('undo_evidence'), list, row)
                        self.assertTrue(row['undo_evidence'], row)
                        for case in row['undo_evidence']:
                            self.assertRegex(case, r'^undo_[a-z0-9_]+$', row)
                    else:
                        self.assertNotIn(name, table, row)
                        self.assertNotIn('undo_steps', row, row)
                        self.assertNotIn('undo_evidence', row, row)
                    if row['undo'] == 'not_applicable':
                        self.assertEqual(row['native'], 'unsupported', row)
                    if row['undo'] == 'not_counted':
                        self.assertTrue(row.get('undo_reason'), row)
                self.assertEqual({name for name, row in variants(op).items() if row['undo'] == 'counted'}, set(table))

    def test_the_measured_boundary(self):
        objects = {name: row['undo'] for name, row in variants('object_insert_exact').items()}
        self.assertEqual({name for name, undo in objects.items() if undo == 'not_counted'}, {'memo', 'memo:selection', 'hyperlink'})
        self.assertEqual(objects['line:diagonal'], 'not_applicable')
        layout = {name: row['undo'] for name, row in variants('layout_exact').items()}
        self.assertEqual({name for name, undo in layout.items() if undo == 'not_counted'}, set())
        self.assertEqual(layout['columns:from_caret_new'], 'not_applicable')
        self.assertEqual(layout['section_insert:paragraph_start_or_end'], 'not_applicable')
        steps = object_insert.UNDO_STEPS
        self.assertEqual((steps['footnote'], steps['endnote'], steps['bookmark'], steps['equation'], steps['header:odd'], steps['footer:even']), (2, 2, 1, 1, 2, 2))
        self.assertEqual({steps[f'{shape}:{place}'] for shape in ('rectangle', 'ellipse') for place in ('inline', 'floating')}, {1})
        self.assertEqual(set(layout_ops.UNDO_STEPS.values()), {1})

    def test_the_insertion_engine_names_the_variant_it_counts(self):
        cases = {
            'footnote': {'kind': 'footnote', 'text': 'x'}, 'bookmark': {'kind': 'bookmark', 'name': 'm'},
            'line:horizontal_inline': {'kind': 'line', 'width_mm': 40.0, 'height_mm': 0.0, 'treat_as_char': True},
            'line:vertical_floating': {'kind': 'line', 'width_mm': 0.0, 'height_mm': 30.0, 'treat_as_char': False},
            'ellipse:floating': {'kind': 'ellipse', 'width_mm': 40.0, 'height_mm': 20.0, 'treat_as_char': False},
            'header:even': {'kind': 'header', 'text': 'x', 'apply_to': 'even'}, 'footer:both': {'kind': 'footer', 'text': 'x'},
            'hyperlink': {'kind': 'hyperlink', 'url': 'https://example.com/', 'display_text': 'x'},
        }
        for name, fields in cases.items():
            with self.subTest(name):
                plan = object_insert.normalize_step({'op': 'object_insert_exact', 'expected_pos': [0, 0, 0], 'confirm_mutation': True, **fields})
                self.assertEqual(object_insert.undo_variant(plan, selected=False), name)
        memo = object_insert.normalize_step({'op': 'object_insert_exact', 'kind': 'memo', 'text': 'x', 'expected_pos': [0, 0, 0], 'confirm_mutation': True})
        self.assertEqual((object_insert.undo_variant(memo, selected=False), object_insert.undo_variant(memo, selected=True)), ('memo', 'memo:selection'))

    def test_the_layout_engine_names_the_variants_a_request_touches(self):
        def touched(**fields):
            if fields['kind'] == 'page_setup':
                fields['expected_before'] = {key: value for key, value in fields.items() if key.endswith('_mm') or key == 'landscape'}
            return layout_ops.undo_variants(layout_ops.normalize_request({'op': 'layout_exact', 'expected_pos': [0, 0, 0], 'confirm_layout': True, **fields}))
        self.assertEqual(touched(kind='page_setup', margin_left_mm=20.0), {'page_setup:margins'})
        self.assertEqual(touched(kind='page_setup', landscape=True, margin_top_mm=20.0), {'page_setup:landscape', 'page_setup:margins'})
        self.assertEqual(touched(kind='page_setup', paper_width_mm=182.0, paper_height_mm=257.0), {'page_setup:paper_size'})
        self.assertEqual(touched(kind='page_setup', margin_left_mm=20.0, apply_to='whole_document'), {'page_setup:whole_document', 'page_setup:margins'})
        self.assertEqual(touched(kind='columns', count=2), {'columns:current_section'})
        self.assertEqual(touched(kind='columns', count=2, apply_to='from_caret_new'), {'columns:from_caret_new'})
        self.assertEqual(touched(kind='section_insert'), {'section_insert:inside_paragraph'})
        self.assertEqual(touched(kind='section_delete', section_index=2), {'section_delete'})
        self.assertEqual(touched(kind='hanging_indent', marker_text='1. '), {'hanging_indent'})


if __name__ == '__main__':
    unittest.main()
