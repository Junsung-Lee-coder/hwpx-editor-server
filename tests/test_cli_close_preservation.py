"""B1 successor: real service + manager + owning STA, fake native/storage edges.

The immutable reviewer witnesses remain separate and untouched. These tests
accept rightful refusal rather than requiring the predecessor's unsafe close.
"""
import json
import threading
import unittest
from unittest.mock import patch
from app import local_cli_runtime as rt
from app import local_cli_service as svc
import test_cli_native_close as fixture


class ClosePreservationTests(unittest.TestCase):
    owned = fixture.NativeCloseTests.owned

    def prepare(self, *, prior_dirty=False):
        manager, session, events, state, *_ = self.owned()
        stored = {'session_id': 'close-case', 'source_filename': 'fixture.hwpx',
                  'working_copy_dirty': prior_dirty, 'dirty': prior_dirty,
                  'command_generation': 0, 'native_command_sequence': 0,
                  'live_session_bound': True}
        service = object.__new__(svc.LocalCliService)
        service.runtime_manager = manager
        service._require_ready_runtime = lambda *a: None
        service._read_binding = lambda **kw: dict(stored)
        def save_binding(binding):
            stored.clear()
            stored.update(binding)
            return binding
        service._save_binding = save_binding
        service._working_copy_path = lambda b: session.working_copy_path
        service._verify_artifact_readback = lambda *a, **kw: events.append(('readback', threading.get_ident()))
        service._record_local_cli_command = lambda *a, **kw: None
        service._binding_session_id = lambda b: 'close-case'
        service._artifact_name = lambda **kw: 'fixture.hwpx'
        service._artifact_download_path = lambda **kw: 'fixture-download'
        for name in ('_record_session_close', '_clear_binding', '_mark_session_closed', '_cleanup_managed_session_root'):
            setattr(service, name, lambda *a, _name=name, **kw: events.append((_name,)))
        state['save_result'] = True
        state['save_error'] = None
        def save(*, save_if_dirty):
            events.append(('save', threading.get_ident(), save_if_dirty))
            if state['save_error'] is not None:
                raise state['save_error']
            result = state['save_result']
            if result is True:
                state['modified'] = False
            return result
        session._active_hwp.hwp.Save = save
        return service, manager, session, events, state, stored

    def checkpoint(self, service):
        with patch.object(svc, 'snapshot_live_location', return_value={'document_is_modified': False}):
            return service.save()

    def retained(self, manager, session, events):
        names = [e[0] for e in events]
        for forbidden in ('quit', 'clear', 'uninitialize', '_record_session_close', '_clear_binding',
                          '_mark_session_closed', '_cleanup_managed_session_root'):
            self.assertNotIn(forbidden, names)
        self.assertTrue(session._thread.is_alive())
        self.assertFalse(session._closed.is_set())
        self.assertIsNotNone(session._active_hwp)
        self.assertIs(manager._sessions.get('close-case'), session)
        self.assertEqual(manager.reap_settled_sessions(), 0)
        self.assertIn('close-case', manager._session_roots)

    def test_prior_dirty_clean_noop_public_close_refuses_and_retains(self):
        service, manager, session, events, state, stored = self.prepare(prior_dirty=True)
        self.assertTrue(self.checkpoint(service)['ok'])
        self.assertTrue(stored['working_copy_dirty'])
        diag = session._commands_by_id[session._last_command_id].save_diagnostic
        self.assertEqual(diag['save_decision'], 'clean_noop')
        self.assertIs(diag['save_return_arrived'], False)
        self.assertNotIn('save', [e[0] for e in events])
        with self.assertRaises(svc.LocalCliServiceError):
            service.close()
        self.retained(manager, session, events)

    def test_real_save_caller_propagates_prior_dirty_to_direct_manager_close(self):
        service, manager, session, events, state, stored = self.prepare(prior_dirty=True)
        self.assertTrue(self.checkpoint(service)['ok'])
        with self.assertRaises(rt.LocalCliRuntimeError):
            manager.close_session('close-case', timeout=1)
        self.retained(manager, session, events)

    def test_clean_no_prior_edit_noop_still_closes_once(self):
        service, manager, session, events, state, stored = self.prepare()
        self.assertTrue(self.checkpoint(service)['ok'])
        self.assertFalse(stored['working_copy_dirty'])
        self.assertTrue(service.close()['ok'])
        self.assertTrue(session._closed.is_set())
        self.assertNotIn('close-case', manager._sessions)
        self.assertEqual([e[0] for e in events].count('quit'), 1)
        self.assertEqual([e[0] for e in events].count('uninitialize'), 2)
        self.assertNotIn('save', [e[0] for e in events])

    def test_prior_dirty_without_save_stale_cleanup_cannot_bypass_admission(self):
        service, manager, session, events, state, stored = self.prepare(prior_dirty=True)
        self.assertIs(service._cleanup_stale_binding(dict(stored)), False)
        self.retained(manager, session, events)

    def test_export_native_clean_and_dirty_projection_reset_do_not_erase_edit_debt(self):
        service, manager, session, events, state, stored = self.prepare()
        def mutate(handle):
            events.append(('edit', threading.get_ident()))
            state['modified'] = True
            return {'ok': True, 'dirty': True}
        service._execute_live(binding=dict(stored), command_name='type', task_label='test.edit', handler=mutate)
        exported = session.session_root / 'output' / 'export.pdf'
        exported.parent.mkdir(exist_ok=True)
        exported.write_bytes(b'synthetic PDF fixture')
        def export(**kw):
            events.append(('export', threading.get_ident()))
            state['modified'] = False
            return exported
        service._validated_artifact_projection = lambda **kw: kw['artifacts']
        with patch.object(svc, 'export_document_pdf', export), patch.object(svc, 'snapshot_live_location', return_value={'document_is_modified': False}):
            self.assertTrue(service.export()['ok'])
        self.assertFalse(stored['working_copy_dirty'])
        self.assertTrue(self.checkpoint(service)['ok'])
        with self.assertRaises(rt.LocalCliRuntimeError):
            manager.close_session('close-case', timeout=1)
        self.retained(manager, session, events)

    def test_only_affirmative_ordinary_save_with_readbacks_can_discharge_debt(self):
        service, manager, session, events, state, stored = self.prepare(prior_dirty=True)
        self.assertTrue(self.checkpoint(service)['ok'])
        with self.assertRaises(svc.LocalCliServiceError):
            service.close()
        state['modified'] = True
        self.assertTrue(self.checkpoint(service)['ok'])
        self.assertFalse(stored['working_copy_dirty'])
        self.assertIs(stored['dirty'], False)
        self.assertTrue(service.close()['ok'])
        self.assertEqual([e[0] for e in events].count('save'), 1)
        self.assertEqual([e[0] for e in events].count('quit'), 1)
        self.assertTrue(session._closed.is_set())
        self.assertTrue(session._lifetime_edit_possible, 'Original sticky safety history must not be reset.')

    def test_new_edit_after_confirmed_save_requires_new_preservation(self):
        service, manager, session, events, state, stored = self.prepare(prior_dirty=True)
        state['modified'] = True
        self.assertTrue(self.checkpoint(service)['ok'])
        self.assertFalse(stored['working_copy_dirty'])
        manager.execute(session_id='close-case', command_name='type', handler=lambda h: {'ok': True, 'dirty': False})
        # An old successful Save plus a later false delta/native clean flag
        # must not authorize teardown of the later invocation.
        with self.assertRaises(svc.LocalCliServiceError):
            service.close()
        self.retained(manager, session, events)

    def test_dirty_false_unknown_and_exception_save_results_remain_held(self):
        for value in (False, None, 1, 'true', OSError('synthetic save failure')):
            with self.subTest(result=type(value).__name__):
                service, manager, session, events, state, stored = self.prepare(prior_dirty=True)
                state['modified'] = True
                if isinstance(value, BaseException):
                    state['save_error'] = value
                else:
                    state['save_result'] = value
                with self.assertRaises(svc.LocalCliServiceError):
                    self.checkpoint(service)
                self.assertTrue(stored['working_copy_dirty'])
                with self.assertRaises(rt.LocalCliRuntimeError):
                    manager.close_session('close-case', timeout=1)
                self.retained(manager, session, events)
                self.assertEqual([e[0] for e in events].count('save'), 1)
                self.assertNotIn('readback', [e[0] for e in events])

    def test_affirmative_save_readback_failure_does_not_authorize_close(self):
        service, manager, session, events, state, stored = self.prepare(prior_dirty=True)
        state['modified'] = True
        def fail(*a, **kw):
            raise OSError('synthetic custody readback failure')
        service._verify_artifact_readback = fail
        with self.assertRaises(svc.LocalCliServiceError):
            self.checkpoint(service)
        self.assertTrue(stored['working_copy_dirty'])
        with self.assertRaises(rt.LocalCliRuntimeError):
            manager.close_session('close-case', timeout=1)
        self.retained(manager, session, events)

    def test_close_queued_during_real_mutator_rechecks_debt_before_finalizer(self):
        service, manager, session, events, state, stored = self.prepare()
        entered, finish = threading.Event(), threading.Event()
        failures = []
        def mutate(handle):
            entered.set()
            if not finish.wait(2):
                raise AssertionError('test release missing')
            return {'ok': True, 'dirty': True}
        def execute():
            try:
                manager.execute(session_id='close-case', command_name='type', handler=mutate)
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=execute)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            # Native clean flag intentionally disagrees with known invocation.
            state['modified'] = False
            timer = threading.Timer(.05, finish.set)
            timer.start()
            try:
                with self.assertRaises(rt.LocalCliRuntimeError):
                    manager.close_session('close-case', timeout=1)
            finally:
                timer.join(1)
        finally:
            finish.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.retained(manager, session, events)

    def test_dequeued_close_rechecks_debt_even_when_admission_was_bypassed(self):
        service, manager, session, events, state, stored = self.prepare()
        manager.execute(session_id='close-case', command_name='type', handler=lambda h: {'ok': True, 'dirty': True})
        future = rt.Future()
        command = rt._LiveCommand('close-case:close', '__close__', None, future)
        with session._state_lock:
            session._commands_by_id[command.command_id] = command
            session._closing = True
        session._commands.put(command)
        with self.assertRaises(rt.LocalCliRuntimeError):
            future.result(timeout=1)
        self.assertFalse(session._closing)
        self.retained(manager, session, events)

    def test_finalizer_native_boundary_independently_refuses_unpreserved_debt(self):
        service, manager, session, events, state, stored = self.prepare()
        manager.execute(session_id='close-case', command_name='type', handler=lambda h: {'ok': True, 'dirty': True})
        def native_boundary(handle):
            try:
                session._close_original_native(handle.hwp)
            except rt.LocalCliRuntimeError:
                return {'ok': True, 'refused': True}
            return {'ok': True, 'refused': False}
        result = manager.execute(session_id='close-case', command_name='health_probe', handler=native_boundary)
        self.assertIs(result['refused'], True)
        self.retained(manager, session, events)


if __name__ == '__main__':
    unittest.main(verbosity=2)
