"""Saved-HWPX picture proof through the REAL owning STA, service, save, history and public projection.

Real LocalCliRuntimeManager/LocalCliLiveSession thread, real command_bundle,
real save/undo/redo, real managed-root artifact custody, real public picture
projection and CLI consumer. Only the external SDK (tests/hwpx_package_fakes.py)
and OS process/window observations are synthetic. This is NOT native evidence.
"""
from __future__ import annotations

import contextlib
import copy
import re
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from app import hwpx_saved_proof as proof
from app import local_cli_runtime as rt
from app.command_packages.runtime import get_command_package_registry
from app.local_cli_service import LocalCliService, LocalCliUndoRefused
from app.local_cli_service_support import LocalCliServiceError
from tests import hwpx_package_fakes as fakes
from tests import native_picture_typed_fakes as typed
from tests import test_picture_mutation_contract as sdk


def com_module():
    module = types.ModuleType('pythoncom')
    module.CoInitialize = lambda: None
    module.CoUninitialize = lambda: None
    module.TYPEATTR = typed.TypeAttr
    iid = types.ModuleType('pywintypes')
    iid.IID = typed.IID
    return {'pythoncom': module, 'pywintypes': iid}


class SavedOwner:
    """Real manager + owning thread + service over a managed session root; fake SDK only."""

    def __init__(self, case, hwp, *, env=None, sid='saved-session-0001', working=None):
        self.case, self.hwp, self.sid = case, hwp, sid
        stack = ExitStack()
        case.addCleanup(stack.close)
        tmp = tempfile.TemporaryDirectory()
        stack.callback(tmp.cleanup)
        base = Path(tmp.name)
        self.base = base
        self.sessions = base / 'sessions'
        self.root = self.sessions / sid
        rt.ensure_session_layout(self.root)
        self.wc = self.root / 'working' / 'document.hwpx'
        (working or (lambda path: fakes.write_package(hwp, path)))(self.wc)
        hwp.path, hwp.PageCount = str(self.wc), 1
        hwp.modified = getattr(hwp, 'initial_modified', False)
        self.input_sha = hashlib.sha256(self.wc.read_bytes()).hexdigest()
        self.external = base / 'external'
        self.external.mkdir()
        old_hook = threading.excepthook
        self.hook = []
        threading.excepthook = lambda args: self.hook.append(args.exc_type.__name__)
        stack.callback(setattr, threading, 'excepthook', old_hook)
        stack.enter_context(patch.dict(sys.modules, {**com_module(), 'pyhwpx': NS(Hwp=lambda: hwp)}))
        stack.enter_context(patch.dict(os.environ, env or {}, clear=False))
        if not env:
            for key in ('HWP_PICTURE_HWPX_PROFILE', 'HWP_PICTURE_HWPX_PROFILE_SHA256'):
                stack.enter_context(patch.dict(os.environ, {key: ''}))
        for name, value in (
            ('create_hwp_instance_with_recovery', lambda Hwp, **kw: Hwp()),
            ('open_document_with_recovery', lambda *, Hwp, hwp, **kw: hwp),
            ('start_runtime_watchdog', lambda *a, **k: (None, None)),
            ('update_runtime_status', lambda *a, **k: None),
            ('current_windows_session_snapshot', lambda: {'interactive': True, 'session_id': 1, 'session_name': 'fake'}),
            ('_native_close_processes', lambda: None),
            ('collect_hwp_snapshot', lambda h: {}),
        ):
            stack.enter_context(patch.object(rt, name, value))
        self.manager = rt.LocalCliRuntimeManager()
        case.addCleanup(self.final_release)
        try:
            self.manager.open_session(session_id=sid, session_root=self.root, working_copy_path=self.wc,
                                      source_filename='document.hwpx')
        finally:
            self.session = self.manager._sessions.get(sid)
        service = object.__new__(LocalCliService)
        service.command_packages = get_command_package_registry()
        service.runtime_manager = self.manager
        service.sessions_root = self.sessions
        store = {'session_id': sid, 'command_generation': 0, 'native_command_sequence': 0,
                 'session_root_path': str(self.root), 'session_root_identity': service._managed_path_identity(self.root),
                 'working_copy_path': str(self.wc), 'source_filename': 'document.hwpx', 'undo_stack': [], 'redo_stack': []}
        self.store = store
        service._load_active_binding = lambda session_id=None, **kw: store
        service._read_binding = lambda session_id=None: dict(store)
        service._require_ready_runtime = lambda label: {}
        service.interactive_sessions = NS(record_command=lambda *a, **k: None)
        service._cleanup_stale_binding = lambda *a, **k: False

        def save(current):
            store.update({k: v for k, v in current.items() if not k.startswith('_')})
            return current

        def update(current, **kw):
            if 'dirty' in kw:
                current['working_copy_dirty'] = kw['dirty']
            return save(current)
        service._save_binding = save
        service._update_live_binding = update
        service._record_local_cli_command = lambda *a, **k: None
        service._validated_artifact_projection = lambda **kw: {}
        service._public_artifacts = lambda **kw: {}
        self.service = service

    def final_release(self):
        """TEST-ONLY settle of the fake owning thread after all assertions; not a production path."""
        session = self.session
        if session is None or not session._thread.is_alive():
            return
        session._allow_native_cleanup = True
        with patch.object(rt.LocalCliLiveSession, '_finalize', lambda *a, **k: None):
            session._commands.put(rt._LiveCommand('test-release', '__close__', None, rt.Future()))
            session._thread.join(2)

    def bundle(self, *steps):
        return self.service.command_bundle(steps=[dict(s) for s in steps], session_id=self.sid)

    def call(self, *steps):
        try:
            return self.bundle(*steps), None
        except rt.LocalCliRuntimeError as exc:
            return None, exc

    def inventory(self):
        out = self.bundle({'op': 'picture_inventory', 'label': 'inventory'})
        self.case.assertTrue(out['ok'], out)
        return out['steps'][0]['result']['pictures']

    def request(self, op, index=0, **fields):
        target = self.inventory()[index]
        return {'op': op, 'label': 'saved', 'target_id': target['target_id'], 'expected_hash': target['hash'],
                'confirm_layout': True, **fields}

    def held(self):
        return self.session._native_custody_held is True

    def section(self, path=None):
        with zipfile.ZipFile(path or self.wc) as archive:
            return archive.read('Contents/section0.xml').decode()


