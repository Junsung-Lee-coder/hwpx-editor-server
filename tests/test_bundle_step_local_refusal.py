"""Same public bundle/owning worker, intact snapshots/serializer; synthetic SDK only.

Unlike earlier separate-request tests, the edit and refusal here share one handle.
Native REST/MCP/save/reopen qualification is recorded separately in artifacts.
"""
from __future__ import annotations

import inspect
import unittest
from tests.test_native_paragraph_edit import _doc
from unittest.mock import patch

from app import local_cli_runtime as rt
from app import native_numbering as nn
from app.local_cli_service import LocalCliService
from tests.native_table_fakes import sample_document
from tests.test_guarded_bundle_observations import Calls, real_owned, run
from tests.test_native_table import binding


def style(hwp, inst='1012', color='#FF0000'):
    return dict(op='table_style_exact', **binding(hwp, inst), scope='cell', cell='B2', fill=color)


def in_where():
    frame = inspect.currentframe().f_back
    try:
        while frame:
            if frame.f_code.co_filename.replace('\\', '/').endswith('/commands/where/run.py'):
                return True
            frame = frame.f_back
        return False
    finally:
        del frame


def owner_for(case, hwp, **state):
    owner = real_owned(case, hwp, **state)
    owner.service._public_bundle_steps = LocalCliService._public_bundle_steps.__get__(owner.service)
    return owner


