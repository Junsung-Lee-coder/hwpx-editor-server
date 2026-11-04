"""Undo and Redo of object insertion and layout edits as one logical request each, compared with the recorded document states.

Measured on Hancom 2024 13.0.0.3457 through pyhwpx 1.7.2: each editing action an insertion or layout edit reports is one native
Undo step (Cancel and CloseEx are not), Undo returns to the recorded state, and Redo of a re-created object can renew its
instance id, z-order or header/footer series number. The SDK here is tests.native_text_fakes; source behavior only.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import local_cli_service as service_module
from app import native_format
from tests.native_numbering_fakes import FakeCtrl, chain
from tests.native_text_fakes import TextHwp, plan_document
from tests.test_repair_runtime_safety import owned_doc

STATES = {'pre_state_sha256': 'a' * 64, 'post_state_sha256': 'b' * 64}


def body_text(hwp: TextHwp, para: int) -> str:
    return ''.join(c for c, _props in hwp.text_props(0, para))


class Policy(unittest.TestCase):
    def step(self, op: str, undo: dict, ok: bool = True) -> dict:
        return {'op': op, 'ok': ok, 'dirty': True, 'result': {'undo': undo}}

    def test_insertion_and_layout_count_their_reported_actions_when_both_states_were_recorded(self) -> None:
        for op in ('object_insert_exact', 'layout_exact'):
            with self.subTest(op=op):
                self.assertTrue(service_module._bundle_step_single_undo(self.step(op, {'native_editing_actions': 2, **STATES})))
                self.assertEqual(service_module._native_undo_steps([self.step(op, {'native_editing_actions': 2, **STATES})]), 2)
                self.assertFalse(service_module._bundle_step_single_undo(self.step(op, {'native_editing_actions': 1})))
                self.assertFalse(service_module._bundle_step_single_undo(self.step(op, {'native_editing_actions': 1, 'pre_state_sha256': 'a' * 64})))
                self.assertFalse(service_module._bundle_step_single_undo(self.step(op, {'native_editing_actions': 0, **STATES})))
                self.assertFalse(service_module._bundle_step_single_undo(self.step(op, {'native_editing_actions': True, **STATES})))
                self.assertFalse(service_module._bundle_step_single_undo(self.step(op, {'native_editing_actions': 1, **STATES}, ok=False)))

    def test_a_recorded_renewal_free_state_is_kept_for_redo(self) -> None:
        steps = [self.step('object_insert_exact', {'native_editing_actions': 1, **STATES, 'post_renewal_free_sha256': 'c' * 64})]
        self.assertEqual(service_module._recorded_states(steps), {'pre': 'a' * 64, 'post': 'b' * 64, 'post_renewed': 'c' * 64})

    def test_renewed_identity_alone_is_left_out_of_the_tolerant_digest(self) -> None:
        hwp = plan_document()
        text = hwp.get_text_file('HWPML2X', '')
        controls = ['secd', 'cold', 'head', 'foot']
        renewed = text.replace('InstId="', 'InstId="9', 1)
        self.assertNotEqual(renewed, text)
        self.assertEqual(native_format.renewal_free_digest(text, controls), native_format.renewal_free_digest(renewed, controls))
        changed = text.replace('Annual Plan 2025', 'Annual Plan 2026', 1)
        self.assertNotEqual(native_format.renewal_free_digest(text, controls), native_format.renewal_free_digest(changed, controls))
        state = native_format.history_state(text, lambda: controls)
        self.assertEqual(set(state), {'sha', 'renewal_free_sha'})
        self.assertIsNone(native_format.history_state('<HWPML/>', lambda: controls))

        def unreadable() -> list[str]:
            from app.native_numbering import ControlEnumerationUnproven
            raise ControlEnumerationUnproven('not exact text')
        self.assertIsNone(native_format.history_state(text, unreadable))


class Session(unittest.TestCase):
    def setUp(self) -> None:
        self.hwp = plan_document()
        self.owned = owned_doc(self, self.hwp, undo_stack=[], redo_stack=[])

    def step(self, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def history(self, name):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}):
            return getattr(self.owned.service, name)()

    def insert(self, kind: str, **fields):
        self.hwp.set_pos(0, 10, 4)
        return self.step(op='object_insert_exact', kind=kind, expected_pos=[0, 10, 4], confirm_mutation=True, **fields)

    def history_refusal(self, name: str) -> str | None:
        try:
            self.history(name)
        except Exception as exc:  # noqa: BLE001 - the service raises its refusal type
            return getattr(exc, 'code', type(exc).__name__)
        return None

    def test_insertion_undo_redo_then_a_later_edit_and_its_undo(self) -> None:
        original = [body_text(self.hwp, number) for number in range(11)]
        inserted = self.insert('equation', script='a+b')
        self.assertTrue(inserted['ok'], inserted)
        report = inserted['steps'][0]['result']['undo']
        self.assertEqual((report['native_editing_actions'], report['hwpx_undo'], report['undo_units_verified']), (1, 'counted', True))
        self.assertRegex(report['post_renewal_free_sha256'], r'^[0-9a-f]{64}$')
        with_equation = [atom.get('ctrl') for atom in self.hwp.atoms_of(0, 10)]
        self.assertIn('eqed', with_equation)
        undone = self.history('undo')
        self.assertTrue(undone['ok'], undone)
        self.assertEqual(undone['native_undo_steps'], 1)
        self.assertEqual(undone['confirmation']['expected_state'], {'compared': True, 'matches': True})
        self.assertNotIn('eqed', [atom.get('ctrl') for atom in self.hwp.atoms_of(0, 10)])
        self.hwp.renew_on_redo = True
        redone = self.history('redo')
        self.assertTrue(redone['ok'], redone)
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True, 'renewed': ['instid', 'seriesnum', 'zorder']})
        self.assertEqual([atom.get('ctrl') for atom in self.hwp.atoms_of(0, 10)], with_equation)
        later = self.step(op='text_replace_exact', expected_page=1, confirm_layout=True, match='Annual Plan 2025', old_text='2025', new_text='2026')
        self.assertTrue(later['ok'], later)
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual(body_text(self.hwp, 0), 'Annual Plan 2025')
        self.assertIn('eqed', [atom.get('ctrl') for atom in self.hwp.atoms_of(0, 10)])
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual([body_text(self.hwp, number) for number in range(11)], original)
        self.assertNotIn('eqed', [atom.get('ctrl') for atom in self.hwp.atoms_of(0, 10)])
        self.assertEqual(self.history_refusal('undo'), 'no_undo_available')

    def test_a_bookmark_comes_back_exactly(self) -> None:
        self.assertTrue(self.insert('bookmark', name='plan_mark')['ok'])
        self.assertTrue(self.history('undo')['ok'])
        redone = self.history('redo')
        self.assertEqual(redone['confirmation']['expected_state'], {'compared': True, 'matches': True})

    def test_redo_reaching_other_content_is_reported_and_stops_history(self) -> None:
        self.assertTrue(self.insert('equation', script='a+b')['ok'])
        self.assertTrue(self.history('undo')['ok'])

        def damage(native: TextHwp) -> None:
            for atom in native.atoms_of(0, 10):
                if atom.get('ctrl') == 'eqed':
                    atom['xml'] = atom['xml'].replace('<SCRIPT>a+b</SCRIPT>', '<SCRIPT>a-b</SCRIPT>')
        original_step = self.hwp.history_step

        def redo_then_damage(source, target, *, after_undo):
            done = original_step(source, target, after_undo=after_undo)
            if not after_undo:
                damage(self.hwp)
            return done
        self.hwp.history_step = redo_then_damage
        redone = self.history('redo')
        self.assertFalse(redone['ok'], redone)
        self.assertIs(redone['confirmation']['expected_state']['matches'], False)
        self.assertIn('redo_state_mismatch', redone['detail'])
        self.assertEqual(self.history_refusal('undo'), 'undo_not_counted')

    def test_undo_on_a_document_whose_controls_cannot_be_read_calls_no_native_undo(self) -> None:
        self.assertTrue(self.step(op='text_replace_exact', expected_page=1, confirm_layout=True, old_text='budget', new_text='costs')['ok'])
        plain = type(self.hwp).HeadCtrl

        class Unreadable(TextHwp):
            @property
            def HeadCtrl(self):  # noqa: N802
                return chain([FakeCtrl('secd'), FakeCtrl('')])
        self.hwp.__class__ = Unreadable
        self.hwp.calls.clear()
        self.assertEqual(self.history_refusal('undo'), 'undo_evidence_unavailable')
        self.assertNotIn(('Run', 'Undo'), self.hwp.calls)
        self.hwp.__class__ = TextHwp
        self.assertIsNotNone(plain)
        self.assertTrue(self.history('undo')['ok'])

    def test_an_insertion_whose_controls_cannot_be_read_afterwards_is_not_counted(self) -> None:
        def unreadable_after(native: TextHwp) -> None:
            native.__class__ = _UnreadableAfterInsert
        self.hwp.after_execute = unreadable_after
        inserted = self.insert('bookmark', name='plan_mark')
        self.assertTrue(inserted['ok'], inserted)
        report = inserted['steps'][0]['result']['undo']
        self.assertEqual(report['hwpx_undo'], 'refused')
        self.assertIs(report['undo_units_verified'], False)
        self.hwp.__class__ = TextHwp
        self.hwp.after_execute = None
        self.assertEqual(self.history_refusal('undo'), 'undo_not_counted')
        self.assertNotIn(('Run', 'Undo'), self.hwp.calls)


class _UnreadableAfterInsert(TextHwp):
    @property
    def HeadCtrl(self):  # noqa: N802
        return chain([FakeCtrl('secd'), FakeCtrl('bokm'), FakeCtrl('')])


class LayoutReport(unittest.TestCase):
    def test_a_layout_edit_records_both_states_when_the_document_is_evidence(self) -> None:
        from tests.test_layout_exact import _FakeLayoutHwp, _page_step, _run
        hwp = _FakeLayoutHwp()
        states = iter([{'sha': 'a' * 64, 'renewal_free_sha': 'c' * 64}, {'sha': 'b' * 64, 'renewal_free_sha': 'd' * 64}])
        with patch('app.local_cli_layout.history_state', side_effect=lambda text, controls: next(states)):
            result = _run(hwp, _page_step())
        self.assertEqual(result['undo'], {'native_editing_actions': 1, 'pre_state_sha256': 'a' * 64, 'post_state_sha256': 'b' * 64,
                                          'post_renewal_free_sha256': 'd' * 64, 'hwpx_undo': 'counted', 'undo_units_verified': True,
                                          'note': result['undo']['note']})

    def test_a_layout_edit_on_a_document_that_is_no_evidence_stays_refused(self) -> None:
        from tests.test_layout_exact import _FakeLayoutHwp, _page_step, _run
        result = _run(_FakeLayoutHwp(), _page_step())
        self.assertEqual(result['undo']['hwpx_undo'], 'refused')
        self.assertIs(result['undo']['undo_units_verified'], False)


if __name__ == '__main__':
    unittest.main()
