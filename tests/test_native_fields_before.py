"""ACTUAL native BEFORE data of four click-here fields through the real field engine, census and service.

tests/fixtures/native_fields holds the GetTextFile('HWPML2X') string Hancom 13.0.0.3457 returned for a native-authored
document, and the raw field values read on the same open (MANIFEST.json). No fill was run natively on it, so there is
no after state: the replay stops at the one PutFieldText, which the recorded object answers with AfterStateNotCaptured,
never with an invented after state. The field-list forms and the FieldExist answer the native session did not read are
DERIVED below and labelled so; the caret and selection are the fake's own, and the fill walks no live control list. This is eligibility evidence
for the actual before state, not field-fill, save or render evidence.
"""
from __future__ import annotations

import hashlib
import json
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app import hwpx_saved_proof as proof
from app import native_fields
from app.local_cli_bundle_fields import LocalCliBundleFieldsMixin
from tests.native_fields_fakes import FieldsHwp, click_here
from tests.native_format_fakes import paragraph
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc

FIXTURE = Path(__file__).parent / 'fixtures' / 'native_fields'
MANIFEST = json.loads((FIXTURE / 'MANIFEST.json').read_text(encoding='utf-8'))
ROW = MANIFEST['files']['before.hwpml2x']
OBSERVED = MANIFEST['raw_field_api_same_open']
NUMBERED = OBSERVED['GetFieldList(1, 2)']
VALUES = OBSERVED['GetFieldText']
SELECTORS = NUMBERED.split('\x02')


def producer_string() -> str:
    """The actual COM string, restored by the qualified UTF-8 reader (its UTF-16 declaration is not a byte decoding)."""
    return proof.producer_text((FIXTURE / 'before.hwpml2x').read_bytes(), **ROW['producer_witness'])


def _counted(numbered: str) -> str:
    counts: dict[str, int] = {}
    for item in numbered.split('\x02'):
        name = item[:item.rindex('{{')]
        counts[name] = counts.get(name, 0) + 1
    return '\x02'.join(f'{name}{{{{{count}}}}}' for name, count in counts.items())


# DERIVED, not read natively: the actual HWPML2X and HWPX hold no field other than these four click-here fields, so
# option 0 (every field) lists what option 2 (click-here) listed, options 1 (cell) and 8 (text box) list nothing, and
# the counted form (number 2) counts the observed numbered list. FieldExist is documented BOOL; True is derived from the list.
LISTS = {(1, 2): NUMBERED, (1, 0): NUMBERED, (2, 0): _counted(NUMBERED), (2, 2): _counted(NUMBERED), (2, 1): '', (2, 8): ''}
EXISTS = {item[:item.rindex('{{')]: True for item in SELECTORS}


class AfterStateNotCaptured(RuntimeError):
    """The native session ran no fill, so nothing is known after a PutFieldText."""


