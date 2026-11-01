"""Real owning callers over synthetic COM; no native closure/feature evidence."""
import unittest
from unittest.mock import patch
from app import local_cli_runtime as rt, local_cli_service as svc
from tests.test_native_numbering_runtime_custody import Owned, make_hwp, NUMBERING_STEP, PAGE_STEP
from tests.test_cli_failure_recovery import RecoveryTests


class CallbackSafetyTests(unittest.TestCase):
    def test_numbering_never_projects_untrusted_exception_descriptors_or_repr(self):
        for step, action in ((NUMBERING_STEP, 'ParagraphShape'), (PAGE_STEP, 'PageNumPos')):
            for signal in (True, False):
                with self.subTest(action=action, signal=signal):
                    callbacks = []
                    class Fault(BaseException if signal else Exception):
                        @property
                        def __dict__(self):
                            callbacks.append('dict')
                            return {}
                        def __repr__(self):
                            callbacks.append('repr')
                            return 'PRIVATE_SYNTHETIC'
                        def __str__(self):
                            callbacks.append('str')
                            return 'PRIVATE_SYNTHETIC'
                    owned = Owned(self, make_hwp())
                    fault = owned.arm(action, make_exc=Fault)
                    result, error = owned.run(step)
                    self.assertTrue(fault.attempted)
                    self.assertEqual(callbacks, [])
                    self.assertTrue(owned.session._native_custody_held)
                    self.assertTrue(owned.session._thread.is_alive())
                    self.assertIs(owned.manager._sessions[owned.sid], owned.session)
                    self.assertFalse(owned.session._closed.is_set())
                    self.assertEqual(owned.cleanup_events(), [])
                    self.assertEqual(fault.after(), [])
                    self.assertEqual(owned.snapshots_after_effect, 0)
                    self.assertFalse(result['ok'])
                    self.assertTrue(result['dirty'])

    def test_exact_builtin_signal_with_foreign_dictionary_key_is_not_tagged(self):
        callbacks = []
        class Key:
            def __hash__(self):
                return hash('mutation_state')
            def __eq__(self, other):
                callbacks.append('key-equality')
                return False
        signal = KeyboardInterrupt()
        key = Key()
        signal.__dict__[key] = 'SYNTHETIC_UNTRUSTED_METADATA'
        callbacks.clear()  # construction is outside the observed caller interval
        owned = Owned(self, make_hwp())
        fault = owned.arm('ParagraphShape', make_exc=lambda: signal)
        result, error = owned.run(NUMBERING_STEP)
        self.assertEqual(callbacks, [])
        self.assertEqual(fault.attempted, ['ParagraphShape'])
        self.assertTrue(owned.session._native_custody_held)
        self.assertTrue(owned.session._thread.is_alive())
        self.assertEqual(owned.cleanup_events(), [])
        self.assertEqual(fault.after(), [])
        self.assertEqual(owned.snapshots_after_effect, 0)
        self.assertFalse(result['ok'])
        self.assertTrue(result['dirty'])

    def test_public_font_never_reads_exception_metaclass_or_metadata(self):
        # Reuse only the existing synthetic owning-session constructor, not its
        # inherited test methods or any finalization override under assertion.
        manager, session, wrapper, calls, wc = RecoveryTests.owned(self)
        callbacks = []
        class Meta(type):
            @property
            def __name__(self):
                callbacks.append('name')
                return 'ValueError'
        class Fault(Exception, metaclass=Meta):
            def __getattribute__(self, name):
                callbacks.append('attribute')
                return BaseException.__getattribute__(self, name)
            def __repr__(self):
                callbacks.append('repr')
                return 'PRIVATE_SYNTHETIC'
        service = object.__new__(svc.LocalCliService)
        service._load_active_binding = lambda **k: {}
        service._style_selection_refusal = lambda *a, **k: None
        service._execute_live = lambda **k: manager.execute(session_id='recovery', command_name=k['command_name'], handler=k['handler'], timeout=.5)
        def edit(*a, **k):
            calls.append('edit')
            raise Fault()
        before = wc.read_bytes()
        with patch.object(svc, '_snapshot_cursor_context', lambda *a: {}), patch.object(svc, 'apply_char_style', edit):
            with self.assertRaises(rt.LocalCliNativeCustodyHeldError):
                service.font_family(face_name='Arial')
        self.assertEqual(callbacks, [])
        self.assertEqual(session.command_status()['native_diagnostic']['causal_class'], 'UNKNOWN')
        self.assertTrue(session._native_custody_held)
        self.assertTrue(session._thread.is_alive())
        self.assertIs(manager._sessions['recovery'], session)
        self.assertEqual(calls, ['initialize', 'edit'])
        self.assertEqual(wc.read_bytes(), before)
        self.assertEqual(callbacks, [])


if __name__ == '__main__':
    unittest.main()
