"""An observation never drops the live selection it reports, through the real owning worker.

select_target leaves an active selection; hwpx_where (/local-cli/where) must report it and leave it, so a hyperlink
object_insert_exact whose expected_pos is the caret that where reported finds the selection still there. Without a
selection, where still reads the nearby paragraphs.
"""
from __future__ import annotations

import unittest

from tests.test_bundle_step_local_refusal import owner_for
from tests.test_guarded_bundle_observations import run
from tests.test_object_insert_exact import _FakeHwp


class HancomSelection(_FakeHwp):
    """Selections as Hancom keeps them: select_text makes one, set_pos and paragraph moves drop it."""

    @property
    def SelectionMode(self):  # noqa: N802
        return 1 if self.selection is not None else 0

    def select_text(self, spara, spos, epara, epos, slist=0):
        tokens = self.paras[spara]
        self.caret = [slist, spara, len(tokens) if epos < 0 else epos]
        self.selection = (spos, len(tokens) if epos < 0 else epos)
        return True

    def set_pos(self, list_id, para, pos):
        self.caret = [list_id, para, pos]
        self.selection = None
        return True


def where(owner):
    return owner.service.where()


class WhereKeepsSelection(unittest.TestCase):
    def test_where_reports_and_keeps_an_active_selection(self):
        hwp = HancomSelection()
        hwp.select(6, 11)  # 'world'
        owner = owner_for(self, hwp)
        seen = where(owner)
        self.assertEqual(hwp.selection, (6, 11))
        self.assertEqual(hwp.caret, [0, 0, 11])
        self.assertTrue(seen['cursor']['has_selection'], seen)
        self.assertEqual(seen['selection_mode'], 1)
        self.assertIn('active selection', seen['selection_summary'])
        self.assertFalse(owner.session._native_custody_held)

    def test_hyperlink_after_select_and_where(self):
        hwp = HancomSelection()
        hwp.select(6, 11)
        owner = owner_for(self, hwp)
        pos = where(owner)['cursor']['pos']
        step = {'op': 'object_insert_exact', 'kind': 'hyperlink', 'url': 'https://example.com/a', 'display_text': 'world',
                'expected_pos': pos, 'confirm_mutation': True}
        result = run(owner, step)
        self.assertTrue(result['ok'], result)
        xml = hwp.xml()
        self.assertEqual(xml.count('<FIELDBEGIN Type="Hyperlink" Command="https://example.com/a;'), 1, xml)
        self.assertIn('<FIELDBEGIN Type="Hyperlink" Command="https://example.com/a;1;0;0;"/><CHAR>world</CHAR><FIELDEND Type="Hyperlink"/>', xml)
        self.assertFalse(owner.session._native_custody_held)

    def test_without_a_selection_where_reads_the_nearby_paragraphs(self):
        hwp = HancomSelection()
        owner = owner_for(self, hwp)
        seen = where(owner)
        self.assertFalse(seen['cursor']['has_selection'])
        self.assertEqual(seen['current_paragraph_preview'], 'Hello world')
        self.assertEqual(hwp.caret, [0, 0, 5])
        self.assertIsNone(hwp.selection)


class ObjectInsertPreconditionsAreDiscoverable(unittest.TestCase):
    def test_schema_states_the_per_kind_preconditions(self):
        import json
        from hwpx_mcp.operations import ObjectInsertStep
        text = json.dumps(ObjectInsertStep.model_json_schema(), ensure_ascii=False)
        for phrase in ('live selection whose text is exactly display_text', 'hwpx_where keeps it', 'caret at the selection end',
                       'memo works with or without a selection', 'every other kind needs no selection',
                       'existing_header_footer', 'bookmark_exists'):
            self.assertIn(phrase, text)


if __name__ == '__main__':
    unittest.main()