def events(hwp, kind):
    return [e for e in hwp.events if isinstance(e, tuple) and e[0] == kind]


class SavedPackageProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = fakes.profile_env(Path(self.tmp.name))
        from PIL import Image
        self.png = Path(self.tmp.name) / 'replacement.png'
        Image.new('RGB', (300, 150), 'purple').save(self.png)

    def owner(self, hwp=None, *, env=True, profile=None, **kw):
        hwp = hwp if hwp is not None else fakes.PackagePictureHwp(**kw)
        environment = None
        if env:
            environment = fakes.profile_env(Path(tempfile.mkdtemp(dir=self.tmp.name)), profile) if profile else self.env
        return SavedOwner(self, hwp, env=environment)

    # ------------------------------------------------------------------ positives

    def test_layout_edit_verified_save_false_external_capture_and_reopen_bytes(self):
        o = self.owner(size_drift=True)
        out = o.bundle(o.request('picture_layout_exact', mode='square'))
        self.assertTrue(out['ok'], out)
        result = out['steps'][0]['result']
        self.assertEqual(result['preservation_strategy'], 'native_hwpx_package')
        saved = result['saved_proof']
        self.assertEqual(saved['legacy_hwpml_bindata_size'], 'unknown_unqualified_not_compared')
        self.assertFalse(saved['native_qualified'])
        self.assertEqual(saved['qualification'], 'unqualified')
        self.assertEqual(saved['bridge']['method'], 'owpml_physical_ordinal/v1')
        self.assertEqual(saved['regenerated_not_compared'], 1)
        self.assertGreaterEqual(saved['byte_identical'], 10)
        self.assertFalse(saved['private_copy_is_user_save'])
        self.assertEqual(len(events(o.hwp, 'SaveAs')), 2)
        # Our own export cleared the native flag; the logical debt is retained.
        self.assertFalse(o.hwp.modified)
        self.assertTrue(o.session._normal_close_requires_save)
        self.assertEqual(o.store['working_copy_dirty'], True)
        self.assertEqual(hashlib.sha256(o.wc.read_bytes()).hexdigest(), o.input_sha, 'export never wrote the working copy')
        with self.assertRaises(LocalCliServiceError) as refused:
            o.service.close(session_id=o.sid)
        self.assertIn('save_required', str(refused.exception.message))
        saved_out = o.service.save(session_id=o.sid)
        self.assertEqual(saved_out['save_decision'], 'verified_save_false')
        self.assertEqual(events(o.hwp, 'Save'), [('Save', False)])
        proof_out = saved_out['saved_proof']
        self.assertEqual(proof_out['basis'], 'saved_package_equals_verified_private_copy')
        self.assertFalse(o.session._normal_close_requires_save)
        self.assertFalse(o.held())
        self.assertIn('textWrap="SQUARE"', o.section())
        self.assertIn('treatAsChar="0"', o.section())
        # External capture outside the managed root, then static complete re-read.
        external = o.external / 'document-edited.hwpx'
        shutil.copyfile(o.wc, external)
        verdict = proof.verify_external(external, proof_out)
        self.assertTrue(verdict['verified'], verdict)
        damaged = o.external / 'damaged.hwpx'
        with zipfile.ZipFile(external) as src, zipfile.ZipFile(damaged, 'w') as dst:
            for info in src.infolist():
                data = src.read(info)
                dst.writestr(info, data + b' ' if info.filename == 'Scripts/headerScripts' else data)
        self.assertFalse(proof.verify_external(damaged, proof_out)['verified'])

    def test_every_operation_kind_through_real_service(self):
        cases = {'picture_layout_exact': dict(mode='square', horizontal_offset_mm=15.0),
                 'picture_crop_exact': dict(left=.25, top=.25, right=.75, bottom=.75),
                 'picture_replace_exact': None}
        for op, fields in cases.items():
            with self.subTest(op=op):
                o = self.owner(shared=True)
                fields = fields if fields is not None else {'source_path': str(self.png)}
                out = o.bundle(o.request(op, **fields))
                self.assertTrue(out['ok'], out)
                saved = out['steps'][0]['result']['saved_proof']
                if op == 'picture_replace_exact':
                    self.assertEqual(saved['replacement'], 'allocated')
                    self.assertEqual(saved['added_media'], 1)
                    with zipfile.ZipFile(o.wc) as before_file:
                        original = before_file.read('BinData/image1.png')
                self.assertFalse(o.held())
                saved_out = o.service.save(session_id=o.sid)
                self.assertTrue(saved_out['saved_proof']['members'] >= 14)
                with zipfile.ZipFile(o.wc) as archive:
                    names = archive.namelist()
                    if op == 'picture_replace_exact':
                        self.assertEqual(archive.read('BinData/image1.png'), original, 'original/shared asset kept')
                        self.assertIn('BinData/image2.png', names)
                    if op == 'picture_crop_exact':
                        self.assertIn('left="5625"', archive.read('Contents/section0.xml').decode())

    def test_dirty_preserving_export_uses_ordinary_save_and_still_proves_saved_package(self):
        profile = copy.deepcopy(fakes.PROFILE)
        profile['export_native_dirty'] = 'unchanged'
        o = self.owner(profile=profile, export_clears=False)
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        self.assertTrue(o.hwp.modified)
        out = o.service.save(session_id=o.sid)
        self.assertEqual(out['save_decision'], 'ordinary_save')
        self.assertEqual(events(o.hwp, 'Save'), [('Save', True)])
        self.assertEqual(out['saved_proof']['save_decision'], 'ordinary_save')
        self.assertFalse(o.session._normal_close_requires_save)

    def test_noop_request_makes_no_private_copy_or_save_and_keeps_history(self):
        o = self.owner()
        o.store['pending_logical_undo_count'] = 3
        out = o.bundle(o.request('picture_layout_exact', mode='inline'))
        self.assertTrue(out['ok'], out)
        self.assertFalse(out['steps'][0]['dirty'])
        self.assertEqual(events(o.hwp, 'SaveAs'), [])
        self.assertEqual(o.store['pending_logical_undo_count'], 3)
        out = o.service.save(session_id=o.sid)
        self.assertEqual(out['save_decision'], 'clean_noop')
        self.assertIsNone(out['saved_proof'])
        self.assertEqual(events(o.hwp, 'Save'), [])

    def test_inventory_and_101st_binding_reach_saved_proof(self):
        hwp = fakes.PackagePictureHwp()
        parent = next(e for e in hwp.root.iter() if sdk.first(hwp.root, 'picture') in list(e))
        base = sdk.first(hwp.root, 'picture')
        for i in range(1, 101):
            q = copy.deepcopy(base)
            sdk.first(q, 'shapeobject').set('InstId', str(800000 + i))
            sdk.first(q, 'shapecomponent').set('InstID', str(900000 + i))
            parent.append(q)
        hwp.relink()
        o = self.owner(hwp)
        out = o.bundle(o.request('picture_layout_exact', index=100, mode='square'))
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['steps'][0]['result']['saved_proof']['bridge']['ordinal'], 100)
        self.assertEqual(out['steps'][0]['result']['saved_proof']['bridge']['pictures'], 101)

    # ------------------------------------------------------------------ strategy admission

    def test_legacy_route_without_profile_is_unchanged_and_size_drift_still_holds(self):
        o = self.owner(env=False, size_drift=True)
        out, error = o.call(o.request('picture_layout_exact', mode='square'))
        self.assertFalse(out is not None and out.get('ok') is True)
        self.assertTrue(o.held())
        self.assertEqual(events(o.hwp, 'SaveAs'), [], 'no fallback export after the legacy failure')
        self.assertEqual(events(o.hwp, 'Save'), [])

    def test_profile_misconfiguration_refuses_before_any_sdk_call(self):
        bad_pin = fakes.profile_env(Path(tempfile.mkdtemp(dir=self.tmp.name)), pin='0' * 64)
        malformed = copy.deepcopy(fakes.PROFILE)
        malformed['bridge'] = 'instid_equality'
        no_caret = copy.deepcopy(fakes.PROFILE)
        no_caret['caret'] = None
        for name, env in (('pin', bad_pin), ('bridge', fakes.profile_env(Path(tempfile.mkdtemp(dir=self.tmp.name)), malformed)),
                          ('caret', fakes.profile_env(Path(tempfile.mkdtemp(dir=self.tmp.name)), no_caret))):
            with self.subTest(name=name):
                hwp = fakes.PackagePictureHwp()
                o = SavedOwner(self, hwp, env=self.env)
                request = o.request('picture_layout_exact', mode='square')
                before = list(hwp.events)
                with patch.dict(os.environ, env):
                    out = o.bundle(request)
                self.assertFalse(out['ok'])
                self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_proof_unqualified')
                self.assertEqual(hwp.events[len(before):], [], 'refused before any SDK read or effect')
                self.assertFalse(o.held())

    def test_unbridgeable_physical_facts_and_caret_refuse_before_selection(self):
        def wrong_width(entries):
            return [(n, d.replace(b'width="17007"', b'width="17008"', 1) if n == 'Contents/section0.xml' else d) for n, d in entries]

        def wrong_caret(entries):
            return [(n, d.replace(b'pos="32"', b'pos="33"') if n == 'settings.xml' else d) for n, d in entries]
        for name, damage in (('frame', wrong_width), ('caret', wrong_caret)):
            with self.subTest(name=name):
                hwp = fakes.PackagePictureHwp()
                o = self.owner(hwp)
                request = o.request('picture_layout_exact', mode='square')
                original = fakes.members
                fakes_members = lambda fake: damage(original(fake))
                with patch.object(fakes, 'members', fakes_members):
                    out = o.bundle(request)
                self.assertFalse(out['ok'])
                self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_bridge_unproven')
                self.assertFalse(any(str(e).startswith(('Run:SelectCtrlFront', 'Execute', 'Run:Cancel')) for e in hwp.events))
                self.assertFalse(o.held())
                self.assertTrue(o.bundle({'op': 'picture_inventory', 'label': 'still'})['ok'])

    # ------------------------------------------------------------------ package adversaries

    def test_unrequested_package_deltas_after_execute_hold_without_followons(self):
        def edit(member, old, new):
            return lambda entries: [(n, d.replace(old, new, 1) if n == member else d) for n, d in entries]
        damages = {
            'unknown_member': edit('Custom/unknown.bin', b'unknown', b'UNKNOWN'),
            'script': lambda entries: [(n, d + b'//x' if n == 'Scripts/headerScripts' else d) for n, d in entries],
            'shared_media_bytes': lambda entries: [(n, d + b'\x00' if n == 'BinData/image1.png' else d) for n, d in entries],
            'header_style': edit('Contents/header.xml', b'version="1.31"', b'version="1.32"'),
            'unrelated_body_text': edit('Contents/section0.xml', b'Body sentinel', b'BODY SENTINEL'),
            'comment': edit('Contents/section0.xml', b'retained comment', b'changed comment'),
            'extra_member': lambda entries: entries + [('Custom/new.xml', b'<x/>')],
            'removed_member': lambda entries: [(n, d) for n, d in entries if n != 'Preview/PrvText.txt'],
            'reordered_members': lambda entries: entries[:2] + entries[3:4] + entries[2:3] + entries[4:],
            'manifest_metadata': edit('Contents/content.hpf', b'CreatedDate" content="text">2026', b'CreatedDate" content="text">2027'),
            'target_rotation': edit('Contents/section0.xml', b'Angle="0"', b'Angle="30"'),
            'other_picture_frame': lambda entries: [(n, d[::-1].replace(b'"70071"=htdiw', b'"80071"=htdiw', 1)[::-1] if n == 'Contents/section0.xml' else d) for n, d in entries],
            'declaration': edit('Contents/section0.xml', b'standalone="yes"', b'standalone="no"'),
        }
        for name, damage in damages.items():
            with self.subTest(damage=name):
                hwp = fakes.PackagePictureHwp(shared=True)
                hwp.after_execute_package_damage = damage
                o = self.owner(hwp)
                out, error = o.call(o.request('picture_layout_exact', mode='square'))
                self.assertTrue(hwp.executed)
                self.assertFalse(out is not None and out.get('ok') is True, (name, out))
                self.assertTrue(o.held(), name)
                self.assertEqual(events(hwp, 'SaveAs')[-1][0], 'SaveAs')
                self.assertEqual(len(events(hwp, 'SaveAs')), 2, 'no further export after the first failed proof')
                self.assertEqual(events(hwp, 'Save'), [])

    def test_refusal_after_before_copy_keeps_earlier_unsaved_edit_savable_and_proved(self):
        hwp = fakes.PackagePictureHwp()
        o = self.owner(hwp)
        request = o.request('picture_layout_exact', mode='square')

        def earlier_edit(handle):
            next(e for e in hwp.root.iter() if sdk.local(e) == 'char' and e.text).text = 'EARLIER VALID UNSAVED EDIT'
            hwp.modified = True
            return {'ok': True}
        o.manager.execute(session_id=o.sid, command_name='type', handler=earlier_edit)
        original = fakes.members
        # One consistent synthetic serializer whose frame differs from HWPML for the whole session.
        with patch.object(fakes, 'members', lambda fake: [(n, d.replace(b'width="17007"', b'width="17008"', 1) if n == 'Contents/section0.xml' else d) for n, d in original(fake)]):
            out = o.bundle(request)
            self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_bridge_unproven')
            self.assertFalse(hwp.modified, 'our before-copy export cleared the native flag')
            self.assertTrue(o.session._normal_close_requires_save)
            saved = o.service.save(session_id=o.sid)
        self.assertEqual(saved['save_decision'], 'verified_save_false')
        self.assertIn('EARLIER VALID UNSAVED EDIT', o.section())
        self.assertFalse(o.session._normal_close_requires_save)

    def test_container_timestamp_difference_needs_explicit_profile_data(self):
        o = self.owner(vary_time=True)  # profile declares no container field volatile, as observed natively
        out, error = o.call(o.request('picture_layout_exact', mode='square'))
        self.assertFalse(out is not None and out.get('ok') is True)
        self.assertTrue(o.held())

    def test_replacement_rewriting_the_original_member_holds(self):
        def overwrite(entries):
            data = dict(entries)
            return [(n, data['BinData/image2.png'] if n == 'BinData/image1.png' else d) for n, d in entries]
        hwp = fakes.PackagePictureHwp(shared=True)
        hwp.after_execute_package_damage = overwrite
        o = self.owner(hwp)
        out, error = o.call(o.request('picture_replace_exact', source_path=str(self.png)))
        self.assertFalse(out is not None and out.get('ok') is True)
        self.assertTrue(o.held())

    # ------------------------------------------------------------------ save adversaries

    def test_save_false_non_true_return_holds_without_readback_and_keeps_debt(self):
        for value in (False, None, 1):
            with self.subTest(value=value):
                o = self.owner()
                self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
                o.hwp.save_result = value
                with self.assertRaises(LocalCliServiceError) as held:
                    o.service.save(session_id=o.sid)
                self.assertIn('native_custody_held', held.exception.message)
                self.assertTrue(o.held())
                self.assertTrue(o.session._normal_close_requires_save)
                self.assertEqual(events(o.hwp, 'Save'), [('Save', False)])

    def test_saved_file_mismatch_holds_and_keeps_debt(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        o.hwp.saved_package_damage = lambda entries: [(n, d + b'!' if n == 'Custom/unknown.bin' else d) for n, d in entries]
        with self.assertRaises(LocalCliServiceError) as held:
            o.service.save(session_id=o.sid)
        self.assertIn('native_custody_held', held.exception.message)
        self.assertTrue(o.held())
        self.assertTrue(o.session._normal_close_requires_save)

    def test_profile_declared_dirty_effect_must_match_observed_export(self):
        # Clean before the edit, so the first export cannot contradict 'cleared';
        # the dirty after-export keeps the flag and is uncertainty, not success.
        o = self.owner(export_clears=False)
        out, error = o.call(o.request('picture_layout_exact', mode='square'))
        self.assertFalse(out is not None and out.get('ok') is True)
        self.assertTrue(o.held())
        self.assertTrue(o.hwp.executed)
        self.assertEqual(len(events(o.hwp, 'SaveAs')), 2)
        self.assertIsNone(rt._HwpxProofCustody(o.session).current(), 'no current package after the failed export')
        with self.assertRaises(LocalCliServiceError):
            o.service.save(session_id=o.sid)
        self.assertEqual(events(o.hwp, 'Save'), [])

    def test_later_intent_without_a_native_change_saves_through_a_fresh_proved_copy(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        # A later intent-marking command that leaves the native flag False stales the edit's package;
        # the public save then proves a fresh private copy instead of trapping the debt.
        o.manager.execute(session_id=o.sid, command_name='type', handler=lambda handle: {'ok': True})
        out = o.service.save(session_id=o.sid)
        self.assertEqual(out['save_decision'], 'verified_save_false')
        self.assertEqual(out['saved_proof']['baseline'], 'save_fresh_copy')
        self.assertFalse(out['logical_debt_retained'])
        self.assertEqual(events(o.hwp, 'Save'), [('Save', False)])
        self.assertFalse(o.session._normal_close_requires_save)
        self.assertIn('textWrap="SQUARE"', o.section())

    def test_export_clearing_profile_must_declare_a_completing_save(self):
        profile = copy.deepcopy(fakes.PROFILE)
        profile['public_save']['save_false_after_export_clear'] = False
        o = self.owner(profile=profile)
        out = o.bundle(o.request('picture_layout_exact', mode='square'))
        self.assertFalse(out['ok'])
        self.assertEqual(out['steps'][0]['refusal_code'], 'picture_saved_proof_unqualified')
        self.assertEqual(events(o.hwp, 'SaveAs'), [])
        self.assertFalse(o.hwp.executed)

    # ------------------------------------------------------------------ history

    def test_profile_declared_layout_undo_redo_compare_complete_package_states(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        undone = o.service.undo(session_id=o.sid)
        self.assertTrue(undone['ok'], undone)
        self.assertTrue(undone['confirmation']['expected_state']['matches'])
        self.assertEqual(undone['confirmation']['basis'], 'hwpx_private_copy_state_digest')
        self.assertIn('treatAsChar="1"', o.section(sorted(o.root.glob('private-proof/*/*-history.hwpx'))[-1]))
        redone = o.service.redo(session_id=o.sid)
        self.assertTrue(redone['ok'], redone)
        self.assertTrue(redone['confirmation']['expected_state']['matches'])
        # The redone verified package may be saved with the bounded public save.
        saved = o.service.save(session_id=o.sid)
        self.assertEqual(saved['save_decision'], 'verified_save_false')
        self.assertIn('textWrap="SQUARE"', o.section())

    def test_aggregate_picture_history_chains_and_undoes_as_counted_total(self):
        o = self.owner(shared=True)
        first = o.request('picture_layout_exact', index=0, mode='square')
        second = o.request('picture_layout_exact', index=1, mode='square')
        initial = o.inventory()
        out = o.bundle(first, second)
        self.assertTrue(out['ok'], out)
        self.assertEqual(o.store['undo_stack'], [2])
        self.assertEqual(o.store['undo_states'][-1]['edits'], 2)
        undone = o.service.undo(session_id=o.sid)
        self.assertTrue(undone['ok'], undone)
        self.assertEqual(undone['native_undo_steps'], 2)
        self.assertEqual([e for e in o.hwp.events if e == 'Run:Undo'], ['Run:Undo', 'Run:Undo'])
        self.assertEqual([p['hash'] for p in o.inventory()], [p['hash'] for p in initial])
        redone = o.service.redo(session_id=o.sid)
        self.assertTrue(redone['ok'], redone)

    def test_mixed_or_unchained_picture_composition_refuses_history(self):
        o = self.owner(shared=True)
        crop = o.request('picture_crop_exact', index=1, left=.25, top=.25, right=.75, bottom=.75)
        layout = o.request('picture_layout_exact', index=0, mode='square')
        self.assertTrue(o.bundle(layout, crop)['ok'])
        with self.assertRaises(LocalCliUndoRefused) as refused:
            o.service.undo(session_id=o.sid)
        self.assertEqual(refused.exception.code, 'undo_not_counted')
        self.assertNotIn('Run:Undo', o.hwp.events)

    def test_history_after_a_verified_save_restores_debt_despite_its_export_clearing_the_flag(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        self.assertEqual(o.service.save(session_id=o.sid)['save_decision'], 'verified_save_false')
        self.assertFalse(o.session._normal_close_requires_save)
        self.assertTrue(o.service.undo(session_id=o.sid)['ok'])
        self.assertFalse(o.hwp.modified, 'the private history export cleared the native flag')
        self.assertTrue(o.session._normal_close_requires_save, 'an exact-False flag after our own export is no clean evidence')
        with self.assertRaises(LocalCliServiceError) as refused:
            o.service.close(session_id=o.sid)
        self.assertIn('save_required', refused.exception.message)
        self.assertEqual(o.service.save(session_id=o.sid)['save_decision'], 'verified_save_false')
        self.assertIn('treatAsChar="1"', o.section())

    def test_subsequent_edit_after_undo_drops_redo_and_records_its_own_states(self):
        o = self.owner(shared=True)
        self.assertTrue(o.bundle(o.request('picture_layout_exact', index=0, mode='square'))['ok'])
        self.assertTrue(o.service.undo(session_id=o.sid)['ok'])
        self.assertTrue(o.bundle(o.request('picture_layout_exact', index=1, mode='square'))['ok'])
        self.assertEqual(o.store['redo_stack'], [])
        with self.assertRaises(LocalCliUndoRefused) as refused:
            o.service.redo(session_id=o.sid)
        self.assertEqual(refused.exception.code, 'no_redo_available')
        undone = o.service.undo(session_id=o.sid)
        self.assertTrue(undone['ok'], undone)
        self.assertTrue(undone['confirmation']['expected_state']['matches'])

    def test_undeclared_kinds_keep_refusing_before_native_history(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_crop_exact', left=.25, top=.25, right=.75, bottom=.75))['ok'])
        with self.assertRaises(LocalCliUndoRefused) as refused:
            o.service.undo(session_id=o.sid)
        self.assertEqual(refused.exception.code, 'undo_not_counted')
        self.assertEqual([e for e in o.hwp.events if e in ('Run:Undo', 'Run:Redo')], [])

    def test_undo_reaching_a_different_package_state_is_a_named_mismatch(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])

        def damage(fake):
            next(e for e in fake.root.iter() if sdk.local(e) == 'char' and e.text).text = 'UNDO DAMAGE'
        o.hwp.undo_damage = damage
        undone = o.service.undo(session_id=o.sid)
        self.assertFalse(undone['ok'])
        self.assertFalse(undone['confirmation']['expected_state']['matches'])
        # The reached state is an exact private copy; the public save names it as that baseline.
        out = o.service.save(session_id=o.sid)
        self.assertEqual(out['save_decision'], 'verified_save_false')
        self.assertEqual(out['saved_proof']['baseline'], 'history_mismatch_reached')
        self.assertIn('UNDO DAMAGE', o.section())

    def test_changed_profile_refuses_history_before_native_dispatch(self):
        o = self.owner()
        self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
        other = copy.deepcopy(fakes.PROFILE)
        other['profile_id'] = 'synthetic-portable-test/other'
        with patch.dict(os.environ, fakes.profile_env(Path(tempfile.mkdtemp(dir=self.tmp.name)), other)):
            with self.assertRaises(LocalCliUndoRefused) as refused:
                o.service.undo(session_id=o.sid)
        self.assertEqual(refused.exception.code, 'undo_evidence_unavailable')
        self.assertNotIn('Run:Undo', o.hwp.events)

    # ------------------------------------------------------------------ public consumers

    def test_public_cli_bundle_and_verify_saved_consume_the_same_proof(self):
        from local_cli_v1 import main as cli
        o = self.owner()
        request = o.request('picture_layout_exact', mode='square')
        path = Path(self.tmp.name) / 'bundle.json'
        path.write_text(json.dumps({'steps': [request]}))

        def transport(base, route, payload):
            if route == '/local-cli/command-bundle':
                return json.loads(json.dumps(o.service.command_bundle(**payload)))
            raise AssertionError(route)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, 'load_state', return_value={'base_url': 'http://synthetic.invalid', 'session_id': o.sid}), \
                patch.object(cli, 'post_json', side_effect=transport), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli.main(['bundle', '--json', str(path)])
        self.assertEqual(status, 0, stderr.getvalue())
        body = stdout.getvalue()
        self.assertIn('native_hwpx_package', body)
        self.assertIn('unknown_unqualified_not_compared', body)
        self.assertNotIn(str(o.root), body, 'no private paths in public output')
        saved = o.service.save(session_id=o.sid)
        external = o.external / 'out.hwpx'
        shutil.copyfile(o.wc, external)
        proof_file = o.external / 'proof.json'
        proof_file.write_text(json.dumps(saved['saved_proof']))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = cli.main(['verify-saved', str(external), '--proof', str(proof_file), '--json'])
        self.assertEqual(status, 0)
        self.assertTrue(json.loads(stdout.getvalue())['verified'])
        external.write_bytes(external.read_bytes() + b'x')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['verify-saved', str(external), '--proof', str(proof_file), '--json']), 1)

    def test_cli_save_keeps_only_bytes_equal_to_the_proved_saved_package(self):
        from local_cli_v1 import main as cli
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                o = self.owner()
                self.assertTrue(o.bundle(o.request('picture_layout_exact', mode='square'))['ok'])
                destination = o.external / 'captured.hwpx'
                state = {'base_url': 'http://synthetic.invalid', 'session_id': o.sid, 'state_generation': 0}

                def transport(base, route, payload):
                    self.assertEqual(route, '/local-cli/save')
                    return json.loads(json.dumps(o.service.save(**payload)))

                def download(base, route, target):
                    self.assertEqual(route, f'/local-cli/session/{o.sid}/artifact/working-copy')
                    target.write_bytes(o.wc.read_bytes() + (b'corrupt' if corrupt else b''))
                    return target
                recorded = {}
                with patch.object(cli, 'load_state', return_value=state), \
                        patch.object(cli, '_state_cas_snapshot', return_value=(state, 0, o.sid)), \
                        patch.object(cli, 'update_state', side_effect=lambda fn, **kw: recorded.update(fn(dict(state)))), \
                        patch.object(cli, 'post_json', side_effect=transport), patch.object(cli, 'download_to_path', side_effect=download), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    status = cli.main(['save', '--out', str(destination)])
                if corrupt:
                    self.assertNotEqual(status, 0)
                    self.assertFalse(destination.exists())
                    self.assertEqual(list(o.external.iterdir()), [], 'no partial or unverified file is left')
                else:
                    self.assertEqual(status, 0)
                    self.assertEqual(destination.read_bytes(), o.wc.read_bytes())
                    self.assertEqual(recorded['last_saved_proof']['saved_package_sha256'], hashlib.sha256(o.wc.read_bytes()).hexdigest())


class ComparatorDeltas(unittest.TestCase):
    """The complete comparator itself names each unrequested delta (no SDK, no service)."""

    T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)

    def pair(self, damage, profile=None, *, after_clock=2, windows=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hwp = fakes.PackagePictureHwp(shared=True)
        before_path, after_path = Path(tmp.name) / 'before.hwpx', Path(tmp.name) / 'after.hwpx'
        times = [self.T0, self.T0 + timedelta(seconds=after_clock)]
        hwp.clock = lambda: times[0]
        fakes.write_package(hwp, before_path)
        hwp.clock = lambda: times[1]
        fakes.write_package(hwp, after_path, damage=damage)
        data = profile or fakes.PROFILE
        loaded = proof.Profile(proof.validate_profile(copy.deepcopy(data)), 'f' * 64, 'operator_configured_file')
        around = lambda t: (t - timedelta(milliseconds=100), t + timedelta(milliseconds=100))
        return (proof.read_state(before_path, window=around(self.T0) if windows else None),
                proof.read_state(after_path, window=around(self.T0 + timedelta(seconds=2)) if windows else None), loaded)

    def kinds(self, damage, profile=None, *, rendering=False, **kw):
        before, after, loaded = self.pair(damage, profile, **kw)
        try:
            proof.compare(before, after, proof.Expectation({}, rendering_request=rendering), loaded)
        except proof.PackageMismatch as exc:
            return {d['kind'] for d in exc.deltas}
        return set()

    def test_healthy_noop_serialization_passes_only_through_declared_allowances(self):
        self.assertEqual(self.kinds(None), set())
        strict = copy.deepcopy(fakes.PROFILE)
        strict.update(volatile_values=[])
        self.assertEqual(self.kinds(None, strict), {'bytes'})  # content.hpf ModifiedDate only

    def test_typed_modified_date_is_bound_to_each_write_window(self):
        self.assertEqual(self.kinds(None, windows=False), {'volatile_window'})
        outside = lambda entries: [(n, re.sub(rb'ModifiedDate" content="text">[^<]*<', b'ModifiedDate" content="text">2026-11-05T12:00:09Z<', d)
                                    if n == 'Contents/content.hpf' else d) for n, d in entries]
        self.assertEqual(self.kinds(outside), {'volatile_window'})
        malformed = lambda entries: [(n, re.sub(rb'ModifiedDate" content="text">[^<]*<', b'ModifiedDate" content="text">2026-11-05 12:00:02<', d)
                                      if n == 'Contents/content.hpf' else d) for n, d in entries]
        self.assertEqual(self.kinds(malformed), {'volatile_type'})
        local = lambda entries: [(n, re.sub(rb'ModifiedDate" content="text">[^<]*<', b'ModifiedDate" content="text">2026-11-05T21:00:02+09:00<', d)
                                  if n == 'Contents/content.hpf' else d) for n, d in entries]
        self.assertEqual(self.kinds(local), {'volatile_type'})
        before, after, loaded = self.pair(None)
        with self.assertRaises(proof.PackageMismatch) as reversed_order:
            proof.compare(after, before, proof.Expectation({}), loaded)
        self.assertEqual({d['kind'] for d in reversed_order.exception.deltas}, {'volatile_order'})

    def test_other_metadata_values_are_never_volatile(self):
        def meta(old, new):
            return lambda entries: [(n, d.replace(old, new, 1) if n == 'Contents/content.hpf' else d) for n, d in entries]
        self.assertEqual(self.kinds(meta(b'CreatedDate" content="text">2026-11-05T00:00:00Z', b'CreatedDate" content="text">2026-11-05T12:00:02Z')), {'xml_delta'})

    def test_preview_regeneration_is_request_bound_and_typed(self):
        changed = lambda entries: [(n, fakes._png(7) if n == 'Preview/PrvImage.png' else d) for n, d in entries]
        self.assertEqual(self.kinds(changed), {'bytes'}, 'no rendering request: previews must be identical')
        self.assertEqual(self.kinds(changed, rendering=True), set())
        invalid = lambda entries: [(n, b'not a png' if n == 'Preview/PrvImage.png' else d) for n, d in entries]
        self.assertEqual(self.kinds(invalid, rendering=True), {'regenerated_invalid'})

        def other_size(entries):
            from PIL import Image
            data = io.BytesIO()
            Image.new('RGB', (5, 3), 'red').save(data, format='PNG')
            return [(n, data.getvalue() if n == 'Preview/PrvImage.png' else d) for n, d in entries]
        self.assertEqual(self.kinds(other_size, rendering=True), {'regenerated_pixel_size'})
        text = lambda entries: [(n, d + b'x' if n == 'Preview/PrvText.txt' else d) for n, d in entries]
        self.assertEqual(self.kinds(text, rendering=True), {'bytes'}, 'text preview is not regenerated representation')

    def test_container_fields_need_explicit_profile_data(self):
        def zip_time(entries):
            return entries
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        hwp = fakes.PackagePictureHwp(vary_time=True)
        a, b = Path(tmp.name) / 'a.hwpx', Path(tmp.name) / 'b.hwpx'
        hwp.clock = lambda: self.T0
        fakes.write_package(hwp, a)
        fakes.write_package(hwp, b)
        window = (self.T0 - timedelta(seconds=1), self.T0 + timedelta(seconds=1))
        loaded = proof.Profile(proof.validate_profile(copy.deepcopy(fakes.PROFILE)), 'f' * 64, 'x')
        with self.assertRaises(proof.PackageMismatch) as held:
            proof.compare(proof.read_state(a, window=window), proof.read_state(b, window=window), proof.Expectation({}), loaded)
        self.assertEqual({d['kind'] for d in held.exception.deltas}, {'container'})

    def test_each_unrequested_delta_kind_is_named(self):
        def edit(member, old, new):
            return lambda entries: [(n, d.replace(old, new, 1) if n == member else d) for n, d in entries]
        cases = {
            'bytes': edit('Custom/unknown.bin', b'unknown', b'UNKNOWN'),
            'member_set_or_order': lambda entries: entries + [('Custom/new.xml', b'<x/>')],
            'xml_declaration': edit('Contents/content.hpf', b'standalone="yes"', b'standalone="no"'),
            'xml_delta': edit('Contents/content.hpf', b'id="header"', b'id="header2"'),
            'namespace_declarations': edit('Contents/content.hpf', b'xmlns:opf=', b'xmlns:opf2="urn:x" xmlns:opf='),
        }
        for kind, damage in cases.items():
            with self.subTest(kind=kind):
                self.assertIn(kind, self.kinds(damage))

    def test_semantic_digest_masks_only_profile_allowances(self):
        before, after, loaded = self.pair(None)
        self.assertEqual(proof.state_digest(before, loaded), proof.state_digest(after, loaded))
        before, after, loaded = self.pair(lambda entries: [(n, d + b' ' if n == 'Scripts/headerScripts' else d) for n, d in entries])
        self.assertNotEqual(proof.state_digest(before, loaded), proof.state_digest(after, loaded))
        before, after, loaded = self.pair(lambda entries: [(n, fakes._png(9) if n == 'Preview/PrvImage.png' else d) for n, d in entries])
        self.assertNotEqual(proof.state_digest(before, loaded), proof.state_digest(after, loaded), 'previews are compared in history states')


class ProfileContract(unittest.TestCase):
    def test_profile_schema_is_exact(self):
        self.assertEqual(proof.validate_profile(copy.deepcopy(fakes.PROFILE))['profile_id'], 'synthetic-portable-test/v2')
        mutations = {
            'extra_key': lambda p: p.update(extra=1),
            'v1_schema': lambda p: p.update(schema='hwpx-saved-proof-profile/v1'),
            'qualified_without_evidence': lambda p: p['qualification'].update(status='qualified'),
            'other_args': lambda p: p['materialization'].update(args='export:true'),
            'body_volatile': lambda p: p['volatile_values'].append({'member': 'Contents/section0.xml', 'element': '{u}t', 'where': {'a': 'b'},
                                                                     'field': 'text', 'value_type': 'utc_second_z', 'bound': 'native_write_window'}),
            'untyped_volatile': lambda p: p['volatile_values'][0].pop('value_type'),
            'unbounded_volatile': lambda p: p['volatile_values'][0].update(bound='any'),
            'media_regenerated': lambda p: p['regenerated_members'].update({'BinData/image1.png': {'kind': 'png', 'when': 'rendering_request', 'same_pixel_size': True}}),
            'unconditional_regeneration': lambda p: p['regenerated_members']['Preview/PrvImage.png'].update(when='always'),
            'unknown_container_field': lambda p: p['container_volatile'].append('crc'),
            'caret_without_domain': lambda p: p['caret'].pop('observed_domain'),
            'caret_negative_domain': lambda p: p['caret']['observed_domain'].update(Para=[-1]),
            'replacement_without_href': lambda p: p.update(replacement_manifest_attributes=['id', 'media-type']),
            'history_bool': lambda p: p['history'].update(picture_crop_exact={'undo_native_steps': True, 'redo_native_steps': 1}),
            'non_picture_history': lambda p: p['history'].update(table_edit_exact={'undo_native_steps': 1, 'redo_native_steps': 1}),
            'missing_evidence_field': lambda p: p['evidence'].pop('caret'),
            'evidence_self_qualified': lambda p: p['evidence']['caret'].update(status='qualified'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = copy.deepcopy(fakes.PROFILE)
                mutate(data)
                with self.assertRaises(proof.ProofUnavailable):
                    proof.validate_profile(data)

    def test_native_qualified_needs_qualified_profile_and_observed_used_fields(self):
        data = copy.deepcopy(fakes.PROFILE)
        loaded = proof.Profile(proof.validate_profile(data), 'f' * 64, 'x')
        self.assertFalse(loaded.native_qualified(proof.EDIT_EVIDENCE))
        data['qualification'].update(status='qualified', evidence_sha256='a' * 64)
        for row in data['evidence'].values():
            row['status'] = 'observed'
        data['evidence']['public_save']['status'] = 'not_run'
        loaded = proof.Profile(proof.validate_profile(data), 'f' * 64, 'x')
        self.assertTrue(loaded.native_qualified(proof.EDIT_EVIDENCE))
        self.assertFalse(loaded.native_qualified(proof.SAVE_EVIDENCE + ('public_save',)))
        self.assertEqual(loaded.not_observed(proof.SAVE_EVIDENCE + ('public_save',)), ['public_save'])

    def test_unconfigured_profile_selects_legacy(self):
        self.assertIsNone(proof.load_profile({}))
        with self.assertRaises(proof.ProofUnavailable):
            proof.load_profile({'HWP_PICTURE_HWPX_PROFILE': '/x.json'})


if __name__ == '__main__':
    unittest.main()
