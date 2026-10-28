"""Fake SDK, real service save caller, owning STA, journal and failure-finally.

The two forwarding bodies below are from locked pyhwpx 1.7.2 core.py,
wheel SHA256 c79d6aa67baae9cd82fd7ae11217e985fa2fe832c61a09a2715aae854a0d1349.
The author receipt separately authenticates them against that retained wheel.
No physical COM, Hancom, release, or document-success evidence is claimed.
"""
import json
import threading
import unittest
from unittest.mock import patch

from app import atomic_json
from app import local_cli_runtime as rt
from app import local_cli_service as svc
from tests import test_cli_failure_recovery as recovery


class LockedSaveWrapper(recovery.LockedWrapper):
    @property
    def IsModified(self) -> bool:
        return self.hwp.IsModified

    def save(self, save_if_dirty: bool = True) -> bool:
        return self.hwp.Save(save_if_dirty=save_if_dirty)


class SaveDiagnosticTests(unittest.TestCase):
    owned = recovery.RecoveryTests.owned

    def prepare(self, *, modified=True, read_error=None, save_result=True, save_error=None):
        manager, session, wrapper, calls, wc = self.owned()
        wrapper.__class__ = LockedSaveWrapper
        sdk_events = []
        pre_save_rows = []
        journal = rt.command_journal_path(session.session_root)
        class SDK:
            @property
            def IsModified(self):
                sdk_events.append(('read', threading.get_ident()))
                if read_error is not None:
                    raise read_error
                return modified

            def Save(self, *, save_if_dirty):
                sdk_events.append(('save', threading.get_ident(), save_if_dirty))
                pre_save_rows.append(json.loads(journal.read_text())['commands'][-1])
                if save_error is not None:
                    raise save_error
                return save_result
        wrapper.hwp = SDK()
        service = object.__new__(svc.LocalCliService)
        service._load_active_binding = lambda **k: {'source_filename': 'fixture.hwpx'}
        service._working_copy_path = lambda b: wc
        service._verify_artifact_readback = lambda *a, **k: sdk_events.append(('readback', threading.get_ident()))
        service._execute_live = lambda **k: manager.execute(session_id='recovery', command_name='save', handler=k['handler'])
        service._update_live_binding = lambda b, **k: b
        service._record_local_cli_command = lambda *a, **k: None
        service._binding_session_id = lambda b: 'recovery'
        service._artifact_name = lambda **k: 'fixture.hwpx'
        service._artifact_download_path = lambda **k: 'fixture-download'
        return service, session, calls, sdk_events, pre_save_rows, journal

    def save(self, service):
        with patch.object(svc, 'snapshot_live_location', lambda **k: {}):
            return service.save()

    def private_row(self, session):
        return session._command_record(session._active_command or list(session._commands_by_id.values())[-1], include_private=True)

    def assert_held(self, session, calls, events):
        self.assertTrue(session._native_custody_held)
        self.assertTrue(session._thread.is_alive())
        self.assertTrue(list(session._commands_by_id.values())[-1].completed_event.wait(1))
        self.assertEqual(calls, ['initialize'])
        with self.assertRaises(rt.LocalCliRuntimeError):
            session.close(timeout=.1)
        self.assertEqual(calls, ['initialize'])
        self.assertNotIn('readback', [e[0] for e in events])

    def test_exact_clean_observation_skips_save_without_fabricating_return(self):
        for result in (True, False, None):
            with self.subTest(result=result):
                service, session, calls, events, before, journal = self.prepare(modified=False, save_result=result)
                self.assertTrue(self.save(service)['ok'])
                self.assertEqual([e[0] for e in events], ['read', 'readback'])
                self.assertEqual(before, [])
                final = json.loads(journal.read_text())['commands'][-1]['save_diagnostic']
                self.assertEqual(final['save_decision'], 'clean_noop')
                self.assertEqual(final['pre_save_receipt'], 'write_returned')
                self.assertIs(final['is_modified_value'], False)
                self.assertIs(final['save_return_arrived'], False)
                self.assertIsNone(final['save_returned_false'])
                self.assertNotIn('save_diagnostic', session.command_status())
                row = session._commands_by_id[session._last_command_id]
                self.assertIs(row.result['ordinary_save_confirmed'], False)
                self.assertIs(row.result['clean_save_noop_confirmed'], True)

    def test_dirty_observation_preserves_one_ordinary_save_and_false_failure(self):
        for result in (True, False, None):
            with self.subTest(result=result):
                service, session, calls, events, before, journal = self.prepare(modified=True, save_result=result)
                if result is not True:
                    with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                        self.save(service)
                    self.assert_held(session, calls, events)
                else:
                    self.assertTrue(self.save(service)['ok'])
                self.assertEqual([e[0] for e in events][:2], ['read', 'save'])
                self.assertEqual(events[0][1], session._thread.ident)
                self.assertEqual(events[1], ('save', session._thread.ident, True))
                self.assertEqual(sum(e[0] == 'read' for e in events), 1)
                self.assertEqual(sum(e[0] == 'save' for e in events), 1)
                prior = before[0]['save_diagnostic']
                self.assertEqual(prior['is_modified_type'], 'bool')
                self.assertIs(prior['is_modified_value'], True)
                self.assertIs(prior['read_return_arrived'], True)
                self.assertEqual(prior['pre_save_receipt'], 'pending')
                self.assertIs(prior['save_return_arrived'], False)
                final = json.loads(journal.read_text())['commands'][-1]['save_diagnostic']
                self.assertEqual(final['pre_save_receipt'], 'write_returned')
                self.assertEqual(final['save_decision'], 'ordinary_save')
                self.assertIs(final['save_return_arrived'], True)
                self.assertIs(final['save_returned_false'], result is False)
                self.assertNotIn('save_diagnostic', session.command_status())

    def test_clean_noop_keeps_all_readbacks_and_readback_failure_held(self):
        for site in ('save_file_readback', 'save_custody_readback', 'save_location_readback'):
            with self.subTest(site=site):
                service, session, calls, events, before, journal = self.prepare(modified=False)
                def fail(*a, **k):
                    raise OSError('PRIVATE')
                if site == 'save_file_readback':
                    service._working_copy_path({}).unlink()
                elif site == 'save_custody_readback':
                    service._verify_artifact_readback = fail
                with patch.object(svc, 'snapshot_live_location', fail if site == 'save_location_readback' else lambda **k: {}):
                    with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                        service.save()
                self.assertEqual(session.command_status()['native_diagnostic']['site'], site)
                self.assertTrue(session._native_custody_held)
                self.assertNotIn('save', [e[0] for e in events])

    def test_nonbool_primitives_and_objects_are_not_clean_or_coerced(self):
        callbacks = []
        class Meta(type):
            @property
            def __name__(cls):
                callbacks.append('metaclass')
                raise AssertionError('PRIVATE')
        class Foreign(metaclass=Meta):
            def __bool__(self):
                callbacks.append('bool')
                return False
            def __str__(self):
                callbacks.append('str')
                return 'PRIVATE'
            def __repr__(self):
                callbacks.append('repr')
                return 'PRIVATE'
        for value, kind in ((0, 'int'), (1, 'int'), (-1, 'int'), (1.5, 'float'),
                            (None, 'NoneType'), ('PRIVATE', 'str'), (Foreign(), 'UNKNOWN'),
                            (1 << 10000, 'int'), (float('nan'), 'float')):
            with self.subTest(kind=kind):
                service, session, calls, events, before, journal = self.prepare(modified=value)
                with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                    self.save(service)
                self.assert_held(session, calls, events)
                self.assertEqual([e[0] for e in events], ['read'])
                row = json.loads(journal.read_text())['commands'][-1]['save_diagnostic']
                self.assertIs(row['read_return_arrived'], True)
                self.assertEqual(row['is_modified_state'], 'OTHER')
                self.assertEqual(row['is_modified_type'], kind)
                if type(value) is int and -(2**31) <= value < 2**31 or type(value) is float and value == 1.5:
                    self.assertEqual(row['is_modified_value'], value)
                self.assertNotIn('PRIVATE', journal.read_text())
        self.assertEqual(callbacks, [])

    def test_missing_read_errors_and_signals_preserve_arrival_and_stop(self):
        callbacks = []
        class PrivateFault(Exception):
            def __str__(self):
                callbacks.append('str')
                raise AssertionError('PRIVATE')
            def __repr__(self):
                callbacks.append('repr')
                raise AssertionError('PRIVATE')
        for fault, state in ((AttributeError('PRIVATE'), 'UNAVAILABLE'),
                             (ValueError('PRIVATE'), 'ERROR'),
                             (KeyboardInterrupt('PRIVATE'), 'ERROR'),
                             (PrivateFault(), 'ERROR')):
            with self.subTest(state=state):
                service, session, calls, events, before, journal = self.prepare(read_error=fault)
                with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                    self.save(service)
                self.assert_held(session, calls, events)
                row = json.loads(journal.read_text())['commands'][-1]['save_diagnostic']
                self.assertIs(row['read_return_arrived'], False)
                self.assertEqual(row['is_modified_state'], state)
                self.assertIs(row['save_return_arrived'], False)
                self.assertEqual([e[0] for e in events], ['read'])
                self.assertNotIn('PRIVATE', journal.read_text())
        self.assertEqual(callbacks, [])

    def test_journal_read_or_write_failure_retains_observed_value_before_save(self):
        for fault in ('read', 'write'):
            with self.subTest(fault=fault):
                service, session, calls, events, before, journal = self.prepare(modified=False)
                triggered = []
                original_open = type(journal).open
                original_replace = atomic_json.os.replace
                def eligible():
                    command = session._active_command
                    return command is not None and getattr(command, 'save_diagnostic', None) is not None and not triggered
                def open_file(path, *args, **kwargs):
                    if fault == 'read' and path == journal and args and args[0] == 'rb' and eligible():
                        triggered.append('read')
                        raise OSError('PRIVATE_READ')
                    return original_open(path, *args, **kwargs)
                def replace(src, dst):
                    if fault == 'write' and dst == journal and eligible():
                        triggered.append('write')
                        raise OSError('PRIVATE_WRITE')
                    return original_replace(src, dst)
                with patch.object(type(journal), 'open', open_file), patch.object(atomic_json.os, 'replace', replace):
                    with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                        self.save(service)
                self.assertEqual(triggered, [fault])
                self.assert_held(session, calls, events)
                self.assertEqual([e[0] for e in events], ['read'])
                row = json.loads(journal.read_text())['commands'][-1]['save_diagnostic']
                self.assertIs(row['read_return_arrived'], True)
                self.assertIs(row['is_modified_value'], False)
                self.assertEqual(row['pre_save_receipt'], 'write_failed')
                self.assertIs(row['save_return_arrived'], False)
                self.assertNotIn('PRIVATE', journal.read_text())

    def test_native_exception_and_failure_finally_keep_pre_save_receipt(self):
        for reporting_fails in (False, True):
            with self.subTest(reporting_fails=reporting_fails):
                service, session, calls, events, before, journal = self.prepare(save_error=ValueError('PRIVATE'))
                persist = session._persist_command
                def report(command):
                    if reporting_fails and command.custody_held:
                        raise OSError('PRIVATE_REPORT')
                    return persist(command)
                with patch.object(session, '_persist_command', report):
                    with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                        self.save(service)
                self.assert_held(session, calls, events)
                row = self.private_row(session)['save_diagnostic']
                self.assertEqual(row['pre_save_receipt'], 'write_returned')
                self.assertIs(row['save_return_arrived'], False)
                self.assertIsNone(row['save_returned_false'])
                self.assertEqual([e[0] for e in events], ['read', 'save'])
                self.assertIs(json.loads(journal.read_text())['commands'][-1]['save_diagnostic']['read_return_arrived'], True)
                self.assertNotIn('PRIVATE', journal.read_text())

    def test_observer_refuses_non_owner_thread_and_held_sta_without_sdk_read(self):
        service, session, calls, events, before, journal = self.prepare()
        with self.assertRaises(rt.LocalCliRuntimeError):
            session._observe_pre_save_is_modified()
        self.assertEqual(events, [])
        def held(handle):
            handle.hold_native_custody('test hold', None)
            return handle.observe_pre_save()
        with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
            session.execute(command_name='save', handler=held, timeout=2)
        self.assertEqual(events, [])
        self.assert_held(session, calls, events)

    def test_hold_during_receipt_write_stops_before_save_or_post_latch_read(self):
        service, session, calls, events, before, journal = self.prepare()
        persist = session._persist_command
        latched = []
        def report(command):
            result = persist(command)
            if command.save_diagnostic is not None and not latched:
                latched.append(True)
                session._hold_native_custody(command, 'test concurrent hold', None)
            return result
        with patch.object(session, '_persist_command', report):
            with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                self.save(service)
        self.assertEqual(latched, [True])
        self.assertEqual([e[0] for e in events], ['read'])
        self.assert_held(session, calls, events)
        row = json.loads(journal.read_text())['commands'][-1]['save_diagnostic']
        self.assertEqual(row['pre_save_receipt'], 'write_returned')
        self.assertIs(row['save_return_arrived'], False)

    def test_false_return_arrival_is_not_falsely_claimed_durable_when_reporting_fails(self):
        service, session, calls, events, before, journal = self.prepare(save_result=False)
        persist = session._persist_command
        def report(command):
            if command.custody_held:
                raise OSError('PRIVATE_REPORT')
            return persist(command)
        with patch.object(session, '_persist_command', report):
            with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                self.save(service)
            # Observe before the explicit close-refusal control can flush the journal.
            memory = self.private_row(session)['save_diagnostic']
            retained = json.loads(journal.read_text())['commands'][-1]['save_diagnostic']
        self.assert_held(session, calls, events)
        self.assertIs(memory['save_return_arrived'], True)
        self.assertIs(memory['save_returned_false'], True)
        self.assertEqual(retained['pre_save_receipt'], 'pending')
        self.assertIs(retained['save_return_arrived'], False)
        self.assertIsNone(retained['save_returned_false'])
        self.assertEqual([e[0] for e in events], ['read', 'save'])

    def test_pre_save_write_and_all_failure_reporting_fail_without_native_continuation(self):
        service, session, calls, events, before, journal = self.prepare(modified=False)
        persist = session._persist_command
        def report(command):
            if command.save_diagnostic is not None:
                raise OSError('PRIVATE_REPORT')
            return persist(command)
        with patch.object(session, '_persist_command', report):
            with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                self.save(service)
            row = self.private_row(session)['save_diagnostic']
            self.assertEqual(row['pre_save_receipt'], 'write_failed')
            self.assertIs(row['read_return_arrived'], True)
            self.assertIs(row['is_modified_value'], False)
            self.assertIs(row['save_return_arrived'], False)
            self.assertNotIn('save_diagnostic', json.loads(journal.read_text())['commands'][-1])
        self.assertEqual([e[0] for e in events], ['read'])
        self.assert_held(session, calls, events)


if __name__ == '__main__':
    unittest.main()
