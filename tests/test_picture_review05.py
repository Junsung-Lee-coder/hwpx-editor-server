"""Regressions for the independent source review findings C587-* (real service/runtime/save/history).

Only the external SDK (tests/hwpx_package_fakes.py, tests/test_picture_mutation_contract.py)
is synthetic. Product guards and proof functions are the real ones. Not native evidence.
"""
from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import hwpx_saved_proof as proof
from app import local_cli_runtime as rt
from app import local_cli_service as service_module
from app.local_cli_service import LocalCliUndoRefused
from app.local_cli_service_support import LocalCliServiceError
from tests import hwpx_package_fakes as fakes
from tests import test_picture_mutation_contract as sdk
from tests.test_picture_saved_package import SavedOwner, events


def section(path) -> str:
    with zipfile.ZipFile(path) as archive:
        return archive.read('Contents/section0.xml').decode()


def caret_pos(path) -> str:
    with zipfile.ZipFile(path) as archive:
        text = archive.read('settings.xml').decode()
    return text.split('pos="', 1)[1].split('"', 1)[0]


def floating(hwp, wrap: str):
    """SYNTHETIC start state: the first picture floats with the given wrap."""
    picture = sdk.first(hwp.root, 'picture')
    sdk.first(picture, 'position').set('TreatAsChar', 'false')
    sdk.first(picture, 'shapeobject').set('TextWrap', wrap)
    return hwp


class Review05Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def env(self, profile=None):
        return fakes.profile_env(Path(tempfile.mkdtemp(dir=self.tmp.name)), profile)

    def owner(self, hwp=None, *, profile=None, legacy=False, **kw):
        hwp = hwp if hwp is not None else fakes.PackagePictureHwp(**kw)
        return SavedOwner(self, hwp, env=None if legacy else self.env(profile))


