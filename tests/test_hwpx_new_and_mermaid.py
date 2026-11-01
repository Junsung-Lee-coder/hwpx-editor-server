from __future__ import annotations

import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
import zlib
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from local_cli_v1.hwpx_new import HwpxNewError, header_xml, mm_to_hwpunit, page_setup, write_blank_hwpx
from local_cli_v1.main import build_command_status, build_parser, main
from local_cli_v1.mermaid_render import MermaidRenderError, png_size, render_png
from local_cli_v1.static_inspector import inspect_static


def _png(width: int = 4, height: int = 3) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xFFFFFFFF)
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    raw = b''.join(b'\x00' + b'\xff' * (width * 3) for _ in range(height))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


class BlankHwpxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_package_layout_and_xml(self) -> None:
        result = write_blank_hwpx(self.tmp / 'blank.hwpx', title='제목 <1>')
        with zipfile.ZipFile(result['path']) as archive:
            names = archive.namelist()
            self.assertEqual(names[0], 'mimetype')
            self.assertEqual(archive.getinfo('mimetype').compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read('mimetype'), b'application/hwp+zip')
            for name in names:
                if name.endswith(('.xml', '.hpf', '.rdf')):
                    ET.fromstring(archive.read(name))
            section = archive.read('Contents/section0.xml').decode('utf-8')
            self.assertIn('width="59528"', section)
            self.assertIn('<opf:title>제목 &lt;1&gt;</opf:title>', archive.read('Contents/content.hpf').decode('utf-8'))
        self.assertTrue(inspect_static(result['path'])['ok'])

    def test_header_references_resolve(self) -> None:
        root = ET.fromstring(header_xml().split('?>', 1)[1])
        ids = {(el.tag.split('}')[-1], el.get('id')) for el in root.iter() if el.get('id') is not None}
        self.assertIn(('charPr', '0'), ids)
        self.assertIn(('paraPr', '0'), ids)
        self.assertIn(('borderFill', '2'), ids)
        self.assertIn(('numbering', '1'), ids)
        for group in root.iter():
            count = group.get('itemCnt')
            if count is not None:
                self.assertEqual(int(count), len(list(group)), group.tag)

    def test_paper_and_margins(self) -> None:
        page = page_setup(paper='letter', margins_mm={'left': 25.4, 'top': None})
        self.assertEqual(page['width'], mm_to_hwpunit(215.9))
        self.assertEqual(page['left'], 7200)
        self.assertEqual(page['top'], 5668)
        custom = page_setup(width_mm=100, height_mm=150)
        self.assertEqual((custom['width'], custom['height']), (mm_to_hwpunit(100), mm_to_hwpunit(150)))

    def test_refusals(self) -> None:
        cases = (
            ({'paper': 'A9'}, 'unknown paper'),
            ({'width_mm': 100}, 'both width_mm and height_mm'),
            ({'width_mm': 20, 'height_mm': 100}, '50..1000'),
            ({'margins_mm': {'left': 400}}, '0..300'),
            ({'margins_mm': {'inside': 1}}, 'unknown margin'),
            ({'paper': 'A5', 'margins_mm': {'left': 70, 'right': 70}}, 'less than 10 mm'),
        )
        for options, message in cases:
            with self.subTest(options=options), self.assertRaisesRegex(HwpxNewError, message):
                page_setup(**options)
        target = self.tmp / 'x.hwpx'
        write_blank_hwpx(target)
        with self.assertRaisesRegex(HwpxNewError, 'already exists'):
            write_blank_hwpx(target)
        write_blank_hwpx(target, overwrite=True)
        with self.assertRaisesRegex(HwpxNewError, 'must end with .hwpx'):
            write_blank_hwpx(self.tmp / 'x.hwp')
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), ['x.hwpx'])

    def test_cli(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['new', str(self.tmp / 'cli.hwpx'), '--paper', 'B5', '--margin-left-mm', '20', '--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['page_hwpunit']['left'], mm_to_hwpunit(20))
        self.assertEqual(payload['evidence'], 'generated-package')


@unittest.skipIf(os.name == 'nt', 'fake renderer is a POSIX script')
class MermaidRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / 'd.mmd'
        self.source.write_text('graph TD; A-->B', encoding='utf-8')
        self.log = self.tmp / 'argv.json'
        (self.tmp / 'ok.png').write_bytes(_png(40, 20))

    def _fake(self, body: str) -> str:
        script = self.tmp / 'mmdc'
        script.write_text(
            f'#!{sys.executable}\n'
            'import json, pathlib, sys, time\n'
            f'pathlib.Path({str(self.log)!r}).write_text(json.dumps(sys.argv[1:]))\n'
            'args = sys.argv[1:]\n'
            "out = pathlib.Path(args[args.index('-o') + 1])\n"
            "config = pathlib.Path(args[args.index('-c') + 1]).read_text()\n"
            f'{body}\n',
            encoding='utf-8',
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return str(script)

    def test_renders_png_with_strict_security(self) -> None:
        mmdc = self._fake(f"assert 'strict' in config\nout.write_bytes(pathlib.Path({str(self.tmp / 'ok.png')!r}).read_bytes())")
        result = render_png(self.source, self.tmp / 'out.png', renderer=mmdc, width=800)
        self.assertEqual((result['width_px'], result['height_px']), (40, 20))
        self.assertEqual(png_size((self.tmp / 'out.png').read_bytes()), (40, 20))
        argv = json.loads(self.log.read_text())
        self.assertIn('-w', argv)
        self.assertEqual(argv[argv.index('-b') + 1], 'white')

    def test_failures(self) -> None:
        cases = (
            ("sys.stderr.write('Parse error'); sys.exit(1)", 'failed \\(exit 1\\): Parse error'),
            ("out.write_bytes(b'not a png')", 'not a PNG'),
            ('pass', 'wrote no PNG'),
            ('time.sleep(5)', 'timed out'),
        )
        for body, message in cases:
            with self.subTest(body=body), self.assertRaisesRegex(MermaidRenderError, message):
                render_png(self.source, self.tmp / 'out.png', renderer=self._fake(body), timeout=1)
        self.assertFalse((self.tmp / 'out.png').exists())

    def test_input_checks(self) -> None:
        mmdc = self._fake('pass')
        empty = self.tmp / 'empty.mmd'
        empty.write_text('  ', encoding='utf-8')
        big = self.tmp / 'big.mmd'
        big.write_text('x' * (100 * 1024 + 1), encoding='utf-8')
        for source, out, options, message in (
            (self.tmp / 'missing.mmd', 'o.png', {}, 'not found'),
            (empty, 'o.png', {}, 'empty'),
            (big, 'o.png', {}, 'exceeds'),
            (self.source, 'o.jpg', {}, '.png'),
            (self.source, 'o.png', {'scale': 9}, 'scale'),
            (self.source, 'o.png', {'background': 'red'}, 'background'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(MermaidRenderError, message):
                render_png(source, self.tmp / out, renderer=mmdc, **options)
        with patch.dict(os.environ, {'PATH': str(self.tmp / 'nowhere')}, clear=True):
            with self.assertRaisesRegex(MermaidRenderError, 'not found'):
                render_png(self.source, self.tmp / 'o.png')


class CommandStatusTests(unittest.TestCase):
    def test_new_commands_are_documented_as_local(self) -> None:
        status = build_command_status(build_parser())
        for name in ('new', 'mermaid-render'):
            self.assertEqual(status[name]['source'], 'parser-local')
            self.assertIn('no server call', status[name]['note'])


if __name__ == '__main__':
    unittest.main()
