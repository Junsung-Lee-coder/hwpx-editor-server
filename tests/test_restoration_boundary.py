"""Where a caret read stops being a clean refusal: reads before anything moved, while pages are found, when the caret is given back,
and when a refusal has to prove it changed nothing.

A read that raises leaves the outcome unknown: the session is held and no further SDK call follows. A read that returns something
that is not an exact position proves nothing, so a give-back it was to verify has failed. Only a refusal whose restoration is proven
is reported as having changed nothing. Through the real service and owning worker over fakes; not a native run.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import native_numbering
from app.local_cli_service import LocalCliServiceError
from tests.native_format_fakes import sample_document as format_document
from tests.native_table_fakes import sample_document
from tests.test_native_paragraph_edit import _doc as paragraph_document
from tests.test_native_table import binding
from tests.test_repair_runtime_safety import owned_doc

NONCANONICAL = {'get_pos': (0, 'x', 0), 'get_selected_pos': ('bad',)}
READS = ('get_pos', 'get_selected_pos')


class ReadFault:
    """Once the caret is set to `at` (a give-back), the n-th caret or selection read raises or returns a non-canonical value.
    Every caret, selection and movement call after the fault is recorded, so a test can prove nothing followed it."""

    def __init__(self, hwp, *, at, occurrence: int, kind: str, armed: bool = False) -> None:
        self.hwp, self.at, self.occurrence, self.kind = hwp, tuple(at), occurrence, kind
        self.armed, self.count, self.fired, self.after = armed, 0, False, []
        self.native_calls_at_fire = None
        for name in (*READS, 'set_pos', 'set_pos_by_set', 'select_text'):
            original = getattr(hwp, name, None)
            if callable(original):
                setattr(hwp, name, self._wrap(name, original))

    def _wrap(self, name, original):
        def call(*args, **kwargs):
            if self.fired:
                self.after.append(name)
            if name == 'set_pos' and tuple(args[:3]) == self.at:
                result = original(*args, **kwargs)
                self.armed = True
                return result
            if name in READS and self.armed and not self.fired:
                self.count += 1
                if self.count == self.occurrence:
                    self.fired = True
                    self.native_calls_at_fire = len(getattr(self.hwp, 'calls', []))
                    if self.kind == 'raise':
                        raise OSError('synthetic native read failure')
                    return NONCANONICAL[name]
            return original(*args, **kwargs)
        return call

    def nothing_followed(self) -> bool:
        return self.after == [] and len(getattr(self.hwp, 'calls', [])) == self.native_calls_at_fire


def caret_of(hwp):
    return tuple(hwp.get_pos())


def table_step(owned, op, **fields):
    with patch('app.local_cli_service.snapshot_live_location', return_value={}):
        return owned.service.command_bundle(steps=[{'op': op, 'label': 't', **fields}])


def review(owned):
    with patch('app.local_cli_service.snapshot_live_location', return_value={}):
        return owned.service.numbering_readback()


def held(test, owned, outcome=None):
    test.assertTrue(owned.session._native_custody_held, outcome)
    if outcome is not None:
        step = outcome['steps'][0]
        test.assertIsNone(step.get('refusal_code'), step)
        test.assertNotIn('verified', step['error'], step)


def clean_refusal(test, owned, outcome, code):
    step = outcome['steps'][0]
    test.assertEqual(step.get('refusal_code'), code, step)
    test.assertFalse(owned.session._native_custody_held)


PATHS = {
    'inventory': lambda owned: table_step(owned, 'table_inventory'),
    'read': lambda owned: table_step(owned, 'table_read', table_id='tbl:1012'),
    'review': review,
}


class GiveBack(unittest.TestCase):
    """The give-back read in inventory, read and review: each of its three reads, raising and non-canonical."""

    def run_path(self, path, *, occurrence, kind):
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        fault = ReadFault(hwp, at=caret_of(hwp), occurrence=occurrence, kind=kind)
        if path == 'review':
            with self.assertRaises(LocalCliServiceError):
                PATHS[path](owned)
            outcome = None
        else:
            outcome = PATHS[path](owned)
            self.assertFalse(outcome['ok'])
        self.assertTrue(fault.fired, path)
        return owned, fault, outcome

    def test_a_raising_give_back_read_is_unknown_with_no_next_call(self) -> None:
        for path in PATHS:
            for occurrence in (1, 2, 3):
                with self.subTest(path=path, occurrence=occurrence):
                    owned, fault, outcome = self.run_path(path, occurrence=occurrence, kind='raise')
                    held(self, owned, outcome)
                    self.assertTrue(fault.nothing_followed(), fault.after)

    def test_a_non_canonical_give_back_read_is_a_failed_restoration(self) -> None:
        for path in PATHS:
            for occurrence in (1, 2, 3):
                with self.subTest(path=path, occurrence=occurrence):
                    owned, fault, outcome = self.run_path(path, occurrence=occurrence, kind='noncanonical')
                    held(self, owned, outcome)
                    if outcome is not None:
                        self.assertIn('LocalCliRuntimeError', outcome['steps'][0]['error'])


class PageScan(unittest.TestCase):
    def test_a_read_failure_while_finding_pages_gives_nothing_back(self) -> None:
        for path in PATHS:
            with self.subTest(path=path):
                hwp = sample_document()
                owned = owned_doc(self, hwp)
                start = caret_of(hwp)
                fault = ReadFault(hwp, at=start, occurrence=99, kind='raise')

                def failing_reader(native):
                    def read():
                        raise native_numbering.NativeNumberingReadFailure('synthetic page read failure', code='target_unlocatable')
                    return read
                owned.service._table_page_reader = failing_reader
                if path == 'review':
                    with self.assertRaises(LocalCliServiceError):
                        PATHS[path](owned)
                else:
                    outcome = PATHS[path](owned)
                    held(self, owned, outcome)
                self.assertTrue(owned.session._native_custody_held)
                self.assertFalse(fault.armed, 'the caret was given back after a read failure')

    def test_a_refusal_while_finding_pages_whose_give_back_fails_is_not_a_clean_refusal(self) -> None:
        for path in ('inventory', 'read', 'review'):
            for kind in ('raise', 'noncanonical'):
                with self.subTest(path=path, kind=kind):
                    hwp = sample_document()
                    owned = owned_doc(self, hwp)
                    fault = ReadFault(hwp, at=caret_of(hwp), occurrence=1, kind=kind)

                    def refusing_reader(native):
                        def read():
                            raise native_numbering.NativeNumberingRefusal('synthetic page refusal', code='target_unlocatable')
                        return read
                    owned.service._table_page_reader = refusing_reader
                    if path == 'review':
                        with self.assertRaises(LocalCliServiceError):
                            PATHS[path](owned)
                    else:
                        held(self, owned, PATHS[path](owned))
                    self.assertTrue(fault.fired)
                    self.assertTrue(owned.session._native_custody_held)

    def test_a_refusal_while_finding_pages_with_a_proven_give_back_stays_clean(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        start = caret_of(hwp)
        hwp.control_id_override = 7
        clean_refusal(self, owned, table_step(owned, 'table_inventory'), 'control_enumeration_unavailable')
        self.assertEqual(caret_of(hwp), start)
        facts = review(owned)
        self.assertEqual(facts['tables_pages_reason'], 'control_enumeration_unavailable')
        self.assertFalse(owned.session._native_custody_held)
        hwp.control_id_override = None
        self.assertTrue(table_step(owned, 'table_inventory')['ok'])


class SetPos(unittest.TestCase):
    def test_a_give_back_set_pos_that_fails_or_raises_is_unknown(self) -> None:
        for path in PATHS:
            for behaviour in ('false', 'raise'):
                with self.subTest(path=path, behaviour=behaviour):
                    hwp = sample_document()
                    owned = owned_doc(self, hwp)
                    start = caret_of(hwp)
                    original = hwp.set_pos

                    def set_pos(*args, behaviour=behaviour, original=original, start=start):
                        if tuple(args[:3]) == start:
                            if behaviour == 'raise':
                                raise OSError('synthetic SetPos failure')
                            return False
                        return original(*args)
                    hwp.set_pos = set_pos
                    if path == 'review':
                        with self.assertRaises(LocalCliServiceError):
                            PATHS[path](owned)
                    else:
                        held(self, owned, PATHS[path](owned))
                    self.assertTrue(owned.session._native_custody_held)


class FirstRead(unittest.TestCase):
    """Before anything moved: a raising read is unknown, a non-canonical one refuses with nothing to give back."""

    def test_a_raising_first_read_is_unknown(self) -> None:
        for path in PATHS:
            for occurrence in (1, 2, 3):
                with self.subTest(path=path, occurrence=occurrence):
                    hwp = sample_document()
                    owned = owned_doc(self, hwp)
                    fault = ReadFault(hwp, at=(-1, -1, -1), occurrence=occurrence, kind='raise', armed=True)
                    if path == 'review':
                        with self.assertRaises(LocalCliServiceError):
                            PATHS[path](owned)
                    else:
                        held(self, owned, PATHS[path](owned))
                    self.assertTrue(fault.nothing_followed(), fault.after)

    def test_a_non_canonical_first_read_moves_nothing(self) -> None:
        for path in ('inventory', 'read'):
            with self.subTest(path=path):
                hwp = sample_document()
                owned = owned_doc(self, hwp)
                fault = ReadFault(hwp, at=(-1, -1, -1), occurrence=1, kind='noncanonical', armed=True)
                clean_refusal(self, owned, PATHS[path](owned), 'target_unlocatable')
                self.assertNotIn('set_pos_by_set', fault.after)
                self.assertNotIn('set_pos', fault.after)
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        fault = ReadFault(hwp, at=(-1, -1, -1), occurrence=7, kind='noncanonical', armed=True)  # The table facts' own caret read, after the readback's two.
        facts = review(owned)
        self.assertEqual(facts['tables_pages_reason'], 'caret_unreadable')
        self.assertNotIn('set_pos_by_set', fault.after)
        self.assertFalse(owned.session._native_custody_held)


class EarlierWork(unittest.TestCase):
    def test_a_failed_give_back_after_an_edit_keeps_the_edit_and_the_debt(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        fields = binding(hwp, '1012')
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            self.assertTrue(owned.service.command_bundle(steps=[{'op': 'table_style_exact', 'label': 'a', **fields, 'scope': 'cell', 'cell': 'B2', 'fill': '#FFF2CC'}])['ok'])
        ReadFault(hwp, at=caret_of(hwp), occurrence=1, kind='noncanonical')
        held(self, owned, table_step(owned, 'table_inventory'))
        self.assertIsNotNone(hwp.by_inst('1012').named('B2').fill)
        self.assertTrue(owned.session._normal_close_requires_save)
        with self.assertRaises(Exception):
            table_step(owned, 'table_inventory')

    def test_after_a_clean_refusal_work_goes_on(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        clean_refusal(self, owned, table_step(owned, 'table_read', table_id='tbl:9999'), 'table_not_found')
        self.assertTrue(table_step(owned, 'table_inventory')['ok'])
        self.assertTrue(review(owned)['tables'])


class EditRefusal(unittest.TestCase):
    """A table edit refused after it moved: the give-back and the proof that nothing changed must both hold before it is clean."""

    def refuse(self, owned, fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            return owned.service.command_bundle(steps=[{'op': 'table_style_exact', 'label': 'a', **fields, 'scope': 'cell', 'cell': 'B2', 'borders': [{'edges': ['top'], 'line': 'none'}]}])

    def test_the_refusal_is_clean_when_its_give_back_is_proven(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        clean_refusal(self, owned, self.refuse(owned, binding(hwp, '1015')), 'edge_held_by_wider_neighbour')

    def test_a_give_back_that_cannot_be_proven_is_not_clean(self) -> None:
        for kind in ('raise', 'noncanonical'):
            for occurrence in (1, 2, 3):
                with self.subTest(kind=kind, occurrence=occurrence):
                    hwp = sample_document()
                    owned = owned_doc(self, hwp)
                    fields = binding(hwp, '1015')
                    fault = ReadFault(hwp, at=caret_of(hwp), occurrence=occurrence, kind=kind)
                    held(self, owned, self.refuse(owned, fields))
                    self.assertTrue(fault.fired)

    def test_a_no_change_proof_that_cannot_be_read_is_not_clean(self) -> None:
        hwp = sample_document()
        owned = owned_doc(self, hwp)
        fields = binding(hwp, '1015')
        start = caret_of(hwp)
        original = hwp.set_pos

        def set_pos(*args):
            result = original(*args)
            if tuple(args[:3]) == start:
                hwp.control_id_override = 7  # The census read after the give-back cannot enumerate the controls.
            return result
        hwp.set_pos = set_pos
        held(self, owned, self.refuse(owned, fields))


class Siblings(unittest.TestCase):
    """Formatting and paragraph edits give the caret back the same way and keep the same boundary."""

    def format_refusal(self, hwp, owned):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            return owned.service.command_bundle(steps=[{'op': 'character_format_exact', 'label': 'f', 'expected_page': 1, 'confirm_layout': True, 'match': '없는 문구', 'bold': True}])

    def paragraph_refusal(self, hwp, owned):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            return owned.service.command_bundle(steps=[{'op': 'paragraph_insert_exact', 'label': 'p', 'expected_page': 1, 'confirm_layout': True, 'match': 'Item', 'text': '새', 'position': 'after'}])

    def test_their_clean_refusals_stay_clean(self) -> None:
        hwp = format_document()
        owned = owned_doc(self, hwp)
        clean_refusal(self, owned, self.format_refusal(hwp, owned), 'target_not_found')
        hwp = paragraph_document('Title', 'Item', 'Item again')
        owned = owned_doc(self, hwp)
        clean_refusal(self, owned, self.paragraph_refusal(hwp, owned), 'target_ambiguous')

    def test_a_give_back_that_cannot_be_proven_is_not_clean(self) -> None:
        for name, make, refuse in (('format', format_document, self.format_refusal), ('paragraph', lambda: paragraph_document('Title', 'Item', 'Item again'), self.paragraph_refusal)):
            for kind in ('raise', 'noncanonical'):
                for occurrence in (1, 2, 3):
                    with self.subTest(name=name, kind=kind, occurrence=occurrence):
                        hwp = make()
                        owned = owned_doc(self, hwp)
                        fault = ReadFault(hwp, at=caret_of(hwp), occurrence=occurrence, kind=kind)
                        held(self, owned, refuse(hwp, owned))
                        self.assertTrue(fault.fired)

    def test_a_format_no_change_proof_that_cannot_be_read_is_not_clean(self) -> None:
        hwp = format_document()
        owned = owned_doc(self, hwp)
        start = caret_of(hwp)
        original_set = hwp.set_pos
        original_text = hwp.get_text_file
        state = {'after': False}

        def set_pos(*args):
            result = original_set(*args)
            if tuple(args[:3]) == start:
                state['after'] = True
            return result

        def get_text_file(*args, **kwargs):
            if state['after']:
                return '<not a document'  # Returned, unusable: the proof cannot be made.
            return original_text(*args, **kwargs)
        hwp.set_pos = set_pos
        hwp.get_text_file = get_text_file
        held(self, owned, self.format_refusal(hwp, owned))


if __name__ == '__main__':
    unittest.main()
