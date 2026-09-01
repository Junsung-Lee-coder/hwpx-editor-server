from __future__ import annotations

from pathlib import Path
from queue import Queue
import threading
import unittest

from app.local_cli_runtime import LocalCliLiveSession, LocalCliRuntimeManager


class _CloseObservingSession:
    def __init__(self, manager: LocalCliRuntimeManager, session_id: str, *, fail: bool = False) -> None:
        self.manager = manager
        self.session_id = session_id
        self.fail = fail
        self.was_registered_during_close = False

    def is_alive(self) -> bool:
        return True

    def close(self, *, timeout: float) -> None:
        del timeout
        self.was_registered_during_close = self.session_id in self.manager._sessions
        if self.fail:
            raise RuntimeError("close failed")


class LocalCliRuntimeManagerLifecycleTests(unittest.TestCase):
    def test_close_waits_for_runtime_thread_cleanup_after_close_command(self) -> None:
        session = object.__new__(LocalCliLiveSession)
        session._closed = threading.Event()
        session._commands = Queue()
        cleanup_started = threading.Event()
        allow_cleanup = threading.Event()
        close_returned = threading.Event()
        close_errors: list[BaseException] = []

        def finish_cleanup() -> None:
            command = session._commands.get(timeout=1.0)
            command.future.set_result({'ok': True})
            cleanup_started.set()
            if not allow_cleanup.wait(timeout=1.0):
                raise AssertionError('test cleanup release was not enabled')
            session._closed.set()

        thread = threading.Thread(target=finish_cleanup)
        thread.start()

        def run_close() -> None:
            try:
                session.close(timeout=1.0)
            except BaseException as exc:  # pragma: no cover - asserted below
                close_errors.append(exc)
            finally:
                close_returned.set()

        caller = threading.Thread(target=run_close)
        caller.start()
        try:
            self.assertTrue(cleanup_started.wait(timeout=1.0))
            self.assertFalse(
                close_returned.wait(timeout=0.05),
                'close must wait while native/COM cleanup is still blocked',
            )
            allow_cleanup.set()
        finally:
            allow_cleanup.set()
            caller.join(timeout=1.0)
            thread.join(timeout=1.0)

        self.assertFalse(caller.is_alive(), 'close caller did not finish')
        self.assertFalse(close_errors)
        self.assertTrue(
            session._closed.is_set(),
            "close must not return before native/COM cleanup finishes",
        )

    def test_close_keeps_handle_registered_until_native_close_finishes(self) -> None:
        manager = LocalCliRuntimeManager()
        session_id = "fixture-session"
        session = _CloseObservingSession(manager, session_id)
        manager._sessions[session_id] = session  # type: ignore[assignment]

        manager.close_session(session_id)

        self.assertTrue(
            session.was_registered_during_close,
            "the live handle must remain discoverable while native cleanup runs",
        )
        self.assertNotIn(session_id, manager._sessions)

    def test_close_failure_does_not_drop_handle_before_cleanup_can_retry(self) -> None:
        manager = LocalCliRuntimeManager()
        session_id = "fixture-session-failure"
        session = _CloseObservingSession(manager, session_id, fail=True)
        manager._sessions[session_id] = session  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "close failed"):
            manager.close_session(session_id)

        self.assertTrue(
            session.was_registered_during_close,
            "the live handle must remain discoverable during a failed close",
        )
        self.assertIn(
            session_id,
            manager._sessions,
            "a failed native close must not lose the handle needed for retry/diagnosis",
        )


class FixtureVerifierLifecycleTests(unittest.TestCase):
    def test_fixture_verifier_records_close_cleanup_and_waits_for_closed_status(self) -> None:
        verifier = (Path(__file__).resolve().parents[1] / 'scripts' / 'verify_windows.ps1').read_text()
        for token in (
            '$closeAttempted',
            '$closeConfirmed',
            '$closeCleanupResult',
            'fixture_close_cleanup',
            'close_attempted',
            'close_confirmed',
            'Start-Sleep -Milliseconds 500',
        ):
            with self.subTest(token=token):
                self.assertIn(token, verifier)

    def test_fixture_verifier_keeps_required_sequence_order(self) -> None:
        verifier = (Path(__file__).resolve().parents[1] / 'scripts' / 'verify_windows.ps1').read_text()
        sequence = (
            "name = 'fixture_open'",
            "name = 'fixture_status_open'",
            "name = 'fixture_where'",
            "name = 'fixture_page_screenshot'",
            "name = 'fixture_close'",
            "name = 'fixture_status_closed'",
        )
        offsets = [verifier.index(item) for item in sequence]
        self.assertEqual(offsets, sorted(offsets))


if __name__ == "__main__":
    unittest.main()
