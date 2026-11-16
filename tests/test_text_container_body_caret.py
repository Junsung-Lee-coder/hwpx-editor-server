"""Portable source contracts; modeled SDK only, no native navigation qualification."""
import unittest
from pydantic import TypeAdapter, ValidationError
from hwpx_mcp.schema import BundleStep
from tests.native_text_containers_fakes import ContainersHwp, container
from tests.native_format_fakes import paragraph
from tests.test_native_paragraph_edit import Flow

class BodyCaretContract(unittest.TestCase):
    def test_guarded_owner_anchor_navigation_preserves_complete_text(self):
        h = ContainersHwp([paragraph('', container(text='Public source model'))])
        h.cursor = (2, 0, len('Public source model'))
        flow = Flow(h)
        box = flow.step(op='text_container_inventory')['steps'][0]['result']['containers'][0]
        request = {'op': 'text_container_body_caret', 'target_id': box['target_id'], 'expected_hash': box['hash'],
                   'expected_text': box['text'], 'confirm_navigation': True}
        typed = TypeAdapter(BundleStep).validate_python(request)
        reply = flow.step(**typed.model_dump(exclude_none=True))
        self.assertTrue(reply['ok'], reply)
        result = reply['steps'][0]['result']
        self.assertEqual(result['anchor_basis'], 'native_GetAnchorPos_exact_owner_census')
        self.assertEqual(result['body_anchor'], list(h.cursor))
        self.assertEqual(h.cursor[0], 0)
        self.assertEqual(result['container']['text'], box['text'])
        self.assertTrue(result['result']['navigation_only'])
        self.assertFalse(result['result']['content_changed'])

    def test_confirmation_is_exact_boolean_and_no_coordinates_escape(self):
        h = ContainersHwp([paragraph('', container())]); flow = Flow(h)
        box = flow.step(op='text_container_inventory')['steps'][0]['result']['containers'][0]
        request = {'op':'text_container_body_caret','target_id':box['target_id'],'expected_hash':box['hash'],
                   'expected_text':box['text'],'confirm_navigation':True}
        for value in (1, 0, 'true', None, False):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                TypeAdapter(BundleStep).validate_python({**request,'confirm_navigation':value})
        with self.assertRaises(ValidationError):
            TypeAdapter(BundleStep).validate_python({**request,'pos':[0,0,0]})
