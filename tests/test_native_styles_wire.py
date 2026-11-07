"""The real backend's command-bundle bodies for the named-style commands, as the MCP tests receive them.

Each reply comes from a real owned service (its public projection included) over tests.native_styles_fakes, and must equal
tests/fixtures/native_styles/backend_replies.json, which mcp_tests/test_styles_mcp.py sends through the official MCP client.
Not Hancom evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.native_styles_fakes import _Typed, styles_document
from tests.test_native_styles import plain_document
from tests.test_repair_runtime_safety import owned_doc

FIXTURE = Path(__file__).parent / 'fixtures' / 'native_styles' / 'backend_replies.json'


def _bundle(owned, **fields) -> dict:
    with patch('app.local_cli_service.snapshot_live_location', return_value={}), patch('app.local_cli_runtime.snapshot_live_location', return_value={}):
        return owned.service.command_bundle(steps=[{'label': 'wire', **fields}])


def _row(owned, name: str) -> dict:
    return _bundle(owned, op='style_inventory', name=name)['steps'][0]['result']['styles'][0]


class _RaisingApply(_Typed):
    """HStyle whose Apply setter raises: a technical fault after the block was selected."""

    def __setattr__(self, name: str, value) -> None:
        if name == 'Apply':
            raise RuntimeError('native Apply setter failed')
        super().__setattr__(name, value)


def replies(case: unittest.TestCase) -> dict[str, dict]:
    found: dict[str, dict] = {}
    owned = owned_doc(case, styles_document(), undo_stack=[], redo_stack=[])
    found['inventory'] = _bundle(owned, op='style_inventory')
    apply = {'op': 'style_apply_exact', 'style_name': '개요 1', 'paragraph_index': 3, 'expected_text': '바탕 둘째 문단', 'expected_page': 1, 'confirm_layout': True}
    found['refusal'] = _bundle(owned, **apply, expected_style_hash='sha256:' + '0' * 24)
    found['apply'] = _bundle(owned, **apply, expected_style_hash=_row(owned, '개요 1')['hash'])
    owned = owned_doc(case, styles_document(), undo_stack=[], redo_stack=[])
    listing = _bundle(owned, op='style_inventory')['steps'][0]['result']
    based = next(item for item in listing['styles'] if item['name'] == '본문')
    found['create'] = _bundle(owned, op='style_create_exact', name='강조 본문', kind='paragraph', based_on='본문', expected_based_on_hash=based['hash'],
                              expected_styles_hash=listing['styles_hash'], size_pt=14, bold=True, confirm_layout=True)
    owned = owned_doc(case, styles_document(), undo_stack=[], redo_stack=[])
    listing = _bundle(owned, op='style_inventory')['steps'][0]['result']
    based = next(item for item in listing['styles'] if item['name'] == '본문')
    found['next_style_refusal'] = _bundle(owned, op='style_create_exact', name='소제목', kind='paragraph', based_on='본문', expected_based_on_hash=based['hash'],
                                          expected_styles_hash=listing['styles_hash'], next_style='쪽 번호', confirm_layout=True)
    hwp = styles_document()
    owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
    bound = _row(owned, '개요 1')['hash']
    hwp.HParameterSet.HStyle = _RaisingApply('Style')
    found['technical_hold'] = _bundle(owned, **apply, expected_style_hash=bound)
    hwp = styles_document()
    owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
    bound = _row(owned, '개요 1')['hash']
    hwp.HParameterSet = _Typed('ParameterSets')  # Dynamic, as a COM object is, and without HStyle: its AttributeError is not absence.
    found['capability_hold'] = _bundle(owned, **apply, expected_style_hash=bound)
    hwp = styles_document()
    owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
    bound = _row(owned, '개요 1')['hash']
    del hwp.HAction  # A plain wrapper object without HAction at all.
    found['absent_refusal'] = _bundle(owned, **apply, expected_style_hash=bound)
    hwp = styles_document()
    owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
    hwp.get_text_file = lambda *_args: ''
    found['document_text_hold'] = _bundle(owned, op='style_inventory')
    for name in ('update', 'held'):
        hwp = plain_document()
        owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
        bound = _row(owned, '본문')
        hwp.change_skips_uses = name == 'held'
        found[name] = _bundle(owned, op='style_update_exact', style_name='본문', expected_style_hash=bound['hash'], expected_uses=bound['uses']['total'],
                              expected_uses_hash=bound['uses']['hash'], all_uses=True, size_pt=11, confirm_layout=True)
    hwp = plain_document()
    owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
    bound = _row(owned, '본문')
    # The shape read at 본문's use carries a shade colour 본문 does not have, where the binding does not read: the style and its uses are
    # at 11 pt with that shade, so the proof holds the one unrequested item.
    hwp.char_shape_from = lambda owner, source: {**source, 'ShadeColor': 0xCCCCCC}
    found['update_proof_hold'] = _bundle(owned, op='style_update_exact', style_name='본문', expected_style_hash=bound['hash'], expected_uses=bound['uses']['total'],
                                         expected_uses_hash=bound['uses']['hash'], all_uses=True, size_pt=11, confirm_layout=True)
    hwp = plain_document()
    owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
    bound = _row(owned, '본문')
    # The shape read at 본문's use lacks a documented item: refused before the action is made.
    hwp.char_shape_drop = ('BorderFill',)
    found['shape_refusal'] = _bundle(owned, op='style_update_exact', style_name='본문', expected_style_hash=bound['hash'], expected_uses=bound['uses']['total'],
                                     expected_uses_hash=bound['uses']['hash'], all_uses=True, size_pt=11, confirm_layout=True)
    hwp = plain_document()
    owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
    bound = _row(owned, '본문')
    found['rename_refusal'] = _bundle(owned, op='style_update_exact', style_name='본문', expected_style_hash=bound['hash'], expected_uses=bound['uses']['total'],
                                      expected_uses_hash=bound['uses']['hash'], all_uses=True, new_name='본문 글', confirm_layout=True)
    for name in ('variant_refusal', 'legacy_hold', 'get_default_hold'):
        hwp = styles_document()
        owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
        listing = _bundle(owned, op='style_inventory')['steps'][0]['result']
        based = next(item for item in listing['styles'] if item['name'] == '본문')
        # GetDefault answers True, or an int other than the 1 it answered natively: held at its fixed site either way.
        hwp.legacy_defaults_result = {'legacy_hold': True, 'get_default_hold': 2}.get(name, 1)
        fields = {'align': 'center'} if name == 'variant_refusal' else {'size_pt': 14}
        found[name] = _bundle(owned, op='style_create_exact', name='보고서 본문', kind='paragraph', based_on='본문', expected_based_on_hash=based['hash'],
                              expected_styles_hash=listing['styles_hash'], confirm_layout=True, **fields)
    hwp = styles_document()
    owned = owned_doc(case, hwp, undo_stack=[], redo_stack=[])
    listing = _bundle(owned, op='style_inventory')['steps'][0]['result']
    based = next(item for item in listing['styles'] if item['name'] == '본문')
    # Put at the end (5), not in the slot after 본문 the set's Next was planned for: Next 3, planned for 개요 1, names 강조.
    hwp.add_at = 5
    found['proof_hold'] = _bundle(owned, op='style_create_exact', name='보고서 본문', kind='paragraph', based_on='본문', expected_based_on_hash=based['hash'],
                                  expected_styles_hash=listing['styles_hash'], next_style='개요 1', confirm_layout=True)
    return json.loads(json.dumps(found, ensure_ascii=False))


class Wire(unittest.TestCase):
    def test_the_fixture_is_what_the_backend_returns_now(self) -> None:
        produced = replies(self)
        self.assertEqual(produced, json.loads(FIXTURE.read_text(encoding='utf-8')))

    def test_the_replies_are_the_outcomes_the_mcp_tests_name(self) -> None:
        produced = replies(self)
        for name in ('inventory', 'apply', 'create', 'update'):
            self.assertTrue(produced[name]['ok'], produced[name])
        refusal = produced['refusal']['steps'][0]
        self.assertEqual((produced['refusal']['ok'], refusal['refusal_code'], refusal['mutation_may_have_persisted']), (False, 'style_hash_mismatch', False))
        held = produced['held']
        self.assertEqual((held['ok'], held['steps'][0]['mutation_may_have_persisted'], held.get('native_custody_held')), (False, True, True))
        next_style = produced['next_style_refusal']['steps'][0]
        self.assertEqual((produced['next_style_refusal']['ok'], next_style.get('refusal_code'), next_style['mutation_may_have_persisted']),
                         (False, 'style_kind_unsupported', False))
        technical = produced['technical_hold']
        step = technical['steps'][0]
        self.assertNotIn('refusal_code', step, 'a raising parameter write is never a clean refusal')
        self.assertEqual((technical['ok'], step['mutation_may_have_persisted'], step.get('native_custody_held'), technical.get('native_custody_held')),
                         (False, False, True, True))
        for name in ('capability_hold', 'document_text_hold'):
            outcome = produced[name]
            step = outcome['steps'][0]
            self.assertNotIn('refusal_code', step, name)
            self.assertEqual((outcome['ok'], step['mutation_may_have_persisted'], step.get('native_custody_held'), outcome.get('native_custody_held')),
                             (False, False, True, True), name)
        for name, code in (('variant_refusal', 'style_variant_unsupported'), ('rename_refusal', 'style_rename_unsupported')):
            step = produced[name]['steps'][0]
            self.assertEqual((produced[name]['ok'], step.get('refusal_code'), step['mutation_may_have_persisted'], step.get('native_custody_held')),
                             (False, code, False, None), name)
        for name, shape in (('legacy_hold', 'true'), ('get_default_hold', 'int')):
            legacy = produced[name]['steps'][0]
            self.assertNotIn('refusal_code', legacy)
            self.assertEqual((legacy['mutation_may_have_persisted'], legacy.get('native_custody_held'), legacy['failure_diagnostic'].get('native_site'),
                              legacy['failure_diagnostic'].get('native_return')), (False, True, 'get_default.call', shape), name)
        self.assertEqual(produced['create']['steps'][0]['result']['result']['native_actions'], ['Execute:StyleAddNew'])
        self.assertEqual(produced['update']['steps'][0]['result']['result']['native_actions'], ['Execute:StyleDirectEdit'])
        proof = produced['proof_hold']
        step = proof['steps'][0]
        self.assertEqual((proof['ok'], step['mutation_may_have_persisted'], step.get('native_custody_held'), 'refusal_code' in step), (False, True, True, False))
        self.assertEqual(step['style_proof']['problems'], [{'predicate': 'new_style_wrong', 'name': '보고서 본문', 'new_index': 5, 'fields': ['next'],
                                                            'expected': '개요 1', 'found': '강조'}])
        update_proof = produced['update_proof_hold']
        step = update_proof['steps'][0]
        self.assertEqual((update_proof['ok'], step['mutation_may_have_persisted'], step.get('native_custody_held'), 'refusal_code' in step), (False, True, True, False))
        self.assertEqual(step['style_proof']['problems'], [{'predicate': 'changed_style_wrong', 'name': '본문', 'old_index': 1, 'new_index': 1, 'fields': ['character'],
                                                            'size_expected': 11.0, 'size_found': 11.0, 'character_items': ['attr_shadecolor'],
                                                            'character_item_count': 1, 'unrequested_changed': True}])
        shape = produced['shape_refusal']['steps'][0]
        self.assertEqual((produced['shape_refusal']['ok'], shape.get('refusal_code'), shape['mutation_may_have_persisted'], shape.get('native_custody_held')),
                         (False, 'style_shape_unverified', False, None))
        self.assertIn('BorderFill', shape['refusal_message'])
        absent = produced['absent_refusal']['steps'][0]
        self.assertEqual((produced['absent_refusal']['ok'], absent.get('refusal_code'), absent['mutation_may_have_persisted'], absent.get('native_custody_held')),
                         (False, 'native_unavailable', False, None))


if __name__ == '__main__':
    unittest.main()
