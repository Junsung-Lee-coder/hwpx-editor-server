"""Integration of object_insert_exact, layout_exact and layout_inspect into the service and CLI."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from app.command_packages.runtime import get_command_package_registry
from app.local_cli_runtime import LocalCliRuntimeError, snapshot_live_location
from app.local_cli_service import LocalCliService, LocalCliServiceError
from app.local_cli_service_support import _BUNDLE_ALLOWED_OPS
from local_cli_v1.bundles import BUNDLE_SERVER_OPS, BundleError, build_named_bundle
from tests.test_layout_exact import A4, _FakeLayoutHwp
from tests.test_object_insert_exact import _FakeHwp


def _service() -> LocalCliService:
    service = object.__new__(LocalCliService)
    service.command_packages = get_command_package_registry()
    return service


class RegistrationTests(unittest.TestCase):
    def test_ops_are_registered_everywhere(self) -> None:
        registry = get_command_package_registry()
        for op, read_only in (('object_insert_exact', False), ('layout_exact', False), ('layout_inspect', True)):
            with self.subTest(op=op):
                self.assertIn(op, _BUNDLE_ALLOWED_OPS)
                self.assertIn(op, BUNDLE_SERVER_OPS)
                self.assertEqual(registry.get(op).read_only, read_only)
                self.assertTrue(callable(getattr(LocalCliService, f'_bundle_{op}')))


class CliBundleTests(unittest.TestCase):
    def _validated(self, name: str, argv: list[str]) -> dict[str, Any]:
        steps = build_named_bundle(name, argv).server_payload()['steps']
        return _service()._validate_command_bundle_steps(steps)[1]

    def test_object_insert_payloads_pass_server_validation(self) -> None:
        cases = (
            ['--kind', 'footnote', '--text', '각주 내용'],
            ['--kind', 'memo', '--text', 'check this'],
            ['--kind', 'hyperlink', '--url', 'https://example.com/a', '--display-text', '여기'],
            ['--kind', 'bookmark', '--name', 'intro'],
            ['--kind', 'equation', '--script', '{a} over {b}'],
            ['--kind', 'ellipse', '--width-mm', '20', '--height-mm', '10', '--treat-as-char', 'off'],
            ['--kind', 'footer', '--text', '- 1 -', '--apply-to', 'odd'],
        )
        for extra in cases:
            with self.subTest(kind=extra[1]):
                step = self._validated('object-insert-exact', ['--expected-pos', '0,3,5', *extra, '--confirm-mutation'])
                self.assertEqual((step['kind'], step['expected_pos']), (extra[1], [0, 3, 5]))

    def test_object_insert_rejections(self) -> None:
        with self.assertRaisesRegex(BundleError, 'LIST,PARA,POS'):
            build_named_bundle('object-insert-exact', ['--kind', 'bookmark', '--name', 'x', '--expected-pos', '0,3', '--confirm-mutation'])
        with self.assertRaisesRegex(LocalCliServiceError, 'does not take'):
            self._validated('object-insert-exact', ['--kind', 'bookmark', '--name', 'x', '--text', 'y', '--expected-pos', '0,3,5', '--confirm-mutation'])
        with self.assertRaisesRegex(LocalCliServiceError, 'url'):
            self._validated('object-insert-exact', ['--kind', 'hyperlink', '--url', 'javascript:alert(1)', '--display-text', 'x',
                                                    '--expected-pos', '0,3,5', '--confirm-mutation'])

    def test_layout_payloads_pass_server_validation(self) -> None:
        cases = (
            ['--kind', 'page_setup', '--margin-left-mm', '25', '--expected-before', 'margin_left_mm=30', '--apply-to', 'whole_document'],
            ['--kind', 'page_setup', '--landscape', 'on', '--expected-before', 'landscape=false'],
            ['--kind', 'columns', '--count', '2', '--gap-mm', '8'],
            ['--kind', 'section_insert'],
            ['--kind', 'hanging_indent', '--marker-text', '1. '],
        )
        for extra in cases:
            with self.subTest(extra=extra):
                step = self._validated('layout-exact', ['--expected-pos', '0,0,0', *extra, '--confirm-layout'])
                self.assertEqual(step['kind'], extra[1])
        step = self._validated('layout-exact', ['--expected-pos', '0,9,0', '--kind', 'section_delete', '--section-index', '2', '--confirm-layout'])
        self.assertEqual(step['section_index'], 2)
        self.assertEqual(self._validated('layout-inspect', [])['op'], 'layout_inspect')

    def test_layout_rejections(self) -> None:
        with self.assertRaisesRegex(BundleError, 'KEY=VALUE'):
            build_named_bundle('layout-exact', ['--kind', 'page_setup', '--expected-pos', '0,0,0', '--margin-left-mm', '25',
                                                '--expected-before', 'margin_left_mm', '--confirm-layout'])
        with self.assertRaisesRegex(LocalCliServiceError, 'expected_before'):
            self._validated('layout-exact', ['--kind', 'page_setup', '--expected-pos', '0,0,0', '--margin-left-mm', '25', '--confirm-layout'])
        with self.assertRaises(LocalCliServiceError):
            self._validated('layout-exact', ['--kind', 'section_insert', '--expected-pos', '0,0,0', '--count', '2', '--confirm-layout'])


class LayoutInspectTests(unittest.TestCase):
    def test_reads_page_and_columns_without_executing(self) -> None:
        hwp = _FakeLayoutHwp([['a'], ['b']])
        hwp.caret = (0, 1, 0)
        result = _service()._bundle_layout_inspect(hwp, {'op': 'layout_inspect'})
        self.assertTrue(result['read_only'])
        self.assertEqual(result['caret_pos'], [0, 1, 0])
        self.assertEqual(result['section'], {'count': 2, 'current': 2})
        self.assertAlmostEqual(result['page_setup']['margin_left_mm'], A4['LeftMargin'] * 25.4 / 7200, places=1)
        self.assertFalse(result['page_setup']['landscape'])
        self.assertEqual(result['columns'], {'count': 1, 'same_width': True, 'gap_mm': 0.0})
        self.assertEqual(hwp.log, ['GetDefault(PageSetup)', 'GetDefault(MultiColumn)'])

    def test_values_feed_layout_exact_expected_before(self) -> None:
        hwp = _FakeLayoutHwp()
        service = _service()
        service._bundle_compact_snapshot = lambda _hwp: {  # type: ignore[method-assign]
            'pos': hwp.caret, 'is_cell': hwp.in_cell, 'has_selection': hwp.selection is not None, 'selection_mode': 0,
        }
        before = service._bundle_layout_inspect(hwp, {'op': 'layout_inspect'})['page_setup']
        step = service._validate_command_bundle_steps([{
            'op': 'layout_exact', 'kind': 'page_setup', 'expected_pos': [0, 0, 0], 'margin_left_mm': 25.0,
            'expected_before': {'margin_left_mm': before['margin_left_mm']}, 'confirm_layout': True,
        }])[0]
        result = service._bundle_layout_exact(hwp, step)
        self.assertTrue(result['succeeded'])


class _SelectingHwp(_FakeHwp):
    """Object-insert fake whose paragraph reads select text and whose set_pos drops the selection, like Hancom."""

    def select_text(self, spara: int, spos: int, epara: int, epos: int, slist: int = 0) -> bool:
        self.selection = (spos, len(self.paras[spara]) if epos == -1 else epos)
        return True

    def set_pos(self, list_id: int, para: int, pos: int) -> bool:
        self.caret, self.selection = [list_id, para, pos], None
        return True


class WhereKeepsSelectionTests(unittest.TestCase):
    def test_nearby_capture_is_what_drops_a_selection(self) -> None:
        hwp = _SelectingHwp()
        hwp.select(6, 11)
        snapshot_live_location(hwp=hwp, source_filename='a.hwpx', working_copy_id='s')
        self.assertIsNone(hwp.selection)

    def test_where_then_hyperlink_recipe_keeps_the_selection(self) -> None:
        hwp = _SelectingHwp()
        hwp.select(6, 11)
        service = _service()
        steps = build_named_bundle('object-insert-exact', ['--kind', 'hyperlink', '--url', 'https://example.com/a', '--display-text', 'world',
                                                           '--expected-pos', '0,0,11', '--confirm-mutation']).server_payload()['steps']
        self.assertEqual(steps[0]['op'], 'where')
        service._bundle_where_location(hwp, source_filename='a.hwpx', working_copy_id='s')
        self.assertEqual(hwp.selection, (6, 11))
        step = service._validate_command_bundle_steps(steps)[1]
        result = service._bundle_object_insert_exact(hwp, step)
        self.assertTrue(result['verification']['ok'], result['verification'])

    def test_where_refuses_if_the_selection_moved(self) -> None:
        hwp = _SelectingHwp()
        hwp.select(6, 11)

        def clobber(**kwargs: Any) -> dict[str, Any]:
            kwargs['hwp'].selection = None
            return {}

        with patch('app.local_cli_service.snapshot_live_location', side_effect=clobber), \
                self.assertRaisesRegex(LocalCliRuntimeError, 'where changed the live'):
            _service()._bundle_where_location(hwp, source_filename='a.hwpx', working_copy_id='s')


if __name__ == '__main__':
    unittest.main()
