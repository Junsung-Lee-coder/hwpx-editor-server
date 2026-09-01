from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest

from app.local_cli_service import LocalCliService, LocalCliServiceError


class BindingGenerationPersistenceTests(unittest.TestCase):
    def _service(self, root: Path) -> LocalCliService:
        service = object.__new__(LocalCliService)
        service.root = root / 'local_cli_v1'
        service.sessions_root = service.root / 'sessions'
        service.active_binding_path = service.root / 'active_binding.json'
        service.root.mkdir(parents=True, exist_ok=True)
        service.sessions_root.mkdir(parents=True, exist_ok=True)
        return service

    def test_save_binding_uses_generation_cas_and_atomic_projection_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            original = {'session_id': 'session-1', 'command_generation': 0, 'value': 'first'}

            saved = service._save_binding(dict(original))

            self.assertEqual(saved['command_generation'], 1)
            session_path = service._binding_path('session-1')
            self.assertEqual(service._read_json(session_path), service._read_json(service.active_binding_path))
            self.assertEqual(service._read_json(session_path)['value'], 'first')
            self.assertFalse(list(service.root.rglob('*.tmp')))

            stale = dict(original)
            stale['value'] = 'stale-overwrite'
            with self.assertRaisesRegex(LocalCliServiceError, 'generation'):
                service._save_binding(stale)

            self.assertEqual(service._read_json(session_path)['value'], 'first')

    def test_binding_read_fails_closed_on_partial_or_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            path = service.active_binding_path
            path.write_text('{"session_id":', encoding='utf-8')

            with self.assertRaisesRegex(LocalCliServiceError, 'parse|JSON|read'):
                service._read_json(path)

    def test_binding_generation_type_corruption_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service._write_json(
                service._binding_path('session-1'),
                {'session_id': 'session-1', 'command_generation': 'not-an-integer'},
            )

            with self.assertRaisesRegex(LocalCliServiceError, 'generation|invalid'):
                service._save_binding({'session_id': 'session-1', 'command_generation': 0, 'value': 'late'})

    def test_live_command_rebinds_to_current_generation_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            current = {'session_id': 'session-1', 'command_generation': 1, 'value': 'newer'}
            service._save_binding({'session_id': 'session-1', 'command_generation': 0, 'value': 'initial'})
            service._save_binding(current)

            class RuntimeManager:
                def has_session(self, session_id: str) -> bool:
                    return session_id == 'session-1'

                def execute(self, **kwargs):  # noqa: ANN003
                    return {'native': True}

                def command_status(self, session_id: str) -> dict[str, object]:
                    return {
                        'command_id': f'{session_id}:command-2',
                        'state': 'succeeded',
                    }

            service.runtime_manager = RuntimeManager()
            service._require_ready_runtime = lambda _label: None
            stale = {'session_id': 'session-1', 'command_generation': 1, 'value': 'stale'}

            result = service._execute_live(
                binding=stale,
                command_name='find',
                task_label='test.find',
                handler=lambda _handle: {'unused': True},
            )

            self.assertEqual(result['native'], True)
            self.assertEqual(stale['command_generation'], 2)
            self.assertEqual(stale['_expected_command_generation'], 2)
            self.assertEqual(result['_local_cli_command']['generation'], 2)

    def test_clearing_stale_binding_does_not_remove_newer_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            first = service._save_binding({'session_id': 'session-1', 'command_generation': 0, 'value': 'first'})
            service._save_binding(dict(first, value='newer'))

            service._clear_binding(binding=dict(first))

            self.assertIsNotNone(service._read_json(service._binding_path('session-1')))
            self.assertIsNotNone(service._read_json(service.active_binding_path))

    def test_stale_save_cannot_resurrect_binding_after_close_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            committed = service._save_binding({'session_id': 'session-1', 'command_generation': 0, 'value': 'open'})
            service._clear_binding(binding=dict(committed))

            with self.assertRaisesRegex(LocalCliServiceError, 'cleared|generation|resurrect'):
                service._save_binding(dict(committed, value='late-command-result'))

            self.assertIsNone(service._read_json(service._binding_path('session-1')))
            self.assertIsNone(service._read_json(service.active_binding_path))

    def test_save_binding_rejects_overwriting_another_active_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service._save_binding({'session_id': 'session-1', 'command_generation': 0, 'value': 'one'})
            service._write_json(
                service.active_binding_path,
                {'session_id': 'foreign-session', 'command_generation': 4, 'value': 'foreign'},
            )

            with self.assertRaisesRegex(LocalCliServiceError, 'active binding|session'):
                service._save_binding({'session_id': 'session-1', 'command_generation': 1, 'value': 'overwrite'})

            self.assertEqual(service._read_json(service.active_binding_path)['session_id'], 'foreign-session')

    def test_concurrent_find_edit_export_commits_keep_atomic_projection_generations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            initial = service._save_binding({'session_id': 'session-1', 'command_generation': 0, 'value': 'initial'})
            errors: list[BaseException] = []
            start = threading.Barrier(4)

            def commit(command: str) -> None:
                try:
                    start.wait()
                    base = dict(initial)
                    base['_binding_base'] = dict(initial)
                    base['_expected_command_generation'] = initial['command_generation']
                    base['last_command'] = command
                    service._save_binding(base)
                except BaseException as exc:  # pragma: no cover - diagnostic
                    errors.append(exc)

            threads = [threading.Thread(target=commit, args=(command,)) for command in ('find', 'edit', 'export', 'close')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            session = service._read_json(service._binding_path('session-1'))
            active = service._read_json(service.active_binding_path)
            self.assertEqual(session, active)
            self.assertEqual(session['command_generation'], 5)
            self.assertIn(session['last_command'], {'find', 'edit', 'export', 'close'})

    def test_close_marks_session_before_clear_and_rejects_late_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            committed = service._save_binding({'session_id': 'session-1', 'command_generation': 0, 'value': 'open'})

            class RuntimeManager:
                def close_session(self, session_id: str) -> None:
                    self.session_id = session_id

            class InteractiveSessions:
                def record_command(self, *args: object, **kwargs: object) -> None:
                    return None

            service.runtime_manager = RuntimeManager()
            service.interactive_sessions = InteractiveSessions()
            service.close(session_id='session-1')

            with self.assertRaisesRegex(LocalCliServiceError, 'closed|resurrect'):
                service._save_binding(dict(committed, value='late'))
            self.assertIsNone(service._read_json(service._binding_path('session-1')))
            self.assertIsNone(service._read_json(service.active_binding_path))

    def test_status_clears_binding_after_terminal_session_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._service(Path(temp_dir))
            service._save_binding({'session_id': 'session-1', 'command_generation': 0, 'value': 'open'})
            service._mark_session_closed('session-1')
            service._runtime_snapshot = lambda: {'ready': True, 'checks': {}}

            class RuntimeManager:
                def has_session(self, session_id: str) -> bool:
                    return False

            service.runtime_manager = RuntimeManager()

            status = service.status()

            self.assertFalse(status['live_session_bound'])
            self.assertIsNone(service._read_json(service._binding_path('session-1')))
            self.assertIsNone(service._read_json(service.active_binding_path))


if __name__ == '__main__':
    unittest.main()