class C587Save1(Review05Base):
    """Non-content commands and history outcomes after a verified edit must stay saveable."""

    def assert_saved_with_proof(self, o, out, basis):
        self.assertEqual(out['save_decision'], 'verified_save_false')
        self.assertEqual(events(o.hwp, 'Save'), [('Save', False)])
        self.assertEqual(out['saved_proof']['basis'], 'saved_package_equals_verified_private_copy')
        self.assertEqual(out['saved_proof']['baseline'], basis)
        self.assertFalse(out['logical_debt_retained'])
        self.assertFalse(o.session._normal_close_requires_save, 'debt discharged only with the saved-package proof')

    def test_picture_edit_then_cursormove_then_save_keeps_the_edit(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        anchor = caret_pos(sorted(o.root.glob('private-proof/*/*-after.hwpx'))[-1])
        moved = o.service.cursor_move(direction='right', count=1, session_id=o.sid)
        self.assertTrue(moved['ok'])
        self.assertFalse(o.hwp.modified, 'navigation does not set the native flag')
        out = o.service.save(session_id=o.sid)
        self.assert_saved_with_proof(o, out, 'save_fresh_copy')
        self.assertIn('textWrap="SQUARE"', section(o.wc))
        self.assertEqual(caret_pos(o.wc), str(int(anchor) + 1), 'the saved caret is the moved caret, proved by the fresh copy')

    def test_bundle_picture_edit_with_read_only_context_step_then_save(self):
        o = self.owner()
        out = o.bundle(o.request('picture_layout_exact', mode='square'), {'op': 'context', 'label': 'context'})
        self.assertTrue(out['ok'], out)
        saved = o.service.save(session_id=o.sid)
        self.assert_saved_with_proof(o, saved, 'save_fresh_copy')
        self.assertIn('textWrap="SQUARE"', section(o.wc))

    def test_history_mismatch_then_save_is_an_explicit_proved_outcome(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])

        def damage(fake):
            next(e for e in fake.root.iter() if sdk.local(e) == 'char' and e.text).text = 'UNDO DAMAGE'
        o.hwp.undo_damage = damage
        undone = o.service.undo(session_id=o.sid)
        self.assertFalse(undone['ok'])
        self.assertIn('undo_state_mismatch', undone['detail'])
        with self.assertRaises(LocalCliUndoRefused):
            o.service.redo(session_id=o.sid)  # history stopped
        out = o.service.save(session_id=o.sid)
        self.assert_saved_with_proof(o, out, 'history_mismatch_reached')
        self.assertIn('UNDO DAMAGE', section(o.wc), 'the reached state is what was saved, and it is named so')

    def test_first_false_undo_not_applied_then_save_keeps_the_edit(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        o.hwp.undo_result = False
        with self.assertRaises(LocalCliUndoRefused) as refused:
            o.service.undo(session_id=o.sid)
        self.assertEqual(refused.exception.code, 'undo_not_applied')
        out = o.service.save(session_id=o.sid)
        self.assert_saved_with_proof(o, out, 'history_not_applied')
        self.assertIn('textWrap="SQUARE"', section(o.wc))

    def test_fresh_copy_route_keeps_every_save_gate(self):
        # Changed profile pin at save: nothing is saved and the debt stays, with a named reason.
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        o.service.cursor_move(direction='right', count=1, session_id=o.sid)
        other = copy.deepcopy(fakes.PROFILE)
        other['profile_id'] = 'synthetic-portable-test/changed'
        before = len(events(o.hwp, 'SaveAs'))
        with patch.dict(os.environ, self.env(other)):
            out = o.service.save(session_id=o.sid)
        self.assertEqual(out['save_decision'], 'clean_noop')
        self.assertTrue(out['logical_debt_retained'])
        self.assertEqual(out['debt_reason'], 'hwpx_profile_changed')
        self.assertEqual(len(events(o.hwp, 'SaveAs')), before, 'no private copy under a changed profile')
        self.assertEqual(events(o.hwp, 'Save'), [])
        self.assertTrue(o.session._normal_close_requires_save)
        # Retargeted working document at save: uncertainty holds custody; nothing is published.
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        o.service.cursor_move(direction='right', count=1, session_id=o.sid)
        o.hwp.path = str(o.base / 'elsewhere.hwpx')
        with self.assertRaises(LocalCliServiceError) as held:
            o.service.save(session_id=o.sid)
        self.assertIn('native_custody_held', held.exception.message)
        self.assertEqual(events(o.hwp, 'Save'), [])
        self.assertTrue(o.session._normal_close_requires_save)

    def test_held_session_issues_no_save_or_copy_afterwards(self):
        hwp = fakes.PackagePictureHwp()
        hwp.after_execute_package_damage = lambda entries: [(n, d + b'!' if n == 'Custom/unknown.bin' else d) for n, d in entries]
        o = self.owner(hwp)
        out, error = o.call(o.request('picture_layout_exact', mode='square'))
        self.assertTrue(o.held())
        sdk_events = len(hwp.events)
        with self.assertRaises((LocalCliServiceError, rt.LocalCliRuntimeError)):
            o.service.save(session_id=o.sid)
        self.assertEqual(hwp.events[sdk_events:], [], 'no SDK diagnostic, copy or Save after the first unknown')


class C587Save1Flows(Review05Base):
    """Decisive flows: verified picture edit -> non-content command -> save -> close."""

    def save_then_close(self, o):
        out = o.service.save(session_id=o.sid)
        self.assertEqual(out['save_decision'], 'verified_save_false', out)
        self.assertEqual(out['saved_proof']['baseline'], 'save_fresh_copy')
        self.assertEqual(out['saved_proof']['fresh_copy_bound_to'], 'verified_edit')
        self.assertEqual(events(o.hwp, 'Save'), [('Save', False)])
        self.assertIn('textWrap="SQUARE"', section(o.wc), 'the prior valid edit is in the saved file')
        self.assertFalse(o.session._normal_close_requires_save)
        self.assert_close_passes_the_save_gate(o)

    def assert_close_passes_the_save_gate(self, o):
        # Close is no longer refused for save; it reaches full native closure, where the real
        # original-constructor ownership guard holds because this portable fake has no proved
        # constructor ownership (not modeled, never faked).
        with self.assertRaises(LocalCliServiceError) as closing:
            o.service.close(session_id=o.sid)
        self.assertNotIn('save_required', closing.exception.message)
        self.assertIn('original constructor ownership is unproved', str(closing.exception.__cause__.__cause__))

    def test_navigation_and_selection_commands_then_save_and_close(self):
        for name in ('move', 'select', 'cell', 'cellmove'):
            with self.subTest(command=name):
                o = self.owner()
                self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
                # The real runtime marks this command's intent by its name; the handler body
                # stands in for the SDK navigation (SYNTHETIC caret move, no content change).
                o.manager.execute(session_id=o.sid, command_name=name, handler=lambda handle: (o.hwp.MoveRight(), {'ok': True})[1])
                self.assertFalse(o.hwp.modified)
                self.save_then_close(o)

    def test_bundle_export_step_then_save_and_close(self):
        o = self.owner()
        out = o.bundle(o.request('picture_layout_exact', mode='square'), {'op': 'export_pdf', 'label': 'pdf'})
        self.assertTrue(out['ok'], out)
        self.assertIn(('SaveAs', 'PDF', None), o.hwp.events)
        self.save_then_close(o)

    def test_bundle_where_step_keeps_the_verified_package_current(self):
        o = self.owner()
        out = o.bundle(o.request('picture_layout_exact', mode='square'), {'op': 'where', 'label': 'where'})
        self.assertTrue(out['ok'], out)
        saved = o.service.save(session_id=o.sid)
        self.assertEqual(saved['save_decision'], 'verified_save_false')
        self.assertEqual(saved['saved_proof']['baseline'], 'verified_edit', 'no fresh copy when nothing staled the package')
        self.assertNotIn('fresh_copy_bound_to', saved['saved_proof'])
        self.assert_close_passes_the_save_gate(o)

    def test_unbound_fresh_copy_saves_nothing_and_keeps_the_debt(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])

        def silent_change(handle):
            # SYNTHETIC native content change that leaves IsModified exactly False.
            next(e for e in o.hwp.root.iter() if sdk.local(e) == 'char' and e.text).text = 'SILENT CHANGE'
            return {'ok': True}
        o.manager.execute(session_id=o.sid, command_name='move', handler=silent_change)
        before = len(events(o.hwp, 'SaveAs'))
        out = o.service.save(session_id=o.sid)
        self.assertEqual(out['save_decision'], 'clean_noop')
        self.assertTrue(out['logical_debt_retained'])
        self.assertEqual(out['debt_reason'], 'hwpx_fresh_copy_unbound')
        self.assertEqual(len(events(o.hwp, 'SaveAs')), before + 1, 'one fresh private copy, not recorded')
        self.assertEqual(events(o.hwp, 'Save'), [])
        self.assertNotIn('SILENT CHANGE', section(o.wc))
        self.assertTrue(o.session._normal_close_requires_save)
        with self.assertRaises(LocalCliServiceError) as refused:
            o.service.close(session_id=o.sid)
        self.assertIn('save_required', refused.exception.message)


