from __future__ import annotations

import unittest

try:
    from app.local_cli_runtime import LocalCliRuntimeError
    from app.local_cli_service import LocalCliService
except ModuleNotFoundError as exc:  # pragma: no cover - dependency-light static environments
    LocalCliRuntimeError = RuntimeError  # type: ignore[assignment]
    LocalCliService = None  # type: ignore[assignment]
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class _FillHwp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def cell_fill(self, face_color: tuple[int, int, int]) -> bool:
        self.calls.append(('cell_fill', face_color))
        return True


class _BorderHwp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def TableCellBorderNo(self) -> bool:
        self.calls.append('TableCellBorderNo')
        return True


class _FakeHSet:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    def SetItem(self, key: str, value: object) -> None:
        self.calls.append((key, value))


class _FakeCellBorderFill:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.HSet = _FakeHSet(calls)
        self.attrs: dict[str, object] = {}

    def __setattr__(self, name: str, value: object) -> None:
        if name in {'HSet', 'attrs'}:
            object.__setattr__(self, name, value)
        else:
            self.attrs[name] = value


class _FakeParameterSet:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.HCellBorderFill = _FakeCellBorderFill(calls)


class _FakeBorderFillAction:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.calls = calls

    def GetDefault(self, action_name: str, hset: object) -> bool:
        self.calls.append(('GetDefault', action_name))
        return True

    def Execute(self, action_name: str, hset: object) -> bool:
        self.calls.append(('Execute', action_name))
        return True

    def Run(self, action_name: str) -> bool:
        self.calls.append(('Run', action_name))
        return True


class _BorderFillHwp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.HAction = _FakeBorderFillAction(self.calls)
        self.HParameterSet = _FakeParameterSet(self.calls)

    def HwpLineType(self, value: str) -> str:
        return f'line:{value}'

    def HwpLineWidth(self, value: str) -> str:
        return f'width:{value}'

    def RGBColor(self, red: int, green: int, blue: int) -> tuple[int, int, int]:
        return red, green, blue


class _RunAction:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def Run(self, action_name: str) -> bool:
        self.calls.append(action_name)
        return action_name == 'TableCellBorderNo'


class _BorderRunFallbackHwp:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.HAction = _RunAction(self.calls)


@unittest.skipIf(LocalCliService is None, f'local_cli_service import unavailable: {IMPORT_ERROR}')
class CellFormatExactServiceHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        assert LocalCliService is not None
        self.service = object.__new__(LocalCliService)

    def test_apply_cell_fill_color_uses_pyhwpx_cell_fill_with_rgb_tuple(self) -> None:
        hwp = _FillHwp()

        result = self.service._bundle_apply_cell_fill_color(hwp, '#12ABEF')

        self.assertEqual(hwp.calls, [('cell_fill', (18, 171, 239))])
        self.assertEqual(result['rgb'], [18, 171, 239])
        self.assertEqual(result['method'], 'cell_fill((r,g,b))')

    def test_apply_cell_fill_color_rejects_invalid_hex(self) -> None:
        with self.assertRaises(LocalCliRuntimeError):
            self.service._bundle_apply_cell_fill_color(_FillHwp(), '#XYZ123')

    def test_apply_cell_border_none_prefers_cellborderfill_parameter_set(self) -> None:
        hwp = _BorderFillHwp()

        result = self.service._bundle_apply_cell_border_none(hwp)

        self.assertEqual(result['method'], 'HAction.Execute(CellBorderFill border none)')
        self.assertIn(('GetDefault', 'CellBorderFill'), hwp.calls)
        self.assertIn(('Execute', 'CellBorderFill'), hwp.calls)
        self.assertIn(('ApplyTo', 1), hwp.calls)
        self.assertIn(('BorderTypeLeft', 'line:None'), hwp.calls)
        self.assertIn(('BorderWidthLeft', 'width:0.1mm'), hwp.calls)

    def test_apply_cell_border_none_falls_back_to_pyhwpx_wrapper(self) -> None:
        hwp = _BorderHwp()

        result = self.service._bundle_apply_cell_border_none(hwp)

        self.assertEqual(hwp.calls, ['TableCellBorderNo'])
        self.assertEqual(result['method'], 'TableCellBorderNo()')

    def test_apply_cell_border_none_falls_back_to_haction_run(self) -> None:
        hwp = _BorderRunFallbackHwp()

        result = self.service._bundle_apply_cell_border_none(hwp)

        self.assertEqual(hwp.calls, ['TableCellBorderNo'])
        self.assertEqual(result['method'], 'HAction.Run(TableCellBorderNo)')


if __name__ == '__main__':
    unittest.main()
