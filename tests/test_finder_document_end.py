"""The guarded paragraph finder at the document end, through the real owning worker and bundle.

A BOOL MoveRight wrapper returns exactly False when the match ends the document. The finder ends the search
there only after proving it: MoveDocEnd confirmed and the exact caret equal to the hit's selection end. Then an
insert after the last paragraph works and ambiguity is still a clean refusal. A False MoveRight anywhere else,
an unconfirmed MoveDocEnd or an unreadable caret stay technical failures that hold the session.
"""
from __future__ import annotations

import unittest

from tests.test_bundle_step_local_refusal import owner_for
from tests.test_guarded_bundle_observations import run
from tests.test_native_paragraph_edit import _doc
from tests.test_required_sdk_read_origin import fake_finder

INSERT = {'op': 'paragraph_insert_exact', 'expected_page': 1, 'confirm_layout': True}


def bool_motion(hwp, *, false_everywhere=False, doc_end_result=True, unreadable_pos=False):
    """Give the fake BOOL motion wrappers: MoveRight is False when the selected hit ends the document."""
    plain_right = hwp.MoveRight
    plain_get_pos = hwp.get_pos

    def move_right():
        last = len(hwp.paragraphs) - 1
        at_end = hwp.selection_para == last
        plain_right()
        return False if (false_everywhere or at_end) else True

    def move_doc_end():
        hwp.calls.append(('MoveDocEnd',))
        last = len(hwp.paragraphs) - 1
        hwp.cursor = (0, last, len(str(hwp.paragraphs[last]['text'])))
        hwp.selection_para = None
        return doc_end_result

    def get_pos():
        if unreadable_pos and ('MoveDocEnd',) in hwp.calls:
            return (0, 'bad', 0)
        return plain_get_pos()
    hwp.MoveRight = move_right
    hwp.MoveDocEnd = move_doc_end
    hwp.get_pos = get_pos
    return hwp


class FinderAtDocumentEnd(unittest.TestCase):
    def owned(self, hwp):
        fake_finder(hwp)
        return owner_for(self, hwp)

    def test_insert_after_the_last_paragraph(self):
        hwp = bool_motion(_doc('Title', 'Item', 'Tail'))
        owner = self.owned(hwp)
        result = run(owner, dict(INSERT, match='Tail', text='Next', position='after'))
        self.assertTrue(result['ok'], result)
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Item', 'Tail', 'Next'])
        self.assertFalse(owner.session._native_custody_held)

    def test_duplicate_match_ending_the_document_is_still_ambiguous(self):
        hwp = bool_motion(_doc('Title', 'Item', 'Item'))
        owner = self.owned(hwp)
        result = run(owner, dict(INSERT, match='Item', text='Next', position='after'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['steps'][0].get('refusal_code'), 'target_ambiguous', result)
        self.assertFalse(owner.session._native_custody_held)
        self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Item', 'Item'])

    def test_unproven_end_states_stay_technical(self):
        for label, kwargs in (('false_mid_document', {'false_everywhere': True}),
                              ('doc_end_unconfirmed', {'doc_end_result': False}),
                              ('caret_unreadable', {'unreadable_pos': True})):
            with self.subTest(label):
                hwp = bool_motion(_doc('Title', 'Item', 'Tail'), **kwargs)
                owner = self.owned(hwp)
                result = run(owner, dict(INSERT, match='Item' if label == 'false_mid_document' else 'Tail', text='Next', position='after'))
                self.assertFalse(result['ok'])
                self.assertNotIn('refusal_code', result['steps'][0])
                self.assertTrue(owner.session._native_custody_held, result)
                self.assertEqual([p['text'] for p in hwp.paragraphs], ['Title', 'Item', 'Tail'])


if __name__ == '__main__':
    unittest.main()