class C587Layout1(Review05Base):
    """One request-derived inline contract: TreatAsChar only; TextWrap unchanged."""

    def run_inline(self, wrap, *, legacy, unrequested=None):
        hwp = floating(fakes.PackagePictureHwp(), wrap)
        hwp.unrequested_wrap = unrequested
        o = self.owner(hwp, legacy=legacy)
        out, error = o.call(o.request('picture_layout_exact', mode='inline'))
        return o, hwp, out

    def test_floating_square_and_top_and_bottom_to_inline_under_both_strategies(self):
        for legacy in (True, False):
            for wrap, owpml in (('Square', 'SQUARE'), ('TopAndBottom', 'TOP_AND_BOTTOM')):
                with self.subTest(legacy=legacy, wrap=wrap):
                    o, hwp, out = self.run_inline(wrap, legacy=legacy)
                    self.assertTrue(out is not None and out['ok'], out)
                    self.assertFalse(o.held())
                    self.assertNotIn('put:HShapeObject.TextWrap', hwp.events, 'inline requests TreatAsChar only')
                    picture = sdk.first(hwp.root, 'picture')
                    self.assertEqual(sdk.first(picture, 'position').get('TreatAsChar'), 'true')
                    self.assertEqual(sdk.first(picture, 'shapeobject').get('TextWrap'), wrap)
                    result = out['steps'][0]['result']
                    self.assertEqual(result['preservation_strategy'], 'legacy_hwpml_strict' if legacy else 'native_hwpx_package')
                    if not legacy:
                        saved = o.service.save(session_id=o.sid)
                        self.assertEqual(saved['save_decision'], 'verified_save_false')
                        text = section(o.wc)
                        self.assertIn('treatAsChar="1"', text)
                        self.assertIn('textWrap="%s"' % owpml, text)

    def test_unrequested_wrap_change_with_inline_holds_under_both_strategies(self):
        for legacy in (True, False):
            for wrap, unrequested in (('Square', 'TopAndBottom'), ('TopAndBottom', 'Square')):
                with self.subTest(legacy=legacy, wrap=wrap):
                    o, hwp, out = self.run_inline(wrap, legacy=legacy, unrequested=unrequested)
                    self.assertTrue(hwp.executed)
                    self.assertFalse(out is not None and out.get('ok') is True, out)
                    self.assertTrue(o.held())

    def test_square_mode_still_requests_and_proves_square(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                hwp = fakes.PackagePictureHwp()
                o = self.owner(hwp, legacy=legacy)
                out = o.bundle(o.request('picture_layout_exact', mode='square'))
                self.assertTrue(out['ok'], out)
                self.assertIn('put:HShapeObject.TextWrap', hwp.events)
                self.assertEqual(sdk.first(sdk.first(hwp.root, 'picture'), 'shapeobject').get('TextWrap'), 'Square')


class C587Profile1(unittest.TestCase):
    """regenerated_members is a fixed Preview allowlist with declared encodings and disjoint selectors."""

    def mutated(self, mutate):
        data = copy.deepcopy(fakes.PROFILE)
        mutate(data)
        return data

    def test_every_non_preview_member_class_is_refused(self):
        # Reuse the synthetic profile's own valid preview rule, so only the member name differs.
        png = dict(fakes.PROFILE['regenerated_members']['Preview/PrvImage.png'])
        text = {**png, 'kind': 'text', 'same_pixel_size': False, 'encoding': 'utf-8'}
        for name in ('Scripts/headerScripts', 'Scripts/sourceScripts', 'settings.xml', 'META-INF/manifest.xml', 'META-INF/container.rdf',
                     'META-INF/container.xml', 'Contents/masterpage0.xml', 'mimetype', 'version.xml', 'BinData/image1.png',
                     'Contents/section0.xml', 'Contents/header.xml', 'Contents/content.hpf', 'Preview/Other.png', 'Custom/unknown.bin'):
            for rule in (png, text):
                with self.subTest(name=name, kind=rule['kind']):
                    with self.assertRaises(proof.ProofUnavailable):
                        proof.validate_profile(self.mutated(lambda p: p['regenerated_members'].update({name: dict(rule)})))

    def test_preview_rules_are_typed_and_selectors_disjoint(self):
        bad = {
            'text_kind_for_image': lambda p: p['regenerated_members'].update({'Preview/PrvImage.png': {'kind': 'text', 'when': 'rendering_request', 'same_pixel_size': False, 'encoding': 'utf-8'}}),
            'png_kind_for_text': lambda p: p['regenerated_members'].update({'Preview/PrvText.txt': {'kind': 'png', 'when': 'rendering_request', 'same_pixel_size': True}}),
            'text_without_encoding': lambda p: p['regenerated_members'].update({'Preview/PrvText.txt': {'kind': 'text', 'when': 'rendering_request', 'same_pixel_size': False}}),
            'png_with_encoding': lambda p: p['regenerated_members']['Preview/PrvImage.png'].update(encoding='utf-8'),
            'unknown_encoding': lambda p: p['regenerated_members'].update({'Preview/PrvText.txt': {'kind': 'text', 'when': 'rendering_request', 'same_pixel_size': False, 'encoding': 'latin-1'}}),
            'volatile_on_caret_element': lambda p: p['volatile_values'].append({'member': 'settings.xml', 'element': p['caret']['element'], 'where': {'listIDRef': '0'},
                                                                                 'field': '@pos', 'value_type': 'utc_second_z', 'bound': 'native_write_window'}),
            'duplicate_volatile': lambda p: p['volatile_values'].append(dict(p['volatile_values'][0])),
            'volatile_on_regenerated': lambda p: p['volatile_values'].append({'member': 'Preview/PrvImage.png', 'element': '{u}x', 'where': {'a': 'b'},
                                                                               'field': 'text', 'value_type': 'utc_second_z', 'bound': 'native_write_window'}),
        }
        for name, mutate in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(proof.ProofUnavailable):
                    proof.validate_profile(self.mutated(mutate))
        ok = self.mutated(lambda p: p['regenerated_members'].update({'Preview/PrvText.txt': {'kind': 'text', 'when': 'rendering_request', 'same_pixel_size': False, 'encoding': 'utf-8'}}))
        self.assertIn('Preview/PrvText.txt', proof.validate_profile(ok)['regenerated_members'])

    def test_declared_text_encoding_is_strict_and_previews_stay_strict_without_a_request(self):
        data = self.mutated(lambda p: p['regenerated_members'].update({'Preview/PrvText.txt': {'kind': 'text', 'when': 'rendering_request', 'same_pixel_size': False, 'encoding': 'utf-8'}}))
        loaded = proof.Profile(proof.validate_profile(data), 'f' * 64, 'x')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hwp = fakes.PackagePictureHwp()
        a, b, c = (Path(tmp.name) / n for n in ('a.hwpx', 'b.hwpx', 'c.hwpx'))
        fakes.write_package(hwp, a)
        fakes.write_package(hwp, b, damage=lambda e: [(n, '설명'.encode('utf-8') if n == 'Preview/PrvText.txt' else d) for n, d in e])
        fakes.write_package(hwp, c, damage=lambda e: [(n, '설명'.encode('utf-16-le') if n == 'Preview/PrvText.txt' else d) for n, d in e])
        window = (proof.datetime.now(proof.timezone.utc) - proof.timedelta(seconds=30), proof.datetime.now(proof.timezone.utc) + proof.timedelta(seconds=1))
        states = {k: proof.read_state(v, window=window) for k, v in (('a', a), ('b', b), ('c', c))}
        proof.compare(states['a'], states['b'], proof.Expectation({}, rendering_request=True), loaded)
        with self.assertRaises(proof.PackageMismatch) as wrong:
            proof.compare(states['a'], states['c'], proof.Expectation({}, rendering_request=True), loaded)
        self.assertEqual({d['kind'] for d in wrong.exception.deltas}, {'regenerated_invalid'})
        with self.assertRaises(proof.PackageMismatch) as strict:
            proof.compare(states['a'], states['b'], proof.Expectation({}), loaded)
        self.assertEqual({d['kind'] for d in strict.exception.deltas}, {'bytes'})


class C587Profile2(Review05Base):
    def test_export_clearing_profile_without_completing_save_is_refused_before_any_sdk_call(self):
        profile = copy.deepcopy(fakes.PROFILE)
        profile['public_save']['save_false_after_export_clear'] = False
        hwp = fakes.PackagePictureHwp()
        o = self.owner(hwp, profile=profile)
        request = o.request('picture_layout_exact', mode='square')
        before = list(hwp.events)
        out = o.bundle(request)
        self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_proof_unqualified')
        self.assertEqual(hwp.events[len(before):], [], 'refused before any SDK read or effect')
        self.assertFalse(o.held())

    def test_dirty_preserving_profile_without_save_false_stays_admissible(self):
        profile = copy.deepcopy(fakes.PROFILE)
        profile['export_native_dirty'] = 'unchanged'
        profile['public_save']['save_false_after_export_clear'] = False
        o = self.owner(profile=profile, export_clears=False)
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        self.assertEqual(o.service.save(session_id=o.sid)['save_decision'], 'ordinary_save')


class C587Xml1(unittest.TestCase):
    """Every XML-compared member binds its whole document; every XML parse refuses DTDs."""

    T = proof.datetime(2026, 11, 5, 12, 0, 0, tzinfo=proof.timezone.utc)

    def compare(self, damage, profile=None, expectation=None, both=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hwp = fakes.PackagePictureHwp()
        hwp.clock = lambda: self.T
        a, b = Path(tmp.name) / 'a.hwpx', Path(tmp.name) / 'b.hwpx'
        fakes.write_package(hwp, a, damage=both)
        fakes.write_package(hwp, b, damage=(lambda e: damage(both(e))) if both and damage else (damage or both))
        window = (self.T - proof.timedelta(seconds=1), self.T + proof.timedelta(seconds=1))
        loaded = proof.Profile(proof.validate_profile(copy.deepcopy(profile or fakes.PROFILE)), 'f' * 64, 'x')
        before, after = proof.read_state(a, window=window), proof.read_state(b, window=window)
        trees = {}
        if expectation == 'section':
            trees['Contents/section0.xml'] = proof._parse(before.members['Contents/section0.xml'])
        return proof.compare(before, after, proof.Expectation(trees, rendering_request=bool(trees)), loaded)

    def edit(self, member, change):
        return lambda entries: [(n, change(d) if n == member else d) for n, d in entries]

    def test_outside_root_nodes_bom_and_epilog_are_bound(self):
        decl = fakes.DECL.encode()
        cases = {
            # Inserted right after the XML declaration, before whichever root the member has.
            'comment_before_root': (None, lambda d: d.replace(b'?>', b'?><!--x-->', 1)),
            'pi_before_root': (None, lambda d: d.replace(b'?>', b'?><?pi y?>', 1)),
            'trailing_comment': (None, lambda d: d + b'<!--z-->'),
            'trailing_whitespace': (None, lambda d: d + b'\n'),
            'bom': (None, lambda d: b'\xef\xbb\xbf' + d),
            # Both copies lack the declaration; only the after copy gains a BOM.
            'bom_without_declaration': (lambda d: d[len(decl):], lambda d: b'\xef\xbb\xbf' + d),
        }
        for member in ('Contents/section0.xml', 'Contents/content.hpf'):
            for name, (both, change) in cases.items():
                with self.subTest(member=member, case=name):
                    damaged = change(both(decl + b'<r/>') if both else decl + b'<r/>')
                    self.assertNotEqual(damaged, both(decl + b'<r/>') if both else decl + b'<r/>', 'the adversary really changes bytes')
                    with self.assertRaises(proof.PackageMismatch) as held:
                        self.compare(self.edit(member, change), expectation='section' if member.startswith('Contents/section') else None,
                                     both=self.edit(member, both) if both else None)
                    self.assertTrue({d['kind'] for d in held.exception.deltas} & {'xml_outer', 'xml_declaration', 'bytes'}, held.exception.deltas)

    def test_legitimate_syntax_equivalence_and_typed_date_stay_positive(self):
        swap = self.edit('Contents/section0.xml', lambda d: d.replace(b'<hs:sec xmlns:hs=', b'<hs:sec  xmlns:hs=', 1))
        self.assertEqual(self.compare(swap, expectation='section')['infoset_compared'], 2)
        self.assertEqual(self.compare(None)['infoset_compared'], 1)

    def test_every_member_parsed_as_xml_refuses_dtd_and_entities(self):
        from local_cli_v1.hwpx_reader import HwpxReadError, read_package_entries
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hwp = fakes.PackagePictureHwp()
        path = Path(tmp.name) / 'rdf.hwpx'
        fakes.write_package(hwp, path, damage=lambda e: e + [('META-INF/container.rdf', b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e "x">]><r>&e;</r>')])
        with self.assertRaises(HwpxReadError):
            read_package_entries(path)
        profile = copy.deepcopy(fakes.PROFILE)
        profile['volatile_values'].append({'member': 'Custom/meta.dat', 'element': '{u}m', 'where': {'a': 'b'}, 'field': 'text',
                                           'value_type': 'utc_second_z', 'bound': 'native_write_window'})
        dtd = b'<!DOCTYPE m [<!ENTITY e "z">]><m xmlns="u" a="b">&e;</m>'
        # The same DTD-bearing member in both copies: only the parser guard can refuse it.
        with self.assertRaises(proof.PackageMismatch) as held:
            self.compare(None, profile, both=lambda e: e + [('Custom/meta.dat', dtd)])
        self.assertEqual({d['kind'] for d in held.exception.deltas}, {'xml_unproved'})
        with self.assertRaises(ValueError):
            proof._parse(dtd)


class C587Test1(Review05Base):
    """The composition guards themselves: mixed declared-picture + non-picture, and unchained states."""

    def test_declared_picture_plus_single_undo_non_picture_step_refuses_before_native_undo(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        raw = o.session._commands_by_id[o.session._last_command_id].result['steps'][0]
        self.assertTrue(service_module._picture_history(raw), 'a genuine declared picture step')
        # SYNTHETIC qualified single-Undo non-picture step result, shaped as its engine reports it.
        other = {'op': 'table_structure_exact', 'ok': True, 'dirty': True, 'result': {'undo': {'native_editing_actions': 1}}}
        self.assertTrue(service_module._bundle_step_single_undo(other))
        binding = {'undo_stack': [], 'redo_stack': []}
        service_module._record_bundle_undo_state(binding, [raw, other], {'pending_logical_undo_count': None, 'logical_undo_unverified': None})
        self.assertIn('picture_history_composition', binding['logical_undo_unverified'])
        self.assertIsNone(binding['pending_logical_undo_count'])
        self.assertEqual(binding['undo_stack'], [None])
        o.store.update(binding)
        with self.assertRaises(LocalCliUndoRefused) as refused:
            o.service.undo(session_id=o.sid)
        self.assertEqual(refused.exception.code, 'undo_not_counted')
        self.assertNotIn('Run:Undo', o.hwp.events)

    def test_two_declared_picture_steps_whose_states_do_not_chain_refuse_history(self):
        hwp = fakes.PackagePictureHwp(shared=True)

        def between(fake):
            # SYNTHETIC native side effect after step 1's after copy and before step 2 starts.
            next(e for e in fake.root.iter() if sdk.local(e) == 'char' and e.text).text = 'BETWEEN STEPS'
        o = self.owner(hwp)
        first = o.request('picture_layout_exact', index=0, mode='square')
        second = o.request('picture_layout_exact', index=1, mode='square')
        hwp.saveas_hooks[2] = between
        out = o.bundle(first, second)
        self.assertTrue(out['ok'], out)
        raw = o.session._commands_by_id[o.session._last_command_id].result['steps']
        histories = [service_module._picture_history(step) for step in raw]
        self.assertTrue(all(histories), 'both steps are declared and individually counted')
        self.assertNotEqual(histories[0]['post_state_sha256'], histories[1]['pre_state_sha256'])
        self.assertIn('picture_history_composition', o.store['logical_undo_unverified'])
        with self.assertRaises(LocalCliUndoRefused) as refused:
            o.service.undo(session_id=o.sid)
        self.assertEqual(refused.exception.code, 'undo_not_counted')
        self.assertNotIn('Run:Undo', hwp.events)


class C587Cli(unittest.TestCase):
    """The CLI save capture treats a malformed or digest-less proof as a mismatch, never as legacy."""

    def run_save(self, payload, data=b'PK-bytes'):
        from local_cli_v1 import main as cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        destination = Path(tmp.name) / 'out.hwpx'
        state = {'base_url': 'http://synthetic.invalid', 'session_id': 's' * 32, 'state_generation': 0}
        with patch.object(cli, 'load_state', return_value=state), patch.object(cli, '_state_cas_snapshot', return_value=(state, 0, 's' * 32)), \
                patch.object(cli, 'update_state', side_effect=lambda fn, **kw: fn(dict(state))), \
                patch.object(cli, 'post_json', return_value={'ok': True, 'download_path': '/x', **payload}), \
                patch.object(cli, 'download_to_path', side_effect=lambda base, route, target: (target.write_bytes(data), target)[1]), \
                patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
            status = cli.main(['save', '--out', str(destination)])
        return status, destination, Path(tmp.name)

    def test_present_but_malformed_proof_is_refused_and_nothing_is_kept(self):
        import hashlib
        good = hashlib.sha256(b'PK-bytes').hexdigest()
        for value in ({'basis': 'x'}, {'saved_package_sha256': good.upper()}, {'saved_package_sha256': good[:63]}, 'not-a-proof', ['x']):
            with self.subTest(value=value):
                status, destination, folder = self.run_save({'saved_proof': value})
                self.assertNotEqual(status, 0)
                self.assertFalse(destination.exists())
                self.assertEqual([p.name for p in folder.iterdir()], [])
        status, destination, _ = self.run_save({'saved_proof': {'saved_package_sha256': good}})
        self.assertEqual(status, 0)
        status, destination, _ = self.run_save({'saved_proof': None})
        self.assertEqual(status, 0, 'an absent proof keeps the legacy capture')


if __name__ == '__main__':
    unittest.main()
