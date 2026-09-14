from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from queue import Queue

from app.local_cli_runtime import LocalCliLiveSession, LocalCliRuntimeError
from app.local_cli_service import LocalCliService
from app.readiness import build_pdf_renderer_check
from local_cli_v1.main import _record_export_proof_manifest_state
from local_cli_v1.bundles import bundle_help


ROOT = Path(__file__).resolve().parents[1]


class _FillAttr:
    def __init__(self) -> None:
        self.Type = None
        self.WinBrushFaceColor = None


class _FillParameterSet:
    def __init__(self) -> None:
        self.FillAttr = _FillAttr()
        self.HSet = self
        self.items: dict[str, object] = {}

    def SetItem(self, key: str, value: object) -> None:
        self.items[key] = value


class _FillAction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def GetDefault(self, action_name: str, hset: object) -> bool:
        self.calls.append(('GetDefault', action_name))
        return True

    def Execute(self, action_name: str, hset: object) -> bool:
        self.calls.append(('Execute', action_name))
        return True


class _FillFallbackHwp:
    def __init__(self) -> None:
        self.HAction = _FillAction()
        self.HParameterSet = SimpleNamespace(HCellBorderFill=_FillParameterSet())

    def RGBColor(self, red: int, green: int, blue: int) -> tuple[int, int, int]:
        return red, green, blue


class G16LifecycleRepairTests(unittest.TestCase):
    def test_close_marks_session_closing_before_sentinel_and_rejects_late_work(self) -> None:
        session = object.__new__(LocalCliLiveSession)
        session._closed = threading.Event()
        session._commands = Queue()
        session._state_lock = threading.Lock()
        session._closing = False
        session._close_future = None

        with self.assertRaisesRegex(LocalCliRuntimeError, 'closing'):
            session.close(timeout=0.01)

        self.assertTrue(session._closing)
        with self.assertRaisesRegex(LocalCliRuntimeError, 'closing'):
            session.execute('race-after-close', lambda handle: None, timeout=0.01)
        self.assertEqual(session._commands.qsize(), 1, 'late work must not queue behind the close sentinel')


