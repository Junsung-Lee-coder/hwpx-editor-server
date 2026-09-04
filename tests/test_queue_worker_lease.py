from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
