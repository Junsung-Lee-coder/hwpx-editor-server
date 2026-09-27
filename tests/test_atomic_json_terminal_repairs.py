from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

import app.atomic_json as atomic_json
from app.atomic_json import atomic_write_json, path_lock, read_json_object, update_json_object


class AtomicJsonTerminalRepairTests(unittest.TestCase):
    def test_non_finite_payload_is_rejected_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "nested" / "state.json"
            with self.assertRaises(ValueError):
                atomic_write_json(path, {"value": math.nan})
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_non_round_trippable_key_is_rejected_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            with self.assertRaises(ValueError):
                atomic_write_json(path, {1: "numeric key"})  # type: ignore[arg-type]
            self.assertFalse(path.exists())

    def test_update_json_object_uses_strict_serialization(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            with self.assertRaises(ValueError):
                update_json_object(path, lambda current: {**current, "value": math.inf})
            self.assertFalse(path.exists())

    def test_path_lock_registry_is_reclaimed_after_each_use(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            for _ in range(100):
                with path_lock(path):
                    pass
            self.assertEqual(atomic_json._LOCKS, {})

    def test_missing_read_does_not_create_deleted_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "deleted" / "state.json"
            self.assertIsNone(read_json_object(path))
            self.assertFalse(path.parent.exists())

    def test_update_preserves_exact_json_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "state.json"
            update_json_object(path, lambda current: {**current, "value": "ok"})
            first = path.read_bytes()
            update_json_object(path, lambda current: dict(current))
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(json.loads(first), {"value": "ok"})


if __name__ == "__main__":
    unittest.main()
