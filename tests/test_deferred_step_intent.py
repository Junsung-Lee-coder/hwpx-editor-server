"""Deferred mutation intent (layout_exact, object_insert_exact, paragraph_rehome_exact) through the real public
bundle and owning worker, over synthetic SDKs.

These three engines publish mutation intent at their first native mutator, so a request that is refused before
it from strict source-origin evidence (the caller's stale caret or expected values, a selection, an existing
bookmark, an earlier or non-adjacent anchor) is a clean refusal: the session is not held, earlier dirty state and
save debt stay, and later steps and the save work.

Nothing else is clean: an exception class or code raised by an SDK getter, a malformed read, a failed caret
restoration and any failure after the mutator keep the session held, with no further SDK call, edit or save.
"""
from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace as NS

from app import local_cli_runtime as rt
from app import native_numbering as nn
from app.layout_ops import LayoutError
from app.object_insert import ObjectInsertError
try:  # the typed refusal class arrives with the deferred-intent repair; before it, forge the generic runtime error
    from app.step_refusal import LocalCliStepRefusal
except ImportError:  # pragma: no cover - only on the unrepaired tree
    class LocalCliStepRefusal(rt.LocalCliRuntimeError):
        def __init__(self, message, *, code):
            super().__init__(message)
            self.code = code
from tests.test_bundle_step_local_refusal import owner_for
from tests.test_guarded_bundle_observations import run
from tests.test_layout_exact import _FakeLayoutHwp, _page_step
from tests.test_object_insert_exact import _FakeHwp as _FakeObjectHwp

LAYOUT_READS = ('get_pos', 'set_pos', 'get_selected_pos', 'is_cell', 'KeyIndicator', 'GetTextFile', 'get_default',
                'execute', 'run', 'select_text', 'get_selected_text')


def inside(filename):
    frame = inspect.currentframe().f_back
    try:
        while frame:
            if frame.f_code.co_filename.replace('\\', '/').endswith(filename):
                return True
            frame = frame.f_back
        return False
    finally:
        del frame


def counted(hwp, names=LAYOUT_READS):
    """Record every call of the named SDK entry points in ``hwp.sdk_calls``."""
    hwp.sdk_calls = []
    for name in names:
        method = getattr(hwp, name, None)
        if callable(method):
            def wrapper(*args, _name=name, _method=method, **kwargs):
                hwp.sdk_calls.append(_name)
                return _method(*args, **kwargs)
            setattr(hwp, name, wrapper)
    return hwp


def fault(hwp, name, behave, *, engine='/app/local_cli_layout.py', nth=1):
    """Make the ``nth`` call of ``name`` inside the engine raise or return ``behave()``; record the call mark."""
    original = getattr(hwp, name)
    seen = []

    def faulty(*args, **kwargs):
        if inside(engine):
            seen.append(1)
            if len(seen) == nth:
                hwp.fault_mark = len(hwp.sdk_calls)
                return behave()
        return original(*args, **kwargs)
    setattr(hwp, name, faulty)
    return hwp


def modifies(hwp, name, edits):
    """Hancom sets IsModified when an edit lands; the bundle reconciles its dirty flag with it."""
    original = getattr(hwp, name)

    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        if edits(*args):
            hwp.IsModified = True
        return result
    setattr(hwp, name, wrapper)
    return hwp


def raises(exc):
    def behave():
        raise exc
    return behave


def save(owner):
    def handler(handle):
        handle.hwp.GetTextFile('HWPML2X', '') if hasattr(handle.hwp, 'GetTextFile') else None
        return {'ok': True, 'dirty': False, 'ordinary_save_confirmed': True}
    return owner.manager.execute(session_id=owner.sid, command_name='save', handler=handler, timeout=5)


