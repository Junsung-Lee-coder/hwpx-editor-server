from __future__ import annotations

import hashlib
import importlib.util
import json
import asyncio
import multiprocessing
import tempfile
import threading
import unittest
from concurrent.futures import Future
from queue import Queue
from pathlib import Path

from app.interactive_session_manager import (
    MAX_INTERACTIVE_EVENT_FILE_BYTES,
    MAX_INTERACTIVE_EVENT_FILES,
    InteractiveSessionManager,
    _append_jsonl,
    _bounded_command_history_payload,
    _bounded_history_value,
)
from app.local_cli_runtime import (
    LocalCliLiveSession,
    LocalCliRuntimeTimeoutError,
    _LiveCommand,
    _bounded_command_result,
)
from app.local_cli_service import LocalCliService, LocalCliServiceError
from local_cli_v1.proof_packet import (
    ProofPacketError,
    _validate_native_border_readback,
    _copy_artifact,
    build_proof_packet,
    seal_native_border_readback,
)
from local_cli_v1.state import StatePersistenceError, clear_session_binding, load_state, update_state


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"g22_{name.replace('.', '_')}", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commit_new_session_from_process(path_text: str) -> None:
    path = Path(path_text)
    update_state(
        lambda state: {**state, 'session_id': 'new-session'},
        path,
        expected_generation=1,
        expected_session_id='old-session',
    )


class G22StateRepairTests(unittest.TestCase):
    def test_stale_clear_requires_generation_and_session_compare_and_swap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            old = update_state(lambda _: {"session_id": "old-session"}, path)
            opened = update_state(
                lambda state: {**state, "session_id": "new-session"},
                path,
                expected_generation=old["state_generation"],
                expected_session_id="old-session",
            )

            with self.assertRaises(StatePersistenceError):
                clear_session_binding(
                    path,
                    expected_generation=old["state_generation"],
                    expected_session_id=old["session_id"],
                )

            final = load_state(path)
            self.assertEqual(final["session_id"], opened["session_id"])
            self.assertEqual(final["state_generation"], opened["state_generation"])

    def test_stale_clear_is_rejected_across_processes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'state.json'
            old = update_state(lambda _: {'session_id': 'old-session'}, path)
            self.assertEqual(old['state_generation'], 1)
            context = multiprocessing.get_context('spawn')
            process = context.Process(target=_commit_new_session_from_process, args=(str(path),))
            process.start()
            process.join(timeout=5)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            with self.assertRaises(StatePersistenceError):
                clear_session_binding(
                    path,
                    expected_generation=old['state_generation'],
                    expected_session_id=old['session_id'],
                )
            self.assertEqual(load_state(path)['session_id'], 'new-session')

    def test_unconditional_clear_refuses_to_delete_an_active_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            update_state(lambda _: {"session_id": "active"}, path)
            with self.assertRaises(StatePersistenceError):
                clear_session_binding(path)
            self.assertEqual(load_state(path)["session_id"], "active")

    def test_artifact_and_find_state_writes_use_generation_and_session_cas(self) -> None:
        main = (ROOT / 'local_cli_v1' / 'main.py').read_text(encoding='utf-8')
        for expected_session in (
            'border_expected_session',
            'find_expected_session',
            'save_expected_session',
            'working_copy_expected_session',
            'screenshot_expected_session',
            'export_expected_session',
        ):
            self.assertIn(f'expected_session_id={expected_session}', main)
        self.assertNotIn("update_state(lambda current: {**current, 'last_find_query': args.text})", main)


