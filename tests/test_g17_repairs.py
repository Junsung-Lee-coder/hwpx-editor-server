from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    from app.local_cli_runtime import LocalCliRuntimeError
    from app.local_cli_service import LocalCliService
except ModuleNotFoundError as exc:  # pragma: no cover - dependency-light static environments
    LocalCliRuntimeError = RuntimeError  # type: ignore[assignment]
    LocalCliService = None  # type: ignore[assignment]
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


ROOT = Path(__file__).resolve().parents[1]


class G17WindowsInstallerRepairTests(unittest.TestCase):
    def test_install_inventory_uses_char_arguments_for_powershell_51(self) -> None:
        text = (ROOT / "scripts" / "install_windows.ps1").read_text(encoding="utf-8")
        start = text.index("function Get-InstallInventory")
        end = text.index("function Ensure-UserScopePoppler", start)
        inventory = text[start:end]

        self.assertIn(".TrimStart([char]92, [char]47)", inventory)
        self.assertNotIn(".TrimStart('\\\\', '/')", inventory)

    def test_preserve_move_captures_backup_identity_before_post_move_fault(self) -> None:
        text = (ROOT / "scripts" / "install_windows.ps1").read_text(encoding="utf-8")
        move = text.index("[System.IO.Directory]::Move($install, $backupRoot)")
        fault = text.index("after-existing-root-compatibility", move)
        post_move = text[move:fault]

        self.assertIn("$backupRootOwnedByRun = $true", post_move)
        self.assertIn("$movedBackupInventory = Get-InstallInventory -Path $backupRoot", post_move)
        self.assertIn("$receipt.backup_identity", post_move)
        self.assertIn("$receipt.backup_inventory = $backupInventory", post_move)


class _FakeHSet:
    def __init__(self, calls: list[Any], owner: "_FakeCellBorderFill | None" = None) -> None:
        self.calls = calls
        self.owner = owner

    def SetItem(self, key: str, value: object) -> None:
        self.calls.append((key, value))
        if self.owner is not None:
            self.owner.attrs[key] = value


class _FakeCellBorderFill:
    def __init__(self, calls: list[object]) -> None:
        self.attrs: dict[str, object] = {}
        self.HSet = _FakeHSet(calls, self)

    def __getattr__(self, name: str) -> object:
        try:
            return self.attrs[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _FakeParameterSet:
    def __init__(self, calls: list[Any]) -> None:
        self.HCellBorderFill = _FakeCellBorderFill(calls)


class _FakeAction:
    def __init__(self, calls: list[object]) -> None:
        self.calls = calls

    def GetDefault(self, action_name: str, hset: object) -> bool:
        self.calls.append(("GetDefault", action_name))
        return True

    def Execute(self, action_name: str, hset: object) -> bool:
        self.calls.append(("Execute", action_name))
        return True


class _ParameterAndWrapperHwp:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.HAction = _FakeAction(self.calls)
        self.HParameterSet = _FakeParameterSet(self.calls)

    def TableCellBorderNo(self) -> bool:
        self.calls.append("TableCellBorderNo")
        return True

    def HwpLineType(self, value: str) -> str:
        return f"line:{value}"

    def HwpLineWidth(self, value: str) -> str:
        return f"width:{value}"


class _MismatchingAction(_FakeAction):
    def __init__(self, calls: list[object], parameter_set: _FakeCellBorderFill) -> None:
        super().__init__(calls)
        self.parameter_set = parameter_set

    def Execute(self, action_name: str, hset: object) -> bool:
        result = super().Execute(action_name, hset)
        self.parameter_set.attrs['BorderTypeRight'] = 'line:Solid'
        return result


class _NativeBorderMismatchHwp(_ParameterAndWrapperHwp):
    def __init__(self) -> None:
        super().__init__()
        self.HAction = _MismatchingAction(self.calls, self.HParameterSet.HCellBorderFill)


class _ParameterWithoutReadbackHwp:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.HAction = _FakeAction(self.calls)
        self.HParameterSet = SimpleNamespace(HCellBorderFill=SimpleNamespace(HSet=_FakeHSet(self.calls)))

    def HwpLineType(self, value: str) -> str:
        return f"line:{value}"

    def HwpLineWidth(self, value: str) -> str:
        return f"width:{value}"


class _WrapperOnlyHwp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def TableCellBorderNo(self) -> bool:
        self.calls.append("TableCellBorderNo")
        return True


@unittest.skipIf(LocalCliService is None, f"local_cli_service import unavailable: {IMPORT_ERROR}")
class G17NativeBorderRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        assert LocalCliService is not None
        self.service = object.__new__(LocalCliService)

    def test_border_none_prefers_explicit_all_side_parameter_set_over_wrapper(self) -> None:
        hwp = _ParameterAndWrapperHwp()

        result = self.service._bundle_apply_cell_border_none(hwp)

        self.assertEqual(result["method"], "HAction.Execute(CellBorderFill border none)")
        self.assertNotIn("TableCellBorderNo", hwp.calls)
        self.assertIn(("ApplyTo", 0), hwp.calls)
        for side in ("Left", "Right", "Top", "Bottom"):
            self.assertIn((f"BorderType{side}", "line:None"), hwp.calls)
            self.assertIn((f"BorderWidth{side}", "width:0.1mm"), hwp.calls)
        self.assertIn(("DiagonalType", 0), hwp.calls)
        self.assertIn(("DiagonalWidth", 0), hwp.calls)
        for flag in (
            "SlashFlag",
            "BackSlashFlag",
            "CounterSlashFlag",
            "CounterBackSlashFlag",
            "CenterLineFlag",
            "CrookedSlashFlag",
            "CrookedSlashFlag1",
            "CrookedSlashFlag2",
        ):
            self.assertIn((flag, 0), hwp.calls)
        self.assertEqual(result["border_sides"], ["Left", "Right", "Top", "Bottom", "Diagonal"])
        self.assertTrue(result["diagonal_none_requested"])
        self.assertTrue(result["readback_proof"]["all_sides_none"])

    def test_border_none_fails_closed_when_all_side_proof_is_unavailable(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, "diagonal"):
            self.service._bundle_apply_cell_border_none(_WrapperOnlyHwp())

        with self.assertRaisesRegex(LocalCliRuntimeError, "readback"):
            self.service._bundle_apply_cell_border_none(_ParameterWithoutReadbackHwp())

    def test_border_none_fails_closed_when_visible_side_readback_is_not_none(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, "BorderTypeRight"):
            self.service._bundle_apply_cell_border_none(_NativeBorderMismatchHwp())


if __name__ == "__main__":
    unittest.main()