class _Checks(unittest.TestCase):
    def assert_clean_refusal(self, owner, result, index, code, *, dirty):
        self.assertFalse(result['ok'], result)
        self.assertEqual(len(result['steps']), index + 1, result)
        step = result['steps'][index]
        self.assertEqual(step.get('refusal_code'), code, step)
        self.assertFalse(step['mutation_may_have_persisted'], step)
        self.assertFalse(step['dirty'], step)
        self.assertNotIn('native_custody_held', step)
        self.assertFalse(owner.session._native_custody_held)
        self.assertEqual(result['dirty'], dirty, result)
        self.assertEqual(owner.store['working_copy_dirty'], dirty)
        self.assertEqual(owner.session._normal_close_requires_save, dirty)

    def assert_continues_and_saves(self, owner, step):
        later = run(owner, step)
        self.assertTrue(later['ok'], later)
        self.assertTrue(owner.session._normal_close_requires_save)
        self.assertTrue(save(owner)['ordinary_save_confirmed'])
        self.assertFalse(owner.session._normal_close_requires_save)
        self.assertFalse(owner.session._native_custody_held)

    def assert_held(self, owner, hwp, result, *, mutated, phase=None):
        step = result['steps'][-1]
        self.assertFalse(result['ok'], result)
        self.assertNotIn('refusal_code', step)
        self.assertTrue(step.get('native_custody_held'), step)
        self.assertTrue(owner.session._native_custody_held)
        self.assertEqual(step['mutation_may_have_persisted'], mutated, step)
        if phase is not None:
            self.assertEqual(step['failure_diagnostic'].get('step_phase'), phase, step['failure_diagnostic'])
        mark = len(hwp.sdk_calls)
        for name in ('edit', 'save'):
            with self.assertRaises(rt.LocalCliRuntimeError):
                owner.manager.execute(session_id=owner.sid, command_name=name, handler=lambda handle: handle.hwp.get_pos(), timeout=1)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owner.manager.close_session(owner.sid, timeout=1)
        self.assertEqual(len(hwp.sdk_calls), mark)
        self.assertEqual(owner.cleanup_events(), [])


# ------------------------------------------------------------------ layout_exact

def layout_doc(**faults):
    hwp = counted(_FakeLayoutHwp(**faults))
    for name in ('execute', 'run'):
        modifies(hwp, name, lambda action, *rest: action in _FakeLayoutHwp.MUTATING and action not in hwp.noop)
    hwp.fault_mark = None
    return hwp


def left_margin(hwp):
    return hwp.sections[0]['pagedef']['LeftMargin']


