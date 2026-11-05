"""Request-bound derived line reflow for a changed picture layout, and C601-REPORT-1.

Part A runs the product proof functions on the ACTUAL h01 before/after private copies
with their actual native write windows, the exact request and the recorded typed
prestate; nothing is rewritten except the test-local adversaries, which are named.
Part B replays the same native data through the real owning STA, service, save and
history (tests/native_h01_replay.py labels its SYNTHETIC parts). Part C uses the
SYNTHETIC line-cache SDK model (tests/line_cache_fakes.py) for multi-paragraph and
multi-section flows. Only the external SDK/OS is faked. None of this is native
evidence of a saved picture edit.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import hwpx_saved_proof as proof
from app import native_picture as picture
from app import picture_request_proof
from app.local_cli_service import LocalCliUndoRefused
from app.local_cli_service_support import LocalCliServiceError
from tests import hwpx_package_fakes as fakes
from tests import line_cache_fakes as lines
from tests import native_h01_replay as replay
from tests.test_picture_saved_package import SavedOwner, events

FIVE = ['baseline', 'horzpos', 'horzsize', 'textheight', 'vertsize']
BEFORE, AFTER = 'before.package.json', 'after.package.json'


def load(path: Path) -> proof.Profile:
    data = path.read_bytes()
    return proof.Profile(proof.validate_profile(json.loads(data)), hashlib.sha256(data).hexdigest(), 'operator_configured_file')


def env_for(directory: Path, data: bytes, name: str = 'profile.json') -> dict[str, str]:
    path = Path(directory) / name
    path.write_bytes(data)
    return {'HWP_PICTURE_HWPX_PROFILE': str(path), 'HWP_PICTURE_HWPX_PROFILE_SHA256': hashlib.sha256(data).hexdigest()}


def reflow_profile(**changes) -> dict:
    """The synthetic portable profile plus a SYNTHETIC line_reflow declaration (not native evidence)."""
    data = copy.deepcopy(fakes.PROFILE)
    data['line_reflow'] = {'picture_layout_exact': {'scope': 'target_paragraph_lines', 'fields': list(FIVE)}}
    data['evidence']['line_reflow'] = {'status': 'not_run', 'basis': 'SYNTHETIC portable test fixture'}
    data.update(changes)
    return data


def lineseg(section: bytes) -> list[dict[str, str]]:
    root = ET.fromstring(section)
    return [dict(e.attrib) for e in root.iter() if e.tag.endswith('}lineseg')]


def member(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


# --------------------------------------------------------------------------- Part A: actual pair


class ActualH01Pair(unittest.TestCase):
    """The actual native copies, actual windows, exact request and typed prestate; real proof functions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before, self.after = replay.state(BEFORE), replay.state(AFTER)
        self.model = picture.parse_pictures(replay.producer_string('parse-002.hwpml2x'))
        self.target = self.model['pictures'][0]
        self.request = replay.request()
        self.link = proof.bridge(self.model, self.target, self.before, self.request)
        self.flow = picture_request_proof.flow_prestate(self.target, self.request, replay.H01['typed']['TextFlow_before'])
        self.ui = picture_request_proof.plan(self.model, self.request, tuple(replay.H01['ui_plan']['original']), tuple(replay.H01['anchor']))
        self.settings = {'TreatAsChar': 0, 'TextWrap': 0}

    def expectation(self, profile, after=None, *, before=None):
        before = before or self.before
        scope = proof.reflow_scope(before, self.link, self.request, profile)
        return proof.expect(before, after or self.after, self.link, self.request, settings=self.settings, expected_crop=None,
                            flow_prestate=self.flow, ui_plan=self.ui, profile=profile, source=None, reflow=scope)

    def damaged(self, name, edit) -> proof.PackageState:
        path = Path(tempfile.mkdtemp(dir=self.tmp.name)) / (name.replace('.package.json', '') + '-adversary.hwpx')
        replay.write(path, edit(replay.members(name)))
        return proof.read_state(path, window=replay.window(name))

    def test_exact_request_and_typed_prestate_reproduce_the_native_ledger(self):
        self.assertEqual(self.ui.request_sha256, replay.H01['request_sha256'])
        self.assertEqual((self.flow.observed, self.flow.may_materialize), (0, True))
        self.assertEqual((self.link.ordinal, self.link.count, self.link.section, self.link.path), (0, 1, 'Contents/section0.xml', (0, 1, 0)))
        self.assertEqual(self.before.sha256, replay.MANIFEST['files'][BEFORE]['decodes_to_sha256'])
        self.assertEqual(self.after.sha256, replay.MANIFEST['files'][AFTER]['decodes_to_sha256'])

    def test_without_a_reflow_declaration_the_actual_pair_still_stops_at_the_line_cache(self):
        # The current comparator outcome on the actual pair, retained as the strict default.
        profile = load(replay.G01_PROFILE)
        expectation = proof.expect(self.before, self.after, self.link, self.request, settings=self.settings, expected_crop=None,
                                   flow_prestate=self.flow, ui_plan=self.ui, profile=profile, source=None)
        with self.assertRaises(proof.PackageMismatch) as held:
            proof.compare(self.before, self.after, expectation, profile)
        self.assertEqual(held.exception.deltas, [{'kind': 'xml_delta', 'member': 'Contents/section0.xml',
                                                  'at': '/sec[0]/p[2]/linesegarray[0]/lineseg:@baseline,horzpos,horzsize,textheight,vertsize'}])

    def test_declared_reflow_admits_exactly_the_actual_target_paragraph_line_cache(self):
        profile = load(replay.TEST_PROFILE)
        expectation = self.expectation(profile)
        counts = proof.compare(self.before, self.after, expectation, profile)
        self.assertEqual((counts['byte_identical'], counts['infoset_compared'], counts['regenerated_not_compared']), (9, 3, 0))
        self.assertEqual(expectation.reflow, {
            'contract': 'request_bound_line_reflow/v1', 'trigger': 'picture_layout_exact', 'section_member': 'Contents/section0.xml',
            'paragraph_child_index': 0, 'paragraph_id': '0', 'lines': 1, 'fields': FIVE, 'changed_lines': 1, 'changed_fields': FIVE,
            'text_width': 42520})
        used = proof.edit_evidence('picture_layout_exact', profile, counts, reflow=expectation.reflow)
        self.assertIn('line_reflow', used)
        self.assertFalse(profile.native_qualified(used), 'the profile is unqualified')

    def test_actual_after_cache_is_bound_exactly_by_save_fresh_copy_and_history(self):
        profile = load(replay.TEST_PROFILE)
        drift = self.damaged(AFTER, replay.section_edit(b'baseline="850"', b'baseline="851"'))
        # A saved file equal to the after copy: 11 members byte-identical, content.hpf typed (as the actual g01 save).
        saved = proof.prove_saved(self.after, self.after, profile)
        self.assertEqual((saved['byte_identical'], saved['infoset_compared']), (11, 1))
        with self.assertRaises(proof.PackageMismatch):
            proof.prove_saved(self.after, drift, profile)
        self.assertNotEqual(proof.state_digest(self.after, profile), proof.state_digest(drift, profile))
        self.assertNotEqual(proof.state_digest(self.before, profile), proof.state_digest(self.after, profile))

    def test_line_cache_adversaries_on_the_actual_after_copy(self):
        profile = load(replay.TEST_PROFILE)
        edit = replay.section_edit
        cases = {
            'textpos_anchor': edit(b'textpos="0"', b'textpos="1"'),
            'vertpos_anchor': edit(b'vertpos="0"', b'vertpos="1"'),
            'spacing_anchor': edit(b'spacing="600"', b'spacing="601"'),
            'flags_anchor': edit(b'flags="393216"', b'flags="393217"'),
            'added_line': edit(b'"/></hp:linesegarray>', b'"/><hp:lineseg textpos="0" vertpos="1600" vertsize="1000" textheight="1000" '
                               b'baseline="850" spacing="600" horzpos="9600" horzsize="32920" flags="393216"/></hp:linesegarray>'),
            'removed_line': lambda e: [(i, re.sub(rb'<hp:linesegarray>.*</hp:linesegarray>', b'<hp:linesegarray></hp:linesegarray>', d)
                                        if i.filename == 'Contents/section0.xml' else d) for i, d in e],
            'non_integer': edit(b'baseline="850"', b'baseline="abc"'),
            'plus_sign': edit(b'baseline="850"', b'baseline="+850"'),
            'leading_zero': edit(b'baseline="850"', b'baseline="0850"'),
            'negative': edit(b'horzpos="9600"', b'horzpos="-1"'),
            'out_of_int32': edit(b'horzsize="32920"', b'horzsize="4294967296"'),
            'missing_attribute': edit(b' horzsize="32920"', b''),
            'added_attribute': edit(b'flags="393216"', b'flags="393216" extra="1"'),
            'namespaced_attribute': edit(b'baseline="850"', b'hp:baseline="850"'),
            'case_alias': edit(b'baseline="850"', b'Baseline="850"'),
            'baseline_below_line': edit(b'baseline="850"', b'baseline="1001"'),
            'text_taller_than_line': edit(b'textheight="1000"', b'textheight="1001"'),
            'beyond_text_width': edit(b'horzsize="32920"', b'horzsize="32921"'),
            'taller_than_paper': lambda e: edit(b'textheight="1000"', b'textheight="90000"')(edit(b'vertsize="1000"', b'vertsize="90000"')(e)),
            'array_attribute': edit(b'<hp:linesegarray>', b'<hp:linesegarray x="1">'),
            'comment_in_array': edit(b'<hp:linesegarray>', b'<hp:linesegarray><!--c-->'),
            'foreign_child': edit(b'"/></hp:linesegarray>', b'"/><hp:foreign/></hp:linesegarray>'),
            'text_in_line': edit(b'flags="393216"/>', b'flags="393216">x</hp:lineseg>'),
            'second_array': edit(b'</hp:linesegarray>', b'</hp:linesegarray><hp:linesegarray/>'),
            # Unrequested non-line changes next to the admitted reflow.
            'paragraph_property': edit(b'paraPrIDRef="0"', b'paraPrIDRef="1"'),
            'run_format': edit(b'<hp:run charPrIDRef="0"><hp:pic', b'<hp:run charPrIDRef="1"><hp:pic'),
            'text_added': edit(b'<hp:t/>', b'<hp:t>x</hp:t>'),
            'rotation': edit(b'angle="0"', b'angle="90"'),
            'alpha': edit(b'alpha="0"', b'alpha="1"'),
            'opaque_control': edit(b'<hp:t/>', b'<hp:t/><hp:ctrl><hp:bookmark name="x"/></hp:ctrl>'),
            'section_property': edit(b'tabStop="8000"', b'tabStop="8001"'),
            'header_font': lambda e: [(i, d.replace(b'face="', b'face="X', 1) if i.filename == 'Contents/header.xml' else d) for i, d in e],
            'media_bytes': lambda e: [(i, d + b'\x00' if i.filename == 'BinData/image1.PNG' else d) for i, d in e],
        }
        anchors = {'textpos_anchor': 'textpos', 'vertpos_anchor': 'vertpos', 'spacing_anchor': 'spacing', 'flags_anchor': 'flags'}
        content = {'paragraph_property', 'run_format', 'text_added', 'rotation', 'alpha', 'opaque_control', 'section_property'}
        # Control: a rewrite of the actual after copy with no damage compares clean, so every
        # refusal below comes from the named adversary, not from the rewrite itself.
        control = self.damaged(AFTER, lambda entries: entries)
        self.assertEqual(control.container, self.after.container)
        proof.compare(self.before, control, self.expectation(profile, control), profile)
        for name, damage in cases.items():
            with self.subTest(adversary=name):
                after = self.damaged(AFTER, damage)
                self.assertEqual(after.container, self.after.container, 'only the named member bytes differ')
                with self.assertRaises(proof.PackageMismatch) as held:
                    proof.compare(self.before, after, self.expectation(profile, after), profile)
                kinds = [(d['kind'], d.get('reason'), d.get('member')) for d in held.exception.deltas]
                if name in anchors:
                    self.assertEqual(held.exception.deltas[0]['field'], anchors[name])
                    self.assertEqual(kinds, [('line_reflow', 'anchor', 'Contents/section0.xml')])
                elif name == 'added_line':
                    self.assertEqual(kinds, [('line_reflow', 'cardinality', 'Contents/section0.xml')])
                elif name in content:
                    self.assertEqual(kinds, [('xml_delta', None, 'Contents/section0.xml')], held.exception.deltas)
                elif name == 'header_font':
                    self.assertEqual(kinds, [('bytes', None, 'Contents/header.xml')])
                elif name == 'media_bytes':
                    self.assertEqual(kinds, [('bytes', None, 'BinData/image1.PNG')])
                else:
                    self.assertEqual(kinds, [('line_reflow', 'malformed', 'Contents/section0.xml')], held.exception.deltas)

    def test_scope_is_bound_before_effect_and_refuses_unproved_before_state(self):
        profile = load(replay.TEST_PROFILE)
        scope = proof.reflow_scope(self.before, self.link, self.request, profile)
        self.assertEqual(scope.public(), {'contract': 'request_bound_line_reflow/v1', 'trigger': 'picture_layout_exact',
                                          'section_member': 'Contents/section0.xml', 'paragraph_child_index': 0, 'paragraph_id': '0',
                                          'lines': 1, 'fields': FIVE, 'text_width': 42520})
        self.assertIsNone(proof.reflow_scope(self.before, self.link, self.request, load(replay.G01_PROFILE)), 'undeclared: strict')
        self.assertIsNone(proof.reflow_scope(self.before, self.link, {**self.request, 'op': 'picture_crop_exact'}, profile),
                          'another operation has no reflow allowance')
        edit = replay.section_edit
        cases = {
            'missing_attribute': edit(b' horzsize="42520"', b''),
            'non_integer': edit(b'baseline="8160"', b'baseline="x"'),
            'negative': edit(b'horzpos="0"', b'horzpos="-1"'),
            'extra_attribute': edit(b'flags="393216"', b'flags="393216" extra="1"'),
            'no_array': lambda e: [(i, re.sub(rb'<hp:linesegarray>.*</hp:linesegarray>', b'', d) if i.filename == 'Contents/section0.xml' else d) for i, d in e],
            'two_arrays': edit(b'</hp:linesegarray>', b'</hp:linesegarray><hp:linesegarray/>'),
            'foreign_child': edit(b'"/></hp:linesegarray>', b'"/><hp:foreign/></hp:linesegarray>'),
            'baseline_below_line': edit(b'baseline="8160"', b'baseline="9601"'),
            'beyond_text_width': edit(b'horzsize="42520"', b'horzsize="42521"'),
            'landscape_unobserved': edit(b'landscape="WIDELY"', b'landscape="NARROWLY"'),
            'columns_unobserved': edit(b'colCount="1"', b'colCount="2"'),
            'gutter_unobserved': edit(b'gutter="0"', b'gutter="100"'),
        }
        for name, damage in cases.items():
            with self.subTest(before_state=name):
                before = self.damaged(BEFORE, damage)
                link = proof.bridge(self.model, self.target, before, self.request)
                with self.assertRaises(proof.ProofUnavailable):
                    proof.reflow_scope(before, link, self.request, profile)

    def test_g01_completing_save_method_package_proof_still_holds_on_current_source(self):
        # The actual g01 Save(False) output equals its private copy byte for byte; the current proof re-proves it.
        g01 = replay.MANIFEST['g01']
        path = Path(tempfile.mkdtemp(dir=self.tmp.name)) / 'g01.hwpx'
        path.write_bytes(replay.package_bytes('g01-saved.package.json'))
        from datetime import datetime
        window = tuple(datetime.fromisoformat(t) for t in g01['private_copy_window_utc'])
        private = proof.read_state(path, window=window)
        saved = proof.read_state(path, window=tuple(datetime.fromisoformat(t) for t in g01['save_window_utc']))
        result = proof.prove_saved(private, saved, load(replay.G01_PROFILE))
        self.assertEqual({k: result[k] for k in g01['saved_proof']}, g01['saved_proof'])


