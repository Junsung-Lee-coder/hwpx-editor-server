from __future__ import annotations

import unittest

from local_cli_v1.main import build_command_status, build_parser


class ImageAtAnchorCliTests(unittest.TestCase):
    def test_parser_accepts_image_at_anchor_options(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                'image-at-anchor',
                'chart.png',
                '--target',
                'Figure 5.',
                '--position',
                'before-anchor',
                '--width',
                '140',
                '--treat-as-char',
                'on',
                '--embedded',
                'on',
            ]
        )
        self.assertEqual(args.command, 'image-at-anchor')
        self.assertEqual(str(args.file), 'chart.png')
        self.assertEqual(args.target, 'Figure 5.')
        self.assertEqual(args.position, 'before-anchor')
        self.assertEqual(args.width, 140)
        self.assertEqual(args.treat_as_char, 'on')
        self.assertEqual(args.embedded, 'on')

    def test_command_status_documents_image_at_anchor(self) -> None:
        status = build_command_status(build_parser())
        self.assertIn('image-at-anchor', status)
        self.assertEqual(status['image-at-anchor']['status'], 'direct-backlog')
        self.assertIn('resolves one text anchor', status['image-at-anchor']['note'])


if __name__ == '__main__':
    unittest.main()
