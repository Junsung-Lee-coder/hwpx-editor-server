from __future__ import annotations

import unittest

from local_cli_v1.bundles import BundleError, build_named_bundle


_BASE_ARGS = [
    '--page-from',
    '19',
    '--target-id',
    'ctrl/94/tbl/1235',
    '--expected-hash',
    'sha256:fixture',
    '--expected-page',
    '19',
]


def _mutation_step(args: list[str]) -> dict[str, object]:
    spec = build_named_bundle('cell-format-exact', [*_BASE_ARGS, *args, '--confirm-layout'])
    payload = spec.server_payload()
    return payload['steps'][1]


class CellFormatExactBundleTests(unittest.TestCase):
    def test_builds_guarded_fill_color_step_with_normalized_hex(self) -> None:
        step = _mutation_step(['--fill-color', '#12abEF'])

        self.assertEqual(step['op'], 'cell_format_exact')
        self.assertEqual(step['fill_color'], '#12ABEF')
        self.assertEqual(step['target_id'], 'ctrl/94/tbl/1235')
        self.assertEqual(step['expected_hash'], 'sha256:fixture')
        self.assertEqual(step['expected_page'], 19)
        self.assertIs(step['confirm_layout'], True)
        self.assertNotIn('cell_margin_mm', step)
        self.assertNotIn('vertical_align', step)
        self.assertNotIn('border', step)

    def test_builds_guarded_border_none_step(self) -> None:
        step = _mutation_step(['--border', 'none'])

        self.assertEqual(step['op'], 'cell_format_exact')
        self.assertEqual(step['border'], 'none')
        self.assertEqual(step['expected_page'], 19)
        self.assertIs(step['confirm_layout'], True)
        self.assertNotIn('fill_color', step)
        self.assertNotIn('cell_margin_mm', step)
        self.assertNotIn('vertical_align', step)

    def test_rejects_invalid_fill_colors_fail_closed(self) -> None:
        bad_values = ['12ABEF', '#123', '#GG0000', '#1234567', '']
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(BundleError):
                    _mutation_step(['--fill-color', value])

    def test_rejects_unsupported_border_values_fail_closed(self) -> None:
        with self.assertRaises(BundleError):
            _mutation_step(['--border', 'solid'])

    def test_rejects_multiple_format_selectors(self) -> None:
        with self.assertRaises(BundleError):
            _mutation_step(['--fill-color', '#112233', '--border', 'none'])

        with self.assertRaises(BundleError):
            _mutation_step(['--fill-color', '#112233', '--vertical-align', 'center'])


if __name__ == '__main__':
    unittest.main()
