"""Public bundle envelopes over a real owning worker, real packages and SDK fakes.

The legacy snapshot functions are NOT replaced. Optional observations must be
absent on guarded bundles, rather than silently swallowing native failures.
Required operation/explicit-where reads still fail into the actual custody path.
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from app import local_cli_runtime as rt
from app.local_cli_service import LocalCliService
from tests.native_table_fakes import sample_document
from tests.native_format_fakes import sample_document as format_document
from tests.test_native_table import binding
from tests.test_native_numbering_review_contract import PAGE_STEP, NUMBERING_STEP, _hwpml
from tests.test_native_paragraph_edit import _doc
from tests.test_repair_runtime_safety import owned_doc


class Calls:
    """Fault only the SDK, using the real legacy cursor stack as the optional-read witness."""
    def __init__(self, hwp, *, method='get_pos', occurrence=1, kind='raise', required=False):
        self.hwp, self.method, self.occurrence, self.kind = hwp, method, occurrence, kind
        self.required = required
        self.optional = 0
        self.count = 0
        self.fired = False
        self.after = []
        self.mark = None
        self.cause = OSError('synthetic SDK read boundary')
        for name in ('get_pos', 'get_selected_pos', 'set_pos', 'select_text', 'get_text_file', 'get_cell_addr', 'is_cell', 'KeyIndicator'):
            original = getattr(hwp, name, None)
            if callable(original):
                setattr(hwp, name, self.wrap(name, original))

    def wrap(self, name, original):
        def call(*args, **kwargs):
            if self.fired:
                self.after.append(name)
            frame = inspect.currentframe().f_back
            optional = False
            while frame is not None:
                if frame.f_code.co_name == '_snapshot_cursor_context':
                    optional = True
                    break
                frame = frame.f_back
            del frame
            if optional:
                self.optional += 1
            if name == self.method and (optional or self.required) and not self.fired:
                self.count += 1
                if self.count == self.occurrence:
                    self.fired = True
                    self.mark = len(self.hwp.calls)
                    if self.kind == 'raise':
                        raise self.cause
                    return (0, 'bad', 0) if name == 'get_pos' else ('bad',)
            return original(*args, **kwargs)
        return call


def real_owned(case, hwp, **state):
    owner = owned_doc(case, hwp, **state)
    # Owned's original fixture stubs compact snapshots. Restore the actual
    # production bound method; do not patch service.snapshot_live_location.
    owner.service._bundle_compact_snapshot = LocalCliService._bundle_compact_snapshot.__get__(owner.service)
    return owner


def run(owner, *steps):
    return owner.service.command_bundle(steps=[dict(step) for step in steps])


def scenarios():
    for op in ('table_inventory', 'table_read', 'table_edit_exact', 'table_style_exact', 'table_width_exact', 'table_create_exact'):
        hwp = sample_document()
        fields = {'op': op}
        if op == 'table_read':
            fields['table_id'] = 'tbl:1012'
        elif op not in ('table_inventory', 'table_create_exact'):
            fields.update(binding(hwp, '1012'))
            fields.update({'table_edit_exact': {'action': 'insert_row_below', 'row': 1},
                           'table_style_exact': {'scope': 'cell', 'cell': 'B2', 'fill': '#FF0000'},
                           'table_width_exact': {'fit': 'text_width'}}[op])
        elif op == 'table_create_exact':
            fields.update(match='끝 문단', expected_page=1, rows=1, cols=2, cells=[['새 가', '새 나']], confirm_layout=True)
        yield op, hwp, fields
    for op, fields in (('character_format_exact', {'bold': True}), ('paragraph_format_exact', {'align': 'center'})):
        yield op, format_document(), {'op': op, 'match': 'English', 'expected_page': 1, 'confirm_layout': True, **fields}
    for op in ('paragraph_insert_exact', 'paragraph_remove_exact'):
        fields = dict(op=op, match='Item', expected_page=1)
        fields.update(dict(text='New', position='after', confirm_layout=True) if op == 'paragraph_insert_exact' else dict(expected_text='Item', confirm_remove=True))
        yield op, _doc('Before', 'Item', 'After'), fields
    yield 'page_number_apply', _hwpml(), PAGE_STEP
    yield 'official_numbering_apply', _hwpml(), NUMBERING_STEP


class OptionalEnvelope(unittest.TestCase):
    def test_all_guarded_packages_omit_only_optional_envelope_reads(self):
        covered = []
        for op, hwp, step in scenarios():
            with self.subTest(op=op):
                owner = real_owned(self, hwp)
                calls = Calls(hwp, occurrence=999)
                result = run(owner, step)
                self.assertFalse(owner.session._native_custody_held, result)
                self.assertEqual(calls.optional, 0, (op, calls.optional))
                self.assertEqual(result['before']['observation_status'], 'omitted_guarded_envelope')
                self.assertEqual(result['after']['observation_status'], 'omitted_guarded_envelope')
                self.assertEqual(result['steps'][0]['before']['observation_status'], 'omitted_guarded_envelope')
                self.assertEqual(result['steps'][0]['after']['observation_status'], 'omitted_guarded_envelope')
                covered.append(op)
        self.assertEqual(len(covered), 12)

    def test_removed_pre_post_final_reads_cannot_fire_sdk_faults(self):
        # The four legacy envelopes were initial/pre-step/post-step/final.
        # A never-triggered fault proves omission ONLY, never propagation.
        for method in ('get_pos', 'get_selected_pos'):
            for occurrence in (1, 2, 3, 4):
                for kind in ('raise', 'malformed'):
                    with self.subTest(method=method, occurrence=occurrence, kind=kind):
                        hwp = sample_document()
                        owner = real_owned(self, hwp)
                        fault = Calls(hwp, method=method, occurrence=occurrence, kind=kind)
                        result = run(owner, {'op': 'table_inventory'})
                        self.assertTrue(result['ok'], result)
                        self.assertFalse(fault.fired, 'an optional envelope still entered the SDK')
                        self.assertEqual(fault.optional, 0)

    def test_legacy_unguarded_snapshot_compatibility_is_retained(self):
        hwp = sample_document()
        owner = real_owned(self, hwp)
        calls = Calls(hwp, occurrence=999)
        result = run(owner, {'op': 'where'})
        self.assertTrue(result['ok'], result)
        self.assertGreater(calls.optional, 0)


class RequiredReads(unittest.TestCase):
    def assert_held(self, owner, fault, result, *, dirty):
        self.assertTrue(fault.fired)
        self.assertFalse(result['ok'], result)
        self.assertTrue(result['native_custody_held'], result)
        self.assertEqual(result['dirty'], dirty)
        self.assertEqual(owner.store['working_copy_dirty'], dirty)
        self.assertEqual(fault.after, [])
        self.assertEqual(len(owner.hwp.calls), fault.mark)
        self.assertTrue(owner.session._thread.is_alive())
        self.assertIs(owner.manager._sessions[owner.sid], owner.session)
        self.assertEqual(owner.cleanup_events(), [])
        calls = list(owner.hwp.calls)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owner.manager.execute(session_id=owner.sid, command_name='health_probe', handler=lambda h: h.hwp.get_pos(), timeout=2)
        with self.assertRaises(rt.LocalCliRuntimeError):
            owner.manager.close_session(owner.sid, timeout=1)
        owner.manager.reconcile_command(owner.sid, owner.command()['command_id'])
        self.assertEqual(owner.manager.reap_settled_sessions(), 0)
        self.assertEqual(owner.hwp.calls, calls)
        self.assertEqual(owner.cleanup_events(), [])

    def test_retained_required_reads_really_fail_and_preserve_prior_dirty(self):
        for method in ('get_pos', 'get_selected_pos'):
            for dirty in (False, True):
                with self.subTest(method=method, prior_dirty=dirty):
                    hwp = sample_document()
                    owner = real_owned(self, hwp, working_copy_dirty=dirty)
                    fault = Calls(hwp, method=method, required=True)
                    result = run(owner, {'op': 'table_inventory'}, {'op': 'table_inventory'})
                    self.assert_held(owner, fault, result, dirty=dirty)
                    self.assertEqual(len(result['steps']), 1)
                    self.assertIn('OSError', result['steps'][0]['failure_diagnostic'].values())
                    causes = []
                    error = owner.session._terminal_error
                    while error is not None:
                        causes.append(error)
                        error = error.__cause__
                    self.assertIn(fault.cause, causes)

    def test_applied_step_and_dirty_debt_survive_later_required_failure(self):
        hwp = sample_document()
        step = {'op': 'table_style_exact', **binding(hwp, '1012'), 'scope': 'cell', 'cell': 'B2', 'fill': '#FF0000'}
        owner = real_owned(self, hwp)
        applied = run(owner, step)
        self.assertTrue(applied['ok'], applied)
        effects = list(hwp.native_calls())
        fault = Calls(hwp, method='get_selected_pos', required=True)
        result = run(owner, {'op': 'table_inventory'}, {'op': 'table_inventory'})
        self.assert_held(owner, fault, result, dirty=True)
        self.assertEqual(hwp.native_calls(), effects)
        self.assertTrue(owner.session._normal_close_requires_save)

    def test_semantic_refusal_after_edit_keeps_session_usable(self):
        hwp = sample_document()
        owner = real_owned(self, hwp)
        self.assertTrue(run(owner, {'op': 'table_style_exact', **binding(hwp, '1012'), 'scope': 'cell', 'cell': 'B2', 'fill': '#FF0000'})['ok'])
        refusal = run(owner, {'op': 'table_read', 'table_id': 'tbl:999999'})
        self.assertFalse(refusal['ok'])
        self.assertIn('refusal_code', refusal['steps'][0])
        self.assertFalse(owner.session._native_custody_held)
        self.assertTrue(refusal['dirty'])
        self.assertTrue(run(owner, {'op': 'table_inventory'})['ok'])

    def test_mixed_where_never_uses_legacy_best_effort_reads(self):
        hwp = sample_document()
        owner = real_owned(self, hwp)
        calls = Calls(hwp, occurrence=999)
        result = run(owner, {'op': 'where'}, {'operation': ' table_inventory '}, {'op': 'where'})
        self.assertTrue(result['ok'], result)
        self.assertEqual(calls.optional, 0)
        self.assertEqual(result['steps'][0]['result']['observation_status'], 'partial_guarded_cursor')

    def test_mixed_where_first_technical_or_malformed_read_holds(self):
        for method in ('get_pos', 'get_selected_pos'):
            for kind in ('raise', 'malformed'):
                with self.subTest(method=method, kind=kind):
                    hwp = sample_document()
                    owner = real_owned(self, hwp, working_copy_dirty=True)
                    fault = Calls(hwp, method=method, kind=kind, required=True)
                    result = run(owner, {'op': 'where'}, {'op': 'table_inventory'})
                    self.assert_held(owner, fault, result, dirty=True)
                    self.assertEqual(len(result['steps']), 1)
                    self.assertNotIn('refusal_code', result['steps'][0])


if __name__ == '__main__':
    unittest.main()
