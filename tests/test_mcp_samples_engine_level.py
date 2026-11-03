"""MCP samples and stated requirements checked against the backend engines, not only the validators.

Every exposed queued sample passes the queued runtime's own validator. The scaffold sample runs through the real
queued engine on an empty paragraph; a scaffold request that pairs require_empty_scaffold with a find of visible
text is refused by MCP, because the engine always refuses it (the found paragraph holds that text). Engine-level
tests for the other deterministic requirements: list_items counts in tests/test_list_items_supported_counts.py, the
cell_replace table scope in tests/test_table_scoped_cell_and_list_primitives.py, object insert preconditions in
tests/test_deferred_step_intent.py and tests/test_where_keeps_selection.py, legacy column widths in
tests/test_column_width_equivalence.py.
"""
from __future__ import annotations

import copy
import unittest

from pydantic import TypeAdapter, ValidationError

from app.edit_ops import EditOperationError, apply_edit_operations, normalize_operations
from hwpx_mcp import job_schema
from mcp_tests import parity_samples as samples
from tests.test_table_scoped_cell_and_list_primitives import FakeHwp

SCAFFOLD = 'replace_empty_native_list_scaffold'


def queued_model(op):
    from tests.test_mcp_backend_parity import QueuedParity
    return QueuedParity()._job_operation_models()[op]


class QueuedSamplesPassTheRuntimeValidator(unittest.TestCase):
    def test_every_exposed_sample(self):
        problems = []
        for op, sample in sorted(samples.QUEUED_SAMPLES.items()):
            try:
                normalize_operations([{'op': op, **copy.deepcopy(sample)}])
            except EditOperationError as exc:
                problems.append(f'{op}: {exc}')
        self.assertEqual(problems, [])


class EmptyScaffold(unittest.TestCase):
    def doc(self):
        hwp = FakeHwp()
        hwp.paragraphs = ['2. 추진 배경', '', '다음 절']
        hwp.mode = 'paragraph'
        hwp.para = 1
        return hwp

    def test_the_sample_runs_through_the_engine_on_an_empty_paragraph(self):
        sample = samples.QUEUED_SAMPLES[SCAFFOLD]
        queued_model(SCAFFOLD).model_validate({'op': SCAFFOLD, **sample})
        hwp = self.doc()
        summary = apply_edit_operations(hwp, normalize_operations([{'op': SCAFFOLD, **copy.deepcopy(sample)}]))
        self.assertEqual(hwp.paragraphs, ['2. 추진 배경', '첫째', '둘째', '다음 절'])
        self.assertEqual(summary[0]['matches'], 1)

    def test_require_empty_with_a_visible_find_is_refused_by_mcp_as_the_engine_always_refuses_it(self):
        request = {'op': SCAFFOLD, 'find': '추진 배경', 'items': ['첫째'], 'kind': 'number', 'require_empty_scaffold': True}
        with self.assertRaises(ValidationError):
            queued_model(SCAFFOLD).model_validate(request)
        hwp = self.doc()
        with self.assertRaisesRegex(EditOperationError, 'expected an empty native scaffold paragraph'):
            apply_edit_operations(hwp, normalize_operations([dict(request)]))
        self.assertEqual(hwp.paragraphs, ['2. 추진 배경', '', '다음 절'])

    def test_find_without_the_empty_check_is_still_accepted(self):
        queued_model(SCAFFOLD).model_validate({'op': SCAFFOLD, 'find': '추진 배경', 'items': ['첫째'], 'kind': 'number'})


if __name__ == '__main__':
    unittest.main()
