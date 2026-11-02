"""SDK-only injection through intact public bundles and the real owning STA.

The probe is a controlled fake, never a native fault recipe. Every healthy
mandatory read occurrence in each scenario is independently faulted; the public
normalizer, snapshots, compact handler, worker and finalizers stay intact.
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from app import native_numbering as nn
from app import local_cli_runtime as rt
from tests.native_table_fakes import sample_document
from tests.test_bundle_step_local_refusal import owner_for, style
from tests.test_guarded_bundle_observations import run


READS = frozenset({
    'HeadCtrl', 'CtrlID', 'Next', 'GetCtrlInstID', 'GetAnchorPos',
    'current_page', 'get_current_page', 'GetCurrentPage', 'Page', 'CurrentPage', 'KeyIndicator',
    'get_pos', 'GetPos', 'get_selected_pos', 'GetSelectedPos', 'ParentCtrl',
    'SelectionMode', 'is_cell', 'get_cell_addr', 'get_selected_range', 'get_text_file',
    'HAction', 'HParameterSet', 'HCellBorderFill', 'HTableInsertLine', 'HShapeObject',
    'HCharShape', 'HParaShape', 'HSecDef', 'HPageNumPos', 'HSet', 'FillAttr',
    'CreateAction', 'CreateSet', 'GetDefault', 'Properties', 'Item', 'OutlineShape',
    'HeadingType', 'Level', 'NumFormat', 'StrFormat', 'StartNumber',
    'HInsertText', 'find', 'MoveDocBegin',
})
OBJECTS = frozenset({
    'HeadCtrl', 'Next', 'ParentCtrl', 'HAction', 'HParameterSet', 'HCellBorderFill',
    'HTableInsertLine', 'HShapeObject', 'HCharShape', 'HParaShape', 'HSecDef',
    'HPageNumPos', 'HSet', 'FillAttr', 'CreateAction', 'CreateSet', 'Properties', 'OutlineShape', 'HInsertText',
})


class Probe:
    def __init__(self, hwp, *, entry, fault=None, cause=None, malformed=None, page_name=None):
        self.hwp, self.entry, self.fault = hwp, entry, fault
        self.cause = cause
        self.malformed = malformed
        self.page_name = page_name
        self.events, self.after = [], []
        self.fired = False
        self.call_mark = None
        self.root = SDK(self, hwp)

    def event(self, kind, name):
        if self.fired:
            self.after.append((kind, name))
        frame = inspect.currentframe().f_back
        active = False
        try:
            while frame:
                if frame.f_code.co_name == self.entry:
                    active = (self.entry != '_bundle_table_style_exact'
                              or frame.f_locals['step'].get('table_id') == 'tbl:1012')
                    break
                frame = frame.f_back
        finally:
            del frame
        if active and (name in READS or name.startswith(('StrFormatLevel', 'NumFormatLevel'))):
            event = (kind, name)
            self.events.append(event)
            if len(self.events) == self.fault:
                self.fired = True
                self.call_mark = len(self.hwp.calls)
                if self.cause is not None:
                    raise self.cause
                return True
        return False


class SDK:
    def __init__(self, probe, value):
        object.__setattr__(self, '_probe', probe)
        object.__setattr__(self, '_value', value)

    def __getattr__(self, name):
        probe, value = self._probe, self._value
        if probe.page_name is not None and name in {'current_page', 'get_current_page', 'GetCurrentPage', 'Page', 'CurrentPage', 'KeyIndicator'}:
            if name != probe.page_name:
                raise AttributeError(name)
            if probe.event('get', name):
                return probe.malformed
            if name in {'Page', 'CurrentPage'}:
                return 1
            def page():
                if probe.event('call', name):
                    return probe.malformed
                return (0, 0, 0, 1) if name == 'KeyIndicator' else 1
            return page
        if probe.event('get', name):
            return probe.malformed
        result = getattr(value, name)
        if callable(result):
            def call(*args, **kwargs):
                if probe.event('call', name):
                    return probe.malformed
                args = tuple(arg._value if type(arg) is SDK else arg for arg in args)
                kwargs = {key: arg._value if type(arg) is SDK else arg for key, arg in kwargs.items()}
                out = result(*args, **kwargs)
                return SDK(probe, out) if name in OBJECTS and out is not None else out
            return call
        return SDK(probe, result) if name in OBJECTS and result is not None else result

    def __setattr__(self, name, value):
        self._probe.event('set', name)
        setattr(self._value, name, value._value if type(value) is SDK else value)


def scenario(which):
    if which in ('read', 'inventory', 'style', 'merge', 'width'):
        hwp = sample_document()
        if which == 'read':
            step, entry = {'op': 'table_read', 'table_id': 'tbl:1012'}, '_bundle_table_read'
        elif which == 'inventory':
            step, entry = {'op': 'table_inventory'}, '_bundle_table_inventory'
        elif which == 'style':
            step, entry = style(hwp), '_bundle_table_style_exact'
        else:
            from tests.test_native_table import binding
            if which == 'merge':
                step = dict(op='table_edit_exact', **binding(hwp, '1012'), action='merge', cell='A1', end_cell='B1')
                # Public grammar is kept explicit rather than adapting a refused operation.
            else:
                step = dict(op='table_width_exact', **binding(hwp, '1012'), scope='cell', cell='B2', width_mm=30)
            entry = '_bundle_table_edit_exact' if which == 'merge' else '_bundle_table_width_exact'
        return hwp, step, entry
    if which == 'format':
        from tests.native_format_fakes import sample_document as document
        return document(), dict(op='character_format_exact', match='English', expected_page=1, bold=True, confirm_layout=True), '_bundle_format_exact'
    if which == 'paragraph':
        from tests.test_native_paragraph_edit import _doc
        hwp = _doc('Before', 'Item', 'After')
        fake_finder(hwp)
        return hwp, dict(op='paragraph_insert_exact', match='Item', expected_page=1, text='New', position='after', confirm_layout=True), '_bundle_paragraph_edit'
    from tests.test_native_numbering_review_contract import _hwpml, PAGE_STEP, NUMBERING_STEP
    hwp = _hwpml()
    fake_finder(hwp)
    return hwp, dict(PAGE_STEP if which == 'page_number' else NUMBERING_STEP), '_bundle_page_number_apply' if which == 'page_number' else '_bundle_official_numbering_apply'


def fake_finder(hwp):
    """Complete this SDK fixture's page API; retain its real fake search methods."""
    hwp.current_page = lambda: 1


