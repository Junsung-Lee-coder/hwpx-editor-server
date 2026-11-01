"""N1, N4, N5, N6, N7: pre-edit refusals against technical failures, through the real service, bundle and owning thread.

The SDK is a fake. Technical failures here are labelled synthetic faults, never observations of native behavior.
"""
from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app import local_cli_runtime as rt
from app.local_cli_service import LocalCliService
from app.native_numbering_census import _MAX_DEPTH
from tests import test_native_numbering_preservation as preservation
from tests.native_numbering_fakes import FakeCtrl, FakeProps
from tests.test_native_paragraph_edit import _doc
from tests.test_repair_runtime_safety import owned_doc, readback

NUMBERING = {'op': 'official_numbering_apply', 'label': 'x', 'match': 'Item', 'expected_page': 1, 'level': 1, 'confirm_layout': True}
PAGE = {'op': 'page_number_apply', 'label': 'x', 'position': 'bottom-center', 'start_number': 1, 'side_char': 'none', 'confirm_layout': True}
REMOVE = {'op': 'paragraph_remove_exact', 'label': 'x', 'match': 'Item', 'expected_page': 1, 'expected_text': 'Item', 'confirm_remove': True}
INSERT = {'op': 'paragraph_insert_exact', 'label': 'x', 'match': 'Item', 'expected_page': 1, 'text': 'New', 'position': 'after', 'confirm_layout': True}
MUTATORS = {'Execute', 'Run', 'select_text'}
SYNTHETIC = 'synthetic SDK fault'


def mutator_calls(hwp):
    return [call for call in hwp.calls if call[0] == 'Execute' or (call[0] == 'Run' and call[1] != 'Cancel')]


class Base(unittest.TestCase):
    def assert_clean_refusal(self, owned, result, error, code):
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        step = result['steps'][0]
        self.assertEqual(step['refusal_code'], code)
        self.assertIs(step['mutation_may_have_persisted'], False)
        self.assertIs(step['dirty'], False)
        self.assertNotIn('native_custody_held', step)
        self.assertFalse(owned.session._native_custody_held)
        self.assertFalse(owned.session._normal_close_requires_save)
        self.assertFalse(owned.session._lifetime_edit_possible)
        self.assertEqual(mutator_calls(owned.hwp), [])
        self.assertEqual(owned.manager.execute(session_id=owned.sid, command_name='where', handler=lambda h: {'still': 'usable'}, timeout=5)['still'], 'usable')
        self.assertEqual(owned.cleanup_events(), [])

    def assert_held_without_edit(self, owned, result, error, *, mark=None):
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertNotIn('refusal_code', result['steps'][0])
        self.assertEqual(mutator_calls(owned.hwp), [])
        if mark is not None:
            self.assertTrue(mark)
            self.assertEqual(owned.hwp.calls[mark[0]:], [], 'an SDK call followed the unknown read')
        self.assertEqual(owned.cleanup_events(), [])


def fail_after(hwp, name, skip=0, record=True):
    """A named SDK getter raises from its (skip+1)th call on, and marks that moment in the call log."""
    real = getattr(hwp, name)
    mark, seen = [], [0]

    def read(*args, **kwargs):
        seen[0] += 1
        if seen[0] > skip:
            if record:
                hwp.calls.append((f'failed_{name}',))
                mark[:] = [len(hwp.calls)]
            raise OSError(SYNTHETIC)
        return real(*args, **kwargs)

    setattr(hwp, name, read)
    return mark


class CellHwp:
    """Mixin: the search for the match lands in a table cell list, as the native finder reports it."""

    CELL_LIST = 7

    def get_selected_pos(self):
        if self.selection_para is None:
            return (False, None, None, None, None, None, None)
        return (True, self.CELL_LIST, 0, 0, self.CELL_LIST, 0, 4)


class SpanningHwp:
    """Mixin: the search for the match returns a selection across two paragraphs. SetPos ends it, as observed natively."""

    def set_pos(self, list_id, para, pos):
        self.selection_para = None
        super().set_pos(list_id, para, pos)

    def get_selected_pos(self):
        if self.selection_para is None:
            return (False, None, None, None, None, None, None)
        return (True, 0, self.selection_para, 0, 0, self.selection_para + 1, 2)


