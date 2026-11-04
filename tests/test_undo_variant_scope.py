"""Redo tolerance belongs to the object an insertion re-created, layout Redo is exact, and undo is counted per measured variant.

Measured on Hancom 2024 13.0.0.3457 (owned characterization beside existing tables and pictures): Redo of an inserted
equation brings it back with a new z-order; of a line, rectangle or ellipse also with a new drawing instance id (and an
ellipse with new unused arc points); of a header or footer with a new series number. Nothing else differed, and no layout
edit's Redo differed at all. Columns from_caret_new changed the existing column definition instead of adding one, so it
is refused before any edit and has nothing to undo. The SDK here is tests.native_text_fakes and the layout fake; source behavior only, not Hancom
evidence.
"""
from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from app import local_cli_service as service_module
from app import native_format
from tests.native_text_fakes import TextHwp, plan_document
from tests.test_repair_runtime_safety import owned_doc

PICTURE_PARAGRAPH = 6  # body paragraph 6 of plan_document: 'Before', a picture, 'Text after the picture'
HEADER = '<HEADER ApplyPageType="Both" SeriesNum="0">'


def _bump(xml: str, attribute: str) -> str:
    changed = re.sub(attribute + r'="(\d+)"', lambda m: f'{attribute}="{int(m.group(1)) + 1}"', xml, count=1)
    assert changed != xml, attribute
    return changed


class _Session(unittest.TestCase):
    def setUp(self) -> None:
        self.hwp = plan_document()
        self.owned = owned_doc(self, self.hwp, undo_stack=[], redo_stack=[])
        self.header_renewed = False
        plain = self.hwp.get_text_file

        def text(fmt: str = 'UNICODE', option: str = '') -> str:
            out = plain(fmt, option)
            return out.replace(HEADER, HEADER.replace('SeriesNum="0"', 'SeriesNum="1"')) if self.header_renewed else out
        self.hwp.get_text_file = text

    def step(self, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def history(self, name):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}):
            return getattr(self.owned.service, name)()

    def history_refusal(self, name: str) -> str | None:
        try:
            self.history(name)
        except Exception as exc:  # noqa: BLE001 - the service raises its refusal type
            return getattr(exc, 'code', type(exc).__name__)
        return None

    def insert_equation(self):
        self.hwp.set_pos(0, PICTURE_PARAGRAPH, 4)
        inserted = self.step(op='object_insert_exact', kind='equation', script='a+b', expected_pos=[0, PICTURE_PARAGRAPH, 4], confirm_mutation=True)
        self.assertTrue(inserted['ok'], inserted)
        self.assertEqual(inserted['steps'][0]['result']['undo']['hwpx_undo'], 'counted')
        return inserted

    def on_redo(self, change) -> None:
        original = self.hwp.history_step

        def step(source, target, *, after_undo):
            done = original(source, target, after_undo=after_undo)
            if done and not after_undo:
                change(self.hwp)
            return done
        self.hwp.history_step = step

    def picture(self, attribute: str):
        def change(native: TextHwp) -> None:
            atom = next(atom for atom in native.atoms_of(0, PICTURE_PARAGRAPH) if atom.get('ctrl') == 'gso')
            atom['xml'] = _bump(atom['xml'], attribute)
        return change

    def equation(self, attribute: str):
        def change(native: TextHwp) -> None:
            atom = next(atom for atom in native.atoms_of(0, PICTURE_PARAGRAPH) if atom.get('ctrl') == 'eqed')
            atom['xml'] = _bump(atom['xml'], attribute)
        return change

    def assert_redo_mismatch_holds_history(self) -> None:
        redone = self.history('redo')
        self.assertFalse(redone['ok'], redone)
        self.assertIs(redone['confirmation']['expected_state']['matches'], False)
        self.assertIn('redo_state_mismatch', redone['detail'])
        # No follow-on: the history this session recorded no longer describes the document.
        self.assertEqual(self.history_refusal('undo'), 'undo_not_counted')


