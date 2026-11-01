"""Paragraph insert/remove through the real bundle validation, service, package dispatch and census.

The SDK is a fake built on the observed native behavior (one InsertText holding CRLF inherits the neighbor's numbering,
one Delete of a whole-paragraph selection). These are source tests; the native run is recorded separately.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app import native_paragraph_edit as edit
from tests.native_numbering_fakes import FakeCtrl
from tests.test_native_numbering_census import _HwpmlHwp
from tests.test_numbering_user_workflow import _Workflow


class _InsertSet:
    def __init__(self) -> None:
        self.HSet = self
        self.Text = ''


class _EditHwp(_HwpmlHwp):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.HParameterSet.HInsertText = _InsertSet()
        self.selection = None
        self.insert_result = True
        self.corrupt_neighbor = False
        original_execute, original_run, original_default = self.HAction.Execute, self.HAction.Run, self.HAction.GetDefault
        self.HAction.GetDefault = lambda name, hset: True if name == 'InsertText' else original_default(name, hset)

        def execute(name, hset):
            if name != 'InsertText':
                return original_execute(name, hset)
            self.calls.append(('Execute', 'InsertText', hset.Text))
            if self.insert_result is not True:
                return self.insert_result
            index = self.cursor[1]
            base = self.paragraphs[index]
            added = {'text': hset.Text.replace('\r\n', ''), 'heading_type': base['heading_type'], 'level': base['level']}
            self.paragraphs.insert(index + 1 if hset.Text.startswith('\r\n') else index, added)
            if self.corrupt_neighbor:
                self.paragraphs[0]['text'] += ' changed'
            return True

        def run(name):
            if name != 'Delete':
                return original_run(name)
            self.calls.append(('Run', 'Delete'))
            del self.paragraphs[self.selection[1]]
            self.selection = None
            return True

        self.HAction.Execute, self.HAction.Run = execute, run

    def MoveParaEnd(self):  # noqa: N802
        self.calls.append(('MoveParaEnd',))
        self.cursor = (self.cursor[0], self.cursor[1], len(str(self.paragraphs[self.cursor[1]]['text'])))
        self.selection_para = None

    def select_text(self, spara, spos, epara, epos, list_id=0):
        self.calls.append(('select_text', spara, spos, epara, epos, list_id))
        if epara >= len(self.paragraphs):
            return False
        self.selection = (list_id, spara, spos, list_id, epara, epos)
        return True

    def get_selected_pos(self):
        if self.selection is not None:
            return (True, *self.selection)
        if self.selection_para is None:
            return (False, None, None, None, None, None, None)
        return super().get_selected_pos()


def _doc(*texts: str, outline=()) -> _EditHwp:
    hwp = _EditHwp(
        paragraphs=[{'text': text, 'heading_type': 1 if text in outline else 0, 'level': 0} for text in texts],
        controls=[FakeCtrl('secd'), FakeCtrl('tbl'), FakeCtrl('gso')],
    )
    hwp.hwpml_extras = {'picture_width': 900, 'table_width': 1700, 'char_height': 1200, 'footers': ('Footer',)}
    hwp.package_state['header_footer'] = ['Header']
    hwp.controls.append(FakeCtrl('head'))
    hwp.controls.append(FakeCtrl('foot'))
    return hwp


class Flow(_Workflow):
    def step(self, **fields):
        with patch('app.local_cli_service.snapshot_live_location', return_value={}), \
                patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
            return self.service.command_bundle(steps=[{'label': 'test', **fields}])


INSERT = {'op': 'paragraph_insert_exact', 'expected_page': 1, 'confirm_layout': True}
REMOVE = {'op': 'paragraph_remove_exact', 'expected_page': 1, 'confirm_remove': True}


class ParagraphInsert(unittest.TestCase):
    def test_after_continues_the_neighbor_level_with_one_native_action(self) -> None:
        hwp = _doc('Title', 'Item', 'Tail', outline=('Item',))
        result = Flow(hwp).step(match='Item', text='Next', position='after', **INSERT)
        self.assertTrue(result['ok'], result)
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Item', 'Next', 'Tail'])
        self.assertEqual((hwp.paragraphs[2]['heading_type'], hwp.paragraphs[2]['level']), (1, 0))
        self.assertEqual([c for c in hwp.calls if c[:2] == ('Execute', 'InsertText')], [('Execute', 'InsertText', '\r\nNext')])
        self.assertEqual(result['steps'][0]['result']['undo'], {'native_editing_actions': 1})

    def test_before_keeps_the_target_after_the_new_paragraph(self) -> None:
        hwp = _doc('Title', 'Item', 'Tail', outline=('Item',))
        self.assertTrue(Flow(hwp).step(match='Item', text='Front', position='before', **INSERT)['ok'])
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Front', 'Item', 'Tail'])

    def test_a_change_elsewhere_is_an_unknown_outcome_not_a_success(self) -> None:
        hwp = _doc('Title', 'Item', 'Tail')
        hwp.corrupt_neighbor = True
        result = Flow(hwp).step(match='Item', text='Next', position='after', **INSERT)
        self.assertFalse(result['ok'])
        self.assertTrue(result['steps'][0]['mutation_may_have_persisted'])

    def test_refusals_issue_no_native_edit_and_name_the_reason(self) -> None:
        cases = [
            (_doc('Title', 'Item', 'Item again'), {'match': 'Item', 'text': 'New', 'position': 'after'}, 'target_ambiguous'),
            (_doc('Title', 'Item'), {'match': 'Title', 'text': 'New', 'position': 'before'}, 'target_at_document_start'),
            (_doc('Title', 'Item'), {'match': 'Item', 'text': 'Title', 'position': 'after'}, 'target_ambiguous'),
        ]
        for hwp, fields, code in cases:
            with self.subTest(code=code, fields=fields):
                result = Flow(hwp).step(**fields, **INSERT)
                self.assertFalse(result['ok'])
                self.assertEqual(result['steps'][0]['refusal_code'], code)
                self.assertFalse(result['steps'][0]['mutation_may_have_persisted'])
                self.assertEqual([c for c in hwp.calls if c[:2] == ('Execute', 'InsertText')], [])

    def test_input_validation(self) -> None:
        good = {'match': 'Item', 'expected_page': 1, 'text': 'New', 'position': 'after', 'confirm_layout': True}
        self.assertEqual(edit.validate_insert_step(good)['position'], 'after')
        for bad in ({**good, 'text': 'a\r\nb'}, {**good, 'text': ' '}, {**good, 'position': 'inside'}, {**good, 'confirm_layout': False},
                    {**good, 'expected_page': True}, {**good, 'match': 'a\nb'}, {**good, 'extra': 1}):
            with self.subTest(bad=bad), self.assertRaises(edit.NativeNumberingInputError):
                edit.validate_insert_step(bad)


class ParagraphRemove(unittest.TestCase):
    def test_removes_exactly_one_text_paragraph_with_one_native_action(self) -> None:
        hwp = _doc('Title', 'Item', 'Tail')
        result = Flow(hwp).step(match='Item', **REMOVE)
        self.assertTrue(result['ok'], result)
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Tail'])
        self.assertIn(('select_text', 1, 0, 2, 0, 0), hwp.calls)
        self.assertEqual(result['steps'][0]['result']['undo'], {'native_editing_actions': 1})

    def test_last_paragraph_is_refused_without_a_native_edit(self) -> None:
        hwp = _doc('Title', 'Item')
        result = Flow(hwp).step(match='Item', **REMOVE)
        self.assertFalse(result['ok'])
        self.assertEqual(result['steps'][0]['refusal_code'], 'target_is_last_paragraph')
        self.assertNotIn(('Run', 'Delete'), hwp.calls)

    def _refused_on_census(self, change, code) -> None:
        hwp = _doc('Title', 'Item', 'Tail')
        real = edit._read_protection

        def protected(reader):
            census = real(reader)
            for index, item in enumerate(census['paragraphs']):
                change(item, index)
            return census

        with patch.object(edit, '_read_protection', protected):
            result = Flow(hwp).step(match='Item', **REMOVE)
        self.assertFalse(result['ok'])
        self.assertEqual(result['steps'][0]['refusal_code'], code)
        self.assertNotIn(('Run', 'Delete'), hwp.calls)

    def test_paragraph_with_an_inline_picture_is_refused(self) -> None:
        # Issue #3 surface: removing text beside an inline picture must not remove the picture.
        self._refused_on_census(lambda item, _: item.update(inline_controls=['picture']) if item['text'] == 'Item' else None, 'target_has_controls')

    def test_next_paragraph_page_break_is_refused(self) -> None:
        # Observed natively: the next paragraph loses its PageBreak flag and a page disappears.
        self._refused_on_census(lambda item, _: item.update(page_break=True) if item['text'] == 'Tail' else None, 'neighbor_has_break')

    def test_table_cell_paragraph_is_refused(self) -> None:
        self._refused_on_census(lambda item, _: item.update(body_index=None) if item['text'] == 'Item' else None, 'target_not_in_body')


class UndoAccounting(unittest.TestCase):
    def test_an_undo_refusal_reaches_the_caller_with_its_reason(self) -> None:
        from app.local_cli_runtime import LocalCliRuntimeError
        from app.local_cli_service import LocalCliUndoRefused, as_http_error
        refused = as_http_error(LocalCliUndoRefused('undo refused: private detail'))
        self.assertEqual(refused.status_code, 409)
        self.assertIn('cannot be undone', refused.detail)
        self.assertNotIn('private detail', refused.detail)
        self.assertEqual(as_http_error(LocalCliRuntimeError('private')).detail, 'Local CLI native operation failed.')

    def test_one_counted_step_after_a_paragraph_edit(self) -> None:
        flow = Flow(_doc('Title', 'Item', 'Tail'))
        self.assertTrue(flow.step(match='Item', text='Next', position='after', **INSERT)['ok'])
        self.assertEqual(flow.binding['pending_logical_undo_count'], 1)

    def test_numbering_that_also_defines_the_outline_refuses_counted_undo(self) -> None:
        from app.local_cli_service import _bundle_step_single_undo
        one = {'op': 'official_numbering_apply', 'ok': True, 'result': {'undo': {'native_editing_actions': 1}}}
        two = {'op': 'official_numbering_apply', 'ok': True, 'result': {'undo': {'native_editing_actions': 2}}}
        self.assertTrue(_bundle_step_single_undo(one))
        self.assertFalse(_bundle_step_single_undo(two))


if __name__ == '__main__':
    unittest.main()