class StepLocalRefusal(unittest.TestCase):
    def assert_refusal(self, owner, result, index, code, *, dirty):
        self.assertFalse(result['ok'], result)
        self.assertEqual(len(result['steps']), index + 1, result)
        self.assertTrue(all(s['ok'] for s in result['steps'][:index]), result)
        step = result['steps'][index]
        self.assertFalse(step['mutation_may_have_persisted'], step)
        self.assertFalse(step['dirty'], step)
        self.assertEqual(step.get('refusal_code'), code, step)
        self.assertFalse(step.get('native_custody_held', False), step)
        self.assertFalse(owner.session._native_custody_held, result)
        self.assertEqual(result['dirty'], dirty, result)
        self.assertEqual(owner.store['working_copy_dirty'], dirty)
        self.assertEqual(owner.session._normal_close_requires_save, dirty)
        self.assertTrue(owner.session._thread.is_alive())

    def test_same_bundle_edit_then_missing_table_preserves_edit_and_continues(self):
        for prior_dirty in (False, True):
            for prefixes in (0, 1, 2):
                with self.subTest(prior_dirty=prior_dirty, prefixes=prefixes):
                    hwp = sample_document()
                    steps = [style(hwp)] if prefixes else []
                    if prefixes == 2:
                        steps.append(style(hwp, '1011', '#00FF00'))
                    owner = owner_for(self, hwp, working_copy_dirty=prior_dirty)
                    result = run(owner, *steps, {'operation': ' table_read ', 'table_id': 'tbl:999999'}, {'op': 'table_inventory'})
                    dirty = prior_dirty or bool(prefixes)
                    self.assert_refusal(owner, result, prefixes, 'table_not_found', dirty=dirty)
                    if prefixes:
                        self.assertEqual(hwp.by_inst('1012').named('B2').fill['face'], 0x0000FF)
                        self.assertTrue(owner.session._lifetime_edit_possible)
                        self.assertTrue(owner.command()['may_have_mutated'])
                    self.assertTrue(run(owner, {'op': 'table_inventory'})['ok'])
                    continued = run(owner, style(hwp, color='#0000FF'))
                    self.assertTrue(continued['ok'], continued)
                    self.assertEqual(hwp.by_inst('1012').named('B2').fill['face'], 0xFF0000)
                    # Exercise real worker save-debt settlement with an explicit
                    # fake SDK persistence adapter. This is not native Save proof.
                    output = owner.root / 'saved-model.xml'
                    def save(handle):
                        output.write_text(handle.hwp.get_text_file('HWPML2X', ''), encoding='utf-8')
                        return {'ok': True, 'dirty': False, 'ordinary_save_confirmed': True}
                    saved = owner.manager.execute(session_id=owner.sid, command_name='save', handler=save, timeout=5)
                    self.assertTrue(saved['ordinary_save_confirmed'])
                    self.assertIn('TABLE', output.read_text(encoding='utf-8'))
                    self.assertFalse(owner.session._normal_close_requires_save)
                    self.assertFalse(owner.session._native_custody_held)

    def test_same_bundle_noop_prefix_does_not_create_or_erase_debt(self):
        hwp = sample_document()
        owner = owner_for(self, hwp)
        self.assertTrue(run(owner, style(hwp))['ok'])
        effects = list(hwp.native_calls())
        result = run(owner, style(hwp), {'op': 'table_read', 'table_id': 'tbl:999999'})
        self.assert_refusal(owner, result, 1, 'table_not_found', dirty=True)
        self.assertFalse(result['steps'][0]['dirty'])
        self.assertEqual(hwp.native_calls(), effects)

    def test_same_bundle_table_validation_siblings_keep_confirmed_prefix(self):
        for sibling, code in [('hash', 'table_state_stale'), ('missing_style', 'table_not_found')]:
            with self.subTest(sibling=sibling):
                hwp = sample_document()
                first = style(hwp)
                bad = style(hwp, '1011')
                if sibling == 'hash':
                    bad['expected_hash'] = 'sha256:' + '0' * 24
                elif sibling == 'missing_style':
                    bad['table_id'] = 'tbl:999999'

                owner = owner_for(self, hwp)
                result = run(owner, first, bad, {'op': 'table_inventory'})
                self.assert_refusal(owner, result, 1, code, dirty=True)
                self.assertEqual(hwp.by_inst('1012').named('B2').fill['face'], 0x0000FF)
                self.assertTrue(run(owner, {'op': 'table_inventory'})['ok'])

    def test_same_bundle_format_paragraph_numbering_semantic_siblings(self):
        from tests.native_format_fakes import sample_document as format_doc
        from tests.test_native_numbering_review_contract import _hwpml, PAGE_STEP, NUMBERING_STEP
        cases = []
        para = _doc('Before', 'Item', 'After')
        insert = dict(op='paragraph_insert_exact', match='Item', expected_page=1, text='New', position='after', confirm_layout=True)
        cases.append(('paragraph', para, insert, insert, 'target_ambiguous'))
        fmt = format_doc()
        first = dict(op='character_format_exact', match='English', expected_page=1, bold=True, confirm_layout=True)
        bad = dict(op='character_format_exact', match='English', expected_page=1, font_family='DOES_NOT_EXIST', confirm_layout=True)
        cases.append(('format', fmt, first, bad, 'font_not_available'))
        numbering = _hwpml(foreign=True)
        numbering.paragraphs[0]['heading_type'] = 1
        cases.append(('numbering', numbering, dict(PAGE_STEP), dict(NUMBERING_STEP), 'foreign_outline_definition'))
        for family, hwp, first, bad, code in cases:
            with self.subTest(family=family):
                owner = owner_for(self, hwp)
                with patch('app.native_format.installed_font_families', return_value={'Arial'}):
                    result = run(owner, first, bad, {'op': 'table_inventory'})
                self.assert_refusal(owner, result, 1, code, dirty=True)
                self.assertTrue(run(owner, {'op': 'where'})['ok'])

    def test_separate_request_positive_remains_usable(self):
        hwp = sample_document()
        owner = owner_for(self, hwp)
        self.assertTrue(run(owner, style(hwp))['ok'])
        result = run(owner, {'op': 'table_read', 'table_id': 'tbl:999999'})
        self.assert_refusal(owner, result, 0, 'table_not_found', dirty=True)

    def test_same_bundle_required_technical_failure_retains_prefix_and_no_next_sdk(self):
        for kind in ('raise', 'malformed'):
            with self.subTest(kind=kind):
                hwp = sample_document()
                first = style(hwp)
                owner = owner_for(self, hwp)
                # Arm only after the successful prefix, without replacing the
                # public handler, package or compact snapshot implementation.
                original = hwp.get_pos
                fault = []
                def read():
                    if in_where():
                        fault.append(len(hwp.calls))
                        if kind == 'raise':
                            raise OSError('synthetic second-step strict read')
                        return ('bad',)
                    return original()
                hwp.get_pos = read
                result = run(owner, first, {'op': 'where'}, {'op': 'table_inventory'})
                self.assertTrue(fault)
                self.assertTrue(result['steps'][0]['ok'], result)
                self.assertEqual(len(result['steps']), 2)
                self.assertTrue(owner.session._native_custody_held)
                self.assertTrue(result['dirty'])
                self.assertTrue(owner.command()['may_have_mutated'])
                self.assertNotIn('refusal_code', result['steps'][1])
                self.assertEqual(len(hwp.calls), fault[0])
                self.assertEqual(owner.cleanup_events(), [])
                with self.assertRaises(rt.LocalCliRuntimeError):
                    owner.manager.close_session(owner.sid, timeout=1)

    def test_mutator_issued_forged_sdk_refusal_and_posteffect_error_stay_held(self):
        for forged in (False, True):
            with self.subTest(forged=forged):
                hwp = sample_document()
                first = style(hwp)
                owner = owner_for(self, hwp)
                original = hwp.native
                mark = []
                def mutate(*args, **kwargs):
                    result = original(*args, **kwargs)
                    if args[0] == 'CellFill':
                        mark.append(len(hwp.calls))
                        if forged:
                            raise nn.NativeNumberingRefusal('forged SDK semantic refusal', code='table_not_found')
                        raise OSError('synthetic after-effect fault')
                    return result
                hwp.native = mutate
                result = run(owner, first, {'op': 'table_inventory'})
                self.assertTrue(mark)
                self.assertTrue(owner.session._native_custody_held)
                self.assertTrue(result['steps'][0]['mutation_may_have_persisted'])
                self.assertTrue(result['dirty'])
                self.assertEqual(len(result['steps']), 1)
                self.assertNotIn('refusal_code', result['steps'][0])
                self.assertEqual(len(hwp.calls), mark[0])
                self.assertEqual(owner.cleanup_events(), [])

    def test_prefix_debt_survives_semantic_or_technical_reporting_failure(self):
        for technical in (False, True):
            with self.subTest(technical=technical):
                hwp = sample_document()
                first = style(hwp)
                owner = owner_for(self, hwp)
                persist = owner.session._persist_command
                def fail(command):
                    if command.name == 'command-bundle' and command.state != 'queued':
                        raise OSError('synthetic terminal report failure')
                    return persist(command)
                if technical:
                    original = hwp.get_pos
                    def read():
                        if in_where():
                            raise OSError('synthetic read after prefix')
                        return original()
                    hwp.get_pos = read
                stop = {'op': 'where'} if technical else {'op': 'table_read', 'table_id': 'tbl:999999'}
                with patch.object(owner.session, '_persist_command', fail):
                    with self.assertRaises(Exception):
                        run(owner, first, stop)
                self.assertTrue(owner.session._native_custody_held)
                self.assertTrue(owner.store['working_copy_dirty'])
                self.assertTrue(owner.session._normal_close_requires_save)
                self.assertTrue(owner.command()['may_have_mutated'])
                self.assertEqual(hwp.by_inst('1012').named('B2').fill['face'], 0x0000FF)
                self.assertEqual(owner.cleanup_events(), [])


if __name__ == '__main__':
    unittest.main()
