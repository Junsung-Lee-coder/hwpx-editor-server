"""Deterministic RED/GREEN tests for the targeted four-margin getter (WP1/WP2/WP3 service seam).

All tests are fake-backed and portable: no Hancom, COM, worker process or
network backend is started. Fake native mutators (Open/Save/SaveAs/Undo/
Execute/constructor entry points) raise if ever called.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError

from app.models import (
    CellMarginsGetRequest,
    canonical_cell_margins_request_sha256,
)
from app.local_cli_service import LocalCliCellMarginsGetError

SID = 'a' * 32
OTHER_SID = 'b' * 32
ANCHOR = 'R3_EDIT_TARGET'
LIVE_TEXT = 'heading one\n' + ANCHOR + '\ntail'
GEN = f'local-cli/live-document/v1:{SID}:sha256:' + hashlib.sha256(LIVE_TEXT.encode('utf-8')).hexdigest()
GEN_OTHER_SID = f'local-cli/live-document/v1:{OTHER_SID}:sha256:' + hashlib.sha256(LIVE_TEXT.encode('utf-8')).hexdigest()
PROOF_24 = 'a' * 24


def base_target(**overrides: Any) -> dict[str, Any]:
    target: dict[str, Any] = {
        'document_id': SID,
        'expected_document_generation': GEN,
        'target_id': 'ctrl/0/tbl/1',
        'expected_hash': 'sha256:' + PROOF_24,
        'expected_page': 1,
        'expected_cell_page': 1,
        'page_from': 1,
        'page_to': 2,
        'cell_pos': [0, 5, 3],
        'cell_addr': [1, 2],
        'section_anchor': ANCHOR,
    }
    target.update(overrides)
    return target


def base_request(**overrides: Any) -> dict[str, Any]:
    return {'session_id': SID, 'request': base_target(**overrides)}


def canonical_payload_bytes(request: dict[str, Any]) -> bytes:
    validated = CellMarginsGetRequest.model_validate(request)
    payload = {
        'operation': 'cell_margins_get',
        'session_id': validated.session_id,
        'request': validated.request.model_dump(mode='json'),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


class _ForbiddenNativeBase:
    """Any fake runtime touching mutation entry points fails the test."""

    def open(self, *args: Any, **kwargs: Any):  # noqa: D102 - guard
        raise AssertionError('Open must never be called by the getter')

    def Open(self, *args: Any, **kwargs: Any):
        raise AssertionError('Open must never be called by the getter')

    def save(self, *args: Any, **kwargs: Any):
        raise AssertionError('Save must never be called by the getter')

    def Save(self, *args: Any, **kwargs: Any):
        raise AssertionError('Save must never be called by the getter')

    def SaveAs(self, *args: Any, **kwargs: Any):
        raise AssertionError('SaveAs must never be called by the getter')

    def save_hwp_as(self, *args: Any, **kwargs: Any):
        raise AssertionError('save_hwp_as must never be called by the getter')

    def Undo(self, *args: Any, **kwargs: Any):
        raise AssertionError('Undo must never be called by the getter')

    def undo(self, *args: Any, **kwargs: Any):
        raise AssertionError('Undo must never be called by the getter')

    def Execute(self, *args: Any, **kwargs: Any):
        raise AssertionError('HAction.Execute must never be called by the getter')

    def get_cell_margin(self, *args: Any, **kwargs: Any):
        raise AssertionError('cached wrapper get_cell_margin must never be called by the getter')


class _FakeMarginHwp(_ForbiddenNativeBase):
    """A fake live runtime sufficient for the full ordered getter walk."""

    def __init__(self, *, margins: dict[str, int] | None = None, cell_addr: tuple[int, int] = (1, 2),
                 working_copy_path: Path | None = None):
        self.margins = margins or {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}
        self.working_copy_path = working_copy_path
        self.cell_addr = cell_addr
        self.calls: list[tuple[str, tuple[tuple[Any, ...], dict[str, Any]]]] = []
        self.pos = [0, 0, 0]
        self.selected_pos = None
        self.selection_mode = 0
        self.is_modified = False
        self.path = 'C:\\managed\\session\\working-copy.hwpx'
        self.current_page_value = 1
        self.paragraph_text = 'heading one\n' + ANCHOR + '\ntail'
        cell = SimpleNamespace(
            MarginLeft=self.margins['left'],
            MarginRight=self.margins['right'],
            MarginTop=self.margins['top'],
            MarginBottom=self.margins['bottom'],
        )
        shape = SimpleNamespace(HSet=object(), ShapeTableCell=cell)
        self.HParameterSet = SimpleNamespace(HShapeObject=shape)
        self.HAction = SimpleNamespace(GetDefault=self._get_default)
        self.get_default_count = 0
        self.ParentCtrl = _FakeCtrl(inst_id='1')

    def _get_default(self, action_name: str, hset: object) -> bool:
        self.calls.append(('GetDefault', ((action_name,), {})))
        self.get_default_count += 1
        return True

    # --- native surface used by the getter ---------------------------------
    def get_pos(self) -> list[int]:
        return list(self.pos)

    def set_pos(self, list_id: int, para: int, pos: int) -> None:
        self.calls.append(('set_pos', ((list_id, para, pos), {})))
        self.pos = [list_id, para, pos]
        self.selection_mode = 0

    def get_selected_pos(self):
        if self.selected_pos is None:
            return (False,)
        return self.selected_pos

    def SelectionMode(self):
        return self.selection_mode

    def IsModified(self) -> bool:
        return self.is_modified

    def Path(self) -> str:
        if self.working_copy_path is not None:
            return str(self.working_copy_path)
        return self.path

    def get_text_file(self, *, format: str, option: str) -> str:  # noqa: A002 - native spelling
        self.calls.append(('GetTextFile', ((format, option), {})))
        return self.paragraph_text

    def current_page(self) -> int:
        return self.current_page_value

    def HeadCtrl(self):
        return _FakeCtrl(inst_id='1')

    def is_cell(self) -> bool:
        return True

    def get_cell_addr(self, *, as_: str = 'str'):
        self.calls.append(('get_cell_addr', ((), {'as_': as_})))
        if as_ == 'tuple':
            return self.cell_addr
        column, row = self.cell_addr
        letters = ''
        value = column + 1
        while value > 0:
            value, remainder = divmod(value - 1, 26)
            letters = chr(ord('A') + remainder) + letters
        return f'{letters}{row + 1}'

    def get_cell_margin(self, *args: Any, **kwargs: Any):
        raise AssertionError('cached wrapper get_cell_margin must never be called by the getter')


class _FakeCtrl:
    def __init__(self, *, inst_id: str, ctrl_id: str = 'tbl', chain: list['_FakeCtrl'] | None = None):
        self.CtrlID = ctrl_id
        self.CtrlInstID = inst_id
        self._chain = chain or []

    @property
    def Next(self):
        return self._chain[0] if self._chain else None


def working_copy_file(path: Path, content: bytes = b'working-copy-bytes') -> dict[str, Any]:
    path.write_bytes(content)
    return {
        'sha256': 'sha256:' + hashlib.sha256(content).hexdigest(),
        'size_bytes': len(content),
        'basis': 'managed-on-disk-copy-not-live-format-state',
    }


class CellMarginsGetModelTests(unittest.TestCase):
    def test_valid_request_fills_default_and_keeps_fields(self) -> None:
        request = CellMarginsGetRequest.model_validate(base_request())
        self.assertEqual(request.request.max_controls, 100)
        self.assertEqual(request.request.cell_addr, [1, 2])
        self.assertEqual(request.request.section_anchor, ANCHOR)

    def test_explicit_default_max_controls_is_canonically_equal(self) -> None:
        without = CellMarginsGetRequest.model_validate(base_request())
        with_default = CellMarginsGetRequest.model_validate(base_request(max_controls=100))
        self.assertEqual(
            canonical_cell_margins_request_sha256(without),
            canonical_cell_margins_request_sha256(with_default),
        )

    def test_hash_prefix_and_width_forms_reject_or_normalize(self) -> None:
        bare = CellMarginsGetRequest.model_validate(base_request(expected_hash=PROOF_24))
        self.assertEqual(bare.request.normalized_expected_hash(), 'sha256:' + PROOF_24)
        prefixed = CellMarginsGetRequest.model_validate(base_request(expected_hash='sha256:' + PROOF_24))
        self.assertEqual(bare.request.normalized_expected_hash(), prefixed.request.normalized_expected_hash())
        for bad in ('A' * 24, PROOF_24 + 'a' * 40, ' sha256:' + PROOF_24, PROOF_24 + '\n', 'sha256:' + 'g' * 24):
            with self.assertRaises(ValidationError):
                CellMarginsGetRequest.model_validate(base_request(expected_hash=bad))

    def test_cross_session_and_generation_identity_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(document_id=OTHER_SID))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(expected_document_generation=GEN_OTHER_SID))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate({'session_id': OTHER_SID, 'request': base_target()})

    def test_target_locator_shape_is_strict(self) -> None:
        for bad in ('no-inst', 'unknown', 'tbl/1', '2', 'ctrl/0/tbl/a/b', 'ctrl/0/tbl/', ' ctrl/0/tbl/1',
                    'ctrl/0/tbl/1 ', 'ctrl/-1/tbl/1'):
            with self.assertRaises(ValidationError):
                CellMarginsGetRequest.model_validate(base_request(target_id=bad))
        accepted = CellMarginsGetRequest.model_validate(base_request(target_id='ctrl/314/tbl/T4b_9'))
        self.assertEqual(accepted.request.target_id, 'ctrl/314/tbl/T4b_9')

    def test_pages_bounds_ordering_and_membership(self) -> None:
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(page_from=3, page_to=2))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(expected_page=3))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(expected_cell_page=0))
        ok = CellMarginsGetRequest.model_validate(base_request(expected_page=2, expected_cell_page=1))
        self.assertEqual((ok.request.expected_page, ok.request.expected_cell_page), (2, 1))

    def test_booleans_are_not_integers_anywhere(self) -> None:
        for field, value in (
            ('expected_page', True),
            ('expected_cell_page', False),
            ('page_from', True),
            ('max_controls', False),
        ):
            with self.assertRaises(ValidationError):
                CellMarginsGetRequest.model_validate(base_request(**{field: value}))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(cell_addr=[True, 0]))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(cell_pos=[0, True, 0]))

    def test_numeric_strings_extra_fields_and_nested_session_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(expected_page='1'))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(unexpected='x'))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(session_id=OTHER_SID))

    def test_coordinate_bounds_are_native_integers(self) -> None:
        ok = CellMarginsGetRequest.model_validate(base_request(cell_pos=[2147483647, 0, 0],
                                                               cell_addr=[2147483647, 2147483647]))
        self.assertEqual(ok.request.cell_pos[0], 2147483647)
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(cell_pos=[2147483648, 0, 0]))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(cell_pos=[0, -1, 0]))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(cell_addr=[0]))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(cell_pos=[0, 0]))

    def test_anchor_rejects_whitespace_and_control_characters(self) -> None:
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(section_anchor=' lead'))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(section_anchor='trail '))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(section_anchor='a\nb'))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(section_anchor='a\tb'))
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(section_anchor=''))
        kept = CellMarginsGetRequest.model_validate(base_request(section_anchor='double  space  inside'))
        self.assertEqual(kept.request.section_anchor, 'double  space  inside')

    def test_anchor_length_bounds(self) -> None:
        with self.assertRaises(ValidationError):
            CellMarginsGetRequest.model_validate(base_request(section_anchor='x' * 1025))
        ok = CellMarginsGetRequest.model_validate(base_request(section_anchor='x' * 1024))
        self.assertEqual(len(ok.request.section_anchor), 1024)

    def test_non_a1_coordinates_supported(self) -> None:
        ok = CellMarginsGetRequest.model_validate(base_request(cell_addr=[7, 3], cell_pos=[2, 9, 1]))
        self.assertEqual(ok.request.cell_addr, [7, 3])

    def test_canonical_hash_is_stable_and_covers_request(self) -> None:
        request = CellMarginsGetRequest.model_validate(base_request())
        first = canonical_cell_margins_request_sha256(request)
        second = canonical_cell_margins_request_sha256(
            CellMarginsGetRequest.model_validate(base_request()))
        self.assertEqual(first, second)
        self.assertTrue(first.startswith('sha256:'))
        self.assertEqual(len(first), len('sha256:') + 64)
        digest = hashlib.sha256(canonical_payload_bytes(base_request())).hexdigest()
        self.assertEqual(first, 'sha256:' + digest)
        changed = CellMarginsGetRequest.model_validate(base_request(expected_page=2))
        self.assertNotEqual(first, canonical_cell_margins_request_sha256(changed))

    def test_canonical_hash_operation_field_is_backend_operation_name(self) -> None:
        validated = CellMarginsGetRequest.model_validate(base_request())
        payload_bytes = canonical_payload_bytes(base_request())
        payload = json.loads(payload_bytes)
        self.assertEqual(payload['operation'], 'cell_margins_get')
        self.assertEqual(payload['session_id'], validated.session_id)


class _FullWalkHwp(_FakeMarginHwp):
    """Fake live runtime exposing the full ordered native walk surface."""

    cell_paragraph_text: str = ANCHOR

    def get_ctrl_pos(self, ctrl: object, *, option: int = 1) -> tuple[int, int, int]:
        self.calls.append(('get_ctrl_pos', ((option,), {})))
        return (0, 5, 3)

    def select_text(self, start: int, s: int, end: int, e: int, list_id: int) -> None:
        self.calls.append(('select_text', ((start, s, end, e, list_id), {})))
        self.selection_mode = 1

    def get_selected_text(self, *, keep_select: bool = True) -> str:
        self.calls.append(('get_selected_text', ((), {'keep_select': keep_select})))
        self.selection_mode = 0
        return self.cell_paragraph_text


def _proof_basis(index: int = 0, *, inst_id: str = '1', page: int = 1, anchor: tuple[int, int, int] = (0, 5, 3), type_name: str | None = None) -> dict[str, Any]:
    return {
        'index': index,
        'ctrl_id': 'tbl',
        'ctrl_inst_id': inst_id,
        'user_desc': None,
        'anchor_pos': list(anchor),
        'bounds': None,
        'page': page,
        'type': type_name,
    }


def proof_hash(basis: dict[str, Any]) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()[:24]


TARGET_PROOF = proof_hash(_proof_basis())


def make_service(tmp: Path, hwp: Any) -> tuple[Any, dict[str, Any]]:
    from app import local_cli_service as service_mod

    service = object.__new__(service_mod.LocalCliService)
    service._cell_margins_custody_binding = {}  # type: ignore[attr-defined]
    working_copy = tmp / 'working-copy.hwpx'
    working_copy.write_bytes(b'working-copy-bytes')
    binding = {
        'session_id': SID,
        'session_root_path': str(tmp),
        'source_filename': 'fixture.hwpx',
        'working_copy_path': str(working_copy),
        'live_session_bound': True,
        'working_copy_dirty': False,
        'native_command_sequence': 0,
        'document_session_state': 'open',
    }

    def fake_verify(read_binding: Any, path: Any, *, expected: Any = None, readback: Any = None) -> Any:
        if readback is not None:
            readback.update({
                'size_bytes': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        return path

    def fake_execute_live(**kwargs: Any) -> Any:
        handle = SimpleNamespace(
            session_id=SID,
            session_root=tmp,
            working_copy_path=working_copy,
            source_filename='fixture.hwpx',
            log_path=tmp / 'runtime.log',
            hwp=hwp,
        )
        result = kwargs['handler'](handle)
        result['_local_cli_command'] = {
            'command': kwargs.get('command_name'),
            'generation': 0,
            'command_id': 'cmd-cell-margins-1',
            'sequence': 7,
            'state': 'succeeded',
            'semantic_ok': True,
            'recovery': None,
        }
        return result

    service._load_active_binding = lambda session_id=None: dict(binding)  # type: ignore[method-assign]
    service._binding_session_id = lambda b: b['session_id']  # type: ignore[method-assign]
    service._working_copy_path = lambda b: working_copy  # type: ignore[method-assign]
    service._verify_artifact_readback = fake_verify  # type: ignore[method-assign]
    service._execute_live = fake_execute_live  # type: ignore[method-assign]
    service._update_live_binding = lambda b, **kwargs: b  # type: ignore[method-assign]
    service._save_binding = lambda b: b  # type: ignore[method-assign]
    service._record_local_cli_command = lambda *a, **k: None  # type: ignore[method-assign]
    return service, binding


def valid_target_request(**overrides: Any) -> CellMarginsGetRequest:
    overrides.setdefault('target_id', 'ctrl/0/tbl/1')
    overrides.setdefault('expected_hash', TARGET_PROOF)
    return CellMarginsGetRequest.model_validate(base_request(**overrides))


class CellMarginsGetServiceWalkTests(unittest.TestCase):
    """Full ordered walk over the real service orchestration with a fake runtime."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hwp = _FullWalkHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')

    def test_full_walk_success_shape(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        request = valid_target_request()

        result = service.cell_margins_get(session_id=SID, request=request)

        self.assertTrue(result['ok'])
        self.assertTrue(result['semantic_ok'])
        self.assertEqual(result['operation'], 'cell_margins_get')
        self.assertEqual(result['schema_version'], 'local-cli/cell-margins-get/v1')
        self.assertTrue(result['read_only'])
        self.assertFalse(result['dirty'])
        self.assertFalse(result['may_have_mutated'])
        self.assertEqual(result['session_id'], SID)
        self.assertEqual(result['document_id'], SID)
        self.assertEqual(result['unit'], 'hwpunit')
        self.assertEqual(result['units_per_inch'], 7200)
        self.assertEqual(result['side_order'], ['left', 'right', 'top', 'bottom'])
        self.assertEqual(result['margins_hu'], {'left': 510, 'right': 510, 'top': 141, 'bottom': 141})
        provenance = result['provenance']
        self.assertTrue(provenance['refresh_succeeded'])
        self.assertFalse(provenance['cache_used'])
        self.assertEqual(provenance['refresh_method'], 'HAction.GetDefault')
        self.assertEqual(provenance['refresh_action'], 'TablePropertyDialog')
        state = result['state']
        self.assertFalse(state['document_modified_before'])
        self.assertFalse(state['document_modified_after'])
        self.assertTrue(state['document_state_unchanged'])
        self.assertTrue(state['navigation_restored'])
        self.assertFalse(state['mutation_attempted'])
        self.assertEqual(result['command']['state'], 'succeeded')
        self.assertEqual(result['command']['command_id'], 'cmd-cell-margins-1')
        self.assertEqual(result['command']['sequence'], 7)
        target_payload = result['target']
        self.assertEqual(target_payload['target_id'], 'ctrl/0/tbl/1')
        self.assertEqual(target_payload['ctrl_inst_id'], '1')
        self.assertEqual(target_payload['section_binding'], 'literal-in-target-cell-paragraph')
        self.assertEqual(target_payload['cell_addr'], [1, 2])
        self.assertEqual(target_payload['cell_pos'], [0, 5, 3])
        self.assertEqual(
            target_payload['section_anchor_sha256'],
            'sha256:' + hashlib.sha256(ANCHOR.encode('utf-8')).hexdigest(),
        )
        working_copy_file = result['working_copy_file']
        self.assertTrue(working_copy_file['size_bytes'] > 0)
        self.assertTrue(working_copy_file['basis'], 'managed-on-disk-copy-not-live-format-state')
        self.assertNotIn('path', working_copy_file)
        self.assertEqual(result['request_sha256'], canonical_cell_margins_request_sha256(request))

    def test_native_refresh_ran_exactly_once_per_fresh_read(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        service.cell_margins_get(session_id=SID, request=valid_target_request())
        refreshes = [c for c in self.hwp.calls if c[0] == 'GetDefault']
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(refreshes[0][1][0][0], 'TablePropertyDialog')

    def test_position_restored_after_read(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        self.hwp.pos = [0, 1, 0]
        service.cell_margins_get(session_id=SID, request=valid_target_request())
        set_positions = [c[1][0] for c in self.hwp.calls if c[0] == 'set_pos']
        self.assertEqual(set_positions[-1], (0, 1, 0))

    def test_stale_native_refresh_is_not_a_success(self) -> None:
        class _StaleHwp(_FullWalkHwp):
            def _get_default(self, action_name: str, hset: object) -> bool:
                return False

        hwp = _StaleHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        service, _binding = make_service(Path(self.tmp.name), hwp)

        result = service.cell_margins_get(session_id=SID, request=valid_target_request())

        self.assertFalse(result['ok'])
        self.assertIsNone(result.get('observation', None) if 'observation' in result else None)
        self.assertEqual(result['error']['code'], 'NATIVE_MARGIN_UNAVAILABLE')
        self.assertFalse(result['may_have_mutated'])
        self.assertFalse(result['dirty'])

    def test_wrong_cell_page_fails(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        result = service.cell_margins_get(
            session_id=SID,
            request=valid_target_request(expected_cell_page=2),
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'PAGE_MISMATCH')

    def test_wrong_generation_fails_before_navigation(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        other_digest = hashlib.sha256(b'different\n').hexdigest()
        request = CellMarginsGetRequest.model_validate(base_request(
            target_id='ctrl/0/tbl/1',
            expected_hash=TARGET_PROOF,
            expected_document_generation=f'local-cli/live-document/v1:{SID}:sha256:{other_digest}',
        ))
        result = service.cell_margins_get(session_id=SID, request=request)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'DOCUMENT_GENERATION_MISMATCH')
        self.assertFalse(any(c[0] == 'GetDefault' for c in self.hwp.calls))
        self.assertEqual([c[0] for c in self.hwp.calls if c[0] == 'GetTextFile'], ['GetTextFile'])

    def test_stale_target_hash_fails(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        result = service.cell_margins_get(
            session_id=SID,
            request=valid_target_request(expected_hash='sha256:' + '0' * 24),
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'TARGET_HASH_MISMATCH')

    def test_non_table_target_fails(self) -> None:
        class _NoInstCtrlHwp(_FullWalkHwp):
            def HeadCtrl(self):
                return _FakeCtrl(inst_id=None)

        hwp = _NoInstCtrlHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        service, _binding = make_service(Path(self.tmp.name), hwp)
        basis = dict(_proof_basis(), ctrl_inst_id='no-inst')
        request = valid_target_request(expected_hash=proof_hash(basis))
        target_dict = dict(request.request.model_dump(mode='json'), target_id='ctrl/0/tbl/no-inst')
        from app.models import CellMarginsGetTarget
        resolver_target = CellMarginsGetTarget.model_validate(target_dict)
        with self.assertRaises(LocalCliCellMarginsGetError) as raised:
            service._cell_margins_resolve_target(hwp, target=resolver_target)
        self.assertEqual(raised.exception.primary_code, 'TARGET_NOT_TABLE')

    def test_wrong_locator_counts_as_not_found(self) -> None:
        class _GsoOnlyHwp(_FullWalkHwp):
            def HeadCtrl(self):
                return _FakeCtrl(inst_id='9', ctrl_id='gso')

        hwp = _GsoOnlyHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        service, _binding = make_service(Path(self.tmp.name), hwp)
        with self.assertRaises(LocalCliCellMarginsGetError) as raised:
            service._cell_margins_resolve_target(hwp, target=valid_target_request().request)
        self.assertEqual(raised.exception.primary_code, 'TARGET_NOT_FOUND')

    def test_wrong_cell_address_fails(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        result = service.cell_margins_get(
            session_id=SID,
            request=valid_target_request(cell_addr=[0, 0]),
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'CELL_TARGET_MISMATCH')

    def test_duplicate_anchor_in_paragraph_fails(self) -> None:
        hwp = _FullWalkHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        hwp.cell_paragraph_text = ANCHOR + ' and ' + ANCHOR
        service, _binding = make_service(Path(self.tmp.name), hwp)
        result = service.cell_margins_get(session_id=SID, request=valid_target_request())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'SECTION_ANCHOR_MISMATCH')

    def test_missing_anchor_in_paragraph_fails(self) -> None:
        hwp = _FullWalkHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        hwp.cell_paragraph_text = 'something else entirely'
        service, _binding = make_service(Path(self.tmp.name), hwp)
        result = service.cell_margins_get(session_id=SID, request=valid_target_request())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'SECTION_ANCHOR_MISMATCH')

    def test_active_selection_refused_before_navigation(self) -> None:
        class _SelectedHwp(_FullWalkHwp):
            def get_selected_pos(self):
                return (True, 0, 1, 5)

        hwp = _SelectedHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        service, _binding = make_service(Path(self.tmp.name), hwp)
        result = service.cell_margins_get(session_id=SID, request=valid_target_request())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'ACTIVE_SELECTION_UNSUPPORTED')
        self.assertFalse(any(c[0] == 'GetDefault' for c in hwp.calls))
        self.assertFalse(any(c[0] == 'set_pos' for c in hwp.calls))

    def test_document_drift_between_checks_suppresses_values(self) -> None:
        class _DriftHwp(_FullWalkHwp):
            def get_text_file(self, *, format: str, option: str) -> str:  # noqa: A002
                self.calls.append(('GetTextFile', ((format, option), {})))
                self._reads = getattr(self, '_reads', 0) + 1
                if self._reads >= 2:
                    self.paragraph_text = 'changed\n' + ANCHOR + '\ntail'
                return self.paragraph_text

        hwp = _DriftHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        service, _binding = make_service(Path(self.tmp.name), hwp)
        result = service.cell_margins_get(session_id=SID, request=valid_target_request())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'DOCUMENT_CHANGED_DURING_READ')

    def test_restore_failure_suppresses_values(self) -> None:
        class _RestoreFailHwp(_FullWalkHwp):
            def set_pos(self, list_id: int, para: int, pos: int) -> None:
                self.calls.append(('set_pos', ((list_id, para, pos), {})))
                if para == 0 and pos == 0:
                    raise RuntimeError('simulated SetPos failure')

        hwp = _RestoreFailHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        service, _binding = make_service(Path(self.tmp.name), hwp)
        result = service.cell_margins_get(session_id=SID, request=valid_target_request())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'NAVIGATION_RESTORE_FAILED')

    def test_failed_command_state_blocks_success(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)

        def failing_execute_live(**kwargs: Any) -> Any:
            handle = SimpleNamespace(
                session_id=SID,
                session_root=Path(self.tmp.name),
                working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx',
                source_filename='fixture.hwpx',
                log_path=Path(self.tmp.name) / 'runtime.log',
                hwp=self.hwp,
            )
            kwargs['handler'](handle)
            return {
                '_local_cli_command': {
                    'command': 'cell_margins_get',
                    'generation': 0,
                    'command_id': 'cmd-cell-margins-2',
                    'sequence': 8,
                    'state': 'pending',
                    'semantic_ok': None,
                    'recovery': None,
                },
            }

        service._execute_live = failing_execute_live  # type: ignore[method-assign]
        result = service.cell_margins_get(session_id=SID, request=valid_target_request())
        self.assertFalse(result['ok'])
        self.assertEqual(result['command']['state'], 'pending')

    def test_already_dirty_document_reads_true_to_true(self) -> None:
        class _DirtyHwp(_FullWalkHwp):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self.is_modified = True

        hwp = _DirtyHwp(working_copy_path=Path(self.tmp.name) / 'working-copy.hwpx')
        service, _binding = make_service(Path(self.tmp.name), hwp)
        result = service.cell_margins_get(session_id=SID, request=valid_target_request())
        self.assertTrue(result['ok'])
        self.assertTrue(result['state']['document_modified_before'])
        self.assertTrue(result['state']['document_modified_after'])
        self.assertFalse(result['dirty'])

    def test_cached_wrapper_margin_getter_is_never_used(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        service.cell_margins_get(session_id=SID, request=valid_target_request())
        self.assertFalse(any(c[0] == 'get_cell_margin' for c in self.hwp.calls))

    def test_open_save_undo_execute_never_called(self) -> None:
        service, _binding = make_service(Path(self.tmp.name), self.hwp)
        service.cell_margins_get(session_id=SID, request=valid_target_request())
        forbidden = {'open', 'Open', 'save', 'Save', 'SaveAs', 'save_hwp_as', 'Undo', 'undo', 'Execute'}
        self.assertFalse(any(c[0] in forbidden for c in self.hwp.calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
