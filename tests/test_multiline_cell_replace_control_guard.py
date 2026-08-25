from __future__ import annotations

import unittest

from app.local_cli_runtime import LocalCliRuntimeError, insert_multiline_text_at_caret_native
from app.local_cli_service import LocalCliService


class _InsertTextSet:
    def __init__(self) -> None:
        self.HSet = object()
        self.Text = ''


class _ParameterSet:
    def __init__(self) -> None:
        self.HInsertText = _InsertTextSet()


class _NativeAction:
    def __init__(self, hwp: '_NativeMultilineHwp') -> None:
        self.hwp = hwp

    def GetDefault(self, action: str, _hset: object) -> bool:
        if action != 'InsertText':
            raise AssertionError(action)
        return True

    def Execute(self, action: str, _hset: object) -> bool:
        if action != 'InsertText':
            raise AssertionError(action)
        text = self.hwp.HParameterSet.HInsertText.Text
        self.hwp.paragraphs[self.hwp.paragraph_index] += text
        self.hwp.actions.append(('InsertText', text))
        return True

    def Run(self, action: str) -> bool:
        if action != 'BreakPara':
            raise AssertionError(action)
        inherited_style = self.hwp.styles[self.hwp.paragraph_index]
        self.hwp.paragraph_index += 1
        self.hwp.paragraphs.insert(self.hwp.paragraph_index, '')
        self.hwp.styles.insert(self.hwp.paragraph_index, inherited_style)
        self.hwp.actions.append(('BreakPara', None))
        return True


class _NativeMultilineHwp:
    def __init__(self) -> None:
        self.paragraphs = ['']
        self.styles = ['source-cell-style']
        self.paragraph_index = 0
        self.actions: list[tuple[str, str | None]] = []
        self.set_text_file_calls = 0
        self.HParameterSet = _ParameterSet()
        self.HAction = _NativeAction(self)

    def set_text_file(self, *_args: object, **_kwargs: object) -> bool:
        self.set_text_file_calls += 1
        raise AssertionError('set_text_file must not be used for multiline cell replacement')


class _Control:
    def __init__(self, ctrl_id: str, type_name: str | None = None) -> None:
        self.CtrlID = ctrl_id
        self.Type = type_name
        self.CtrlInstID = ''


class _ControlHwp:
    def __init__(self, controls: list[_Control]) -> None:
        self.ctrl_list = controls


class MultilineCellReplaceControlGuardTests(unittest.TestCase):
    def test_multiline_native_insertion_uses_inserttext_and_breakpara_only(self) -> None:
        hwp = _NativeMultilineHwp()

        proof = insert_multiline_text_at_caret_native(
            hwp,
            'Operating expense\r\nEquipment lease\nProcessing fee',
        )

        self.assertEqual(hwp.paragraphs, ['Operating expense', 'Equipment lease', 'Processing fee'])
        self.assertEqual(hwp.styles, ['source-cell-style'] * 3)
        self.assertEqual(
            hwp.actions,
            [
                ('InsertText', 'Operating expense'),
                ('BreakPara', None),
                ('InsertText', 'Equipment lease'),
                ('BreakPara', None),
                ('InsertText', 'Processing fee'),
            ],
        )
        self.assertEqual(hwp.set_text_file_calls, 0)
        self.assertEqual(proof['strategy'], 'hancom-native-inserttext-breakpara')
        self.assertEqual(proof['line_count'], 3)
        self.assertEqual(proof['paragraph_break_count'], 2)

    def test_control_guard_reports_39_to_44_cold_drift_to_operator(self) -> None:
        service = object.__new__(LocalCliService)
        hwp = _ControlHwp([_Control('secd') for _ in range(38)] + [_Control('tbl')])
        before = service._capture_control_map_signature(hwp)
        self.assertEqual(before['control_count'], 39)

        hwp.ctrl_list.extend(_Control('cold') for _ in range(5))
        after = service._capture_control_map_signature(hwp)
        self.assertEqual(after['control_count'], 44)

        with self.assertRaisesRegex(LocalCliRuntimeError, r'CONTROL_DRIFT.*39.*44.*cold'):
            service._assert_control_map_unchanged(before=before, after=after, operation='cell-replace A6')

    def test_control_guard_accepts_exact_count_and_type_sequence(self) -> None:
        service = object.__new__(LocalCliService)
        hwp = _ControlHwp([_Control('secd'), _Control('tbl'), _Control('gso')])
        before = service._capture_control_map_signature(hwp)
        after = service._capture_control_map_signature(hwp)

        proof = service._assert_control_map_unchanged(before=before, after=after, operation='cell-replace A10')

        self.assertTrue(proof['passed'])
        self.assertEqual(proof['before_control_count'], 3)
        self.assertEqual(proof['after_control_count'], 3)
        self.assertEqual(proof['before_type_sequence_sha256'], proof['after_type_sequence_sha256'])


if __name__ == '__main__':
    unittest.main()
