from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import Future
from pathlib import Path
import tempfile
import threading
import unittest
from queue import Queue
from types import SimpleNamespace
from unittest.mock import patch

from app.interactive_session_manager import InteractiveSessionManager
from app.local_cli_runtime import (
    _LiveCommand,
    LocalCliLiveSession,
    LocalCliRuntimeError,
    read_command_journal,
    reconcile_command_journal,
    command_journal_path,
)
from app.local_cli_router import build_local_cli_router
from app.local_cli_service import LocalCliMutationError, LocalCliService, LocalCliServiceError
from app.poppler import _iter_winget_candidates
from local_cli_v1 import main as cli_main
from local_cli_v1.proof_packet import ProofPacketError, build_proof_packet
from local_cli_v1 import state as cli_state

StatePersistenceError = getattr(cli_state, 'StatePersistenceError', RuntimeError)
load_state = cli_state.load_state


def update_state(*args, **kwargs):
    updater = getattr(cli_state, 'update_state', None)
    if not callable(updater):
        raise AssertionError('locked state update API is not implemented')
    return updater(*args, **kwargs)


class InstalledIdentityRepairTests(unittest.TestCase):
    @staticmethod
    def _manifest(root: Path, entries: list[tuple[str, bytes]], *, repository: str = 'https://example.invalid/repo.git') -> tuple[dict[str, object], str]:
        files = []
        for relative, data in entries:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            files.append({
                'path': relative,
                'size': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
            })
        manifest = {
            'schema_version': 'hwpx/source-bundle/v1',
            'identity_source': 'asserted-gitless',
            'identity_verified': False,
            'repository': repository,
            'commit': 'a' * 40,
            'tree': 'b' * 40,
            'file_count': len(files),
            'files': files,
            'archive_sha256': 'c' * 64,
        }
        manifest_path = root / 'source-manifest.json'
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return manifest, hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    def _install(self, root: Path) -> dict[str, str]:
        _, manifest_hash = self._manifest(
            root,
            [
                ('local_cli_v1/main.py', b'installed module bytes\n'),
                ('README.md', b'installed readme\n'),
            ],
        )
        marker = {
            'schema_version': 'hwpx/windows-install-marker/v1',
            'repository': 'https://example.invalid/repo.git',
            'commit': 'a' * 40,
            'tree': 'b' * 40,
            'source_manifest_sha256': manifest_hash,
        }
        (root / '.hwpx-install.json').write_text(json.dumps(marker) + '\n', encoding='utf-8')
        return marker

    def test_identity_ignores_caller_cwd_marker_and_binds_module_root_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            module_root = Path(temp_dir) / 'install'
            caller_root = Path(temp_dir) / 'caller'
            module_root.mkdir()
            caller_root.mkdir()
            marker = self._install(module_root)
            (caller_root / '.hwpx-install.json').write_text(json.dumps({
                **marker,
                'commit': 'd' * 40,
            }), encoding='utf-8')
            module_file = module_root / 'local_cli_v1' / 'main.py'
            with patch.object(cli_main, '__file__', str(module_file)), patch.object(Path, 'cwd', return_value=caller_root):
                identity = cli_main._read_candidate_identity()
            self.assertEqual(identity['commit'], marker['commit'])
            self.assertEqual(identity['manifest_sha256'], marker['source_manifest_sha256'])

    def test_identity_rejects_installed_module_bytes_changed_after_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._install(root)
            (root / 'local_cli_v1' / 'main.py').write_bytes(b'tampered module bytes\n')
            self.assertEqual(cli_main._read_candidate_identity(root), {})


