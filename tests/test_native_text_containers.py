"""Portable service regressions, not Hancom acceptance or simulated native proof."""
from __future__ import annotations
import unittest
from pydantic import ValidationError
from hwpx_mcp.schema import BundleStep
from pydantic import TypeAdapter
COMMAND = TypeAdapter(BundleStep)
from tests.test_native_paragraph_edit import Flow
from tests.native_format_fakes import char, control, paragraph
from tests.native_text_containers_fakes import ContainersHwp, container


class TextContainerSlice(unittest.TestCase):
    def test_backend_inventory_is_a_dedicated_read(self):
        hwp = ContainersHwp([paragraph('Body sentinel', container())])
        reply = Flow(hwp).step(op='text_container_inventory')
        self.assertTrue(reply['ok'], reply)
        result = reply['steps'][0]['result']
        self.assertTrue(result['read_only'])
        self.assertEqual(result['containers'][0]['kind'], 'text_box')
        self.assertEqual(result['containers'][0]['text'], 'Box text')
        self.assertEqual(hwp.executes(), [])

    def test_mcp_schema_exposes_creation_and_complete_text_binding(self):
        command = COMMAND.validate_python({'op': 'text_box_create', 'text': 'Caller box', 'width_mm': 40,
                                         'height_mm': 20, 'expected_pos': [0, 1, 0], 'confirm_layout': True})
        self.assertEqual(command.op, 'text_box_create')
        command = COMMAND.validate_python({'op': 'text_container_text_exact', 'target_id': 'text_box:101',
                                         'expected_hash': 'sha256:' + 'a' * 24, 'expected_text': 'Box text',
                                         'new_text': 'New box text', 'confirm_layout': True})
        self.assertEqual(command.expected_text, 'Box text')
        with self.assertRaises(ValidationError):
            COMMAND.validate_python({'op': 'text_container_text_exact', 'target_id': 'text_box:101',
                                    'expected_hash': 'sha256:' + 'a' * 24, 'new_text': 'New box text', 'confirm_layout': True})


def listing(hwp):
    reply = Flow(hwp).step(op='text_container_inventory')
    assert reply['ok'], reply
    return reply['steps'][0]['result']['containers']


def change(hwp, target=None, **updates):
    item = target or listing(hwp)[0]
    request = {'op': 'text_container_text_exact', 'target_id': item['target_id'], 'expected_hash': item['hash'],
               'expected_text': item['text'], 'new_text': 'Replacement text', 'confirm_layout': True}
    return Flow(hwp).step(**{**request, **updates})


