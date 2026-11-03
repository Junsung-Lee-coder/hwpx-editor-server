"""Paragraph replacement and the paragraph break, through the real queued dispatcher and the real compiler.

Direct queued replace_paragraph_safe / paragraph_replace_native keep their literal semantics: the found
paragraph is selected together with its paragraph break, so a replacement without a trailing "\\r\\n" joins the
next paragraph. keep_paragraph_break=true selects only the paragraph's content, so the break and the next
paragraph (with its shape) stay untouched. The authoring compiler emits that mode for an exact target paragraph,
so a one-line section followed by a heading no longer merges the heading, and the last paragraph needs no
special case.
"""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from app import edit_ops, template_engine


class _HAction:
    def __init__(self, doc):
        self.doc = doc

    def Run(self, name):
        return self.doc.run(name)


class ParaDoc:
    """One body list of paragraphs with a shape each; selection, find, delete and typing as Hancom does them.

    Deleting a range that spans a paragraph break merges the two paragraphs into the first one's shape; typing a
    paragraph break splits the current paragraph and both halves keep its shape.
    """

    def __init__(self, *paragraphs):
        self.paras = [{'text': text, 'shape': shape} for text, shape in paragraphs]
        self.caret = (0, 0)
        self.sel = None
        self.HAction = _HAction(self)
        self.calls = []

    def texts(self):
        return [p['text'] for p in self.paras]

    def shapes(self):
        return [p['shape'] for p in self.paras]

    def MoveDocBegin(self):
        self.caret, self.sel = (0, 0), None

    def find(self, text, direction='Forward', MatchCase=1, WholeWordOnly=0):
        para, pos = self.caret
        for index in range(para, len(self.paras)):
            start = self.paras[index]['text'].find(text, pos if index == para else 0)
            if start >= 0:
                self.sel = (index, start, index, start + len(text))
                self.caret = (index, start + len(text))
                return True
        return False

    def get_selected_pos(self):
        if self.sel is None:
            return (False, 0, 0, 0, 0, 0, 0)
        sp, spos, ep, epos = self.sel
        return (True, 0, sp, spos, 0, ep, epos)

    def get_pos(self):
        return (0, *self.caret)

    def set_pos(self, list_id, para, pos):
        self.caret = (para, len(self.paras[para]['text']) if pos < 0 else pos)
        self.sel = None

    def select_text(self, spara, spos=None, epara=None, epos=None, slist=0):
        if isinstance(spara, tuple):
            _, _, spara, spos, _, epara, epos = spara
        end = len(self.paras[epara]['text']) if epos < 0 else epos
        self.calls.append(('select_text', spara, spos, epara, epos))
        self.sel = (spara, spos, epara, end)
        self.caret = (epara, end)
        return True

    def get_selected_text(self, keep_select=True):
        sp, spos, ep, epos = self.sel
        if sp == ep:
            return self.paras[sp]['text'][spos:epos]
        parts = [self.paras[sp]['text'][spos:]] + [p['text'] for p in self.paras[sp + 1:ep]] + [self.paras[ep]['text'][:epos]]
        return '\r\n'.join(parts)

    def run(self, name):
        self.calls.append(('run', name))
        if name == 'MoveSelNextParaBegin':
            para, pos = self.caret
            end = (para + 1, 0) if para + 1 < len(self.paras) else (para, len(self.paras[para]['text']))
            self.sel = (para, pos, *end)
            self.caret = end
            return True
        if name in ('Delete', 'Erase') and self.sel is not None:
            sp, spos, ep, epos = self.sel
            merged = {'text': self.paras[sp]['text'][:spos] + self.paras[ep]['text'][epos:], 'shape': self.paras[sp]['shape']}
            self.paras[sp:ep + 1] = [merged]
            self.caret, self.sel = (sp, spos), None
            return True
        return False

    def insert_text(self, text):
        para, pos = self.caret
        current = self.paras[para]
        head, tail = current['text'][:pos], current['text'][pos:]
        pieces = text.split('\r\n')
        new = [{'text': head + pieces[0], 'shape': current['shape']}]
        new += [{'text': piece, 'shape': current['shape']} for piece in pieces[1:]]
        new[-1]['text'] += tail
        self.paras[para:para + 1] = new
        self.caret = (para + len(pieces) - 1, len(new[-1]['text']) - len(tail))

    def get_text_file(self, option=''):
        return '\r\n'.join(self.texts())


def doc():
    return ParaDoc(('1. 개요', 'heading'), ('담당자 기준 본문입니다.', 'body'), ('2. 추진 계획', 'heading'), ('추진 계획 본문입니다.', 'body'))


def replace(op, **fields):
    return {'op': op, 'find': '담당자 기준 본문입니다.', 'replace': '작성 본문.', 'apply': 'first',
            'match_case': True, 'whole_word': False, 'allow_packed_paragraph': True, **fields}


class DirectQueuedLiteralSemantics(unittest.TestCase):
    def test_without_trailing_break_the_next_paragraph_joins(self):
        for op in ('replace_paragraph_safe', 'paragraph_replace_native'):
            with self.subTest(op=op):
                hwp = doc()
                edit_ops.apply_edit_operations(hwp, [replace(op)])
                self.assertEqual(hwp.texts(), ['1. 개요', '작성 본문.2. 추진 계획', '추진 계획 본문입니다.'])
                self.assertIn(('run', 'MoveSelNextParaBegin'), hwp.calls)

    def test_a_trailing_break_keeps_the_paragraphs_apart(self):
        hwp = doc()
        edit_ops.apply_edit_operations(hwp, [replace('paragraph_replace_native', replace='작성 본문.\r\n')])
        self.assertEqual(hwp.texts(), ['1. 개요', '작성 본문.', '2. 추진 계획', '추진 계획 본문입니다.'])


