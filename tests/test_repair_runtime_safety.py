"""F01, F05, F07, F12: read-only and pre-edit failures, Undo/Redo boundaries and close refusals.

A real manager and owning thread over a fake SDK. Source behavior only; the native runs are recorded separately.
"""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from app import local_cli_runtime as rt
from app.local_cli_service import LocalCliService, LocalCliServiceError, as_http_error
from app.native_numbering_census import MAX_HWPML_CHARS
from tests import test_cli_native_close as close_harness
from tests import test_native_numbering_bundle_translation as translation
from tests import test_native_numbering_preservation as preservation
from tests import test_native_numbering_runtime_custody as custody
from tests.native_numbering_fakes import FakeCtrl
from tests.test_native_paragraph_edit import Flow, _doc

INSERT = {'op': 'paragraph_insert_exact', 'label': 'x', 'match': 'Item', 'expected_page': 1, 'text': 'New', 'position': 'after', 'confirm_layout': True}
REMOVE = {'op': 'paragraph_remove_exact', 'label': 'x', 'match': 'Item', 'expected_page': 1, 'expected_text': 'Item', 'confirm_remove': True}
OVERSIZED = 'x' * (MAX_HWPML_CHARS + 1)


def owned_doc(case, hwp=None, **binding):
    hwp = hwp if hwp is not None else preservation.make_doc()
    hwp.PageCount = 1
    hwp.IsModified = False
    owned = custody.Owned(case, hwp, real_execute_live=True)
    store = {'session_id': owned.sid, 'command_generation': 0, 'native_command_sequence': 0, **binding}
    service = owned.service
    service._load_active_binding = lambda session_id=None, **kw: store
    service._read_binding = lambda session_id=None: dict(store)

    def save(current):
        store.update({k: v for k, v in current.items() if not k.startswith('_')})
        owned.saved_bindings.append(dict(current))
        return current

    def update(current, **kw):
        owned.persisted.update(kw)
        if 'dirty' in kw:
            current['working_copy_dirty'] = kw['dirty']
        return save(current)

    service._save_binding = save
    service._update_live_binding = update
    owned.store = store
    return owned


def readback(owned):
    with patch('app.local_cli_service.snapshot_live_location', return_value={}):
        return owned.service.numbering_readback()


class ReadOnlyReviewFailure(unittest.TestCase):
    """F01: known evidence limits report unavailable; the clean session stays usable and closable."""

    def assert_usable(self, owned):
        self.assertFalse(owned.session._native_custody_held)
        self.assertFalse(owned.session._normal_close_requires_save)
        self.assertFalse(owned.session._lifetime_edit_possible)
        self.assertEqual(owned.manager.execute(session_id=owned.sid, command_name='where', handler=lambda h: {'still': 'usable'}, timeout=5)['still'], 'usable')

    def test_oversized_document_text(self):
        owned = owned_doc(self)
        owned.hwp.hwpml_text = OVERSIZED
        result = readback(owned)
        self.assertIs(result['available'], False)
        self.assertEqual(result['reason'], 'census_unavailable')
        self.assert_usable(owned)

    def test_more_controls_than_the_enumeration_limit(self):
        owned = owned_doc(self, preservation.make_doc(controls=[FakeCtrl('secd')] + [FakeCtrl('tbl') for _ in range(5000)]))
        result = readback(owned)
        self.assertIsNone(result['controls'])
        self.assertEqual(result['controls_reason'], 'control_limit')
        self.assert_usable(owned)

    def test_unresolvable_paragraph_shape_references(self):
        owned = owned_doc(self)
        owned.hwp.hwpml_text = owned.hwp.get_text_file('HWPML2X', '').replace('ParaShape="0" Style="0"', 'ParaShape="99" Style="0"')
        result = readback(owned)
        self.assertIs(result['available'], False)
        self.assertEqual(result['reason'], 'paragraphs_unresolved')
        self.assert_usable(owned)

    def test_an_unexpected_native_exception_stays_held_but_adds_no_edit_debt(self):
        owned = owned_doc(self)

        def failing(fmt='UNICODE', option=''):
            raise OSError('synthetic native failure')

        owned.hwp.get_text_file = failing
        with self.assertRaises(LocalCliServiceError):
            readback(owned)
        self.assertTrue(owned.session._native_custody_held)
        self.assertFalse(owned.session._normal_close_requires_save)
        self.assertIs(owned.store.get('working_copy_dirty'), False)

    def test_earlier_edit_debt_survives_a_review_failure(self):
        owned = owned_doc(self, working_copy_dirty=True)
        owned.manager.execute(session_id=owned.sid, command_name='action', handler=lambda h: {'ok': True, 'dirty': True}, timeout=5)
        owned.hwp.hwpml_text = OVERSIZED
        self.assertIs(readback(owned)['available'], False)
        self.assertFalse(owned.session._native_custody_held)
        self.assertTrue(owned.session._normal_close_requires_save)


