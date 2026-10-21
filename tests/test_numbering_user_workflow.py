"""CLI -> named bundle -> real service/package -> native numbering regressions.

Only HTTP transport, session persistence/ownership, page evidence and wrapper location snapshots
are fixtures. Search, validation, numbering actions, census and output formatting are real product
code. SDK state and HWPML2X are SYNTHETIC (not Hancom execution/save/reopen/Undo evidence).
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.command_packages.runtime import get_command_package_registry
from app.local_cli_service import LocalCliService
from local_cli_v1 import main as cli
from tests.native_numbering_fakes import FakeCtrl, FakeProps
from tests.test_native_numbering_census import _HwpmlHwp


SESSION = 'a' * 32
PAGE_ARGS = ['page-number-apply', '--position', 'bottom-center', '--confirm-layout']
OFFICIAL_ARGS = ['official-numbering-apply', '--match', 'Item', '--expected-page', '1', '--level', '2', '--confirm-layout']
PAGE_PROPS = {'DrawPos': 5, 'NewNumber': 1, 'SideChar': 0, 'NumberFormat': 0}


def _doc(*texts: str) -> _HwpmlHwp:
    hwp = _HwpmlHwp(
        paragraphs=[{'text': text, 'heading_type': 0, 'level': 0} for text in (texts or ('Title', 'Item', 'Tail'))],
        controls=[FakeCtrl('secd'), FakeCtrl('head'), FakeCtrl('tbl'), FakeCtrl('gso')],
    )
    hwp.package_state['header_footer'] = ['Existing header']
    hwp.hwpml_extras = {
        'footers': ('Existing footer',),
        'picture_width': 900,
        'table_width': 1700,
        'char_height': 1200,
        'margins': {str(p['text']): 100 + i * 20 for i, p in enumerate(hwp.paragraphs)},
    }
    hwp.controls.append(FakeCtrl('foot'))
    return hwp


class _Workflow:
    def __init__(self, hwp: _HwpmlHwp, *, dirty: bool = False) -> None:
        self.hwp = hwp
        self.requests: list[dict] = []
        self.responses: list[dict] = []
        self.binding = {'session_id': SESSION, 'command_generation': 0, 'native_command_sequence': 0, 'dirty': dirty}
        service = self.service = object.__new__(LocalCliService)
        service.command_packages = get_command_package_registry()
        service._load_active_binding = lambda session_id=None: dict(self.binding)
        service._bundle_compact_snapshot = lambda hwp: {'pos': list(hwp.get_pos())}
        service._execute_live = lambda *, handler, **kwargs: handler(
            SimpleNamespace(hwp=hwp, source_filename='document.hwpx', session_id=SESSION)
        )
        service._bundle_page_evidence = lambda hwp: {'page': 1}
        service._update_live_binding = self._update
        service._save_binding = self._save
        service._record_local_cli_command = lambda *a, **kw: None
        service._validated_artifact_projection = lambda **kw: {}
        service._public_bundle_steps = lambda **kw: kw['steps']
        service._public_artifacts = lambda **kw: {}

    def _update(self, binding, **kwargs):
        self.binding.update(binding)
        self.binding['dirty'] = kwargs['dirty']
        return dict(self.binding)

    def _save(self, binding):
        self.binding.update(binding)
        return dict(self.binding)

    def _post(self, base_url, path, payload):
        if path != '/local-cli/command-bundle':
            raise AssertionError(f'unexpected HTTP route {path}')
        self.requests.append(copy.deepcopy(payload))
        response = self.service.command_bundle(**payload)
        self.responses.append(response)
        return response

    def run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        state = {'base_url': 'http://fixture.invalid', 'session_id': SESSION}
        with patch.object(cli, 'load_state', return_value=state), \
                patch.object(cli, 'post_json', side_effect=self._post), \
                patch('app.local_cli_service.snapshot_live_location', return_value={}), \
                patch('app.local_cli_runtime.snapshot_live_location', return_value={}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def executes(self):
        return [c for c in self.hwp.calls if c[0] == 'Execute']


class NumberingUserWorkflowTests(unittest.TestCase):
    def test_option_like_match_text_reaches_the_real_numbering_action(self) -> None:
        for match in ('--항목', '--confirm-layout', '- Item'):
            with self.subTest(match=match):
                flow = _Workflow(_doc('Title', match, 'Tail'))
                argv = ['official-numbering-apply', f'--match={match}', '--expected-page', '1', '--level', '4', '--confirm-layout']
                rc, out, err = flow.run(argv)
                self.assertEqual(rc, 0, err)
                self.assertEqual(flow.requests[0]['steps'][1]['match'], match)
                self.assertEqual(flow.hwp.paragraphs[1], {'text': match, 'heading_type': 1, 'level': 3})
                self.assertEqual([c[1] for c in flow.executes()], ['ParagraphShape'])

    def test_clean_refusals_are_not_successful_cli_executions(self) -> None:
        for argv in (PAGE_ARGS, OFFICIAL_ARGS):
            with self.subTest(command=argv[0]):
                hwp = _doc()
                if argv[0] == 'page-number-apply':
                    hwp.controls.append(FakeCtrl('pgnp', FakeProps({**PAGE_PROPS, 'DrawPos': 1})))
                else:
                    hwp.paragraphs[1]['heading_type'] = 2
                flow = _Workflow(hwp)
                rc, out, err = flow.run(argv)
                self.assertFalse(flow.responses[-1]['ok'])
                self.assertEqual(flow.executes(), [])
                self.assertEqual(rc, 1, out + err)
                self.assertNotIn('executed against', out)
                self.assertIn('stopped', out)

    def test_unknown_outcomes_return_failure_and_keep_mutation_evidence(self) -> None:
        for argv in (PAGE_ARGS, OFFICIAL_ARGS):
            for as_json in (False, True):
                with self.subTest(command=argv[0], json=as_json):
                    flow = _Workflow(_doc())
                    if argv[0] == 'page-number-apply':
                        flow.hwp.HAction.page_number_mode = 'insert-unreadable'
                    else:
                        flow.hwp.HAction.shape_mode = 'prefix-text'
                    rc, out, err = flow.run([*argv, *(['--json'] if as_json else [])])
                    response = flow.responses[-1]
                    self.assertFalse(response['ok'])
                    self.assertTrue(response['dirty'])
                    self.assertTrue(response['steps'][1]['mutation_may_have_persisted'])
                    self.assertEqual(response['steps'][1]['rollback'], {'attempted': False, 'succeeded': False})
                    self.assertEqual(len(flow.executes()), 1)
                    self.assertEqual(rc, 1, out + err)
                    self.assertNotIn('executed against', out)
                    if as_json:
                        self.assertFalse(json.loads(out)['ok'])

    def test_reapplying_identical_numbering_has_no_dirty_delta(self) -> None:
        for argv in (PAGE_ARGS, OFFICIAL_ARGS):
            for prior_dirty in (False, True):
                with self.subTest(command=argv[0], prior_dirty=prior_dirty):
                    flow = _Workflow(_doc())
                    self.assertEqual(flow.run(argv)[0], 0)
                    first_count = len(flow.executes())
                    # Simulated clean/dirty session binding, not native save evidence.
                    flow.binding['dirty'] = prior_dirty
                    rc, out, err = flow.run(argv)
                    response = flow.responses[-1]
                    step = response['steps'][1]
                    self.assertTrue(response['ok'])
                    self.assertFalse(step['result']['result']['changed'])
                    self.assertEqual(len(flow.executes()), first_count)
                    self.assertFalse(step['dirty'])
                    self.assertEqual(response['dirty'], prior_dirty)
                    self.assertEqual(flow.binding['dirty'], prior_dirty)
                    self.assertEqual(rc, 0, err)
                    self.assertNotIn('executed against', out)
                    self.assertIn('already', out)

    def test_six_page_positions_start_numbers_and_dash_reach_hancom_parameters(self) -> None:
        positions = ('top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right')
        for draw_pos, position in enumerate(positions, start=1):
            for side in ('none', 'dash'):
                with self.subTest(position=position, side=side):
                    flow = _Workflow(_doc())
                    before = copy.deepcopy(flow.hwp.paragraphs)
                    state_before = copy.deepcopy(flow.hwp.package_state)
                    start = 65535 if side == 'dash' else 3
                    argv = ['page-number-apply', '--position', position, '--start-number', str(start), '--confirm-layout', '--json']
                    argv += ['--prefix', '-', '--suffix', '-'] if side == 'dash' else ['--side-char', 'none']
                    rc, out, err = flow.run(argv)
                    self.assertEqual(rc, 0, err)
                    self.assertTrue(json.loads(out)['ok'])
                    self.assertEqual(flow.executes(), [('Execute', 'PageNumPos', {
                        'DrawPos': draw_pos, 'NewNumber': start, 'SideChar': 45 if side == 'dash' else 0, 'NumberFormat': 0,
                    })])
                    self.assertEqual(flow.hwp.paragraphs, before)
                    self.assertEqual(flow.hwp.package_state, state_before)
                    self.assertTrue(flow.responses[-1]['steps'][1]['result']['result']['protection']['checked'])

    def test_four_levels_share_one_real_outline_definition_without_text_prefixes(self) -> None:
        flow = _Workflow(_doc('Title', 'First', 'Second', 'Third', 'Fourth', 'Tail'))
        flow.hwp.outline_shape['StrFormatLevel0'] = 'wrong ^1'
        original = copy.deepcopy(flow.hwp.paragraphs)
        state = copy.deepcopy(flow.hwp.package_state)
        for level, text in enumerate(('First', 'Second', 'Third', 'Fourth'), start=1):
            rc, out, err = flow.run(['official-numbering-apply', '--match', text, '--expected-page', '1', '--level', str(level), '--confirm-layout', '--json'])
            self.assertEqual(rc, 0, out + err)
            response = flow.responses[-1]
            self.assertTrue(response['ok'])
            self.assertEqual(flow.hwp.paragraphs[level]['heading_type'], 1)
            self.assertEqual(flow.hwp.paragraphs[level]['level'], level - 1)
            self.assertTrue(response['steps'][1]['result']['numbering_format_enforced'])
            self.assertTrue(response['steps'][1]['result']['result']['protection']['checked'])
        self.assertEqual([p['text'] for p in flow.hwp.paragraphs], [p['text'] for p in original])
        self.assertEqual(flow.hwp.paragraphs[0], original[0])
        self.assertEqual(flow.hwp.paragraphs[-1], original[-1])
        self.assertEqual(flow.hwp.package_state, state)
        self.assertEqual([c[1] for c in flow.executes()], ['OutlineNumber', *(['ParagraphShape'] * 4)])
        shape = flow.hwp.outline_shape
        self.assertEqual([(shape[f'StrFormatLevel{i}'], shape[f'NumFormatLevel{i}']) for i in range(4)],
                         [('^1.', 0), ('^2.', 8), ('^3)', 0), ('^4)', 8)])
        self.assertEqual([(c[2]['HeadingType'], c[2]['Level']) for c in flow.executes() if c[1] == 'ParagraphShape'],
                         [(1, 0), (1, 1), (1, 2), (1, 3)])

    def test_invalid_numbering_arguments_never_reach_the_service(self) -> None:
        for argv in ([*PAGE_ARGS, '--start-number', '0'], [*OFFICIAL_ARGS, '--level', '5']):
            with self.subTest(command=argv[0]):
                flow = _Workflow(_doc())
                rc, out, err = flow.run(argv)
                self.assertEqual(rc, 1, out + err)
                self.assertEqual(flow.requests, [])
                self.assertEqual(flow.executes(), [])


if __name__ == '__main__':
    unittest.main()
