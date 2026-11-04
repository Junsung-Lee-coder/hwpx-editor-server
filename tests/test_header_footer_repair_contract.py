"""N1 review regressions at public bundle/service/runtime boundaries.

SDK fault injection is portable source evidence, never real-Hancom corruption.
"""
import copy
import threading
import unittest
from unittest.mock import patch

from app.local_cli_service import LocalCliUndoRefused
from hwpx_mcp.schema import Command
from pydantic import ValidationError
from tests.native_format_fakes import paragraph
from tests.native_header_footer_fakes import HeaderFooterHwp, area, report_document, sections_document
from tests.test_header_footer_exact import TEXT, FORMAT, bound, inventory
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc


def request(hwp, area_id, operation=TEXT, **extra):
    fields = {'old_text': '2025', 'new_text': '2026'} if operation is TEXT else {'match': 'HEADER', 'bold': True}
    return {**operation, **{k: v for k, v in bound(hwp, area_id).items() if k != 'expected_affected_sections'}, **fields, **extra}


class AffectedSections(unittest.TestCase):
    def check_refusal(self, step, code, hwp):
        before = hwp.get_text_file('HWPML2X', '')
        hwp.calls.clear()
        result = Flow(hwp).step(**step)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['steps'][0].get('refusal_code'), code, result)
        self.assertFalse(result.get('native_custody_held', False))
        self.assertEqual(hwp.executes(), [])
        self.assertNotIn(('Run', 'Delete'), hwp.calls)
        self.assertEqual(hwp.get_text_file('HWPML2X', ''), before)
        self.assertTrue(Flow(hwp).step(op='header_footer_inventory')['ok'])

    def test_shared_owner_without_acknowledgement_refuses_text_and_format(self):
        for operation in (TEXT, FORMAT):
            with self.subTest(op=operation['op']):
                hwp = sections_document()
                self.check_refusal(request(hwp, 'header:s1:odd', operation), 'affected_sections_required', hwp)

    def test_subset_foreign_and_stale_topology_refuse_before_edit(self):
        for expected in ([1], [0, 1, 2], [1, 2, 3]):
            for operation in (TEXT, FORMAT):
                with self.subTest(expected=expected, op=operation['op']):
                    hwp = sections_document()
                    self.check_refusal(request(hwp, 'header:s1:odd', operation, expected_affected_sections=expected),
                                       'affected_sections_mismatch', hwp)
        hwp = sections_document()
        step = request(hwp, 'header:s1:odd', expected_affected_sections=[1, 2])
        # Real topology change in the fake document; the definition bytes/hash do not change.
        hwp.body.append(paragraph('New inheriting section 2025'))
        from tests.native_header_footer_fakes import _SECTION_DEF
        from tests.native_format_fakes import control, char
        hwp.body[-1]['section'] = 3
        hwp.body[-1]['atoms'].insert(0, {'cs': char(), **control(_SECTION_DEF, 'secd')})
        hwp._number_cells(hwp.body)
        self.assertEqual(bound(hwp, 'header:s1:odd')['expected_area_hash'], step['expected_area_hash'])
        self.check_refusal(step, 'affected_sections_mismatch', hwp)

    def test_exact_acknowledgement_preserves_other_areas_and_all_kind_page_scopes(self):
        for kind in ('header', 'footer'):
            for scope in ('both', 'odd', 'even'):
                for operation in (TEXT, FORMAT):
                    with self.subTest(kind=kind, scope=scope, op=operation['op']):
                        hwp = HeaderFooterHwp([paragraph('Body 2025'), paragraph('Inherited body')],
                                             sections=(1,), areas=[area(kind, paragraph('HEADER 2025'), apply=scope.title())])
                        step = request(hwp, f'{kind}:s0:{scope}', operation, expected_affected_sections=[0, 1])
                        before = copy.deepcopy(hwp.body[1])
                        result = Flow(hwp).step(**step)
                        self.assertTrue(result['ok'], result)
                        self.assertEqual(result['steps'][0]['result']['area']['shown_in_sections'], [0, 1])
                        self.assertEqual(hwp.body[1], before)

    def test_ordinary_owner_omission_and_exact_set_work_but_inherited_target_does_not(self):
        for supplied in ({}, {'expected_affected_sections': [0]}):
            hwp = report_document()
            self.assertTrue(Flow(hwp).step(**TEXT, **{k: v for k, v in bound(hwp, 'header:s0:both').items() if k != 'expected_affected_sections'}, old_text='SAMPLE', new_text='UPDATED', **supplied)['ok'])
        hwp = sections_document()
        step = request(hwp, 'header:s1:odd', expected_affected_sections=[1, 2])
        step['section'] = 2
        self.check_refusal(step, 'area_inherited', hwp)

    def test_malformed_sets_are_rejected_by_service_and_mcp_before_native(self):
        for value in (None, [], [True], [1, 1, 2], [2, 1], [-1], [10001], [1.0, 2], '1,2', (1, 2)):
            with self.subTest(value=value):
                hwp = sections_document()
                step = request(hwp, 'header:s1:odd', expected_affected_sections=value)
                with self.assertRaises(ValidationError):
                    Command.model_validate({'session_id': 'a' * 32, 'request': step})
                hwp.calls.clear()
                with self.assertRaises(Exception):
                    Flow(hwp).step(**step)
                self.assertEqual(hwp.calls, [])