class ExportFailure(unittest.TestCase):
    def run_export(self, owned):
        def failing(**kw):
            raise rt.LocalCliRuntimeError('synthetic export failure')

        with patch('app.local_cli_service.export_document_pdf', failing), patch('app.local_cli_service.snapshot_live_location', return_value={}):
            return owned.service.export()

    def test_a_failed_export_of_a_clean_document_does_not_hold_it(self):
        owned = owned_doc(self)
        owned.hwp.IsModified = False
        owned.hwp.Path = 'wc.hwpx'
        with self.assertRaises(LocalCliServiceError) as caught:
            self.run_export(owned)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn('export_failed', caught.exception.message)
        self.assertFalse(owned.session._native_custody_held)
        self.assertFalse(owned.session._normal_close_requires_save)

    def test_a_failed_export_of_a_modified_document_stays_held(self):
        owned = owned_doc(self, working_copy_dirty=True)
        owned.hwp.IsModified = True
        owned.hwp.Path = 'wc.hwpx'
        with self.assertRaises(LocalCliServiceError) as caught:
            self.run_export(owned)
        self.assertNotIn('export_failed', caught.exception.message)
        self.assertTrue(owned.session._native_custody_held)


class PreEditRefusal(unittest.TestCase):
    """F05: nothing was issued, so nothing may be reported as possibly changed and the document stays usable."""

    def assert_clean_refusal(self, owned, result, error, code='preservation_unprovable'):
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        step = result['steps'][0]
        self.assertEqual(step.get('refusal_code'), code)
        self.assertIs(step['mutation_may_have_persisted'], False)
        self.assertIs(result['dirty'], False)
        self.assertFalse(owned.session._native_custody_held)
        self.assertFalse(owned.session._normal_close_requires_save)
        self.assertEqual([c for c in owned.hwp.calls if c[0] == 'Execute' and c[1] in {'InsertText', 'ParagraphShape', 'OutlineNumber', 'PageNumPos'}], [])

    def incomplete(self, hwp):
        hwp.hwpml_text = hwp.get_text_file('HWPML2X', '').replace('STYLELIST', 'XSTYLELIST')

    def test_paragraph_edits_on_incomplete_evidence(self):
        for step in (INSERT, REMOVE):
            with self.subTest(op=step['op']):
                owned = owned_doc(self, _doc('Title', 'Item', 'Tail'))
                self.incomplete(owned.hwp)
                result, error = owned.run(step)
                self.assert_clean_refusal(owned, result, error)

    def test_paragraph_edits_on_an_oversized_document(self):
        for step in (INSERT, REMOVE):
            with self.subTest(op=step['op']):
                owned = owned_doc(self, _doc('Title', 'Item', 'Tail'))
                owned.hwp.hwpml_text = OVERSIZED
                result, error = owned.run(step)
                self.assert_clean_refusal(owned, result, error)

    def test_numbering_and_page_number_on_an_oversized_document(self):
        for step in (translation.NUMBERING_STEP, translation.PAGE_STEP):
            with self.subTest(op=step['op']):
                owned = owned_doc(self, translation._doc())
                owned.hwp.hwpml_text = OVERSIZED
                result, error = owned.run(step)
                self.assert_clean_refusal(owned, result, error, code='preservation_unprovable' if step['op'] == 'official_numbering_apply' else 'protection_unavailable')

    def test_an_unexpected_native_exception_before_the_edit_is_not_reported_as_an_edit(self):
        owned = owned_doc(self, _doc('Title', 'Item', 'Tail'))
        owned.hwp.HParameterSet.HInsertText = None
        result, error = owned.run(INSERT)
        self.assertIsNone(error)
        step = result['steps'][0]
        self.assertIs(step['mutation_may_have_persisted'], False)
        self.assertIs(result['dirty'], False)
        self.assertFalse(owned.session._normal_close_requires_save)

    def test_a_real_finder_miss_restores_the_caret_and_refuses(self):
        for step in ({**INSERT, 'expected_page': 2}, {**REMOVE, 'expected_page': 2}):
            with self.subTest(op=step['op']):
                hwp = _doc('Title', 'Item', 'Tail')
                hwp.cursor = (0, 2, 3)
                flow = Flow(hwp)
                result = flow.step(**{k: v for k, v in step.items() if k != 'label'})
                self.assertFalse(result['ok'])
                self.assertEqual(result['steps'][0].get('refusal_code'), 'target_not_found')
                self.assertIs(result['steps'][0]['mutation_may_have_persisted'], False)
                self.assertIs(result['dirty'], False)
                self.assertEqual(hwp.cursor, (0, 2, 3))

    def test_a_numbering_finder_miss_on_the_expected_page_is_a_clean_refusal(self):
        hwp = _doc('Title', 'Item', 'Tail')
        result = Flow(hwp).step(op='official_numbering_apply', match='Item', expected_page=2, level=1, confirm_layout=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['steps'][0].get('refusal_code'), 'target_not_found')
        self.assertIs(result['steps'][0]['mutation_may_have_persisted'], False)