class KeepParagraphBreak(unittest.TestCase):
    def test_next_heading_and_its_shape_stay(self):
        for op in ('replace_paragraph_safe', 'paragraph_replace_native'):
            with self.subTest(op=op):
                hwp = doc()
                edit_ops.apply_edit_operations(hwp, [replace(op, keep_paragraph_break=True, expected_present_after='작성 본문.',
                                                             expected_absent_after='담당자 기준 본문입니다.')])
                self.assertEqual(hwp.texts(), ['1. 개요', '작성 본문.', '2. 추진 계획', '추진 계획 본문입니다.'])
                self.assertEqual(hwp.shapes(), ['heading', 'body', 'heading', 'body'])
                self.assertNotIn(('run', 'MoveSelNextParaBegin'), hwp.calls)

    def test_last_paragraph(self):
        hwp = doc()
        edit_ops.apply_edit_operations(hwp, [replace('paragraph_replace_native', find='추진 계획 본문입니다.', keep_paragraph_break=True)])
        self.assertEqual(hwp.texts(), ['1. 개요', '담당자 기준 본문입니다.', '2. 추진 계획', '작성 본문.'])
        self.assertEqual(hwp.shapes(), ['heading', 'body', 'heading', 'body'])

    def test_multi_line_replacement_stays_before_the_next_paragraph(self):
        hwp = doc()
        edit_ops.apply_edit_operations(hwp, [replace('paragraph_replace_native', replace='첫 줄.\r\n둘째 줄.', keep_paragraph_break=True)])
        self.assertEqual(hwp.texts(), ['1. 개요', '첫 줄.', '둘째 줄.', '2. 추진 계획', '추진 계획 본문입니다.'])
        self.assertEqual(hwp.shapes(), ['heading', 'body', 'body', 'heading', 'body'])

    def test_flag_must_be_a_boolean(self):
        hwp = doc()
        with self.assertRaises(edit_ops.EditOperationError):
            edit_ops.normalize_operations([replace('paragraph_replace_native', keep_paragraph_break='yes')])
        self.assertEqual(hwp.texts()[1], '담당자 기준 본문입니다.')


HS = 'http://www.hancom.co.kr/hwpml/2011/section'
HP = 'http://www.hancom.co.kr/hwpml/2011/paragraph'


def template(path, *texts):
    para = '<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:t>{}</hp:t></hp:run></hp:p>'
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/hwp+zip')
        z.writestr('Contents/section0.xml', f'<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">'
                   + ''.join(para.format(t) for t in texts) + '</hs:sec>')
    return path


class CompilerUsesKeepParagraphBreak(unittest.TestCase):
    def compiled(self, body_lines, *texts):
        with tempfile.TemporaryDirectory() as tmp:
            source = template(Path(tmp) / 't.hwpx', *texts)
            first = template_engine.compile_authoring_payload(source, '# 개요\n\n' + body_lines + '\n')
            target = first['target_recommendations'][0]['recommended_resolved_target_id']
            plan = template_engine.compile_authoring_payload(source, f'# 개요\n@resolved_target_id: {target}\n\n' + body_lines + '\n')
        ops = [o for o in plan['edit_plan']['operations'] if o['op'] == 'paragraph_replace_native']
        self.assertEqual(len(ops), 1, plan['edit_plan']['operations'])
        return ops[0]

    def test_one_line_section_followed_by_a_heading(self):
        op = self.compiled('작성 본문.', '시험 문서', '1. 개요', '담당자 기준 본문입니다.', '2. 추진 계획', '추진 계획 본문입니다.')
        self.assertIs(op.get('keep_paragraph_break'), True)
        self.assertEqual(op['replace'], '작성 본문.')
        hwp = ParaDoc(('시험 문서', 'body'), ('1. 개요', 'heading'), ('담당자 기준 본문입니다.', 'body'), ('2. 추진 계획', 'heading'), ('추진 계획 본문입니다.', 'body'))
        edit_ops.apply_edit_operations(hwp, edit_ops.normalize_operations([op]))
        self.assertEqual(hwp.texts(), ['시험 문서', '1. 개요', '작성 본문.', '2. 추진 계획', '추진 계획 본문입니다.'])
        self.assertEqual(hwp.shapes(), ['body', 'heading', 'body', 'heading', 'body'])

    def test_one_line_section_on_the_last_paragraph(self):
        op = self.compiled('작성 본문.', '시험 문서', '1. 개요', '담당자 기준 본문입니다.')
        self.assertIs(op.get('keep_paragraph_break'), True)
        self.assertEqual(op['replace'], '작성 본문.')

    def test_multi_line_section_has_no_trailing_break(self):
        op = self.compiled('첫 줄입니다.\n\n둘째 줄입니다.', '시험 문서', '1. 개요', '담당자 기준 본문입니다.', '2. 추진 계획', '추진 계획 본문입니다.')
        self.assertIs(op.get('keep_paragraph_break'), True)
        self.assertFalse(op['replace'].endswith('\r\n'), op['replace'])
        self.assertIn('\r\n', op['replace'])


if __name__ == '__main__':
    unittest.main()
