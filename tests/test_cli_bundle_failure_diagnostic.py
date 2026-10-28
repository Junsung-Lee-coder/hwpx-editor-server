"""Synthetic SDK faults, real bundle/dispatch/STA/private journal/failure-finally.

No historical native cause, COM identity/HRESULT, render or closure proof.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import MethodType, SimpleNamespace as NS
from typing import Any, Callable, Literal, Optional
import threading
import unittest
from unittest.mock import patch

from app import atomic_json, native_numbering as nn
from app import local_cli_runtime as rt
from app import local_cli_service as svc
from app.command_packages.runtime import get_command_package_registry
from tests import test_cli_failure_recovery as recovery
from tests.test_native_numbering_bundle_translation import _doc, NUMBERING_STEP


class LockedFindWrapper:
    hwp: Any
    SetMessageBoxMode: Callable[[int], Any]
    FindDir: Callable[[str], int]
    PointToHwpUnit: Callable[[float], float]

    # Executable AST from pyhwpx 1.7.2 core.Hwp.find, docstring removed.
    # The external author receipt authenticates its exact wheel/body identity.
    def find(self, src: str='', direction: Literal['Forward', 'Backward', 'AllDoc']='Forward', regex: bool=False, TextColor: Optional[int]=None, Height: Optional[int | float]=None, FaceName: Optional[str]=None, MatchCase: int=1, SeveralWords: int=0, UseWildCards: int=0, WholeWordOnly: int=0, AutoSpell: int=1, HanjaFromHangul: int=1, AllWordForms: int=0, FindStyle: str='', ReplaceStyle: str='', FindJaso: int=0, FindType: int=1, **kwargs) -> bool:
        self.SetMessageBoxMode(196593)
        pset = self.hwp.HParameterSet.HFindReplace
        self.hwp.HAction.GetDefault('FindDlg', pset.HSet)
        self.hwp.HAction.Execute('FindDlg', pset.HSet)
        pset = self.hwp.HParameterSet.HFindReplace
        pset.MatchCase = MatchCase
        pset.SeveralWords = SeveralWords
        pset.UseWildCards = UseWildCards
        pset.WholeWordOnly = WholeWordOnly
        pset.AutoSpell = AutoSpell
        pset.Direction = self.FindDir(direction)
        pset.FindString = src
        pset.IgnoreMessage = 0
        pset.HanjaFromHangul = HanjaFromHangul
        pset.AllWordForms = AllWordForms
        pset.FindJaso = FindJaso
        pset.FindStyle = FindStyle
        pset.ReplaceStyle = ReplaceStyle
        pset.FindRegExp = regex
        pset.FindType = FindType
        if TextColor is not None:
            pset.FindCharShape.TextColor = TextColor
        if Height is not None:
            pset.FindCharShape.Height = self.PointToHwpUnit(Height)
        if FaceName is not None:
            pset.FindCharShape.FaceNameHangul = FaceName
        for key, value in kwargs.items():
            setattr(pset, key, value)
        try:
            return self.hwp.HAction.Execute('RepeatFind', pset.HSet)
        finally:
            self.SetMessageBoxMode(1048575)


class BundleFailureDiagnosticTests(unittest.TestCase):
    owned = recovery.RecoveryTests.owned

    def prepare(self):
        manager, session, wrapper, calls, wc = self.owned()
        hwp = _doc()
        hwp.IsModified = False
        hwp.save = lambda: self.fail('clean save must not call Save')
        session._active_handle.hwp = hwp  # Synthetic SDK adapter, not a native rebind.
        binding = {'session_id': 'recovery', 'source_filename': 'fixture.hwpx',
                   'command_generation': 0, 'native_command_sequence': 0}
        service = object.__new__(svc.LocalCliService)
        service.runtime_manager = manager
        service.command_packages = get_command_package_registry()
        service._require_ready_runtime = lambda *a: None
        service._load_active_binding = lambda **k: dict(binding)
        service._read_binding = lambda **k: dict(binding)
        service._save_binding = lambda b: b
        service._update_live_binding = lambda b, **k: b
        service._record_local_cli_command = lambda *a, **k: None
        service._working_copy_path = lambda b: wc
        service._verify_artifact_readback = lambda *a, **k: wc
        service._artifact_name = lambda **k: 'fixture.hwpx'
        service._artifact_download_path = lambda **k: 'fixture-download'
        service._bundle_page_evidence = lambda h: {'page': 1}
        service._bundle_compact_snapshot = lambda h: {}
        hwp_calls_at_hold = []
        hold = session._hold_native_custody
        def held(*args):
            hold(*args)
            hwp_calls_at_hold.append(list(hwp.calls))
        self.enterContext(patch.object(session, '_hold_native_custody', held))
        self.enterContext(patch.object(svc, 'snapshot_live_location', lambda **k: {}))
        return service, manager, session, hwp, calls, hwp_calls_at_hold

    def run_bundle(self, service, *, match='Item'):
        # Same public three-step shape as official-numbering-apply. The existing
        # where intent/dirty policy is intentionally neither corrected nor widened.
        return service.command_bundle(steps=[{'op': 'where'},
            {**NUMBERING_STEP, 'match': match}, {'op': 'where'}])

    def command(self, session):
        command = session._commands_by_id[session._last_command_id]
        self.assertTrue(command.completed_event.wait(1))
        return command

    def capture(self, payload):
        # Optional author evidence sink, outside the readonly source tree.
        if os.environ.get('HWPX_DIAGNOSTIC_TEST_RECORDS') == '/out/observed-records.jsonl':
            with Path('/out/observed-records.jsonl').open('a') as stream:
                stream.write(json.dumps({'test': self.id(), **payload}, sort_keys=True) + '\n')

    def diagnostic(self, session):
        command = self.command(session)
        row = session._command_record(command, include_private=True)
        self.capture({'private_command': row, 'public_command': session.command_status()})
        return row['reconciliation_data']['bundle_failure_diagnostic']

    def assert_held(self, manager, session, hwp, calls, at_hold):
        command = self.command(session)
        self.assertTrue(command.custody_held)
        self.assertTrue(command.may_have_mutated)
        self.assertFalse(command.semantic_ok)
        self.assertFalse(command.timed_out)
        self.assertTrue(session._thread.is_alive())
        self.assertEqual(calls, ['initialize'])
        self.assertTrue(at_hold)
        self.assertEqual(hwp.calls, at_hold[0], 'native calls occurred after UNKNOWN latch')
        reached = []
        with self.assertRaises(rt.LocalCliRuntimeError):
            manager.execute(session_id='recovery', command_name='where', handler=lambda h: reached.append(True))
        with self.assertRaises(rt.LocalCliRuntimeError):
            manager.close_session('recovery', timeout=.1)
        self.assertEqual(reached, [])
        self.assertEqual(hwp.calls, at_hold[0])
        self.assertEqual(calls, ['initialize'])

    def test_real_no_match_after_clean_save_has_private_fixed_class_not_a_cause_verdict(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.assertTrue(service.save()['ok'])
        result = self.run_bundle(service, match='N2NOMATCH')
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertEqual(len(result['steps']), 2)
        self.assertEqual(result['steps'][1]['error'], 'native command step failed')
        self.assertEqual(self.diagnostic(session), {'site': 'bundle_step',
            'causal_class': 'LocalCliRuntimeError', 'cause_class': 'UNKNOWN',
            'cause2_class': 'UNKNOWN', 'refusal_code': 'UNKNOWN'})
        self.assert_held(manager, session, hwp, calls, at_hold)
        journal = rt.command_journal_path(session.session_root)
        rows = json.loads(journal.read_text())['commands']
        self.capture({'private_journal': rows, 'public_bundle': result})
        self.assertEqual(rows[0]['save_diagnostic']['save_decision'], 'clean_noop')
        self.assertEqual(rows[-1]['reconciliation_data']['bundle_failure_diagnostic'], self.diagnostic(session))
        public = json.dumps([result, session.command_status(), rt.read_command_journal(session.session_root)])
        self.assertNotIn('bundle_failure_diagnostic', public)
        self.assertNotIn('N2NOMATCH', journal.read_text())
        self.assertFalse(any(c[0] == 'Execute' for c in hwp.calls))

    def test_locked_find_body_through_real_caller_distinguishes_false_typed_and_foreign_error(self):
        class ForeignSdkError(Exception):
            pass
        for fault, label in ((None, 'LocalCliRuntimeError'),
                             (RuntimeError('PRIVATE_SDK'), 'RuntimeError'),
                             (ForeignSdkError('PRIVATE_SDK'), 'UNKNOWN')):
            with self.subTest(expected=label):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                def execute(name, pset):
                    hwp.calls.append(('locked_sdk_execute', name))
                    if name == 'RepeatFind' and fault is not None:
                        raise fault
                    return name != 'RepeatFind'
                sdk_action = NS(Execute=execute, GetDefault=lambda *a: hwp.calls.append(('locked_sdk_defaults',)))
                hwp.hwp = NS(HAction=sdk_action, HParameterSet=NS(HFindReplace=NS(HSet=object())))
                hwp.SetMessageBoxMode = lambda mode: hwp.calls.append(('locked_message_mode', mode))
                hwp.FindDir = lambda direction: 0
                hwp.find = MethodType(LockedFindWrapper.find, hwp)
                result = self.run_bundle(service, match='N2NOMATCH')
                self.assertFalse(result['ok'])
                self.assertEqual(self.diagnostic(session)['causal_class'], label)
                self.assertEqual(sum(c == ('locked_sdk_execute', 'RepeatFind') for c in hwp.calls), 1)
                self.assertIn(('locked_message_mode', 1048575), hwp.calls)
                self.assert_held(manager, session, hwp, calls, at_hold)
                self.capture({'sdk_calls': hwp.calls, 'public_bundle': result})
                self.assertNotIn('PRIVATE_SDK', rt.command_journal_path(session.session_root).read_text())

    def test_real_translated_refusal_and_native_execute_error_keep_bounded_chain(self):
        for mode in ('refusal', 'execute'):
            with self.subTest(mode=mode):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                # Synthetic page-target adapter only: inner native numbering and
                # its error translation remain real, as does the owning runtime.
                service._find_text_on_expected_page = lambda *a, **k: {'page': 1}
                if mode == 'refusal':
                    hwp.outline_unreadable = True
                else:
                    hwp.HAction.execute_raises = RuntimeError('PRIVATE_NATIVE')
                result = self.run_bundle(service)
                diagnostic = self.diagnostic(session)
                self.assertFalse(result['ok'])
                self.assertEqual(diagnostic['site'], 'bundle_step')
                self.assertEqual(diagnostic['causal_class'], 'LocalCliRuntimeError' if mode == 'refusal' else 'LocalCliMutationError')
                self.assertEqual(diagnostic['cause_class'], 'NativeNumberingRefusal' if mode == 'refusal' else 'NativeNumberingMutationError')
                self.assertEqual(diagnostic['cause2_class'], 'UNKNOWN' if mode == 'refusal' else 'RuntimeError')
                self.assertEqual(diagnostic['refusal_code'], 'outline_definition_unreadable' if mode == 'refusal' else 'UNKNOWN')
                self.assert_held(manager, session, hwp, calls, at_hold)
                self.assertNotIn('PRIVATE_NATIVE', rt.command_journal_path(session.session_root).read_text())

    def test_foreign_exception_descriptors_repr_metaclass_and_cause_are_not_called(self):
        callbacks = []
        class Meta(type):
            @property
            def __name__(cls):
                callbacks.append('name')
                raise AssertionError('PRIVATE')
        class Foreign(Exception, metaclass=Meta):
            def __getattribute__(self, key):
                if key in ('__cause__', '__dict__', 'code'):
                    callbacks.append(key)
                    raise AssertionError('PRIVATE')
                return super().__getattribute__(key)
            def __str__(self):
                callbacks.append('str')
                raise AssertionError('PRIVATE')
            def __repr__(self):
                callbacks.append('repr')
                raise AssertionError('PRIVATE')
        for fault in (Foreign(), KeyboardInterrupt('PRIVATE'), SystemExit('PRIVATE')):
            with self.subTest(kind=type(fault) is Foreign):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                fault.__cause__ = ValueError('PRIVATE')
                def fail(*a, **k):
                    raise fault
                hwp.find = fail
                result = self.run_bundle(service)
                diagnostic = self.diagnostic(session)
                self.assertFalse(result['ok'])
                self.assertEqual(diagnostic['causal_class'], 'UNKNOWN' if type(fault) is Foreign else ('KeyboardInterrupt' if type(fault) is KeyboardInterrupt else 'SystemExit'))
                self.assertEqual(diagnostic['cause_class'], 'UNKNOWN' if type(fault) is Foreign else 'ValueError')
                self.assert_held(manager, session, hwp, calls, at_hold)
                self.assertNotIn('PRIVATE', rt.command_journal_path(session.session_root).read_text())
        self.assertEqual(callbacks, [])

    def test_exact_refusal_code_projection_never_compares_foreign_keys_or_values(self):
        callbacks = []
        class Key:
            def __hash__(self):
                return hash('code')
            def __eq__(self, other):
                callbacks.append('key_eq')
                return False
            def __str__(self):
                callbacks.append('key_str')
                return 'PRIVATE_KEY'
        class Code(str):
            def __eq__(self, other):
                callbacks.append('code_eq')
                return True
        for key_kind in ('collision', 'noncollision'):
            for value in (None, 'target_not_found', Code('PRIVATE_CODE'), 'PRIVATE' * 10000, 123):
                with self.subTest(key_kind=key_kind, canonical=type(value) is str and len(value) < 100):
                    service, manager, session, hwp, calls, at_hold = self.prepare()
                    fault = nn.NativeNumberingRefusal('PRIVATE_MESSAGE', code='target_not_found')
                    data = BaseException.__dict__['__dict__'].__get__(fault)
                    data.clear()
                    data[Key() if key_kind == 'collision' else object()] = 'PRIVATE_KEY_VALUE'
                    if value is not None:
                        data['code'] = value
                    callbacks.clear()  # Construction may compare colliding keys; observation must not.
                    service._find_text_on_expected_page = lambda *a, **k: {'page': 1}
                    with patch.object(nn, 'apply_official_numbering_at_match', side_effect=fault):
                        result = self.run_bundle(service)
                    diagnostic = self.diagnostic(session)
                    self.assertEqual(diagnostic['cause_class'], 'NativeNumberingRefusal')
                    self.assertEqual(diagnostic['refusal_code'], 'target_not_found' if type(value) is str and value == 'target_not_found' else 'UNKNOWN')
                    self.assertFalse(result['ok'])
                    self.assertEqual(callbacks, [])
                    self.assert_held(manager, session, hwp, calls, at_hold)
                    self.assertNotIn('PRIVATE', rt.command_journal_path(session.session_root).read_text())

    def test_journal_read_write_and_all_reporting_failures_do_not_erase_memory_or_release(self):
        for fault in ('read', 'write', 'all_reports'):
            with self.subTest(fault=fault):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                journal = rt.command_journal_path(session.session_root)
                original_open, original_replace = type(journal).open, atomic_json.os.replace
                persist = session._persist_command
                triggered = []
                def eligible():
                    return session._active_command is not None and session._active_command.custody_held and not triggered
                def open_file(path, *a, **k):
                    if fault == 'read' and path == journal and a and a[0] == 'rb' and eligible():
                        triggered.append(fault)
                        raise OSError('PRIVATE_READ')
                    return original_open(path, *a, **k)
                def replace(src, dst):
                    if fault == 'write' and dst == journal and eligible():
                        triggered.append(fault)
                        raise OSError('PRIVATE_WRITE')
                    return original_replace(src, dst)
                final_report_done = threading.Event()
                def report(command):
                    try:
                        if fault == 'all_reports' and command.custody_held:
                            triggered.append(fault)
                            raise OSError('PRIVATE_REPORT')
                        return persist(command)
                    finally:
                        # completed_event precedes the final persistence call;
                        # it alone is not evidence that that writer returned.
                        if command.completed_event.is_set():
                            final_report_done.set()
                with patch.object(type(journal), 'open', open_file), patch.object(atomic_json.os, 'replace', replace), patch.object(session, '_persist_command', report):
                    with self.assertRaises(svc.LocalCliServiceError):
                        self.run_bundle(service, match='N2NOMATCH')
                    self.assertTrue(final_report_done.wait(1))
                    command = self.command(session)
                    self.assertEqual(self.diagnostic(session)['causal_class'], 'LocalCliRuntimeError')
                    retained = json.loads(journal.read_text())['commands'][-1]
                    self.assertEqual('bundle_failure_diagnostic' in retained.get('reconciliation_data', {}), fault != 'all_reports')
                    self.assertTrue(command.error.reporting_errors)
                    self.assertNotIn('PRIVATE', journal.read_text())
                self.assertTrue(triggered)
                self.assert_held(manager, session, hwp, calls, at_hold)

    def test_trusted_exception_dictionary_subclass_and_oversize_remain_unknown_without_callbacks(self):
        callbacks = []
        class ForeignDict(dict):
            def __len__(self):
                callbacks.append('len')
                raise AssertionError('PRIVATE_DICT')
            def items(self):
                callbacks.append('items')
                raise AssertionError('PRIVATE_DICT')
        for data in (ForeignDict(code='target_not_found'),
                     {'code': 'target_not_found', **{str(n): 'PRIVATE' for n in range(16)}}):
            with self.subTest(foreign=type(data) is ForeignDict):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                fault = nn.NativeNumberingRefusal('PRIVATE', code='target_not_found')
                fault.__dict__ = data
                service._find_text_on_expected_page = lambda *a, **k: {'page': 1}
                with patch.object(nn, 'apply_official_numbering_at_match', side_effect=fault):
                    result = self.run_bundle(service)
                self.assertFalse(result['ok'])
                self.assertEqual(self.diagnostic(session)['refusal_code'], 'UNKNOWN')
                self.assertEqual(callbacks, [])
                self.assert_held(manager, session, hwp, calls, at_hold)
                self.assertNotIn('PRIVATE', rt.command_journal_path(session.session_root).read_text())

    def test_explicit_cause_cycles_are_bounded_not_traversed_as_a_root_verdict(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        fault = ValueError('PRIVATE')
        fault.__cause__ = fault
        def fail(*a, **k):
            raise fault
        hwp.find = fail
        self.assertFalse(self.run_bundle(service)['ok'])
        self.assertEqual(self.diagnostic(session), {'site': 'bundle_step',
            'causal_class': 'ValueError', 'cause_class': 'ValueError',
            'cause2_class': 'ValueError', 'refusal_code': 'UNKNOWN'})
        self.assert_held(manager, session, hwp, calls, at_hold)

    def test_success_and_nonbundle_error_do_not_gain_a_private_bundle_diagnostic(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        service._find_text_on_expected_page = lambda *a, **k: {'page': 1}
        result = self.run_bundle(service)
        self.assertTrue(result['ok'])
        command = self.command(session)
        self.assertNotIn('bundle_failure_diagnostic', command.reconciliation_data)
        self.assertFalse(session._native_custody_held)
        self.assertEqual(at_hold, [])
        def fail(handle):
            raise ValueError('PRIVATE')
        with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
            manager.execute(session_id='recovery', command_name='action', handler=fail)
        command = self.command(session)
        self.assertNotIn('bundle_failure_diagnostic', command.reconciliation_data)
        self.assertEqual(session.command_status()['native_diagnostic']['causal_class'], 'ValueError')


if __name__ == '__main__':
    unittest.main()