def history_doc():
    hwp = _doc('Title', 'Item', 'Tail')
    hwp.undone, hwp.redone = [], []
    execute, run = hwp.HAction.Execute, hwp.HAction.Run

    def tracked_execute(name, hset):
        before = copy.deepcopy(hwp.paragraphs)
        result = execute(name, hset)
        if name in {'InsertText', 'ParagraphShape'}:
            hwp.undone.append(before)
            hwp.redone.clear()
        return result

    def tracked_run(name):
        if name not in {'Undo', 'Redo'}:
            return run(name)
        hwp.calls.append(('Run', name))
        source, target = (hwp.undone, hwp.redone) if name == 'Undo' else (hwp.redone, hwp.undone)
        if not source:
            return False
        target.append(copy.deepcopy(hwp.paragraphs))
        hwp.paragraphs = source.pop()
        return True

    hwp.HAction.Execute, hwp.HAction.Run = tracked_execute, tracked_run
    return hwp


class UndoRedoBoundary(unittest.TestCase):
    """F07, with a fresh session's empty history as the open path records it."""

    def owned(self):
        owned = owned_doc(self, history_doc(), undo_stack=[], redo_stack=[])
        for name in ('_snapshot_cursor_context', '_capture_nearby_text_context'):
            patcher = patch(f'app.local_cli_service.{name}', return_value={})
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('app.local_cli_service.snapshot_live_location', return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)
        return owned

    def native(self, owned, name):
        return [c for c in owned.hwp.calls if c == ('Run', name)]

    def assert_refused(self, owned, call, code, name):
        with self.assertRaises(Exception) as caught:
            call()
        http = as_http_error(caught.exception)
        self.assertEqual(http.status_code, 409)
        self.assertTrue(http.detail.startswith(code), http.detail)
        self.assertEqual(self.native(owned, name), [])
        self.assertFalse(owned.session._native_custody_held)

    def test_empty_history_makes_no_native_call_and_leaves_the_document_clean(self):
        owned = self.owned()
        self.assert_refused(owned, owned.service.undo, 'no_undo_available', 'Undo')
        self.assert_refused(owned, owned.service.redo, 'no_redo_available', 'Redo')
        self.assertFalse(owned.session._normal_close_requires_save)
        self.assertIsNot(owned.store.get('working_copy_dirty'), True)

    def test_one_edit_undoes_once_and_redoes_once_within_the_boundary(self):
        owned = self.owned()
        result, error = owned.run(INSERT)
        self.assertIsNone(error)
        self.assertTrue(result['ok'], result)
        undone = owned.service.undo()
        self.assertEqual(undone['native_undo_steps'], 1)
        self.assertEqual([p['text'] for p in owned.hwp.paragraphs], ['Title', 'Item', 'Tail'])
        count = len(self.native(owned, 'Undo'))
        with self.assertRaises(Exception) as caught:
            owned.service.undo()
        self.assertTrue(as_http_error(caught.exception).detail.startswith('no_undo_available'))
        self.assertEqual(len(self.native(owned, 'Undo')), count)
        redone = owned.service.redo()
        self.assertIn('summary', redone)
        self.assertEqual([p['text'] for p in owned.hwp.paragraphs], ['Title', 'Item', 'New', 'Tail'])
        count = len(self.native(owned, 'Redo'))
        with self.assertRaises(Exception) as caught:
            owned.service.redo()
        self.assertTrue(as_http_error(caught.exception).detail.startswith('no_redo_available'))
        self.assertEqual(len(self.native(owned, 'Redo')), count)

    def test_a_new_edit_ends_the_redo_boundary(self):
        owned = self.owned()
        owned.run(INSERT)
        owned.service.undo()
        owned.run({**INSERT, 'text': 'Other'})
        with self.assertRaises(Exception) as caught:
            owned.service.redo()
        self.assertTrue(as_http_error(caught.exception).detail.startswith('no_redo_available'))

    def test_the_result_says_what_actually_changed(self):
        owned = self.owned()
        owned.run(INSERT)
        confirmation = owned.service.undo()['confirmation']
        self.assertIs(confirmation['available'], True)
        self.assertIs(confirmation['document_changed'], True)
        self.assertEqual(confirmation['paragraph_count'], {'before': 5, 'after': 4})  # Including the fixture's table cell paragraph.
        self.assertEqual(confirmation['changes'], [{'kind': 'removed', 'text': 'New'}])
        confirmation = owned.service.redo()['confirmation']
        self.assertEqual(confirmation['changes'], [{'kind': 'added', 'text': 'New'}])

    def test_a_native_false_makes_no_fallback_call_and_is_not_applied(self):
        owned = self.owned()
        owned.run(INSERT)
        owned.hwp.undone.clear()
        fallback = []
        owned.hwp.Undo = lambda *a: fallback.append(a)
        with self.assertRaises(Exception) as caught:
            owned.service.undo()
        self.assertEqual(fallback, [])
        self.assertTrue(as_http_error(caught.exception).detail.startswith('undo_not_applied'))
        self.assertFalse(owned.session._native_custody_held)
        self.assertEqual([p['text'] for p in owned.hwp.paragraphs], ['Title', 'Item', 'New', 'Tail'])

    def test_a_native_exception_makes_no_fallback_call_and_stays_unknown(self):
        owned = self.owned()
        owned.run(INSERT)
        fallback = []
        owned.hwp.Undo = lambda *a: fallback.append(a)
        run = owned.hwp.HAction.Run

        def failing(name):
            if name == 'Undo':
                raise OSError('synthetic native failure')
            return run(name)

        owned.hwp.HAction.Run = failing
        with self.assertRaises(LocalCliServiceError):
            owned.service.undo()
        self.assertEqual(fallback, [])
        self.assertTrue(owned.session._native_custody_held)

    def test_a_nonboolean_native_return_is_not_success(self):
        owned = self.owned()
        owned.run(INSERT)
        owned.hwp.HAction.Run = lambda name: None
        with self.assertRaises(LocalCliServiceError):
            owned.service.undo()
        self.assertTrue(owned.session._native_custody_held)


