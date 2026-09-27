from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest

from local_cli_v1.state import StatePersistenceError, load_state, save_state


class AtomicStatePersistenceTests(unittest.TestCase):
    def test_save_state_replaces_atomically_and_reads_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'state.json'
            payload = {'session_id': 'session-1', 'generation': 4}

            self.assertEqual(save_state(payload, path), path)
            self.assertEqual(load_state(path), payload)
            self.assertFalse(list(path.parent.glob('state.json.*.tmp')))

    def test_load_state_fails_closed_on_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'state.json'
            path.write_text('{not-json', encoding='utf-8')

            with self.assertRaises(StatePersistenceError):
                load_state(path)

    def test_concurrent_state_writers_leave_valid_complete_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'state.json'
            errors: list[BaseException] = []

            def write(index: int) -> None:
                try:
                    save_state({'writer': index, 'values': list(range(index + 1))}, path)
                except BaseException as exc:  # pragma: no cover - diagnostic
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            payload = load_state(path)
            self.assertIsInstance(payload, dict)
            self.assertIn('writer', payload)
            self.assertEqual(payload['values'], list(range(int(payload['writer']) + 1)))
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), payload)
            self.assertFalse(list(path.parent.glob('state.json.*.tmp')))


if __name__ == '__main__':
    unittest.main()