class G22SourceBundleRepairTests(unittest.TestCase):
    def test_builder_and_receiver_share_the_same_private_member_policy(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        policy = load_script("source_bundle_policy.py")
        for name in (
            'PRIVATE.HWP', 'nested/screenshot.PNG', 'id_ed25519',
            'credentials.json', 'runtime/cache.db', 'payload.zip',
        ):
            self.assertTrue(policy.is_prohibited_member(name))
            self.assertTrue(builder.is_prohibited_member(name))
            self.assertTrue(verifier.is_prohibited_member(name))
        self.assertFalse(policy.is_prohibited_member('app/command/manifest.json'))

    def test_gitless_builder_excludes_private_payload_extensions(self) -> None:
        builder = load_script("build_source_bundle.py")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "source"
            root.mkdir()
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            for name in ("fixture.hwp", "private.pdf", "screenshot.png", "id_ed25519", "credentials.json"):
                (root / name).write_bytes(b"private")
            manifest = builder.build_source_bundle(
                source_root=root,
                archive_path=Path(raw) / "source.zip",
                manifest_path=Path(raw) / "source-manifest.json",
                repository="r",
                commit="c",
                tree="t",
            )
            self.assertEqual([item["path"] for item in manifest["files"]], ["main.py"])

    def test_gitless_verification_requires_independent_archive_binding(self) -> None:
        builder = load_script("build_source_bundle.py")
        verifier = load_script("verify_source_bundle.py")
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "source"
            root.mkdir()
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            archive = Path(raw) / "source.zip"
            manifest_path = Path(raw) / "source-manifest.json"
            builder.build_source_bundle(
                source_root=root,
                archive_path=archive,
                manifest_path=manifest_path,
                repository="r",
                commit="c",
                tree="t",
            )
            kwargs = {
                "archive_path": archive,
                "manifest_path": manifest_path,
                "destination": Path(raw) / "extract",
                "expected_repository": "r",
                "expected_commit": "c",
                "expected_tree": "t",
            }
            with self.assertRaisesRegex(verifier.SourceBundleVerificationError, "archive"):
                verifier.verify_source_bundle(**kwargs)
            verifier.verify_source_bundle(
                **kwargs,
                expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                expected_archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            )


class G22ProofRepairTests(unittest.TestCase):
    def test_proof_packet_refuses_a_symlinked_destination_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source.txt'
            outside = root / 'outside.txt'
            destination = root / 'destination.txt'
            source.write_text('source', encoding='utf-8')
            outside.write_text('outside', encoding='utf-8')
            destination.symlink_to(outside)
            with self.assertRaises(ProofPacketError):
                _copy_artifact(
                    source=source,
                    destination=destination,
                )
            self.assertEqual(outside.read_text(encoding='utf-8'), 'outside')

    def test_sealed_border_readback_bounds_target_identity_as_well_as_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(Exception, 'oversized|deeply nested|field'):
                seal_native_border_readback(
                    destination=Path(raw) / 'border.json',
                    candidate_generation='candidate-1',
                    source_manifest_sha256='a' * 64,
                    target_identity={'cell': 'x' * 257},
                    pre_quit_readback={'left': 'thin'},
                    persisted_readback={'left': 'thin'},
                )

    def test_contradictory_border_readbacks_are_not_valid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "border.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "local-cli/native-border-readback/v1",
                        "candidate_generation": "candidate-1",
                        "source_manifest_sha256": "manifest-1",
                        "target_identity": {"cell": "A1"},
                        "pre_quit_readback": {"left": "thin", "right": "thin"},
                        "persisted_readback": {"left": "thin", "right": "double"},
                    }
                ),
                encoding="utf-8",
            )
            valid, reason = _validate_native_border_readback(
                path,
                state={
                    "candidate_generation": "candidate-1",
                    "source_manifest_sha256": "manifest-1",
                    "target_identity": {"cell": "A1"},
                },
                proof_binding={
                    "candidate_generation": "candidate-1",
                    "source_manifest_sha256": "manifest-1",
                    "target_identity": {"cell": "A1"},
                },
            )
            self.assertFalse(valid)
            self.assertIn("match", reason.lower())

    def test_border_readback_comparison_distinguishes_boolean_and_numeric_values(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "border.json"
            path.write_text(
                json.dumps({
                    "schema_version": "local-cli/native-border-readback/v1",
                    "candidate_generation": "candidate-1",
                    "source_manifest_sha256": "manifest-1",
                    "target_identity": {"cell": "A1"},
                    "pre_quit_readback": {"left": True},
                    "persisted_readback": {"left": 1},
                }),
                encoding="utf-8",
            )
            valid, _reason = _validate_native_border_readback(
                path,
                state={
                    "candidate_generation": "candidate-1",
                    "source_manifest_sha256": "manifest-1",
                    "target_identity": {"cell": "A1"},
                },
                proof_binding={
                    "candidate_generation": "candidate-1",
                    "source_manifest_sha256": "manifest-1",
                    "target_identity": {"cell": "A1"},
                },
            )
            self.assertFalse(valid)

    def test_unbound_candidate_labels_cannot_make_replacement_bytes_delivery_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            working = root / "replacement.hwp"
            proof = root / "replacement.png"
            working.write_bytes(b"replacement")
            proof.write_bytes(b"proof")
            packet = build_proof_packet(
                out_dir=root / "packet",
                state={
                    "session_id": "s1",
                    "candidate_generation": "trusted-looking-label",
                    "repository": "github:example/repo",
                    "commit": "a" * 40,
                    "tree": "b" * 40,
                    "source_manifest_sha256": "c" * 64,
                    "last_saved_working_copy_path": str(working),
                    "last_page_screenshot_path": str(proof),
                },
            )
            self.assertFalse(packet["delivery_ready"])
            self.assertIn("proof generation", packet["delivery_ready_reason"])


class G22DependencyRepairTests(unittest.TestCase):
    def test_pywin32_mapping_covers_all_production_import_surfaces(self) -> None:
        checker = load_script("check_windows_dependencies.py")
        modules = set(checker._DISTRIBUTION_IMPORTS["pywin32"])
        self.assertTrue({"pythoncom", "win32gui", "win32ui", "win32process", "win32con"}.issubset(modules))

    def test_each_pywin32_import_failure_is_reported_individually(self) -> None:
        checker = load_script("check_windows_dependencies.py")
        with tempfile.TemporaryDirectory() as raw:
            lock = Path(raw) / 'requirements.txt'
            lock.write_text('pywin32==308\n', encoding='utf-8')

            def importer(module: str) -> object:
                raise ImportError(module)

            report = checker.verify_dependencies(
                lock,
                version_lookup=lambda _name: '308',
                importer=importer,
            )
        self.assertFalse(report['ok'])
        self.assertEqual(
            set(report['import_failures']),
            {'pythoncom', 'win32com', 'win32gui', 'win32ui', 'win32process', 'win32con'},
        )


class G22HistoryRepairTests(unittest.TestCase):
    def test_command_history_uses_an_allowlist_for_opaque_payloads(self) -> None:
        bounded = _bounded_command_history_payload(
            'find',
            {
                'candidate_count': 2,
                'opaque_document_payload': {'body': 'private document text'},
                'query': 'private query text',
            },
        )
        serialized = json.dumps(bounded)
        self.assertEqual(bounded['candidate_count'], 2)
        self.assertNotIn('private document text', serialized)
        self.assertNotIn('private query text', serialized)
        self.assertIn('omitted', serialized)

    def test_history_redacts_document_content_and_local_paths(self) -> None:
        bounded = _bounded_history_value({
            "selected_text": "private document content",
            "document_path": "/home/user/private.hwpx",
            "safe_count": 3,
        })
        self.assertNotIn("private document content", json.dumps(bounded))
        self.assertNotIn("/home/user/private.hwpx", json.dumps(bounded))
        self.assertEqual(bounded["safe_count"], 3)

    def test_event_history_is_size_bounded_and_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'session_events.jsonl'
            for index in range(20):
                _append_jsonl(path, {'command': 'status', 'payload': {'safe': index, 'text': 'x' * 4000}})
            event_files = [path, *[path.with_name(f'{path.name}.{index}') for index in range(1, MAX_INTERACTIVE_EVENT_FILES + 1)]]
            existing = [candidate for candidate in event_files if candidate.exists()]
            self.assertLessEqual(len(existing), MAX_INTERACTIVE_EVENT_FILES + 1)
            self.assertTrue(all(candidate.stat().st_size <= MAX_INTERACTIVE_EVENT_FILE_BYTES for candidate in existing))

    def test_central_retention_ledger_deduplicates_expired_entries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            manager = object.__new__(InteractiveSessionManager)
            manager.settings = type('Settings', (), {
                'spool_root': Path(raw),
                'retention_days': 7,
            })()
            session_id = 's1'
            manager.sessions_root.mkdir(parents=True, exist_ok=True)
            manager.session_dir(session_id).mkdir(parents=True, exist_ok=True)
            entry = {
                'step': 'verify-post',
                'recorded_at': '2026-01-01T00:00:00+00:00',
                'pruned_at': '2026-01-02T00:00:00+00:00',
                'expired_at': '2026-01-08T00:00:00+00:00',
                'reason': 'terminal_session_expired',
                'anchor_field': 'closed_at',
                'anchor_at': '2026-01-01T00:00:00+00:00',
            }
            manager._save_verify_evidence_retention_ledger(
                session_id,
                entries=[entry],
                swept_at='2026-01-02T00:00:00+00:00',
            )
            manager._merge_retention_ledger_to_central(session_id)
            manager._merge_retention_ledger_to_central(session_id)
            central = json.loads(manager.retention_ledger_path.read_text(encoding='utf-8'))
            self.assertEqual(len(central['entries']), 1)
            self.assertEqual(central['entries'][0]['step'], 'verify-post')

    def test_failure_history_redacts_paths_and_secret_assignments(self) -> None:
        manager = object.__new__(InteractiveSessionManager)
        failure = manager._normalize_failure_reason(
            'failed at /home/user/private.hwpx password=do-not-store',
            command='replace',
        )
        serialized = json.dumps(failure)
        self.assertNotIn('/home/user/private.hwpx', serialized)
        self.assertNotIn('do-not-store', serialized)


class G22HealthProbeRepairTests(unittest.TestCase):
    def test_probe_timeout_projects_pending_command_before_cleanup(self) -> None:
        class Runtime:
            def has_session(self, _session_id: str) -> bool:
                return True

            def execute(self, **_kwargs):
                raise LocalCliRuntimeTimeoutError(
                    'probe timed out',
                    command_id='s1:command-1',
                    command_state='timed_out_pending_reconciliation',
                )

            def command_status(self, _session_id: str, _command_id: str | None = None):
                return {
                    'command_id': 's1:command-1',
                    'sequence': 4,
                    'state': 'timed_out_pending_reconciliation',
                }

        service = object.__new__(LocalCliService)
        service.runtime_manager = Runtime()
        service._save_binding = lambda value: setattr(service, '_saved_binding', dict(value))
        service.interactive_sessions = type('Sessions', (), {
            'record_command': lambda *_args, **_kwargs: None,
        })()
        service._cleanup_stale_binding = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('timeout must not be treated as stale cleanup')
        )
        binding = {'session_id': 's1', 'native_command_sequence': 3}

        self.assertFalse(service._probe_live_binding(binding))
        self.assertEqual(service._saved_binding['pending_command']['command_id'], 's1:command-1')
        self.assertEqual(service._saved_binding['document_session_state'], 'timed_out_pending_reconciliation')

    def test_status_rechecks_pending_projection_after_probe_timeout(self) -> None:
        source = (ROOT / 'app' / 'local_cli_service.py').read_text(encoding='utf-8')
        status = source[source.index('    def status(self)') : source.index('\n\n', source.index('    def status(self)'))]
        self.assertIn('pending_reconciliation = self._command_status_for_binding(active_binding)', status)
        self.assertIn('_binding_has_pending_reconciliation(active_binding)', status)

    def test_status_keeps_closed_binding_visible_until_cleanup_is_verified(self) -> None:
        source = (ROOT / 'app' / 'local_cli_service.py').read_text(encoding='utf-8')
        status = source[source.index('    def status(self)') : source.index('\n\n', source.index('    def status(self)'))]
        self.assertIn("document_session_state'] = 'closed_cleanup_pending'", status)
        self.assertIn('retry close cleanup', source)

    def test_stale_cleanup_does_not_delete_root_when_runtime_close_is_uncertain(self) -> None:
        service = object.__new__(LocalCliService)
        service._closed_session_ids = set()
        service._closed_session_ids_lock = threading.Lock()
        service.runtime_manager = type('Runtime', (), {
            'close_session': lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('close timed out')),
        })()
        calls = []
        service._cleanup_managed_session_root = lambda _binding: calls.append(_binding) or {'removed': True}
        service._record_session_close = lambda **_kwargs: calls.append('recorded')
        service._clear_binding = lambda **_kwargs: calls.append('cleared')
        binding = {'session_id': 's1'}

        service._cleanup_stale_binding(binding)

        self.assertEqual(calls, [])


