from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


DEFAULT_LOG_EVERY_POLLS = 10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Submit one HWPX file to the local converter and wait for the PDF result.')
    parser.add_argument('base_url', help='Converter base URL, for example http://127.0.0.1:8765')
    parser.add_argument('source', type=Path, help='Input .hwpx file')
    parser.add_argument('--output', type=Path, help='Output PDF path. Default: next to the source file')
    parser.add_argument('--poll-interval', type=float, default=3.0, help='Seconds between job status polls')
    parser.add_argument('--timeout-seconds', type=float, default=600.0, help='Stop waiting after this many seconds')
    parser.add_argument('--max-polls', type=int, help='Hard cap on job-status poll attempts. Default: derived from timeout-seconds and poll-interval')
    parser.add_argument('--poll-log', type=Path, help='Append JSONL poll events to this file')
    parser.add_argument('--log-every-polls', type=int, default=DEFAULT_LOG_EVERY_POLLS, help='Emit a heartbeat line after this many unchanged polls (0 disables)')
    return parser


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def resolve_max_polls(timeout_seconds: float, poll_interval: float, explicit_max_polls: int | None) -> int:
    if poll_interval <= 0:
        raise ValueError('--poll-interval must be greater than 0.')
    if timeout_seconds <= 0:
        raise ValueError('--timeout-seconds must be greater than 0.')
    if explicit_max_polls is not None:
        if explicit_max_polls <= 0:
            raise ValueError('--max-polls must be greater than 0 when provided.')
        return explicit_max_polls
    return max(1, int(timeout_seconds / poll_interval) + 1)