class InsertionRedo(_Session):
    def test_a_pre_existing_pictures_stacking_order_changed_on_redo_is_a_mismatch(self) -> None:
        self.insert_equation()
        self.assertTrue(self.history('undo')['ok'])
        self.on_redo(self.picture('ZOrder'))
        self.assert_redo_mismatch_holds_history()

    def test_a_pre_existing_pictures_object_instance_id_changed_on_redo_is_a_mismatch(self) -> None:
        self.insert_equation()
        self.assertTrue(self.history('undo')['ok'])
        self.on_redo(self.picture('InstId'))
        self.assert_redo_mismatch_holds_history()

    def test_a_pre_existing_pictures_drawing_instance_id_changed_on_redo_is_a_mismatch(self) -> None:
        self.insert_equation()
        self.assertTrue(self.history('undo')['ok'])
        self.on_redo(self.picture('InstID'))
        self.assert_redo_mismatch_holds_history()

    def test_an_existing_headers_series_number_changed_on_redo_is_a_mismatch(self) -> None:
        self.insert_equation()
        self.assertTrue(self.history('undo')['ok'])

        def renew_header(_native: TextHwp) -> None:
            self.header_renewed = True
        self.on_redo(renew_header)
        self.assert_redo_mismatch_holds_history()

    def test_an_attribute_the_recreated_equation_was_not_measured_to_renew_is_a_mismatch(self) -> None:
        self.insert_equation()
        self.assertTrue(self.history('undo')['ok'])
        self.on_redo(self.equation('InstId'))
        self.assert_redo_mismatch_holds_history()

    def test_the_recreated_equations_new_z_order_is_accepted_and_named(self) -> None:
        self.insert_equation()
        self.assertTrue(self.history('undo')['ok'])
        self.on_redo(self.equation('ZOrder'))
        redone = self.history('redo')
        self.assertTrue(redone['ok'], redone)
        state = redone['confirmation']['expected_state']
        self.assertEqual(state['renewed_object'], {'element': 'EQUATION', 'ordinal': 0})
        self.assertEqual(state['renewed'], [{'element': 'EQUATION/SHAPEOBJECT', 'attribute': 'ZOrder', 'recorded': '7', 'now': '8'}])
        # The session goes on: a later edit, then its undo, then the insertion's undo back to the opened document.
        later = self.step(op='text_replace_exact', expected_page=1, confirm_layout=True, match='Annual Plan 2025', old_text='2025', new_text='2026')
        self.assertTrue(later['ok'], later)
        self.assertTrue(self.history('undo')['ok'])
        self.assertTrue(self.history('undo')['ok'])
        self.assertNotIn('eqed', [atom.get('ctrl') for atom in self.hwp.atoms_of(0, PICTURE_PARAGRAPH)])

    def test_a_redo_that_reaches_the_recorded_state_names_no_renewal(self) -> None:
        self.insert_equation()
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual(self.history('redo')['confirmation']['expected_state'], {'compared': True, 'matches': True})


class LayoutRedo(_Session):
    """The real layout engine's undo report, recorded into this session's history over a real paragraph-shape edit."""

    def layout_edit(self, **step) -> dict:
        from tests.test_layout_exact import _FakeLayoutHwp, _page_step, _run
        controls = [name for _control, name in __import__('app.native_numbering', fromlist=['x'])._control_ids(self.hwp)]  # noqa: SLF001
        texts = [self.hwp.get_text_file('HWPML2X', '')]
        self.hwp.set_pos(0, 1, 0)
        self.hwp.execute('ParagraphShape', {'LeftMargin': 2000})
        texts.append(self.hwp.get_text_file('HWPML2X', ''))
        states = iter(texts)
        with patch('app.local_cli_layout.history_state', side_effect=lambda _text, _controls: native_format.history_state(next(states), lambda: controls)):
            result = _run(_FakeLayoutHwp(), _page_step(**step))
        self.assertEqual(result['undo']['hwpx_undo'], 'counted', result['undo'])
        binding = self.owned.store
        service_module._record_bundle_undo_state(binding, [{'op': 'layout_exact', 'ok': True, 'dirty': True, 'result': result}], {})
        return result

    def test_a_layout_redo_with_any_renewed_identity_is_a_mismatch(self) -> None:
        self.layout_edit()
        self.assertTrue(self.history('undo')['ok'])
        self.on_redo(self.picture('ZOrder'))
        self.assert_redo_mismatch_holds_history()

    def test_a_layout_redo_reaching_the_recorded_state_matches_exactly(self) -> None:
        self.layout_edit()
        self.assertTrue(self.history('undo')['ok'])
        self.assertEqual(self.history('redo')['confirmation']['expected_state'], {'compared': True, 'matches': True})


class LayoutUndoVariants(unittest.TestCase):
    """Undo is counted only for a layout variant with a measured native Undo; the rest refuse before any history call."""

    def states(self):
        values = iter([{'sha': 'a' * 64, 'renewal_free_sha': 'c' * 64}, {'sha': 'b' * 64, 'renewal_free_sha': 'd' * 64}])
        return patch('app.local_cli_layout.history_state', side_effect=lambda _text, _controls: next(values))

    def test_columns_from_caret_new_is_refused_before_any_edit_so_nothing_is_undone(self) -> None:
        from app.step_refusal import LocalCliStepRefusal
        from tests.test_layout_exact import _FakeLayoutHwp, _run
        hwp = _FakeLayoutHwp()
        with self.states(), self.assertRaises(LocalCliStepRefusal) as caught:
            _run(hwp, {'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 3, 'gap_mm': 8.0, 'apply_to': 'from_caret_new'})
        self.assertEqual(caught.exception.code, 'columns_from_caret_new_unsupported')
        self.assertEqual(hwp.mutations(), [])

    def test_columns_in_the_current_section_report_a_counted_undo(self) -> None:
        from tests.test_layout_exact import _FakeLayoutHwp, _run
        with self.states():
            result = _run(_FakeLayoutHwp(), {'kind': 'columns', 'expected_pos': [0, 0, 0], 'confirm_layout': True, 'count': 2, 'gap_mm': 8.0})
        self.assertEqual((result['undo']['hwpx_undo'], result['undo']['native_editing_actions']), ('counted', 1), result['undo'])
        self.assertNotIn('post_renewal_free_sha256', result['undo'])


if __name__ == '__main__':
    unittest.main()