class G22ManagedFixtureCleanupTests(unittest.TestCase):
    def test_close_cleanup_removes_and_verifies_server_managed_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'sessions'
            session_root = root / 's1'
            (session_root / 'working').mkdir(parents=True)
            (session_root / 'working' / 'working-copy.hwpx').write_bytes(b'fixture')
            service = object.__new__(LocalCliService)
            service.sessions_root = root
            binding = {
                'session_id': 's1',
                'session_root_path': str(session_root),
                'session_root_identity': service._managed_path_identity(session_root),
            }

            result = service._cleanup_managed_session_root(binding)

            self.assertTrue(result['removed'])
            self.assertFalse(session_root.exists())

    def test_close_removes_managed_session_root_before_clearing_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'local_cli_v1'
            sessions_root = root / 'sessions'
            session_root = sessions_root / 's1'
            (session_root / 'working').mkdir(parents=True)
            (session_root / 'working' / 'working-copy.hwpx').write_bytes(b'fixture')
            service = object.__new__(LocalCliService)
            service.root = root
            service.sessions_root = sessions_root
            service.active_binding_path = root / 'active_binding.json'
            service._closed_session_ids = set()
            service._closed_session_ids_lock = threading.Lock()
            service.runtime_manager = type('Runtime', (), {
                'close_session': lambda _self, _session_id: None,
            })()
            service.interactive_sessions = type('Interactive', (), {
                'record_command': lambda *_args, **_kwargs: None,
            })()
            binding = {
                'session_id': 's1',
                'session_root_path': str(session_root),
                'session_root_identity': service._managed_path_identity(session_root),
                'command_generation': 0,
                'native_command_sequence': 0,
            }
            service._write_json(service._binding_path('s1'), binding)
            service._write_json(service.active_binding_path, binding)

            result = service.close(session_id='s1')

            self.assertTrue(result['ok'])
            self.assertTrue(result['cleanup']['removed'])
            self.assertFalse(session_root.exists())
            self.assertIsNone(service._read_json(service._binding_path('s1')))
            self.assertIsNone(service._read_json(service.active_binding_path))

    def test_close_retains_binding_when_managed_root_identity_cannot_be_proven(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'local_cli_v1'
            sessions_root = root / 'sessions'
            session_root = sessions_root / 's1'
            session_root.mkdir(parents=True)
            service = object.__new__(LocalCliService)
            service.root = root
            service.sessions_root = sessions_root
            service.active_binding_path = root / 'active_binding.json'
            service._closed_session_ids = set()
            service._closed_session_ids_lock = threading.Lock()
            service.runtime_manager = type('Runtime', (), {
                'close_session': lambda _self, _session_id: None,
            })()
            service.interactive_sessions = type('Interactive', (), {
                'record_command': lambda *_args, **_kwargs: None,
            })()
            binding = {
                'session_id': 's1',
                'session_root_path': str(session_root),
                'session_root_identity': {'device': 1, 'inode': 2, 'mode': 0o40700},
                'command_generation': 0,
                'native_command_sequence': 0,
            }
            service._write_json(service._binding_path('s1'), binding)
            service._write_json(service.active_binding_path, binding)

            with self.assertRaisesRegex(LocalCliServiceError, 'identity changed'):
                service.close(session_id='s1')

            self.assertTrue(session_root.exists())
            self.assertIsNotNone(service._read_json(service._binding_path('s1')))
            self.assertIsNotNone(service._read_json(service.active_binding_path))

    def test_close_cleanup_does_not_treat_unexpectedly_missing_root_as_verified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / 'local_cli_v1'
            sessions_root = root / 'sessions'
            session_root = sessions_root / 's1'
            session_root.mkdir(parents=True)
            service = object.__new__(LocalCliService)
            service.sessions_root = sessions_root
            binding = {
                'session_id': 's1',
                'session_root_path': str(session_root),
                'session_root_identity': service._managed_path_identity(session_root),
            }
            session_root.rmdir()

            with self.assertRaisesRegex(LocalCliServiceError, 'identity|verified|missing'):
                service._cleanup_managed_session_root(binding)

    def test_cleanup_rejects_nested_root_even_when_it_is_inside_session_store(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            sessions_root = Path(raw) / 'sessions'
            session_root = sessions_root / 'nested' / 's1'
            session_root.mkdir(parents=True)
            service = object.__new__(LocalCliService)
            service.sessions_root = sessions_root
            binding = {
                'session_id': 's1',
                'session_root_path': str(session_root),
                'session_root_identity': service._managed_path_identity(session_root),
            }
            with self.assertRaisesRegex(LocalCliServiceError, 'child directory'):
                service._cleanup_managed_session_root(binding)
            self.assertTrue(session_root.exists())

    def test_cleanup_rejects_symlinked_session_store_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            real_sessions = base / 'real' / 'sessions'
            real_session = real_sessions / 's1'
            real_session.mkdir(parents=True)
            alias_parent = base / 'alias-parent'
            alias_parent.symlink_to(base / 'real', target_is_directory=True)
            sessions_root = alias_parent / 'sessions'
            service = object.__new__(LocalCliService)
            service.sessions_root = sessions_root
            binding = {
                'session_id': 's1',
                'session_root_path': str(sessions_root / 's1'),
                'session_root_identity': service._managed_path_identity(real_session),
            }

            with self.assertRaisesRegex(LocalCliServiceError, 'symlink|reparse|child directory'):
                service._cleanup_managed_session_root(binding)
            self.assertTrue(real_session.exists())

    def test_open_failure_keeps_root_when_runtime_close_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            service = object.__new__(LocalCliService)
            service.settings = type('Settings', (), {
                'spool_root': Path(raw) / 'spool',
                'allowed_extensions_list': ['.hwpx'],
                'max_upload_mb': 1,
            })()
            service.sessions_root = service.settings.spool_root / 'interactive_sessions'
            service._require_ready_runtime = lambda _operation: None
            service._read_binding = lambda **_kwargs: None
            calls = []

            class Upload:
                filename = 'document.hwpx'
                content_type = 'application/octet-stream'

                def __init__(self) -> None:
                    self.chunks = [b'document', b'']

                async def read(self, _size: int) -> bytes:
                    return self.chunks.pop(0)

                async def close(self) -> None:
                    return None

            service.interactive_sessions = type('Interactive', (), {
                'open_session': lambda *_args, **_kwargs: {'session_id': _kwargs['session_id']},
            })()
            service.runtime_manager = type('Runtime', (), {
                'open_session': lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('native startup failed')),
                'close_session': lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('native close timed out')),
            })()
            service._cleanup_managed_session_root = lambda binding: calls.append(('cleanup', binding)) or {'removed': True}
            service._record_session_close = lambda **_kwargs: calls.append(('record', _kwargs))
            service._clear_binding = lambda **_kwargs: calls.append(('clear', _kwargs))

            with self.assertRaisesRegex(RuntimeError, 'native startup failed'):
                asyncio.run(service.open_upload(file=Upload()))
            self.assertEqual(calls, [])

    def test_binding_session_id_rejects_path_components(self) -> None:
        service = object.__new__(LocalCliService)
        with self.assertRaisesRegex(LocalCliServiceError, 'session_id'):
            service._binding_session_id({'session_id': '../outside'})