def build_job_snapshot(job_id: str, job: dict) -> dict[str, object]:
    status = str(job.get('status') or 'unknown')
    attempts = job.get('attempts')
    updated_at = job.get('updated_at')
    error = job.get('error')
    state_key = json.dumps(
        {
            'status': status,
            'attempts': attempts,
            'updated_at': updated_at,
            'error': error,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        'job_id': job_id,
        'phase': 'execution',
        'status': status,
        'attempts': attempts,
        'updated_at': updated_at,
        'error': error,
        'state_key': state_key,
    }


def build_poll_event(
    *,
    event: str,
    started_at: float,
    poll_count: int,
    snapshot: dict[str, object] | None,
    unchanged_polls: int = 0,
    timeout_seconds: float | None = None,
    max_polls: int | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        'ts': utc_now_iso(),
        'event': event,
        'poll_count': poll_count,
        'elapsed_seconds': round(max(0.0, time.monotonic() - started_at), 3),
    }
    if timeout_seconds is not None:
        payload['timeout_seconds'] = timeout_seconds
    if max_polls is not None:
        payload['max_polls'] = max_polls
    if unchanged_polls:
        payload['unchanged_polls'] = unchanged_polls
    if snapshot:
        for key, value in snapshot.items():
            if value is None or key == 'state_key':
                continue
            payload[key] = value
    return payload


def append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
        handle.write('\n')


def format_poll_event(event: dict[str, object]) -> str:
    ordered_keys = (
        'event',
        'job_id',
        'poll_count',
        'elapsed_seconds',
        'phase',
        'status',
        'attempts',
        'updated_at',
        'unchanged_polls',
        'timeout_seconds',
        'max_polls',
        'error',
    )
    return ' '.join(f'{key}={event[key]}' for key in ordered_keys if event.get(key) is not None)


def emit_poll_event(args: argparse.Namespace, event: dict[str, object], *, should_print: bool) -> None:
    if args.poll_log:
        append_jsonl(args.poll_log, event)
    if should_print:
        print(format_poll_event(event))


def main() -> int:
    try:
        args = build_parser().parse_args()
        if args.log_every_polls < 0:
            raise ValueError('--log-every-polls must be 0 or greater.')
        max_polls = resolve_max_polls(args.timeout_seconds, args.poll_interval, args.max_polls)
    except ValueError as exc:
        print(str(exc))
        return 1

    base_url = args.base_url.rstrip('/')
    source_path = args.source
    if not source_path.exists():
        print(f'Input file not found: {source_path}')
        return 1

    with source_path.open('rb') as handle:
        response = requests.post(
            f'{base_url}/convert',
            files={'file': (source_path.name, handle, 'application/octet-stream')},
            timeout=60,
        )
    response.raise_for_status()
    payload = response.json()
    job_id = payload['job']['job_id']
    print(f'Submitted job: {job_id}')

    deadline = time.monotonic() + args.timeout_seconds
    started_at = time.monotonic()
    output_path = args.output or source_path.with_suffix('.pdf')
    last_snapshot: dict[str, object] | None = None
    unchanged_polls = 0
    poll_count = 0

    emit_poll_event(
        args,
        build_poll_event(
            event='wait_started',
            started_at=started_at,
            poll_count=0,
            snapshot={'job_id': job_id, 'phase': 'startup', 'status': 'waiting'},
            timeout_seconds=args.timeout_seconds,
            max_polls=max_polls,
        ),
        should_print=True,
    )

    while True:
        poll_count += 1
        if time.monotonic() > deadline:
            emit_poll_event(
                args,
                build_poll_event(
                    event='wait_timeout',
                    started_at=started_at,
                    poll_count=poll_count - 1,
                    snapshot=last_snapshot,
                    unchanged_polls=unchanged_polls,
                    timeout_seconds=args.timeout_seconds,
                    max_polls=max_polls,
                ),
                should_print=True,
            )
            print(f'Timed out while waiting for job {job_id}')
            return 3

        status_response = requests.get(f'{base_url}/jobs/{job_id}', timeout=30)
        status_response.raise_for_status()
        job = status_response.json()['job']
        snapshot = build_job_snapshot(job_id, job)
        changed = snapshot.get('state_key') != (last_snapshot or {}).get('state_key')
        unchanged_polls = 0 if changed else unchanged_polls + 1
        should_print = changed or (args.log_every_polls > 0 and unchanged_polls > 0 and unchanged_polls % args.log_every_polls == 0)
        emit_poll_event(
            args,
            build_poll_event(
                event='state_change' if changed else 'poll_heartbeat',
                started_at=started_at,
                poll_count=poll_count,
                snapshot=snapshot,
                unchanged_polls=unchanged_polls,
                timeout_seconds=args.timeout_seconds,
                max_polls=max_polls,
            ),
            should_print=should_print,
        )
        last_snapshot = snapshot

        if job['status'] == 'succeeded':
            emit_poll_event(
                args,
                build_poll_event(
                    event='wait_finished',
                    started_at=started_at,
                    poll_count=poll_count,
                    snapshot=snapshot,
                    timeout_seconds=args.timeout_seconds,
                    max_polls=max_polls,
                ),
                should_print=True,
            )
            result = requests.get(f'{base_url}/jobs/{job_id}/result', timeout=60)
            result.raise_for_status()
            output_path.write_bytes(result.content)
            print(f'Downloaded PDF to {output_path}')
            return 0

        if job['status'] == 'failed':
            emit_poll_event(
                args,
                build_poll_event(
                    event='wait_finished',
                    started_at=started_at,
                    poll_count=poll_count,
                    snapshot=snapshot,
                    timeout_seconds=args.timeout_seconds,
                    max_polls=max_polls,
                ),
                should_print=True,
            )
            print(f"Job failed: {job.get('error')}")
            return 2

        if poll_count >= max_polls:
            emit_poll_event(
                args,
                build_poll_event(
                    event='wait_poll_limit_reached',
                    started_at=started_at,
                    poll_count=poll_count,
                    snapshot=last_snapshot,
                    unchanged_polls=unchanged_polls,
                    timeout_seconds=args.timeout_seconds,
                    max_polls=max_polls,
                ),
                should_print=True,
            )
            print(f'Poll limit reached while waiting for job {job_id}')
            return 3

        time.sleep(args.poll_interval)


if __name__ == '__main__':
    raise SystemExit(main())