class ProfileContract(unittest.TestCase):
    def test_line_reflow_declaration_has_fixed_typed_semantics(self):
        good = reflow_profile()
        self.assertEqual(proof.validate_profile(copy.deepcopy(good))['line_reflow'], good['line_reflow'])
        self.assertNotIn('line_reflow', proof.validate_profile(copy.deepcopy(fakes.PROFILE)), 'the key stays optional')
        subset = reflow_profile()
        subset['line_reflow']['picture_layout_exact']['fields'] = ['horzpos', 'horzsize']
        self.assertEqual(proof.validate_profile(subset)['line_reflow']['picture_layout_exact']['fields'], ['horzpos', 'horzsize'])

        def rule(**fields):
            data = reflow_profile()
            data['line_reflow']['picture_layout_exact'].update(fields)
            return data
        bad = {
            'other_operation': lambda: {**reflow_profile(), 'line_reflow': {'picture_crop_exact': {'scope': 'target_paragraph_lines', 'fields': FIVE}}},
            'empty_mapping': lambda: {**reflow_profile(), 'line_reflow': {}},
            'other_scope': lambda: rule(scope='section'),
            'anchor_textpos': lambda: rule(fields=FIVE + ['textpos']),
            'anchor_vertpos': lambda: rule(fields=['vertpos']),
            'anchor_spacing': lambda: rule(fields=['spacing']),
            'anchor_flags': lambda: rule(fields=['flags']),
            'unknown_field': lambda: rule(fields=['height']),
            'duplicate_field': lambda: rule(fields=['baseline', 'baseline']),
            'empty_fields': lambda: rule(fields=[]),
            'fields_not_list': lambda: rule(fields='baseline'),
            'extra_rule_key': lambda: rule(selector='p[2]'),
        }
        for name, build in bad.items():
            with self.subTest(case=name):
                with self.assertRaises(proof.ProofUnavailable):
                    proof.validate_profile(build())
        missing_evidence = reflow_profile()
        del missing_evidence['evidence']['line_reflow']
        stray_evidence = copy.deepcopy(fakes.PROFILE)
        stray_evidence['evidence']['line_reflow'] = {'status': 'not_run', 'basis': 'x'}
        for name, data in (('missing_evidence', missing_evidence), ('evidence_without_declaration', stray_evidence)):
            with self.subTest(case=name):
                with self.assertRaises(proof.ProofUnavailable):
                    proof.validate_profile(data)

    def test_public_projection_keeps_only_typed_reflow_facts(self):
        from app import picture_public
        raw = {'strategy': 'native_hwpx_package', 'evidence_used': ['materialization', 'line_reflow'],
               'derived_line_reflow': {'contract': 'request_bound_line_reflow/v1', 'trigger': 'picture_layout_exact',
                                       'section_member': 'Contents/section0.xml', 'paragraph_child_index': 0, 'paragraph_id': '0',
                                       'lines': 1, 'changed_lines': 1, 'changed_fields': FIVE, 'fields': FIVE, 'text_width': 42520,
                                       'path': '/private/x', 'raw': '<hp:lineseg/>'}}
        out = picture_public.saved_proof(raw)
        self.assertEqual(out['evidence_used'], ['materialization', 'line_reflow'])
        self.assertEqual(out['derived_line_reflow'], {'contract': 'request_bound_line_reflow/v1', 'trigger': 'picture_layout_exact',
                                                      'section_member': 'Contents/section0.xml', 'paragraph_child_index': 0,
                                                      'paragraph_id': '0', 'lines': 1, 'changed_lines': 1, 'changed_fields': FIVE})
        hostile = copy.deepcopy(raw)
        hostile['derived_line_reflow'].update(section_member='../x', paragraph_child_index=-1, paragraph_id='p2', lines='1',
                                              changed_fields=['textpos'])
        self.assertEqual(picture_public.saved_proof(hostile)['derived_line_reflow'],
                         {'contract': 'request_bound_line_reflow/v1', 'trigger': 'picture_layout_exact', 'changed_lines': 1})
        for contract, trigger in (('other/v1', 'picture_layout_exact'), ('request_bound_line_reflow/v1', 'picture_crop_exact')):
            other = copy.deepcopy(raw)
            other['derived_line_reflow'].update(contract=contract, trigger=trigger)
            self.assertNotIn('derived_line_reflow', picture_public.saved_proof(other))

    def test_shipped_and_test_profiles(self):
        shipped = load(Path(proof.__file__).parent.parent / 'docs' / 'profiles' / 'hwpx-saved-proof-e01-candidate.json')
        self.assertNotIn('line_reflow', shipped.data, 'the measurement-only candidate is unchanged')
        test = load(replay.TEST_PROFILE)
        self.assertEqual(test.data['qualification']['status'], 'unqualified')
        self.assertIs(test.data['public_save']['save_false_after_export_clear'], True)
        g01 = json.loads(replay.G01_PROFILE.read_bytes())
        self.assertEqual(test.data['evidence']['public_save'], g01['evidence']['public_save'], 'the g01 observation is reused verbatim')
        for name in proof.EVIDENCE_FIELDS:
            if name != 'public_save':
                self.assertEqual(test.data['evidence'][name]['status'], g01['evidence'][name]['status'], name)
        self.assertEqual(test.data['line_reflow'], {'picture_layout_exact': {'scope': 'target_paragraph_lines', 'fields': FIVE}})


