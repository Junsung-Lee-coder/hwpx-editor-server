"""Real service/bundle/owning STA/serializer, synthetic locked SDK boundary.

Forward source defect evidence only; the historical LocalCliRuntimeError site
and native cause remain UNKNOWN. Helpers/locked find AST are attributed reuse
from tests.test_cli_bundle_failure_diagnostic, not native acceptance.
"""
from types import MethodType, SimpleNamespace as NS
import copy
import json
import unittest
from unittest.mock import patch

from app import atomic_json, local_cli_runtime as rt, native_numbering as nn
from app import local_cli_service as svc
from tests import test_cli_bundle_failure_diagnostic as prior
from tests import test_cli_failure_recovery as recovery


class NumberingTargetGuardTests(unittest.TestCase):
    owned = recovery.RecoveryTests.owned
    prepare = prior.BundleFailureDiagnosticTests.prepare
    run_bundle = prior.BundleFailureDiagnosticTests.run_bundle
    command = prior.BundleFailureDiagnosticTests.command
    assert_held = prior.BundleFailureDiagnosticTests.assert_held

    def locked(self, hwp, result=False, fault=None):
        original_find = hwp.find
        def execute(name, pset):
            hwp.calls.append(('locked_execute', name))
            if name == 'RepeatFind':
                if fault is not None:
                    raise fault
                return original_find(pset.FindString) if result == 'body' else result
            return True
        pset = NS()
        pset.HSet = pset
        hwp.hwp = NS(HAction=NS(Execute=execute, GetDefault=lambda *a: True),
                     HParameterSet=NS(HFindReplace=pset))
        hwp.SetMessageBoxMode = lambda n: hwp.calls.append(('locked_mode', n))
        hwp.FindDir = lambda d: 0
        hwp.find = MethodType(prior.LockedFindWrapper.find, hwp)

    def diagnostic(self, session):
        command = self.command(session)
        row = session._command_record(command, include_private=True)
        journal = json.loads(rt.command_journal_path(session.session_root).read_text())['commands'][-1]
        diagnostic = row['reconciliation_data']['bundle_failure_diagnostic']
        self.assertEqual(journal['reconciliation_data']['bundle_failure_diagnostic'], diagnostic)
        public = json.dumps([session.command_status(), rt.read_command_journal(session.session_root)])
        self.assertNotIn('numbering_phase', public)
        self.assertNotIn('PRIVATE', json.dumps(row))
        return diagnostic

    def test_exact_false_after_clean_save_is_a_failed_nonmutating_guard_not_success(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.assertTrue(service.save()['ok'])
        self.locked(hwp)
        hwp.cursor = (0, 2, 2)
        before = copy.deepcopy(hwp.paragraphs)
        result = self.run_bundle(service, match='N2NOMATCH')
        self.assertFalse(result['ok'])
        self.assertFalse(result['dirty'])
        self.assertEqual(len(result['steps']), 2)
        self.assertFalse(result['steps'][1]['mutation_may_have_persisted'])
        self.assertEqual(result['steps'][1]['refusal_code'], 'target_not_found')
        self.assertEqual(hwp.cursor, (0, 2, 2))
        self.assertEqual(hwp.paragraphs, before)
        self.assertEqual(sum(c == ('locked_execute', 'RepeatFind') for c in hwp.calls), 1)
        self.assertFalse(any(c[0] == 'Execute' for c in hwp.calls))
        self.assertFalse(session._native_custody_held)
        self.assertFalse(session._normal_close_requires_save)
        self.assertFalse(self.command(session).may_have_mutated)
        self.assertEqual(at_hold, [])
        self.assertEqual(calls, ['initialize'])

    def test_original_selection_is_restored_and_proved(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.locked(hwp)
        hwp.selection_para = 2
        hwp.cursor = (0, 2, 0)
        original = (hwp.cursor, hwp.get_selected_pos())
        result = self.run_bundle(service, match='N2NOMATCH')
        self.assertFalse(result['ok'])
        self.assertFalse(result['dirty'])
        self.assertEqual((hwp.cursor, hwp.get_selected_pos()), original)
        self.assertFalse(session._native_custody_held)

    def test_sdk_exceptions_and_nonexact_find_results_remain_held_without_retry(self):
        class Foreign(Exception):
            def __str__(self):
                raise AssertionError('PRIVATE_CALLBACK')
        for value, fault in ((None, None), (0, None), (1, None),
                             (False, RuntimeError('PRIVATE')), (False, Foreign())):
            with self.subTest(value=value, foreign=type(fault) is Foreign):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                self.locked(hwp, value, fault)
                result = self.run_bundle(service, match='N2NOMATCH')
                self.assertFalse(result['ok'])
                self.assert_held(manager, session, hwp, calls, at_hold)
                d = self.diagnostic(session)
                self.assertEqual(d['numbering_phase'], 'pre_target')
                self.assertEqual(d['numbering_reason'], 'finder_failed')
                self.assertEqual(sum(c == ('locked_execute', 'RepeatFind') for c in hwp.calls), 1)

    def test_restore_failure_is_held_and_no_postfailure_sdk_is_issued(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.locked(hwp)
        hwp.fail_set_pos = True
        self.assertFalse(self.run_bundle(service, match='N2NOMATCH')['ok'])
        self.assert_held(manager, session, hwp, calls, at_hold)
        self.assertEqual(self.diagnostic(session)['numbering_phase'], 'restore')

    def test_changed_preservation_is_not_a_clean_guard(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.locked(hwp)
        original = hwp.hwp.HAction.Execute
        def execute(name, pset):
            value = original(name, pset)
            if name == 'RepeatFind':
                hwp.paragraphs[0]['text'] = 'PRIVATE_CHANGED'
            return value
        hwp.hwp.HAction.Execute = execute
        self.assertFalse(self.run_bundle(service, match='N2NOMATCH')['ok'])
        self.assert_held(manager, session, hwp, calls, at_hold)
        d = self.diagnostic(session)
        self.assertEqual(d['numbering_phase'], 'preservation')
        self.assertEqual(d['numbering_reason'], 'preservation_unproven')

    def test_missing_protection_stops_before_finder(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.locked(hwp)
        service._numbering_protection_reader = lambda h: lambda: {}
        self.assertFalse(self.run_bundle(service, match='N2NOMATCH')['ok'])
        self.assert_held(manager, session, hwp, calls, at_hold)
        self.assertEqual(self.diagnostic(session)['numbering_phase'], 'preflight')
        self.assertFalse(any(c == ('locked_execute', 'RepeatFind') for c in hwp.calls))

    def test_native_mutator_failure_is_discriminated_from_pre_target(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.locked(hwp, 'body')
        hwp.HAction.execute_result = False
        self.assertFalse(self.run_bundle(service)['ok'])
        self.assert_held(manager, session, hwp, calls, at_hold)
        d = self.diagnostic(session)
        self.assertEqual(d['numbering_phase'], 'native')
        self.assertEqual(d['cause_class'], 'NativeNumberingMutationError')
        self.assertEqual(sum(c[0] == 'Execute' for c in hwp.calls), 1)

    def test_generic_local_error_is_not_mapped_to_target_not_found(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        service._find_text_on_expected_page = lambda *a, **k: (_ for _ in ()).throw(rt.LocalCliRuntimeError('PRIVATE'))
        self.assertFalse(self.run_bundle(service)['ok'])
        self.assert_held(manager, session, hwp, calls, at_hold)
        self.assertEqual(self.diagnostic(session)['numbering_reason'], 'finder_failed')

    def test_prior_unsaved_debt_survives_a_clean_guard_and_clean_noop_save(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        manager.execute(session_id='recovery', command_name='action', handler=lambda h: {'ok': True, 'dirty': True})
        binding = service._load_active_binding()
        binding['working_copy_dirty'] = True
        service._load_active_binding = lambda **k: dict(binding)
        service._read_binding = lambda **k: dict(binding)
        self.locked(hwp)
        result = self.run_bundle(service, match='N2NOMATCH')
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertFalse(self.command(session).may_have_mutated)
        self.assertTrue(session._normal_close_requires_save)
        self.assertTrue(service.save()['ok'])
        self.assertTrue(session._normal_close_requires_save)
        with self.assertRaises(rt.LocalCliRuntimeError):
            manager.close_session('recovery', timeout=.1)
        self.assertEqual(calls, ['initialize'])

    def test_reporting_failure_does_not_authorize_release(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.locked(hwp)
        persist = session._persist_command
        def fail(command):
            if command.name == 'command-bundle' and command.result is not None:
                raise OSError('PRIVATE_REPORT')
            return persist(command)
        with patch.object(session, '_persist_command', fail):
            with self.assertRaises(svc.LocalCliServiceError):
                self.run_bundle(service, match='N2NOMATCH')
            self.assert_held(manager, session, hwp, calls, at_hold)

    def test_protection_and_nonexact_native_read_failure_issue_no_restore_or_later_sdk(self):
        for mode in ('protection', 'nonexact_defaults', 'false_defaults'):
            with self.subTest(mode=mode):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                self.locked(hwp, 'body')
                mark = []
                reads = []
                if mode == 'protection':
                    reader = service._numbering_protection_reader(session._active_handle)
                    def protection():
                        reads.append(True)
                        if len(reads) == 2:
                            mark[:] = [len(hwp.calls)]
                            raise OSError('PRIVATE_PROTECTION_READ')
                        return reader()
                    service._numbering_protection_reader = lambda h: protection
                else:
                    defaults = hwp.HAction.GetDefault
                    def get_default(name, hset):
                        defaults(name, hset)
                        mark[:] = [len(hwp.calls)]
                        return None if mode == 'nonexact_defaults' else False
                    hwp.HAction.GetDefault = get_default
                result = self.run_bundle(service)
                self.assertFalse(result['ok'])
                self.assertTrue(mark, 'the intended technical precondition failure was not reached')
                self.assert_held(manager, session, hwp, calls, at_hold)
                self.assertEqual(hwp.calls[mark[0]:], [], 'technical failure must not be followed by restore or readback')
                if mode == 'protection':
                    self.assertEqual(len(reads), 2, 'a failed preservation getter must not be retried')
                self.assertEqual(self.diagnostic(session)['numbering_phase'], 'native')

    def test_on_mutating_callback_failure_retains_custody_before_any_execute(self):
        service, manager, session, hwp, calls, at_hold = self.prepare()
        self.locked(hwp, 'body')
        mark = session._active_handle.mark_mutating_intent
        def failed_mark():
            mark()
            raise ValueError('PRIVATE_CALLBACK')
        session._active_handle.mark_mutating_intent = failed_mark
        self.assertFalse(self.run_bundle(service)['ok'])
        self.assert_held(manager, session, hwp, calls, at_hold)
        self.assertFalse(any(c[0] == 'Execute' for c in hwp.calls))
        self.assertEqual(self.diagnostic(session)['numbering_phase'], 'native')

    def test_unproven_page_and_failed_preservation_read_never_become_absence(self):
        for mode in ('page', 'post_census'):
            with self.subTest(mode=mode):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                if mode == 'page':
                    self.locked(hwp, 'body')
                    service._bundle_page_evidence = lambda h: {'page': None}
                else:
                    self.locked(hwp)
                    original = service._numbering_protection_reader(session._active_handle)
                    count = 0
                    def protection():
                        nonlocal count
                        count += 1
                        if count == 2:
                            raise OSError('PRIVATE_CENSUS')
                        return original()
                    service._numbering_protection_reader = lambda h: protection
                self.assertFalse(self.run_bundle(service, match='Item' if mode == 'page' else 'N2NOMATCH')['ok'])
                self.assert_held(manager, session, hwp, calls, at_hold)
                self.assertFalse(any(c[0] == 'Execute' for c in hwp.calls))
                self.assertEqual(self.diagnostic(session)['numbering_phase'], 'pre_target' if mode == 'page' else 'preservation')

    def test_optional_fixed_phase_projection_rejects_callback_metadata(self):
        callbacks = []
        class ForeignDict(dict):
            def __len__(self):
                callbacks.append('len')
                raise AssertionError('PRIVATE')
            def items(self):
                callbacks.append('items')
                raise AssertionError('PRIVATE')
        class Key:
            def __hash__(self):
                return hash('numbering_phase')
            def __eq__(self, other):
                callbacks.append('key_eq')
                return False
        class Text(str):
            def __eq__(self, other):
                callbacks.append('value_eq')
                return True
        rows = [({}, False), (ForeignDict(numbering_phase='native', numbering_reason='native_failed'), False),
                ({'numbering_phase': Text('PRIVATE'), 'numbering_reason': 'native_failed'}, False),
                ({'numbering_phase': 'native', 'numbering_reason': 'PRIVATE' * 10000}, False),
                ({'numbering_phase': 'native', 'numbering_reason': 'native_failed'}, True)]
        for canonical in (False, True):
            data = {Key(): 'PRIVATE'}
            if canonical:
                data.update(numbering_phase='native', numbering_reason='native_failed')
            rows.append((data, canonical))
        for data, eligible in rows:
            with self.subTest(eligible=eligible):
                service, manager, session, hwp, calls, at_hold = self.prepare()
                fault = rt.LocalCliRuntimeError('PRIVATE')
                fault.__dict__ = data
                def fail(*a, **k):
                    raise fault
                service._bundle_official_numbering_apply = fail
                callbacks.clear()
                self.assertFalse(self.run_bundle(service)['ok'])
                d = self.diagnostic(session)
                self.assertEqual('numbering_phase' in d, eligible)
                self.assertEqual('numbering_reason' in d, eligible)
                if eligible:
                    self.assertEqual((d['numbering_phase'], d['numbering_reason']), ('native', 'native_failed'))
                self.assertEqual(callbacks, [])
                self.assert_held(manager, session, hwp, calls, at_hold)


if __name__ == '__main__':
    unittest.main()
