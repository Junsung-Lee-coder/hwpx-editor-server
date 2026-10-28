"""Owning-STA custody regressions for numbering outcomes that are unknown after a native mutator was reached.

Real LocalCliRuntimeManager + LocalCliLiveSession owning thread + real command_bundle + real numbering
callbacks; only the SDK/COM objects are fakes. This proves source behaviour against fakes; it is NOT Hancom
evidence and makes no native claim.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app import local_cli_runtime as rt
from app import native_numbering as nn
from app import native_numbering_census as census_mod
from app.command_packages.runtime import get_command_package_registry
from app.local_cli_runtime import LocalCliRuntimeError, LocalCliRuntimeTimeoutError
from app.local_cli_service import LocalCliService
from app.local_cli_service_support import LocalCliServiceError
from tests.test_native_numbering_review_contract import NUMBERING_STEP, PAGE_STEP, _hwpml, executes

NAV = {'set_pos', 'SetPos', 'MoveDocBegin', 'MoveRight', 'MoveDocEnd', 'select_text', 'find', 'Run', 'Undo', 'Close', 'Quit', 'Clear', 'Discard', 'save', 'save_as'}
MUTATORS = {'OutlineNumber', 'ParagraphShape', 'PageNumPos'}
CLEANUP = {'clear', 'quit', 'CoUninitialize'}


class Terminate(BaseException):
    pass


class Exploding:
    def __getattr__(self, name):
        raise RuntimeError(f'COM property {name} failed')


_UNSET = object()


class Fault:
    """Wraps HAction.Execute: records mutator reach and optionally raises after the real fake mutator returned."""

    def __init__(self, hwp, action, make_exc=None, exploding=False, ret=_UNSET):
        self.action, self.exploding, self.make_exc, self.ret = action, exploding, make_exc, ret
        self.hwp = hwp
        self.attempted: list[str] = []
        self.injected: list[BaseException] = []
        self.mark = None
        self.fired = False
        original = hwp.HAction.Execute

        def run(name, hset):
            if name in MUTATORS:
                self.attempted.append(name)
            out = original(name, hset)
            if name in MUTATORS:
                self.fired = True
            if name == self.action:
                self.mark = len(hwp.calls)
                if self.exploding:
                    hwp.HParameterSet = Exploding()
                if self.make_exc is not None:
                    exc = self.make_exc()
                    self.injected.append(exc)
                    raise exc
                if self.ret is not _UNSET:
                    return self.ret
            return out

        hwp.HAction.Execute = run

    def after(self):
        if self.mark is None:
            return []
        return sorted({c[0] for c in self.hwp.calls[self.mark:] if c[0] in NAV})


def chain(exc):
    """Every exception reachable from exc through __cause__, __context__ and reporting_errors."""
    seen, todo = [], [exc]
    while todo:
        cur = todo.pop()
        if cur is None or any(cur is s for s in seen):
            continue
        seen.append(cur)
        todo.extend([cur.__cause__, cur.__context__, *getattr(cur, 'reporting_errors', [])])
    return seen


class Owned:
    """A real manager + real owning thread over a fake SDK, with the real command_bundle bound to it."""

    def __init__(self, case, hwp, *, protection=None, session_id='custody-session-0001', real_execute_live=False):
        self.case = case
        self.hwp = hwp
        hwp.cursor = (0, 2, 0)
        self.events: list[str] = []
        self.hook: list[str] = []
        self.sid = session_id
        self.snapshots_after_effect = 0
        self.fault: Fault | None = None
        self.persisted: dict = {}
        self.saved_bindings: list[dict] = []
        hwp.clear = lambda *a, **k: self.events.append('clear')
        hwp.quit = lambda: self.events.append('quit')
        fake_com = NS(CoInitialize=lambda: self.events.append('CoInitialize'), CoUninitialize=lambda: self.events.append('CoUninitialize'))
        stack = ExitStack()
        case.addCleanup(stack.close)
        old_hook = threading.excepthook
        threading.excepthook = lambda args: self.hook.append(f'{args.exc_type.__name__}')
        stack.callback(setattr, threading, 'excepthook', old_hook)
        tmp = tempfile.TemporaryDirectory()
        stack.callback(tmp.cleanup)
        root = Path(tmp.name)
        stack.enter_context(patch.dict(sys.modules, {'pythoncom': fake_com, 'pyhwpx': NS(Hwp=lambda: hwp)}))
        for name, value in (
            ('create_hwp_instance_with_recovery', lambda Hwp, **kw: Hwp()),
            ('open_document_with_recovery', lambda *, Hwp, hwp, **kw: hwp),
            ('start_runtime_watchdog', lambda *a, **k: (None, None)),
            ('update_runtime_status', lambda *a, **k: None),
            ('current_windows_session_snapshot', lambda: {'interactive': True, 'session_id': 1, 'session_name': 'fake'}),
            ('collect_hwp_snapshot', lambda h: {}),
            ('snapshot_live_location', lambda **kw: {}),
        ):
            stack.enter_context(patch.object(rt, name, value))
        (root / 'session').mkdir()
        self.root = root / 'session'
        self.manager = rt.LocalCliRuntimeManager()
        self.manager.open_session(session_id=session_id, session_root=self.root, working_copy_path=root / 'wc.hwpx', source_filename='document.hwpx')
        self.session = self.manager._sessions[session_id]
        # Registered after stack.close so the final-release adapter runs while the fakes are still patched.
        case.addCleanup(self.final_release)
        self.service = self._make_service(protection, real_execute_live)

    def arm(self, action, make_exc=None, exploding=False, ret=_UNSET):
        self.fault = Fault(self.hwp, action, make_exc=make_exc, exploding=exploding, ret=ret)
        return self.fault

    def final_release(self):
        """TEST-ONLY adapter, run after every assertion, so the fake owning thread can settle. Not a production path."""
        session = self.session
        if session._thread.is_alive():
            session._allow_native_cleanup = True
            # Fake-only release after assertions. The real wrapper has no
            # qualified full closure interface; never fake one in production.
            with patch.object(rt.LocalCliLiveSession, '_finalize', lambda *a, **k: None):
                session._commands.put(rt._LiveCommand('test-release', '__close__', None, rt.Future()))
                session._thread.join(2)
        if session._thread.is_alive():
            raise AssertionError('fake owning thread did not settle')

    def _snapshot_location(self, **kw):
        if self.fault is not None and self.fault.fired:
            self.snapshots_after_effect += 1
        return {}

    def _make_service(self, protection, real_execute_live):
        service = object.__new__(LocalCliService)
        binding = {'session_id': self.sid, 'command_generation': 0, 'native_command_sequence': 0}
        service.command_packages = get_command_package_registry()
        service._load_active_binding = lambda session_id=None: dict(binding)
        if real_execute_live:
            service.runtime_manager = self.manager
            service._require_ready_runtime = lambda label: {}
            service._read_binding = lambda session_id=None: dict(binding)
            service.interactive_sessions = NS(record_command=lambda *a, **k: None)
            service._cleanup_stale_binding = lambda *a, **k: False
        else:
            def executor(*, binding, command_name, task_label, handler, timeout=90.0):
                return self.manager.execute(session_id=self.sid, command_name=command_name, handler=handler, timeout=20.0)

            service._execute_live = executor

        def find(h, *, match, expected_page):
            for i, p in enumerate(h.paragraphs):
                if match in str(p['text']):
                    h.cursor = (0, i, 0)
                    return {'page': expected_page, 'pos': [0, i, 0]}
            raise LocalCliRuntimeError('target not found')

        def bundle_snapshot(h):
            if self.fault is not None and self.fault.fired:
                self.snapshots_after_effect += 1
            return {}

        service._find_text_on_expected_page = find
        service._bundle_compact_snapshot = bundle_snapshot
        service._update_live_binding = lambda cur, **kw: self.persisted.update(kw) or cur
        service._save_binding = lambda cur: self.saved_bindings.append(dict(cur)) or cur
        service._record_local_cli_command = lambda *a, **k: None
        service._validated_artifact_projection = lambda **kw: {}
        service._public_bundle_steps = lambda **kw: kw['steps']
        service._public_artifacts = lambda **kw: {}
        service._binding_session_id = lambda cur: cur['session_id']
        if protection is not None:
            service._numbering_protection_reader = lambda handle: protection
        return service

    def run(self, step):
        with patch('app.local_cli_service.snapshot_live_location', side_effect=self._snapshot_location):
            try:
                return self.service.command_bundle(steps=[dict(step)]), None
            except BaseException as exc:  # noqa: BLE001
                return None, exc

    def cleanup_events(self):
        return [e for e in self.events if e in CLEANUP]

    def command(self):
        return self.manager.command_status(self.sid)


def make_hwp(*, foreign=False):
    return _hwpml(foreign=foreign)


class CustodyAssertions:
    def assert_noncomplete_result(self, owned, result, error, *, signal=None):
        self.assertIsNone(error, f'the caller got an exception instead of an honest noncomplete result: {error!r}')
        self.assertIsNotNone(result)
        self.assertIs(result['ok'], False)
        self.assertIs(result['dirty'], True)
        step = result['steps'][0]
        self.assertIs(step['ok'], False)
        self.assertIs(step['dirty'], True)
        self.assertIs(step['mutation_may_have_persisted'], True)
        self.assertIs(owned.persisted.get('dirty'), True)
        if signal is not None:
            self.assertIn('UNKNOWN' if type(signal) is Terminate else type(signal).__name__, step['error'])

    def assert_retained(self, owned):
        s = owned.session
        self.assertTrue(s._thread.is_alive(), 'the owning STA thread exited')
        self.assertFalse(s._closed.is_set())
        self.assertEqual(owned.cleanup_events(), [], 'native cleanup ran')
        self.assertEqual(owned.hook, [], 'a BaseException escaped the owning thread')
        self.assertIs(owned.manager._sessions.get(owned.sid), s, 'registry binding was removed')
        self.assertTrue(s._quarantined)
        self.assertTrue(s.is_terminal())
        self.assertTrue(s.has_unreconciled_reconciliation())
        status = owned.command()
        self.assertEqual(status.get('state'), 'failed')
        self.assertIs(status.get('timed_out'), False)
        self.assertIs(status.get('may_have_mutated'), True)
        self.assertIs(status.get('semantic_ok'), False)

    def assert_no_follow_on(self, owned, fault):
        self.assertEqual(fault.after(), [])
        self.assertEqual(owned.snapshots_after_effect, 0)

    def assert_custody_survives_follow_ons(self, owned):
        manager, session, sid, hwp = owned.manager, owned.session, owned.sid, owned.hwp
        cid = owned.command()['command_id']
        calls = list(hwp.calls)
        events = list(owned.events)
        invoked: list[int] = []
        with self.assertRaises(LocalCliRuntimeError) as refused:
            manager.execute(session_id=sid, command_name='health_probe', handler=lambda h: invoked.append(1) or {'ok': True}, timeout=5)
        self.assertNotIsInstance(refused.exception, LocalCliRuntimeTimeoutError)
        self.assertEqual(invoked, [])
        for timeout in (30.0, 1.0):
            with self.assertRaises(LocalCliRuntimeError):
                manager.close_session(sid, timeout=timeout)
        with self.assertRaises(LocalCliRuntimeError):
            session.close(timeout=1.0)
        self.assertEqual(manager.reap_settled_sessions(), 0)
        recon = manager.reconcile_command(sid, cid)
        self.assertNotEqual((recon.get('recovery') or {}).get('state'), 'preserved')
        self.assertIsNot(recon.get('reconciled'), True)
        ack = manager.acknowledge_reconciliation(sid, cid)
        self.assertIsNot(ack.get('reconciled'), True)
        with self.assertRaises(LocalCliRuntimeError):
            manager.close_session(sid, timeout=1.0)
        self.assertEqual(manager.reap_settled_sessions(), 0)
        with self.assertRaises(LocalCliRuntimeError):
            manager.open_session(session_id=sid, session_root=owned.root, working_copy_path=owned.root / 'wc.hwpx', source_filename='document.hwpx')
        self.assertIs(manager._sessions.get(sid), session)
        self.assertTrue(session._thread.is_alive())
        self.assertFalse(session._closed.is_set())
        self.assertTrue(session.has_unreconciled_reconciliation())
        self.assertEqual(hwp.calls, calls, 'a follow-on native callback ran')
        self.assertEqual(owned.events, events)
        self.assertEqual(owned.cleanup_events(), [])
        status = owned.command()
        self.assertIs(status.get('may_have_mutated'), True)
        self.assertIs(manager.command_custody(sid, cid).get('may_have_mutated'), True)
        journal = rt.read_command_journal(owned.root, cid)
        self.assertIs(journal.get('may_have_mutated'), True)
        self.assertNotEqual(journal.get('state'), 'succeeded')


SIGNALS = (KeyboardInterrupt, SystemExit, Terminate)


class OwningThreadSignalTests(CustodyAssertions, unittest.TestCase):
    def _signal_case(self, label, step, action, exc_type, *, foreign=False, protection=False):
        with self.subTest(case=label):
            hwp = make_hwp(foreign=foreign)
            owned = Owned(self, hwp, protection=census_mod.live_protection_reader(hwp) if protection else None)
            fault = owned.arm(action, make_exc=exc_type)
            result, error = owned.run(step)
            self.assertEqual(len(fault.injected), 1)
            self.assert_noncomplete_result(owned, result, error, signal=fault.injected[0])
            self.assert_retained(owned)
            self.assert_no_follow_on(owned, fault)
            record = owned.session._commands_by_id[owned.command()['command_id']]
            self.assertTrue(any(e is fault.injected[0] for e in chain(record.error)), 'the original signal is not in the cause chain')
            self.assert_custody_survives_follow_ons(owned)

    def test_official_existing_definition_signal_after_paragraph_shape(self):
        for exc_type in SIGNALS:
            self._signal_case(f'official/existing/{exc_type.__name__}', NUMBERING_STEP, 'ParagraphShape', exc_type)

    def test_official_definition_first_signal_after_each_mutator(self):
        for exc_type in SIGNALS:
            self._signal_case(f'official/definition-first/OutlineNumber/{exc_type.__name__}', NUMBERING_STEP, 'OutlineNumber', exc_type, foreign=True)
            self._signal_case(f'official/definition-first/ParagraphShape/{exc_type.__name__}', NUMBERING_STEP, 'ParagraphShape', exc_type, foreign=True)

    def test_page_number_signal_after_page_num_pos(self):
        for exc_type in SIGNALS:
            self._signal_case(f'page/{exc_type.__name__}', PAGE_STEP, 'PageNumPos', exc_type)

    def test_page_number_signal_inside_the_post_call_comparison(self):
        for exc_type in SIGNALS:
            with self.subTest(exc=exc_type.__name__):
                hwp = make_hwp()
                owned = Owned(self, hwp, protection=census_mod.live_protection_reader(hwp))
                fault = owned.arm('NoSuchAction')
                signal = exc_type()
                with patch.object(nn, '_page_number_delta_problems', side_effect=signal):
                    result, error = owned.run(PAGE_STEP)
                self.assertEqual(fault.attempted, ['PageNumPos'])
                self.assert_noncomplete_result(owned, result, error, signal=signal)
                self.assert_retained(owned)
                self.assert_no_follow_on(owned, fault)
                record = owned.session._commands_by_id[owned.command()['command_id']]
                self.assertTrue(any(e is signal for e in chain(record.error)))
                self.assert_custody_survives_follow_ons(owned)

    def test_an_untagged_signal_before_any_mutator_is_not_swallowed(self):
        hwp = make_hwp()
        owned = Owned(self, hwp)
        owned.arm('NoSuchAction')
        owned.service._find_text_on_expected_page = lambda *a, **k: (_ for _ in ()).throw(Terminate())
        result, error = owned.run(NUMBERING_STEP)
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertIsInstance(error, Terminate)
        self.assertFalse(owned.session._lifetime_edit_possible)
        owned.session._thread.join(.1)
        self.assertEqual(owned.hook, [])
        self.assertTrue(owned.session._thread.is_alive())
        self.assertFalse(owned.session._closed.is_set())
        self.assertEqual(owned.cleanup_events(), [])


class OrdinaryUnknownOutcomeTests(CustodyAssertions, unittest.TestCase):
    def _unknown_case(self, label, step, *, action='NoSuchAction', foreign=False, exploding=False, make_exc=None, ret=_UNSET, patches=(), protection=False, attempted=None):
        with self.subTest(case=label):
            hwp = make_hwp(foreign=foreign)
            owned = Owned(self, hwp, protection=census_mod.live_protection_reader(hwp) if protection else None)
            fault = owned.arm(action, make_exc=make_exc, exploding=exploding, ret=ret)
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                result, error = owned.run(step)
            self.assert_noncomplete_result(owned, result, error)
            if attempted is not None:
                self.assertEqual(fault.attempted, attempted)
            self.assert_retained(owned)
            self.assert_no_follow_on(owned, fault)
            self.assert_custody_survives_follow_ons(owned)

    def test_official_returned_unknown_outcomes_are_held(self):
        self._unknown_case('raw HParameterSet error after ParagraphShape', NUMBERING_STEP, action='ParagraphShape', exploding=True, attempted=['ParagraphShape'])
        self._unknown_case('ordinary Exception raised by ParagraphShape', NUMBERING_STEP, action='ParagraphShape', make_exc=lambda: RuntimeError('COM exploded'), attempted=['ParagraphShape'])
        self._unknown_case('definition-first raw error after OutlineNumber', NUMBERING_STEP, action='OutlineNumber', foreign=True, exploding=True, attempted=['OutlineNumber'])
        self._unknown_case(
            'raw preservation-delta error after both mutators', NUMBERING_STEP, foreign=True, attempted=['OutlineNumber', 'ParagraphShape'],
            patches=(patch.object(nn, '_official_delta_problems', side_effect=KeyError('digest')),),
        )

    def test_page_number_returned_unknown_outcomes_are_held(self):
        self._unknown_case('PageNumPos itself raised', PAGE_STEP, action='PageNumPos', make_exc=lambda: RuntimeError('COM exploded'), attempted=['PageNumPos'])
        self._unknown_case(
            'raw comparison error after PageNumPos', PAGE_STEP, protection=True, attempted=['PageNumPos'],
            patches=(patch.object(nn, '_page_number_delta_problems', side_effect=KeyError('digest')),),
        )
        self._unknown_case('PageNumPos returned None', PAGE_STEP, action='PageNumPos', ret=None, attempted=['PageNumPos'])


class KnownOutcomeControlTests(CustodyAssertions, unittest.TestCase):
    def _assert_open_and_usable(self, owned):
        s = owned.session
        self.assertTrue(s._thread.is_alive())
        self.assertFalse(s._quarantined)
        self.assertFalse(s.is_terminal())
        self.assertFalse(s.has_unreconciled_reconciliation())
        self.assertEqual(owned.cleanup_events(), [])
        self.assertEqual(owned.manager.execute(session_id=owned.sid, command_name='health_probe', handler=lambda h: {'ok': True}, timeout=5), {'ok': True})

    def test_successful_dirty_numbering_is_not_quarantined(self):
        for label, step in (('official', NUMBERING_STEP), ('page', PAGE_STEP)):
            with self.subTest(op=label):
                owned = Owned(self, make_hwp())
                fault = owned.arm('NoSuchAction')
                result, error = owned.run(step)
                self.assertIsNone(error)
                self.assertIs(result['ok'], True)
                self.assertIs(result['dirty'], True)
                self.assertEqual(len(fault.attempted), 1)
                self.assertTrue(owned.session._lifetime_edit_possible)
                self._assert_open_and_usable(owned)

    def test_clean_typed_refusal_is_not_dirty_and_not_quarantined(self):
        hwp = make_hwp(foreign=True)
        hwp.paragraphs[0]['heading_type'] = 1
        owned = Owned(self, hwp)
        fault = owned.arm('NoSuchAction')
        result, error = owned.run(NUMBERING_STEP)
        self.assertIsNone(error)
        self.assertIs(result['ok'], False)
        self.assertIs(result['dirty'], False)
        self.assertIs(result['steps'][0]['mutation_may_have_persisted'], False)
        self.assertEqual(fault.attempted, [])
        self.assertFalse(owned.session._lifetime_edit_possible)
        self._assert_open_and_usable(owned)

    def test_raw_exception_before_any_mutator_is_not_dirty_and_not_quarantined(self):
        for label, step in (('official', NUMBERING_STEP), ('page', PAGE_STEP)):
            with self.subTest(op=label):
                hwp = make_hwp()
                hwp.HParameterSet = Exploding()
                owned = Owned(self, hwp)
                fault = owned.arm('NoSuchAction')
                result, error = owned.run(step)
                self.assertIsNone(error)
                self.assertIs(result['ok'], False)
                self.assertIs(result['dirty'], False)
                self.assertIs(result['steps'][0]['mutation_may_have_persisted'], False)
                self.assertEqual(fault.attempted, [])
                self._assert_open_and_usable(owned)


class ReporterFailureCustodyTests(CustodyAssertions, unittest.TestCase):
    def _reporter_case(self, label, step, action, *, make_exc=None, exploding=False, foreign=False, fail='journal', protection=False):
        with self.subTest(case=label, fail=fail):
            hwp = make_hwp(foreign=foreign)
            owned = Owned(self, hwp, protection=census_mod.live_protection_reader(hwp) if protection else None)
            fault = owned.arm(action, make_exc=make_exc, exploding=exploding)
            reporting = LocalCliRuntimeError(f'injected {fail} failure')
            original_persist = owned.session._persist_command

            def failing_persist(command):
                if command.name == 'command-bundle' and command.state != 'queued':
                    raise reporting
                return original_persist(command)

            def failing_prune():
                raise reporting

            target = '_persist_command' if fail == 'journal' else '_prune_in_memory_commands'
            replacement = failing_persist if fail == 'journal' else failing_prune
            patcher = patch.object(owned.session, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
            result, error = owned.run(step)
            self.assertGreaterEqual(len(fault.attempted), 1)
            if fail == 'journal':
                self.assertIsNone(result, 'a reporter failure was presented as a completed result')
                self.assertIsInstance(error, LocalCliRuntimeError)
                self.assertNotIsInstance(error, LocalCliRuntimeTimeoutError)
                self.assertTrue(any(e is reporting for e in chain(error)), 'the reporting error is not preserved')
                if fault.injected:
                    self.assertTrue(any(e is fault.injected[0] for e in chain(error)), 'the original signal is not preserved')
            else:
                self.assertIsNone(error)
                self.assertIs(result['ok'], False)
                self.assertIs(result['dirty'], True)
            patcher.stop()
            s = owned.session
            self.assertTrue(s._thread.is_alive(), 'the owning STA died on a reporting error')
            self.assertEqual(owned.cleanup_events(), [])
            self.assertEqual(owned.hook, [])
            self.assertIs(owned.manager._sessions.get(owned.sid), s)
            self.assertTrue(s._quarantined)
            self.assertTrue(s.has_unreconciled_reconciliation())
            self.assertIs(owned.command().get('may_have_mutated'), True)
            self.assert_no_follow_on(owned, fault)
            record = s._commands_by_id[owned.command()['command_id']]
            if fault.injected:
                self.assertTrue(any(e is fault.injected[0] for e in chain(record.error)))
            self.assert_custody_survives_follow_ons(owned)

    def test_journal_failure_after_a_signal_keeps_the_held_sta(self):
        for exc_type in (KeyboardInterrupt, Terminate):
            self._reporter_case(f'official/{exc_type.__name__}', NUMBERING_STEP, 'ParagraphShape', make_exc=exc_type)
            self._reporter_case(f'page/{exc_type.__name__}', PAGE_STEP, 'PageNumPos', make_exc=exc_type)

    def test_journal_failure_after_an_ordinary_unknown_keeps_the_held_sta(self):
        self._reporter_case('official/raw', NUMBERING_STEP, 'ParagraphShape', exploding=True)
        self._reporter_case('page/raw', PAGE_STEP, 'PageNumPos', make_exc=lambda: RuntimeError('COM exploded'))

    def test_pruning_failure_after_an_unknown_keeps_the_held_sta(self):
        self._reporter_case('official/signal', NUMBERING_STEP, 'ParagraphShape', make_exc=KeyboardInterrupt, fail='prune')
        self._reporter_case('page/raw', PAGE_STEP, 'PageNumPos', make_exc=lambda: RuntimeError('COM exploded'), fail='prune')


class PageNumberBoundaryTests(unittest.TestCase):
    def _run_page(self, hwp, *, protection=None):
        return nn.apply_page_number(hwp, nn.validate_page_number_step(PAGE_STEP), protection=protection)

    def test_raw_comparison_error_becomes_a_typed_unknown_with_its_cause(self):
        hwp = make_hwp()
        hwp.cursor = (0, 2, 0)
        fault = Fault(hwp, 'NoSuchAction')
        reader = census_mod.live_protection_reader(hwp)
        raw = KeyError('digest')
        with patch.object(nn, '_page_number_delta_problems', side_effect=raw):
            with self.assertRaises(nn.NativeNumberingMutationError) as ctx:
                self._run_page(hwp, protection=reader)
        self.assertIs(ctx.exception.__cause__, raw)
        self.assertEqual(ctx.exception.mutation_state, 'unknown')
        self.assertIs(ctx.exception.mutation_may_have_persisted, True)
        self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
        self.assertEqual(fault.attempted, ['PageNumPos'])
        self.assertEqual(fault.after(), [])

    def test_signal_after_the_mutator_propagates_unchanged_and_tagged_without_caret_restore(self):
        for exc_type in SIGNALS:
            with self.subTest(exc=exc_type.__name__):
                hwp = make_hwp()
                hwp.cursor = (0, 2, 0)
                fault = Fault(hwp, 'PageNumPos', make_exc=exc_type)
                with self.assertRaises(BaseException) as ctx:
                    self._run_page(hwp)
                self.assertIs(ctx.exception, fault.injected[0])
                if type(ctx.exception) is Terminate:
                    self.assertEqual(vars(ctx.exception), {})
                else:
                    self.assertEqual(ctx.exception.mutation_state, 'unknown')
                if type(ctx.exception) is Terminate:
                    self.assertEqual(vars(ctx.exception), {})
                else:
                    self.assertIs(ctx.exception.mutation_may_have_persisted, True)
                if type(ctx.exception) is not Terminate:
                    self.assertEqual(ctx.exception.rollback, {'attempted': False, 'succeeded': False})
                self.assertEqual(fault.after(), [])

    def test_signal_inside_the_comparison_propagates_unchanged_and_tagged(self):
        for exc_type in SIGNALS:
            with self.subTest(exc=exc_type.__name__):
                hwp = make_hwp()
                hwp.cursor = (0, 2, 0)
                fault = Fault(hwp, 'NoSuchAction')
                signal = exc_type()
                with patch.object(nn, '_page_number_delta_problems', side_effect=signal):
                    with self.assertRaises(BaseException) as ctx:
                        self._run_page(hwp, protection=census_mod.live_protection_reader(hwp))
                self.assertIs(ctx.exception, signal)
                if type(ctx.exception) is Terminate:
                    self.assertEqual(vars(ctx.exception), {})
                else:
                    self.assertIs(ctx.exception.mutation_may_have_persisted, True)
                self.assertEqual(fault.after(), [])

    def test_signal_before_the_mutator_is_not_tagged(self):
        hwp = make_hwp()
        hwp.cursor = (0, 2, 0)
        signal = Terminate()

        def mover():
            raise signal

        hwp.MoveDocBegin = mover
        with self.assertRaises(Terminate) as ctx:
            self._run_page(hwp)
        self.assertIs(ctx.exception, signal)
        self.assertFalse(hasattr(signal, 'mutation_may_have_persisted'))
        self.assertEqual(executes(hwp), [])

    def test_success_and_pre_mutation_refusal_are_unchanged(self):
        hwp = make_hwp()
        hwp.cursor = (0, 2, 0)
        result = self._run_page(hwp, protection=census_mod.live_protection_reader(hwp))
        self.assertIs(result['changed'], True)
        self.assertEqual(result['status'], 'inserted')
        hwp = make_hwp()
        hwp.controls.append(type(hwp.controls[0])('foot'))
        with self.assertRaises(nn.NativeNumberingRefusal) as ctx:
            self._run_page(hwp)
        self.assertEqual(ctx.exception.mutation_state, 'none')
        self.assertEqual(executes(hwp), [])


class RealExecuteLiveHeldTests(CustodyAssertions, unittest.TestCase):
    def _held_service(self, *, fail_journal):
        hwp = make_hwp()
        owned = Owned(self, hwp, real_execute_live=True)
        fault = owned.arm('ParagraphShape', make_exc=KeyboardInterrupt)
        if fail_journal:
            original = owned.session._persist_command
            reporting = LocalCliRuntimeError('injected journal failure')

            def failing(command):
                if command.name == 'command-bundle' and command.state != 'queued':
                    raise reporting
                return original(command)

            patcher = patch.object(owned.session, '_persist_command', failing)
            patcher.start()
            self.addCleanup(patcher.stop)
        return owned, fault

    def test_custody_is_projected_into_the_binding_with_a_noncomplete_result(self):
        owned, fault = self._held_service(fail_journal=False)
        result, error = owned.run(NUMBERING_STEP)
        self.assertIsNone(error)
        self.assertIs(result['ok'], False)
        self.assertIs(result['dirty'], True)
        self.assertIs(result.get('native_custody_held'), True)
        self.assertTrue(any(b.get('working_copy_dirty') is True and b.get('native_custody_held') for b in owned.saved_bindings))
        self.assert_retained(owned)
        calls = list(owned.hwp.calls)
        result, error = owned.run(NUMBERING_STEP)
        self.assertIsNone(result)
        self.assertIsInstance(error, LocalCliServiceError)
        self.assertEqual(owned.hwp.calls, calls)
        self.assert_retained(owned)

    def test_journal_failure_is_a_service_error_that_still_records_dirty_custody(self):
        owned, fault = self._held_service(fail_journal=True)
        result, error = owned.run(NUMBERING_STEP)
        self.assertIsNone(result)
        self.assertIsInstance(error, LocalCliServiceError)
        self.assertTrue(any(b.get('working_copy_dirty') is True and b.get('native_custody_held') for b in owned.saved_bindings))
        s = owned.session
        self.assertTrue(s._thread.is_alive())
        self.assertEqual(owned.cleanup_events(), [])
        self.assertEqual(owned.hook, [])
        self.assertIs(owned.manager._sessions.get(owned.sid), s)
        self.assertTrue(s.has_unreconciled_reconciliation())


if __name__ == '__main__':
    unittest.main()
