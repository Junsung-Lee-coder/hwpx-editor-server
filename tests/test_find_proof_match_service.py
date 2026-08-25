from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app import local_cli_service as service_mod
from app.local_cli_service import LocalCliService, LocalCliServiceError


class FindProofMatchServiceTests(unittest.TestCase):
    def make_service(self) -> LocalCliService:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = SimpleNamespace(spool_root=Path(tmp.name))
        return LocalCliService(settings=settings, interactive_sessions=None)

    def test_find_proof_match_returns_live_page_evidence_for_numbered_match(self) -> None:
        service = self.make_service()
        hwp = SimpleNamespace(current_page=lambda: 7)
        handle = SimpleNamespace(hwp=hwp, source_filename='source.hwp', session_id='session-1')
        calls: dict[str, Any] = {}
        original_snapshot_live_location = service_mod.snapshot_live_location
        original_snapshot_cursor_context = service_mod._snapshot_cursor_context
        original_set_pos = service_mod._set_pos

        def fake_execute_live(**kwargs):
            return kwargs['handler'](handle)

        def fake_find_live_match(hwp_arg: Any, *, query: str, occurrence: int) -> dict[str, Any]:
            calls['find_live_match'] = {'hwp': hwp_arg, 'query': query, 'occurrence': occurrence}
            return {'snapshot': {'pos': [10, 20, 30], 'selected_pos': None}}

        def fake_set_pos(hwp_arg: Any, a: int, b: int, c: int) -> None:
            calls.setdefault('set_pos', []).append((hwp_arg, a, b, c))

        try:
            service._load_active_binding = lambda session_id=None: {'session_id': 'session-1'}  # type: ignore[method-assign]
            service._execute_live = fake_execute_live  # type: ignore[method-assign]
            service._live_paragraph_records = lambda handle, purpose: [  # type: ignore[method-assign]
                {'section': 'live', 'section_paragraph_index': 1, 'global_index': 1, 'text': 'Repeated target paragraph'},
                {'section': 'live', 'section_paragraph_index': 2, 'global_index': 2, 'text': 'Repeated target paragraph'},
            ]
            service._find_live_match = fake_find_live_match  # type: ignore[method-assign]
            service._update_live_binding = lambda binding, location: binding  # type: ignore[method-assign]
            service._record_local_cli_command = lambda *args, **kwargs: None  # type: ignore[method-assign]
            service_mod.snapshot_live_location = lambda **kwargs: {'cursor': {'pos': [1, 2, 3]}}  # type: ignore[assignment]
            service_mod._snapshot_cursor_context = lambda hwp_arg: {'pos': [1, 2, 3], 'selected_pos': None}  # type: ignore[assignment]
            service_mod._set_pos = fake_set_pos  # type: ignore[assignment]

            result = service.find(query='target', proof_match=2)
        finally:
            service_mod.snapshot_live_location = original_snapshot_live_location  # type: ignore[assignment]
            service_mod._snapshot_cursor_context = original_snapshot_cursor_context  # type: ignore[assignment]
            service_mod._set_pos = original_set_pos  # type: ignore[assignment]

        self.assertTrue(result['ok'])
        self.assertEqual(result['proof_match']['number'], 2)
        self.assertEqual(result['proof_match']['page'], 7)
        self.assertEqual(result['proof_match']['page_evidence']['method'], 'current_page')
        self.assertEqual(calls['find_live_match']['query'], 'Repeated target paragraph')
        self.assertEqual(calls['find_live_match']['occurrence'], 2)
        self.assertEqual(calls['set_pos'][-1][1:], (1, 2, 3))

    def test_find_proof_match_rejects_out_of_range_match(self) -> None:
        service = self.make_service()
        handle = SimpleNamespace(hwp=SimpleNamespace(current_page=lambda: 7), source_filename='source.hwp', session_id='session-1')

        service._load_active_binding = lambda session_id=None: {'session_id': 'session-1'}  # type: ignore[method-assign]
        service._execute_live = lambda **kwargs: kwargs['handler'](handle)  # type: ignore[method-assign]
        service._live_paragraph_records = lambda handle, purpose: [  # type: ignore[method-assign]
            {'section': 'live', 'section_paragraph_index': 1, 'global_index': 1, 'text': 'one target'},
        ]

        with self.assertRaises(LocalCliServiceError) as ctx:
            service.find(query='target', proof_match=2)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn('No find match number 2', ctx.exception.message)


if __name__ == '__main__':
    unittest.main()