# --------------------------------------------------------------------------- Part B: actual data, real caller


class ActualH01ThroughRealCaller(unittest.TestCase):
    """Real runtime/service/save/history over the actual h01 replay; its Save(False) output is SYNTHETIC."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = env_for(Path(self.tmp.name), replay.TEST_PROFILE.read_bytes(), 'test-profile.json')

    def owner(self, hwp=None, env=None):
        hwp = hwp or replay.NativeH01ReplayHwp()
        return SavedOwner(self, hwp, env=env or self.env, working=lambda path: replay.write_input(path))

    def edit(self, o):
        inventory = o.inventory()
        self.assertEqual([(p['target_id'], p['hash']) for p in inventory],
                         [(replay.H01['validated_request']['target_id'], replay.H01['validated_request']['expected_hash'])])
        return o.bundle(o.request('picture_layout_exact', mode='square'))

    def test_test_profile_square_saves_the_exact_actual_line_cache(self):
        o = self.owner()
        out = self.edit(o)
        self.assertTrue(out['ok'], out)
        result = out['steps'][0]['result']
        self.assertEqual(result['ui_position']['request_sha256'], replay.H01['request_sha256'], 'the native request, exactly')
        self.assertEqual(len([e for e in o.hwp.events if str(e).startswith('Execute:')]), 1)
        saved = result['saved_proof']
        self.assertEqual(saved['derived_line_reflow'], {
            'contract': 'request_bound_line_reflow/v1', 'trigger': 'picture_layout_exact', 'section_member': 'Contents/section0.xml',
            'paragraph_child_index': 0, 'paragraph_id': '0', 'lines': 1, 'changed_lines': 1, 'changed_fields': FIVE})
        self.assertIn('line_reflow', saved['evidence_used'])
        self.assertFalse(saved['native_qualified'])
        result = o.service.save(session_id=o.sid)
        self.assertEqual((result['save_decision'], result['saved_proof']['baseline']), ('verified_save_false', 'verified_edit'))
        self.assertEqual(events(o.hwp, 'Save'), [('Save', False)])
        self.assertEqual(lineseg(member(o.wc, 'Contents/section0.xml')), lineseg(member(replay.package_path(AFTER), 'Contents/section0.xml')))
        external = o.external / 'saved.hwpx'
        shutil.copyfile(o.wc, external)
        self.assertTrue(proof.verify_external(external, result['saved_proof'])['verified'])
        self.assertFalse(o.session._normal_close_requires_save)

    def test_no_op_layout_issues_no_copy_scope_or_native_action(self):
        o = self.owner()
        out = o.bundle(o.request('picture_layout_exact', mode='inline'))  # the actual picture is already inline
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['steps'][0]['result']['reason'], 'already_requested_state')
        self.assertEqual(events(o.hwp, 'SaveAs'), [])
        self.assertFalse(o.hwp.executed)
        self.assertNotIn('saved_proof', out['steps'][0]['result'])

    def test_without_a_declaration_the_actual_replay_is_held_after_execute_as_natively(self):
        o = self.owner(env=env_for(Path(self.tmp.name), replay.G01_PROFILE.read_bytes(), 'g01.json'))
        out, error = o.call(*[o.request('picture_layout_exact', mode='square')])
        self.assertTrue(o.hwp.executed)
        self.assertFalse(out is not None and out.get('ok') is True, out)
        self.assertTrue(o.held())
        self.assertEqual(len(events(o.hwp, 'SaveAs')), 2)
        self.assertEqual(events(o.hwp, 'Save'), [])

    def test_unproved_before_state_refuses_before_any_selection(self):
        hwp = replay.NativeH01ReplayHwp()
        original = replay.members
        damage = replay.section_edit(b'colCount="1"', b'colCount="2"')
        with patch.object(replay, 'members', lambda name: damage(original(name)) if name == BEFORE else original(name)):
            o = self.owner(hwp)
            out = self.edit(o)
        self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_proof_unqualified')
        self.assertFalse(any(str(e).startswith(('Run:SelectCtrlFront', 'Execute')) for e in hwp.events))
        self.assertFalse(o.held())

    def test_after_copy_adversaries_hold_without_followons(self):
        edit = replay.section_edit
        cases = {'unrequested_text': edit(b'<hp:t/>', b'<hp:t>x</hp:t>'), 'textpos': edit(b'textpos="0"', b'textpos="1"'),
                 'beyond_width': edit(b'horzsize="32920"', b'horzsize="32921"'), 'extra_line': edit(
                     b'"/></hp:linesegarray>', b'"/><hp:lineseg textpos="0" vertpos="1600" vertsize="1000" textheight="1000" baseline="850" '
                     b'spacing="600" horzpos="9600" horzsize="32920" flags="393216"/></hp:linesegarray>')}
        for name, damage in cases.items():
            with self.subTest(adversary=name):
                hwp = replay.NativeH01ReplayHwp()
                hwp.after_entries_damage = damage
                o = self.owner(hwp)
                out, error = o.call(*[o.request('picture_layout_exact', mode='square')])
                self.assertTrue(hwp.executed)
                self.assertFalse(out is not None and out.get('ok') is True, (name, out))
                self.assertTrue(o.held(), name)
                self.assertEqual(events(hwp, 'Save'), [])

    def test_save_readback_cache_drift_holds_with_the_debt(self):
        hwp = replay.NativeH01ReplayHwp()
        hwp.saved_entries_damage = replay.section_edit(b'baseline="850"', b'baseline="851"')
        o = self.owner(hwp)
        self.assertTrue(self.edit(o)['ok'])
        with self.assertRaises(LocalCliServiceError) as held:
            o.service.save(session_id=o.sid)
        self.assertIn('native_custody_held', held.exception.message)
        self.assertTrue(o.session._normal_close_requires_save)

    def test_fresh_save_copy_binds_the_exact_cache(self):
        o = self.owner()
        self.assertTrue(self.edit(o)['ok'])
        o.service.cursor_move(direction='right', count=1, session_id=o.sid)
        out = o.service.save(session_id=o.sid)
        self.assertEqual((out['save_decision'], out['saved_proof']['baseline']), ('verified_save_false', 'save_fresh_copy'))
        hwp = replay.NativeH01ReplayHwp()
        hwp.later_copy_damage = replay.section_edit(b'horzpos="9600"', b'horzpos="9601"')
        o = self.owner(hwp)
        self.assertTrue(self.edit(o)['ok'])
        o.service.cursor_move(direction='right', count=1, session_id=o.sid)
        out = o.service.save(session_id=o.sid)
        self.assertEqual((out['save_decision'], out['debt_reason']), ('clean_noop', 'hwpx_fresh_copy_unbound'))
        self.assertEqual(events(hwp, 'Save'), [])
        self.assertTrue(o.session._normal_close_requires_save)

    def test_history_compares_the_exact_cache_both_ways(self):
        # SYNTHETIC history declaration on a test-local copy; no native picture history was observed.
        data = json.loads(replay.TEST_PROFILE.read_bytes())
        data['profile_id'] = 'hancom-13.0.0.3457-h01-reflow-test-history-synthetic/v1'
        data['history'] = {'picture_layout_exact': {'undo_native_steps': 1, 'redo_native_steps': 1}}
        env = env_for(Path(self.tmp.name), json.dumps(data, sort_keys=True).encode(), 'history.json')
        o = self.owner(env=env)
        self.assertTrue(self.edit(o)['ok'])
        undone = o.service.undo(session_id=o.sid)
        self.assertTrue(undone['ok'], undone)
        self.assertTrue(undone['confirmation']['expected_state']['matches'])
        hwp = replay.NativeH01ReplayHwp()
        hwp.history_copy_damage = replay.section_edit(b'baseline="8160"', b'baseline="8161"')
        o = self.owner(hwp, env=env)
        self.assertTrue(self.edit(o)['ok'])
        undone = o.service.undo(session_id=o.sid)
        self.assertFalse(undone['ok'])
        self.assertFalse(undone['confirmation']['expected_state']['matches'])
        with self.assertRaises(LocalCliUndoRefused):
            o.service.redo(session_id=o.sid)


# --------------------------------------------------------------------------- Part C: synthetic line cache


def variant(kind: str) -> ET.Element:
    """SYNTHETIC HWPML start states derived from the existing synthetic fixture."""
    root = ET.fromstring(Path(fakes.sdk.FIXTURE).read_text(encoding='utf-8'))
    body = next(e for e in root if e.tag == 'BODY')
    section = next(e for e in body if e.tag == 'SECTION')
    if kind in ('last_paragraph', 'floating_last', 'floating_top_bottom_last', 'second_section'):
        for paragraph in list(section)[4:]:
            section.remove(paragraph)
    if kind == 'floating_last':
        shape = next(e for e in root.iter() if e.tag == 'SHAPEOBJECT')
        shape.set('TextWrap', 'Square')
        next(e for e in shape if e.tag == 'POSITION').set('TreatAsChar', 'false')
    if kind == 'floating_top_bottom_last':
        shape = next(e for e in root.iter() if e.tag == 'SHAPEOBJECT')
        next(e for e in shape if e.tag == 'POSITION').set('TreatAsChar', 'false')
    if kind == 'second_section':
        second = copy.deepcopy(section)
        body.append(second)
        text = next(e for e in list(section)[3] if e.tag == 'TEXT')
        text.remove(next(e for e in text if e.tag == 'PICTURE'))
    return root


class SyntheticLineCacheThroughRealService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def owner(self, hwp, profile=None):
        env = fakes.profile_env(Path(tempfile.mkdtemp(dir=self.tmp.name)), profile if profile is not None else reflow_profile())
        return SavedOwner(self, hwp, env=env)

    def test_eligible_paragraph_positives(self):
        cases = {
            'last_paragraph_inline_to_square': (dict(hwpml=variant('last_paragraph')), dict(mode='square'), 'Contents/section0.xml', 4),
            'second_section_inline_to_square': (dict(hwpml=variant('second_section')), dict(mode='square'), 'Contents/section1.xml', 4),
            'floating_square_to_inline': (dict(hwpml=variant('floating_last')), dict(mode='inline'), 'Contents/section0.xml', 4),
            # SYNTHETIC: a floating TopAndBottom picture becomes Square; its two lines only narrow.
            'two_line_horizontal_only': (dict(hwpml=variant('floating_top_bottom_last'), lines_per_paragraph={(1, 0, 3): 2}),
                                         dict(mode='square'), 'Contents/section0.xml', 4),
        }
        for name, (kwargs, fields, member_name, index) in cases.items():
            with self.subTest(case=name):
                hwp = lines.LineCacheHwp(**kwargs)
                o = self.owner(hwp)
                out = o.bundle(o.request('picture_layout_exact', **fields))
                self.assertTrue(out['ok'], (name, out))
                reflow = out['steps'][0]['result']['saved_proof']['derived_line_reflow']
                self.assertEqual((reflow['section_member'], reflow['paragraph_child_index']), (member_name, index))
                self.assertGreaterEqual(reflow['changed_lines'], 1)
                if name == 'two_line_horizontal_only':
                    self.assertEqual((reflow['lines'], reflow['changed_lines'], reflow['changed_fields']), (2, 2, ['horzpos', 'horzsize']))
                saved = o.service.save(session_id=o.sid)
                self.assertEqual(saved['save_decision'], 'verified_save_false', name)
                self.assertNotIn('line_reflow', saved['saved_proof']['evidence_used'], 'a save never uses the request-time allowance')

    def test_reflow_outside_the_target_paragraph_is_refused(self):
        def change_other(paragraph_key):
            def damage(fake, key):
                fake.lines[paragraph_key][0]['horzsize'] -= 1
            return damage

        def unrelated_only(h):
            h.reflow_target = False
            h.reflow_damage = change_other((1, 0, 1))
        cases = {
            # The picture paragraph is not last: a native-like reflow moves every later paragraph.
            'following_paragraphs_move': (lambda: lines.LineCacheHwp(), lambda h: setattr(h, 'reflow_following', True)),
            'unrelated_paragraph_only': (lambda: lines.LineCacheHwp(hwpml=variant('last_paragraph')), unrelated_only),
            'target_and_unrelated_paragraph': (lambda: lines.LineCacheHwp(hwpml=variant('last_paragraph')),
                                               lambda h: setattr(h, 'reflow_damage', change_other((1, 0, 1)))),
            'other_section_paragraph': (lambda: lines.LineCacheHwp(hwpml=variant('second_section')),
                                        lambda h: setattr(h, 'reflow_damage', change_other((1, 0, 2)))),
        }
        for name, (build, configure) in cases.items():
            with self.subTest(case=name):
                hwp = build()
                o = self.owner(hwp)
                request = o.request('picture_layout_exact', mode='square')
                configure(hwp)
                out, error = o.call(request)
                self.assertTrue(hwp.executed)
                self.assertFalse(out is not None and out.get('ok') is True, (name, out))
                self.assertTrue(o.held(), name)

    def test_same_cache_change_without_an_authorized_layout_is_refused(self):
        def widen_target(fake, key):
            fake.lines[key][0]['horzsize'] -= 7
        # A crop with the same kind of target-paragraph cache change.
        hwp = lines.LineCacheHwp(hwpml=variant('last_paragraph'))
        hwp.reflow_damage = widen_target
        o = self.owner(hwp)
        out, error = o.call(o.request('picture_crop_exact', left=0.1, top=0.1, right=0.9, bottom=0.9))
        self.assertTrue(hwp.executed)
        self.assertTrue(o.held(), 'crop has no reflow allowance')
        # The same layout reflow under a profile without the declaration.
        hwp = lines.LineCacheHwp(hwpml=variant('last_paragraph'))
        o = self.owner(hwp, profile=copy.deepcopy(fakes.PROFILE))
        out, error = o.call(o.request('picture_layout_exact', mode='square'))
        self.assertTrue(hwp.executed)
        self.assertTrue(o.held(), 'no declaration: strict')
        # A cache change that appears only in the public save output.
        hwp = lines.LineCacheHwp(hwpml=variant('last_paragraph'))
        o = self.owner(hwp)
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        hwp.saved_package_damage = lambda entries: [(n, d.replace(b'spacing="600"', b'spacing="601"', 1) if n == 'Contents/section0.xml' else d)
                                                    for n, d in entries]
        with self.assertRaises(LocalCliServiceError):
            o.service.save(session_id=o.sid)
        self.assertTrue(o.session._normal_close_requires_save)

    def test_line_order_and_cardinality_adversaries(self):
        def reorder(fake, key):
            fake.lines[key].reverse()

        def add(fake, key):
            fake.lines[key].append(dict(fake.lines[key][-1], textpos=fake.lines[key][-1]['textpos'] + 12))

        def remove(fake, key):
            fake.lines[key].pop()
        for name, damage in (('reorder', reorder), ('add', add), ('remove', remove)):
            with self.subTest(case=name):
                hwp = lines.LineCacheHwp(hwpml=variant('floating_top_bottom_last'), lines_per_paragraph={(1, 0, 3): 2})
                hwp.reflow_damage = damage
                o = self.owner(hwp)
                out, error = o.call(o.request('picture_layout_exact', mode='square'))
                self.assertTrue(hwp.executed)
                self.assertTrue(o.held(), name)


# --------------------------------------------------------------------------- C601-REPORT-1


class C601Report1(unittest.TestCase):
    """A fresh-copy save reports the caret mapping and export dirty declaration it relied on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def qualified(self, *, not_run=()):
        data = copy.deepcopy(fakes.PROFILE)
        data['qualification'] = {'status': 'qualified', 'basis': 'SYNTHETIC qualified portable profile', 'evidence_sha256': 'e' * 64}
        for name in proof.EVIDENCE_FIELDS:
            data['evidence'][name] = {'status': 'not_run' if name in not_run else 'observed', 'basis': 'SYNTHETIC'}
        return data

    def save_after(self, profile, *, navigate):
        hwp = fakes.PackagePictureHwp()
        o = SavedOwner(self, hwp, env=fakes.profile_env(Path(tempfile.mkdtemp(dir=self.tmp.name)), profile))
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        if navigate:
            o.service.cursor_move(direction='right', count=1, session_id=o.sid)
        return o.service.save(session_id=o.sid)['saved_proof']

    def test_fresh_copy_reports_caret_and_export_dirty(self):
        for missing in ('caret', 'export_native_dirty'):
            with self.subTest(not_run=missing):
                saved = self.save_after(self.qualified(not_run=(missing,)), navigate=True)
                self.assertEqual(saved['baseline'], 'save_fresh_copy')
                self.assertIn('caret', saved['evidence_used'])
                self.assertIn('export_native_dirty', saved['evidence_used'])
                self.assertEqual(saved['evidence_not_observed'], [missing])
                self.assertFalse(saved['native_qualified'])
        saved = self.save_after(self.qualified(), navigate=True)
        self.assertTrue(saved['native_qualified'])
        self.assertEqual(saved['evidence_not_observed'], [])

    def test_ordinary_verified_save_evidence_is_unchanged(self):
        saved = self.save_after(self.qualified(not_run=('caret',)), navigate=False)
        self.assertEqual(saved['baseline'], 'verified_edit')
        self.assertEqual(saved['evidence_used'], list(proof.SAVE_EVIDENCE) + ['public_save'])
        self.assertTrue(saved['native_qualified'])


if __name__ == '__main__':
    unittest.main()