class RequiredReadOrigin(unittest.TestCase):
    def own(self, probe, **state):
        self.doCleanups()  # release only the previous synthetic owner's test adapter
        owner = owner_for(self, probe.root, **state)
        from app.local_cli_service import LocalCliService
        owner.service._find_text_on_expected_page = LocalCliService._find_text_on_expected_page.__get__(owner.service)
        return owner

    def assert_stopped(self, owner, probe, result, prefix, prior):
        self.assertTrue(probe.fired, probe.events)
        self.assertTrue(owner.session._native_custody_held, result)
        self.assertTrue(owner.session._thread.is_alive())
        if probe.cause is not None:
            cause = owner.session._terminal_error
            chain = []
            while cause is not None and not any(cause is old for old in chain):
                chain.append(cause)
                cause = cause.__cause__
            self.assertTrue(any(cause is probe.cause for cause in chain), [type(cause).__name__ for cause in chain])
        self.assertEqual(probe.after, [], (probe.events, probe.after))
        self.assertEqual(len(probe.hwp.calls), probe.call_mark)
        self.assertEqual(owner.cleanup_events(), [])
        self.assertEqual(len(result['steps']), int(prefix) + 1, result)
        self.assertNotIn('refusal_code', result['steps'][-1])
        if prefix:
            self.assertTrue(result['steps'][0]['ok'], result)
            self.assertEqual(probe.hwp.by_inst('1011').named('B2').fill['face'], 0x0000FF)
        if prefix or prior:
            self.assertTrue(owner.store['working_copy_dirty'])
            self.assertTrue(owner.session._normal_close_requires_save)
        mark = len(probe.hwp.calls)
        for name in ('edit', 'save'):
            with self.assertRaises(rt.LocalCliRuntimeError):
                owner.manager.execute(session_id=owner.sid, command_name=name,
                                      handler=lambda handle: handle.hwp.get_pos(), timeout=1)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owner.manager.close_session(owner.sid, timeout=1)
        self.assertEqual(probe.after, [])
        self.assertEqual(len(probe.hwp.calls), mark)

    def test_control_page_anchor_all_occurrences_real_wrapper(self):
        covered = set()
        trials = 0
        # Read/inventory cover first/later control nodes, repeat scan, each anchor,
        # page visit and give-back. Style also covers targeting and post-proof.
        for which in ('read', 'inventory', 'style'):
            hwp, step, entry = scenario(which)
            healthy = Probe(hwp, entry=entry)
            owner = self.own(healthy)
            result = run(owner, step)
            self.assertTrue(result['ok'], (which, result))
            events = healthy.events
            self.assertTrue(events)
            for index, (kind, name) in enumerate(events, start=1):
                covered.add((kind, name))
                # Fault getter resolution and invocation independently. Only
                # source-facing SDK accesses are instrumented; no helper replaced.
                for prefix, prior in ((False, False), (True, False), (False, True)):
                    with self.subTest(which=which, index=index, event=(kind, name), prefix=prefix, prior=prior):
                        hwp, step, entry = scenario(which)
                        cause = nn.NativeNumberingRefusal('synthetic SDK origin', code='table_not_found')
                        probe = Probe(hwp, entry=entry, fault=index, cause=cause)
                        owner = self.own(probe, working_copy_dirty=prior)
                        steps = [style(hwp, '1011')] if prefix else []
                        result = run(owner, *steps, step, {'op': 'table_inventory'})
                        trials += 1
                        self.assert_stopped(owner, probe, result, prefix, prior)
                        covered.add((kind, name))
        self.assertTrue({'HeadCtrl', 'CtrlID', 'Next', 'GetAnchorPos', 'current_page'} <= {name for _, name in covered})
        print('T25 control/page/anchor trials:', trials, 'read boundaries:', sorted(covered))

    def test_adjacent_public_families_all_occurrences(self):
        trials = 0
        for which in ('format', 'paragraph', 'page_number', 'numbering'):
            hwp, step, entry = scenario(which)
            healthy = Probe(hwp, entry=entry)
            owner = self.own(healthy)
            with patch('app.native_format.installed_font_families', return_value={'Arial'}):
                result = run(owner, step)
            self.assertTrue(result['ok'], (which, result))
            for index, event in enumerate(healthy.events, start=1):
                with self.subTest(which=which, index=index, event=event):
                    hwp, step, entry = scenario(which)
                    cause = nn.NativeNumberingRefusal('synthetic adjacent SDK origin', code='target_not_found')
                    probe = Probe(hwp, entry=entry, fault=index, cause=cause)
                    owner = self.own(probe, working_copy_dirty=True)
                    with patch('app.native_format.installed_font_families', return_value={'Arial'}):
                        result = run(owner, step, {'op': 'where'})
                    self.assert_stopped(owner, probe, result, False, True)
                    trials += 1
        print('T25 adjacent family trials:', trials)

    def test_page_spellings_raising_and_malformed_mandatory_returns(self):
        for page_name in ('current_page', 'get_current_page', 'GetCurrentPage', 'Page', 'CurrentPage', 'KeyIndicator'):
            hwp, step, entry = scenario('inventory')
            healthy = Probe(hwp, entry=entry, page_name=page_name)
            owner = self.own(healthy)
            self.assertTrue(run(owner, step)['ok'])
            occurrences = [(i, kind) for i, (kind, name) in enumerate(healthy.events, 1) if name == page_name]
            self.assertTrue(occurrences)
            for index, kind in occurrences:
                for cause in (nn.NativeNumberingRefusal('synthetic page origin', code='table_not_found'), OSError('synthetic page read')):
                    with self.subTest(page=page_name, index=index, cause=type(cause).__name__):
                        hwp, step, entry = scenario('inventory')
                        probe = Probe(hwp, entry=entry, fault=index, cause=cause, page_name=page_name)
                        owner = self.own(probe, working_copy_dirty=True)
                        self.assert_stopped(owner, probe, run(owner, step), False, True)
                if kind == 'call' or page_name in {'Page', 'CurrentPage'}:
                    with self.subTest(page=page_name, index=index, malformed=True):
                        hwp, step, entry = scenario('inventory')
                        probe = Probe(hwp, entry=entry, fault=index, malformed=('bad',), page_name=page_name)
                        owner = self.own(probe)
                        self.assert_stopped(owner, probe, run(owner, step), False, False)

    def test_sdk_collision_classes_and_already_typed_cause(self):
        from app import native_numbering_census as census
        for name in ('HeadCtrl', 'CtrlID', 'Next', 'get_text_file'):
            hwp, step, entry = scenario('read')
            healthy = Probe(hwp, entry=entry)
            owner = self.own(healthy)
            self.assertTrue(run(owner, step)['ok'])
            index = next(i for i, event in enumerate(healthy.events, 1) if event == ('get', name))
            for cause in (OSError('synthetic read'), nn.ControlEnumerationLimit('forged limit'),
                          census.CensusError('forged parse refusal'), census.CensusTextUnusable('forged unusable'),
                          rt.LocalCliRuntimeError('forged runtime refusal'),
                          nn.NativeNumberingReadFailure('already classified', code='target_unlocatable')):
                with self.subTest(name=name, cause=type(cause).__name__):
                    hwp, step, entry = scenario('read')
                    probe = Probe(hwp, entry=entry, fault=index, cause=cause)
                    owner = self.own(probe)
                    self.assert_stopped(owner, probe, run(owner, step), False, False)


if __name__ == '__main__':
    unittest.main()