class TextContainerEdits(unittest.TestCase):
    def test_whole_text_replacement_for_each_kind(self):
        for kind in ('text_box', 'caption', 'footnote'):
            with self.subTest(kind=kind):
                hwp = ContainersHwp([paragraph('Body sentinel'), paragraph('Tail', container(kind))])
                result = change(hwp)
                self.assertTrue(result['ok'], result)
                self.assertEqual(listing(hwp)[0]['text'], 'Replacement text')
                self.assertIn(('Run', 'Delete'), hwp.calls)
                self.assertEqual(''.join(x.get('c', '') for x in hwp.body[0]['atoms']), 'Body sentinel')

    def test_native_creation_carries_caller_text_and_inventory_identity(self):
        hwp = ContainersHwp([paragraph('Body sentinel'), paragraph('Anchor'), paragraph('Tail sentinel')])
        hwp.cursor = (0, 1, 0)
        reply = Flow(hwp).step(op='text_box_create', text='Caller supplied text', width_mm=40, height_mm=20,
                               expected_pos=[0, 1, 0], confirm_layout=True)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(listing(hwp)[0]['text'], 'Caller supplied text')
        self.assertEqual(listing(hwp)[0]['target_id'], 'text_box:999')

    def test_duplicate_words_do_not_choose_the_other_owner(self):
        hwp = ContainersHwp([paragraph('Body', container(ident='101')), paragraph('Tail', container(ident='102'))])
        item = listing(hwp)[1]
        reply = change(hwp, item)
        self.assertTrue(reply['ok'], reply)
        self.assertEqual([x['text'] for x in listing(hwp)], ['Box text', 'Replacement text'])

    def test_wrong_target_stale_hash_and_partial_text_are_clean_refusals(self):
        for update, code in (({'target_id': 'text_box:999'}, 'container_not_found'),
                             ({'expected_hash': 'sha256:'+'a'*24}, 'container_hash_mismatch'),
                             ({'expected_text': 'Box'}, 'container_text_mismatch')):
            with self.subTest(update=update):
                hwp = ContainersHwp()
                reply = change(hwp, **update)
                self.assertFalse(reply['ok'], reply)
                self.assertEqual(reply['steps'][0]['refusal_code'], code, reply)
                self.assertFalse(reply['steps'][0]['mutation_may_have_persisted'])
                self.assertEqual(hwp.executes(), [])

    def test_complete_read_never_substitutes_inventory_preview(self):
        hwp = ContainersHwp([paragraph('Body', container(text='Long text ' * 301))])
        item = listing(hwp)[0]
        self.assertIsNone(item['text'])
        self.assertFalse(item['text_complete'])
        reply = Flow(hwp).step(op='text_container_read', target_id=item['target_id'], expected_hash=item['hash'])
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(reply['steps'][0]['result']['container']['text'], 'Long text ' * 301)

    def test_unrelated_body_change_after_insert_holds(self):
        hwp = ContainersHwp()
        original = hwp.execute
        def corrupt(name, items):
            result = original(name, items)
            if name == 'InsertText':
                hwp.body[0]['atoms'].append({'cs': hwp.body[0]['atoms'][-1]['cs'], 'c': '!'})
            return result
        hwp.execute = corrupt
        reply = change(hwp)
        self.assertFalse(reply['ok'], reply)
        self.assertTrue(reply['steps'][0]['mutation_may_have_persisted'], reply)

    def test_malformed_entered_document_read_has_no_follow_on(self):
        hwp = ContainersHwp()
        def bad(fmt, option):
            hwp.calls.append(('FAULT',))
            return '<broken'
        hwp.GetTextFile = bad
        reply = Flow(hwp).step(op='text_container_inventory')
        self.assertFalse(reply['ok'], reply)
        self.assertEqual(hwp.calls[hwp.calls.index(('FAULT',))+1:], [], hwp.calls)

    def test_failed_mutation_has_no_follow_on_delete_or_restoration(self):
        hwp = ContainersHwp()
        item = listing(hwp)[0]
        original = hwp.HAction.Execute
        def fail(name, hset):
            if name == 'InsertText':
                hwp.calls.append(('FAULT',))
                return 1  # Not the documented BOOL True.
            return original(name, hset)
        hwp.HAction.Execute = fail
        reply = change(hwp, item)
        self.assertFalse(reply['ok'], reply)
        self.assertTrue(reply['steps'][0]['mutation_may_have_persisted'], reply)
        self.assertEqual(hwp.calls[hwp.calls.index(('FAULT',))+1:], [], hwp.calls[-15:])

    def test_wrong_owner_with_identical_text_is_not_edited(self):
        hwp = ContainersHwp([paragraph('Body', container(ident='101')), paragraph('Tail', container(ident='102'))])
        item = listing(hwp)[0]
        hwp.owners[2].ident = '777'
        reply = change(hwp, item)
        self.assertFalse(reply['ok'], reply)
        self.assertEqual(hwp.executes(), [])

    def test_noop_does_not_issue_mutations(self):
        hwp = ContainersHwp()
        reply = change(hwp, new_text='Box text')
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(hwp.executes(), [])


    def test_leading_note_or_caption_number_is_not_deleted(self):
        for kind in ('caption', 'footnote'):
            hwp = ContainersHwp([paragraph('Body', container(kind))])
            p = hwp.body[0]['atoms'][-1]['drawtext'][0]
            p['atoms'].insert(0, {'cs': char(), **control('<AUTONUM Number="1" NumberType="Footnote"><AUTONUMFORMAT Type="Digit"/></AUTONUM>', 'atno')})
            reply = change(hwp)
            self.assertTrue(reply['ok'], reply)
            self.assertEqual(p['atoms'][0]['ctrl'], 'atno')
            self.assertEqual(listing(hwp)[0]['text'], 'Replacement text')

    def test_multi_paragraph_and_mixed_format_are_explicit_refusals(self):
        for variant, code in (('multiple', 'container_multi_paragraph'), ('mixed', 'container_mixed_format')):
            hwp = ContainersHwp()
            item = hwp.body[-1]['atoms'][-1]
            if variant == 'multiple':
                item['drawtext'].append(paragraph('Another line'))
            else:
                item['drawtext'][0]['atoms'][0]['cs'] = char(Bold=True)
            row = listing(hwp)[0]
            self.assertFalse(row['editable'])
            self.assertEqual(row['refusal_code'], code)
            if variant == 'mixed':
                reply = change(hwp, row)
                self.assertEqual(reply['steps'][0]['refusal_code'], code, reply)
                self.assertEqual(hwp.executes(), [])

    def test_invalid_navigation_return_has_no_next_getter(self):
        hwp = ContainersHwp()
        item = listing(hwp)[0]
        def bad(*args):
            hwp.calls.append(('FAULT',))
            return 1
        hwp.SetPos = bad
        reply = change(hwp, item)
        self.assertFalse(reply['ok'], reply)
        self.assertEqual(hwp.calls[hwp.calls.index(('FAULT',))+1:], [])

    def test_invalid_shape_setter_has_no_follow_on_sdk(self):
        hwp = ContainersHwp()
        hwp.cursor = (0, 1, 0)
        def bad(*args):
            hwp.calls.append(('FAULT',))
            return True
        hwp.HParameterSet.HShapeObject.SetItem = bad
        reply = Flow(hwp).step(op='text_box_create', text='Caller text', width_mm=40, height_mm=20,
                               expected_pos=[0, 1, 0], confirm_layout=True)
        self.assertFalse(reply['ok'], reply)
        self.assertEqual(hwp.calls[hwp.calls.index(('FAULT',))+1:], [])

    def test_non_target_definition_and_owner_shell_changes_are_held(self):
        edits = (
            lambda text: text.replace('Name="함초롬바탕"', 'Name="Changed font"'),
            lambda text: text.replace('Width="11339"', 'Width="11340"'),
        )
        for corrupt in edits:
            hwp = ContainersHwp()
            item = listing(hwp)[0]
            original = hwp.GetTextFile
            applied = []
            def changed(fmt, option):
                text = original(fmt, option)
                if any(c[:2] == ('Run', 'Delete') for c in hwp.calls) and not option:
                    altered = corrupt(text)
                    applied.append(altered != text)
                    return altered
                return text
            hwp.GetTextFile = changed
            reply = change(hwp, item)
            self.assertTrue(any(applied), 'adversary must actually alter this fixture')
            self.assertFalse(reply['ok'], reply)
            self.assertTrue(reply['steps'][0]['mutation_may_have_persisted'], reply)

    def test_non_target_media_payload_change_is_held(self):
        import xml.etree.ElementTree as ET
        hwp = ContainersHwp()
        item = listing(hwp)[0]
        original = hwp.GetTextFile
        applied = []
        def changed(fmt, option):
            text = original(fmt, option)
            if any(c[:2] == ('Run', 'Delete') for c in hwp.calls) and not option:
                root = ET.fromstring(text)
                media = next(e for e in root.iter() if e.tag.lower() == 'bindata')
                media.text = 'Y2hhbmdlZCBtZWRpYQ=='
                applied.append(True)
                return ET.tostring(root, encoding='unicode')
            return text
        hwp.GetTextFile = changed
        reply = change(hwp, item)
        self.assertTrue(applied)
        self.assertFalse(reply['ok'], reply)
        self.assertTrue(reply['steps'][0]['mutation_may_have_persisted'], reply)


if __name__ == '__main__':
    unittest.main()
