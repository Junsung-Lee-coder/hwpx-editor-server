from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from app.local_cli_runtime import LocalCliRuntimeError
    from app.local_cli_service import (
        LocalCliMutationError,
        LocalCliService,
        _require_observed_cell_format_mutation,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - dependency-light static environments
    LocalCliRuntimeError = RuntimeError  # type: ignore[assignment]
    LocalCliMutationError = RuntimeError  # type: ignore[assignment]
    LocalCliService = None  # type: ignore[assignment]
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class _FillHwp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.current_color = (0, 0, 0)
        self.HParameterSet = _FillParameterRoot(self)
        self.HAction = _FillAction(self)

    def cell_fill(self, face_color: tuple[int, int, int]) -> bool:
        self.calls.append(('cell_fill', face_color))
        self.current_color = face_color
        return True


class _FillAttr:
    def __init__(self) -> None:
        self.Type = 0
        self.WinBrushFaceColor: object = None


class _FillCellBorderFill:
    def __init__(self, owner: object) -> None:
        self.owner = owner
        self.FillAttr = _FillAttr()
        self.HSet = self


class _FillParameterRoot:
    def __init__(self, owner: object) -> None:
        self.HCellBorderFill = _FillCellBorderFill(owner)


class _FillAction:
    def __init__(self, owner: object) -> None:
        self.owner = owner
        self.calls: list[tuple[str, str]] = []
        self.get_default_result: bool | None = True
        self.execute_result: bool | None = True
        self.apply_color = True

    def GetDefault(self, action_name: str, hset: object) -> bool | None:
        self.calls.append(('GetDefault', action_name))
        if action_name == 'CellFill' and self.get_default_result is not False:
            attr = getattr(hset, 'FillAttr')
            attr.WinBrushFaceColor = getattr(self.owner, 'current_color')
        return self.get_default_result

    def Execute(self, action_name: str, hset: object) -> bool | None:
        self.calls.append(('Execute', action_name))
        if action_name == 'CellFill' and self.execute_result is not False and self.apply_color:
            color = getattr(hset, 'FillAttr').WinBrushFaceColor
            setattr(self.owner, 'current_color', tuple(color))
        return self.execute_result


class _FallbackFillHwp:
    def __init__(self) -> None:
        self.current_color = (18, 171, 239)
        self.HParameterSet = _FillParameterRoot(self)
        self.HAction = _FillAction(self)

    def RGBColor(self, red: int, green: int, blue: int) -> tuple[int, int, int]:
        return red, green, blue


class _FalseFillHwp(_FillHwp):
    def cell_fill(self, face_color: tuple[int, int, int]) -> bool:
        self.calls.append(('cell_fill', face_color))
        return False


class _ZeroFillHwp(_FillHwp):
    def cell_fill(self, face_color: tuple[int, int, int]) -> int:  # type: ignore[override]
        self.calls.append(('cell_fill', face_color))
        return 0


class _MismatchedFillHwp(_FillHwp):
    def cell_fill(self, face_color: tuple[int, int, int]) -> bool:
        self.calls.append(('cell_fill', face_color))
        self.current_color = (1, 2, 3) if len(self.calls) == 1 else face_color
        return True


class _MarginHwp:
    def __init__(self, *, result: bool | None = True, raise_after_apply: bool = False) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.result = result
        self.raise_after_apply = raise_after_apply
        self.margins = {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}

    def set_cell_margin(self, *args: object, **kwargs: object) -> bool | None:
        self.calls.append((args, kwargs))
        values = dict(zip(('left', 'right', 'top', 'bottom'), args[:4]))
        self.margins.update(values)
        if self.raise_after_apply:
            raise RuntimeError('simulated failure after mutation')
        return self.result


class _VerticalReadbackHwp:
    def __init__(self, *, default_result: bool | None = False, value: int = 1) -> None:
        self.HAction = SimpleNamespace(GetDefault=self._get_default)
        self.HParameterSet = SimpleNamespace(
            HShapeObject=SimpleNamespace(HSet=object(), ShapeTableCell=SimpleNamespace(VertAlign=value))
        )
        self.default_result = default_result

    def _get_default(self, action_name: str, hset: object) -> bool | None:
        return self.default_result


class _BorderHwp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def TableCellBorderNo(self) -> bool:
        self.calls.append('TableCellBorderNo')
        return True


class _FakeHSet:
    def __init__(self, calls: list[tuple[str, object]], owner: '_FakeCellBorderFill | None' = None) -> None:
        self.calls = calls
        self.owner = owner

    def SetItem(self, key: str, value: object) -> None:
        self.calls.append((key, value))
        if self.owner is not None:
            self.owner.attrs[key] = value


class _FakeCellBorderFill:
    def __init__(self, calls: list[tuple[str, object]]) -> None:
        self.attrs: dict[str, object] = {}
        self.HSet = _FakeHSet(calls, self)

    def __getattr__(self, name: str) -> object:
        try:
            return self.attrs[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

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


class _FailingBorderFillAction(_FakeBorderFillAction):
    def __init__(self, calls: list[tuple[str, object]], owner: '_RollbackBorderFillHwp') -> None:
        super().__init__(calls)
        self.owner = owner
        self.execute_count = 0

    def Execute(self, action_name: str, hset: object) -> bool:
        self.calls.append(('Execute', action_name))
        if action_name == 'CellBorderFill':
            self.execute_count += 1
            if self.execute_count == 1:
                self.owner.HParameterSet.HCellBorderFill.attrs['BorderTypeLeft'] = 'line:Solid'
        return True


class _RollbackBorderFillHwp(_BorderFillHwp):
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.HParameterSet = _FakeParameterSet(self.calls)
        parameter = self.HParameterSet.HCellBorderFill
        for side in ('Left', 'Right', 'Top', 'Bottom'):
            parameter.attrs[f'BorderType{side}'] = 'line:Solid'
            parameter.attrs[f'BorderWidth{side}'] = 'width:0.5mm'
        parameter.attrs['DiagonalType'] = 1
        parameter.attrs['DiagonalWidth'] = 'width:0.5mm'
        for flag in (
            'SlashFlag', 'BackSlashFlag', 'CounterSlashFlag', 'CounterBackSlashFlag',
            'CenterLineFlag', 'CrookedSlashFlag', 'CrookedSlashFlag1', 'CrookedSlashFlag2',
        ):
            parameter.attrs[flag] = 1
        parameter.attrs['ApplyTo'] = 1
        self.HAction = _FailingBorderFillAction(self.calls, self)


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
        self.assertEqual(result['readback_proof']['observed_rgb'], [18, 171, 239])

    def test_apply_cell_fill_color_rejects_false_native_wrapper_result(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'returned false'):
            self.service._bundle_apply_cell_fill_color(_FalseFillHwp(), '#12ABEF')

    def test_apply_cell_fill_color_rejects_zero_native_wrapper_result(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'returned false'):
            self.service._bundle_apply_cell_fill_color(_ZeroFillHwp(), '#12ABEF')

    def test_apply_cell_fill_color_rejects_native_readback_mismatch(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'readback'):
            self.service._bundle_apply_cell_fill_color(_MismatchedFillHwp(), '#12ABEF')

    def test_apply_cell_fill_color_rolls_back_after_native_readback_mismatch(self) -> None:
        hwp = _MismatchedFillHwp()

        with self.assertRaises(LocalCliMutationError) as raised:
            self.service._bundle_apply_cell_fill_color(hwp, '#12ABEF')

        self.assertEqual(hwp.current_color, (0, 0, 0))
        self.assertEqual(hwp.calls, [('cell_fill', (18, 171, 239)), ('cell_fill', (0, 0, 0))])
        self.assertTrue(raised.exception.rollback['attempted'])
        self.assertTrue(raised.exception.rollback['succeeded'])
        self.assertFalse(raised.exception.mutation_may_have_persisted)

    def test_apply_cell_fill_color_rejects_false_fallback_get_default(self) -> None:
        hwp = _FallbackFillHwp()
        hwp.HAction.get_default_result = False

        with self.assertRaisesRegex(LocalCliRuntimeError, 'GetDefault returned false'):
            self.service._bundle_apply_cell_fill_color(hwp, '#12ABEF')

    def test_apply_cell_fill_color_rejects_false_fallback_execute(self) -> None:
        hwp = _FallbackFillHwp()
        hwp.HAction.execute_result = False

        with self.assertRaisesRegex(LocalCliRuntimeError, 'Execute returned false'):
            self.service._bundle_apply_cell_fill_color(hwp, '#12ABEF')

    def test_apply_cell_fill_color_rejects_fallback_readback_mismatch(self) -> None:
        hwp = _FallbackFillHwp()
        hwp.current_color = (1, 2, 3)
        hwp.HAction.apply_color = False

        with self.assertRaisesRegex(LocalCliRuntimeError, 'readback'):
            self.service._bundle_apply_cell_fill_color(hwp, '#12ABEF')

    def test_apply_cell_fill_color_rejects_invalid_hex(self) -> None:
        with self.assertRaises(LocalCliRuntimeError):
            self.service._bundle_apply_cell_fill_color(_FillHwp(), '#XYZ123')

    def test_cell_format_exact_rejects_vertical_alignment_without_distinct_readback(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'no usable native before readback'):
            _require_observed_cell_format_mutation(
                'vertical-align',
                {'cell_addr': [0, 0]},
                {'cell_addr': [0, 0]},
                {},
            )

    def test_cell_format_exact_accepts_distinct_vertical_alignment_readback(self) -> None:
        _require_observed_cell_format_mutation(
            'vertical-align',
            {'vertical_align': {'available': True, 'value': 0, 'name': 'top'}},
            {'vertical_align': {'available': True, 'value': 1, 'name': 'center'}},
            {'vertical_align': {'before': 0, 'after': 1}},
            expected_vertical_align='center',
        )

    def test_cell_format_exact_rejects_vertical_alignment_readback_mismatch(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'mismatched'):
            _require_observed_cell_format_mutation(
                'vertical-align',
                {'vertical_align': {'available': True, 'value': 0, 'name': 'top'}},
                {'vertical_align': {'available': True, 'value': 2, 'name': 'bottom'}},
                {'vertical_align': {'before': 0, 'after': 2}},
                expected_vertical_align='center',
            )

    def test_cell_format_exact_rejects_unchanged_vertical_alignment_readback(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'changed vertical alignment'):
            _require_observed_cell_format_mutation(
                'vertical-align',
                {'vertical_align': {'available': True, 'value': 1, 'name': 'center'}},
                {'vertical_align': {'available': True, 'value': 1, 'name': 'center'}},
                {},
                expected_vertical_align='center',
            )

    def test_cell_format_exact_rejects_unchanged_cell_margin(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'usable native four-side cell-margin readback'):
            _require_observed_cell_format_mutation(
                'set-cell-margin',
                {'cell_margin_hu': 1200},
                {'cell_margin_hu': 1200},
                {},
            )

    def test_uniform_cell_margin_passes_all_four_explicit_values(self) -> None:
        hwp = _MarginHwp()

        result = self.service._bundle_set_uniform_cell_margin(hwp, 1984)

        self.assertEqual(hwp.calls, [((1984, 1984, 1984, 1984), {'as_': 'hwpunit'})])
        self.assertEqual(hwp.margins, {'left': 1984, 'right': 1984, 'top': 1984, 'bottom': 1984})
        self.assertEqual(result['result'], True)

    def test_uniform_cell_margin_does_not_retry_side_effecting_failure(self) -> None:
        hwp = _MarginHwp(raise_after_apply=True)

        with self.assertRaises(LocalCliMutationError) as raised:
            self.service._bundle_set_uniform_cell_margin(hwp, 1984)

        self.assertEqual(len(hwp.calls), 1)
        self.assertTrue(raised.exception.mutation_may_have_persisted)

    def test_uniform_cell_margin_rejects_explicit_false(self) -> None:
        with self.assertRaises(LocalCliMutationError):
            self.service._bundle_set_uniform_cell_margin(_MarginHwp(result=False), 1984)

    def test_cell_format_exact_margin_guard_requires_valid_four_side_readback(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'usable native'):
            _require_observed_cell_format_mutation(
                'set-cell-margin',
                {'cell_margin_hu': {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}},
                {'cell_margin_hu': {'error': 'getter failed'}},
                {},
                expected_cell_margin_hu={'left': 1984, 'right': 1984, 'top': 1984, 'bottom': 1984},
            )

    def test_cell_format_exact_margin_guard_requires_request_derived_value(self) -> None:
        with self.assertRaisesRegex(LocalCliRuntimeError, 'requested value'):
            _require_observed_cell_format_mutation(
                'set-cell-margin',
                {'cell_margin_hu': {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}},
                {'cell_margin_hu': {'left': 1984, 'right': 2, 'top': 0, 'bottom': 0}},
                {'cell_margin_hu': {'before': {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}, 'after': {'left': 1984, 'right': 2, 'top': 0, 'bottom': 0}}},
                expected_cell_margin_hu={'left': 1984, 'right': 1984, 'top': 1984, 'bottom': 1984},
            )

    def test_cell_format_exact_guard_failure_preserves_mutation_truth(self) -> None:
        with self.assertRaises(LocalCliMutationError) as raised:
            _require_observed_cell_format_mutation(
                'set-cell-margin',
                {'cell_margin_hu': {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}},
                {'cell_margin_hu': {'left': 1984, 'right': 2, 'top': 0, 'bottom': 0}},
                {'cell_margin_hu': {'before': {'left': 510, 'right': 510, 'top': 141, 'bottom': 141}, 'after': {'left': 1984, 'right': 2, 'top': 0, 'bottom': 0}}},
                expected_cell_margin_hu={'left': 1984, 'right': 1984, 'top': 1984, 'bottom': 1984},
            )

        self.assertTrue(raised.exception.mutation_may_have_persisted)
        self.assertFalse(raised.exception.rollback['succeeded'])

    def test_table_cell_metrics_marks_invalid_margin_readback_unavailable(self) -> None:
        self.service._bundle_enter_table_cell_for_ctrl = lambda hwp, ctrl: {'is_cell': True}  # type: ignore[method-assign]
        self.service._style_parameter_snapshot = lambda *args, **kwargs: {'values': {}}  # type: ignore[method-assign]
        hwp = SimpleNamespace(
            get_cell_addr=lambda **kwargs: [0, 0],
            get_row_height=lambda **kwargs: 1282,
            get_table_height=lambda **kwargs: 1282,
            get_cell_margin=lambda **kwargs: {'error': 'getter failed'},
            get_table_inside_margin=lambda **kwargs: {'left': 0},
            get_table_outside_margin=lambda **kwargs: {'left': 0},
        )
        self.service._bundle_table_cell_vertical_align = lambda hwp: {  # type: ignore[method-assign]
            'available': True,
            'value': 1,
            'name': 'center',
        }

        with patch('app.local_cli_service._get_pos', return_value=(0, 0, 0)), patch('app.local_cli_service._set_pos'):
            metrics = self.service._bundle_table_cell_metrics(hwp, object())

        self.assertFalse(metrics['cell_margin_hu_available'])
        self.assertFalse(metrics['available'])

    def test_vertical_alignment_getter_rejects_invalid_native_enum(self) -> None:
        result = self.service._bundle_table_cell_vertical_align(_VerticalReadbackHwp(default_result=True, value=9))

        self.assertFalse(result['available'])
        self.assertIn('enum is invalid', result['error'])

    def test_vertical_alignment_getdefault_failure_is_unavailable(self) -> None:
        result = self.service._bundle_table_cell_vertical_align(_VerticalReadbackHwp())

        self.assertFalse(result['available'])
        self.assertIn('GetDefault', result['error'])

    def test_apply_cell_border_none_prefers_cellborderfill_parameter_set(self) -> None:
        hwp = _BorderFillHwp()

        result = self.service._bundle_apply_cell_border_none(hwp)

        self.assertEqual(result['method'], 'HAction.Execute(CellBorderFill border none)')
        self.assertIn(('GetDefault', 'CellBorderFill'), hwp.calls)
        self.assertIn(('Execute', 'CellBorderFill'), hwp.calls)
        self.assertIn(('ApplyTo', 0), hwp.calls)
        self.assertIn(('BorderTypeLeft', 'line:None'), hwp.calls)
        self.assertIn(('BorderWidthLeft', 'width:0.1mm'), hwp.calls)
        self.assertIn(('DiagonalType', 0), hwp.calls)
        self.assertIn(('DiagonalWidth', 0), hwp.calls)
        self.assertTrue(result['readback_proof']['all_sides_none'])

    def test_apply_cell_border_none_rolls_back_after_readback_failure(self) -> None:
        hwp = _RollbackBorderFillHwp()
        original = dict(hwp.HParameterSet.HCellBorderFill.attrs)

        with self.assertRaises(LocalCliMutationError) as raised:
            self.service._bundle_apply_cell_border_none(hwp)

        self.assertEqual(hwp.HParameterSet.HCellBorderFill.attrs, original)
        self.assertTrue(raised.exception.rollback['attempted'])
        self.assertTrue(raised.exception.rollback['succeeded'])
        self.assertFalse(raised.exception.mutation_may_have_persisted)

    def test_apply_cell_border_none_falls_back_to_pyhwpx_wrapper(self) -> None:
        hwp = _BorderHwp()

        with self.assertRaisesRegex(LocalCliRuntimeError, 'diagonal'):
            self.service._bundle_apply_cell_border_none(hwp)

        self.assertEqual(hwp.calls, [])

    def test_apply_cell_border_none_falls_back_to_haction_run(self) -> None:
        hwp = _BorderRunFallbackHwp()

        with self.assertRaisesRegex(LocalCliRuntimeError, 'diagonal'):
            self.service._bundle_apply_cell_border_none(hwp)

        self.assertEqual(hwp.calls, [])


if __name__ == '__main__':
    unittest.main()
