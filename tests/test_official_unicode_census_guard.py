"""Captured structural replay, NOT a native execution or observed UNICODE output.

raw-1/raw-2 are authenticated retained HWPML2X snapshots. The two UNICODE
strings below are deliberately abstract SYNTHETIC values, not a guessed
number prefix. SDK/COM actions are modelled by the existing recording fake;
the public mutator, census, comparisons, bundle and owning runtime are real.
The two hash-pinned snapshots of a generated test document are bundled under
tests/fixtures/native_numbering/unicode_guard.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from app import native_numbering as nn
from app.native_numbering_census import live_protection_reader, parse_hwpml2x_census
from tests.native_numbering_fakes import FakeCtrl, FakeHwp
from tests.test_native_numbering_bundle_translation import NUMBERING_STEP, _run_bundle
from tests.test_native_numbering_runtime_custody import CustodyAssertions, Owned

CAPTURES = Path(__file__).resolve().parent / 'fixtures' / 'native_numbering' / 'unicode_guard'
PINS = {
    'raw-1.hwpml': ('14ca007a678d4f01bba183b1b857d9a588fe595a4e37b32fffb43ff1bfd5f822', 29195),
    'raw-2.hwpml': ('e36148c0b6b5676635d0c1cc8bb81cd5916533963e96438aff33a4484d63e033', 29754),
}
SYNTHETIC_UNICODE_BEFORE = 'SYNTHETIC abstract export representation A'
SYNTHETIC_UNICODE_AFTER = 'SYNTHETIC abstract export representation B'


class CaptureSetupError(RuntimeError):
    """Input/setup failures must not masquerade as behavioral RED."""


def captured(name):
    raw = (CAPTURES / name).read_bytes()
    if (hashlib.sha256(raw).hexdigest(), len(raw)) != PINS[name]:
        raise CaptureSetupError('captured HWPML pin mismatch: ' + name)
    # The declaration says UTF-16, but these retained bytes are UTF-8.
    # Verify bytes first; pass decoded Unicode to the real parser, never rewrite evidence.
    return raw.decode('utf-8')


def census_pair():
    return tuple(parse_hwpml2x_census(captured(name)) for name in PINS)


def altered(after, kind):
    """Explicitly SYNTHETIC counterexamples in memory, not captured native output."""
    root = ET.fromstring(after)
    paras = list(root.find('BODY').iter('P'))
    if kind == 'body-text-loss':
        paras[1].find('.//CHAR').text = ''
    elif kind == 'literal-typed-prefix':
        char = paras[1].find('.//CHAR')
        char.text = '1. ' + char.text
    elif kind == 'unrelated-paragraph':
        paras[2].set('SyntheticUnrelatedAttribute', 'changed')
    elif kind == 'style':
        style = root.find('.//STYLE')
        style.set('Name', style.get('Name', '') + '-SYNTHETIC-change')
    elif kind == 'control':
        ET.SubElement(paras[2], 'FOOTNOTE', {'Synthetic': 'inserted'})
    elif kind == 'media':
        storage = ET.SubElement(root, 'BINDATASTORAGE')
        ET.SubElement(storage, 'BINDATA', {'Id': '1'}).text = 'SYNTHETIC changed media payload'
    elif kind == 'target-format':
        shape = root.find('.//PARASHAPE[@Id="20"]')
        shape.set('Align', 'Right')
    elif kind == 'wrong-target':
        paras[1].attrib.pop('InstId', None)
        paras[1].set('ParaShape', '0')
        paras[2].set('ParaShape', '20')
    else:
        raise CaptureSetupError('unknown counterexample')
    changed = ET.tostring(root, encoding='unicode')
    if changed == after:
        raise CaptureSetupError('counterexample did not alter the captured input')
    return changed


class CapturedHwp(FakeHwp):
    """Recording action model + exact captured XML, with abstract SYNTHETIC UNICODE."""
    def __init__(self, *, after=None, changed_export=True):
        self.before_xml = captured('raw-1.hwpml')
        self.after_xml = captured('raw-2.hwpml') if after is None else after
        before = parse_hwpml2x_census(self.before_xml)
        super().__init__(paragraphs=[
            {'text': p['text'], 'heading_type': 0, 'level': 0, 'section': p['section']}
            for p in before['paragraphs']
        ], controls=[FakeCtrl('secd')])
        self.effect = False
        self.changed_export = changed_export
        self.exports = []
        execute = self.HAction.Execute

        def recording_execute(name, hset):
            returned = execute(name, hset)
            if name == 'ParagraphShape':
                self.effect = True
            return returned

        self.HAction.Execute = recording_execute
        self.cursor = (0, 1, 0)

    def get_text_file(self, fmt='UNICODE', option=''):
        self.calls.append(('get_text_file', fmt, option))
        self.exports.append(fmt)
        if fmt == 'HWPML2X':
            return self.after_xml if self.effect else self.before_xml
        if fmt != 'UNICODE':
            raise CaptureSetupError('unexpected export format')
        return SYNTHETIC_UNICODE_AFTER if self.effect and self.changed_export else SYNTHETIC_UNICODE_BEFORE

    @property
    def target(self):
        return self.paragraphs[1]['text']


def apply(hwp, *, protection=True):
    return nn.apply_official_numbering_at_match(
        hwp, find=hwp.target, level=1,
        protection=live_protection_reader(hwp) if protection else None,
    )


def executes(hwp):
    return [c[:2] for c in hwp.calls if c[0] == 'Execute']


class CapturedGuardTests(unittest.TestCase):
    def test_actual_pair_passes_real_complete_target_specific_comparison(self):
        before, after = [nn._validated_census(c) for c in census_pair()]
        self.assertEqual(nn._incomplete(before), [])
        self.assertEqual(nn._incomplete(after), [])
        self.assertEqual([i for i, (a, b) in enumerate(zip(before['paragraphs'], after['paragraphs'])) if a != b], [1])
        self.assertEqual(nn._official_delta_problems(before, after, target_index=1, native_level=0,
                                                   define=False, target_written=True), [])
        self.assertEqual(before['body_sha256'], after['body_sha256'])
        self.assertEqual(before['paragraphs'][1]['text_sha256'], after['paragraphs'][1]['text_sha256'])

    def test_public_match_accepts_synthetic_export_delta_only_with_captured_preservation(self):
        hwp = CapturedHwp()
        result = apply(hwp)
        self.assertTrue(result['changed'])
        self.assertEqual(result['status'], 'applied')
        self.assertTrue(result['protection']['checked'])
        self.assertEqual(result['protection']['unproven'], [])
        self.assertEqual(executes(hwp), [('Execute', 'ParagraphShape')])
        self.assertEqual(hwp.exports, ['UNICODE', 'HWPML2X', 'UNICODE', 'HWPML2X'])

    def test_matching_unicode_positive_has_identical_native_call_sequence(self):
        unchanged, changed = CapturedHwp(changed_export=False), CapturedHwp()
        apply(unchanged)
        apply(changed)
        self.assertEqual(unchanged.calls, changed.calls)
        self.assertEqual(unchanged.exports, changed.exports)

    def test_captured_structure_corruptions_remain_unknown_with_either_export(self):
        for kind in ('body-text-loss', 'literal-typed-prefix', 'unrelated-paragraph', 'style', 'control', 'target-format', 'wrong-target'):
            for changed_export in (True, False):
                with self.subTest(kind=kind, changed_export=changed_export):
                    hwp = CapturedHwp(after=altered(captured('raw-2.hwpml'), kind), changed_export=changed_export)
                    with self.assertRaises(nn.NativeNumberingMutationError) as error:
                        apply(hwp)
                    self.assertTrue(error.exception.mutation_may_have_persisted)
                    self.assertEqual(error.exception.rollback, {'attempted': False, 'succeeded': False})
                    self.assertEqual(executes(hwp), [('Execute', 'ParagraphShape')])

    def test_media_payload_change_cannot_authorize_a_changed_export(self):
        hwp = CapturedHwp(after=altered(captured('raw-2.hwpml'), 'media'))
        with self.assertRaises(nn.NativeNumberingMutationError):
            apply(hwp)

    def test_unchanged_synthetic_media_payload_is_positive_but_changed_payload_refuses(self):
        for change in (False, True):
            hwp = CapturedHwp()
            hwp.before_xml = altered(hwp.before_xml, 'media')
            hwp.after_xml = altered(hwp.after_xml, 'media')
            if change:
                hwp.after_xml = hwp.after_xml.replace('SYNTHETIC changed media payload', 'SYNTHETIC different payload')
                with self.assertRaises(nn.NativeNumberingMutationError):
                    apply(hwp)
            else:
                self.assertTrue(apply(hwp)['changed'])

    def test_non_string_unicode_is_not_coerced_even_with_valid_structure(self):
        for phase in ('before', 'after'):
            for bad in (None, True, 1, b'bytes'):
                hwp = CapturedHwp()
                reader = hwp.get_text_file
                def get_text_file(fmt='UNICODE', option=''):
                    value = reader(fmt, option)
                    return bad if fmt == 'UNICODE' and ((phase == 'after') == hwp.effect) else value
                hwp.get_text_file = get_text_file
                with self.subTest(phase=phase, bad=bad), self.assertRaises((nn.NativeNumberingRefusal, nn.NativeNumberingMutationError)):
                    apply(hwp)

    def test_no_protection_direct_helper_and_public_match_stay_strict(self):
        for public in (True, False):
            with self.subTest(public=public):
                hwp = CapturedHwp()
                with self.assertRaises(nn.NativeNumberingMutationError) as error:
                    if public:
                        apply(hwp, protection=False)
                    else:
                        nn.apply_official_numbering(hwp, {'level': 1}, protection=None)
                self.assertIn('text_hash_mismatch', error.exception.fixed_reason['judgements'])

    def test_missing_or_malformed_protection_cannot_replace_unicode_guard(self):
        for phase in ('before', 'after', 'both'):
            for field in ('body_sha256', 'styles_sha256', 'numberings_sha256', 'media_sha256',
                          'header_footer', 'frame', 'paragraphs', 'definitions', 'outline_shapes'):
                for bad in (None, '', 'not-a-sha256'):
                    # Composite fields use None for missing and a scalar for malformed input.
                    with self.subTest(phase=phase, field=field, bad=bad):
                        hwp = CapturedHwp()
                        live = live_protection_reader(hwp)
                        def protection():
                            c = live()
                            if phase == 'both' or (phase == 'after') == hwp.effect:
                                c[field] = bad
                            return c
                        with self.assertRaises((nn.NativeNumberingRefusal, nn.NativeNumberingMutationError)):
                            nn.apply_official_numbering_at_match(hwp, find=hwp.target, level=1, protection=protection)
                        if hwp.effect:
                            self.assertEqual(executes(hwp), [('Execute', 'ParagraphShape')])
                        else:
                            self.assertEqual(executes(hwp), [])

    def test_malformed_paragraph_and_definition_digests_are_not_complete_proof(self):
        for field in ('sha256', 'masked_sha256', 'text_sha256', 'stripped_sha256'):
            hwp = CapturedHwp()
            live = live_protection_reader(hwp)
            def protection():
                c = live()
                for p in c['paragraphs']:
                    p[field] = ''
                return c
            with self.subTest(field=field), self.assertRaises((nn.NativeNumberingRefusal, nn.NativeNumberingMutationError)):
                nn.apply_official_numbering_at_match(hwp, find=hwp.target, level=1, protection=protection)
        hwp = CapturedHwp()
        live = live_protection_reader(hwp)
        def invalid_definition():
            c = live()
            c['definitions']['charshapelist'][0]['sha'] = ''
            return c
        with self.assertRaises(nn.NativeNumberingMutationError):
            nn.apply_official_numbering_at_match(hwp, find=hwp.target, level=1, protection=invalid_definition)

    def test_malformed_xml_after_action_and_ambiguous_action_remain_unknown(self):
        for after, status in (('<HWPML><BODY>', True), (None, None), (None, 1), (None, 'True')):
            with self.subTest(after=after, status=status):
                hwp = CapturedHwp(after=after)
                hwp.HAction.execute_result = status
                with self.assertRaises(nn.NativeNumberingMutationError):
                    apply(hwp)
                self.assertEqual(executes(hwp), [('Execute', 'ParagraphShape')])

    def test_ambiguous_target_stays_pre_mutation_refusal(self):
        hwp = CapturedHwp()
        hwp.paragraphs[2]['text'] = hwp.target
        with self.assertRaises(nn.NativeNumberingRefusal) as error:
            apply(hwp)
        self.assertEqual(error.exception.code, 'target_ambiguous')
        self.assertFalse(hwp.effect)

    def test_real_bundle_accepts_captured_preservation_and_persists_dirty(self):
        hwp = CapturedHwp()
        step = dict(NUMBERING_STEP, match=hwp.target, level=1)
        result, persisted = _run_bundle(hwp, step)
        self.assertTrue(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(persisted['dirty'])
        self.assertEqual(executes(hwp), [('Execute', 'ParagraphShape')])


class CapturedRuntimeTests(CustodyAssertions, unittest.TestCase):
    def assert_held_result(self, owned, result, error):
        # real_execute_live persists custody via _save_binding, not the helper's
        # simplified _update_live_binding seam used by assert_noncomplete_result.
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(result['native_custody_held'])
        self.assertTrue(any(b.get('working_copy_dirty') is True and b.get('native_custody_held')
                            for b in owned.saved_bindings))

    def test_captured_positive_through_real_bundle_and_owning_runtime(self):
        hwp = CapturedHwp()
        owned = Owned(self, hwp, real_execute_live=True)
        fault = owned.arm('ParagraphShape')
        result, error = owned.run(dict(NUMBERING_STEP, match=hwp.target, level=1))
        self.assertIsNone(error)
        self.assertTrue(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(owned.session._thread.is_alive())
        self.assertFalse(owned.session._quarantined)
        self.assertEqual(owned.cleanup_events(), [])
        self.assertEqual(fault.attempted, ['ParagraphShape'])

    def test_captured_text_loss_holds_same_sta_and_blocks_cleanup_and_follow_ons(self):
        hwp = CapturedHwp(after=altered(captured('raw-2.hwpml'), 'body-text-loss'))
        owned = Owned(self, hwp, real_execute_live=True)
        fault = owned.arm('ParagraphShape')
        result, error = owned.run(dict(NUMBERING_STEP, match=hwp.target, level=1))
        self.assert_held_result(owned, result, error)
        self.assert_retained(owned)
        self.assert_no_follow_on(owned, fault)
        self.assert_custody_survives_follow_ons(owned)

    def test_ambiguous_execute_is_not_cleared_by_structural_preservation(self):
        hwp = CapturedHwp()
        owned = Owned(self, hwp, real_execute_live=True)
        fault = owned.arm('ParagraphShape', ret=None)
        result, error = owned.run(dict(NUMBERING_STEP, match=hwp.target, level=1))
        self.assert_held_result(owned, result, error)
        self.assert_retained(owned)
        self.assert_no_follow_on(owned, fault)