class G22TimeoutCompletionRepairTests(unittest.TestCase):
    def test_timeout_does_not_relabel_already_completed_native_result(self) -> None:
        session = object.__new__(LocalCliLiveSession)
        session._state_lock = threading.Lock()
        session._terminal_error = None
        session._closing = False
        session.session_id = 's1'
        session._commands = Queue()
        session._commands_by_id = {}
        session._last_command_id = 's1:command-1'
        session._cancel_pending_commands = lambda *_args, **_kwargs: None
        session._persist_command = lambda _command: None
        command = _LiveCommand(
            command_id='s1:command-1',
            name='replace',
            handler=None,
            future=Future(),
            state='succeeded',
            result={'ok': True, 'dirty': True},
        )
        session._commands_by_id[command.command_id] = command

        error = session._terminalize_timeout(
            command_name='replace',
            command_id=command.command_id,
            message='late timeout',
            command=command,
            enqueue_close=False,
        )

        self.assertEqual(error.command_state, 'timed_out_pending_reconciliation')
        self.assertEqual(command.state, 'completed_after_timeout')
        self.assertEqual(command.result, {'ok': True, 'dirty': True})
        self.assertFalse(command.future.done())

    def test_timeout_cannot_enter_between_terminal_state_and_journal_persistence(self) -> None:
        session = object.__new__(LocalCliLiveSession)
        session._state_lock = threading.Lock()
        session._terminal_error = None
        session._closing = False
        session.session_id = 's1'
        session._commands = Queue()
        session._commands_by_id = {}
        session._last_command_id = 's1:command-1'
        session._cancel_pending_commands = lambda *_args, **_kwargs: None
        persist_started = threading.Event()
        release_persist = threading.Event()

        def persist(_command: _LiveCommand) -> None:
            persist_started.set()
            self.assertTrue(release_persist.wait(timeout=2))

        session._persist_command = persist
        command = _LiveCommand(
            command_id='s1:command-1',
            name='replace',
            handler=None,
            future=Future(),
            state='running',
            sequence=1,
        )
        session._commands_by_id[command.command_id] = command

        def finish_command() -> None:
            with session._state_lock:
                command.result = {'ok': True, 'dirty': True}
                command.state = 'succeeded'
                session._persist_command(command)
                command.completed_event.set()

        worker = threading.Thread(target=finish_command)
        worker.start()
        self.assertTrue(persist_started.wait(timeout=2))
        timeout_result: dict[str, LocalCliRuntimeTimeoutError] = {}
        timeout_thread = threading.Thread(
            target=lambda: timeout_result.setdefault(
                'error',
                session._terminalize_timeout(
                    command_name='replace',
                    command_id=command.command_id,
                    message='late timeout',
                    command=command,
                    enqueue_close=False,
                ),
            )
        )
        timeout_thread.start()
        self.assertTrue(timeout_thread.is_alive())
        release_persist.set()
        worker.join(timeout=2)
        timeout_thread.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(timeout_thread.is_alive())
        self.assertEqual(command.state, 'completed_after_timeout')
        self.assertEqual(command.result, {'ok': True, 'dirty': True})
        self.assertEqual(timeout_result['error'].command_state, 'timed_out_pending_reconciliation')

    def test_runtime_journal_redacts_document_text_and_sensitive_errors(self) -> None:
        bounded = _bounded_command_result({
            'ok': True,
            'artifact_path': '/home/user/private/output.pdf',
            'location': {
                'document_name': 'private-document-title',
                'current_paragraph_preview': 'private document text',
                'document_path': '/home/user/private.hwpx',
                'nearby_context': {'text': 'private nearby text'},
            },
            'warnings': ['private warning at /home/user/private.hwpx'],
            'steps': [{'error': 'password=do-not-store'}],
        })
        serialized = json.dumps(bounded)
        self.assertNotIn('private document text', serialized)
        self.assertNotIn('private-document-title', serialized)
        self.assertNotIn('private nearby text', serialized)
        self.assertNotIn('private warning at', serialized)
        self.assertNotIn('/home/user/private/output.pdf', serialized)
        self.assertNotIn('/home/user/private.hwpx', serialized)
        self.assertNotIn('password=do-not-store', serialized)


