"""Held-runtime race regressions for the numbering owning-STA custody contract.

Real LocalCliRuntimeManager + LocalCliLiveSession owning thread + real command_bundle (and, where named, the
real LocalCliService._execute_live); only the SDK/COM objects are fakes. The immutable ``Owned`` fixture of the
custody module is reused unchanged. This proves source behaviour against fakes; it is NOT Hancom evidence.

F1: a close admitted while a numbering callback is still in flight (before its outcome is known) must not end
the owning STA once that callback enters held custody.
F2: a BaseException raised by the journal/prune reporters while an ALREADY held command is being finalized must
not end the owning STA or leave the waiting caller to the request timeout.
"""
from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from app import local_cli_runtime as rt
from app.local_cli_runtime import LocalCliRuntimeError, LocalCliRuntimeTimeoutError
from app.local_cli_service_support import LocalCliServiceError
from tests.test_native_numbering_review_contract import NUMBERING_STEP, PAGE_STEP
from tests.test_native_numbering_runtime_custody import CustodyAssertions, Owned, Terminate, chain, make_hwp

HELD_CLOSE_TEXT = 'is retained and is not closed, discarded or released by this runtime'
JOIN = 30.0
CLOSE_TIMEOUT = 10.0


def wait_until(predicate, what, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f'timed out waiting for {what}')
        time.sleep(0.005)


def drain_owning_thread(session, timeout=20.0):
    """Queue a barrier command: the held loop cancels it only after it finished the preceding finalization.

    Returns True when the owning thread serviced the barrier, False when that thread had ended instead.
    """
    barrier = rt._LiveCommand(
        command_id=f'{session.session_id}:barrier', name='barrier', handler=None, future=Future(), sequence=-1,
    )
    session._commands.put(barrier)
    wait_until(lambda: barrier.completed_event.is_set() or not session._thread.is_alive(), 'the owning thread to drain its queue', timeout)
    return barrier.completed_event.is_set()


def blocking_exc(factory, entered, release):
    def make():
        entered.set()
        if not release.wait(JOIN):
            raise AssertionError('the in-flight numbering callback was never released')
        return factory()

    return make


PRIMARIES = (
    ('KeyboardInterrupt', KeyboardInterrupt),
    ('SystemExit', SystemExit),
    ('Terminate', Terminate),
    ('ordinary RuntimeError', lambda: RuntimeError('COM exploded')),
)