class RecordedCom:
    """The raw COM object answering only what the native session read (or the derived forms above)."""

    def __init__(self, owner: 'RecordedHwp') -> None:
        self.owner = owner
        self.calls: list[tuple] = []

    def _log(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        self.owner.calls.append(('com', name, *args))

    def GetFieldList(self, number: int, option: int) -> str:  # noqa: N802
        self._log('GetFieldList', number, option)
        return LISTS[(number, option)]

    def GetFieldText(self, field: str) -> str:  # noqa: N802
        self._log('GetFieldText', field)
        return VALUES[field]

    def FieldExist(self, field: str) -> bool:  # noqa: N802
        self._log('FieldExist', field)
        return EXISTS[field]

    def PutFieldText(self, field: str, text: str) -> None:  # noqa: N802
        self._log('PutFieldText', field, text)
        raise AfterStateNotCaptured('no native after state was captured for this document')


def mirror() -> list[dict[str, Any]]:
    """The actual paragraph's text and fields as the fake's own atoms: only for the fake caret, selection and (unused by the fill) control walk."""
    root = ET.fromstring(producer_string())
    runs: list[Any] = []
    children = list(root.find('BODY/SECTION/P/TEXT'))
    index = 0
    while index < len(children):
        item = children[index]
        if item.tag == 'CHAR':
            runs.append(item.text or '')
        elif item.tag == 'FIELDBEGIN':
            text = children[index + 1].text if children[index + 1].tag == 'CHAR' else ''
            runs.extend(click_here(item.get('Name'), text or '', inst=int(item.get('InstId'))))
            index += 1 if children[index + 1].tag == 'CHAR' else 0
        index += 1
    return [paragraph(*runs)]


class RecordedHwp(FieldsHwp):
    def __init__(self) -> None:
        super().__init__(mirror())
        self.hwp = RecordedCom(self)
        self.native = producer_string()

    def get_text_file(self, fmt: str = 'UNICODE', option: str = '') -> str:
        self.calls.append(('get_text_file', fmt, option))
        if (fmt, option) != ('HWPML2X', ''):
            raise AssertionError(f'not captured natively: GetTextFile({fmt!r}, {option!r})')
        return self.native


def reads(hwp: RecordedHwp) -> dict[str, Any]:
    return LocalCliBundleFieldsMixin()._fields_read_state(hwp)()  # noqa: SLF001


def inventory(hwp: RecordedHwp) -> dict[str, Any]:
    outcome = Flow(hwp).step(op='field_inventory')
    assert outcome['ok'], outcome
    return outcome['steps'][0]['result']


READS = {'GetFieldList', 'GetFieldText', 'FieldExist'}


class ActualBeforeString(unittest.TestCase):
    def test_the_fixture_restores_the_actual_com_string(self) -> None:
        raw = (FIXTURE / 'before.hwpml2x').read_bytes()
        self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (ROW['bytes'], ROW['sha256']))
        text = producer_string()
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"'), 'the in-memory declaration is kept as Hancom wrote it')
        utf16 = text.encode('utf-16-le')
        self.assertEqual((len(utf16), hashlib.sha256(utf16).hexdigest()), (ROW['utf16le_witness']['bytes'], ROW['utf16le_witness']['sha256']),
                         'the same COM string as the UTF-16LE capture of the same session')
        with self.assertRaises(ET.ParseError):
            ET.fromstring(raw)  # the declaration is not a decoding of the persisted bytes

    def test_fields_pair_by_their_order_not_by_a_shared_field_id(self) -> None:
        root = ET.fromstring(producer_string())
        controls = [item for item in root.iter() if item.tag in ('FIELDBEGIN', 'FIELDEND')]
        self.assertEqual([item.tag for item in controls], ['FIELDBEGIN', 'FIELDEND'] * 4, 'each begin is closed before the next opens')
        begins, ends = controls[0::2], controls[1::2]
        self.assertEqual({item.get('FieldId') for item in controls}, {'627272811'}, 'one FieldId for every begin and end: not an identity')
        self.assertEqual(len({item.get('InstId') for item in begins}), 4)
        self.assertEqual([item.get('InstId') for item in ends], [None] * 4, 'an end names no begin')
        self.assertEqual(len({json.dumps(sorted(item.attrib.items())) for item in ends}), 1, 'the four ends are identical')
        self.assertEqual({item.get('Type') for item in controls}, {'Clickhere'})
        names = [item.get('Name') for item in begins]
        self.assertEqual([f'{name}{{{{{names[:number].count(name)}}}}}' for number, name in enumerate(names)], SELECTORS,
                         'the n-th begin of a name in the text is the native name{{n}}')
        text = list(root.find('BODY/SECTION/P/TEXT'))
        held = [text[text.index(begin) + 1].text for begin in begins]
        self.assertEqual(held, [VALUES[selector] for selector in SELECTORS], 'each field text sits directly between its begin and end')


