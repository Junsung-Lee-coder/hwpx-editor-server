"""Malformed-input behavior of helpers whose broad ``except Exception`` was narrowed."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.runtime_state import _load_json_artifact, read_runtime_status
from local_cli_v1.gate_verdict import _load_render_manifest
from local_cli_v1.output_parser import _int_cap
from local_cli_v1.readback_diff import _float_value


class NarrowedExceptionHandlerTests(unittest.TestCase):
    def test_int_cap_falls_back_on_unparseable_values(self) -> None:
        for value in (None, 'abc', float('inf'), float('nan'), object(), [1]):
            with self.subTest(value=value):
                self.assertEqual(_int_cap(value, 7), 7)
        self.assertEqual(_int_cap('12', 7), 12)

    def test_float_value_returns_none_on_unparseable_values(self) -> None:
        for value in ('abc', object(), [1], 10 ** 400):
            with self.subTest(value=value):
                self.assertIsNone(_float_value(value))
        self.assertEqual(_float_value('1.23456'), 1.235)

    def test_json_readers_return_none_for_malformed_or_undecodable_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad_json = root / 'bad.json'
            bad_json.write_text('{not json', encoding='utf-8')
            bad_bytes = root / 'bad-bytes.json'
            bad_bytes.write_bytes(b'\xff\xfe\x00')
            for path in (bad_json, bad_bytes):
                with self.subTest(path=path.name):
                    self.assertIsNone(_load_render_manifest(path))
                    self.assertIsNone(_load_json_artifact(path))

            job_dir = root / 'job'
            (job_dir / 'metadata').mkdir(parents=True)
            (job_dir / 'metadata' / 'runtime_status.json').write_text('[', encoding='utf-8')
            self.assertIsNone(read_runtime_status(job_dir))

    def test_json_reader_returns_none_when_path_is_a_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(_load_json_artifact(Path(tmp)))


if __name__ == '__main__':
    unittest.main()
