"""Existing native named fields: the inventory by name and occurrence, and filling one click-here field.

Through the real bundle validation, package dispatch, service and census. The SDK is tests.native_fields_fakes, which
follows the official desktop field contract (HwpAutomation_2504 pp. 16-19); not Hancom evidence.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from app import native_fields, native_format as nf, native_table
from app.local_cli_bundle_fields import LocalCliBundleFieldsMixin
from app.local_cli_service_support import LocalCliServiceError
from tests.native_fields_fakes import FIELD_END_XML, FieldsHwp, cell_field, click_here, field_begin_xml, text_box_field
from tests.native_format_fakes import bookmark, char, page_hiding, paragraph, table, textbox
from tests.native_header_footer_fakes import area
from tests.test_native_paragraph_edit import Flow
from tests.test_repair_runtime_safety import owned_doc

FILL = {'op': 'field_text_exact', 'confirm_layout': True}


def form() -> FieldsHwp:
    return FieldsHwp([paragraph('신청서'), paragraph('성명: ', *click_here('성명', '홍길동'), ' 님'),
                      paragraph('주소: ', *click_here('주소')), paragraph('연락처: ', *click_here('연락처', '010\t1234'))])


def duplicates() -> FieldsHwp:
    return FieldsHwp([paragraph('갑: ', *click_here('서명', '김갑동', inst=1)), paragraph('을: ', *click_here('서명', '이을순', inst=2)),
                      paragraph('끝')])


def paragraph_texts(hwp: FieldsHwp) -> list[str]:
    return [item['text'] for item in nf.parse_format_model(hwp.get_text_file('HWPML2X', ''))['paragraphs']]


def inventory(hwp: FieldsHwp, **fields) -> dict:
    outcome = Flow(hwp).step(op='field_inventory', **fields)
    assert outcome['ok'], outcome
    return outcome['steps'][0]['result']


def bound(hwp: FieldsHwp, name: str, occurrence: int = 1) -> dict:
    found = next(item for item in inventory(hwp)['fields'] if item['name'] == name and item['occurrence'] == occurrence)
    binding = {'name': name, 'expected_hash': found['hash']}
    if found['occurrences'] > 1:
        binding.update(occurrence=occurrence, expected_occurrences=found['occurrences'])
    return binding


def ok(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertTrue(outcome['ok'], outcome)
    return outcome['steps'][0]['result']


def refused(test: unittest.TestCase, outcome: dict, code: str, hwp: FieldsHwp, before: list) -> dict:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertEqual(step.get('refusal_code'), code, step)
    test.assertFalse(step['mutation_may_have_persisted'])
    test.assertFalse(outcome.get('native_custody_held', False))
    test.assertEqual(hwp.puts(), [], 'nothing is written')
    test.assertEqual(hwp.field_texts(), before)
    return step


def held(test: unittest.TestCase, outcome: dict) -> dict:
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertTrue(step['mutation_may_have_persisted'], step)
    test.assertNotIn('refusal_code', step)
    return step


def held_before_write(test: unittest.TestCase, outcome: dict, hwp: FieldsHwp) -> dict:
    """A native read raised before any write: an unproven outcome, never a clean refusal (OwnedCustody shows the session held)."""
    test.assertFalse(outcome['ok'], outcome)
    step = outcome['steps'][0]
    test.assertNotIn('refusal_code', step)
    test.assertFalse(step['mutation_may_have_persisted'], step)
    test.assertIn('outcome is unproven', step['error'])
    test.assertEqual(hwp.puts(), [])
    return step


def side(hwp: FieldsHwp) -> dict:
    return LocalCliBundleFieldsMixin()._fields_read_state(hwp)()  # noqa: SLF001


def proof_reads(calls: list[tuple]) -> list[tuple]:
    """The document reads and control walks among logged calls (not the field list or field text reads)."""
    return [call for call in calls if call[0] != 'com']


class Inventory(unittest.TestCase):
    def test_lists_every_field_with_name_occurrence_kind_text_and_hash_without_writing(self) -> None:
        hwp = form()
        hwp.cursor = (0, 1, 3)
        result = inventory(hwp)
        self.assertTrue(result['read_only'])
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['coverage']['complete'], True)
        rows = {item['name']: item for item in result['fields']}
        self.assertEqual(list(rows), ['성명', '주소', '연락처'])
        name = rows['성명']
        self.assertEqual({key: name[key] for key in ('occurrence', 'occurrences', 'native_address', 'kind', 'text', 'empty', 'readable', 'fillable')},
                         {'occurrence': 1, 'occurrences': 1, 'native_address': '성명{{0}}', 'kind': 'click_here', 'text': '홍길동', 'empty': False,
                          'readable': True, 'fillable': True})
        self.assertRegex(name['hash'], r'^sha256:[0-9a-f]{24}$')
        self.assertEqual((rows['주소']['text'], rows['주소']['empty']), ('', True), 'an empty field is listed as empty, not missing')
        self.assertEqual(rows['연락처']['text'], '010\t1234', 'a tab stays a tab')
        self.assertEqual(hwp.puts(), [])
        self.assertEqual(hwp.executes(), [])
        self.assertEqual(hwp.cursor, (0, 1, 3))
        self.assertEqual(inventory(hwp)['fields'][0]['hash'], name['hash'], 'the same document gives the same hash')
        calls = [call[0] for call in hwp.hwp.calls]
        self.assertNotIn('PutFieldText', calls)
        self.assertTrue(all(call[1].endswith('}}') for call in hwp.hwp.calls if call[0] == 'GetFieldText'), 'every read names its occurrence')

    def test_duplicates_are_listed_one_by_one_with_their_own_hash(self) -> None:
        rows = inventory(duplicates())['fields']
        self.assertEqual([(item['name'], item['occurrence'], item['occurrences'], item['native_address'], item['text']) for item in rows],
                         [('서명', 1, 2, '서명{{0}}', '김갑동'), ('서명', 2, 2, '서명{{1}}', '이을순')])
        self.assertNotEqual(rows[0]['hash'], rows[1]['hash'])

    def test_same_text_in_two_occurrences_still_gives_two_bindings(self) -> None:
        hwp = FieldsHwp([paragraph(*click_here('x', 'same', inst=1)), paragraph(*click_here('x', 'same', inst=2))])
        first, second = inventory(hwp)['fields']
        self.assertNotEqual(first['hash'], second['hash'])

    def test_a_name_filter_reads_only_that_field(self) -> None:
        result = inventory(form(), name='주소')
        self.assertEqual([(item['name'], item['text']) for item in result['fields']], [('주소', '')])
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['document_field_count'], 3)

    def test_a_name_without_fields_is_an_empty_list_not_a_missing_census(self) -> None:
        result = inventory(form(), name='없는필드')
        self.assertEqual((result['fields'], result['count'], result['coverage']['complete']), ([], 0, True))

    def test_a_document_without_fields(self) -> None:
        result = inventory(FieldsHwp([paragraph('본문')]))
        self.assertEqual((result['count'], result['fields'], result['coverage']['complete']), (0, [], True))

    def test_the_text_delimiter_after_a_field_text_is_not_part_of_it(self) -> None:
        hwp = form()
        hwp.hwp.text_end = '\x02'
        self.assertEqual(inventory(hwp)['fields'][0]['text'], '홍길동')

    def test_a_cell_field_is_listed_but_not_filled_by_this_command(self) -> None:
        hwp = FieldsHwp([paragraph('표'), paragraph(table(cell_field(paragraph('A1값'), '셀이름'), paragraph('b'), paragraph('c'), paragraph('d'))), paragraph('끝')])
        row = inventory(hwp)['fields'][0]
        self.assertEqual((row['name'], row['kind'], row['text'], row['fillable'], row['fill_refusal']), ('셀이름', 'cell', 'A1값', False, 'field_kind_unsupported'))

    def test_a_name_that_cannot_be_addressed_is_listed_without_reading_it(self) -> None:
        hwp = FieldsHwp([paragraph(*click_here('a{{b}}', 'v'))])
        row = inventory(hwp)['fields'][0]
        self.assertEqual((row['name'], row['readable'], row['text'], row['fillable'], row['fill_refusal']), ('a{{b}}', False, None, False, 'field_name_unaddressable'))
        self.assertEqual([call for call in hwp.hwp.calls if call[0] == 'GetFieldText'], [])

    def test_an_unreadable_text_is_not_reported_as_empty(self) -> None:
        hwp = form()
        hwp.hwp.overrides['GetFieldText'] = lambda spec: 'a\x02b' if spec.startswith('성명') else ''
        row = inventory(hwp)['fields'][0]
        self.assertEqual((row['readable'], row['text'], row['empty'], row['fillable'], row['fill_refusal']), (False, None, None, False, 'field_text_unreadable'))
        self.assertFalse(inventory(hwp)['coverage']['complete'])

    def test_a_list_that_is_not_text_or_disagrees_with_its_counts_is_refused(self) -> None:
        for override in (None, 7, b'x', lambda number, option: '성명{{0}}' if number == 1 else '성명{{2}}', lambda number, option: '성명' if number == 1 else '',
                         lambda number, option: '성명{{1}}' if number == 1 else '성명{{1}}'):
            with self.subTest(override=override):
                hwp = form()
                hwp.hwp.overrides['GetFieldList'] = override
                outcome = Flow(hwp).step(op='field_inventory')
                self.assertFalse(outcome['ok'])
                self.assertEqual(outcome['steps'][0].get('refusal_code'), 'field_census_unusable', outcome)
                self.assertFalse(outcome['steps'][0]['mutation_may_have_persisted'])
                self.assert_read_only_refusal(outcome['steps'][0])

    def assert_read_only_refusal(self, step: dict) -> None:
        """An inventory refusal ran reads only and no state check: it must not claim one."""
        self.assertNotIn('verified', step['error'])
        self.assertNotIn('preservation', step['error'])
        self.assertIn('nothing was written', step['error'])

    def test_a_missing_field_api_is_a_clean_read_only_refusal(self) -> None:
        for broken in ('no_com', 'GetFieldList', 'GetFieldText'):
            with self.subTest(broken=broken):
                hwp = form()
                if broken == 'no_com':
                    hwp.hwp = None
                else:
                    setattr(hwp.hwp, broken, None)
                step = Flow(hwp).step(op='field_inventory')['steps'][0]
                self.assertEqual(step.get('refusal_code'), 'native_unavailable', step)
                self.assertFalse(step['mutation_may_have_persisted'])
                self.assertFalse(step.get('native_custody_held', False))
                self.assert_read_only_refusal(step)

    def test_a_field_read_that_raises_holds_the_session(self) -> None:
        for method in ('GetFieldList', 'GetFieldText'):
            with self.subTest(method=method):
                hwp = form()
                hwp.hwp.raises[method] = RuntimeError('COM read failed')
                held_before_write(self, Flow(hwp).step(op='field_inventory'), hwp)

    def test_kinds_text_box_mixed_and_unclassified_are_listed_as_they_are(self) -> None:
        hwp = FieldsHwp([paragraph('상자 ', textbox(text_box_field(paragraph('상자값'), '상자'))),
                         paragraph('겸용 ', *click_here('겸용', '누름값')),
                         paragraph(table(cell_field(paragraph('셀값'), '겸용'), paragraph('b'), paragraph('c'), paragraph('d'))),
                         paragraph('분류 ', *click_here('분류', '값'))])
        rows = inventory(hwp)['fields']
        self.assertEqual([(row['name'], row['occurrence'], row['kind'], row['text'], row['fillable'], row['fill_refusal']) for row in rows],
                         [('상자', 1, 'text_box', '상자값', False, 'field_kind_unsupported'), ('겸용', 1, 'mixed', '누름값', False, 'field_kind_unsupported'),
                          ('겸용', 2, 'mixed', '셀값', False, 'field_kind_unsupported'), ('분류', 1, 'click_here', '값', True, None)])
        options = sorted({call[2] for call in hwp.hwp.calls if call[0] == 'GetFieldList' and call[1] == 2})
        self.assertEqual(options, [0, 1, 2, 8], 'every kind list is read')
        hwp.hwp.overrides['GetFieldList'] = lambda number, option: '' if (number, option) == (2, 2) else hwp.hwp.listed(number, option)
        rows = inventory(hwp)['fields']
        self.assertEqual(rows[3]['kind'], 'unclassified', 'a name no kind list covers is not guessed')
        self.assertEqual(rows[3]['fill_refusal'], 'field_kind_unsupported')

    def test_the_read_budget_lists_the_rest_unread_with_a_name_filter_too(self) -> None:
        hwp = FieldsHwp([paragraph(*click_here('가', '1')), paragraph(*click_here('나', '2')), paragraph(*click_here('다', '3'))])
        with patch.object(native_fields, 'MAX_TEXT_READS', 2):
            result = inventory(hwp)
            last = result['fields'][2]
            self.assertEqual((last['readable'], last['text'], last['empty'], last['fillable'], last['fill_refusal']), (False, None, None, False, 'field_census_budget'))
            self.assertEqual((result['coverage']['complete'], result['coverage']['unread']), (False, ['field_census_budget']))
            hwp.hwp.calls.clear()
            only = inventory(hwp, name='다')['fields']
            self.assertEqual([(row['readable'], row['fillable'], row['fill_refusal']) for row in only], [(False, False, 'field_census_budget')],
                             'a name filter reads fewer fields, never further than a fill can')
            self.assertEqual([call for call in hwp.hwp.calls if call[0] == 'GetFieldText'], [])

    def test_rows_past_the_listing_limit_are_counted_and_flagged_not_hidden(self) -> None:
        hwp = FieldsHwp([paragraph(*click_here('가', '1')), paragraph(*click_here('나', '2')), paragraph(*click_here('다', '3'))])
        with patch.object(native_fields, 'MAX_ROWS', 2):
            result = inventory(hwp)
        self.assertEqual([row['name'] for row in result['fields']], ['가', '나'])
        self.assertEqual((result['count'], result['listed'], result['truncated']), (3, 2, True))
        self.assertEqual((result['coverage']['complete'], result['coverage']['rows_omitted']), (False, 1))
        whole = inventory(hwp)
        self.assertEqual((whole['count'], whole['listed'], whole['truncated'], whole['coverage']['complete'], whole['coverage']['rows_omitted']), (3, 3, False, True, 0))

    def test_a_name_longer_than_a_request_may_give_is_listed_cut_and_not_fillable(self) -> None:
        name = '긴' * 205
        hwp = FieldsHwp([paragraph(*click_here(name, 'v'))])
        row = inventory(hwp)['fields'][0]
        self.assertEqual((len(row['name']), row['name_truncated'], row['native_address'], row['readable'], row['fillable'], row['fill_refusal']),
                         (200, True, None, False, False, 'field_name_unaddressable'))
        self.assertEqual([call for call in hwp.hwp.calls if call[0] == 'GetFieldText'], [])
        self.assertFalse(inventory(form())['fields'][0]['name_truncated'])

    def test_a_long_text_is_cut_with_its_flag(self) -> None:
        hwp = FieldsHwp([paragraph(*click_here('긴글', 'x' * 2005))])
        row = inventory(hwp)['fields'][0]
        self.assertEqual((len(row['text']), row['text_truncated']), (2000, True))

    def test_unknown_or_invalid_inventory_fields_are_refused_at_validation(self) -> None:
        hwp = form()
        for fields in ({'occurrence': 1}, {'name': ''}, {'name': 'a{{0}}'}, {'name': 7}, {'name': 'x' * 201}, {'name': '\ud800'}, {'name': 'a\udfffb'}):
            with self.subTest(fields=ascii(fields)), self.assertRaises(LocalCliServiceError):
                Flow(hwp).step(op='field_inventory', **fields)
        self.assertEqual(hwp.hwp.calls, [])
        self.assertEqual(hwp.calls, [], 'no document read either')

    def test_a_document_name_with_a_lone_surrogate_is_listed_but_never_read_or_fillable(self) -> None:
        hwp = FieldsHwp([paragraph(*click_here('\ud800', 'v', inst=1)), paragraph(*click_here('a\udfffb', 'w', inst=2)), paragraph(*click_here('성명😀', 'x', inst=3))])
        rows = inventory(hwp)['fields']
        self.assertEqual([(row['name'], row['native_address'], row['readable'], row['text'], row['fillable'], row['fill_refusal']) for row in rows],
                         [('\ud800', None, False, None, False, 'field_name_unaddressable'), ('a\udfffb', None, False, None, False, 'field_name_unaddressable'),
                          ('성명😀', '성명😀{{0}}', True, 'x', True, None)])
        self.assertEqual([call[1] for call in hwp.hwp.calls if call[0] == 'GetFieldText'], ['성명😀{{0}}'], 'only the name a request can give is read')


class Fill(unittest.TestCase):
    def test_one_field_filled_by_name_and_occurrence_with_siblings_and_body_kept(self) -> None:
        hwp = form()
        binding = bound(hwp, '성명')
        texts_before = paragraph_texts(hwp)
        flow = Flow(hwp)
        result = ok(self, flow.step(**FILL, **binding, new_text='김철수'))
        self.assertEqual(hwp.puts(), [('PutFieldText', '성명{{0}}', '김철수')], 'the occurrence is always named: a bare name writes every field of it')
        self.assertEqual(hwp.field_texts(), [('성명', '김철수'), ('주소', ''), ('연락처', '010\t1234')])
        self.assertEqual(paragraph_texts(hwp), [texts_before[0], '성명: 김철수 님', *texts_before[2:]], 'only the field\'s own paragraph changed')
        self.assertEqual(result['field']['name'], '성명')
        self.assertEqual(result['field']['native_address'], '성명{{0}}')
        self.assertEqual(result['result']['text_before'], '홍길동')
        self.assertEqual(result['result']['text_after'], '김철수', 'the observed value after the fill')
        self.assertEqual(result['result']['native_calls'], ['PutFieldText'])
        self.assertEqual(result['result']['location']['context'], 'body')
        self.assertEqual(result['result']['paragraph_after'], '성명: 김철수 님')
        nxt = result['next_binding']
        self.assertEqual(nxt['expected_hash'], bound(hwp, '성명')['expected_hash'], 'the next binding is the hash the inventory gives now')
        self.assertNotEqual(nxt['expected_hash'], binding['expected_hash'])
        self.assertEqual(result['undo']['native_editing_actions'], 1)
        self.assertEqual(result['result']['format_source'], 'field')
        self.assertIn(('get_text_file', 'HWPML2X', ''), proof_reads(hwp.calls_after_write()), 'a successful fill is proven from the document text')
        self.assertIsNone(flow.binding.get('pending_logical_undo_count'))
        self.assertIn('field_text_exact', flow.binding.get('logical_undo_unverified') or '', 'a field fill is not a measured Undo step')

    def test_a_duplicate_name_needs_its_occurrence_and_only_that_one_changes(self) -> None:
        hwp = duplicates()
        before = hwp.field_texts()
        first = bound(hwp, '서명', 1)
        refused(self, Flow(hwp).step(**FILL, name='서명', expected_hash=first['expected_hash'], new_text='X'), 'field_occurrence_required', hwp, before)
        second = bound(hwp, '서명', 2)
        ok(self, Flow(hwp).step(**FILL, **second, new_text='박병삼'))
        self.assertEqual(hwp.field_texts(), [('서명', '김갑동'), ('서명', '박병삼')], 'no broadcast to the other field of the same name')
        self.assertEqual(hwp.puts(), [('PutFieldText', '서명{{1}}', '박병삼')])

    def test_a_changed_count_of_same_name_fields_is_refused(self) -> None:
        hwp = duplicates()
        before = hwp.field_texts()
        binding = {**bound(hwp, '서명', 2), 'expected_occurrences': 3, 'occurrence': 2}
        refused(self, Flow(hwp).step(**FILL, **binding, new_text='X'), 'field_count_mismatch', hwp, before)

    def test_a_stale_binding_or_a_missing_field_is_refused_before_any_write(self) -> None:
        hwp = form()
        before = hwp.field_texts()
        binding = bound(hwp, '성명')
        refused(self, Flow(hwp).step(**FILL, **{**binding, 'expected_hash': 'sha256:' + '0' * 24}, new_text='X'), 'field_hash_mismatch', hwp, before)
        refused(self, Flow(hwp).step(**FILL, name='없음', expected_hash=binding['expected_hash'], new_text='X'), 'field_not_found', hwp, before)
        refused(self, Flow(hwp).step(**FILL, name='성명', occurrence=2, expected_occurrences=2, expected_hash=binding['expected_hash'], new_text='X'),
                'field_count_mismatch', hwp, before)

    def test_the_field_changing_after_the_inventory_is_a_stale_binding(self) -> None:
        hwp = form()
        binding = bound(hwp, '성명')
        hwp.hwp.PutFieldText('성명{{0}}', '다른사람')
        hwp.hwp.calls.clear()
        refused(self, Flow(hwp).step(**FILL, **binding, new_text='X'), 'field_hash_mismatch', hwp, hwp.field_texts())

    def test_a_field_that_does_not_exist_by_the_official_check_is_refused(self) -> None:
        hwp = form()
        before = hwp.field_texts()
        binding = bound(hwp, '성명')
        for value in (False, None, 1, 'True'):
            with self.subTest(value=value):
                hwp.hwp.overrides['FieldExist'] = value
                code = 'field_not_found' if value is False else 'field_census_unusable'
                refused(self, Flow(hwp).step(**FILL, **binding, new_text='X'), code, hwp, before)

    def test_the_same_value_is_reported_without_any_write(self) -> None:
        hwp = form()
        result = ok(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text='홍길동'))
        self.assertEqual((result['result']['changed'], result['result']['status'], result['result']['native_calls']), (False, 'already_set', []))
        self.assertEqual(hwp.puts(), [])

    def test_an_empty_field_is_filled_and_a_filled_one_cleared(self) -> None:
        hwp = form()
        ok(self, Flow(hwp).step(**FILL, **bound(hwp, '주소'), new_text='서울시 중구 세종대로 110'))
        ok(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text=''))
        self.assertEqual(hwp.field_texts(), [('성명', ''), ('주소', '서울시 중구 세종대로 110'), ('연락처', '010\t1234')])

    def test_a_tab_in_the_new_text_is_written_as_a_tab(self) -> None:
        hwp = form()
        ok(self, Flow(hwp).step(**FILL, **bound(hwp, '연락처'), new_text='02\t555\t0100'))
        self.assertEqual(hwp.field_texts()[2], ('연락처', '02\t555\t0100'))

    def test_a_field_in_a_table_cell_is_filled_with_the_table_and_other_cells_kept(self) -> None:
        hwp = FieldsHwp([paragraph('표'), paragraph(table(paragraph('성명 ', *click_here('성명', '가')), paragraph('b'), paragraph('c'), paragraph('d'))), paragraph('끝')])
        result = ok(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text='나다라'))
        self.assertEqual(hwp.field_texts(), [('성명', '나다라')])
        self.assertEqual(result['result']['location']['context'], 'table_cell')

    def test_a_field_in_a_header_is_filled(self) -> None:
        hwp = FieldsHwp([paragraph('본문')], areas=[area('header', paragraph('문서번호 ', *click_here('문서번호', 'A-1')))])
        result = ok(self, Flow(hwp).step(**FILL, **bound(hwp, '문서번호'), new_text='B-22'))
        self.assertEqual(hwp.field_texts(), [('문서번호', 'B-22')])
        self.assertEqual(result['result']['location']['context'], 'header')

    def test_a_field_whose_text_spans_paragraphs_is_refused(self) -> None:
        hwp = form()
        hwp.hwp.overrides['GetFieldText'] = lambda spec: '줄1\r\n줄2' if spec.startswith('성명') else ''
        before = hwp.field_texts()
        binding = bound(hwp, '성명')
        refused(self, Flow(hwp).step(**FILL, **binding, new_text='X'), 'field_multi_paragraph', hwp, before)

    def test_a_cell_field_is_refused_by_kind(self) -> None:
        hwp = FieldsHwp([paragraph('표'), paragraph(table(cell_field(paragraph('A1값'), '셀이름'), paragraph('b'), paragraph('c'), paragraph('d'))), paragraph('끝')])
        before = hwp.field_texts()
        refused(self, Flow(hwp).step(**FILL, **bound(hwp, '셀이름'), new_text='X'), 'field_kind_unsupported', hwp, before)

    def test_a_field_text_the_document_does_not_hold_is_refused_before_writing(self) -> None:
        hwp = form()
        hwp.hwp.overrides['GetFieldText'] = lambda spec: '딴값' if spec.startswith('성명') else ''
        before = hwp.field_texts()
        refused(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text='X'), 'field_text_unprovable', hwp, before)

    def test_invalid_requests_never_read_or_write(self) -> None:
        hwp = form()
        binding = bound(hwp, '성명')
        hwp.hwp.calls.clear()
        for fields in ({**binding, 'new_text': 'two\nlines'}, {**binding, 'new_text': 'a\x02b'}, {**binding, 'new_text': 'cr\r'}, {**binding, 'new_text': 'x' * 2001},
                       {**binding, 'new_text': 1}, {key: value for key, value in binding.items() if key != 'expected_hash'}, {**binding, 'expected_hash': 'sha256:abc'},
                       {**binding, 'name': ''}, {**binding, 'name': 'a{{0}}'}, {**binding, 'occurrence': 1}, {**binding, 'occurrence': 0, 'expected_occurrences': 1},
                       {**binding, 'occurrence': 3, 'expected_occurrences': 2}, {**binding, 'occurrence': True, 'expected_occurrences': 1}, {**binding, 'expected_page': 1},
                       {**binding, 'name': '\ud800'}, {**binding, 'name': 'a\udfffb'}, {**binding, 'name': '\ude00\ud83d'}):
            with self.subTest(fields=fields), self.assertRaises(LocalCliServiceError):
                Flow(hwp).step(**FILL, **{'new_text': 'X', **fields})
        with self.assertRaises(LocalCliServiceError):
            Flow(hwp).step(op='field_text_exact', **binding, new_text='X')
        self.assertEqual(hwp.hwp.calls, [], 'no field read or write for an invalid request')

    def test_a_value_returned_by_the_void_write_is_an_unknown_outcome_never_coerced(self) -> None:
        for value in (True, False, 0, 1, ''):
            with self.subTest(value=value):
                hwp = form()
                hwp.hwp.put_result = value
                step = held(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text='김철수'))
                self.assertIn('PutFieldText', step['error'])

    def test_a_write_that_raises_is_an_unknown_outcome(self) -> None:
        hwp = form()
        hwp.hwp.raises['PutFieldText'] = RuntimeError('COM failed')
        held(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text='김철수'))

    def test_a_write_that_reaches_a_sibling_is_an_unknown_outcome(self) -> None:
        hwp = duplicates()
        binding = bound(hwp, '서명', 1)
        hwp.hwp.put_side_effect = lambda owner: owner.hwp._write(owner.hwp.fields()[1], '같이바뀜')  # noqa: SLF001
        step = held(self, Flow(hwp).step(**FILL, **binding, new_text='새이름'))
        self.assertIn('field', step['error'])
        after = hwp.calls_after_write()
        self.assertTrue(after, 'the field list is read once after the write')
        self.assertEqual(proof_reads(after), [], 'a known mismatch stops at once: no document read or control walk follows')
        self.assertEqual({call[1] for call in after}, {'GetFieldList', 'GetFieldText'})

    def test_a_write_that_changes_other_text_is_an_unknown_outcome(self) -> None:
        hwp = form()
        binding = bound(hwp, '성명')
        hwp.hwp.put_side_effect = lambda owner: owner.body[0]['atoms'].append({'c': '!', 'cs': char()})
        held(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'))

    def test_a_write_that_leaves_the_old_text_is_an_unknown_outcome(self) -> None:
        hwp = form()
        binding = bound(hwp, '성명')
        hwp.hwp.put_side_effect = lambda owner: owner.hwp._write(owner.hwp.fields()[0], '홍길동')  # noqa: SLF001
        held(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'))

    def test_an_unreadable_list_after_the_write_is_an_unknown_outcome(self) -> None:
        hwp = form()
        binding = bound(hwp, '성명')

        def broken(owner):
            owner.hwp.overrides['GetFieldList'] = None
        hwp.hwp.put_side_effect = broken
        held(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'))

    def test_the_filled_text_in_another_format_than_the_field_is_an_unknown_outcome(self) -> None:
        hwp = form()
        binding = bound(hwp, '성명')

        def restyle(owner):
            field = owner.hwp.fields()[0]
            atoms = field['para']['atoms']
            for atom in atoms[field['begin'] + 1:owner.hwp._end(field)]:  # noqa: SLF001
                atom['cs'] = char(Bold=True)
        hwp.hwp.put_side_effect = restyle
        held(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'))


    def test_paragraph_lists_that_disagree_are_refused_before_the_write(self) -> None:
        hwp = form()
        before = hwp.field_texts()
        binding = bound(hwp, '성명')
        real = native_table.parse_table_model

        def short(source):
            model = real(source)
            return {**model, 'paragraphs': model['paragraphs'][:-1]}
        with patch('app.native_table.parse_table_model', side_effect=short):
            step = refused(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'), 'preservation_unprovable', hwp, before)
        self.assertIn('nothing was written', step['refusal_message'])

    def test_the_actual_paragraph_lists_agree_before_a_fill(self) -> None:
        state = side(form())
        texts = [item['text'] for item in state['model']['paragraphs']]
        self.assertEqual(texts, [item['text'] for item in state['census']['paragraphs']])
        self.assertEqual(texts, [item['text'] for item in state['tables']['paragraphs']])

    def test_a_missing_method_refuses_cleanly_and_a_raising_read_holds(self) -> None:
        for method in ('FieldExist', 'PutFieldText'):
            with self.subTest(missing=method):
                hwp = form()
                before = hwp.field_texts()
                binding = bound(hwp, '성명')
                setattr(hwp.hwp, method, None)
                refused(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'), 'native_unavailable', hwp, before)
        hwp = form()
        binding = bound(hwp, '성명')
        hwp.hwp = None
        step = Flow(hwp).step(**FILL, **binding, new_text='김철수')['steps'][0]
        self.assertEqual((step.get('refusal_code'), step['mutation_may_have_persisted']), ('native_unavailable', False))
        for method in ('GetFieldList', 'GetFieldText', 'FieldExist'):
            with self.subTest(raises=method):
                hwp = form()
                binding = bound(hwp, '성명')
                hwp.hwp.raises[method] = RuntimeError('COM read failed')
                held_before_write(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'), hwp)

    def test_mixed_unclassified_and_text_box_fields_are_refused_by_kind_before_any_write(self) -> None:
        hwp = FieldsHwp([paragraph('상자 ', textbox(text_box_field(paragraph('상자값'), '상자'))),
                         paragraph('겸용 ', *click_here('겸용', '누름값')),
                         paragraph(table(cell_field(paragraph('셀값'), '겸용'), paragraph('b'), paragraph('c'), paragraph('d')))])
        before = hwp.field_texts()
        refused(self, Flow(hwp).step(**FILL, **bound(hwp, '상자'), new_text='X'), 'field_kind_unsupported', hwp, before)
        refused(self, Flow(hwp).step(**FILL, **bound(hwp, '겸용', 1), new_text='X'), 'field_kind_unsupported', hwp, before)
        hwp = form()
        before = hwp.field_texts()
        binding = bound(hwp, '성명')
        hwp.hwp.overrides['GetFieldList'] = lambda number, option: '' if (number, option) == (2, 2) else hwp.hwp.listed(number, option)
        refused(self, Flow(hwp).step(**FILL, **binding, new_text='X'), 'field_kind_unsupported', hwp, before)
        unclassified = bound(hwp, '성명')
        self.assertNotEqual(unclassified['expected_hash'], binding['expected_hash'], 'the kind is part of the hash')
        refused(self, Flow(hwp).step(**FILL, **unclassified, new_text='X'), 'field_kind_unsupported', hwp, before)

    def test_a_write_that_renews_its_own_field_controls_is_an_unknown_outcome(self) -> None:
        def renew_begin(owner):
            field = owner.hwp.fields()[0]
            field['para']['atoms'][field['begin']]['xml'] = field_begin_xml('성명', 1, dirty='false')

        def renew_end(owner):
            field = owner.hwp.fields()[0]
            field['para']['atoms'][owner.hwp._end(field)]['xml'] = FIELD_END_XML.replace('Editable="true"', 'Editable="false"')  # noqa: SLF001
        for renew in (renew_begin, renew_end):
            with self.subTest(renew=renew.__name__):
                hwp = form()
                binding = bound(hwp, '성명')
                hwp.hwp.put_side_effect = renew
                step = held(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'))
                self.assertEqual(hwp.field_texts()[0], ('성명', '김철수'), 'the field list alone reads as a success')
                self.assertIn('field controls unchanged', step['outcome_detail'])

    def test_the_new_text_in_the_previous_texts_own_format_is_accepted_and_named(self) -> None:
        bold = char(Bold=True)
        hwp = FieldsHwp([paragraph('성명: ', *click_here('성명', '홍길동', text_cs=bold)), paragraph('끝')])

        def keep_previous(owner):
            field = owner.hwp.fields()[0]
            for atom in field['para']['atoms'][field['begin'] + 1:owner.hwp._end(field)]:  # noqa: SLF001
                atom['cs'] = dict(bold)
        hwp.hwp.put_side_effect = keep_previous
        result = ok(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text='김철수'))
        self.assertEqual(result['result']['format_source'], 'previous_text')

    def test_a_character_format_left_unused_by_the_fill_may_go(self) -> None:
        own = char(Bold=True, Font='전용서체')
        hwp = FieldsHwp([paragraph('성명: ', *click_here('성명', '홍길동', text_cs=own)), paragraph('끝')])
        definitions = side(hwp)['census']['definitions']
        result = ok(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text='김철수'))
        self.assertEqual(result['result']['format_source'], 'field')
        after = side(hwp)['census']['definitions']
        for name in ('charshapelist', 'facenamelist'):
            self.assertLess(len(after[name]), len(definitions[name]), f'the text\'s own {name} entry is gone after the fill')

    def test_long_texts_in_the_fill_report_carry_their_flags(self) -> None:
        hwp = FieldsHwp([paragraph('앞 ', *click_here('긴글', 'x' * 2005))])
        result = ok(self, Flow(hwp).step(**FILL, **bound(hwp, '긴글'), new_text='y' * 2000))['result']
        self.assertEqual((len(result['text_before']), result['text_before_truncated']), (2000, True))
        self.assertEqual((len(result['text_after']), result['text_after_truncated']), (2000, False))
        self.assertEqual((result['paragraph_before_truncated'], result['paragraph_after_truncated']), (True, True))
        self.assertEqual((result['location']['paragraph_before_truncated'], result['location']['paragraph_after_truncated']), (True, True))
        short = ok(self, Flow(form()).step(**FILL, **bound(form(), '성명'), new_text='김철수'))['result']
        self.assertEqual([short[key] for key in ('text_before_truncated', 'text_after_truncated', 'paragraph_before_truncated', 'paragraph_after_truncated')],
                         [False] * 4)


class RequestParity(unittest.TestCase):
    """The MCP models and the backend validators give one verdict for every name and new_text as JSON delivers it."""

    NAMES = ['"성명"', '""', '"a{{0}}"', '"a}}"', '"tab\\tname"', '"\\u0001x"', '"\\u007f"', '"' + 'n' * 200 + '"', '"' + 'n' * 201 + '"',
             '"\\ud83d\\ude00"', '"\\u2028"', '"\\ud800"', '"a\\udfffb"', '"\\ude00\\ud83d"', '"성명\\ud83d\\ude00"', '"\\ud835\\udd38이름"']
    TEXTS = ['""', '"김철수"', '"a\\tb"', '"two\\nlines"', '"cr\\r"', '"a\\u0002b"', '"\\u007f"', '"\\u0085"', '"\\u2028"', '"' + 'x' * 2000 + '"',
             '"' + 'x' * 2001 + '"', '"\\ud800"', '"a\\udfffb"', '"\\ude00\\ud83d"', '"\\ud83d\\ude00"', '"' + '😀' * 2000 + '"', '"' + '😀' * 2001 + '"']

    def verdicts(self, field: str, values: list[str]) -> list[tuple]:
        from pydantic import ValidationError

        from hwpx_mcp.schema import FieldTextStep
        rows = []
        base = {'op': 'field_text_exact', 'name': '성명', 'expected_hash': 'sha256:' + 'e' * 24, 'new_text': 'x', 'confirm_layout': True}
        for raw in values:
            value = json.loads(raw)
            request = {**base, field: value}
            try:
                FieldTextStep.model_validate(request)
                mcp = True
            except ValidationError:
                mcp = False
            try:
                native_fields.validate_fill_step(request)
                backend = True
            except native_fields.NativeNumberingInputError:
                backend = False
            rows.append((raw[:40], mcp, backend))
        return rows

    def test_new_text_has_one_verdict_including_unpaired_surrogates(self) -> None:
        rows = self.verdicts('new_text', self.TEXTS)
        self.assertEqual([row for row in rows if row[1] != row[2]], [])
        self.assertEqual({row[0]: row[1] for row in rows}['"\\ud800"'], False)
        self.assertEqual({row[0]: row[1] for row in rows}['"\\ud83d\\ude00"'], True, 'a JSON surrogate pair is one character')

    def test_name_has_one_verdict(self) -> None:
        rows = self.verdicts('name', self.NAMES)
        self.assertEqual([row for row in rows if row[1] != row[2]], [])
        verdict = {row[0]: row[1] for row in rows}
        self.assertEqual([verdict[key] for key in ('"\\ud800"', '"a\\udfffb"', '"\\ude00\\ud83d"')], [False] * 3, 'a lone surrogate is refused on both sides')
        self.assertEqual([verdict[key] for key in ('"성명"', '"\\ud83d\\ude00"', '"성명\\ud83d\\ude00"', '"\\ud835\\udd38이름"')], [True] * 4,
                         'Korean and characters outside the BMP stay valid')


class RestBoundary(unittest.TestCase):
    """The backend REST route decodes the JSON body as Python does, so an escaped lone surrogate arrives as one; it is refused before any native call."""

    def post(self, hwp: FieldsHwp, body: str):
        from app.local_cli_router import LocalCliCommandBundleRequest, build_local_cli_router
        flow = Flow(hwp)
        router = build_local_cli_router(settings=None, interactive_sessions=None, service=flow.service)
        endpoint = next(route.endpoint for route in router.routes if route.path == '/local-cli/command-bundle')
        request = LocalCliCommandBundleRequest.model_validate(json.loads(body))
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return endpoint(request)

    def test_an_escaped_lone_surrogate_name_is_a_400_before_any_native_call(self) -> None:
        from fastapi import HTTPException
        listed = inventory(form())['fields'][0]
        bodies = ['{"steps":[{"op":"field_inventory","name":"\\ud800"}]}',
                  '{"steps":[{"op":"field_text_exact","name":"a\\udfffb","expected_hash":"%s","new_text":"x","confirm_layout":true}]}' % listed['hash']]
        for body in bodies:
            with self.subTest(body=body):
                hwp = form()
                with self.assertRaises(HTTPException) as raised:
                    self.post(hwp, body)
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual((hwp.hwp.calls, hwp.calls), ([], []))
        hwp = form()
        outcome = self.post(hwp, '{"steps":[{"op":"field_text_exact","name":"\\uc131\\uba85","expected_hash":"%s","new_text":"\\ud83d\\ude00","confirm_layout":true}]}' % listed['hash'])
        self.assertTrue(outcome['ok'], outcome)
        self.assertEqual(hwp.puts(), [('PutFieldText', '성명{{0}}', '\U0001F600')], 'an escaped name and an escaped astral character work as before')

    def test_the_cli_transport_cannot_even_encode_one(self) -> None:
        from local_cli_v1 import transport
        with patch.object(transport, '_request') as sent, self.assertRaises(UnicodeEncodeError):
            transport.post_json('http://127.0.0.1:1', '/local-cli/command-bundle', {'steps': [{'op': 'field_inventory', 'name': '\ud800'}]})
        sent.assert_not_called()


WALK_FAULTS = ('empty', 'not_text', 'endless')


def walks(hwp: FieldsHwp) -> int:
    return sum(1 for call in hwp.calls if call == ('HeadCtrl',))


class LiveControlWalk(unittest.TestCase):
    """The fill is proven from one HWPML2X text per side, which holds every control by kind, place and data; it never walks the live control list.

    On Hancom 13.0.0.3457 field_inventory read four actual click-here fields, then field_text_exact stopped before any write at that walk
    (control_enumeration_unavailable, session held). The faults here are synthetic: the native value was not retained."""

    def test_a_fill_succeeds_whatever_the_live_control_walk_would_return(self) -> None:
        for fault in WALK_FAULTS:
            with self.subTest(fault=fault):
                hwp = form()
                hwp.walk_fault = fault
                binding = bound(hwp, '성명')
                texts_before = paragraph_texts(hwp)
                result = ok(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'))
                self.assertEqual(hwp.puts(), [('PutFieldText', '성명{{0}}', '김철수')])
                self.assertEqual(hwp.field_texts(), [('성명', '김철수'), ('주소', ''), ('연락처', '010\t1234')])
                self.assertEqual(paragraph_texts(hwp), [texts_before[0], '성명: 김철수 님', *texts_before[2:]])
                self.assertEqual(result['result']['format_source'], 'field')
                self.assertEqual(walks(hwp), 0, 'the live control list is never walked')
                self.assertRegex(result['undo']['pre_state_sha256'], r'^[0-9a-f]{64}$')
                self.assertNotEqual(result['undo']['pre_state_sha256'], result['undo']['post_state_sha256'])

    def test_the_public_f02_step_through_an_owned_service(self) -> None:
        hwp = form()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        listed = OwnedCustody.step(self, owned, op='field_inventory')['steps'][0]['result']['fields']
        hwp.walk_fault = 'not_text'
        outcome = OwnedCustody.step(self, owned, **FILL, name='성명', expected_hash=listed[0]['hash'], new_text='김철수')
        self.assertTrue(outcome['ok'], outcome)
        self.assertTrue(outcome['dirty'])
        self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001
        self.assertTrue(owned.session._normal_close_requires_save, 'the save and close ownership applies as before')  # noqa: SLF001
        self.assertEqual(hwp.puts(), [('PutFieldText', '성명{{0}}', '김철수')])
        self.assertEqual(walks(hwp), 0)

    def test_every_control_change_is_caught_by_the_document_text_alone(self) -> None:
        def hwp_with_controls() -> FieldsHwp:
            return FieldsHwp([paragraph('표지 ', bookmark('표지'), ' ', page_hiding()), paragraph('성명: ', *click_here('성명', '홍길동'), ' 님'),
                              paragraph(table(paragraph('칸 ', bookmark('칸')), paragraph('b'), paragraph('c'), paragraph('d')))],
                             areas=[area('header', paragraph('머리 ', bookmark('머리')))])

        def field_paragraph(owner):
            return owner.hwp.fields()[0]['para']

        def cell(owner):
            return owner.body[2]['atoms'][0]['cells'][0]

        def header(owner):
            return next(atom for atom in owner.body[0]['atoms'] if atom.get('ctrl') == 'head')['paras'][0]

        def swap_controls(owner):
            atoms = owner.body[0]['atoms']
            first, second = (index for index, atom in enumerate(atoms) if atom.get('ctrl') in {'bokm', 'pghd'})
            atoms[first], atoms[second] = atoms[second], atoms[first]

        adversaries = {
            'added_elsewhere': lambda owner: owner.body[0]['atoms'].append({'cs': char(), **bookmark('새것')}),
            'removed_elsewhere': lambda owner: owner.body[0]['atoms'].remove(next(atom for atom in owner.body[0]['atoms'] if atom.get('ctrl') == 'pghd')),
            'reordered': swap_controls,
            'data_changed': lambda owner: next(atom for atom in owner.body[0]['atoms'] if atom.get('ctrl') == 'bokm').update(xml='<BOOKMARK Name="다른이름"/>'),
            'beside_the_field': lambda owner: field_paragraph(owner)['atoms'].append({'cs': char(), **bookmark('옆')}),
            'inside_the_field_text': lambda owner: field_paragraph(owner)['atoms'].insert(owner.hwp.fields()[0]['begin'] + 2, {'cs': char(), **bookmark('안')}),
            'in_a_table_cell': lambda owner: cell(owner)['atoms'].append({'cs': char(), **page_hiding()}),
            'in_a_header': lambda owner: header(owner)['atoms'].append({'cs': char(), **page_hiding()}),
        }
        hwp = hwp_with_controls()
        hwp.walk_fault = 'not_text'
        ok(self, Flow(hwp).step(**FILL, **bound(hwp, '성명'), new_text='김철수'))
        self.assertEqual(walks(hwp), 0, 'the clean fill of this control-rich document needs no walk either')
        for name, adversary in adversaries.items():
            with self.subTest(adversary=name):
                hwp = hwp_with_controls()
                hwp.walk_fault = 'not_text'
                binding = bound(hwp, '성명')
                hwp.hwp.put_side_effect = adversary
                step = held(self, Flow(hwp).step(**FILL, **binding, new_text='김철수'))
                self.assertEqual(len(hwp.puts()), 1, 'the write happened, so the proof itself caught the change')
                self.assertIn('outcome is not as requested', step['outcome_detail'])
                if name != 'data_changed':
                    self.assertIn('the document control sequence changed', step['outcome_detail'], 'the control sequence of the text sees it')
                self.assertEqual(walks(hwp), 0)


def margins() -> FieldsHwp:
    """A header holding a field beside a two-paragraph text box, then a table; a footer holding a field beside a text box.

    The body already uses a bold run, so making text in an area bold adds no definition: only the area comparison can see it."""
    return FieldsHwp([paragraph('본문 ', ('굵게', char(Bold=True)))], areas=[
        area('header', paragraph('문서번호 ', *click_here('문서번호', 'A-1', inst=1), ' ', textbox(paragraph('상자글'), paragraph('둘째줄'))),
             paragraph(table(paragraph('칸1'), paragraph('칸2'), paragraph('칸3'), paragraph('칸4')))),
        area('footer', paragraph('쪽 ', *click_here('쪽글', 'F', inst=2), ' ', textbox(paragraph('꼬리상자'))))])


def margin_area(owner: FieldsHwp, ctrl: str) -> list[dict]:
    return next(atom for atom in owner.body[0]['atoms'] if atom.get('ctrl') == ctrl)['paras']


def margin_box(owner: FieldsHwp, ctrl: str) -> list[dict]:
    return next(atom for atom in margin_area(owner, ctrl)[0]['atoms'] if 'drawtext' in atom)['drawtext']


def margin_cells(owner: FieldsHwp) -> list[dict]:
    return margin_area(owner, 'head')[1]['atoms'][0]['cells']


def retext(para: dict, text: str) -> None:
    shape = para['atoms'][0]['cs'] if para['atoms'] else char()
    para['atoms'] = [{'c': c, 'cs': dict(shape)} for c in text]


class AreaPreservation(unittest.TestCase):
    """A field in a header or footer: the whole area, its nested text boxes and tables included, stays as it was but for the field's text."""

    def test_a_header_field_and_a_footer_field_are_filled_with_their_areas_kept(self) -> None:
        for name, ctrl, context in (('문서번호', 'head', 'header'), ('쪽글', 'foot', 'footer')):
            with self.subTest(field=name):
                hwp = margins()
                result = ok(self, Flow(hwp).step(**FILL, **bound(hwp, name), new_text='새값'))
                self.assertEqual(result['result']['location']['context'], context)
                self.assertEqual(dict(hwp.field_texts())[name], '새값')
                self.assertEqual(len(hwp.puts()), 1)
                self.assertEqual([''.join(a['c'] for a in p['atoms']) for p in margin_box(hwp, ctrl)], ['상자글', '둘째줄'] if ctrl == 'head' else ['꼬리상자'])

    def test_a_change_inside_the_fields_own_area_is_an_unknown_outcome(self) -> None:
        bold = char(Bold=True)
        adversaries = {
            'text_box_text': lambda owner: retext(margin_box(owner, 'head')[0], '바뀐글'),
            'text_box_format': lambda owner: [atom.update(cs=dict(bold)) for atom in margin_box(owner, 'head')[0]['atoms']],
            'text_box_paragraph_removed': lambda owner: margin_box(owner, 'head').pop(),
            'text_box_paragraphs_reordered': lambda owner: margin_box(owner, 'head').reverse(),
            'text_box_control_added': lambda owner: margin_box(owner, 'head')[0]['atoms'].append({'cs': char(), **bookmark('상자안')}),
            'table_cell_text': lambda owner: retext(margin_cells(owner)[1], '칸이'),
            'table_cell_format': lambda owner: [atom.update(cs=dict(bold)) for atom in margin_cells(owner)[2]['atoms']],
            'table_cell_control_added': lambda owner: margin_cells(owner)[3]['atoms'].append({'cs': char(), **page_hiding()}),
        }
        for name, adversary in adversaries.items():
            with self.subTest(adversary=name):
                hwp = margins()
                binding = bound(hwp, '문서번호')
                hwp.hwp.put_side_effect = adversary
                step = held(self, Flow(hwp).step(**FILL, **binding, new_text='새값'))
                self.assertEqual(len(hwp.puts()), 1, 'the write happened, so the proof itself caught the change')
                self.assertIn('outcome is not as requested', step['outcome_detail'])

    def test_the_fields_own_paragraph_may_take_a_new_instid_but_keeps_its_settings(self) -> None:
        """Hancom renews or adds the InstId of the paragraph it edits (native pairs in tests/fixtures/native_repairs and native_paragraph_edit)."""
        hwp = margins()
        hwp.hwp.put_side_effect = lambda owner: margin_area(owner, 'head')[0].update(p_inst='162134256')
        ok(self, Flow(hwp).step(**FILL, **bound(hwp, '문서번호'), new_text='새값'))
        for name, change in (('page_break', lambda owner: margin_area(owner, 'head')[0].update(p_page_break=True)),
                             ('other_paragraph_instid', lambda owner: margin_area(owner, 'head')[1].update(p_inst='5'))):
            with self.subTest(change=name):
                hwp = margins()
                binding = bound(hwp, '문서번호')
                hwp.hwp.put_side_effect = change
                held(self, Flow(hwp).step(**FILL, **binding, new_text='새값'))

    def test_a_footer_fields_own_text_box_is_kept(self) -> None:
        hwp = margins()
        binding = bound(hwp, '쪽글')
        hwp.hwp.put_side_effect = lambda owner: retext(margin_box(owner, 'foot')[0], '바뀐꼬리')
        held(self, Flow(hwp).step(**FILL, **binding, new_text='새값'))

    def test_a_change_in_another_area_or_the_body_is_still_an_unknown_outcome(self) -> None:
        for name, adversary in (('other_area_text_box', lambda owner: retext(margin_box(owner, 'foot')[0], '바뀐꼬리')),
                                ('body', lambda owner: owner.body[0]['atoms'].append({'c': '!', 'cs': char()}))):
            with self.subTest(adversary=name):
                hwp = margins()
                binding = bound(hwp, '문서번호')
                hwp.hwp.put_side_effect = adversary
                held(self, Flow(hwp).step(**FILL, **binding, new_text='새값'))


