"""Issue #2: a second font/size command must not silently fall back to the caret after a selected-range style edit.

`StyleSelectionScopeTests` models `set_font` leaving no active selection and fakes the nearby-context/location readers.
`RealReaderSelectionTests` keeps those readers: they select paragraphs and restore only the caret, so the selection is
gone after the command even when `set_font` keeps it. The service `font_family`/`font_size` paths run unchanged; only
the Hancom object and `_execute_live` are faked. This is not Hancom evidence.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.local_cli_router import LocalCliFontRequest, LocalCliFontSizeRequest
from app.local_cli_service import LocalCliService
from app.local_cli_service_support import LocalCliServiceError
from local_cli_v1.main import build_parser

RANGE = (3, 17)


class CollapsingStyleHwp:
    def __init__(self, *, collapse: bool = True, selection: tuple[int, int] | None = RANGE) -> None:
        self.collapse = collapse
        self.selection = selection
        self.caret = 0
        self.selection_unreadable = False
        self.styled: list[tuple[dict[str, Any], tuple[int, int] | None]] = []

    def set_font(self, **kwargs: Any) -> bool:
        self.styled.append((dict(kwargs), self.selection))
        if self.collapse and self.selection is not None:
            self.caret = self.selection[1]
            self.selection = None
        return True

    def get_pos(self) -> tuple[int, int, int]:
        return (0, 0, self.caret)

    def get_selected_pos(self) -> tuple[Any, ...]:
        if self.selection_unreadable:
            raise RuntimeError('selection state is unreadable')
        if self.selection is None:
            return (False, 0, 0, 0, 0, 0, 0)
        return (True, 0, 0, self.selection[0], 0, 0, self.selection[1])


def make_service(hwp: Any, store: dict[str, Any]) -> LocalCliService:
    service = object.__new__(LocalCliService)

    def save(binding: dict[str, Any]) -> dict[str, Any]:
        store['binding'] = binding
        return binding

    def execute_live(*, binding: dict[str, Any], handler: Any, **kwargs: Any) -> Any:
        return handler(SimpleNamespace(hwp=hwp, source_filename='a.hwpx', session_id='s'))

    service._load_active_binding = lambda session_id=None: store['binding']  # type: ignore[method-assign]
    service._save_binding = save  # type: ignore[method-assign]
    service._update_live_binding = lambda current, **kwargs: save(current)  # type: ignore[method-assign]
    service._record_local_cli_command = lambda *args, **kwargs: None  # type: ignore[method-assign]
    service._execute_live = execute_live  # type: ignore[method-assign]
    return service


class StyleSelectionScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        patches = (
            patch('app.local_cli_service.snapshot_live_location', return_value={}),
            patch('app.local_cli_service._capture_nearby_text_context', return_value={}),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.store: dict[str, Any] = {'binding': {'session_id': 's'}}

    def service(self, hwp: CollapsingStyleHwp) -> LocalCliService:
        return make_service(hwp, self.store)

    def test_fontsize_after_a_font_that_collapsed_the_selection_refuses_instead_of_styling_the_caret(self) -> None:
        hwp = CollapsingStyleHwp()
        service = self.service(hwp)
        first = service.font_family(face_name='맑은 고딕')
        self.assertIn('current selection', first['summary'])
        self.assertTrue(first['selection_consumed'])
        self.assertIn('reselect', first['warning'])
        with self.assertRaises(LocalCliServiceError) as ctx:
            service.font_size(size_pt=10.5)
        self.assertEqual(ctx.exception.status_code, 409)
        for needle in ('no change was made', 'font', 'reselect', '--at-caret'):
            self.assertIn(needle, ctx.exception.message.lower())
        self.assertEqual(hwp.styled, [({'FaceName': '맑은 고딕'}, RANGE)])

    def test_font_after_a_fontsize_that_collapsed_the_selection_also_refuses(self) -> None:
        hwp = CollapsingStyleHwp()
        service = self.service(hwp)
        service.font_size(size_pt=10.5)
        with self.assertRaises(LocalCliServiceError):
            service.font_family(face_name='맑은 고딕')
        self.assertEqual(hwp.styled, [({'Height': 10.5}, RANGE)])

    def test_reselecting_before_the_second_command_styles_the_same_range(self) -> None:
        hwp = CollapsingStyleHwp()
        service = self.service(hwp)
        service.font_family(face_name='맑은 고딕')
        hwp.selection = RANGE
        second = service.font_size(size_pt=10.5)
        self.assertIn('current selection', second['summary'])
        self.assertEqual(hwp.styled, [({'FaceName': '맑은 고딕'}, RANGE), ({'Height': 10.5}, RANGE)])

    def test_an_explicit_caret_request_is_honoured_and_ends_the_guard(self) -> None:
        hwp = CollapsingStyleHwp()
        service = self.service(hwp)
        service.font_family(face_name='맑은 고딕')
        caret = service.font_size(size_pt=10.5, at_caret=True)
        self.assertIn('caret', caret['summary'])
        self.assertIsNone(self.store['binding'].get('style_selection_consumed'))
        service.font_size(size_pt=11.0)
        self.assertEqual([item[1] for item in hwp.styled], [RANGE, None, None])

    def test_caret_styling_without_an_earlier_selected_edit_is_unchanged(self) -> None:
        hwp = CollapsingStyleHwp(selection=None)
        result = self.service(hwp).font_size(size_pt=10.5)
        self.assertIn('caret', result['summary'])
        self.assertFalse(result['selection_consumed'])
        self.assertNotIn('warning', result)
        self.assertEqual(hwp.styled, [({'Height': 10.5}, None)])

    def test_a_selection_that_survives_the_command_needs_no_guard(self) -> None:
        hwp = CollapsingStyleHwp(collapse=False)
        service = self.service(hwp)
        first = service.font_family(face_name='맑은 고딕')
        self.assertFalse(first['selection_consumed'])
        self.assertIsNone(self.store['binding'].get('style_selection_consumed'))
        service.font_size(size_pt=10.5)
        self.assertEqual([item[1] for item in hwp.styled], [RANGE, RANGE])

    def test_an_unreadable_selection_after_a_collapse_refuses(self) -> None:
        hwp = CollapsingStyleHwp()
        service = self.service(hwp)
        service.font_family(face_name='맑은 고딕')
        hwp.selection_unreadable = True
        with self.assertRaises(LocalCliServiceError):
            service.font_size(size_pt=10.5)
        self.assertEqual(len(hwp.styled), 1)


class ReaderHwp(CollapsingStyleHwp):
    """Adds the calls the real nearby-context and location readers make; `set_font` keeps the selection."""

    def __init__(self) -> None:
        super().__init__(collapse=False)

    def set_pos(self, list_id: int, para: int, pos: int) -> bool:
        self.caret, self.selection = pos, None
        return True

    def select_text(self, *args: Any) -> bool:
        self.selection = (0, 40)
        return True

    def get_selected_text(self, keep_select: bool = True) -> str:
        text = 'paragraph text' if self.selection else ''
        if not keep_select:
            self.selection = None
        return text


class RealReaderSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict[str, Any] = {'binding': {'session_id': 's'}}

    def test_a_selection_replaced_by_the_service_reads_is_reported_and_guards_the_next_command(self) -> None:
        hwp = ReaderHwp()
        service = make_service(hwp, self.store)
        first = service.font_family(face_name='맑은 고딕')
        self.assertIn('current selection', first['summary'])
        self.assertTrue(first['selection_consumed'])
        self.assertIsNone(hwp.selection)
        with self.assertRaises(LocalCliServiceError):
            service.font_size(size_pt=10.5)
        self.assertEqual(hwp.styled, [({'FaceName': '맑은 고딕'}, RANGE)])

    def test_a_fontsize_whose_selection_was_replaced_by_the_service_reads_guards_the_next_font(self) -> None:
        hwp = ReaderHwp()
        service = make_service(hwp, self.store)
        first = service.font_size(size_pt=10.5)
        self.assertIn('current selection', first['summary'])
        self.assertTrue(first['selection_consumed'])
        with self.assertRaises(LocalCliServiceError):
            service.font_family(face_name='맑은 고딕')
        self.assertEqual(hwp.styled, [({'Height': 10.5}, RANGE)])

    def test_reselecting_after_the_service_reads_styles_the_range(self) -> None:
        hwp = ReaderHwp()
        service = make_service(hwp, self.store)
        service.font_family(face_name='맑은 고딕')
        hwp.selection = RANGE
        service.font_size(size_pt=10.5)
        self.assertEqual([item[1] for item in hwp.styled], [RANGE, RANGE])


class StyleSurfaceTests(unittest.TestCase):
    def test_cli_and_requests_carry_the_explicit_caret_opt_in(self) -> None:
        parser = build_parser()
        self.assertFalse(parser.parse_args(['fontsize', '10.5']).at_caret)
        self.assertTrue(parser.parse_args(['fontsize', '10.5', '--at-caret']).at_caret)
        self.assertFalse(parser.parse_args(['font', 'Batang']).at_caret)
        self.assertTrue(parser.parse_args(['font', 'Batang', '--at-caret']).at_caret)
        self.assertFalse(LocalCliFontSizeRequest(size_pt=10.5).at_caret)
        self.assertTrue(LocalCliFontRequest(face_name='Batang', at_caret=True).at_caret)


if __name__ == '__main__':
    unittest.main()