class ProofByteBindingRepairTests(unittest.TestCase):
    @staticmethod
    def _hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _state(self, root: Path, *, tamper_pdf: bool = False, missing_page_hash: bool = False) -> dict[str, object]:
        pdf = root / 'export.pdf'
        page = root / 'page-001.png'
        pdf.write_bytes(b'authoritative pdf bytes')
        page.write_bytes(b'authoritative page bytes')
        expected_pdf_hash = self._hash(pdf)
        manifest = {
            'schema_version': 'local-cli/export-proof-range/v1',
            'export_generation': 'export-generation-1',
            'exported_pdf_path': str(pdf),
            'exported_pdf_sha256': f'sha256:{self._hash(pdf)}',
            'pages': [{
                'page': 1,
                'png_path': str(page),
                **({} if missing_page_hash else {'png_sha256': f'sha256:{self._hash(page)}'}),
            }],
            'target_identity': {'section_anchor': 'heading'},
        }
        manifest_path = root / 'export-manifest.json'
        manifest_path.write_text(json.dumps(manifest) + '\n', encoding='utf-8')
        if tamper_pdf:
            pdf.write_bytes(b'replaced pdf bytes')
        return {
            'session_id': 'session-proof',
            'source_path': str(root / 'source.hwpx'),
            'last_export_path': str(pdf),
            'last_export_proof_page_paths': [str(page)],
            'last_export_proof_manifest_path': str(manifest_path),
            'last_export_proof_generation': 'export-generation-1',
            'last_export_proof_export_sha256': f'sha256:{expected_pdf_hash}',
            'last_export_proof_manifest_sha256': f'sha256:{self._hash(manifest_path)}',
        }

    def test_packet_records_manifest_bound_pdf_and_page_hash_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = self._state(root)
            packet = build_proof_packet(out_dir=root / 'packet', state=state)
            records = {item['role']: item for item in packet['artifacts']}
            self.assertTrue(records['exported_pdf']['hash_verified'])
            self.assertTrue(records['export_proof_page']['hash_verified'])
            self.assertEqual(records['exported_pdf']['required_sha256'], state['last_export_proof_export_sha256'])

    def test_packet_rejects_replaced_pdf_before_delivery_ready(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = self._state(root, tamper_pdf=True)
            with self.assertRaisesRegex(ProofPacketError, 'PDF|hash|bound'):
                build_proof_packet(out_dir=root / 'packet', state=state)

    def test_packet_rejects_export_page_without_manifest_bound_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = self._state(root, missing_page_hash=True)
            with self.assertRaisesRegex(ProofPacketError, 'page|hash|bound'):
                build_proof_packet(out_dir=root / 'packet', state=state)


class LockedStateCasRepairTests(unittest.TestCase):
    def test_update_state_serializes_read_modify_write_and_increments_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'state.json'
            update_state(lambda state: {**state, 'session_id': 'session-1', 'counter': 0}, path)
            errors: list[BaseException] = []

            def worker() -> None:
                try:
                    update_state(
                        lambda state: {**state, 'counter': int(state.get('counter', 0)) + 1},
                        path,
                        expected_session_id='session-1',
                    )
                except BaseException as exc:  # pragma: no cover - assertion below
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            state = load_state(path)
            self.assertEqual(state['counter'], 8)
            self.assertEqual(state['state_generation'], 9)

    def test_update_state_rejects_stale_generation_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'state.json'
            saved = update_state(lambda state: {**state, 'session_id': 'session-1'}, path)
            with self.assertRaisesRegex(StatePersistenceError, 'generation'):
                update_state(lambda state: state, path, expected_generation=saved['state_generation'] - 1)
            with self.assertRaisesRegex(StatePersistenceError, 'session'):
                update_state(lambda state: state, path, expected_session_id='session-2')


class RuntimeJournalRepairTests(unittest.TestCase):
    def _session(self, root: Path) -> LocalCliLiveSession:
        session = object.__new__(LocalCliLiveSession)
        session.session_id = 'session-journal'
        session.session_root = root
        session.working_copy_path = root / 'working' / 'document.hwpx'
        session.source_filename = 'document.hwpx'
        session.log_path = root / 'logs' / 'runtime.log'
        from concurrent.futures import Future
        from queue import Queue
        session._commands = Queue()
        session._start_future = Future()
        session._closed = threading.Event()
        session._state_lock = threading.Lock()
        session._closing = False
        session._close_future = None
        session._terminal_error = None
        session._command_counter = 0
        session._commands_by_id = {}
        session._last_command_id = None
        session._thread = type('Thread', (), {'is_alive': lambda self: True})()
        return session

    def test_timeout_command_is_durable_and_late_result_can_be_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = self._session(Path(temp_dir))
            with self.assertRaises(Exception) as raised:
                session.execute('command-bundle', lambda _handle: {'dirty': True}, timeout=0.01)
            self.assertIsInstance(raised.exception, LocalCliRuntimeError)
            command_id = getattr(raised.exception, 'command_id', None)
            self.assertTrue(command_id)
            journal = Path(temp_dir) / 'metadata' / 'command-journal.json'
            self.assertTrue(journal.is_file())
            persisted = json.loads(journal.read_text(encoding='utf-8'))
            self.assertEqual(persisted['commands'][0]['state'], 'timed_out_pending_reconciliation')
            command = session._commands_by_id[command_id]
            command.result = {'dirty': True, 'location': {'cursor_summary': 'late'}}
            command.state = 'completed_after_timeout'
            session._persist_command(command)
            status = session.reconcile_command(command_id)
            self.assertEqual(status['state'], 'completed_after_timeout')
            self.assertTrue(status['reconciled'])


class ProjectionSequenceRepairTests(unittest.TestCase):
    def _service(self, root: Path) -> LocalCliService:
        service = object.__new__(LocalCliService)
        service.root = root / 'local_cli_v1'
        service.sessions_root = service.root / 'sessions'
        service.active_binding_path = service.root / 'active_binding.json'
        service.root.mkdir(parents=True, exist_ok=True)
        service.sessions_root.mkdir(parents=True, exist_ok=True)
        service._closed_session_ids = set()
        service._closed_session_ids_lock = threading.Lock()
        return service

    def test_stale_native_command_sequence_cannot_project_over_newer_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service.runtime_manager = type('Runtime', (), {'latest_command_sequence': lambda self, session_id, **kwargs: 0})()
            service._save_binding({'session_id': 'session-sequence', 'command_generation': 0, 'native_command_sequence': 0})
            service.runtime_manager = type('Runtime', (), {'latest_command_sequence': lambda self, session_id, **kwargs: 2})()
            with self.assertRaisesRegex(LocalCliServiceError, 'sequence'):
                service._save_binding({
                    'session_id': 'session-sequence',
                    'command_generation': 1,
                    'native_command_sequence': 1,
                    '_expected_native_command_sequence': 0,
                })

    def test_health_probe_projects_its_native_sequence_before_binding_save(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            binding = {
                'session_id': 'session-health-probe',
                'command_generation': 0,
                'native_command_sequence': 0,
            }
            service.runtime_manager = type('InitialRuntime', (), {'latest_command_sequence': lambda self, session_id, **kwargs: 0})()
            service._save_binding(binding)

            class Runtime:
                def has_session(self, session_id: str) -> bool:
                    return True

                def execute(self, **kwargs: object) -> dict[str, object]:
                    return {}

                def command_status(self, session_id: str, command_id: str | None = None, **kwargs: object) -> dict[str, object]:
                    return {
                        'command_id': 'session-health-probe:command-1',
                        'sequence': 1,
                        'state': 'succeeded',
                    }

                def latest_command_sequence(self, session_id: str, **kwargs: object) -> int:
                    return 1

            service.runtime_manager = Runtime()
            with patch('app.local_cli_service.snapshot_live_location', return_value={}):
                self.assertTrue(service._probe_live_binding(binding))

            persisted = service._read_binding(session_id='session-health-probe')
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted['native_command_sequence'], 1)


class ServiceReconciliationRepairTests(unittest.TestCase):
    def test_reconciliation_is_exposed_by_the_existing_router_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            router = build_local_cli_router(
                settings=SimpleNamespace(
                    spool_root=Path(temp_dir),
                    log_level='INFO',
                    logs_root=Path(temp_dir) / 'logs',
                ),
                interactive_sessions=object(),
            )
            routes = {route.path for route in router.routes}
            self.assertIn('/local-cli/command-reconcile', routes)

    def test_late_command_reconciliation_updates_binding_and_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = LocalCliService.__new__(LocalCliService)
            service.root = root / 'local_cli_v1'
            service.sessions_root = service.root / 'sessions'
            service.active_binding_path = service.root / 'active_binding.json'
            service.root.mkdir(parents=True)
            service.sessions_root.mkdir(parents=True)
            service._closed_session_ids = set()
            service._closed_session_ids_lock = threading.Lock()
            session_root = service.sessions_root / 'session-reconcile'
            (session_root / 'working').mkdir(parents=True)
            working_copy = session_root / 'working' / 'working-copy.hwpx'
            working_copy.write_bytes(b'working copy')
            service.runtime_manager = type('Runtime', (), {
                'has_session': lambda self, session_id: False,
                'latest_command_sequence': lambda self, session_id, **kwargs: 0,
                'command_status': lambda self, session_id, command_id=None, **kwargs: read_command_journal(session_root, command_id),
                'reconcile_command': lambda self, session_id, command_id, **kwargs: reconcile_command_journal(session_root, command_id),
            })()
            service.interactive_sessions = type('Interactive', (), {
                'record_command': lambda self, *args, **kwargs: None,
            })()
            service._save_binding({
                'session_id': 'session-reconcile',
                'session_root_path': str(session_root),
                'working_copy_path': str(working_copy),
                'source_filename': 'document.hwpx',
                'command_generation': 0,
                'native_command_sequence': 0,
            })
            service.runtime_manager = type('Runtime', (), {
                'has_session': lambda self, session_id: False,
                'latest_command_sequence': lambda self, session_id, **kwargs: 1,
                'command_status': lambda self, session_id, command_id=None, **kwargs: read_command_journal(session_root, command_id),
                'reconcile_command': lambda self, session_id, command_id, **kwargs: reconcile_command_journal(session_root, command_id),
            })()
            service._save_binding({
                'session_id': 'session-reconcile',
                'session_root_path': str(session_root),
                'working_copy_path': str(working_copy),
                'source_filename': 'document.hwpx',
                'command_generation': 1,
                'native_command_sequence': 1,
                '_expected_command_generation': 1,
                '_expected_native_command_sequence': 0,
                'pending_command': {
                    'command_id': 'session-reconcile:command-1',
                    'command': 'command-bundle',
                    'sequence': 1,
                },
            })
            command_journal_path(session_root).write_text(json.dumps({
                'schema_version': 'local-cli/command-journal/v1',
                'session_id': 'session-reconcile',
                'latest_sequence': 1,
                'commands': [{
                    'command_id': 'session-reconcile:command-1',
                    'command': 'command-bundle',
                    'sequence': 1,
                    'state': 'completed_after_timeout',
                    'timed_out': True,
                    'reconcilable': True,
                    'reconciled': False,
                    'result': {
                        'ok': True,
                        'dirty': True,
                        'after_location': {'cursor_summary': 'late'},
                        'artifacts': {'latest_export_path': str(session_root / 'output' / 'late.pdf')},
                    },
                }],
            }), encoding='utf-8')
            result = service.reconcile_command(
                command_id='session-reconcile:command-1',
                session_id='session-reconcile',
            )
            self.assertTrue(result['reconciled'])
            self.assertTrue(result['working_copy_dirty'])
            binding = service._read_binding(session_id='session-reconcile')
            self.assertNotIn('pending_command', binding)
            self.assertEqual(binding['native_command_sequence'], 1)
            journal = read_command_journal(session_root, 'session-reconcile:command-1')
            self.assertTrue(journal['reconciled'])

    def test_reconciliation_failure_keeps_pending_binding_until_journal_ack(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            service = LocalCliService.__new__(LocalCliService)
            service.root = root / 'local_cli_v1'
            service.sessions_root = service.root / 'sessions'
            service.active_binding_path = service.root / 'active_binding.json'
            service.root.mkdir(parents=True)
            service.sessions_root.mkdir(parents=True)
            service._closed_session_ids = set()
            service._closed_session_ids_lock = threading.Lock()
            session_root = service.sessions_root / 'session-reconcile-failure'
            (session_root / 'working').mkdir(parents=True)
            working_copy = session_root / 'working' / 'working-copy.hwpx'
            working_copy.write_bytes(b'working copy')
            binding = {
                'session_id': 'session-reconcile-failure',
                'session_root_path': str(session_root),
                'working_copy_path': str(working_copy),
                'source_filename': 'document.hwpx',
                'command_generation': 1,
                'native_command_sequence': 1,
                'pending_command': {
                    'command_id': 'session-reconcile-failure:command-1',
                    'command': 'command-bundle',
                    'sequence': 1,
                },
            }
            service._write_json(service._binding_path(binding['session_id']), binding)
            service._write_json(service.active_binding_path, binding)
            command_journal_path(session_root).write_text(json.dumps({
                'schema_version': 'local-cli/command-journal/v1',
                'session_id': binding['session_id'],
                'latest_sequence': 1,
                'commands': [{
                    'command_id': binding['pending_command']['command_id'],
                    'command': 'command-bundle',
                    'sequence': 1,
                    'state': 'completed_after_timeout',
                    'timed_out': True,
                    'reconcilable': True,
                    'reconciled': False,
                    'result': {'ok': True, 'dirty': True},
                }],
            }), encoding='utf-8')

            class FailingRuntime:
                def has_session(self, session_id: str) -> bool:
                    return False

                def latest_command_sequence(self, session_id: str, **kwargs: object) -> int:
                    return 1

                def command_status(self, session_id: str, command_id: str | None = None, **kwargs: object) -> dict[str, object]:
                    return read_command_journal(session_root, command_id)

                def reconcile_command(self, session_id: str, command_id: str, **kwargs: object) -> dict[str, object]:
                    raise RuntimeError('journal acknowledgment unavailable')

            service.runtime_manager = FailingRuntime()
            service.interactive_sessions = SimpleNamespace(record_command=lambda *args, **kwargs: None)

            with self.assertRaises(LocalCliServiceError):
                service.reconcile_command(
                    command_id='session-reconcile-failure:command-1',
                    session_id=binding['session_id'],
                )

            persisted = service._read_binding(session_id=binding['session_id'])
            self.assertIsNotNone(persisted)
            self.assertEqual(
                persisted['pending_command']['command_id'],
                'session-reconcile-failure:command-1',
            )


class BoundedTraversalRepairTests(unittest.TestCase):
    def test_winget_traversal_has_depth_and_entry_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            shallow = root / 'a' / 'pdftoppm.exe'
            shallow.parent.mkdir(parents=True)
            shallow.write_bytes(b'exe')
            deep = root
            for index in range(5):
                deep /= f'level-{index}'
                deep.mkdir()
            deep_exe = deep / 'pdftoppm.exe'
            deep_exe.write_bytes(b'exe')
            self.assertEqual(list(_iter_winget_candidates([root], platform='win32', max_depth=1)), [])
            self.assertIn(deep_exe, list(_iter_winget_candidates([root], platform='win32', max_depth=8)))

    def test_winget_traversal_stops_after_entry_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for index in range(5):
                (root / f'noise-{index}').write_bytes(b'noise')
            late = root / 'z-last' / 'pdftoppm.exe'
            late.parent.mkdir()
            late.write_bytes(b'exe')
            self.assertNotIn(late, list(_iter_winget_candidates([root], platform='win32', max_entries=5)))
            self.assertIn(late, list(_iter_winget_candidates([root], platform='win32', max_entries=20)))

    def test_winget_traversal_does_not_yield_symlinked_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / 'real-pdftoppm.exe'
            target.write_bytes(b'exe')
            linked = root / 'pdftoppm.exe'
            try:
                linked.symlink_to(target)
            except OSError:
                self.skipTest('symlink creation is unavailable on this host')

            self.assertNotIn(linked, list(_iter_winget_candidates([root], platform='win32')))


class SessionRetentionRepairTests(unittest.TestCase):
    def test_reaper_keeps_reconcilable_and_ledger_sessions_but_bounds_settled_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = object.__new__(InteractiveSessionManager)
            manager.settings = SimpleNamespace(spool_root=Path(temp_dir))
            manager._read_active_session_id = lambda: 'active'  # type: ignore[method-assign]
            manager.sessions_root.mkdir()
            for index in range(105):
                session_dir = manager.sessions_root / f'session-{index:03d}'
                session_dir.mkdir()
                (session_dir / 'session_state.json').write_text(json.dumps({
                    'session_id': session_dir.name,
                    'state': 'closed',
                    'closed_at': f'2020-01-{(index % 28) + 1:02d}T00:00:00+00:00',
                }), encoding='utf-8')
            pending = manager.sessions_root / 'pending'
            pending.mkdir()
            (pending / 'session_state.json').write_text(json.dumps({
                'session_id': 'pending',
                'state': 'closed',
                'live_runtime': {'reconciliation_pending': True},
            }), encoding='utf-8')
            ledger = manager.sessions_root / 'ledger'
            ledger.mkdir()
            (ledger / 'session_state.json').write_text(json.dumps({'state': 'closed'}), encoding='utf-8')
            (ledger / 'verify_evidence_retention.json').write_text('{}', encoding='utf-8')
            reaped = manager._reap_settled_sessions(max_settled_sessions=100)
            self.assertGreater(reaped, 0)
            self.assertTrue(pending.exists())
            self.assertFalse(ledger.exists())
            remaining_settled = [path for path in manager.sessions_root.iterdir() if path.is_dir()]
            self.assertLessEqual(len(remaining_settled), 102)


class InteractiveHistoryRepairTests(unittest.TestCase):
    def test_record_command_bounds_payload_before_persisting_history(self) -> None:
        manager = InteractiveSessionManager.__new__(InteractiveSessionManager)
        session = {
            'session_id': 'session-history',
            'state': 'open',
            'command_history': [],
            'popup_status': {},
        }
        manager._require_session = lambda session_id=None: session  # type: ignore[method-assign]
        manager._save_session = lambda current: current  # type: ignore[method-assign]
        manager._append_event = lambda *args, **kwargs: None  # type: ignore[method-assign]
        manager._log_operator_event = lambda *args, **kwargs: None  # type: ignore[method-assign]

        recorded = manager.record_command(
            'history-boundary',
            session_id='session-history',
            payload={
                'long_text': 'x' * 3000,
                'many_items': list(range(150)),
                'nested': {'level-1': {'level-2': {'level-3': {'level-4': {'level-5': 'hidden'}}}}},
            },
        )

        payload = recorded['command_history'][0]['payload']
        for key in ('long_text', 'many_items', 'nested'):
            with self.subTest(key=key):
                self.assertIsInstance(payload[key], dict)
                self.assertTrue(payload[key]['omitted'])
                self.assertEqual(len(payload[key]['sha256']), 64)
        self.assertEqual(payload['long_text']['reason'], 'sensitive_field')
        self.assertEqual(payload['many_items']['reason'], 'not_allowlisted')
        self.assertEqual(payload['nested']['reason'], 'not_allowlisted')


class RuntimeCommandIdentityRepairTests(unittest.TestCase):
    def test_command_status_prefers_the_callers_command_over_global_last_command(self) -> None:
        session = object.__new__(LocalCliLiveSession)
        session._state_lock = threading.Lock()
        session._commands_by_id = {}
        session._last_command_id = 'session:command-2'
        session._caller_command_ids = threading.local()
        session._caller_command_ids.command_id = 'session:command-1'
        for sequence in (1, 2):
            command_id = f'session:command-{sequence}'
            command = _LiveCommand(
                command_id=command_id,
                name='find',
                handler=None,
                future=Future(),
                state='succeeded',
                sequence=sequence,
            )
            command.completed_event.set()
            session._commands_by_id[command_id] = command

        status = session.command_status()

        self.assertEqual(status['command_id'], 'session:command-1')
        self.assertEqual(status['sequence'], 1)


class RuntimeCommandCancellationRepairTests(unittest.TestCase):
    def test_cancelled_queued_command_is_persisted_to_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = object.__new__(LocalCliLiveSession)
            session.session_id = 'session-cancel'
            session.session_root = Path(temp_dir)
            session._commands = Queue()
            session._commands_by_id = {}
            command = _LiveCommand(
                command_id='session-cancel:command-2',
                name='replace',
                handler=None,
                future=Future(),
                sequence=2,
            )
            session._commands_by_id[command.command_id] = command
            session._persist_command(command)
            session._commands.put(command)

            session._cancel_pending_commands('session timed out')

            status = read_command_journal(session.session_root, command.command_id)
            self.assertEqual(status['state'], 'cancelled')
            self.assertTrue(status['completed'])


class MutationFailureProjectionRepairTests(unittest.TestCase):
    def test_command_bundle_keeps_binding_dirty_when_mutation_may_have_persisted(self) -> None:
        service = object.__new__(LocalCliService)
        binding = {
            'session_id': 'session-mutation-failure',
            'command_generation': 0,
            'native_command_sequence': 0,
        }
        captured: dict[str, object] = {}
        service._load_active_binding = lambda session_id=None: dict(binding)  # type: ignore[method-assign]
        service._bundle_compact_snapshot = lambda hwp: {'snapshot': True}  # type: ignore[method-assign]
        setattr(service, 'command_packages', SimpleNamespace(allowed_keys=lambda op: set(), get=lambda op: None))

        def invoke_live(*, handler, **kwargs):
            return handler(SimpleNamespace(hwp=object(), source_filename='document.hwpx', session_id=binding['session_id']))

        def update_live_binding(current, **kwargs):
            captured.update(kwargs)
            return current

        service._execute_live = invoke_live  # type: ignore[method-assign]
        service._execute_command_bundle_step = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
            LocalCliMutationError(
                'native readback failed and rollback could not be proven',
                mutation_may_have_persisted=True,
                rollback={'attempted': True, 'succeeded': False},
            )
        )
        service._update_live_binding = update_live_binding  # type: ignore[method-assign]
        service._save_binding = lambda current: current  # type: ignore[method-assign]
        service._record_local_cli_command = lambda *args, **kwargs: None  # type: ignore[method-assign]

        with patch('app.local_cli_service.snapshot_live_location', return_value={}):
            result = service.command_bundle(steps=[{'op': 'context'}])

        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertTrue(result['steps'][0]['dirty'])
        self.assertTrue(captured['dirty'])
        self.assertEqual(result['steps'][0]['rollback'], {'attempted': True, 'succeeded': False})
        self.assertEqual(
            result['steps'][0]['mutation'],
            {
                'may_have_persisted': True,
                'rollback': {'attempted': True, 'succeeded': False},
            },
        )


class CellFillRollbackRepairTests(unittest.TestCase):
    def test_hcellborderfill_readback_failure_is_terminalized_as_mutation_error(self) -> None:
        fill_attr = SimpleNamespace(WinBrushFaceColor=0, Type=0)
        hset = SimpleNamespace(FillAttr=fill_attr)
        parameter_set = SimpleNamespace(HSet=hset, FillAttr=fill_attr)
        execute_calls = [0]

        def execute(*args: object) -> bool:
            execute_calls[0] += 1
            fill_attr.WinBrushFaceColor = 0 if execute_calls[0] == 1 else 1
            return True

        hwp = SimpleNamespace(
            HParameterSet=SimpleNamespace(HCellBorderFill=parameter_set),
            HAction=SimpleNamespace(GetDefault=lambda *args: True, Execute=execute),
            RGBColor=lambda red, green, blue: (red, green, blue),
        )
        service = object.__new__(LocalCliService)

        with self.assertRaises(LocalCliMutationError) as raised:
            service._bundle_apply_cell_fill_color(hwp, '#112233')

        self.assertTrue(raised.exception.mutation_may_have_persisted)
        self.assertTrue(raised.exception.rollback['attempted'])
        self.assertFalse(raised.exception.rollback['succeeded'])


class BindingCleanupRepairTests(unittest.TestCase):
    def test_session_only_cleanup_removes_binding_with_native_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = LocalCliService(
                settings=SimpleNamespace(spool_root=Path(temp_dir)),
                interactive_sessions=SimpleNamespace(),
            )
            binding = {
                'session_id': 'session-clear',
                'command_generation': 1,
                'native_command_sequence': 3,
            }
            service._write_json(service._binding_path('session-clear'), binding)
            service._write_json(service.active_binding_path, binding)

            service._clear_binding(session_id='session-clear')

            self.assertFalse(service._binding_path('session-clear').exists())
            self.assertFalse(service.active_binding_path.exists())


if __name__ == '__main__':
    unittest.main()
