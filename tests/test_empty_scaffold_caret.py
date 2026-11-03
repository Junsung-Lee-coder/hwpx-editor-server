"""replace_empty_native_list_scaffold at a caret on an empty paragraph, as Hancom behaves.

Hancom reports no selection for an empty paragraph, so the engine proves the paragraph empty from its end position
(MoveParaEnd stays at position 0) and types the items into it; a paragraph with text is still refused when
require_empty_scaffold is set, and nothing is deleted.
"""
from __future__ import annotations

import unittest

from app.edit_ops import EditOperationError, apply_edit_operations, normalize_operations
from tests.test_paragraph_replace_break import ParaDoc


class HancomParaDoc(ParaDoc):
    def get_selected_pos(self):
        if self.sel is not None and self.sel[0] == self.sel[2] and self.sel[1] == self.sel[3]:
            return (False, 0, 0, 0, 0, 0, 0)  # an empty range is no selection
        return super().get_selected_pos()

    def run(self, name):
        if name == 'MoveParaEnd':
            para, _pos = self.caret
            self.caret, self.sel = (para, len(self.paras[para]['text'])), None
            return True
        return super().run(name)


def scaffold(cursor_para, **extra):
    return {'op': 'replace_empty_native_list_scaffold', 'cursor_pos': [0, cursor_para, 0], 'kind': 'number',
            'require_empty_scaffold': True, 'items': [{'text': '첫째 목록 항목', 'kind': 'number', 'level': 1},
                                                      {'text': '둘째 목록 항목', 'kind': 'number', 'level': 1}], **extra}


class EmptyScaffoldAtCaret(unittest.TestCase):
    def doc(self):
        return HancomParaDoc(('목록 시험 문서', 'body'), ('목록 앞 문단', 'body'), ('', 'body'), ('목록 뒤 문단', 'body'))

    def test_items_go_into_the_empty_paragraph(self):
        hwp = self.doc()
        summary = apply_edit_operations(hwp, normalize_operations([scaffold(2)]))
        self.assertEqual(hwp.texts(), ['목록 시험 문서', '목록 앞 문단', '첫째 목록 항목', '둘째 목록 항목', '목록 뒤 문단'])
        self.assertEqual(summary[0]['matches'], 1)
        self.assertNotIn(('run', 'Delete'), hwp.calls)

    def test_a_paragraph_with_text_is_still_refused(self):
        hwp = self.doc()
        with self.assertRaisesRegex(EditOperationError, 'expected an empty native scaffold paragraph'):
            apply_edit_operations(hwp, normalize_operations([scaffold(1)]))
        self.assertEqual(hwp.texts(), ['목록 시험 문서', '목록 앞 문단', '', '목록 뒤 문단'])


if __name__ == '__main__':
    unittest.main()
