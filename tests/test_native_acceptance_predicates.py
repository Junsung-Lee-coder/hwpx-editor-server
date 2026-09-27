from __future__ import annotations

import unittest

from scripts.native_acceptance_predicates import (
    expected_uniform_margin_hu,
    require_exact_margin_transition,
    require_semantic_target_change,
    semantic_content_members,
)


class NativeAcceptancePredicateTests(unittest.TestCase):
    def test_request_conversion_derives_all_four_sides_independently(self) -> None:
        self.assertEqual(
            expected_uniform_margin_hu(7.0),
            {'left': 1984, 'right': 1984, 'top': 1984, 'bottom': 1984},
        )

    def test_wrong_target_change_is_rejected(self) -> None:
        before = {'left': 510, 'right': 141, 'top': 141, 'bottom': 141}
        wrong_after = {'left': 1984, 'right': 2, 'top': 0, 'bottom': 0}
        expected = expected_uniform_margin_hu(7.0)

        with self.assertRaisesRegex(AssertionError, 'does not match requested'):
            require_exact_margin_transition(before, wrong_after, expected)

    def test_noop_target_change_is_rejected(self) -> None:
        expected = expected_uniform_margin_hu(7.0)

        with self.assertRaisesRegex(AssertionError, 'no-op'):
            require_exact_margin_transition(expected, expected, expected)

    def test_metadata_only_zip_changes_are_rejected(self) -> None:
        changed = [
            'Contents/content.hpf',
            'Preview/PrvImage.png',
        ]

        self.assertEqual(semantic_content_members(changed), [])
        with self.assertRaisesRegex(AssertionError, 'metadata-only'):
            require_semantic_target_change(changed, target_member='Contents/section0.xml')

    def test_target_member_change_is_required_and_non_target_changes_are_rejected(self) -> None:
        changed = [
            'Contents/content.hpf',
            'Contents/section0.xml',
            'Preview/PrvImage.png',
        ]

        self.assertEqual(
            require_semantic_target_change(changed, target_member='Contents/section0.xml'),
            ['Contents/section0.xml'],
        )
        with self.assertRaisesRegex(AssertionError, 'unexpected non-target'):
            require_semantic_target_change(
                changed + ['Contents/header.xml'],
                target_member='Contents/section0.xml',
            )


if __name__ == '__main__':
    unittest.main()
