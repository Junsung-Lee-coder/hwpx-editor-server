"""Operation-specific SDK-origin witnesses through intact public consumers.

Controlled SDK fakes only. Healthy traces qualify each lookup/call before fault
injection; the normalizer, snapshots, packages and owning worker stay real.
"""
import inspect
import unittest

from app import local_cli_runtime as rt, native_numbering as nn
from app import native_paragraph_edit as pe
from app.local_cli_service import LocalCliServiceError
from tests import test_required_sdk_read_origin as original
from tests.test_required_sdk_read_origin import SDK, READS, fake_finder
from tests.test_bundle_step_local_refusal import owner_for, style
from tests.test_guarded_bundle_observations import run
from tests.test_native_paragraph_edit import _doc
from tests.native_table_fakes import sample_document


class Probe:
    def __init__(self, hwp, *, fault=None, cause=None):
        self.hwp, self.fault, self.cause = hwp, fault, cause
        self.page_name = self.malformed = None
        self.events, self.after = [], []
        self.fired, self.call_mark = False, None
        self.root = SDK(self, hwp)

    def event(self, kind, name):
        if self.fired:
            self.after.append((kind, name))
        frame = inspect.currentframe().f_back
        active, preflight, census = False, False, False
        try:
            while frame:
                if frame.f_code.co_name == '_preflight_census':
                    preflight = True
                if frame.f_code.co_name == '_read_census':
                    census = True
                step = frame.f_locals.get('step')
                if type(step) is dict and step.get('label') == 'residual':
                    active = True
                if frame.f_code.co_name == 'numbering_readback':
                    active = True
                frame = frame.f_back
        finally:
            del frame
        if active and name in READS | {'select_text', 'set_pos'}:
            self.events.append((kind, name, preflight, census))
            if len(self.events) == self.fault:
                self.fired, self.call_mark = True, len(self.hwp.calls)
                raise self.cause
        return False


def fixture(which):
    if which == 'create':
        hwp = sample_document()
        # This create fixture is deliberately single-page; TableHwp's body
        # SetPos does not infer a page after a multi-page table census.
        hwp.pages = {}
        step = dict(op='table_create_exact', match='본문 첫 문단', expected_page=1,
                    rows=1, cols=2, cells=[['새 가', '새 나']], confirm_layout=True)
        prefix = style(hwp, '1011')
    else:
        hwp = _doc('Title', 'Item', 'After')
        fake_finder(hwp)
        prefix = dict(op='paragraph_insert_exact', match='After', expected_page=1,
                      text='Prefix retained', position='after', confirm_layout=True)
        if which == 'remove':
            step = dict(op='paragraph_remove_exact', match='Item', expected_page=1,
                        expected_text='Item', confirm_remove=True)
        elif which == 'page':
            step = dict(op='page_number_apply', position='bottom-center', start_number=1,
                        side_char='none', confirm_layout=True)
        elif which == 'official':
            step = dict(op='official_numbering_apply', match='Item', expected_page=1,
                        level=2, confirm_layout=True)
        else:
            step = None
    if step is not None:
        step['label'] = 'residual'
    return hwp, step, prefix


