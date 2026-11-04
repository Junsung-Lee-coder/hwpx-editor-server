"""Malformed entered native reads/results stop the existing-area path before the next SDK call, including restoration.

These are fault-injected portable tests, not native product evidence.
"""
import unittest

from tests.native_header_footer_fakes import HeaderFooterHwp, report_document
from tests.test_header_footer_exact import FORMAT, TEXT, bound
from tests.test_native_paragraph_edit import Flow


class UnknownBoundary(unittest.TestCase):
    def test_no_follow_on_after_unknown_in_existing_area(self):
        for operation in (TEXT, FORMAT):
            variants = [(phase, failure) for phase in ('before_edit', 'after_edit')
                        for failure in ('pos', 'selection', 'holder', 'block', 'select-result', 'cancel')
                        if not (phase == 'after_edit' and failure == 'cancel' and operation is TEXT)]
            for phase, failure in variants:
                with self.subTest(operation=operation['op'], phase=phase, failure=failure):
                    hwp = report_document()
                    binding = bound(hwp, 'header:s0:both')
                    latched, after = [], []

                    def trip():
                        latched.append(failure)

                    def watch(name, fn):
                        def call(*args, **kwargs):
                            if latched:
                                after.append(name)
                            value = fn(*args, **kwargs)
                            in_area = hwp.cursor[0] == 2 and (phase == 'before_edit' or bool(hwp.executes()))
                            if not latched and in_area:
                                if (failure, name) == ('pos', 'get_pos'):
                                    trip(); return (True, 0, 0)
                                if (failure, name) == ('selection', 'get_selected_pos'):
                                    trip(); return (1, 2, 0, 0, 2, 0, 13)
                                if (failure, name) == ('select-result', 'select_text'):
                                    trip(); return 1
                                if (failure, name) == ('block', 'get_text_file') and 'saveblock' in args[1]:
                                    trip(); return '<HWPML><broken'
                                if (failure, name) == ('cancel', 'Run') and args[0] == 'Cancel':
                                    trip(); return 1
                            return value
                        return call

                    for name in ('get_pos', 'get_selected_pos', 'set_pos', 'select_text', 'get_text_file'):
                        setattr(hwp, name, watch(name, getattr(hwp, name)))
                    hwp.HAction.Run = watch('Run', hwp.HAction.Run)
                    if failure == 'holder':
                        class MalformedHolder(HeaderFooterHwp):
                            @property
                            def ParentCtrl(self):  # noqa: N802
                                if latched:
                                    after.append('ParentCtrl')
                                value = HeaderFooterHwp.ParentCtrl.fget(self)
                                if self.cursor[0] == 2 and not latched and (phase == 'before_edit' or bool(self.executes())):
                                    trip()
                                    class Bad:
                                        CtrlID = 1
                                    return Bad()
                                return value
                        hwp.__class__ = MalformedHolder
                    fields = {'old_text': 'SAMPLE', 'new_text': 'UPDATED'} if operation is TEXT else {'match': 'REPORT', 'bold': True, 'align': 'center'}
                    outcome = Flow(hwp).step(**operation, **binding, **fields)
                    self.assertTrue(latched, 'the fault must be reached')
                    self.assertFalse(outcome['ok'], outcome)
                    self.assertEqual(after, [], 'unknown means no next native read, Cancel, caret restoration or diagnostics')


if __name__ == '__main__':
    unittest.main()
