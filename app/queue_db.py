from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from app.models import JobStatus


DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    task_type TEXT NOT NULL DEFAULT 'convert',
    source_filename TEXT NOT NULL,
    source_path TEXT NOT NULL,
    output_path TEXT,
    instructions_path TEXT,
    edited_output_path TEXT,
    job_dir TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    last_heartbeat TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    worker_name TEXT,
    error TEXT,
    file_size_bytes INTEGER NOT NULL DEFAULT 0,
    content_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS worker_leases (
    worker_name TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    candidate_generation TEXT NOT NULL,
    worker_pid INTEGER NOT NULL,
    worker_start_identity TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class QueueDB:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(DDL)
            existing = {row['name'] for row in conn.execute('PRAGMA table_info(jobs)').fetchall()}
            missing_columns = {
                'task_type': "TEXT NOT NULL DEFAULT 'convert'",
                'instructions_path': 'TEXT',
                'edited_output_path': 'TEXT',
            }
            for column, ddl in missing_columns.items():
                if column not in existing:
                    conn.execute(f'ALTER TABLE jobs ADD COLUMN {column} {ddl}')

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute('PRAGMA busy_timeout = 5000;')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def enqueue_job(
        self,
        *,
        task_type: str = 'convert',
        source_filename: str,
        source_path: Path,
        output_path: Path,
        instructions_path: Optional[Path] = None,
        edited_output_path: Optional[Path] = None,
        job_dir: Path,
        file_size_bytes: int,
        content_type: Optional[str],
        max_attempts: int,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        job_id = job_id or uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, status, task_type, source_filename, source_path, output_path,
                    instructions_path, edited_output_path, job_dir,
                    created_at, updated_at, attempts, max_attempts, file_size_bytes, content_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    job_id,
                    JobStatus.queued.value,
                    task_type,
                    source_filename,
                    str(source_path),
                    str(output_path),
                    str(instructions_path) if instructions_path else None,
                    str(edited_output_path) if edited_output_path else None,
                    str(job_dir),
                    now,
                    now,
                    max_attempts,
                    file_size_bytes,
                    content_type,
                ),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as conn:
            row = conn.execute('SELECT * FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
        return dict(row) if row else None

    def count_by_status(self, status: JobStatus) -> int:
        with self.connection() as conn:
            row = conn.execute('SELECT COUNT(*) AS count FROM jobs WHERE status = ?', (status.value,)).fetchone()
        return int(row['count'])

    def acquire_worker_lease(
        self,
        worker_name: str,
        run_id: str,
        candidate_generation: str,
        worker_pid: int,
        worker_start_identity: str,
    ) -> bool:
        """Publish the worker generation used by the transactional claim."""
        if not all(isinstance(value, str) and value.strip() for value in (worker_name, run_id, candidate_generation, worker_start_identity)):
            raise ValueError('Worker lease identity fields must be non-empty strings.')
        if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
            raise ValueError('Worker lease PID must be a positive integer.')
        now = utc_now()
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                """
                INSERT INTO worker_leases (
                    worker_name, run_id, candidate_generation, worker_pid,
                    worker_start_identity, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_name) DO UPDATE SET
                    run_id = excluded.run_id,
                    candidate_generation = excluded.candidate_generation,
                    worker_pid = excluded.worker_pid,
                    worker_start_identity = excluded.worker_start_identity,
                    updated_at = excluded.updated_at
                """,
                (worker_name, run_id, candidate_generation, worker_pid, worker_start_identity, now),
            )
        return True

    def renew_worker_lease(self, worker_name: str, run_id: str, candidate_generation: str) -> bool:
        """Refresh only the currently owned worker lease."""
        now = utc_now()
        with self.connection() as conn:
            updated = conn.execute(
                """
                UPDATE worker_leases
                SET updated_at = ?
                WHERE worker_name = ? AND run_id = ? AND candidate_generation = ?
                """,
                (now, worker_name, run_id, candidate_generation),
            )
        return updated.rowcount == 1

    def claim_next_job(
        self,
        worker_name: str,
        *,
        run_id: str | None = None,
        candidate_generation: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Claim one job only if the supplied worker generation still owns its lease."""
        if (run_id is None) != (candidate_generation is None):
            raise ValueError('run_id and candidate_generation must be supplied together.')
        now = utc_now()
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            lease_clause = ''
            lease_parameters: tuple[str, ...] = ()
            if run_id is not None and candidate_generation is not None:
                lease_clause = """
                    AND EXISTS (
                        SELECT 1 FROM worker_leases lease
                        WHERE lease.worker_name = ?
                          AND lease.run_id = ?
                          AND lease.candidate_generation = ?
                    )
                """
                lease_parameters = (worker_name, run_id, candidate_generation)
            row = conn.execute(
                f"""
                SELECT * FROM jobs
                WHERE status = ? AND attempts < max_attempts
                {lease_clause}
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (JobStatus.queued.value, *lease_parameters),
            ).fetchone()
            if row is None:
                return None

            update_parameters: tuple[Any, ...] = (
                JobStatus.running.value,
                now,
                now,
                now,
                worker_name,
                row['job_id'],
                JobStatus.queued.value,
                *lease_parameters,
            )
            updated = conn.execute(
                f"""
                UPDATE jobs
                SET status = ?, updated_at = ?, started_at = COALESCE(started_at, ?),
                    last_heartbeat = ?, attempts = attempts + 1, worker_name = ?, error = NULL
                WHERE job_id = ? AND status = ?
                {lease_clause}
                """,
                update_parameters,
            )
            if updated.rowcount != 1:
                return None
            refreshed = conn.execute('SELECT * FROM jobs WHERE job_id = ?', (row['job_id'],)).fetchone()
        return dict(refreshed) if refreshed else None

    def touch_heartbeat(self, job_id: str) -> None:
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                'UPDATE jobs SET last_heartbeat = ?, updated_at = ? WHERE job_id = ?',
                (now, now, job_id),
            )

    def mark_succeeded(self, job_id: str) -> None:
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?, finished_at = ?, error = NULL
                WHERE job_id = ?
                """,
                (JobStatus.succeeded.value, now, now, job_id),
            )

    def mark_failed(self, job_id: str, error: str) -> None:
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?, finished_at = ?, error = ?
                WHERE job_id = ?
                """,
                (JobStatus.failed.value, now, now, error, job_id),
            )

    def requeue_job(self, job_id: str, error: str) -> None:
        now = utc_now()
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?, error = ?, started_at = NULL, finished_at = NULL,
                    worker_name = NULL, last_heartbeat = NULL
                WHERE job_id = ?
                """,
                (JobStatus.queued.value, now, error, job_id),
            )

    def recover_stale_running_jobs(self, stale_after_seconds: int) -> int:
        threshold = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        threshold_text = threshold.replace(microsecond=0).isoformat()
        recovered = 0
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                  AND COALESCE(last_heartbeat, started_at, updated_at) < ?
                """,
                (JobStatus.running.value, threshold_text),
            ).fetchall()

            for row in rows:
                now = utc_now()
                if int(row['attempts']) < int(row['max_attempts']):
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, updated_at = ?, started_at = NULL, worker_name = NULL,
                            error = ?, last_heartbeat = NULL
                        WHERE job_id = ?
                        """,
                        (
                            JobStatus.queued.value,
                            now,
                            'Recovered stale running job for retry.',
                            row['job_id'],
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, updated_at = ?, finished_at = ?, error = ?
                        WHERE job_id = ?
                        """,
                        (
                            JobStatus.failed.value,
                            now,
                            now,
                            'Job exceeded max attempts after stale recovery.',
                            row['job_id'],
                        ),
                    )
                recovered += 1
        return recovered
