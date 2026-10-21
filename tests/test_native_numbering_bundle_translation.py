"""Drive the real LocalCliService.command_bundle translation for the two numbering operations.

Only the live-runtime plumbing (session binding, journal, persistence) is replaced; the package
registry, step validation, step dispatch, protection reader and error translation are the real code.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.command_packages.runtime import get_command_package_registry
from app.local_cli_runtime import LocalCliRuntimeError
from app.local_cli_service import LocalCliService
from tests.native_numbering_fakes import FakeCtrl
from tests.test_native_numbering_census import _HwpmlHwp

PAGE_STEP = {
    'op': 'page_number_apply',
    'label': 'page',
    'position': 'bottom-center',
    'start_number': 1,
    'side_char': 'none',
    'confirm_layout': True,
}
NUMBERING_STEP = {
    'op': 'official_numbering_apply',
    'label': 'numbering',
    'match': 'Item',
    'expected_page': 1,
    'level': 2,
    'confirm_layout': True,
}
NO_CLEANUP = {'attempted': False, 'succeeded': False}


def _doc(controls=None) -> _HwpmlHwp:
    paragraphs = [
        {'text': 'Title', 'heading_type': 0, 'level': 0},
        {'text': 'Item', 'heading_type': 0, 'level': 0},
        {'text': 'Tail', 'heading_type': 0, 'level': 0},
    ]
    return _HwpmlHwp(paragraphs=paragraphs, controls=controls or [FakeCtrl('secd')])


def _run_bundle(hwp, step):
    service = object.__new__(LocalCliService)
    binding = {'session_id': 'session-numbering', 'command_generation': 0, 'native_command_sequence': 0}
    persisted: dict[str, object] = {}
    service.command_packages = get_command_package_registry()
    service._load_active_binding = lambda session_id=None: dict(binding)  # type: ignore[method-assign]
    service._bundle_compact_snapshot = lambda handle_hwp: {'snapshot': True}  # type: ignore[method-assign]
    service._execute_live = lambda *, handler, **kwargs: handler(  # type: ignore[method-assign]
        SimpleNamespace(hwp=hwp, source_filename='document.hwpx', session_id=binding['session_id'])
    )

    def find(handle_hwp, *, match, expected_page):
        for index, para in enumerate(handle_hwp.paragraphs):
            if match in str(para['text']):
                handle_hwp.cursor = (0, index, 0)
                return {'page': expected_page, 'pos': [0, index, 0]}
        raise LocalCliRuntimeError('target not found')

    def update_live_binding(current, **kwargs):
        persisted.update(kwargs)
        return current

    service._find_text_on_expected_page = find  # type: ignore[method-assign]
    service._update_live_binding = update_live_binding  # type: ignore[method-assign]
    service._save_binding = lambda current: current  # type: ignore[method-assign]
    service._record_local_cli_command = lambda *args, **kwargs: None  # type: ignore[method-assign]
    service._validated_artifact_projection = lambda **kwargs: {}  # type: ignore[method-assign]
    service._public_bundle_steps = lambda **kwargs: kwargs['steps']  # type: ignore[method-assign]
    service._public_artifacts = lambda **kwargs: {}  # type: ignore[method-assign]
    service._binding_session_id = lambda current: current['session_id']  # type: ignore[method-assign]
    with patch('app.local_cli_service.snapshot_live_location', return_value={}):
        result = service.command_bundle(steps=[dict(step)])
    return result, persisted


class PageNumberBundleTranslationTests(unittest.TestCase):
    def _assert_unknown_outcome(self, result, persisted) -> dict:
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(persisted['dirty'])
        step = result['steps'][0]
        self.assertFalse(step['ok'])
        self.assertTrue(step['dirty'])
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertEqual(step['rollback'], NO_CLEANUP)
        self.assertEqual(step['mutation'], {'may_have_persisted': True, 'rollback': NO_CLEANUP})
        self.assertNotIn('inserted', step['error'].split(':', 1)[0])
        return step

    def test_positive_path_reports_inserted_and_dirty(self) -> None:
        hwp = _doc()
        result, persisted = _run_bundle(hwp, PAGE_STEP)
        self.assertTrue(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertEqual(result['steps'][0]['result']['result']['status'], 'inserted')
        self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)

    def test_native_false_is_an_unknown_outcome_without_cleanup(self) -> None:
        hwp = _doc()
        hwp.HAction.execute_result = False
        result, persisted = _run_bundle(hwp, PAGE_STEP)
        self._assert_unknown_outcome(result, persisted)
        self.assertEqual(sum(1 for c in hwp.calls if c[0] == 'Execute'), 1)

    def test_native_exception_is_an_unknown_outcome_without_cleanup(self) -> None:
        hwp = _doc()
        hwp.HAction.execute_raises = RuntimeError('native boom')
        result, persisted = _run_bundle(hwp, PAGE_STEP)
        self._assert_unknown_outcome(result, persisted)
        self.assertEqual(sum(1 for c in hwp.calls if c[0] == 'Execute'), 1)

    def test_unreadable_post_state_is_not_reported_as_inserted_and_is_not_discarded(self) -> None:
        hwp = _doc()
        hwp.HAction.page_number_mode = 'insert-unreadable'
        result, persisted = _run_bundle(hwp, PAGE_STEP)
        self._assert_unknown_outcome(result, persisted)
        self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)
        self.assertEqual(sum(1 for c in hwp.calls if c[0] == 'Execute'), 1)

    def test_mismatched_post_state_is_an_unknown_outcome(self) -> None:
        hwp = _doc()
        hwp.HAction.page_number_mode = 'wrong-values'
        result, persisted = _run_bundle(hwp, PAGE_STEP)
        self._assert_unknown_outcome(result, persisted)
        self.assertEqual(sum(1 for c in hwp.controls if c.CtrlID == 'pgnp'), 1)

    def test_unprovable_header_is_a_clean_refusal_with_no_native_mutation(self) -> None:
        hwp = _doc(controls=[FakeCtrl('secd'), FakeCtrl('head')])
        result, persisted = _run_bundle(hwp, PAGE_STEP)
        self.assertFalse(result['ok'])
        step = result['steps'][0]
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertFalse(step['dirty'])
        self.assertEqual([c for c in hwp.calls if c[0] in {'Execute', 'Run'}], [])


class OfficialNumberingBundleTranslationTests(unittest.TestCase):
    def _assert_unknown_outcome(self, result, persisted) -> dict:
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        step = result['steps'][0]
        self.assertFalse(step['ok'])
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertEqual(step['rollback'], NO_CLEANUP)
        self.assertEqual(step['mutation'], {'may_have_persisted': True, 'rollback': NO_CLEANUP})
        return step

    def test_positive_path_reports_the_verified_four_level_format(self) -> None:
        hwp = _doc()
        result, persisted = _run_bundle(hwp, NUMBERING_STEP)
        self.assertTrue(result['ok'])
        step_result = result['steps'][0]['result']
        self.assertIs(step_result['numbering_format_enforced'], True)
        self.assertEqual(len(step_result['numbering_format']['levels']), 4)
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 1, 0])
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Item', 'Tail'])

    def test_native_false_is_an_unknown_outcome_without_cleanup(self) -> None:
        hwp = _doc()
        hwp.HAction.execute_result = False
        result, persisted = _run_bundle(hwp, NUMBERING_STEP)
        self._assert_unknown_outcome(result, persisted)

    def test_native_exception_is_an_unknown_outcome_without_cleanup(self) -> None:
        hwp = _doc()
        hwp.HAction.execute_raises = RuntimeError('native boom')
        result, persisted = _run_bundle(hwp, NUMBERING_STEP)
        self._assert_unknown_outcome(result, persisted)
        self.assertEqual(sum(1 for c in hwp.calls if c[0] == 'Execute' and c[1] == 'ParagraphShape'), 1)

    def test_post_readback_failure_is_an_unknown_outcome_and_state_is_kept(self) -> None:
        hwp = _doc()
        hwp.HAction.shape_mode = 'prefix-text'
        result, persisted = _run_bundle(hwp, NUMBERING_STEP)
        self._assert_unknown_outcome(result, persisted)
        self.assertEqual(hwp.paragraphs[1]['text'], '1. Item')
        self.assertEqual(sum(1 for c in hwp.calls if c[0] == 'Execute' and c[1] == 'ParagraphShape'), 1)

    def test_unverifiable_definition_is_a_clean_refusal_with_no_native_mutation(self) -> None:
        hwp = _doc()
        hwp.outline_unreadable = True
        result, persisted = _run_bundle(hwp, NUMBERING_STEP)
        self.assertFalse(result['ok'])
        step = result['steps'][0]
        self.assertFalse(step['mutation_may_have_persisted'])
        self.assertEqual([c for c in hwp.calls if c[0] in {'Execute', 'Run'}], [])
        self.assertEqual([p['heading_type'] for p in hwp.paragraphs], [0, 0, 0])


if __name__ == '__main__':
    unittest.main()