class G16ProofRepairTests(unittest.TestCase):
    def test_proof_match_uses_selected_paragraph_identity_not_global_occurrence(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        service = LocalCliService(settings=SimpleNamespace(spool_root=Path(temp.name)), interactive_sessions=None)
        hwp = SimpleNamespace(current_page=lambda: 7)
        handle = SimpleNamespace(hwp=hwp, source_filename='source.hwp', session_id='session-1')
        calls: dict[str, object] = {}

        def fake_find_live_match(
            hwp_arg: object,
            *,
            query: str,
            occurrence: int,
            target_identity: dict[str, object] | None = None,
        ) -> dict[str, object]:
            calls.update(hwp=hwp_arg, query=query, occurrence=occurrence, target_identity=target_identity)
            return {
                'snapshot': {'pos': [0, 4, 2], 'selected_pos': None},
                'matched_query': query,
                'selected_text': 'x',
            }

        service._load_active_binding = lambda session_id=None: {'session_id': 'session-1'}  # type: ignore[method-assign]
        service._execute_live = lambda **kwargs: kwargs['handler'](handle)  # type: ignore[method-assign]
        service._live_paragraph_records = lambda handle, purpose: [  # type: ignore[method-assign]
            {'section': 'live-text', 'section_paragraph_index': 1, 'global_index': 1, 'text': 'A x'},
            {'section': 'live-text', 'section_paragraph_index': 2, 'global_index': 2, 'text': 'B x'},
        ]
        service._find_live_match = fake_find_live_match  # type: ignore[method-assign]
        service._update_live_binding = lambda binding, location: binding  # type: ignore[method-assign]
        service._record_local_cli_command = lambda *args, **kwargs: None  # type: ignore[method-assign]

        with patch('app.local_cli_service.snapshot_live_location', return_value={'cursor': {'pos': [1, 2, 3]}}), patch(
            'app.local_cli_service._snapshot_cursor_context', return_value={'pos': [1, 2, 3], 'selected_pos': None}
        ), patch('app.local_cli_service._set_pos'):
            result = service.find(query='x', proof_match=2)

        self.assertEqual(result['proof_match']['number'], 2)
        self.assertEqual(calls['query'], 'x')
        self.assertEqual(calls['occurrence'], 1)
        self.assertEqual(calls['target_identity']['global_paragraph_index'], 2)  # type: ignore[index]
        self.assertEqual(calls['target_identity']['normalized_hash'], result['proof_match']['normalized_hash'])


class G16ExportProofRepairTests(unittest.TestCase):
    def test_new_export_clears_all_older_proof_paths_when_pages_are_omitted(self) -> None:
        old_state = {
            'last_export_path': 'old.pdf',
            'last_export_manifest_path': 'old-manifest.json',
            'last_export_proof_manifest_path': 'old-proof-manifest.json',
            'last_export_proof_contact_sheet_path': 'old.png',
            'last_export_proof_page_paths': ['old-p1.png'],
            'last_export_proof_generation': 'old-generation',
        }

        updated = _record_export_proof_manifest_state(
            old_state,
            {
                'exported_pdf_path': 'new.pdf',
                'manifest_path': 'new-manifest.json',
                'pages': [],
                'export_generation': 'new-generation',
            },
        )

        self.assertEqual(updated['last_export_path'], 'new.pdf')
        self.assertEqual(updated['last_export_manifest_path'], 'new-manifest.json')
        self.assertEqual(updated['last_export_proof_generation'], 'new-generation')
        self.assertNotIn('old.png', updated.values())
        self.assertNotIn('old-p1.png', updated.get('last_export_proof_page_paths', []))
        self.assertNotIn('old-proof-manifest.json', updated.values())


class G16FormattingRepairTests(unittest.TestCase):
    def test_fill_fallback_sets_fill_attribute_before_cell_fill_execute(self) -> None:
        hwp = _FillFallbackHwp()
        service = object.__new__(LocalCliService)

        result = service._bundle_apply_cell_fill_color(hwp, '#12ABEF')

        parameter_set = hwp.HParameterSet.HCellBorderFill
        self.assertEqual(parameter_set.FillAttr.Type, 1)
        self.assertEqual(parameter_set.FillAttr.WinBrushFaceColor, (18, 171, 239))
        self.assertEqual(hwp.HAction.calls, [('GetDefault', 'CellFill'), ('Execute', 'CellFill')])
        self.assertEqual(result['method'], 'HAction.Execute(CellFill)')


class G16DiagnosticsAndContractTests(unittest.TestCase):
    def test_public_renderer_status_does_not_disclose_absolute_candidates(self) -> None:
        resolution = SimpleNamespace(
            ok=True,
            path=Path('/home/private-user/Poppler/pdftoppm.exe'),
            source='path',
            detail='resolved',
            candidates=['/home/private-user/Poppler/pdftoppm.exe'],
            as_dict=lambda: {
                'ok': True,
                'path': '/home/private-user/Poppler/pdftoppm.exe',
                'source': 'path',
                'detail': 'resolved',
                'candidates': ['/home/private-user/Poppler/pdftoppm.exe'],
            },
        )
        with patch('app.readiness.resolve_pdftoppm', return_value=resolution):
            result = build_pdf_renderer_check()

        self.assertTrue(result['ok'])
        self.assertEqual(result['source'], 'path')
        self.assertNotIn('path', result)
        self.assertNotIn('candidates', result)

    def test_windows_contracts_bind_ownership_snapshot_actions_processes_and_cleanup(self) -> None:
        common = (ROOT / 'scripts' / 'windows_install_common.psm1').read_text(encoding='utf-8')
        installer = (ROOT / 'scripts' / 'install_windows.ps1').read_text(encoding='utf-8')
        writer = (ROOT / 'scripts' / 'writer_v1.ps1').read_text(encoding='utf-8')
        verifier = (ROOT / 'scripts' / 'verify_windows.ps1').read_text(encoding='utf-8')
        for text, tokens in (
            (common, ('Enter-InstallRootLock', '-NoProjection', 'snapshot_schema', 'action_count', 'xml_action_count', 'RestartCount', 'RestartInterval', 'Stop-NativeProcessTree')),
            (installer, ('candidateRootOwnedByRun', 'backupRootOwnedByRun', 'backup_identity', 'Enter-InstallRootLock', '-ExecutionTimeLimit', '-RestartCount', '-RestartInterval')),
            (writer, ('Test-WriterProcessIdentity', 'process_identity', 'tracked_start_time', 'bootstrap_start_time', 'command_line_hash')),
            (verifier, ('try {', 'finally {', 'fixture_temp_root_cleanup')),
        ):
            for token in tokens:
                with self.subTest(token=token):
                    self.assertIn(token, text)

    def test_documentation_describes_checkout_runtime_state_boundary(self) -> None:
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertNotIn('leaving the source checkout untouched', readme)
        self.assertIn('Git-ignored runtime state is kept inside the checkout', readme)

    def test_cell_format_help_advertises_fill_and_border_operations(self) -> None:
        help_text = bundle_help('cell-format-exact')
        self.assertIn('--fill-color', help_text)
        self.assertIn('--border none', help_text)


if __name__ == '__main__':
    unittest.main()
