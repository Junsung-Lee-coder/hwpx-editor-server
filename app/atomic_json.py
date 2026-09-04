"""Small cross-platform helpers for crash-safe JSON state files."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, tuple[threading.RLock, int]] = {}


@contextmanager
def _thread_lock(path: Path) -> Iterator[None]:
    """Hold a ref-counted in-process lock without leaking registry entries."""

    key = os.path.normcase(os.path.abspath(os.fspath(path.expanduser())))
    with _LOCKS_GUARD:
        entry = _LOCKS.get(key)
        if entry is None:
            lock = threading.RLock()
            _LOCKS[key] = (lock, 1)
        else:
            lock, users = entry
            _LOCKS[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _LOCKS_GUARD:
            current = _LOCKS.get(key)
            if current is not None and current[0] is lock:
                if current[1] <= 1:
                    _LOCKS.pop(key, None)
                else:
                    _LOCKS[key] = (lock, current[1] - 1)


def _encode_json_object(payload: dict[str, Any], *, context: str) -> bytes:
    """Return a strict JSON representation before any filesystem mutation."""

    if not isinstance(payload, dict):
        raise ValueError(f'JSON payload must be an object: {context}')
    try:
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + '\n'
        ).encode('utf-8')
        readback = json.loads(encoded.decode('utf-8'))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f'JSON payload is not strictly round-trippable: {context}') from exc
    if not isinstance(readback, dict) or readback != payload:
        raise ValueError(f'JSON payload changed during strict round-trip: {context}')
    return encoded


def _ensure_lock_byte(handle: Any) -> None:
    """Ensure the Windows byte-range lock has one stable byte to lock."""

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b'0')
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    handle.seek(0)


@contextmanager
def path_lock(path: Path) -> Iterator[None]:
    """Serialize writers in-process and, where available, across processes."""

    lock_path = Path(f'{path}.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(lock_path):
        handle = lock_path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt

                _ensure_lock_byte(handle)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            if os.name == 'nt':
                import msvcrt

                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace *path* and verify the exact JSON object read back."""

    destination = Path(path)
    encoded = _encode_json_object(payload, context=str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    with path_lock(destination):
        try:
            with tempfile.NamedTemporaryFile(
                mode='wb',
                prefix=f'.{destination.name}.',
                suffix='.tmp',
                dir=destination.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            with destination.open('rb') as handle:
                readback_bytes = handle.read()
            if readback_bytes != encoded:
                raise OSError(f'JSON readback bytes differed after atomic replace: {destination}')
            readback = json.loads(readback_bytes.decode('utf-8'))
            if readback != payload or not isinstance(readback, dict):
                raise OSError(f'JSON readback object differed after atomic replace: {destination}')
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


def read_json_object(path: Path) -> dict[str, Any] | None:
    """Read one JSON object under the same lock used for replacement."""

    source = Path(path)
    # A read of a state file that was already removed must not create its
    # parent directory merely to create a lock file.  This matters when a
    # server-managed session root is deleted immediately before binding
    # cleanup reads the now-absent binding path.
    if not source.parent.exists():
        return None
    with path_lock(source):
        if not source.exists():
            return None
        try:
            with source.open('rb') as handle:
                payload = json.loads(handle.read().decode('utf-8'))
        except Exception as exc:
            raise ValueError(f'Could not parse JSON state file: {source}') from exc
        if not isinstance(payload, dict):
            raise ValueError(f'JSON state file must contain an object: {source}')
        return payload


def update_json_object(
    path: Path,
    updater: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    default: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically read, update, replace, and verify one JSON object.

    Unlike a separate ``read_json_object`` followed by ``atomic_write_json``,
    the callback runs while the cross-process path lock is held.  This keeps
    read-modify-write state updates from clobbering a concurrent writer.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with path_lock(destination):
        if destination.exists():
            try:
                with destination.open('rb') as handle:
                    current = json.loads(handle.read().decode('utf-8'))
            except Exception as exc:
                raise ValueError(f'Could not parse JSON state file: {destination}') from exc
            if not isinstance(current, dict):
                raise ValueError(f'JSON state file must contain an object: {destination}')
        else:
            current = dict(default or {})
        updated = updater(dict(current))
        if not isinstance(updated, dict):
            raise ValueError(f'JSON updater must return an object: {destination}')
        encoded = _encode_json_object(updated, context=str(destination))
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='wb',
                prefix=f'.{destination.name}.',
                suffix='.tmp',
                dir=destination.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
            with destination.open('rb') as handle:
                readback_bytes = handle.read()
            if readback_bytes != encoded:
                raise OSError(f'JSON readback bytes differed after atomic update: {destination}')
            readback = json.loads(readback_bytes.decode('utf-8'))
            if readback != updated or not isinstance(readback, dict):
                raise OSError(f'JSON readback object differed after atomic update: {destination}')
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
        return updated