class CloseRefusal(unittest.TestCase):
    """F12 through the real close path."""

    owned = close_harness.NativeCloseTests.owned

    def service(self, manager, **binding):
        service = object.__new__(LocalCliService)
        service.runtime_manager = manager
        service._read_binding = lambda **k: {'session_id': 'close-case', **binding}
        service._binding_session_id = lambda b: 'close-case'
        service._binding_has_pending_reconciliation = lambda b: False
        return service

    def assert_save_required(self, service, session):
        with self.assertRaises(LocalCliServiceError) as caught:
            service.close()
        http = as_http_error(caught.exception)
        self.assertEqual(http.status_code, 409)
        self.assertTrue(http.detail.startswith('save_required'), http.detail)
        self.assertFalse(session._native_custody_held)

    def test_unsaved_edits_refuse_with_a_save_reason(self):
        manager, session, *_ = self.owned()
        self.assert_save_required(self.service(manager, working_copy_dirty=True), session)

    def test_a_native_modified_flag_without_tracked_edits_refuses_without_holding(self):
        manager, session, _events, state, *_ = self.owned()
        state['modified'] = True
        self.assert_save_required(self.service(manager), session)
        state['modified'] = False
        events = []
        service = self.service(manager)
        service._record_session_close = lambda **k: events.append('recorded')
        service._clear_binding = lambda **k: None
        service._mark_session_closed = lambda *a: None
        self.assertTrue(service.close()['ok'])
        self.assertEqual(events, ['recorded'])


if __name__ == '__main__':
    unittest.main()