class UndoAfterFill(unittest.TestCase):
    def setUp(self) -> None:
        self.hwp = form()
        self.owned = owned_doc(self, self.hwp, undo_stack=[], redo_stack=[])

    def step(self, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def test_undo_after_a_fill_is_refused_before_any_native_history_call(self) -> None:
        from app.local_cli_service import LocalCliUndoRefused
        listed = self.step(op='field_inventory')['steps'][0]['result']['fields']
        self.assertTrue(self.step(**FILL, name='성명', expected_hash=listed[0]['hash'], new_text='김철수')['ok'])
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                patch('app.local_cli_service._snapshot_cursor_context', return_value={}), patch('app.local_cli_service._capture_nearby_text_context', return_value={}), \
                self.assertRaises(LocalCliUndoRefused) as refused:
            self.owned.service.undo()
        self.assertEqual(refused.exception.code, 'undo_not_counted')
        self.assertNotIn(('Run', 'Undo'), self.hwp.calls)
        self.assertEqual(self.hwp.field_texts()[0], ('성명', '김철수'))

    def test_a_fill_is_an_unsaved_edit_and_an_unchanged_value_is_not(self) -> None:
        listed = self.step(op='field_inventory')['steps'][0]['result']['fields']
        same = self.step(**FILL, name='성명', expected_hash=listed[0]['hash'], new_text='홍길동')
        self.assertTrue(same['ok'])
        self.assertFalse(same['dirty'], 'the value already present is a proven no-op')
        self.assertFalse(self.owned.session._normal_close_requires_save)
        filled = self.step(**FILL, name='성명', expected_hash=listed[0]['hash'], new_text='김철수')
        self.assertTrue(filled['ok'])
        self.assertTrue(filled['dirty'])
        self.assertTrue(self.owned.session._normal_close_requires_save, 'the existing save and close ownership applies to a fill')


class OwnedCustody(unittest.TestCase):
    """Through a real owned service: a raising read before the write holds the session; a clean refusal does not."""

    def step(self, owned, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return owned.service.command_bundle(steps=[{'label': 'x', **fields}])

    def test_a_raising_field_read_holds_the_session_before_any_write(self) -> None:
        for op, method in (('field_inventory', 'GetFieldList'), ('field_inventory', 'GetFieldText'), ('field_text_exact', 'GetFieldText'),
                           ('field_text_exact', 'FieldExist')):
            with self.subTest(op=op, method=method):
                hwp = form()
                owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
                fields = {'op': op}
                if op == 'field_text_exact':
                    listed = self.step(owned, op='field_inventory')['steps'][0]['result']['fields']
                    fields = {**FILL, 'name': '성명', 'expected_hash': listed[0]['hash'], 'new_text': '김철수'}
                hwp.hwp.raises[method] = RuntimeError('COM read failed')
                outcome = self.step(owned, **fields)
                step = outcome['steps'][0]
                self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, False), step)
                self.assertNotIn('refusal_code', step)
                self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(hwp.puts(), [])

    def test_a_document_text_read_that_fails_holds_the_session_with_no_follow_on(self) -> None:
        """Without the live walk, a failing read of the document text itself stays an unknown outcome, before and after the write."""
        for when in ('before', 'after'):
            with self.subTest(when=when):
                hwp = form()
                owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
                listed = self.step(owned, op='field_inventory')['steps'][0]['result']['fields']
                real = hwp.get_text_file

                def broken(fmt='UNICODE', option=''):
                    hwp.calls.append(('get_text_file', fmt, option))
                    raise RuntimeError('GetTextFile failed')
                if when == 'before':
                    hwp.get_text_file = broken
                else:
                    hwp.hwp.put_side_effect = lambda owner: setattr(owner, 'get_text_file', broken)
                outcome = self.step(owned, **FILL, name='성명', expected_hash=listed[0]['hash'], new_text='김철수')
                step = outcome['steps'][0]
                self.assertEqual((outcome['ok'], step.get('native_custody_held'), step['mutation_may_have_persisted']), (False, True, when == 'after'), step)
                self.assertNotIn('refusal_code', step)
                self.assertTrue(owned.session._native_custody_held)  # noqa: SLF001
                self.assertEqual(len(hwp.puts()), 0 if when == 'before' else 1)
                failed = max(index for index, call in enumerate(hwp.calls) if call[0] == 'get_text_file')
                self.assertEqual(hwp.calls[failed + 1:], [], 'nothing native follows the failed read')
                self.assertEqual(walks(hwp), 0)
                hwp.get_text_file = real

    def test_a_clean_refusal_leaves_the_session_usable(self) -> None:
        hwp = form()
        owned = owned_doc(self, hwp, undo_stack=[], redo_stack=[])
        hwp.hwp.GetFieldList = None
        step = self.step(owned, op='field_inventory')['steps'][0]
        self.assertEqual(step.get('refusal_code'), 'native_unavailable')
        self.assertFalse(owned.session._native_custody_held)  # noqa: SLF001


if __name__ == '__main__':
    unittest.main()