class PreservationWalk(unittest.TestCase):
    def test_first_bad_control_observation_holds_without_any_next_sdk_on_owning_sta(self):
        for operation in (TEXT, FORMAT):
            for site in ('HeadCtrl', 'CtrlID', 'Next'):
                for mode in ('malformed', 'throw', 'mid_walk'):
                    with self.subTest(op=operation['op'], site=site, mode=mode):
                        hwp = report_document()
                        fields = {'old_text': 'SAMPLE', 'new_text': 'UPDATED'} if operation is TEXT else {'match': 'REPORT', 'italic': True}
                        step = {**operation, **{k: v for k, v in bound(hwp, 'header:s0:both').items() if k != 'expected_affected_sections'}, **fields}
                        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
                        latched, after, threads = [], [], []
                        reads = []

                        def touch(name):
                            threads.append(threading.get_ident())
                            if latched:
                                after.append(name)

                        def fault():
                            latched.append(site)
                            if mode == 'throw':
                                raise RuntimeError('injected native read failure')
                            return 1

                        class Node:
                            def __init__(self, ctrl, index=0):
                                self.ctrl, self.index = ctrl, index

                            @property
                            def CtrlID(self):
                                touch('CtrlID')
                                if site == 'CtrlID' and not latched and self.index == (1 if mode == 'mid_walk' else 0):
                                    return fault()
                                return self.ctrl.CtrlID

                            @property
                            def Next(self):
                                touch('Next')
                                if site == 'Next' and not latched and self.index == (1 if mode == 'mid_walk' else 0):
                                    return fault()
                                nxt = self.ctrl.Next
                                return Node(nxt, self.index + 1) if nxt is not None else None

                        class Observed(HeaderFooterHwp):
                            @property
                            def HeadCtrl(self):
                                touch('HeadCtrl')
                                ctrl = HeaderFooterHwp.HeadCtrl.fget(self)
                                if site == 'HeadCtrl' and not latched:
                                    # mid_walk cannot apply to the head: exercise the second entered walk instead.
                                    reads.append('HeadCtrl')
                                    if mode != 'mid_walk' or len(reads) == 2:
                                        return fault()
                                return Node(ctrl) if ctrl is not None else None

                        for name in ('get_pos', 'get_selected_pos', 'set_pos', 'select_text', 'get_text_file', 'get_current_page', 'get_cell_addr', 'clear', 'quit'):
                            original = getattr(hwp, name, None)
                            if callable(original):
                                def observed(*args, _name=name, _fn=original, **kwargs):
                                    touch(_name)
                                    return _fn(*args, **kwargs)
                                setattr(hwp, name, observed)
                        for name in ('Run', 'Execute', 'GetDefault'):
                            original = getattr(hwp.HAction, name)
                            def observed(*args, _name=name, _fn=original, **kwargs):
                                touch(_name)
                                return _fn(*args, **kwargs)
                            setattr(hwp.HAction, name, observed)
                        hwp.__class__ = Observed
                        result, error = owned.run(step)
                        self.assertTrue(latched, 'fault must reach actual preservation enumeration')
                        self.assertIsNone(error, error)
                        self.assertFalse(result['ok'], result)
                        self.assertEqual(after, [], 'no restore, diagnostic, Cancel/CloseEx or read after UNKNOWN')
                        self.assertTrue(result.get('native_custody_held'), result)
                        self.assertTrue(owned.session._thread.is_alive())
                        self.assertIs(owned.manager._sessions[owned.sid], owned.session)
                        self.assertTrue(owned.session._quarantined)
                        self.assertEqual(owned.cleanup_events(), [])
                        self.assertEqual(set(threads), {owned.session._thread.ident})


