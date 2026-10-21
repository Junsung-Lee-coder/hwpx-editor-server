"""Numbering result parity for the existing generic bundle CLI entrypoints (fake SDK only)."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from local_cli_v1 import main as cli
from local_cli_v1.bundles import build_named_bundle
from tests.native_numbering_fakes import FakeCtrl, FakeProps
from tests.test_numbering_user_workflow import _Workflow, _doc, PAGE_ARGS, OFFICIAL_ARGS, PAGE_PROPS, SESSION


class NumberingBundleCliStatusTests(unittest.TestCase):
    def _run(self, argv, entrypoint, fault=None):
        flow = _Workflow(_doc())
        if fault == 'refusal':
            if argv[0] == 'page-number-apply':
                flow.hwp.controls.append(FakeCtrl('pgnp', FakeProps({**PAGE_PROPS, 'DrawPos': 1})))
            else:
                flow.hwp.paragraphs[1]['heading_type'] = 2
        elif fault == 'unknown':
            flow.hwp.HAction.execute_result = False
        payload = build_named_bundle(argv[0], argv[1:]).server_payload()
        payload['session_id'] = SESSION
        if entrypoint == 'bundle-run':
            command = ['bundle-run', '--json', *argv]
        else:
            command = [entrypoint, 'fixture.json', '--json']
        with patch.object(cli, '_load_bundle_payload', return_value=payload), \
                patch.object(cli, '_load_tx_server_payload', return_value=payload):
            rc, out, err = flow.run(command)
        return flow, rc, out, err

    def test_generic_bundle_cli_returns_failure_for_numbering_refusal_and_unknown_outcome(self) -> None:
        for argv in (PAGE_ARGS, OFFICIAL_ARGS):
            for entrypoint in ('bundle-run', 'bundle', 'tx-commit'):
                for fault in ('refusal', 'unknown'):
                    with self.subTest(command=argv[0], entrypoint=entrypoint, fault=fault):
                        flow, rc, out, err = self._run(argv, entrypoint, fault)
                        self.assertFalse(flow.responses[-1]['ok'])
                        self.assertEqual(rc, 1, out + err)
                        self.assertNotIn('transaction committed', out)
                        self.assertEqual(len(flow.executes()), 1 if fault == 'unknown' else 0)

    def test_generic_bundle_cli_retains_success_status_for_applied_numbering(self) -> None:
        for argv in (PAGE_ARGS, OFFICIAL_ARGS):
            for entrypoint in ('bundle-run', 'bundle', 'tx-commit'):
                with self.subTest(command=argv[0], entrypoint=entrypoint):
                    flow, rc, out, err = self._run(argv, entrypoint)
                    self.assertTrue(flow.responses[-1]['ok'])
                    self.assertEqual(rc, 0, out + err)
                    self.assertEqual(len(flow.executes()), 1)


if __name__ == '__main__':
    unittest.main()
