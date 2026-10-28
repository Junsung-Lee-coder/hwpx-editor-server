"""Portable recovery regressions; fake COM, real owning STA and service save caller.

The LockedWrapper methods below are the unmodified executable bodies of
pyhwpx 1.7.2 core.py clear/quit/__del__ (the admitted wheel is independently
compared by the task-local tests). In particular quit performs a hidden Clear.
These are not physical Hancom/exit or installed-byte evidence.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import json
import sys
import tempfile
import threading
import unittest

from app import local_cli_runtime as rt
from app.local_cli_service import LocalCliService

# Test-module global for the exact dependency destructor body, not a COM import.
pythoncom = NS(CoUninitialize=lambda: None)


class LockedWrapper:
    def clear(self, option: int = 1) -> None:
        return self.hwp.XHwpDocuments.Active_XHwpDocument.Clear(option=option)

    def quit(self, save: bool = False) -> None:
        if self.Path == "":
            self.clear()
        elif save:
            self.save()
        else:
            self.clear()
        self.hwp.Quit()

    def __del__(self):
        if self.on_quit:
            try:
                self.quit(save=False)
            except:
                pass
        pythoncom.CoUninitialize()


class RecoveryTests(unittest.TestCase):
    def owned(self, *, clear_fault_at=0):
        stack = ExitStack()
        self.addCleanup(stack.close)
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        root = folder / 'session'
        root.mkdir()
        wc = root / 'working.hwpx'
        wc.write_bytes(b'fixture')
        calls = []
        count = 0
        def clear(option):
            nonlocal count
            count += 1
            calls.append('clear')
            if count == clear_fault_at:
                raise ValueError('PRIVATE_NATIVE_MESSAGE')
        wrapper = LockedWrapper()
        wrapper.Path = str(wc)
        wrapper.on_quit = False
        wrapper.hwp = NS(XHwpDocuments=NS(Active_XHwpDocument=NS(Clear=clear)), Quit=lambda: calls.append('quit'))
        com = NS(CoInitialize=lambda: calls.append('initialize'), CoUninitialize=lambda: calls.append('uninitialize'))
        # Explicit test-only deterministic shutdown; never authorizes real cleanup.
        holder = {}
        def release():
            session = holder['session']
            if session._thread.is_alive():
                session._allow_native_cleanup = True
                with patch.object(rt.LocalCliLiveSession, '_finalize', lambda *a, **k: None):
                    try:
                        session.close(timeout=.2)
                    except Exception:
                        pass
                    session._commands.put(rt._LiveCommand('test-release', '__close__', None, rt.Future()))
                    session._thread.join(2)
            self.assertFalse(session._thread.is_alive(), 'test fake STA leaked')
        stack.callback(lambda: None)
        for name, value in (
            ('create_hwp_instance_with_recovery', lambda *a, **k: wrapper),
            ('open_document_with_recovery', lambda **k: wrapper),
            ('start_runtime_watchdog', lambda *a, **k: (None, None)),
            ('update_runtime_status', lambda *a, **k: None),
            ('current_windows_session_snapshot', lambda: {'interactive': True}),
            ('collect_hwp_snapshot', lambda h: {}),
            ('snapshot_live_location', lambda **k: {}),
        ):
            stack.enter_context(patch.object(rt, name, value))
        stack.enter_context(patch.dict(sys.modules, {'pythoncom': com, 'pyhwpx': NS(Hwp=LockedWrapper)}))
        manager = rt.LocalCliRuntimeManager()
        manager.open_session(session_id='recovery', session_root=root, working_copy_path=wc, source_filename='fixture.hwpx')
        session = manager._sessions['recovery']
        holder['session'] = session
        self.addCleanup(release)
        return manager, session, wrapper, calls, wc

    def test_escaped_handler_error_retains_sta_and_fixed_diagnostic_before_reporting(self):
        manager, session, wrapper, calls, wc = self.owned()
        def fail(handle):
            handle.diagnostic_site = 'save_native'
            raise ValueError('PRIVATE_NATIVE_MESSAGE')
        with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
            manager.execute(session_id='recovery', command_name='save', handler=fail)
        self.assertTrue(session._native_custody_held)
        self.assertTrue(session._thread.is_alive())
        status = session.command_status()
        self.assertEqual(status['native_diagnostic'], {'site': 'save_native', 'causal_class': 'ValueError'})
        raw = rt.command_journal_path(session.session_root).read_text()
        self.assertNotIn('PRIVATE_NATIVE_MESSAGE', raw)
        self.assertEqual(json.loads(raw)['commands'][-1]['native_diagnostic'], status['native_diagnostic'])
        with self.assertRaises(rt.LocalCliRuntimeError):
            manager.close_session('recovery', timeout=.1)
        self.assertNotIn('clear', calls)
        self.assertNotIn('quit', calls)
        self.assertNotIn('uninitialize', calls)

    def test_reporting_failure_does_not_release_ambiguous_handler(self):
        manager, session, wrapper, calls, wc = self.owned()
        persist = session._persist_command
        def report(command):
            if command.state in ('failed', 'failed_after_timeout'):
                raise OSError('PRIVATE_REPORT')
            return persist(command)
        with patch.object(session, '_persist_command', report):
            with self.assertRaises(rt.LocalCliRuntimeError):
                manager.execute(session_id='recovery', command_name='action', handler=lambda h: (_ for _ in ()).throw(ValueError('PRIVATE')))
        self.assertTrue(session._native_custody_held)
        self.assertTrue(session._thread.is_alive())
        self.assertEqual(calls, ['initialize'])

    def test_actual_service_save_localizes_native_vs_later_readback(self):
        for fault in ('native', 'readback'):
            with self.subTest(fault=fault):
                manager, session, wrapper, calls, wc = self.owned()
                def save():
                    if fault == 'native':
                        raise ValueError('PRIVATE')
                    return True
                wrapper.save = save
                service = object.__new__(LocalCliService)
                service._load_active_binding = lambda **k: {}
                service._working_copy_path = lambda b: wc
                service._verify_artifact_readback = lambda *a, **k: (_ for _ in ()).throw(OSError('PRIVATE'))
                service._execute_live = lambda **k: manager.execute(session_id='recovery', command_name=k['command_name'], handler=k['handler'])
                with self.assertRaises(rt.LocalCliRuntimeError):
                    service.save()
                self.assertEqual(session.command_status()['native_diagnostic']['site'], 'save_native' if fault == 'native' else 'save_custody_readback')
                self.assertTrue(session._thread.is_alive())
                self.assertNotIn('clear', calls)

    def test_failed_mutator_envelope_latches_before_reporting(self):
        manager, session, wrapper, calls, wc = self.owned()
        result = manager.execute(session_id='recovery', command_name='save', handler=lambda h: {
            'ok': False, 'dirty': False, 'steps': [{'ok': False, 'dirty': False, 'mutation_may_have_persisted': False}]})
        self.assertFalse(result['ok'])
        self.assertTrue(session._native_custody_held)
        self.assertTrue(session._thread.is_alive())
        with self.assertRaises(rt.LocalCliRuntimeError):
            session.close(timeout=.1)
        self.assertEqual(calls, ['initialize'])

    def test_private_exception_callbacks_are_never_needed_for_diagnostic(self):
        manager, session, wrapper, calls, wc = self.owned()
        callbacks = []
        class PrivateFault(Exception):
            def __str__(self):
                callbacks.append('str')
                raise AssertionError('private callback')
        with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
            manager.execute(session_id='recovery', command_name='save', handler=lambda h: (_ for _ in ()).throw(PrivateFault()))
        self.assertEqual(callbacks, [])
        self.assertEqual(session.command_status()['native_diagnostic'], {'site':'command_handler','causal_class':'UNKNOWN'})
        self.assertEqual(calls, ['initialize'])

    def test_close_command_not_successful_before_actual_finalizer(self):
        manager, session, wrapper, calls, wc = self.owned()
        entered, resume = threading.Event(), threading.Event()
        original = session._finalize
        def delayed(**kwargs):
            entered.set()
            resume.wait(2)
            return original(**kwargs)
        with patch.object(session, '_finalize', delayed), ThreadPoolExecutor(1) as pool:
            pending = pool.submit(session.close, timeout=3)
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(session._close_future.done())
                self.assertEqual(session.command_status()['state'], 'running')
            finally:
                resume.set()
            with self.assertRaises(rt.LocalCliRuntimeError):
                pending.result(3)
        self.assertFalse(session._closed.is_set())
        self.assertTrue(session._thread.is_alive())
        self.assertEqual(session.command_status()['state'], 'failed')
        self.assertEqual(calls, ['initialize'])

    def test_unqualified_close_stops_before_first_or_hidden_clear(self):
        for at in (1, 2):
            with self.subTest(at=at):
                manager, session, wrapper, calls, wc = self.owned(clear_fault_at=at)
                with self.assertRaises(rt.LocalCliRuntimeError):
                    session.close(timeout=.3)
                self.assertFalse(session._closed.is_set())
                self.assertTrue(session._thread.is_alive())
                self.assertTrue(session._native_custody_held)
                self.assertEqual(session.command_status()['state'], 'failed')
                with self.assertRaises(rt.LocalCliRuntimeError):
                    session.close(timeout=.1)
                self.assertEqual(calls, ['initialize'])  # closure unavailable before any Clear


if __name__ == '__main__':
    unittest.main()
