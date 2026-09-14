from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from queue import Queue
import tempfile
import threading
import unittest
from unittest import mock

from app.local_cli_runtime import (
    LocalCliLiveSession,
    LocalCliRuntimeManager,
    LocalCliRuntimeError,
    LocalCliRuntimeTimeoutError,
)


class _NeverStartingThread:
    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return True


class LiveRuntimeTimeoutTests(unittest.TestCase):
    def _session(self) -> LocalCliLiveSession:
        session = object.__new__(LocalCliLiveSession)
        session.session_id = 'session-timeout'
        session.session_root = Path(tempfile.gettempdir()) / 'local-cli-timeout'
        session.working_copy_path = session.session_root / 'working' / 'document.hwpx'
        session.source_filename = 'document.hwpx'
        session.log_path = session.session_root / 'logs' / 'runtime.log'
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
        session._thread = _NeverStartingThread()
        return session

    def test_command_timeout_terminalizes_session_and_rejects_retry(self) -> None:
        session = self._session()

        with self.assertRaises(LocalCliRuntimeTimeoutError) as raised:
            session.execute('edit', lambda _handle: {'mutated': True}, timeout=0.01)

        self.assertIn('timed out', str(raised.exception).lower())
        self.assertTrue(session.is_terminal())
        with self.assertRaisesRegex(LocalCliRuntimeError, 'terminal'):
            session.execute('edit-retry', lambda _handle: {'mutated': True}, timeout=0.01)

    def test_close_timeout_is_terminal_and_does_not_admit_commands(self) -> None:
        session = self._session()

        with self.assertRaises(LocalCliRuntimeTimeoutError):
            session.close(timeout=0.01)

        self.assertTrue(session.is_terminal())
        with self.assertRaisesRegex(LocalCliRuntimeError, 'terminal|closing'):
            session.execute('after-close-timeout', lambda _handle: None, timeout=0.01)

    def test_timeout_error_contains_reconcilable_command_identity(self) -> None:
        session = self._session()

        with self.assertRaises(LocalCliRuntimeTimeoutError) as raised:
            session.execute('export', lambda _handle: None, timeout=0.01)

        error = raised.exception
        self.assertTrue(error.command_id)
        self.assertEqual(error.command_state, 'timed_out_pending_reconciliation')
        status = session.command_status(error.command_id)
        self.assertEqual(status['state'], 'timed_out_pending_reconciliation')
        self.assertEqual(status['command'], 'export')

    def test_synthetic_start_timeout_is_marked_for_cleanup_reconciliation(self) -> None:
        session = self._session()

        error = session._terminalize_timeout(
            command_name='start',
            command_id='session-timeout:start',
            message='start timed out',
            enqueue_close=False,
        )

        command = session._commands_by_id[error.command_id]
        self.assertTrue(command.timed_out)
        self.assertEqual(command.state, 'timed_out_pending_reconciliation')
        self.assertGreater(command.sequence, 0)

    def test_runtime_manager_does_not_replace_terminal_session_before_reconciliation(self) -> None:
        manager = LocalCliRuntimeManager()

        class TerminalSession:
            def is_alive(self) -> bool:
                return False

            def is_terminal(self) -> bool:
                return True

        existing = TerminalSession()
        manager._sessions['session-timeout'] = existing  # type: ignore[assignment]

        with mock.patch('app.local_cli_runtime.LocalCliLiveSession') as live_session:
            with self.assertRaisesRegex(LocalCliRuntimeError, 'terminal|reconcil'):
                manager.open_session(
                    session_id='session-timeout',
                    session_root=Path(tempfile.gettempdir()),
                    working_copy_path=Path(tempfile.gettempdir()) / 'document.hwpx',
                    source_filename='document.hwpx',
                )

        live_session.assert_not_called()
        self.assertIs(manager._sessions['session-timeout'], existing)

    def test_runtime_manager_does_not_replace_reconciled_session_before_cleanup(self) -> None:
        manager = LocalCliRuntimeManager()

        class ReconciledButAlive:
            def is_alive(self) -> bool:
                return True

            def is_terminal(self) -> bool:
                return True

            def has_unreconciled_reconciliation(self) -> bool:
                return False

        existing = ReconciledButAlive()
        manager._sessions['session-timeout'] = existing  # type: ignore[assignment]

        with mock.patch('app.local_cli_runtime.LocalCliLiveSession') as live_session:
            with self.assertRaisesRegex(LocalCliRuntimeError, 'cleanup'):
                manager.open_session(
                    session_id='session-timeout',
                    session_root=Path(tempfile.gettempdir()),
                    working_copy_path=Path(tempfile.gettempdir()) / 'document.hwpx',
                    source_filename='document.hwpx',
                )

        live_session.assert_not_called()
        self.assertIs(manager._sessions['session-timeout'], existing)


if __name__ == '__main__':
    unittest.main()