def mixed(mixin, **kwargs):
    base = preservation.make_doc(**kwargs)
    base.__class__ = type('Doc', (mixin, type(base)), {})
    return base


class NumberingTargetRefusals(Base):
    """N1: an identified pre-edit refusal stays a refusal once restoration and preservation are proved."""

    def run_step(self, hwp, **fields):
        owned = owned_doc(self, hwp)
        before = hwp.get_pos()
        result, error = owned.run({**NUMBERING, **fields})
        return owned, before, result, error

    def test_ambiguous_target(self):
        owned, before, result, error = self.run_step(preservation.make_doc(texts=('Title', 'Item one', 'Item two', 'Tail')))
        self.assert_clean_refusal(owned, result, error, 'target_ambiguous')
        self.assertEqual(owned.hwp.get_pos(), before)

    def test_table_cell_target(self):
        owned, before, result, error = self.run_step(mixed(CellHwp, texts=('Title', 'Item in cell', 'Tail')))
        self.assert_clean_refusal(owned, result, error, 'target_not_in_body')
        self.assertEqual(owned.hwp.get_pos(), before)

    def test_match_across_two_paragraphs(self):
        owned, before, result, error = self.run_step(mixed(SpanningHwp, texts=('Title', 'Item here', 'Tail')))
        self.assert_clean_refusal(owned, result, error, 'target_unlocatable')
        self.assertEqual(owned.hwp.get_pos(), before)

    def test_more_controls_than_the_enumeration_limit(self):
        hwp = preservation.make_doc(texts=('Title', 'Item here', 'Tail'), controls=[FakeCtrl('secd')] + [FakeCtrl('tbl') for _ in range(5000)])
        owned, before, result, error = self.run_step(hwp)
        self.assert_clean_refusal(owned, result, error, 'control_enumeration_unavailable')
        self.assertEqual(owned.hwp.get_pos(), before)

    def test_control_limit_on_page_number(self):
        hwp = preservation.make_doc(controls=[FakeCtrl('secd')] + [FakeCtrl('tbl') for _ in range(5000)])
        owned = owned_doc(self, hwp)
        result, error = owned.run(PAGE)
        self.assert_clean_refusal(owned, result, error, 'control_enumeration_unavailable')

    def test_a_semantic_refusal_reports_its_own_code(self):
        hwp = preservation.make_doc(texts=('Title', 'Item here', 'Tail'))
        hwp.paragraphs[1]['heading_type'] = 2
        owned, before, result, error = self.run_step(hwp)
        self.assert_clean_refusal(owned, result, error, 'existing_number_or_bullet')

    def test_an_active_selection_comes_back(self):
        hwp = preservation.make_doc(texts=('Title', 'Item one', 'Item two', 'Tail'))
        owned = owned_doc(self, hwp)
        hwp.set_pos(0, 0, 0)
        result, error = owned.run(NUMBERING)
        self.assert_clean_refusal(owned, result, error, 'target_ambiguous')
        self.assertEqual(hwp.get_pos(), (0, 0, 0))

    def test_earlier_edit_debt_is_not_erased(self):
        hwp = preservation.make_doc(texts=('Title', 'Item one', 'Item two', 'Tail'))
        owned = owned_doc(self, hwp, working_copy_dirty=True, dirty=True)
        result, error = owned.run({**NUMBERING, 'match': 'Title'})
        self.assertTrue(result['ok'], result)
        self.assertTrue(owned.session._normal_close_requires_save)
        result, error = owned.run(NUMBERING)
        self.assertEqual(result['steps'][0]['refusal_code'], 'target_ambiguous')
        self.assertFalse(owned.session._native_custody_held)
        self.assertTrue(owned.session._normal_close_requires_save)
        self.assertTrue(owned.session._lifetime_edit_possible)
        self.assertIs(owned.store['working_copy_dirty'], True)

    def test_failed_restoration_is_not_a_refusal(self):
        hwp = preservation.make_doc(texts=('Title', 'Item one', 'Item two', 'Tail'))
        owned = owned_doc(self, hwp)
        real = hwp.set_pos

        def set_pos(*args):
            hwp.calls.append(('failed_set_pos',))
            raise OSError(SYNTHETIC)
        hwp.set_pos = set_pos
        result, error = owned.run(NUMBERING)
        self.assertFalse(result['ok'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertNotIn('refusal_code', result['steps'][0])
        self.assertEqual(mutator_calls(hwp), [])
        hwp.set_pos = real

    def test_changed_document_after_the_refusal_is_not_a_refusal(self):
        hwp = preservation.make_doc(texts=('Title', 'Item one', 'Item two', 'Tail'))
        owned = owned_doc(self, hwp)
        real = hwp.MoveRight

        def move():
            hwp.paragraphs[0]['text'] = 'Changed behind the search'
            return real()
        hwp.MoveRight = move
        result, error = owned.run(NUMBERING)
        self.assertFalse(result['ok'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertNotIn('refusal_code', result['steps'][0])

    def test_selection_getter_fault_during_the_search_is_not_semantic(self):
        hwp = preservation.make_doc(texts=('Title', 'Item here', 'Tail'))
        owned = owned_doc(self, hwp)
        real_find = hwp.find
        armed = [False]

        def find(*args, **kwargs):
            armed[0] = True
            return real_find(*args, **kwargs)
        hwp.find = find
        real_selected = hwp.get_selected_pos
        mark = []

        def selected():
            # The strict finder's own read is the first after find; the fault hits the library's search read.
            if armed[0] and len([c for c in hwp.calls if c[0] == 'find_seen']) >= 1:
                hwp.calls.append(('failed_selected',))
                mark[:] = [len(hwp.calls)]
                raise OSError(SYNTHETIC)
            if armed[0]:
                hwp.calls.append(('find_seen',))
            return real_selected()
        hwp.get_selected_pos = selected
        result, error = owned.run(NUMBERING)
        self.assert_held_without_edit(owned, result, error, mark=mark)

    def test_control_id_fault_is_not_a_limit(self):
        class Raising(FakeCtrl):
            @property
            def CtrlID(self):  # noqa: N802
                raise OSError(SYNTHETIC)

            @CtrlID.setter
            def CtrlID(self, value):  # noqa: N802
                pass
        hwp = preservation.make_doc(texts=('Title', 'Item here', 'Tail'), controls=[FakeCtrl('secd'), Raising('tbl')])
        owned = owned_doc(self, hwp)
        result, error = owned.run(NUMBERING)
        self.assertFalse(result['ok'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertNotIn('refusal_code', result['steps'][0])


class CursorReadFaults(Base):
    """N4: an exception from a native cursor read is a held technical failure with no later SDK call."""

    def paragraph_owned(self):
        return owned_doc(self, _doc('Title', 'Item', 'Tail'))

    def test_caret_read_at_the_start_of_a_paragraph_edit(self):
        for step in (REMOVE, INSERT):
            with self.subTest(op=step['op']):
                owned = self.paragraph_owned()
                mark = fail_after(owned.hwp, 'get_pos')
                result, error = owned.run(step)
                self.assert_held_without_edit(owned, result, error, mark=mark)
                self.assertFalse(owned.session._normal_close_requires_save)
                self.assertFalse(owned.session._lifetime_edit_possible)
                self.assertIs(result['steps'][0]['mutation_may_have_persisted'], False)

    def test_selection_read_at_the_start_of_a_paragraph_edit(self):
        owned = self.paragraph_owned()
        mark = fail_after(owned.hwp, 'get_selected_pos')
        result, error = owned.run(REMOVE)
        self.assert_held_without_edit(owned, result, error, mark=mark)
        self.assertFalse(owned.session._lifetime_edit_possible)

    def test_caret_read_while_restoring_after_a_refusal(self):
        owned = self.paragraph_owned()
        hwp = owned.hwp
        calls = {'count': 0}
        real_pos = hwp.get_pos
        mark = []

        def read():
            # Reads before the semantic refusal pass; the restoration re-read fails.
            if calls['count'] and any(c[0] == 'set_pos' for c in hwp.calls):
                hwp.calls.append(('failed_get_pos',))
                mark[:] = [len(hwp.calls)]
                raise OSError(SYNTHETIC)
            calls['count'] += 1
            return real_pos()
        real_set = hwp.set_pos

        def set_pos(*args):
            hwp.calls.append(('set_pos', *args))
            return real_set(*args)
        hwp.get_pos, hwp.set_pos = read, set_pos
        result, error = owned.run({**REMOVE, 'expected_text': 'Not this text'})
        self.assert_held_without_edit(owned, result, error, mark=mark)

    def test_a_pure_refusal_still_restores_and_stays_usable(self):
        owned = self.paragraph_owned()
        result, error = owned.run({**REMOVE, 'expected_text': 'Not this text'})
        self.assert_clean_refusal(owned, result, error, 'target_text_mismatch')

    def test_review_caret_read(self):
        owned = owned_doc(self)
        mark = fail_after(owned.hwp, 'get_pos')
        with self.assertRaises(Exception):
            readback(owned)
        self.assertTrue(owned.session._native_custody_held)
        self.assertEqual(owned.hwp.calls[mark[0]:], [])
        self.assertFalse(owned.session._lifetime_edit_possible)

    def test_page_settings_read_fault_is_not_an_existing_page_number_refusal(self):
        class Raising(FakeProps):
            def Item(self, name):  # noqa: N802
                raise OSError(SYNTHETIC)
        control = FakeCtrl('pgnp')
        control.Properties = Raising({})
        hwp = preservation.make_doc(controls=[FakeCtrl('secd'), control])
        owned = owned_doc(self, hwp)
        result, error = owned.run(PAGE)
        self.assertFalse(result['ok'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertNotIn('refusal_code', result['steps'][0])
        self.assertIs(result['steps'][0]['mutation_may_have_persisted'], False)
        self.assertFalse(owned.session._lifetime_edit_possible)

    def test_page_settings_that_returned_other_values_stay_a_refusal(self):
        control = FakeCtrl('pgnp')
        control.Properties = FakeProps({'DrawPos': 99, 'NumberFormat': 0, 'NewNumber': 1, 'SideChar': 0})
        hwp = preservation.make_doc(controls=[FakeCtrl('secd'), control])
        owned = owned_doc(self, hwp)
        result, error = owned.run(PAGE)
        self.assert_clean_refusal(owned, result, error, 'existing_page_number')


class ReviewParserLimits(unittest.TestCase):
    """N5: a text that parses badly within fixed limits is unavailable in a read-only review, not a new hold."""

    def deep(self, hwp):
        text = hwp.get_text_file('HWPML2X', '')
        depth = _MAX_DEPTH + 5
        return text.replace('</HEAD>', '<X>' * depth + '</X>' * depth + '</HEAD>', 1)

    def contradictory(self, hwp):
        text = hwp.get_text_file('HWPML2X', '')
        return text.replace('</HEAD>', '<BINDATALIST Count="1"><BINITEM BinData="1" Id="2" Type="Embedding"/></BINDATALIST></HEAD>', 1)

    def duplicated(self, hwp):
        text = hwp.get_text_file('HWPML2X', '')
        return text.replace('</HEAD>', '<BINDATALIST Count="2"><BINITEM BinData="1"/><BINITEM BinData="1"/></BINDATALIST></HEAD>', 1)

    def test_bounded_parser_failures(self):
        for name in ('deep', 'contradictory', 'duplicated'):
            with self.subTest(name=name):
                owned = owned_doc(self)
                owned.hwp.hwpml_text = getattr(self, name)(owned.hwp)
                result = readback(owned)
                self.assertIs(result['available'], False)
                self.assertEqual(result['reason'], 'census_unavailable')
                self.assertFalse(owned.session._native_custody_held)
                self.assertFalse(owned.session._normal_close_requires_save)
                self.assertFalse(owned.session._lifetime_edit_possible)

    def test_a_native_text_read_fault_stays_held(self):
        owned = owned_doc(self)

        def read(*args):
            raise OSError(SYNTHETIC)
        owned.hwp.get_text_file = read
        with self.assertRaises(Exception):
            readback(owned)
        self.assertTrue(owned.session._native_custody_held)
        self.assertFalse(owned.session._lifetime_edit_possible)

    def test_earlier_debt_survives_an_unavailable_review(self):
        owned = owned_doc(self, working_copy_dirty=True, dirty=True)
        owned.session._normal_close_requires_save = True
        owned.session._lifetime_edit_possible = True
        owned.hwp.hwpml_text = self.deep(owned.hwp)
        result = readback(owned)
        self.assertIs(result['available'], False)
        self.assertTrue(owned.session._normal_close_requires_save)
        self.assertTrue(owned.session._lifetime_edit_possible)

    def test_a_pre_edit_parser_limit_is_a_refusal_not_a_hold(self):
        owned = owned_doc(self, preservation.make_doc(texts=('Title', 'Item here', 'Tail')))
        owned.hwp.hwpml_text = self.deep(owned.hwp)
        result, error = owned.run(NUMBERING)
        self.assertIsNone(error)
        self.assertEqual(result['steps'][0]['refusal_code'], 'preservation_unprovable')
        self.assertFalse(owned.session._native_custody_held)


class ReportingFailure(unittest.TestCase):
    """N6: a report failing after a refusal keeps custody, is never success, and neither adds nor erases edit debt."""

    def failing_report(self, owned, when):
        persist = owned.session._persist_command

        def fail(command):
            if command.name == 'command-bundle' and when(command):
                raise OSError('synthetic report failure')
            return persist(command)
        return patch.object(owned.session, '_persist_command', fail)

    def refused(self, prior_dirty, when):
        hwp = preservation.make_doc(texts=('Title', 'Item one', 'Item two', 'Tail'))
        owned = owned_doc(self, hwp, working_copy_dirty=prior_dirty, dirty=prior_dirty)
        owned.session._normal_close_requires_save = prior_dirty
        with self.failing_report(owned, when):
            result, error = owned.run(NUMBERING)
        return owned, result, error

    def test_report_after_a_refusal_fails(self):
        for prior_dirty in (False, True):
            with self.subTest(prior_dirty=prior_dirty):
                owned, result, error = self.refused(prior_dirty, lambda command: command.state != 'queued')
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assertTrue(owned.session._native_custody_held)
                self.assertEqual(owned.saved_bindings[-1]['working_copy_dirty'], prior_dirty)
                self.assertIs(owned.session._normal_close_requires_save, prior_dirty)
                self.assertFalse(owned.session._lifetime_edit_possible)
                command = owned.session._commands_by_id[owned.session._last_command_id]
                self.assertIs(command.may_have_mutated, False)
                self.assertEqual(owned.cleanup_events(), [])

    def test_report_of_the_queued_command_fails(self):
        for prior_dirty in (False, True):
            with self.subTest(prior_dirty=prior_dirty):
                owned, result, error = self.refused(prior_dirty, lambda command: True)
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assertEqual(owned.saved_bindings[-1]['working_copy_dirty'], prior_dirty)
                self.assertIs(owned.session._normal_close_requires_save, prior_dirty)
                self.assertEqual(mutator_calls(owned.hwp), [])

    def test_report_after_a_real_edit_keeps_the_edit_debt(self):
        hwp = preservation.make_doc(texts=('Title', 'Item here', 'Tail'))
        owned = owned_doc(self, hwp)
        with self.failing_report(owned, lambda command: command.state != 'queued'):
            result, error = owned.run(NUMBERING)
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertTrue(owned.session._native_custody_held)
        self.assertTrue(owned.session._normal_close_requires_save)
        command = owned.session._commands_by_id[owned.session._last_command_id]
        self.assertIs(command.may_have_mutated, True)

    def test_a_generic_failed_step_is_not_read_as_no_edit(self):
        hwp = preservation.make_doc(texts=('Title', 'Item here', 'Tail'))
        owned = owned_doc(self, hwp)
        session = owned.session

        def boom(handle):
            raise OSError('synthetic handler fault')
        with self.failing_report(owned, lambda command: command.state != 'queued'):
            with self.assertRaises(Exception):
                owned.manager.execute(session_id=owned.sid, command_name='command-bundle', handler=boom, timeout=5)
        command = session._commands_by_id[session._last_command_id]
        self.assertIs(command.may_have_mutated, True)


class CloseIsModified(unittest.TestCase):
    """N7: an IsModified fault at close is held on the same thread, with no finalizer and no later SDK call."""

    def raising(self, hwp):
        calls = hwp.calls

        class Modified(type(hwp)):
            @property
            def IsModified(self):  # noqa: N802
                calls.append(('failed_IsModified',))
                raise OSError(SYNTHETIC)

            @IsModified.setter
            def IsModified(self, value):  # noqa: N802
                pass
        hwp.__class__ = Modified
        return calls

    def test_fault_at_close(self):
        owned = owned_doc(self)
        session = owned.session
        thread = session._thread
        calls = self.raising(owned.hwp)
        before = len(calls)
        with self.assertRaises(rt.LocalCliRuntimeError) as caught:
            owned.manager.close_session(owned.sid, timeout=5)
        self.assertEqual(str(caught.exception), rt._HELD_CLOSE_MESSAGE)
        self.assertFalse(getattr(caught.exception, 'save_required', False))
        self.assertEqual(calls[before:], [('failed_IsModified',)])
        self.assertTrue(session._native_custody_held)
        self.assertFalse(session._lifetime_edit_possible)
        self.assertFalse(session._normal_close_requires_save)
        self.assertTrue(thread.is_alive())
        self.assertFalse(session._closed.is_set())
        self.assertEqual(owned.cleanup_events(), [])
        self.assertIn(owned.sid, owned.manager._sessions)

    def test_the_owning_loop_keeps_serving_after_the_fault(self):
        owned = owned_doc(self)
        session = owned.session
        self.raising(owned.hwp)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owned.manager.close_session(owned.sid, timeout=5)
        # Reached by the loop itself, not by the admission check that a later close would stop at.
        future = rt.Future()
        command = rt._LiveCommand(command_id='late-close', name='__close__', handler=None, future=future, sequence=99)
        session._commands_by_id[command.command_id] = command
        session._commands.put(command)
        with self.assertRaises(rt.LocalCliRuntimeError):
            future.result(timeout=5)
        self.assertTrue(session._thread.is_alive())

    def test_earlier_debt_survives_the_fault(self):
        owned = owned_doc(self, working_copy_dirty=True, dirty=True)
        owned.session._normal_close_requires_save = True
        owned.session._lifetime_edit_possible = True
        self.raising(owned.hwp)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owned.manager.close_session(owned.sid, timeout=5)
        self.assertTrue(owned.session._normal_close_requires_save)
        self.assertTrue(owned.session._lifetime_edit_possible)
        self.assertTrue(owned.session._thread.is_alive())

    def test_an_exact_true_still_asks_for_a_save(self):
        owned = owned_doc(self)
        owned.hwp.IsModified = True
        with self.assertRaises(rt.LocalCliRuntimeError) as caught:
            owned.manager.close_session(owned.sid, timeout=5)
        self.assertTrue(getattr(caught.exception, 'save_required', False))
        self.assertFalse(owned.session._native_custody_held)


class EditingAfterARefusedClose(unittest.TestCase):
    """Found by the native run: a close refused for a save ran no native work, so it must not make the next edit look stale."""

    def refused(self, owned):
        owned.hwp.IsModified = True
        with self.assertRaises(rt.LocalCliRuntimeError) as caught:
            owned.manager.close_session(owned.sid, timeout=5)
        self.assertTrue(getattr(caught.exception, 'save_required', False))

    def test_a_refused_close_is_not_the_latest_native_command(self):
        owned = owned_doc(self)
        owned.manager.execute(session_id=owned.sid, command_name='where', handler=lambda h: {}, timeout=5)
        latest = owned.manager.latest_command_sequence(owned.sid)
        self.refused(owned)
        self.assertEqual(owned.manager.latest_command_sequence(owned.sid), latest)

    def test_the_next_edit_projects_through_the_real_binding_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            owned = owned_doc(self, preservation.make_doc(texts=('Title', 'Item here', 'Tail')))
            service = object.__new__(LocalCliService)
            service.root = Path(tmp)
            service.sessions_root = service.root / 'sessions'
            service.active_binding_path = service.root / 'active_binding.json'
            service.sessions_root.mkdir()
            service._closed_session_ids = set()
            service._closed_session_ids_lock = threading.Lock()
            service.runtime_manager = owned.manager
            binding = {'session_id': owned.sid, 'command_generation': 0, 'native_command_sequence': owned.manager.latest_command_sequence(owned.sid)}
            saved = service._save_binding(dict(binding))
            self.refused(owned)
            self.assertEqual(service._save_binding(dict(saved))['session_id'], owned.sid)

    def test_a_held_close_stays_the_latest_command(self):
        owned = owned_doc(self)
        CloseIsModified.raising(self, owned.hwp)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owned.manager.close_session(owned.sid, timeout=5)
        command = owned.session._commands_by_id[f'{owned.sid}:close']
        self.assertEqual(owned.manager.latest_command_sequence(owned.sid), command.sequence)


if __name__ == '__main__':
    unittest.main()