class ResidualConsumers(unittest.TestCase):
    own = original.RequiredReadOrigin.own

    def invoke(self, owner, step, prefix, mode):
        if step is None:
            if mode == 'prefix':
                self.assertTrue(run(owner, prefix)['ok'])
            try:
                return owner.service.numbering_readback()
            except LocalCliServiceError as error:
                return error
        return run(owner, *([prefix] if mode == 'prefix' else []), step,
                   {'op': 'where'})

    def stopped(self, owner, probe, result, mode, which):
        self.assertTrue(probe.fired, probe.events)
        self.assertEqual(probe.after, [], (which, probe.events, probe.after))
        self.assertEqual(len(probe.hwp.calls), probe.call_mark)
        self.assertTrue(owner.session._native_custody_held, result)
        self.assertTrue(owner.session._thread.is_alive())
        chain, cause = [], owner.session._terminal_error
        while cause is not None and not any(cause is old for old in chain):
            chain.append(cause)
            cause = cause.__cause__
        self.assertTrue(any(cause is probe.cause for cause in chain), [type(x).__name__ for x in chain])
        self.assertEqual(owner.cleanup_events(), [])
        if which != 'readback':
            self.assertEqual(len(result['steps']), 2 if mode == 'prefix' else 1, result)
            self.assertNotIn('refusal_code', result['steps'][-1])
        if mode in ('prefix', 'prior'):
            self.assertTrue(owner.store['working_copy_dirty'])
            self.assertTrue(owner.session._normal_close_requires_save)
        if mode == 'prefix':
            if which == 'create':
                self.assertEqual(probe.hwp.by_inst('1011').named('B2').fill['face'], 0x0000FF)
            else:
                self.assertIn('Prefix retained', [p['text'] for p in probe.hwp.paragraphs])
        mark = len(probe.hwp.calls)
        for name in ('edit', 'save'):
            with self.assertRaises(rt.LocalCliRuntimeError):
                owner.manager.execute(session_id=owner.sid, command_name=name,
                                      handler=lambda h: h.hwp.get_pos(), timeout=1)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owner.manager.close_session(owner.sid, timeout=1)
        self.assertEqual(probe.after, [])
        self.assertEqual(len(probe.hwp.calls), mark)

    def witness(self, which, wanted, causes):
        trials = 0
        for mode in ('none', 'prefix', 'prior'):
            hwp, step, prefix = fixture(which)
            healthy = Probe(hwp)
            owner = self.own(healthy, working_copy_dirty=mode == 'prior')
            result = self.invoke(owner, step, prefix, mode)
            self.assertIsInstance(result, dict)
            self.assertTrue(result['ok'], result)
            indices = [(i, event) for i, event in enumerate(healthy.events, 1) if wanted(event)]
            self.assertTrue(indices, (which, healthy.events))
            self.assertEqual({event[0] for _, event in indices}, {'get', 'call'} if which != 'official' else {'get', 'call'})
            for i, event in indices:
                for label, maker in causes:
                    with self.subTest(which=which, mode=mode, event=event, index=i, cause=label):
                        hwp, step, prefix = fixture(which)
                        probe = Probe(hwp, fault=i, cause=maker())
                        owner = self.own(probe, working_copy_dirty=mode == 'prior')
                        result = self.invoke(owner, step, prefix, mode)
                        self.stopped(owner, probe, result, mode, which)
                        trials += 1
        print('T25 residual', which, 'fault-fired trials', trials)

    def test_official_actual_limit_consumer(self):
        def typed():
            e = nn.NativeNumberingReadFailure('already typed', code='preservation_unprovable')
            e.__cause__ = nn.ControlEnumerationLimit('original SDK limit')
            return e
        self.witness('official', lambda e: e[2] and e[1] in {'get_text_file', 'HeadCtrl', 'CtrlID', 'Next'},
                     [('limit', lambda: nn.ControlEnumerationLimit('SDK limit')), ('typed-limit', typed)])

    def test_page_actual_accepted_code_lookup_and_call(self):
        self.witness('page', lambda e: e[1] == 'MoveDocBegin',
                     [('existing_page_number', lambda: nn.NativeNumberingRefusal('SDK page collision', code='existing_page_number'))])

    def test_remove_actual_setup_lookup_and_call(self):
        self.witness('remove', lambda e: e[1] == 'select_text',
                     [('target_unlocatable', lambda: nn.NativeNumberingRefusal('SDK selection collision', code='target_unlocatable'))])

    def test_create_actual_setup_lookup_and_call(self):
        self.witness('create', lambda e: e[1] == 'set_pos',
                     [('target_wrong_page', lambda: nn.NativeNumberingRefusal('SDK setup collision', code='target_wrong_page'))])

    def test_actual_readback_private_evidence_collision(self):
        self.witness('readback', lambda e: e[1] == 'get_text_file' and e[3],
                     [('_CensusUnusable', lambda: pe._CensusUnusable('SDK evidence collision'))])

    def test_new_setup_ordinary_and_already_typed_errors(self):
        for which, name in [('page', 'MoveDocBegin'), ('remove', 'select_text'), ('create', 'set_pos')]:
            self.witness(which, lambda e: e[1] == name,
                         [('ordinary', lambda: OSError('SDK ordinary')),
                          ('typed', lambda: nn.NativeNumberingReadFailure('typed SDK', code='target_unlocatable'))])


if __name__ == '__main__':
    unittest.main()