class G22WindowsContractRepairTests(unittest.TestCase):
    @staticmethod
    def _read(name: str) -> str:
        return (ROOT / "scripts" / name).read_text(encoding="utf-8")

    def test_fresh_rollback_removes_tasks_before_candidate_root(self) -> None:
        common = self._read("windows_install_common.psm1")
        restore = common[common.index("function Restore-InstallSnapshot") :]
        self.assertLess(restore.index("Register-ScheduledTask"), restore.index("Remove-Item -LiteralPath $CandidateRoot"))
        self.assertIn("Unregister-ScheduledTask", restore[: restore.index("Remove-Item -LiteralPath $CandidateRoot")])

    def test_fresh_activation_has_after_registration_fault_injection(self) -> None:
        installer = self._read("install_windows.ps1")
        registrations = installer[installer.index("Register-ScheduledTask -TaskName $apiTaskName") :]
        registrations = registrations[: registrations.index("$receipt.task_identities_after")]
        self.assertIn("Register-ScheduledTask -TaskName $apiTaskName", registrations)
        self.assertIn("Register-ScheduledTask -TaskName $workerTaskName", registrations)
        self.assertIn("after-task-registrations", registrations)

    def test_preserve_move_rechecks_port_after_predecessor_move(self) -> None:
        installer = self._read("install_windows.ps1")
        activation = installer[installer.index("$phase = 'activation'") :]
        self.assertIn("post_move_port", activation)
        self.assertIn("-not $reused", activation)
        self.assertIn("$postMovePort.available", activation)

    def test_task_identity_rejects_noncanonical_path_segments_and_name_whitespace(self) -> None:
        common = self._read("windows_install_common.psm1")
        path_validator = common[common.index("function Assert-CanonicalScheduledTaskPath") : common.index("function Get-ScheduledTaskExact")]
        name_validator = common[common.index("function Assert-SafeScheduledTaskName") : common.index("function Assert-CanonicalScheduledTaskPath")]
        self.assertIn("-in @('.', '..')", path_validator)
        self.assertIn(".Split([char[]]", path_validator)
        self.assertIn("\\x00", path_validator)
        self.assertIn("Trim", name_validator)
        self.assertIn("[\\x00-\\x1f]", name_validator)

    def test_preserve_move_recovery_boundary_covers_task_admission_and_unregister(self) -> None:
        installer = self._read("install_windows.ps1")
        start = installer.index('$backupRoot = "$install.backup')
        preserve = installer[start : installer.index("Assert-ExistingEnvPreimage", start)]
        seal = preserve.index("Seal-InstallerSnapshot")
        self.assertLess(preserve.index("try {", seal), preserve.index("foreach ($taskName in $taskNames)", seal))
        self.assertLess(seal, preserve.index("Unregister-ScheduledTask"))
        self.assertIn("Restore-PreMoveTaskAdmission", preserve)

    def test_preserve_move_restores_when_disable_fails_before_admission_identity(self) -> None:
        installer = self._read("install_windows.ps1")
        start = installer.index('$backupRoot = "$install.backup')
        preserve = installer[start : installer.index("Assert-ExistingEnvPreimage", start)]
        self.assertIn("if ($preMoveTaskIdentity.Count -gt 0)", preserve)
        self.assertIn('$removedTaskIndex = 0', preserve)
        self.assertIn('after-preserve-task-$removedTaskIndex', preserve)

    def test_preserve_move_fails_closed_if_an_admitted_task_disappears(self) -> None:
        installer = self._read("install_windows.ps1")
        start = installer.index('$backupRoot = "$install.backup')
        preserve = installer[start : installer.index("Assert-ExistingEnvPreimage", start)]
        self.assertIn('Existing scheduled task disappeared during PreserveMove admission', preserve)
        self.assertIn('Existing scheduled task disappeared before PreserveMove root move', preserve)

    def test_preserve_move_restores_tasks_when_root_move_fails(self) -> None:
        installer = self._read("install_windows.ps1")
        rollback = installer[installer.index("if ($receipt.snapshot_path -and (Test-Path", installer.index("catch {")) :]
        restore_gate = rollback[rollback.index("if ($receipt.snapshot_path") : rollback.index("$rollbackRoot =", rollback.index("if ($receipt.snapshot_path"))]
        self.assertIn("-or $backupRoot", restore_gate)
        self.assertIn("Restore-InstallSnapshot", rollback)

    def test_task_and_native_process_contracts_fail_closed_at_mutation_boundary(self) -> None:
        common = self._read("windows_install_common.psm1")
        installer = self._read("install_windows.ps1")
        verifier = self._read("verify_windows.ps1")
        self.assertIn("Assert-SafeScheduledTaskName", common)
        self.assertIn("-TaskPath", common)
        self.assertIn("CREATE_SUSPENDED", common)
        self.assertIn("ResumeThread", common)
        self.assertIn("ProcessAuthority]::Terminate($processHandle)", common)
        self.assertIn("ExpectedProcessIdentity", common)
        process_snapshot = common[common.index("function Get-InstallProcessSnapshot") : common.index("function Stop-InstallProcesses")]
        self.assertIn("Assert-NoReparsePath -Path $RootPath", process_snapshot)
        self.assertIn("-TaskPath", installer)
        self.assertIn("-TaskPath", verifier)

    def test_process_stop_revalidates_root_before_snapshot_and_final_scan(self) -> None:
        common = self._read("windows_install_common.psm1")
        stop = common[common.index("function Stop-InstallProcesses") : common.index("function Wait-ScheduledTaskInactive")]
        self.assertGreaterEqual(stop.count("Assert-NoReparsePath -Path $RootPath"), 1)
        self.assertIn("Assert-NoReparsePath -Path $canonicalRoot", stop)

    def test_path_boundary_check_rejects_reparse_components_before_resolution(self) -> None:
        common = self._read("windows_install_common.psm1")
        boundary = common[common.index("function Test-CanonicalPathWithinRoot") : common.index("function Test-CommandLineModuleToken")]
        self.assertIn("Assert-NoReparsePath -Path $Root", boundary)
        self.assertIn("Assert-NoReparsePath -Path $Path", boundary)

    def test_path_identity_handle_does_not_allow_delete_or_rename_sharing(self) -> None:
        common = self._read("windows_install_common.psm1")
        identity = common[common.index("public static string GetPathIdentity") : common.index("public static class Job")]
        self.assertIn("FILE_SHARE_READ | FILE_SHARE_WRITE", identity)
        self.assertNotIn("FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE", identity)

    def test_suspended_process_constructor_failure_terminates_created_process(self) -> None:
        common = self._read("windows_install_common.psm1")
        create = common[common.index("public static SuspendedProcess Create") : common.index("public void Resume", common.index("public static SuspendedProcess Create"))]
        self.assertIn("TerminateProcess(information.ProcessHandle", create)
        self.assertIn("CREATE_SUSPENDED", create)

    def test_writer_readiness_cleanup_has_direct_owned_launch_fallback(self) -> None:
        writer = self._read("writer_v1.ps1")
        self.assertIn("function Stop-WriterOwnedLaunchTree", writer)
        self.assertIn("process_identity", writer[writer.index("function Start-RoleProcess") : writer.index("function Invoke-PackagedStart")])
        start = writer[writer.index("if (-not (Test-WriterHealthPayload") : writer.index("else {", writer.index("if (-not (Test-WriterHealthPayload"))]
        self.assertIn("Stop-WriterOwnedLaunchTree", start)

    def test_poppler_candidates_are_real_executables_not_batch_wrappers(self) -> None:
        verifier = self._read("verify_windows.ps1")
        poppler = verifier[verifier.index("function Test-VerifierPoppler {") : verifier.index("function Test-VerifierTask {")]
        self.assertIn("'.exe'", poppler)
        self.assertNotIn("'.cmd', '.bat'", poppler)

    def test_writer_poppler_and_publication_paths_are_fail_closed(self) -> None:
        writer = self._read("writer_v1.ps1")
        verifier = self._read("verify_windows.ps1")
        workflow = (ROOT / ".github" / "workflows" / "source-bundle.yml").read_text(encoding="utf-8")
        self.assertIn("ConvertTo-WindowsProcessArgument", writer)
        self.assertIn("Stop-RoleProcess", writer)
        self.assertIn("throw $message", writer)
        self.assertIn("owned = $false", writer)
        self.assertIn("launch.owned", writer)
        self.assertIn("Invoke-NativeChecked", verifier)
        self.assertIn("accepted", verifier)
        poppler = verifier[verifier.index("function Test-VerifierPopplerExecutable") : verifier.index("function Test-VerifierPoppler {")]
        self.assertIn("render_probe", poppler)
        self.assertIn("-singlefile", poppler)
        self.assertIn("-png", poppler)
        self.assertIn("workflow_run", workflow)
        self.assertNotIn("verified-source-publication", workflow)
        self.assertIn("approval prerequisite is absent", workflow)
        self.assertIn("merge-base --is-ancestor", workflow)

    def test_publication_binds_current_main_and_workflow_run_sha(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "source-bundle.yml").read_text(encoding="utf-8")
        self.assertIn("head_repository.full_name == github.repository", workflow)
        self.assertIn('test "$(git rev-parse origin/main)" = "$candidate_sha"', workflow)
        self.assertNotIn("actions/upload-artifact@", workflow)
        self.assertIn("source-bundle-gates", workflow)
        self.assertIn("verified-source", workflow)

    def test_portable_docs_and_ci_use_exact_reproducibility_contract(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        docs = (ROOT / "docs" / "WINDOWS_INSTALL.md").read_text(encoding="utf-8")
        cli_readme = (ROOT / "local_cli_v1" / "README.md").read_text(encoding="utf-8")
        legacy_smoke = (ROOT / "scripts" / "smoke_cli_envelope_static.py").read_text(encoding="utf-8")
        self.assertIn("--require-hashes -r requirements-portable.lock", ci)
        self.assertIn("--expected-archive-sha256", docs)
        self.assertIn("python -m local_cli_v1.main", cli_readme)
        self.assertNotIn("/tmp/fixture", legacy_smoke)


if __name__ == "__main__":
    unittest.main()