class QueuedCloseBeforeHoldTests(CustodyAssertions, unittest.TestCase):
    def _race(self, label, factory, *, real_service):
        with self.subTest(case=label, real_execute_live=real_service):
            owned = Owned(self, make_hwp(), real_execute_live=real_service)
            session, sid = owned.session, owned.sid
            thread_ident = session._thread.ident
            entered, release = threading.Event(), threading.Event()
            fault = owned.arm('ParagraphShape', make_exc=blocking_exc(factory, entered, release))
            box: dict = {}

            def numbering():
                box['A'] = owned.run(NUMBERING_STEP)

            def closer():
                try:
                    owned.manager.close_session(sid, timeout=CLOSE_TIMEOUT)
                    box['B'] = None
                except BaseException as exc:  # noqa: BLE001
                    box['B'] = exc

            ta, tb = threading.Thread(target=numbering), threading.Thread(target=closer)
            ta.start()
            try:
                self.assertTrue(entered.wait(20), 'the numbering mutator was never reached')
                tb.start()
                close_id = f'{sid}:close'

                def close_queued():
                    with session._state_lock:
                        return close_id in session._commands_by_id

                wait_until(close_queued, 'the close command to be queued behind the in-flight numbering callback')
            finally:
                release.set()
                ta.join(JOIN)
                if tb.ident is not None:
                    tb.join(JOIN)
            self.assertFalse(ta.is_alive(), 'the numbering caller thread is still waiting')
            self.assertFalse(tb.is_alive(), 'the closing caller thread is still waiting')
            self.assertTrue(drain_owning_thread(session), 'the owning STA ended instead of servicing its queue')

            result, error = box['A']
            closer_error = box['B']
            self.assertIsInstance(closer_error, LocalCliRuntimeError, 'the queued close was reported as succeeded')
            self.assertNotIsInstance(closer_error, LocalCliRuntimeTimeoutError)
            self.assertIn(HELD_CLOSE_TEXT, str(closer_error))

            self.assertTrue(session._thread.is_alive(), 'the owning STA thread exited')
            self.assertEqual(session._thread.ident, thread_ident)
            self.assertFalse(session._closed.is_set())
            self.assertFalse(session._closing)
            self.assertTrue(owned.manager.has_session(sid), 'the manager reports the live session unavailable')
            self.assertIs(owned.manager._sessions.get(sid), session)
            self.assertEqual(owned.cleanup_events(), [])
            self.assertEqual(owned.hook, [])
            self.assertTrue(session._native_custody_held)
            self.assertTrue(session._quarantined)
            self.assertTrue(session.has_unreconciled_reconciliation())
            self.assertEqual(fault.after(), [])
            self.assertEqual(owned.snapshots_after_effect, 0)

            close_status = owned.manager.command_status(sid, close_id)
            self.assertEqual(close_status.get('state'), 'failed')
            self.assertIs(close_status.get('semantic_ok'), False)
            self.assertIs(close_status.get('completed'), True)
            self.assertNotEqual(rt.read_command_journal(owned.root, close_id).get('state'), 'succeeded')

            with session._state_lock:
                numbering_commands = [c for c in session._commands_by_id.values() if c.name == 'command-bundle']
            self.assertEqual(len(numbering_commands), 1)
            record = numbering_commands[0]
            self.assertTrue(record.custody_held)
            self.assertEqual(record.state, 'failed')
            self.assertIs(record.may_have_mutated, True)
            if fault.injected and not isinstance(fault.injected[0], RuntimeError):
                self.assertTrue(any(e is fault.injected[0] for e in chain(record.error)), 'the original signal was lost')

            if real_service:
                self.assertIsNone(error)
                self.assertIs(result['ok'], False)
                self.assertIs(result['dirty'], True)
                self.assertIs(result.get('native_custody_held'), True)
                self.assertTrue(any(b.get('working_copy_dirty') is True and b.get('native_custody_held') for b in owned.saved_bindings))
                calls = list(owned.hwp.calls)
                next_result, next_error = owned.run(NUMBERING_STEP)
                self.assertIsNone(next_result)
                self.assertIsInstance(next_error, LocalCliServiceError)
                self.assertEqual(next_error.status_code, 500, 'the next bundle was reported as an unavailable (409) session')
                self.assertIn('held', str(next_error))
                self.assertEqual(owned.hwp.calls, calls)
            else:
                signal = fault.injected[0] if not isinstance(fault.injected[0], RuntimeError) else None
                self.assert_noncomplete_result(owned, result, error, signal=signal)
            self.assert_custody_survives_follow_ons(owned)

    def test_close_admitted_before_the_hold_is_refused_by_the_manager_path(self):
        for label, factory in PRIMARIES:
            self._race(label, factory, real_service=False)

    def test_close_admitted_before_the_hold_is_refused_through_the_real_execute_live(self):
        for label, factory in PRIMARIES:
            self._race(label, factory, real_service=True)


