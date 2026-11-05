"""ACTUAL native e01 data through the real product reader, bridge, comparator and caller.

Part A runs the product consumers on the unmodified native bytes (four private
HWPX copies and their HWPML2X producer strings from Hancom 13.0.0.3457). Part B
replays that data through the real owning STA, service, save and history; its
edit after-state and re-dated ModifiedDate are SYNTHETIC (tests/native_e01_replay.py).
Neither part is native picture-edit, save or history evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from app import hwpx_saved_proof as proof
from app import native_picture as picture
from app.local_cli_service import LocalCliUndoRefused
from app.local_cli_service_support import LocalCliServiceError
from tests import native_e01_replay as replay
from tests.test_picture_saved_package import SavedOwner, events

ORDER = ['mimetype', 'version.xml', 'Contents/header.xml', 'BinData/image1.PNG', 'Contents/section0.xml', 'Preview/PrvText.txt',
         'settings.xml', 'Preview/PrvImage.png', 'META-INF/container.rdf', 'Contents/content.hpf', 'META-INF/container.xml',
         'META-INF/manifest.xml']


def candidate() -> proof.Profile:
    data = replay.CANDIDATE.read_bytes()
    return proof.Profile(proof.validate_profile(json.loads(data)), hashlib.sha256(data).hexdigest(), 'operator_configured_file')


def state(epoch: str, *, window=True) -> proof.PackageState:
    return proof.read_state(replay.package_path(epoch), window=replay.window(epoch) if window else None)


def damaged(epoch: str, edit, directory: str) -> proof.PackageState:
    path = Path(tempfile.mkdtemp(dir=directory)) / (epoch + '-damaged.hwpx')
    replay.write(path, edit(replay.real_members(epoch)))
    return proof.read_state(path, window=replay.window(epoch))


def member_edit(name, old, new):
    def edit(entries):
        out = []
        for info, data in entries:
            if info.filename == name:
                if old not in data:
                    raise AssertionError('damage anchor missing in real member ' + name)
                data = data.replace(old, new, 1)
            out.append((info, data))
        return out
    return edit


class ActualProducerStrings(unittest.TestCase):
    def test_qualified_decoder_matches_every_producer_witness(self):
        self.assertEqual([replay.EPOCHS[e]['producer_witness']['chars'] for e in replay.EPOCHS], [12981, 13028, 12981, 13028])
        for epoch in replay.EPOCHS:
            with self.subTest(epoch=epoch):
                raw = (replay.FIXTURE / replay.EPOCHS[epoch]['producer_string']).read_bytes()
                text = replay.producer_string(epoch)
                self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-16"'), 'the stale in-memory declaration is retained')
                self.assertEqual(text.encode('utf-8'), raw)
                with self.assertRaises(ET.ParseError):
                    ET.fromstring(raw)  # trusting the declaration on persisted bytes is the known decoder trap
                for bad in (raw[:-1] + b'?', b'\xef\xbb\xbf' + raw):
                    with self.assertRaises(ValueError):
                        proof.producer_text(bad, **replay.EPOCHS[epoch]['producer_witness'])

    def test_native_control_census_witness_equals_product_universe(self):
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        expected = [digest(k) for k in ('secd', 'cold', 'gso')]
        for epoch, row in replay.EPOCHS.items():
            model = picture.parse_pictures(replay.producer_string(epoch))
            universe = picture._selection_universe(model, model['pictures'][0])
            self.assertEqual(row['native_control_census_witness']['CtrlID_sha256'], expected)
            self.assertEqual(row['native_control_census_witness']['GetCtrlInstID_sha256'], [digest(i) for i in universe['gso_ids']])
        self.assertEqual({k: v for k, v in universe['counts'].items() if v}, {'secd': 1, 'cold': 1, 'gso': 1})


class ActualPackages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = candidate()

    def test_reader_keeps_all_twelve_members_order_and_container_fields(self):
        states = {e: state(e) for e in replay.EPOCHS}
        for epoch, current in states.items():
            with self.subTest(epoch=epoch):
                self.assertEqual(list(current.order), ORDER)
                self.assertEqual(current.sha256, replay.MANIFEST['files'][epoch + '.package.json']['decodes_to_sha256'])
                with zipfile.ZipFile(replay.package_path(epoch)) as archive:
                    self.assertIsNone(archive.testzip())
                self.assertEqual(current.container, states['clean'].container)

    def test_bridge_binds_the_one_picture_by_physical_facts_on_every_pair(self):
        for epoch in replay.EPOCHS:
            with self.subTest(epoch=epoch):
                model = picture.parse_pictures(replay.producer_string(epoch))
                target = model['pictures'][0]
                self.assertEqual((target['target_id'], target['supported'], target['context']), ('picture:1239128181', True, 'body'))
                link = proof.bridge(model, target, state(epoch), {'op': 'picture_layout_exact', 'mode': 'square'})
                self.assertEqual((link.ordinal, link.count, link.section, link.media_member), (0, 1, 'Contents/section0.xml', 'BinData/image1.PNG'))

    def test_bridge_refuses_unbindable_actual_package_damage(self):
        model = picture.parse_pictures(replay.producer_string('clean'))
        target = model['pictures'][0]
        cases = {
            'media_bytes': lambda entries: [(i, d + b'\x00' if i.filename == 'BinData/image1.PNG' else d) for i, d in entries],
            'frame': member_edit('Contents/section0.xml', b'<hp:sz width="9600"', b'<hp:sz width="9601"'),
            'crop': member_edit('Contents/section0.xml', b'<hp:imgClip left="0"', b'<hp:imgClip left="1"'),
            'placement': member_edit('Contents/section0.xml', b'treatAsChar="1"', b'treatAsChar="true"'),
            'second_picture': lambda entries: [(i, d.replace(b'</hp:pic>', b'</hp:pic>' + d[d.index(b'<hp:pic '):d.index(b'</hp:pic>') + 9], 1)
                                                if i.filename == 'Contents/section0.xml' else d) for i, d in entries],
            'dangling_reference': member_edit('Contents/section0.xml', b'binaryItemIDRef="image1"', b'binaryItemIDRef="image9"'),
        }
        for name, edit in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(proof.BridgeUnproven):
                    proof.bridge(model, target, damaged('clean', edit, self.tmp.name), {'op': 'picture_layout_exact', 'mode': 'square'})

    def test_no_change_history_pairs_pass_only_through_the_typed_modified_date(self):
        strict = copy.deepcopy(dict(self.profile.data))
        strict['volatile_values'] = []
        strict = proof.Profile(proof.validate_profile(strict), 'f' * 64, 'x')
        for before, after in (('clean', 'undo'), ('dirty', 'redo')):
            with self.subTest(pair=(before, after)):
                counts = proof.compare(state(before), state(after), proof.Expectation({}), self.profile)
                self.assertEqual(counts, {'byte_identical': 11, 'infoset_compared': 1, 'regenerated_not_compared': 0, 'added_media': 0})
                with self.assertRaises(proof.PackageMismatch) as held:
                    proof.compare(state(before), state(after), proof.Expectation({}), strict)
                self.assertEqual(held.exception.deltas, [{'kind': 'bytes', 'member': 'Contents/content.hpf'}])
                with self.assertRaises(proof.PackageMismatch) as unbound:
                    proof.compare(state(before), state(after, window=False), proof.Expectation({}), self.profile)
                self.assertEqual({d['kind'] for d in unbound.exception.deltas}, {'volatile_window'})
                with self.assertRaises(proof.PackageMismatch) as swapped:
                    proof.compare(state(before), proof.read_state(replay.package_path(after), window=replay.window(before)),
                                  proof.Expectation({}), self.profile)
                self.assertEqual({d['kind'] for d in swapped.exception.deltas}, {'volatile_window'})

    def test_actual_unrequested_body_edit_is_named_member_by_member(self):
        with self.assertRaises(proof.PackageMismatch) as held:
            proof.compare(state('clean'), state('dirty'), proof.Expectation({}), self.profile)
        self.assertEqual({(d['kind'], d['member']) for d in held.exception.deltas},
                         {('bytes', 'Contents/section0.xml'), ('bytes', 'Preview/PrvText.txt'), ('bytes', 'settings.xml'),
                          ('bytes', 'Preview/PrvImage.png')})
        # A rendering request does not excuse the unrequested text and caret change.
        with self.assertRaises(proof.PackageMismatch) as still:
            proof.compare(state('clean'), state('dirty'), proof.Expectation({}, rendering_request=True), self.profile)
        self.assertEqual({d['member'] for d in still.exception.deltas}, {'Contents/section0.xml', 'Preview/PrvText.txt', 'settings.xml'})

    def test_every_other_actual_metadata_and_reference_change_is_refused(self):
        cases = {
            'hashkey': member_edit('Contents/content.hpf', b'hashkey="/sckwJhO8ojLyeoISpisUA=="', b'hashkey="AAAAAAAAAAAAAAAAAAAAAA=="'),
            'lastsaveby': member_edit('Contents/content.hpf', b'name="lastsaveby" content="text">owner', b'name="lastsaveby" content="text">other'),
            'created_date': member_edit('Contents/content.hpf', b'2026-11-05T00:14:06Z', b'2026-11-05T11:35:49Z'),
            'summary_date': member_edit('Contents/content.hpf', '오전 9:17:17'.encode(), '오전 9:17:18'.encode()),
            'image_case': lambda entries: [(zipfile.ZipInfo('BinData/image1.png', i.date_time) if i.filename == 'BinData/image1.PNG' else i,
                                            d.replace(b'BinData/image1.PNG', b'BinData/image1.png') if i.filename == 'Contents/content.hpf' else d)
                                           for i, d in entries],
            'header_bytes': lambda entries: [(i, d + b'\n' if i.filename == 'Contents/header.xml' else d) for i, d in entries],
            'shape_comment': member_edit('Contents/section0.xml', '그림입니다.'.encode(), '그림.'.encode()),
            'cur_size': member_edit('Contents/section0.xml', b'<hp:curSz width="0"', b'<hp:curSz width="1"'),
        }
        for name, edit in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(proof.PackageMismatch):
                    proof.compare(state('clean'), damaged('clean', edit, self.tmp.name), proof.Expectation({}), self.profile)

    def test_history_digests_restore_exactly_on_actual_body_undo_redo(self):
        digest = {e: proof.state_digest(state(e), self.profile) for e in replay.EPOCHS}
        self.assertEqual(digest['clean'], digest['undo'])
        self.assertEqual(digest['dirty'], digest['redo'])
        self.assertNotEqual(digest['clean'], digest['dirty'])

    def test_candidate_profile_is_literal_about_its_evidence(self):
        observed = {name for name in proof.EVIDENCE_FIELDS if self.profile.evidence(name) == 'observed'}
        self.assertEqual(observed, {'materialization', 'bridge', 'export_native_dirty', 'container_volatile', 'volatile_values', 'caret'})
        self.assertFalse(self.profile.qualified)
        self.assertFalse(self.profile.native_qualified(proof.EDIT_EVIDENCE))
        self.assertEqual(self.profile.data['history'], {})
        for epoch in replay.EPOCHS:
            current = state(epoch)
            self.assertEqual(proof.caret_values(current, self.profile), tuple(replay.EPOCHS[epoch]['caret_before_after'][1]))
        link = proof.bridge(picture.parse_pictures(replay.producer_string('clean')), picture.parse_pictures(replay.producer_string('clean'))['pictures'][0],
                            state('clean'), {'op': 'picture_replace_exact'})
        self.assertEqual(proof.replacement_admissible(state('clean'), link, self.profile), ['hashkey'])
        self.assertTrue(proof.caret_in_domain(self.profile, (0, 0, 65)))
        self.assertFalse(proof.caret_in_domain(self.profile, (0, 1, 0)))


class ActualDataThroughRealCaller(unittest.TestCase):
    """Real runtime/service/save/history over the actual capture; edit after-state SYNTHETIC."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        shipped = replay.CANDIDATE.read_bytes()
        (directory / 'shipped.json').write_bytes(shipped)
        self.shipped_env = {'HWP_PICTURE_HWPX_PROFILE': str(directory / 'shipped.json'),
                            'HWP_PICTURE_HWPX_PROFILE_SHA256': hashlib.sha256(shipped).hexdigest()}
        # SYNTHETIC declaration for exercising the save path only: Save(False) stays not_run evidence.
        declared = json.loads(shipped)
        declared['profile_id'] = declared['profile_id'].rsplit('/', 1)[0] + '-save-declared-synthetic/v1'
        declared['public_save']['save_false_after_export_clear'] = True
        data = json.dumps(declared, sort_keys=True).encode()
        (directory / 'candidate.json').write_bytes(data)
        self.env = {'HWP_PICTURE_HWPX_PROFILE': str(directory / 'candidate.json'),
                    'HWP_PICTURE_HWPX_PROFILE_SHA256': hashlib.sha256(data).hexdigest()}

    def test_shipped_candidate_is_measurement_only_and_refused_before_any_sdk_call(self):
        hwp = replay.NativeReplayHwp()
        o = SavedOwner(self, hwp, env=self.shipped_env, working=lambda path: replay.write_input(path))
        request = o.request('picture_layout_exact', mode='square')
        before = list(hwp.events)
        out = o.bundle(request)
        self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_proof_unqualified')
        self.assertEqual(hwp.events[len(before):], [])
        self.assertFalse(o.held())

    def owner(self, hwp=None):
        hwp = hwp or replay.NativeReplayHwp()
        return SavedOwner(self, hwp, env=self.env, working=lambda path: replay.write_input(path))

    def test_layout_square_reaches_verified_public_save_with_literal_unobserved_fields(self):
        o = self.owner()
        inventory = o.inventory()
        self.assertEqual([p['target_id'] for p in inventory], ['picture:1239128181'])
        out = o.bundle(o.request('picture_layout_exact', mode='square'))
        self.assertTrue(out['ok'], out)
        saved = out['steps'][0]['result']['saved_proof']
        self.assertEqual(saved['strategy'], 'native_hwpx_package')
        self.assertFalse(saved['native_qualified'])
        self.assertEqual(saved['evidence_not_observed'], ['regenerated_members'])
        self.assertEqual((saved['byte_identical'], saved['infoset_compared'], saved['regenerated_not_compared']), (8, 3, 1))
        self.assertEqual(saved['bridge']['section_member'], 'Contents/section0.xml')
        self.assertFalse(o.hwp.modified, 'observed: the export clears the native flag')
        self.assertTrue(o.session._normal_close_requires_save, 'logical debt is separate from the native flag')
        result = o.service.save(session_id=o.sid)
        self.assertEqual(result['save_decision'], 'verified_save_false')
        self.assertEqual(result['saved_proof']['evidence_not_observed'], ['public_save'])
        self.assertFalse(result['saved_proof']['native_qualified'])
        with zipfile.ZipFile(o.wc) as archive:
            self.assertEqual(archive.namelist(), ORDER)
            section = archive.read('Contents/section0.xml')
            self.assertIn(b'treatAsChar="0"', section)
            self.assertIn(b'textWrap="SQUARE"', section)
            self.assertEqual(archive.read('BinData/image1.PNG'), dict((i.filename, d) for i, d in replay.real_members('clean'))['BinData/image1.PNG'])
        external = o.external / 'saved.hwpx'
        shutil.copyfile(o.wc, external)
        self.assertTrue(proof.verify_external(external, result['saved_proof'])['verified'])
        self.assertFalse(o.session._normal_close_requires_save)

    def test_actual_data_damage_after_execute_holds_without_followons(self):
        cases = {
            'hashkey': member_edit('Contents/content.hpf', b'hashkey="/sckwJhO8ojLyeoISpisUA=="', b'hashkey="AAAAAAAAAAAAAAAAAAAAAA=="'),
            'recorded_date_outside_window': lambda entries: [(i, re.sub(rb'ModifiedDate" content="text">[^<]*<', b'ModifiedDate" content="text">2026-11-05T11:35:49Z<', d)
                                                              if i.filename == 'Contents/content.hpf' else d) for i, d in entries],
            'caret_not_at_anchor': lambda entries: [(i, re.sub(rb'pos="[0-9]+"', b'pos="24"', d) if i.filename == 'settings.xml' else d) for i, d in entries],
            'cur_size': member_edit('Contents/section0.xml', b'<hp:curSz width="0"', b'<hp:curSz width="1"'),
            'image_case_rename': lambda entries: [(zipfile.ZipInfo('BinData/image1.png', i.date_time) if i.filename == 'BinData/image1.PNG' else i,
                                                   d.replace(b'BinData/image1.PNG', b'BinData/image1.png') if i.filename == 'Contents/content.hpf' else d)
                                                  for i, d in entries],
        }
        dirty_section = dict((i.filename, d) for i, d in replay.real_members('dirty'))['Contents/section0.xml']
        # The actual dirty section (native marker text) with the requested attributes applied.
        cases['actual_unrequested_marker'] = lambda entries: [
            (i, dirty_section.replace(b'treatAsChar="1"', b'treatAsChar="0"', 1).replace(b'textWrap="TOP_AND_BOTTOM"', b'textWrap="SQUARE"', 1)
             if i.filename == 'Contents/section0.xml' else d) for i, d in entries]
        for name, damage in cases.items():
            with self.subTest(damage=name):
                hwp = replay.NativeReplayHwp()
                hwp.after_entries_damage = damage
                o = self.owner(hwp)
                out, error = o.call(o.request('picture_layout_exact', mode='square'))
                self.assertTrue(hwp.executed)
                self.assertFalse(out is not None and out.get('ok') is True, (name, out))
                self.assertTrue(o.held(), name)
                self.assertEqual(len(events(hwp, 'SaveAs')), 2)
                self.assertEqual(events(hwp, 'Save'), [])

    def test_actual_data_refusals_before_selection(self):
        # Replacement: the actual manifest item carries hashkey, unqualified for a new item.
        from PIL import Image
        png = Path(self.tmp.name) / 'replacement.png'
        Image.new('RGB', (128, 128), 'purple').save(png)
        o = self.owner()
        out = o.bundle(o.request('picture_replace_exact', source_path=str(png)))
        self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_proof_unqualified')
        self.assertIn('hashkey', out['steps'][0]['refusal_message'])
        self.assertFalse(any(str(e).startswith(('Run:SelectCtrlFront', 'Execute')) for e in o.hwp.events))
        self.assertFalse(o.held())
        # A copy from another epoch (the actual dirty package for the clean document) does not bind the caret.
        stale = replay.NativeReplayHwp(epoch='clean', package_epoch='dirty')
        o = self.owner(stale)
        out = o.bundle(o.request('picture_layout_exact', mode='square'))
        self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_bridge_unproven')
        self.assertFalse(any(str(e).startswith(('Run:SelectCtrlFront', 'Execute')) for e in stale.events))
        self.assertFalse(o.held())

    def test_caret_outside_the_observed_domain_refuses_before_any_move(self):
        hwp = replay.NativeReplayHwp()
        hwp.native_position = (0, 1, 3)
        caret = next(e for e in hwp.root.iter() if e.tag == 'CARETPOS')
        caret.set('Para', '1')
        caret.set('Pos', '3')

        def settings(entries):
            return [(i, re.sub(rb'paraIDRef="0" pos="24"', b'paraIDRef="1" pos="3"', d) if i.filename == 'settings.xml' else d) for i, d in entries]
        original = replay.real_members
        o = self.owner(hwp)
        request = o.request('picture_layout_exact', mode='square')
        from unittest.mock import patch
        with patch.object(replay, 'real_members', lambda epoch: settings(original(epoch))):
            out = o.bundle(request)
        self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_proof_unqualified')
        self.assertFalse(any(str(e).startswith(('Run:SelectCtrlFront', 'Execute')) for e in hwp.events))
        self.assertEqual(hwp.native_position, (0, 1, 3))

    def test_candidate_declares_no_picture_history_and_refuses_before_dispatch(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        with self.assertRaises(LocalCliUndoRefused) as refused:
            o.service.undo(session_id=o.sid)
        self.assertEqual(refused.exception.code, 'undo_not_counted')
        self.assertNotIn('Run:Undo', o.hwp.events)

    def test_saved_file_must_equal_the_verified_state_even_with_actual_layout(self):
        hwp = replay.NativeReplayHwp()
        hwp.saved_entries_damage = lambda entries: [(i, d + b'\n' if i.filename == 'Contents/header.xml' else d) for i, d in entries]
        o = self.owner(hwp)
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        with self.assertRaises(LocalCliServiceError) as held:
            o.service.save(session_id=o.sid)
        self.assertIn('native_custody_held', held.exception.message)
        self.assertTrue(o.session._normal_close_requires_save)


if __name__ == '__main__':
    unittest.main()