class ComposedHistory(unittest.TestCase):
    def setUp(self):
        self.hwp = report_document()
        self.owned = owned_doc(self, self.hwp, undo_stack=[], redo_stack=[])

    def bundle(self, steps):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.owned.service.command_bundle(steps=steps)

    def history(self, name):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}):
            return getattr(self.owned.service, name)()

    def edit_steps(self):
        return [{**TEXT, **{k: v for k, v in bound(self.hwp, 'header:s0:both').items() if k != 'expected_affected_sections'}, 'old_text': 'SAMPLE', 'new_text': 'UPDATED'},
                {**TEXT, **{k: v for k, v in bound(self.hwp, 'footer:s0:both').items() if k != 'expected_affected_sections'}, 'old_text': 'Sample document', 'new_text': 'Updated footer'}]

    def test_two_actually_dirty_public_steps_refuse_history_before_native_even_with_foreign_change(self):
        result = self.bundle(self.edit_steps())
        self.assertTrue(result['ok'], result)
        self.assertEqual(sum(bool(step['dirty']) for step in result['steps']), 2)
        self.assertEqual(self.hwp.area_text(0), ['UPDATED REPORT'])
        self.assertEqual(self.hwp.area_text(1), ['Updated footer '])
        # An unrelated change must not pass with compared=false or an absent expected hash.
        self.hwp.body[2]['atoms'][0]['c'] = 'X'
        before = self.hwp.get_text_file('HWPML2X', '')
        self.hwp.calls.clear()
        for name in ('undo', 'redo'):
            with self.assertRaises(LocalCliUndoRefused):
                self.history(name)
        self.assertEqual([c for c in self.hwp.calls if c[:2] in [('Run', 'Undo'), ('Run', 'Redo')]], [])
        self.assertEqual(self.hwp.get_text_file('HWPML2X', ''), before)
        self.assertTrue(self.bundle([{'op': 'header_footer_inventory'}])['ok'])

    def test_single_dirty_with_inventory_and_noop_companions_keeps_compared_history(self):
        text = self.edit_steps()[0]
        noop = {**FORMAT, **{k: v for k, v in bound(self.hwp, 'footer:s0:both').items() if k != 'expected_affected_sections'}, 'match': 'Sample', 'italic': False}
        result = self.bundle([{'op': 'header_footer_inventory'}, noop, text, {'op': 'header_footer_inventory'}])
        self.assertTrue(result['ok'], result)
        self.assertEqual(sum(bool(step['dirty']) for step in result['steps']), 1)
        for name in ('undo', 'redo'):
            result = self.history(name)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['confirmation']['expected_state'], {'compared': True, 'matches': True})


if __name__ == '__main__':
    unittest.main()