class ActualBeforeEngine(unittest.TestCase):
    def test_the_engine_finds_each_actual_field_span_in_aligned_paragraph_lists(self) -> None:
        side = reads(RecordedHwp())
        texts = [item['text'] for item in side['model']['paragraphs']]
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts, [item['text'] for item in side['census']['paragraphs']])
        self.assertEqual(texts, [item['text'] for item in side['tables']['paragraphs']])
        self.assertEqual(side['areas']['areas'], [])
        atoms = side['model']['paragraphs'][0]['atoms']
        begins = [index for index, atom in enumerate(atoms) if atom.get('control') == 'fieldbegin']
        ends = [index for index, atom in enumerate(atoms) if atom.get('control') == 'fieldend']
        spans = [native_fields._field_spans(atoms, VALUES[selector]) for selector in SELECTORS]  # noqa: SLF001
        self.assertEqual(spans, [[pair] for pair in zip(begins, ends)], 'one span per field, the n-th begin with the n-th end')
        self.assertEqual(len({atoms[index]['digest'] for index in begins}), 4, 'each begin keeps its own full-attribute digest')
        self.assertEqual(len({atoms[index]['digest'] for index in ends}), 1, 'the ends are alike, so the proof never relies on their identity')

    def test_the_inventory_lists_the_four_actual_fields(self) -> None:
        hwp = RecordedHwp()
        result = inventory(hwp)
        rows = [(row['name'], row['occurrence'], row['occurrences'], row['native_address'], row['kind'], row['text'], row['readable'], row['fillable'])
                for row in result['fields']]
        self.assertEqual(rows, [('성명', 1, 1, '성명{{0}}', 'click_here', '기존성명', True, True), ('주소', 1, 1, '주소{{0}}', 'click_here', '고정주소', True, True),
                                ('태그', 1, 2, '태그{{0}}', 'click_here', '태그하나', True, True), ('태그', 2, 2, '태그{{1}}', 'click_here', '태그둘째', True, True)])
        self.assertEqual((result['count'], result['listed'], result['truncated'], result['coverage']['complete']), (4, 4, False, True))
        self.assertEqual([row['native_address'] for row in result['fields']], SELECTORS)
        self.assertEqual({call[1] for call in hwp.calls if call[0] == 'com'}, {'GetFieldList', 'GetFieldText'})
        self.assertEqual([call[2] for call in hwp.calls if call[:2] == ('com', 'GetFieldText')], SELECTORS, 'each occurrence read once, by its own address')

    def test_one_unique_field_is_eligible_and_only_its_own_occurrence_is_written(self) -> None:
        hwp = RecordedHwp()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        row = inventory(hwp)['fields'][0]
        hwp.calls.clear()
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            outcome = owned.service.command_bundle(steps=[{'label': 'x', 'op': 'field_text_exact', 'confirm_layout': True, 'name': '성명',
                                                           'expected_hash': row['hash'], 'new_text': '변경성명'}])
        self.assertEqual([call[1:] for call in hwp.calls if call[:2] == ('com', 'PutFieldText')], [('PutFieldText', '성명{{0}}', '변경성명')],
                         'every pre-write check passed on the actual before state: one indexed write, never the bare name')
        before_write = hwp.calls[:hwp.calls.index(('com', 'PutFieldText', '성명{{0}}', '변경성명'))]
        self.assertTrue({call[1] for call in before_write if call[0] == 'com'} <= READS)
        self.assertIn(('com', 'FieldExist', '성명'), before_write)
        self.assertIn(('get_text_file', 'HWPML2X', ''), before_write)
        self.assertNotIn(('HeadCtrl',), before_write, 'the proof reads the document text, not the live control list')
        self.assertEqual(hwp.calls_after_write(), [], 'no after state exists, and nothing is read after the unknown write')
        step = outcome['steps'][0]
        self.assertFalse(outcome['ok'])
        self.assertTrue(step['mutation_may_have_persisted'])
        self.assertTrue(step.get('native_custody_held'))
        self.assertTrue(owned.session._native_custody_held, 'an unknown write holds the session')  # noqa: SLF001
        self.assertNotIn('refusal_code', step)

    def test_an_unreadable_live_control_walk_does_not_stop_the_fill_of_the_actual_field(self) -> None:
        """The actual f02 seam: the inventory of these fields succeeded natively and the fill stopped at the live control walk.
        The walk faults are synthetic (the native value was not retained); the HWPML2X and field values are the actual ones."""
        for fault in ('empty', 'not_text', 'endless'):
            with self.subTest(fault=fault):
                hwp = RecordedHwp()
                row = inventory(hwp)['fields'][0]
                hwp.walk_fault = fault
                hwp.calls.clear()
                Flow(hwp).step(op='field_text_exact', confirm_layout=True, name='성명', expected_hash=row['hash'], new_text='변경성명')
                self.assertEqual([call[1:] for call in hwp.calls if call[:2] == ('com', 'PutFieldText')], [('PutFieldText', '성명{{0}}', '변경성명')])
                self.assertNotIn(('HeadCtrl',), hwp.calls, 'no live control walk before the write')
                self.assertEqual(hwp.calls_after_write(), [])

    def test_the_repeated_name_needs_its_occurrence_then_writes_only_that_one(self) -> None:
        hwp = RecordedHwp()
        rows = inventory(hwp)['fields']
        second = rows[3]
        hwp.calls.clear()
        step = Flow(hwp).step(op='field_text_exact', confirm_layout=True, name='태그', expected_hash=second['hash'], new_text='태그변경')['steps'][0]
        self.assertEqual((step.get('refusal_code'), step['mutation_may_have_persisted'], step.get('native_custody_held', False)),
                         ('field_occurrence_required', False, False))
        self.assertNotIn('PutFieldText', [call[1] for call in hwp.calls if call[0] == 'com'])
        Flow(hwp).step(op='field_text_exact', confirm_layout=True, name='태그', occurrence=2, expected_occurrences=2, expected_hash=second['hash'], new_text='태그변경')
        self.assertEqual([call[1:] for call in hwp.calls if call[:2] == ('com', 'PutFieldText')], [('PutFieldText', '태그{{1}}', '태그변경')])
        self.assertEqual(hwp.calls_after_write(), [])

    def test_the_source_fake_answers_as_the_native_session_did(self) -> None:
        fake = FieldsHwp(mirror())
        self.assertEqual(fake.hwp.GetFieldList(1, 2), NUMBERED)
        self.assertEqual(fake.hwp.GetFieldList(0, 2), MANIFEST['raw_field_api_authoring_session']['GetFieldList(0, 2)'])
        self.assertEqual({selector: fake.hwp.GetFieldText(selector) for selector in SELECTORS}, VALUES, 'a single read has no trailing 0x02')
        self.assertEqual({key: fake.hwp.GetFieldList(*key) for key in LISTS}, LISTS, 'the derived forms are what the fake gives for the same fields')


if __name__ == '__main__':
    unittest.main()
