"""R1 pre-Delete preparation gap. Explicit external SDK faults, not native runs.
The fake never infers/validates ownership for Delete: only real source guards
can make owner/census drift produce zero destructive calls.
"""
import unittest
from unittest.mock import patch
from app import native_text_containers as nt
from tests.test_native_container_identity import setup, NATIVE
from tests.native_text_containers_fakes import Ctrl
from tests.native_census_state_review_models import Control, link
from tests.test_empty_ctrlid_product import Empty

def scenario(case, mode):
    own, _, request = setup(case, session_id='predelete-' + mode.replace('_', '-') + '-0001')
    h = own.hwp
    raw, action = h.hwp, h.HAction
    original_run = action.Run
    drift, first, before_delete = [], [], []
    events = []
    original_failure = nt._Native._failure

    def fire(stage):
        if mode in (stage + '_owner', stage + '_census', stage + '_range'):
            drift.append(stage)
            h.calls.append(('SDK_FAULT', stage))
            if mode.endswith('_range'):
                h.selection = (2, 0, 0, 2, 0, len(request['expected_text']) - 1)

    class PreparedAction:
        def __getattr__(self, name):
            return getattr(action, name)

        @property
        def Run(self):
            h.calls.append(('SDK_GET', 'Run'))
            fire('run')
            if mode == 'run_none':
                return None
            if mode == 'run_integer':
                return 1
            if mode == 'run_noncallable':
                return object()
            if mode == 'run_raised_refusal':
                raise nt.nn.NativeNumberingRefusal('synthetic SDK getter, not semantic authority', code='container_text_mismatch')

            def invoke(name):
                h.calls.append(('BOUND_CALL', name))
                if name == 'Delete':
                    before_delete.append(list(h.calls))
                return original_run(name)
            return invoke

    class Boundary:
        def __getattr__(self, name):
            return getattr(raw, name)

        @property
        def HAction(self):
            h.calls.append(('SDK_GET', 'HAction'))
            if h.selection:
                fire('action')
                if mode == 'action_none':
                    return None
                if mode == 'action_integer':
                    return 1
                if mode == 'action_raised_refusal':
                    raise nt.nn.NativeNumberingRefusal('synthetic SDK getter, not semantic authority', code='container_text_mismatch')
            return PreparedAction()

        @property
        def ParentCtrl(self):
            h.calls.append(('SDK_GET', 'ParentCtrl'))
            if drift and mode.endswith('_owner'):
                return Ctrl('gso', '900000002')
            return raw.ParentCtrl

        @property
        def HeadCtrl(self):
            h.calls.append(('SDK_GET', 'HeadCtrl'))
            ident = '900000002' if drift and mode.endswith('_census') else NATIVE
            return link([Empty(events), Control('gso', (0, 1, 4), ident, events)])

    def failure(native, message, site, returned):
        first.append((len(h.calls), native.failed, site))
        return original_failure(native, message, site, returned)

    h.hwp = Boundary()
    with patch.object(nt._Native, '_failure', failure):
        reply, error = own.run(request)
    case.assertIsNone(error)
    case.assertIsNotNone(reply)
    return own, h, reply, drift, first, before_delete

class PreDeleteIdentity(unittest.TestCase):
    def refusal(self, mode):
        own, h, reply, drift, first, _ = scenario(self, mode)
        self.assertTrue(drift, 'external fault must actually fire')
        self.assertFalse(reply['ok'])
        self.assertTrue(own.session._native_custody_held)
        self.assertEqual(sum(c[:2] == ('Run', 'Delete') for c in h.calls), 0)
        self.assertEqual(sum(c[:2] == ('Execute', 'InsertText') for c in h.calls), 0)
        self.assertTrue(first)
        self.assertTrue(first[0][1], 'first technical/identity fault latched before reporting')
        self.assertEqual(len(h.calls), first[0][0], 'no SDK after first fault')
        self.assertEqual(own.cleanup_events(), [])

    def test_owner_drift_at_HAction_acquisition_refuses_before_Delete(self):
        self.refusal('action_owner')

    def test_census_drift_at_HAction_acquisition_refuses_before_Delete(self):
        self.refusal('action_census')

    def test_prepared_Run_getter_drift_and_exact_range_drift_refuse(self):
        for mode in ('run_owner', 'run_census', 'action_range', 'run_range'):
            with self.subTest(mode=mode):
                self.refusal(mode)

    def test_no_drift_positive_uses_prepared_method_after_independent_proof(self):
        own, h, reply, drift, first, boundaries = scenario(self, 'positive')
        self.assertTrue(reply['ok'])
        self.assertFalse(own.session._native_custody_held)
        self.assertTrue(reply['dirty'])
        self.assertEqual(sum(c[:2] == ('Run', 'Delete') for c in h.calls), 1)
        self.assertEqual(sum(c[:2] == ('Execute', 'InsertText') for c in h.calls), 1)
        self.assertFalse(drift)
        self.assertFalse(first)
        self.assertEqual(len(boundaries), 1)
        trace = boundaries[0]
        run_get = max(i for i, c in enumerate(trace) if c == ('SDK_GET', 'Run'))
        owner = max(i for i, c in enumerate(trace) if c == ('SDK_GET', 'ParentCtrl'))
        census = max(i for i, c in enumerate(trace) if c == ('SDK_GET', 'HeadCtrl'))
        self.assertLess(run_get, owner)
        self.assertLess(run_get, census)
        self.assertFalse(any(c in (('SDK_GET', 'HAction'), ('SDK_GET', 'Run')) for c in trace[max(owner, census) + 1:]))

    def test_invalid_preparation_getters_latch_technical_failure_no_next_SDK(self):
        modes = ('action_none', 'action_integer', 'action_raised_refusal',
                 'run_none', 'run_integer', 'run_noncallable', 'run_raised_refusal')
        for mode in modes:
            with self.subTest(mode=mode):
                own, h, reply, _, first, _ = scenario(self, mode)
                self.assertFalse(reply['ok'])
                self.assertTrue(own.session._native_custody_held)
                self.assertTrue(first)
                self.assertTrue(first[0][1])
                self.assertEqual(len(h.calls), first[0][0])
                self.assertFalse(any(c[:2] == ('Run', 'Delete') for c in h.calls))
                self.assertFalse(any(c[:2] == ('Execute', 'InsertText') for c in h.calls))
                diagnostic = reply['steps'][0]['failure_diagnostic']
                self.assertIn(diagnostic['causal_class'], ('NativeNumberingReadFailure', 'NativeNumberingMutationError'))
                self.assertEqual(own.cleanup_events(), [])
