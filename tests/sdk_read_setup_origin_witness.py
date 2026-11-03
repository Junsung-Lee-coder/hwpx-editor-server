"""Supplemental SDK setup-read origins, with the same real public worker.

No production helper is replaced. Trap decorates only fake parameter objects.
"""
import unittest
from app import native_numbering as nn
from tests.test_required_sdk_read_origin import Probe, RequiredReadOrigin, scenario, fake_finder
from tests.test_bundle_step_local_refusal import style
from tests.test_guarded_bundle_observations import run
from tests.test_native_table import binding


class Trap:
    def __init__(self, value, probe, path, target, cause):
        object.__setattr__(self, '_data', (value, probe, path, target, cause))

    def __getattr__(self, name):
        value, probe, path, target, cause = self._data
        path = path + '.' + name
        if path == target:
            probe.fired = True
            probe.call_mark = len(probe.hwp.calls)
            raise cause
        result = getattr(value, name)
        if name in {'HCellBorderFill', 'HShapeObject', 'HSet', 'ShapeTableCell', 'FillAttr', 'HInsertText'}:
            return Trap(result, probe, path, target, cause)
        return result

    def __setattr__(self, name, value):
        obj, probe, path, target, cause = self._data
        if path + '.' + name == target:
            probe.fired = True
            probe.call_mark = len(probe.hwp.calls)
            raise cause
        setattr(obj, name, value)


class SetupReadOrigins(unittest.TestCase):
    own = RequiredReadOrigin.own
    assert_stopped = RequiredReadOrigin.assert_stopped

    def test_parameter_setup_read_origins_before_first_mutator(self):
        for target in ('HParameterSet.HCellBorderFill.FillAttr.type',
                       'HParameterSet.HShapeObject.HSet',
                       'HParameterSet.HShapeObject.ShapeTableCell'):
            with self.subTest(target=target):
                hwp, step, entry = scenario('style')
                if 'HShapeObject' in target:
                    step = dict(op='table_width_exact', **binding(hwp, '1012'), column_widths_mm=[20] * hwp.by_inst('1012').cols)
                    entry = '_bundle_table_width_exact'
                probe = Probe(hwp, entry=entry)
                cause = nn.NativeNumberingRefusal('synthetic parameter SDK origin', code='table_not_found')
                probe.cause = cause
                hwp.HParameterSet = Trap(hwp.HParameterSet, probe, 'HParameterSet', target, cause)
                owner = self.own(probe, working_copy_dirty=True)
                result = run(owner, step, {'op': 'table_inventory'})
                self.assert_stopped(owner, probe, result, False, True)

    def test_paragraph_parameter_write_cannot_forge_semantic_refusal(self):
        hwp, step, entry = scenario('paragraph')
        probe = Probe(hwp, entry=entry)
        cause = nn.NativeNumberingRefusal('synthetic parameter SDK origin', code='target_not_found')
        probe.cause = cause
        hwp.HParameterSet = Trap(hwp.HParameterSet, probe, 'HParameterSet', 'HParameterSet.HInsertText.Text', cause)
        owner = self.own(probe, working_copy_dirty=True)
        self.assert_stopped(owner, probe, run(owner, step), False, True)


if __name__ == '__main__':
    unittest.main()
