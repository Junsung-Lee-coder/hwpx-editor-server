from __future__ import annotations

import tempfile
import unittest
from subprocess import CompletedProcess
from pathlib import Path
from unittest.mock import patch

from app import worker as worker_module
from app.models import JobStatus
from app.queue_db import QueueDB


class QueueWorkerLeaseTests(unittest.TestCase):
    def _enqueue(self, db: QueueDB, job_id: str) -> None:
        db.enqueue_job(
            source_filename=f"{job_id}.hwpx",
            source_path=Path(f"/tmp/{job_id}.hwpx"),
            output_path=Path(f"/tmp/{job_id}.pdf"),
            job_dir=Path(f"/tmp/{job_id}"),
            file_size_bytes=1,
            content_type="application/octet-stream",
            max_attempts=2,
            job_id=job_id,
        )

    def test_claim_requires_matching_persisted_worker_lease(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = QueueDB(Path(raw) / "queue.sqlite3")
            self._enqueue(db, "job-1")
            self.assertIsNone(db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1"))
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            claimed = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["status"], JobStatus.running.value)
            self.assertEqual(claimed["worker_name"], "worker")

    def test_superseding_lease_blocks_old_run_before_next_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = QueueDB(Path(raw) / "queue.sqlite3")
            self._enqueue(db, "job-1")
            self._enqueue(db, "job-2")
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            first = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertEqual(first["job_id"], "job-1")
            db.requeue_job("job-1", "test")
            self.assertTrue(db.acquire_worker_lease("worker", "run-2", "gen-2", 456, "start-2"))
            self.assertIsNone(db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1"))
            second = db.claim_next_job("worker", run_id="run-2", candidate_generation="gen-2")
            self.assertIsNotNone(second)
            self.assertEqual(second["worker_name"], "worker")
            self.assertEqual(second["job_id"], "job-1")

    def test_lease_arguments_must_be_supplied_as_a_pair(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = QueueDB(Path(raw) / "queue.sqlite3")
            with self.assertRaises(ValueError):
                db.claim_next_job("worker", run_id="run-1")
            with self.assertRaises(ValueError):
                db.claim_next_job("worker", candidate_generation="gen-1")

    def test_claimed_job_can_be_requeued_by_exact_owner_after_lease_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = QueueDB(Path(raw) / "queue.sqlite3")
            self._enqueue(db, "job-1")
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            claimed = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertIsNotNone(claimed)

            self.assertTrue(db.acquire_worker_lease("worker", "run-2", "gen-2", 456, "start-2"))
            self.assertTrue(
                db.requeue_claimed_job_if_owned(
                    "job-1",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                    error="lease replaced before execution",
                )
            )
            released = db.get_job("job-1")
            self.assertIsNotNone(released)
            self.assertEqual(released["status"], JobStatus.queued.value)
            self.assertIsNone(released["worker_name"])

    def test_execution_claim_releases_job_when_lease_changes_after_claim(self) -> None:
        class ReplacingQueueDB(QueueDB):
            def claim_next_job(self, worker_name: str, *, run_id: str | None = None, candidate_generation: str | None = None):
                claimed = super().claim_next_job(
                    worker_name,
                    run_id=run_id,
                    candidate_generation=candidate_generation,
                )
                if claimed is not None:
                    self.acquire_worker_lease(worker_name, "successor-run", "successor-generation", 456, "start-2")
                return claimed

        with tempfile.TemporaryDirectory() as raw:
            db = ReplacingQueueDB(Path(raw) / "queue.sqlite3")
            self._enqueue(db, "job-1")
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))

            with patch.object(worker_module, "db", db):
                claimed = worker_module.claim_job_for_execution("worker", "run-1", "gen-1")

            self.assertIsNone(claimed)
            released = db.get_job("job-1")
            self.assertIsNotNone(released)
            self.assertEqual(released["status"], JobStatus.queued.value)
            self.assertIsNone(released["worker_name"])

    def test_stale_claim_cannot_requeue_successor_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = QueueDB(Path(raw) / "queue.sqlite3")
            self._enqueue(db, "job-1")
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            first = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertIsNotNone(first)
            self.assertTrue(db.acquire_worker_lease("worker", "run-2", "gen-2", 456, "start-2"))
            self.assertTrue(
                db.requeue_claimed_job_if_owned(
                    "job-1",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                    error="lease replaced before successor claim",
                )
            )
            second = db.claim_next_job("worker", run_id="run-2", candidate_generation="gen-2")
            self.assertIsNotNone(second)
            self.assertFalse(
                db.requeue_claimed_job_if_owned(
                    "job-1",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                    error="stale worker must not clobber successor",
                )
            )
            current = db.get_job("job-1")
            self.assertIsNotNone(current)
            self.assertEqual(current["status"], JobStatus.running.value)
            self.assertEqual(current["claim_run_id"], "run-2")

    def test_owner_aware_job_updates_do_not_clobber_successor_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = QueueDB(Path(raw) / "queue.sqlite3")
            self._enqueue(db, "job-1")
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            first = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertIsNotNone(first)
            self.assertTrue(
                db.touch_heartbeat_if_owned(
                    "job-1",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                )
            )
            self.assertTrue(
                db.requeue_claimed_job_if_owned(
                    "job-1",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                    error="handoff to successor",
                )
            )
            self.assertTrue(db.acquire_worker_lease("worker", "run-2", "gen-2", 456, "start-2"))
            second = db.claim_next_job("worker", run_id="run-2", candidate_generation="gen-2")
            self.assertIsNotNone(second)
            self.assertFalse(
                db.touch_heartbeat_if_owned(
                    "job-1",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                )
            )
            self.assertFalse(
                db.mark_succeeded_if_owned(
                    "job-1",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                )
            )
            self.assertFalse(
                db.mark_failed_if_owned(
                    "job-1",
                    "stale failure",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                )
            )
            current = db.get_job("job-1")
            self.assertIsNotNone(current)
            self.assertEqual(current["status"], JobStatus.running.value)
            self.assertEqual(current["claim_run_id"], "run-2")

    def test_worker_does_not_start_child_after_successor_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = QueueDB(root / "queue.sqlite3")
            db.enqueue_job(
                source_filename="job-1.hwpx",
                source_path=root / "job-1.hwpx",
                output_path=root / "job-1.pdf",
                job_dir=root / "job-1",
                file_size_bytes=1,
                content_type="application/octet-stream",
                max_attempts=2,
                job_id="job-1",
            )
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            first = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertIsNotNone(first)
            self.assertTrue(
                db.requeue_claimed_job_if_owned(
                    "job-1",
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                    error="handoff to successor",
                )
            )
            self.assertTrue(db.acquire_worker_lease("worker", "run-2", "gen-2", 456, "start-2"))
            successor = db.claim_next_job("worker", run_id="run-2", candidate_generation="gen-2")
            self.assertIsNotNone(successor)

            with patch.object(worker_module, "db", db), patch.object(worker_module, "run_conversion_subprocess") as run_child:
                worker_module.handle_job(
                    first,
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                )

            run_child.assert_not_called()
            current = db.get_job("job-1")
            self.assertIsNotNone(current)
            self.assertEqual(current["status"], JobStatus.running.value)
            self.assertEqual(current["claim_run_id"], "run-2")

    def test_worker_releases_claim_when_lease_replaced_before_handle(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = QueueDB(root / "queue.sqlite3")
            db.enqueue_job(
                source_filename="job-1.hwpx",
                source_path=root / "job-1.hwpx",
                output_path=root / "job-1.pdf",
                job_dir=root / "job-1",
                file_size_bytes=1,
                content_type="application/octet-stream",
                max_attempts=2,
                job_id="job-1",
            )
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            claimed = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertIsNotNone(claimed)
            self.assertTrue(db.acquire_worker_lease("worker", "run-2", "gen-2", 456, "start-2"))

            with patch.object(worker_module, "db", db), patch.object(worker_module, "run_conversion_subprocess") as run_child:
                worker_module.handle_job(
                    claimed,
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                )

            run_child.assert_not_called()
            current = db.get_job("job-1")
            self.assertIsNotNone(current)
            self.assertEqual(current["status"], JobStatus.queued.value)
            self.assertIsNone(current["worker_name"])

    def test_worker_records_success_for_exact_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = QueueDB(root / "queue.sqlite3")
            db.enqueue_job(
                source_filename="job-1.hwpx",
                source_path=root / "job-1.hwpx",
                output_path=root / "job-1.pdf",
                job_dir=root / "job-1",
                file_size_bytes=1,
                content_type="application/octet-stream",
                max_attempts=2,
                job_id="job-1",
            )
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            claimed = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertIsNotNone(claimed)
            (root / "job-1.pdf").write_bytes(b"pdf")

            with patch.object(worker_module, "db", db), patch.object(
                worker_module,
                "run_conversion_subprocess",
                return_value=CompletedProcess([], 0, stdout="", stderr=""),
            ) as run_child:
                worker_module.handle_job(
                    claimed,
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                )

            run_child.assert_called_once()
            current = db.get_job("job-1")
            self.assertIsNotNone(current)
            self.assertEqual(current["status"], JobStatus.succeeded.value)

    def test_worker_requeues_failure_for_exact_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = QueueDB(root / "queue.sqlite3")
            db.enqueue_job(
                source_filename="job-1.hwpx",
                source_path=root / "job-1.hwpx",
                output_path=root / "job-1.pdf",
                job_dir=root / "job-1",
                file_size_bytes=1,
                content_type="application/octet-stream",
                max_attempts=2,
                job_id="job-1",
            )
            self.assertTrue(db.acquire_worker_lease("worker", "run-1", "gen-1", 123, "start-1"))
            claimed = db.claim_next_job("worker", run_id="run-1", candidate_generation="gen-1")
            self.assertIsNotNone(claimed)

            with patch.object(worker_module, "db", db), patch.object(
                worker_module,
                "run_conversion_subprocess",
                return_value=CompletedProcess([], 1, stdout="", stderr="conversion failed"),
            ):
                worker_module.handle_job(
                    claimed,
                    worker_name="worker",
                    run_id="run-1",
                    candidate_generation="gen-1",
                )

            current = db.get_job("job-1")
            self.assertIsNotNone(current)
            self.assertEqual(current["status"], JobStatus.queued.value)
            self.assertIsNone(current["worker_name"])
            self.assertIsNone(current["claim_run_id"])


if __name__ == "__main__":
    unittest.main()
