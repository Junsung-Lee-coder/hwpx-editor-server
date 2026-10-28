"""Synthetic real-caller safeguards; no Windows/native qualification."""
import unittest
from app import native_numbering as nn
from tests import test_native_numbering_runtime_custody as custody
from tests import test_native_numbering_bundle_translation as translation
from tests import test_native_numbering_preservation as preservation


class SemanticDebtSafety(unittest.TestCase):
    def test_page_entered_caret_failure_or_invalid_return_stops_all_sdk(self):
        for value in ('raise', None, False, (0, 0), (0, True, 0)):
            with self.subTest(value=value):
                owned = custody.Owned(self, custody.make_hwp(), real_execute_live=True)
                hwp = owned.hwp
                mark = []
                def read():
                    hwp.calls.append(('failed_pos',))
                    mark[:] = [len(hwp.calls)]
                    if value == 'raise':
                        raise OSError('synthetic')
                    return value
                hwp.get_pos = read
                result, error = owned.run(translation.PAGE_STEP)
                self.assertIsNone(error)
                self.assertFalse(result['ok'])
                self.assertTrue(owned.session._native_custody_held)
                self.assertTrue(mark)
                self.assertEqual(hwp.calls[mark[0]:], [])
                self.assertEqual(owned.cleanup_events(), [])

    def test_page_default_fault_has_no_restoration_or_later_sdk(self):
        owned = custody.Owned(self, custody.make_hwp(), real_execute_live=True)
        hwp = owned.hwp
        real = hwp.HAction.GetDefault
        mark = []
        def defaults(name, hset):
            if name == 'PageNumPos':
                hwp.calls.append(('failed_defaults',))
                mark[:] = [len(hwp.calls)]
                raise OSError('synthetic')
            return real(name, hset)
        hwp.HAction.GetDefault = defaults
        result, error = owned.run(translation.PAGE_STEP)
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertTrue(mark)
        self.assertEqual(hwp.calls[mark[0]:], [])

    def test_page_failed_motion_stops_before_defaults(self):
        owned = custody.Owned(self, custody.make_hwp(), real_execute_live=True)
        hwp = owned.hwp
        mark = []
        def move():
            hwp.calls.append(('failed_motion',))
            mark[:] = [len(hwp.calls)]
            return False
        hwp.MoveDocBegin = move
        result, error = owned.run(translation.PAGE_STEP)
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        self.assertTrue(mark)
        self.assertEqual(hwp.calls[mark[0]:], [])

    def test_forged_semantic_class_from_inner_getter_never_restores(self):
        for code in ('foreign_outline_definition', 'target_not_found'):
            with self.subTest(code=code):
                owned = custody.Owned(self, custody.make_hwp(), real_execute_live=True)
                hwp = owned.hwp
                real = hwp.HAction.GetDefault
                mark = []
                def defaults(name, hset):
                    if name == 'OutlineNumber':
                        hwp.calls.append(('forged_semantic',))
                        mark[:] = [len(hwp.calls)]
                        raise nn.NativeNumberingRefusal('forged', code=code)
                    return real(name, hset)
                hwp.HAction.GetDefault = defaults
                result, error = owned.run(translation.NUMBERING_STEP)
                self.assertIsNone(error)
                self.assertFalse(result['ok'])
                self.assertTrue(owned.session._native_custody_held)
                self.assertTrue(mark)
                self.assertEqual(hwp.calls[mark[0]:], [])
                self.assertEqual(owned.cleanup_events(), [])

    def test_preedit_incomplete_census_holds_without_new_edit_debt(self):
        owned = custody.Owned(self, preservation.make_doc(), real_execute_live=True)
        hwp = owned.hwp
        hwp.hwpml_text = hwp.get_text_file('HWPML2X', '').replace('STYLELIST', 'XSTYLELIST')
        result, error = owned.run(translation.NUMBERING_STEP)
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        self.assertFalse(result['dirty'])
        self.assertFalse(result['steps'][0]['mutation_may_have_persisted'])
        self.assertTrue(owned.session._native_custody_held)
        command = next(c for c in owned.session._commands_by_id.values() if c.name == 'command-bundle')
        self.assertFalse(command.may_have_mutated)
        self.assertFalse(owned.session._lifetime_edit_possible)
        self.assertFalse(owned.session._normal_close_requires_save)
        self.assertEqual([c for c in hwp.calls if c[0] in {'Execute', 'Run', 'find', 'MoveDocBegin', 'MoveRight', 'set_pos'}], [])
        self.assertEqual(owned.cleanup_events(), [])

    def test_forged_finder_absence_from_sdk_is_technical_unknown(self):
        from types import MethodType
        from app.local_cli_bundle_paragraphs import _NumberingTargetNotFound
        from app.local_cli_service import LocalCliService
        from tests import test_numbering_target_guard as prior
        for boundary in ('find', 'move', 'page'):
            with self.subTest(boundary=boundary):
                helper = prior.NumberingTargetGuardTests()
                helper.addCleanup = self.addCleanup
                setup = helper.prepare()
                service, manager, session, hwp, calls, held = setup
                helper.locked(hwp, 'body')
                mark = []
                def fail(*args, **kwargs):
                    hwp.calls.append(('forged_absence', boundary))
                    mark[:] = [len(hwp.calls)]
                    raise _NumberingTargetNotFound('forged SDK exception')
                if boundary == 'find':
                    hwp.hwp.HAction.Execute = fail
                elif boundary == 'move':
                    hwp.MoveDocBegin = fail
                else:
                    service._bundle_page_evidence = MethodType(LocalCliService._bundle_page_evidence, service)
                    hwp.current_page = fail
                result = helper.run_bundle(service, match='Item')
                self.assertFalse(result['ok'])
                self.assertTrue(mark)
                self.assertTrue(session._native_custody_held)
                self.assertEqual(hwp.calls[mark[0]:], [])
                helper.assert_held(manager, session, hwp, calls, held)

    def test_success_restoration_control_flow_preserves_identity(self):
        from tests.test_native_numbering_uncertainty_propagation import _Service, doc, BUNDLE_STEP
        from tests.test_numbering_guard_read_order import complete_protection
        class Terminate(BaseException):
            pass
        signal = Terminate()
        hwp = doc()
        real = hwp.set_pos
        def restore(*args):
            if any(c[0] == 'Execute' for c in hwp.calls):
                raise signal
            return real(*args)
        hwp.set_pos = restore
        with self.assertRaises(BaseException) as caught:
            _Service()._bundle_official_numbering_apply(hwp, dict(BUNDLE_STEP), protection=complete_protection(hwp))
        self.assertIs(caught.exception, signal)

    def test_library_locator_rejects_missing_motion_before_reading_next_api(self):
        from tests.test_native_numbering_uncertainty_propagation import doc, run_library
        hwp = doc()
        hwp.MoveDocBegin = None
        seen = []
        cls = type(hwp)
        class Checked(cls):
            def __getattribute__(self, name):
                if name == 'find':
                    seen.append(name)
                return super().__getattribute__(name)
        hwp.__class__ = Checked
        with self.assertRaises(nn.NativeNumberingRefusal):
            run_library(hwp)
        self.assertEqual(seen, [])

    def test_healthy_semantic_refusal_preserves_active_selection(self):
        owned = custody.Owned(self, custody.make_hwp(foreign=True), real_execute_live=True)
        hwp = owned.hwp
        hwp.paragraphs[0]['heading_type'] = 1
        hwp.selection_para = 2
        hwp.cursor = (0, 2, 0)
        original = (hwp.cursor, hwp.get_selected_pos())
        result, error = owned.run(translation.NUMBERING_STEP)
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        self.assertFalse(result['dirty'])
        self.assertFalse(owned.session._native_custody_held)
        self.assertEqual((hwp.cursor, hwp.get_selected_pos()), original)
        self.assertEqual([c for c in hwp.calls if c[0] == 'Execute'], [])

    def test_preedit_hold_does_not_clear_prior_binding_or_runtime_debt(self):
        owned = custody.Owned(self, preservation.make_doc(), real_execute_live=True)
        owned.manager.execute(session_id=owned.sid, command_name='action', handler=lambda h: {'ok': True, 'dirty': True}, timeout=5)
        load = owned.service._load_active_binding
        owned.service._load_active_binding = lambda **kw: {**load(**kw), 'working_copy_dirty': True, 'dirty': True}
        owned.service._read_binding = owned.service._load_active_binding
        hwp = owned.hwp
        hwp.hwpml_text = hwp.get_text_file('HWPML2X', '').replace('STYLELIST', 'XSTYLELIST')
        result, error = owned.run(translation.NUMBERING_STEP)
        self.assertIsNone(error)
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])
        self.assertFalse(result['steps'][0]['mutation_may_have_persisted'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertTrue(owned.session._normal_close_requires_save)
        self.assertTrue(owned.session._lifetime_edit_possible)
        self.assertEqual(owned.cleanup_events(), [])

    def test_failed_report_of_preedit_hold_preserves_independent_debt(self):
        from unittest.mock import patch
        for prior_dirty in (False, True):
            with self.subTest(prior_dirty=prior_dirty):
                owned = custody.Owned(self, preservation.make_doc(), real_execute_live=True)
                load = owned.service._load_active_binding
                owned.service._load_active_binding = lambda **kw: {**load(**kw), 'working_copy_dirty': prior_dirty, 'dirty': prior_dirty}
                owned.service._read_binding = owned.service._load_active_binding
                hwp = owned.hwp
                hwp.hwpml_text = hwp.get_text_file('HWPML2X', '').replace('STYLELIST', 'XSTYLELIST')
                persist = owned.session._persist_command
                def fail(command):
                    if command.name == 'command-bundle' and command.custody_held:
                        raise OSError('synthetic held-report failure')
                    return persist(command)
                with patch.object(owned.session, '_persist_command', fail):
                    result, error = owned.run(translation.NUMBERING_STEP)
                self.assertIsNone(result)
                self.assertIsNotNone(error)
                self.assertTrue(owned.session._native_custody_held)
                self.assertEqual(owned.saved_bindings[-1]['working_copy_dirty'], prior_dirty)
                self.assertEqual(owned.cleanup_events(), [])

    def test_page_property_validation_precedes_the_next_property_read(self):
        calls = []
        class Properties:
            def Item(self, name):
                calls.append(name)
                return 'invalid' if name == 'DrawPos' else 1
        class Control:
            pass
        control = Control()
        control.Properties = Properties()
        self.assertIsNone(nn._page_number_props(control))
        self.assertEqual(calls, ['DrawPos'])