class QueuedCloseCleanControlTests(unittest.TestCase):
    def _assert_close_unavailable(self, owned, error, *, preservation_required=False):
        if preservation_required:
            self.assertIs(type(error), LocalCliRuntimeError)
            self.assertEqual(str(error), 'Normal close requires proved preservation of prior edits.')
            self.assertTrue(owned.session._normal_close_requires_save)
        else:
            self.assertIsInstance(error, rt.LocalCliNativeCustodyHeldError)
            self.assertIn('Qualified full native closure is unavailable', str(error.__cause__))
        self.assertNotIsInstance(error, LocalCliRuntimeTimeoutError)
        s = owned.session
        self.assertTrue(s._thread.is_alive())
        self.assertFalse(s._closed.is_set())
        self.assertIs(s._native_custody_held, not preservation_required)
        self.assertIs(owned.manager._sessions.get(owned.sid), s)
        self.assertEqual(owned.cleanup_events(), [])
        self.assertEqual(owned.hook, [])
        self.assertEqual(rt.read_command_journal(owned.root, f'{owned.sid}:close')['state'], 'failed')
        before = {p.relative_to(owned.root).as_posix(): p.read_bytes() for p in owned.root.rglob('*') if p.is_file() and p != rt.command_journal_path(owned.root)}
        calls = list(owned.hwp.calls)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owned.manager.close_session(owned.sid, timeout=.2)
        self.assertEqual(owned.hwp.calls, calls)
        self.assertEqual(before, {p.relative_to(owned.root).as_posix(): p.read_bytes() for p in owned.root.rglob('*') if p.is_file() and p != rt.command_journal_path(owned.root)})

    def _blocked_numbering(self, owned):
        entered, release = threading.Event(), threading.Event()
        original = owned.service._find_text_on_expected_page

        def find(h, *, match, expected_page, **kwargs):
            entered.set()
            if not release.wait(JOIN):
                raise AssertionError('the in-flight numbering callback was never released')
            return original(h, match=match, expected_page=expected_page, **kwargs)

        owned.service._find_text_on_expected_page = find
        return entered, release

    def _close_during_inflight(self, owned):
        entered, release = self._blocked_numbering(owned)
        box: dict = {}
        ta = threading.Thread(target=lambda: box.__setitem__('A', owned.run(NUMBERING_STEP)))

        def closer():
            try:
                owned.manager.close_session(owned.sid, timeout=JOIN)
                box['B'] = None
            except BaseException as exc:  # noqa: BLE001
                box['B'] = exc

        tb = threading.Thread(target=closer)
        ta.start()
        try:
            self.assertTrue(entered.wait(20))
            tb.start()
            session = owned.session

            def close_queued():
                with session._state_lock:
                    return f'{owned.sid}:close' in session._commands_by_id

            wait_until(close_queued, 'the close command to be queued')
        finally:
            release.set()
            ta.join(JOIN)
            if tb.ident is not None:
                tb.join(JOIN)
        self.assertFalse(ta.is_alive())
        self.assertFalse(tb.is_alive())
        return box

    def test_close_queued_behind_a_successful_numbering_callback_still_closes(self):
        owned = Owned(self, make_hwp())
        fault = owned.arm('NoSuchAction')
        box = self._close_during_inflight(owned)
        result, error = box['A']
        self.assertIsNone(error)
        self.assertIs(result['ok'], True)
        self.assertEqual(len(fault.attempted), 1)
        self._assert_close_unavailable(owned, box['B'], preservation_required=True)

    def test_close_queued_behind_a_clean_typed_refusal_still_closes(self):
        hwp = make_hwp(foreign=True)
        hwp.paragraphs[0]['heading_type'] = 1
        owned = Owned(self, hwp)
        fault = owned.arm('NoSuchAction')
        box = self._close_during_inflight(owned)
        result, error = box['A']
        self.assertIsNone(error)
        self.assertIs(result['ok'], False)
        self.assertIs(result['dirty'], False)
        self.assertEqual(fault.attempted, [])
        self._assert_close_unavailable(owned, box['B'])

    def test_plain_close_after_numbering_still_closes(self):
        owned = Owned(self, make_hwp())
        owned.arm('NoSuchAction')
        result, error = owned.run(NUMBERING_STEP)
        self.assertIsNone(error)
        self.assertIs(result['ok'], True)
        # Historical method name retained for CI continuity; full close is unsupported.
        calls = list(owned.hwp.calls)
        sentinel = owned.root / 'retained.hwpx'
        sentinel.write_bytes(b'EXACT_SYNTHETIC_DOCUMENT_BYTES')
        with self.assertRaises(LocalCliRuntimeError) as ctx:
            owned.manager.close_session(owned.sid, timeout=JOIN)
        self._assert_close_unavailable(owned, ctx.exception, preservation_required=True)
        self.assertEqual(owned.hwp.calls, calls)
        self.assertEqual(sentinel.read_bytes(), b'EXACT_SYNTHETIC_DOCUMENT_BYTES')


