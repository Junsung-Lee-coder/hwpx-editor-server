"""Paired returned-data semantics, through the same public owning worker."""
import unittest
from app import native_numbering as nn
from tests import test_t25_residual_consumers as faults
from tests.native_numbering_fakes import FakeCtrl
from tests.test_guarded_bundle_observations import run


class ReturnedSemantics(unittest.TestCase):
    own = faults.ResidualConsumers.own

    def healthy_refusal(self, which, setup, code):
        hwp, step, prefix = faults.fixture(which)
        setup(hwp, step)
        owner = self.own(faults.Probe(hwp))
        result = run(owner, step)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['steps'][0].get('refusal_code'), code, result)
        self.assertFalse(owner.session._native_custody_held)
        self.assertTrue(run(owner, {'op': 'where'})['ok'])

    def test_genuine_source_control_limit(self):
        def setup(hwp, step):
            hwp.controls = [FakeCtrl('secd')] + [FakeCtrl('tbl') for _ in range(5000)]
        self.healthy_refusal('official', setup, 'control_enumeration_unavailable')

    def test_genuine_existing_page_number(self):
        hwp, step, _ = faults.fixture('page')
        owner = self.own(faults.Probe(hwp))
        self.assertTrue(run(owner, step)['ok'])
        step['position'] = 'top-center'
        result = run(owner, step)
        self.assertEqual(result['steps'][0].get('refusal_code'), 'existing_page_number', result)
        self.assertFalse(owner.session._native_custody_held)
        self.assertTrue(owner.session._normal_close_requires_save)

    def test_genuine_remove_false_and_create_wrong_page_restore(self):
        def selection_false(hwp, step):
            select = hwp.select_text
            hwp.select_text = lambda sp, so, ep, eo, li=0: False if ep == sp + 1 else select(sp, so, ep, eo, li)
        self.healthy_refusal('remove', selection_false, 'target_unlocatable')
        self.healthy_refusal('create', lambda h, s: s.update(expected_page=2), 'target_wrong_page')

    def test_actual_public_readback_returned_unusable_stays_optional(self):
        hwp, _, _ = faults.fixture('readback')
        from app.native_numbering_census import MAX_HWPML_CHARS
        hwp.get_text_file = lambda *a: 'x' * (MAX_HWPML_CHARS + 1)
        owner = self.own(faults.Probe(hwp))
        result = owner.service.numbering_readback()
        self.assertTrue(result['ok'])
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'census_unavailable')
        self.assertFalse(owner.session._native_custody_held)
        self.assertTrue(run(owner, {'op': 'where'})['ok'])


if __name__ == '__main__':
    unittest.main()