class LayoutDeferredIntent(_Checks):
    REFUSALS = (
        ('caret_moved', lambda h: _page_step(expected_pos=[0, 1, 0])),
        ('editor_not_normal', lambda h: (setattr(h, 'selection', (0, 0, 0, 1)), _page_step())[1]),
        ('stale_expected_before', lambda h: _page_step(expected_before={'margin_left_mm': 25.0})),
        ('nothing_to_change', lambda h: _page_step(margin_left_mm=30.0)),
        ('caret_in_table', lambda h: (setattr(h, 'in_cell', True), {'op': 'layout_exact', 'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2, 'gap_mm': 8})[1]),
    )

    def test_refusals_are_clean_and_the_session_continues_to_save(self):
        for prior_dirty in (False, True):
            for code, make in self.REFUSALS:
                with self.subTest(prior_dirty=prior_dirty, code=code):
                    hwp = layout_doc()
                    owner = owner_for(self, hwp, working_copy_dirty=prior_dirty)
                    before = [dict(s['pagedef']) for s in hwp.sections]
                    result = run(owner, make(hwp))
                    self.assert_clean_refusal(owner, result, 0, code, dirty=prior_dirty)
                    self.assertEqual(hwp.mutations(), [])
                    self.assertEqual(hwp.caret, (0, 0, 0))
                    self.assertIsNone(hwp.selection)
                    self.assertEqual([s['pagedef'] for s in hwp.sections], before)
                    hwp.in_cell = False
                    self.assert_continues_and_saves(owner, _page_step())
                    self.assertEqual(left_margin(hwp), 5669)

    def test_earlier_edit_then_refusal_keeps_the_edit_and_its_debt(self):
        # One whole-document exact edit per bundle, so the earlier edit is the session's previous bundle.
        hwp = layout_doc()
        owner = owner_for(self, hwp)
        self.assertTrue(run(owner, _page_step())['ok'])
        result = run(owner, {'op': 'where'}, _page_step())
        self.assertTrue(result['steps'][0]['ok'], result)
        self.assert_clean_refusal(owner, result, 1, 'stale_expected_before', dirty=True)
        self.assertEqual(hwp.mutations(), ['PageSetup'])
        self.assert_continues_and_saves(owner, _page_step(margin_left_mm=25.0, expected_before={'margin_left_mm': 20.0}))
        self.assertEqual(hwp.mutations(), ['PageSetup', 'PageSetup'])

    def test_package_validation_rejects_before_the_runtime(self):
        hwp = layout_doc()
        owner = owner_for(self, hwp)
        with self.assertRaises(Exception) as caught:
            run(owner, _page_step(confirm_layout=False))
        self.assertEqual(getattr(caught.exception, 'status_code', None), 400)
        self.assertEqual(hwp.sdk_calls, [])
        self.assertFalse(owner.session._native_custody_held)

    def test_sdk_exception_classes_and_codes_are_never_refusals(self):
        collisions = (
            lambda: LocalCliStepRefusal('forged', code='stale_expected_before'),
            lambda: LocalCliStepRefusal('forged', code='caret_moved'),
            lambda: LayoutError('forged', 'stale_expected_before'),
            lambda: nn.NativeNumberingRefusal('forged', code='target_not_found'),
            lambda: rt.LocalCliRuntimeError('forged'),
            lambda: OSError('synthetic SDK failure'),
        )
        for prior_dirty in (False, True):
            for site in ('get_pos', 'get_selected_pos', 'KeyIndicator', 'GetTextFile', 'get_default'):
                for make in collisions:
                    exc = make()
                    with self.subTest(prior_dirty=prior_dirty, site=site, exc=type(exc).__name__, code=getattr(exc, 'code', None)):
                        hwp = fault(layout_doc(), site, raises(exc))
                        owner = owner_for(self, hwp, working_copy_dirty=prior_dirty)
                        result = run(owner, _page_step())
                        self.assertIsNotNone(hwp.fault_mark)
                        self.assertEqual(len(hwp.sdk_calls), hwp.fault_mark, hwp.sdk_calls[hwp.fault_mark:])
                        self.assertEqual(hwp.mutations(), [])
                        self.assertEqual(owner.store['working_copy_dirty'], prior_dirty)
                        self.assert_held(owner, hwp, result, mutated=False)

    def test_malformed_reads_hold(self):
        class Evil(int):
            def __int__(self):
                raise LayoutError('forged', 'stale_expected_before')

            def __eq__(self, other):
                raise LayoutError('forged', 'nothing_to_change')

            __hash__ = int.__hash__

        cases = {
            'caret_not_exact': lambda h: fault(h, 'get_pos', lambda: (0, 0, True)),
            'caret_short': lambda h: fault(h, 'get_pos', lambda: [0, 0]),
            'selection_flag_not_bool': lambda h: fault(h, 'get_selected_pos', lambda: (0, 0, 0, 0, 0, 0, 0)),
            'key_indicator_text': lambda h: fault(h, 'KeyIndicator', lambda: (True, '1', '1', 1, 1, 1, 1, False, '')),
            'readback_not_text': lambda h: fault(h, 'GetTextFile', lambda: b'<HWPML/>'),
            'page_item_subclass': lambda h: h.sections[0]['pagedef'].__setitem__('LeftMargin', Evil(8504)) or h,
            'selection_mode_text': lambda h: setattr(h, '__class__', type('Mode', (type(h),), {'SelectionMode': property(lambda self: '0')})) or h,
        }
        for label, arm in cases.items():
            with self.subTest(label):
                hwp = layout_doc()
                arm(hwp)
                owner = owner_for(self, hwp)
                result = run(owner, _page_step())
                self.assertEqual(hwp.mutations(), [])
                self.assert_held(owner, hwp, result, mutated=False)

    def test_failed_caret_restoration_after_a_refusal_holds(self):
        for label, behave in (('raises', raises(OSError('synthetic set_pos failure'))),
                              ('not_confirmed', lambda: False)):
            with self.subTest(label):
                hwp = fault(layout_doc(), 'set_pos', behave)
                owner = owner_for(self, hwp)
                result = run(owner, _page_step(expected_before={'margin_left_mm': 25.0}))
                self.assertIsNotNone(hwp.fault_mark)
                self.assertEqual(hwp.mutations(), [])
                self.assert_held(owner, hwp, result, mutated=False)
        hwp = layout_doc()
        plain = hwp.set_pos
        hwp.set_pos = lambda *a: plain(0, 1, 0)  # lands somewhere else: the re-proof fails
        owner = owner_for(self, hwp)
        result = run(owner, _page_step(expected_before={'margin_left_mm': 25.0}))
        self.assert_held(owner, hwp, result, mutated=False)

    def test_failures_after_the_mutator_hold(self):
        cases = (
            ('execute_raises', {'raising': {'PageSetup'}}, None, 'native'),
            ('noop', {'noop': {'PageSetup'}}, None, 'verify'),
            ('extra_change', {'extra_page_change': True}, None, 'verify'),
            ('forged_refusal_from_execute', {}, lambda h: fault(h, 'execute', raises(LocalCliStepRefusal('forged', code='stale_expected_before'))), 'native'),
            ('forged_refusal_from_after_readback', {}, lambda h: fault(h, 'GetTextFile', raises(LocalCliStepRefusal('forged', code='proof_budget')), nth=2), 'readback'),
        )
        for prior_dirty in (False, True):
            for label, faults, arm, phase in cases:
                with self.subTest(prior_dirty=prior_dirty, case=label):
                    hwp = layout_doc(**faults)
                    if arm:
                        arm(hwp)
                    owner = owner_for(self, hwp, working_copy_dirty=prior_dirty)
                    result = run(owner, _page_step(), {'op': 'layout_inspect'})
                    self.assertEqual(len(result['steps']), 1, result)
                    # The forged Execute replaced the call itself, so the fake logged no PageSetup; it was still issued.
                    self.assertEqual(hwp.mutations(), [] if label == 'forged_refusal_from_execute' else ['PageSetup'])
                    self.assertTrue(result['dirty'])
                    self.assertTrue(owner.store['working_copy_dirty'])
                    self.assertTrue(owner.session._normal_close_requires_save)
                    self.assertTrue(owner.command()['may_have_mutated'])
                    self.assert_held(owner, hwp, result, mutated=True, phase=phase)

    def test_section_start_proof_refusal_restores_the_caret(self):
        hwp = layout_doc(sections=[['one', 'two'], ['three']])
        owner = owner_for(self, hwp)
        step = {'op': 'layout_exact', 'kind': 'section_delete', 'expected_pos': [0, 1, 0], 'section_index': 2, 'confirm_layout': True}
        hwp.caret = (0, 1, 0)
        result = run(owner, step)
        self.assert_clean_refusal(owner, result, 0, 'not_section_start', dirty=False)
        self.assertEqual(hwp.caret, (0, 1, 0))
        self.assertEqual(hwp.mutations(), [])


# ------------------------------------------------------------------ object_insert_exact

OBJECT_READS = ('get_pos', 'set_pos', 'get_selected_pos', 'GetTextFile', 'get_selected_text', 'native', 'insert_text')


def object_doc(**faults):
    hwp = counted(_FakeObjectHwp(**faults), OBJECT_READS)
    modifies(hwp, 'native', lambda action, *rest: action not in hwp.noop)
    hwp.fault_mark = None
    return hwp


def object_step(hwp, **fields):
    step = {'op': 'object_insert_exact', 'kind': 'footnote', 'text': 'note', 'expected_pos': list(hwp.caret), 'confirm_mutation': True}
    step.update(fields)
    return {key: value for key, value in step.items() if value is not None}


class ObjectDeferredIntent(_Checks):
    def test_refusals_are_clean_and_the_session_continues(self):
        cases = (
            ('selection_required', lambda h: object_step(h, kind='hyperlink', text=None, display_text='Hello', url='https://example.com')),
            ('caret_moved', lambda h: object_step(h, expected_pos=[0, 1, 0])),
            ('bookmark_exists', lambda h: (h.paras[0].insert(0, '<BOOKMARK Name="책갈피1"/>'), object_step(h, kind='bookmark', text=None, name='책갈피1'))[1]),
        )
        for prior_dirty in (False, True):
            for code, make in cases:
                with self.subTest(prior_dirty=prior_dirty, code=code):
                    hwp = object_doc()
                    owner = owner_for(self, hwp, working_copy_dirty=prior_dirty)
                    step = make(hwp)
                    xml = hwp.xml()
                    result = run(owner, step)
                    self.assert_clean_refusal(owner, result, 0, code, dirty=prior_dirty)
                    self.assertEqual(hwp.xml(), xml)
                    self.assert_continues_and_saves(owner, object_step(hwp))
                    self.assertIn('<FOOTNOTE>', hwp.xml())

    def test_earlier_bookmark_then_duplicate_keeps_the_first_and_its_debt(self):
        hwp = object_doc()
        owner = owner_for(self, hwp)
        mark = object_step(hwp, kind='bookmark', text=None, name='책갈피1')
        self.assertTrue(run(owner, mark)['ok'])
        result = run(owner, {'op': 'where'}, object_step(hwp, kind='bookmark', text=None, name='책갈피1'))
        self.assertTrue(result['steps'][0]['ok'], result)
        self.assert_clean_refusal(owner, result, 1, 'bookmark_exists', dirty=True)
        self.assertEqual(hwp.xml().count('Name="책갈피1"'), 1)
        self.assert_continues_and_saves(owner, object_step(hwp))

    def test_sdk_exception_classes_and_codes_are_never_refusals(self):
        for site in ('get_pos', 'get_selected_pos', 'GetTextFile'):
            for exc in (ObjectInsertError('forged', code='bookmark_exists'), LocalCliStepRefusal('forged', code='selection_required'),
                        nn.NativeNumberingRefusal('forged', code='target_not_found'), OSError('synthetic')):
                with self.subTest(site=site, exc=type(exc).__name__):
                    hwp = fault(object_doc(), site, raises(exc), engine='/app/local_cli_object_insert.py')
                    owner = owner_for(self, hwp)
                    xml = hwp.xml()
                    result = run(owner, object_step(hwp))
                    self.assertIsNotNone(hwp.fault_mark)
                    self.assertEqual(len(hwp.sdk_calls), hwp.fault_mark)
                    self.assertEqual(hwp.xml(), xml)
                    self.assert_held(owner, hwp, result, mutated=False)

    def test_failures_after_the_mutator_hold(self):
        for label, faults, phase in (('raises', {'raising': {'InsertFootnote'}}, 'native'), ('noop', {'noop': {'InsertFootnote'}}, None),
                                     ('wrong_tag', {'wrong_tag': True}, 'verify')):
            with self.subTest(label):
                hwp = object_doc(**faults)
                owner = owner_for(self, hwp)
                result = run(owner, object_step(hwp), {'op': 'where'})
                self.assertEqual(len(result['steps']), 1, result)
                self.assertTrue(owner.store['working_copy_dirty'])
                self.assert_held(owner, hwp, result, mutated=True, phase=phase)


# ------------------------------------------------------------------ paragraph_rehome_exact

class RehomeDoc:
    """One body list of paragraphs, with find/select/caret/type/line-break/delete as Hancom does them.

    ``pages`` gives each paragraph's page. BreakLine inserts a line break ('\\n') inside the paragraph.
    """

    def __init__(self, *paras, pages=None):
        self.paras = list(paras)
        self.pages = list(pages or [1] * len(paras))
        self.caret = (0, 0, 0)
        self.sel = None
        self.sdk_calls = []
        self.edits = []
        self.fault_mark = None
        self.HAction = NS(Run=lambda name: self.run(name))

    def current_page(self):
        return self.pages[self.caret[1]]

    def MoveDocBegin(self):  # noqa: N802
        self.sdk_calls.append('MoveDocBegin')
        self.caret, self.sel = (0, 0, 0), None
        return True

    def find(self, text, direction='Forward', MatchCase=1, WholeWordOnly=0):  # noqa: N803
        self.sdk_calls.append('find')
        _, para, pos = self.caret
        for index in range(para, len(self.paras)):
            start = self.paras[index].find(text, pos if index == para else 0)
            if start >= 0:
                self.sel = (index, start, index, start + len(text))
                self.caret = (0, index, start + len(text))
                return True
        return False

    def get_pos(self):
        self.sdk_calls.append('get_pos')
        return self.caret

    def set_pos(self, list_id, para, pos):
        self.sdk_calls.append('set_pos')
        self.caret, self.sel = (list_id, para, pos), None
        return True

    def get_selected_pos(self):
        self.sdk_calls.append('get_selected_pos')
        if self.sel is None:
            return (False, 0, 0, 0, 0, 0, 0)
        sp, spos, ep, epos = self.sel
        return (True, 0, sp, spos, 0, ep, epos)

    def select_text(self, spara, spos, epara, epos, slist=0):
        self.sdk_calls.append('select_text')
        self.sel, self.caret = (spara, spos, epara, epos), (0, epara, epos)
        return True

    def get_selected_text(self, keep_select=True):
        self.sdk_calls.append('get_selected_text')
        sp, spos, ep, epos = self.sel
        if sp == ep:
            return self.paras[sp][spos:epos]
        return '\r\n'.join([self.paras[sp][spos:], *self.paras[sp + 1:ep], self.paras[ep][:epos]])

    def insert_text(self, text):
        self.sdk_calls.append('insert_text')
        self.edits.append(('insert_text', text))
        self.IsModified = True
        _, para, pos = self.caret
        self.paras[para] = self.paras[para][:pos] + text + self.paras[para][pos:]
        self.caret, self.sel = (0, para, pos + len(text)), None

    def run(self, name):
        self.sdk_calls.append(name)
        _, para, pos = self.caret
        if name == 'BreakLine':
            self.edits.append(name)
            self.IsModified = True
            self.paras[para] = self.paras[para][:pos] + '\n' + self.paras[para][pos:]
            self.caret = (0, para, pos + 1)
            return True
        if name == 'DeleteBack' and pos > 0:
            self.edits.append(name)
            self.IsModified = True
            self.paras[para] = self.paras[para][:pos - 1] + self.paras[para][pos:]
            self.caret = (0, para, pos - 1)
            return True
        if name in ('Undo', 'Cancel'):
            self.edits.append(name)
            return True
        return False

    def get_text_file(self, fmt='UNICODE', option=''):
        self.sdk_calls.append('get_text_file')
        return '\r\n'.join(self.paras)


def rehome(**fields):
    step = {'op': 'paragraph_rehome_exact', 'delete_match': '끝말', 'insert_text': '끝말', 'insert_before_match': '다음 문단',
            'delete_expected_page': 1, 'insert_expected_page': 1, 'confirm_layout': True}
    step.update(fields)
    return step


class RehomeDeferredIntent(_Checks):
    def owner(self, hwp, **state):
        from app.local_cli_service import LocalCliService
        owner = owner_for(self, hwp, **state)
        # The harness stubs the finder; these tests run the real one over this fixture.
        owner.service._find_text_on_expected_page = LocalCliService._find_text_on_expected_page.__get__(owner.service)
        return owner

    def test_same_bundle_move_then_refusal_keeps_the_move_and_continues_to_save(self):
        hwp = RehomeDoc('첫 문단 끝말', '다음 문단 시작', '꼬리 문단')
        owner = self.owner(hwp)
        result = run(owner, rehome(), rehome(delete_match='꼬리', insert_text='꼬리', insert_before_match='끝말'))
        self.assertTrue(result['steps'][0]['ok'], result)
        self.assert_clean_refusal(owner, result, 1, 'insert_before_delete', dirty=True)
        self.assertEqual(hwp.paras, ['첫 문단 ', '끝말\n다음 문단 시작', '꼬리 문단'])
        self.assert_continues_and_saves(owner, {'op': 'where'})

    def test_adjacent_forward_move(self):
        hwp = RehomeDoc('첫 문단 끝말', '다음 문단 시작')
        owner = self.owner(hwp)
        result = run(owner, rehome())
        self.assertTrue(result['ok'], result)
        self.assertEqual(hwp.paras, ['첫 문단 ', '끝말\n다음 문단 시작'])
        self.assertEqual(result['steps'][0]['result']['delete_range'], [0, 0, 5, 0, 0, 7])
        self.assertEqual(result['steps'][0]['result']['insert_at'], [0, 1, 0])
        self.assertTrue(owner.session._normal_close_requires_save)
        self.assertFalse(owner.session._native_custody_held)

    def test_the_recorded_range_is_deleted_not_an_earlier_duplicate(self):
        # '끝말' also ends the page-1 paragraph; the move binds the page-2 occurrence and deletes exactly that one.
        hwp = RehomeDoc('표지 끝말', '본문 끝말', '다음 문단 시작', pages=[1, 2, 2])
        owner = self.owner(hwp)
        result = run(owner, rehome(delete_expected_page=2, insert_expected_page=2))
        self.assertTrue(result['ok'], result)
        self.assertEqual(hwp.paras, ['표지 끝말', '본문 ', '끝말\n다음 문단 시작'])

    def test_earlier_anchor_is_refused_before_any_edit(self):
        for prior_dirty in (False, True):
            with self.subTest(prior_dirty=prior_dirty):
                hwp = RehomeDoc('다음 문단 앞', '첫 문단 끝말', '꼬리')
                hwp.caret = (0, 2, 1)
                owner = self.owner(hwp, working_copy_dirty=prior_dirty)
                result = run(owner, rehome())
                self.assert_clean_refusal(owner, result, 0, 'insert_before_delete', dirty=prior_dirty)
                self.assertEqual(hwp.edits, [])
                self.assertEqual(hwp.paras, ['다음 문단 앞', '첫 문단 끝말', '꼬리'])
                self.assertEqual(hwp.caret, (0, 2, 1))
                self.assertTrue(run(owner, {'op': 'where'})['ok'])

    def test_overlapping_and_non_adjacent_anchors_are_refused(self):
        for label, paras, step, code in (
                ('anchor_inside_text', ('첫 문단 끝말 다음 문단',), rehome(delete_match='끝말 다음', insert_text='끝말 다음'), 'insert_before_delete'),
                ('text_between', ('첫 문단 끝말', '중간 문단', '다음 문단 시작'), rehome(), 'not_adjacent')):
            with self.subTest(label):
                hwp = RehomeDoc(*paras)
                owner = self.owner(hwp)
                result = run(owner, step)
                self.assert_clean_refusal(owner, result, 0, code, dirty=False)
                self.assertEqual(hwp.edits, [])
                self.assertEqual(hwp.paras, list(paras))
                self.assertEqual(hwp.caret, (0, 0, 0))

    def test_unproven_location_reads_hold(self):
        def wrong_text(h):
            plain = h.get_selected_text
            h.get_selected_text = lambda keep_select=True: plain(keep_select).replace('끝말', '끝맡')
        def between_unconfirmed(h):
            plain = h.select_text
            def select(spara, spos, epara, epos, slist=0):
                if (spara, spos) == (0, 7):
                    h.fault_mark = len(h.sdk_calls)
                    return False
                return plain(spara, spos, epara, epos, slist)
            h.select_text = select
        def between_wrong_range(h):
            plain = h.select_text
            h.select_text = lambda spara, spos, epara, epos, slist=0: plain(spara, spos, epara, epos + 1 if spara != epara else epos, slist)
        def forged_refusal(h):
            def read(*a, **k):
                raise LocalCliStepRefusal('forged', code='not_adjacent')
            h.get_selected_pos = read
        for label, arm in (('selected_text_differs', wrong_text), ('between_selection_unconfirmed', between_unconfirmed),
                           ('between_selection_elsewhere', between_wrong_range), ('forged_refusal_from_selection_read', forged_refusal)):
            with self.subTest(label):
                hwp = RehomeDoc('첫 문단 끝말', '다음 문단 시작')
                arm(hwp)
                owner = self.owner(hwp)
                result = run(owner, rehome())
                self.assertEqual(hwp.edits, [])
                self.assertEqual(hwp.paras, ['첫 문단 끝말', '다음 문단 시작'])
                self.assert_held(owner, hwp, result, mutated=False)

    def test_failure_after_the_first_edit_holds_without_undo(self):
        hwp = RehomeDoc('첫 문단 끝말', '다음 문단 시작')
        plain = hwp.run
        def run_action(name):
            if name == 'BreakLine':
                raise OSError('synthetic BreakLine failure')
            return plain(name)
        hwp.HAction = NS(Run=run_action)
        owner = self.owner(hwp)
        result = run(owner, rehome())
        self.assertEqual(hwp.edits, [('insert_text', '끝말')])
        self.assertTrue(owner.store['working_copy_dirty'])
        self.assert_held(owner, hwp, result, mutated=True, phase='after_mutation')


if __name__ == '__main__':
    unittest.main()