class HeldReporterBaseExceptionTests(CustodyAssertions, unittest.TestCase):
    REPORTERS = (
        ('LocalCliRuntimeError', lambda: LocalCliRuntimeError('injected reporter failure')),
        ('KeyboardInterrupt', lambda: KeyboardInterrupt('injected reporter signal')),
        ('SystemExit', lambda: SystemExit('injected reporter signal')),
        ('Terminate', lambda: Terminate('injected reporter signal')),
    )
    PRIMARY_CASES = (
        ('official/KeyboardInterrupt', NUMBERING_STEP, 'ParagraphShape', {'make_exc': KeyboardInterrupt}),
        ('page/SystemExit', PAGE_STEP, 'PageNumPos', {'make_exc': SystemExit}),
        ('official/raw', NUMBERING_STEP, 'ParagraphShape', {'exploding': True}),
        ('page/raw', PAGE_STEP, 'PageNumPos', {'make_exc': lambda: RuntimeError('COM exploded')}),
    )
    REQUEST_TIMEOUT = 4.0

    def _bounded_executor(self, owned):
        def executor(*, binding, command_name, task_label, handler, timeout=90.0):
            return owned.manager.execute(session_id=owned.sid, command_name=command_name, handler=handler, timeout=self.REQUEST_TIMEOUT)

        owned.service._execute_live = executor

    def _case(self, primary, reporter_label, make_reporter, *, fail):
        label, step, action, arm_kwargs = primary
        with self.subTest(primary=label, reporter=reporter_label, fail=fail):
            owned = Owned(self, make_hwp())
            self._bounded_executor(owned)
            fault = owned.arm(action, **arm_kwargs)
            reporting = make_reporter()
            session = owned.session
            original_persist = session._persist_command

            def failing_persist(command):
                if command.name == 'command-bundle' and command.state != 'queued':
                    raise reporting
                return original_persist(command)

            def failing_prune():
                raise reporting

            patcher = patch.object(
                session,
                '_persist_command' if fail == 'journal' else '_prune_in_memory_commands',
                failing_persist if fail == 'journal' else failing_prune,
            )
            patcher.start()
            self.addCleanup(patcher.stop)
            started = time.monotonic()
            result, error = owned.run(step)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(len(fault.attempted), 1)
            if fail == 'journal':
                self.assertIsNone(result, 'a reporter failure was presented as a completed result')
                self.assertIsInstance(error, LocalCliRuntimeError)
                self.assertNotIsInstance(error, LocalCliRuntimeTimeoutError, 'the caller was left to the request timeout')
                self.assertLess(elapsed, self.REQUEST_TIMEOUT)
                self.assertTrue(any(e is reporting for e in chain(error)), 'the secondary reporter failure was not retained')
                if fault.injected:
                    self.assertTrue(any(e is fault.injected[0] for e in chain(error)), 'the primary cause was not retained')
            else:
                self.assertIsNone(error)
                self.assertIs(result['ok'], False)
                self.assertIs(result['dirty'], True)
            drained = drain_owning_thread(session)
            patcher.stop()
            self.assertTrue(drained, 'the owning STA ended before it serviced its queue')
            self.assertTrue(session._thread.is_alive(), 'the owning STA died on a reporter failure')
            self.assertFalse(session._closed.is_set())
            self.assertEqual(owned.cleanup_events(), [])
            self.assertEqual(owned.hook, [])
            self.assertIs(owned.manager._sessions.get(owned.sid), session)
            self.assertTrue(session._quarantined)
            self.assertTrue(session._native_custody_held)
            self.assertTrue(session.has_unreconciled_reconciliation())
            self.assertIs(owned.command().get('may_have_mutated'), True)
            self.assert_no_follow_on(owned, fault)
            record = session._commands_by_id[owned.command()['command_id']]
            self.assertTrue(record.custody_held)
            self.assertTrue(any(e is reporting for e in chain(record.error)), 'the secondary reporter failure is not on the held record')
            if fault.injected:
                self.assertTrue(any(e is fault.injected[0] for e in chain(record.error)), 'the primary cause is not on the held record')
            self.assert_custody_survives_follow_ons(owned)

    def test_journal_reporter_failure_after_the_hold(self):
        for primary in self.PRIMARY_CASES:
            for reporter_label, make_reporter in self.REPORTERS:
                self._case(primary, reporter_label, make_reporter, fail='journal')

    def test_prune_reporter_failure_after_the_hold(self):
        for primary in self.PRIMARY_CASES:
            for reporter_label, make_reporter in self.REPORTERS:
                self._case(primary, reporter_label, make_reporter, fail='prune')

    def test_real_execute_live_journal_signal_keeps_the_held_sta_and_records_dirty_custody(self):
        for reporter_label, make_reporter in self.REPORTERS[1:]:
            with self.subTest(reporter=reporter_label):
                owned = Owned(self, make_hwp(), real_execute_live=True)
                real_execute = owned.service._execute_live
                owned.service._execute_live = lambda **kw: real_execute(**{**kw, 'timeout': self.REQUEST_TIMEOUT})
                fault = owned.arm('ParagraphShape', make_exc=KeyboardInterrupt)
                reporting = make_reporter()
                original = owned.session._persist_command

                def failing(command, original=original, reporting=reporting):
                    if command.name == 'command-bundle' and command.state != 'queued':
                        raise reporting
                    return original(command)

                patcher = patch.object(owned.session, '_persist_command', failing)
                patcher.start()
                self.addCleanup(patcher.stop)
                started = time.monotonic()
                result, error = owned.run(NUMBERING_STEP)
                elapsed = time.monotonic() - started
                self.assertIsNone(result)
                self.assertIsInstance(error, LocalCliServiceError)
                self.assertEqual(error.status_code, 500, 'the caller was left to the request timeout')
                self.assertIn('held', str(error))
                self.assertLess(elapsed, self.REQUEST_TIMEOUT)
                drained = drain_owning_thread(owned.session)
                patcher.stop()
                self.assertTrue(drained, 'the owning STA ended before it serviced its queue')
                self.assertTrue(any(b.get('working_copy_dirty') is True and b.get('native_custody_held') for b in owned.saved_bindings))
                self.assertTrue(owned.session._thread.is_alive())
                self.assertEqual(owned.cleanup_events(), [])
                self.assertEqual(owned.hook, [])
                self.assertIs(owned.manager._sessions.get(owned.sid), owned.session)
                self.assertTrue(owned.session.has_unreconciled_reconciliation())
                self.assertEqual(fault.after(), [])


class NonHeldReporterControlTests(unittest.TestCase):
    def test_a_reporter_signal_on_a_command_that_is_not_held_is_not_swallowed(self):
        owned = Owned(self, make_hwp())
        signal = Terminate('prune signal on a clean command')

        def failing_prune():
            raise signal

        patcher = patch.object(owned.session, '_prune_in_memory_commands', failing_prune)
        patcher.start()
        self.addCleanup(patcher.stop)
        value = owned.manager.execute(session_id=owned.sid, command_name='health_probe', handler=lambda h: {'ok': True}, timeout=5)
        self.assertEqual(value, {'ok': True})
        # The command result was delivered; the later reporter signal enters
        # finalization, which must preserve even a clean live wrapper.
        wait_until(lambda: owned.session._native_custody_held, 'unavailable finalization to retain custody')
        self.assertTrue(owned.session._thread.is_alive())
        self.assertFalse(owned.session._closed.is_set())
        self.assertEqual(owned.cleanup_events(), [])
        self.assertEqual(owned.hook, [])
        self.assertIs(owned.manager._sessions[owned.sid], owned.session)


if __name__ == '__main__':
    unittest.main()
