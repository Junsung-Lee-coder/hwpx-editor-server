from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_cli_v1 import main as cli  # noqa: E402
from local_cli_v1.transport import ApiError  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


RAW_PAYLOAD = {
    'ok': True,
    'summary': {'fixture': True},
    'steps': [
        {
            'op': 'export_pdf',
            'result': {
                'download_path': '/download/source.pdf',
                'artifact_path': '/server/source.pdf',
            },
        }
    ],
}


def install_fixture_monkeypatches(rendered_pages: list[int]):
    originals = {
        'download_to_path': cli.download_to_path,
        '_pdf_page_count': cli._pdf_page_count,
        '_render_pdf_page_to_png': cli._render_pdf_page_to_png,
        '_extract_pdf_page_text': cli._extract_pdf_page_text,
        'load_state': cli.load_state,
        '_execute_named_bundle': cli._execute_named_bundle,
    }

    def fake_download_to_path(base_url: str, download_path: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'%PDF-1.4\n% fixture only\n')
        return destination

    def fake_render_pdf_page_to_png(input_pdf: Path, destination: Path, *, page: int, dpi: int) -> Path:
        rendered_pages.append(page)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f'png fixture page {page} dpi {dpi}\n'.encode('utf-8'))
        return destination

    cli.download_to_path = fake_download_to_path
    cli._pdf_page_count = lambda pdf_path: (33, 'fixture')
    cli._render_pdf_page_to_png = fake_render_pdf_page_to_png
    cli._extract_pdf_page_text = lambda pdf_path, *, page: ''
    cli.load_state = lambda: {'source_path': '/fixture/source.hwpx', 'session_id': 'fixture-session'}
    cli._execute_named_bundle = lambda base_url, name, args=None: (SimpleNamespace(name=name), RAW_PAYLOAD)
    return originals


def restore_monkeypatches(originals: dict[str, object]) -> None:
    for name, value in originals.items():
        setattr(cli, name, value)


def assert_clamped_manifest(manifest: dict[str, object], rendered_pages: list[int]) -> None:
    require(rendered_pages == list(range(1, 34)), f'rendered unexpected pages: {rendered_pages!r}')
    require(manifest.get('pages_requested') == list(range(1, 36)), f'pages_requested lost original range: {manifest!r}')
    require(manifest.get('pages_rendered') == list(range(1, 34)), f'pages_rendered not clamped: {manifest!r}')
    require(manifest.get('pages_effective') == list(range(1, 34)), f'pages_effective not clamped: {manifest!r}')
    require(manifest.get('current_pdf_page_count') == 33, f'page count missing: {manifest!r}')
    require(manifest.get('page_count_source') == 'fixture', f'page count source missing: {manifest!r}')
    require(manifest.get('page_count_is_validation_proof') is False, f'page count represented as proof: {manifest!r}')
    warnings = manifest.get('warnings')
    require(isinstance(warnings, list) and len(warnings) == 1, f'expected one clamp warning: {manifest!r}')
    warning = warnings[0]
    require('requested 1-35' in warning, f'warning missing requested range: {warning!r}')
    require('current document has 33 pages' in warning, f'warning missing page count: {warning!r}')
    require('rendered 1-33' in warning, f'warning missing rendered range: {warning!r}')
    require('metadata only' in warning and 'rendered proof remains required' in warning, f'warning missing proof caveat: {warning!r}')
    manifest_path = Path(str(manifest['manifest_path']))
    saved = json.loads(manifest_path.read_text(encoding='utf-8'))
    require(saved.get('pages_requested') == list(range(1, 36)), 'saved manifest lost requested range')
    require(saved.get('pages_rendered') == list(range(1, 34)), 'saved manifest lost rendered range')


def test_direct_manifest_clamps() -> None:
    rendered_pages: list[int] = []
    originals = install_fixture_monkeypatches(rendered_pages)
    try:
        with tempfile.TemporaryDirectory(prefix='export-proof-clamp-') as temp_dir:
            manifest = cli._render_export_proof_manifest(
                base_url='http://fixture',
                raw_payload=RAW_PAYLOAD,
                pages=list(range(1, 36)),
                dpi=160,
                out_dir=Path(temp_dir),
                anchors=[],
            )
            assert_clamped_manifest(manifest, rendered_pages)
    finally:
        restore_monkeypatches(originals)


def test_cli_human_output_names_requested_and_rendered_pages() -> None:
    rendered_pages: list[int] = []
    originals = install_fixture_monkeypatches(rendered_pages)
    try:
        with tempfile.TemporaryDirectory(prefix='export-proof-clamp-cli-') as temp_dir:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = cli.main([
                    '--base-url',
                    'http://fixture',
                    'export-proof-range',
                    '--pages',
                    '1-35',
                    '--dpi',
                    '160',
                    '--out-dir',
                    temp_dir,
                ])
            output = stdout.getvalue()
            require(result == 0, f'CLI returned {result}')
            require('pages requested: 1-35' in output, f'CLI missing requested range: {output!r}')
            require('pages rendered: 1-33' in output, f'CLI missing rendered range: {output!r}')
            require('current PDF page count: 33' in output, f'CLI missing page count: {output!r}')
            require('metadata only, not validation proof' in output, f'CLI missing metadata caveat: {output!r}')
            require('warning: requested 1-35; current document has 33 pages; rendered 1-33' in output, f'CLI missing clamp warning: {output!r}')
            require(rendered_pages == list(range(1, 34)), f'CLI rendered unexpected pages: {rendered_pages!r}')
    finally:
        restore_monkeypatches(originals)


def test_all_requested_pages_out_of_bounds_fails_closed() -> None:
    rendered_pages: list[int] = []
    originals = install_fixture_monkeypatches(rendered_pages)
    try:
        with tempfile.TemporaryDirectory(prefix='export-proof-clamp-empty-') as temp_dir:
            try:
                cli._render_export_proof_manifest(
                    base_url='http://fixture',
                    raw_payload=RAW_PAYLOAD,
                    pages=[34, 35],
                    dpi=160,
                    out_dir=Path(temp_dir),
                    anchors=[],
                )
            except ApiError as exc:
                message = str(exc)
                require('requested 34-35' in message, f'out-of-bounds error missing requested range: {message!r}')
                require('current document has 33 pages' in message, f'out-of-bounds error missing page count: {message!r}')
                require('no requested pages are available' in message, f'out-of-bounds error unclear: {message!r}')
                require('metadata only' in message and 'rendered proof remains required' in message, f'out-of-bounds error missing proof caveat: {message!r}')
            else:
                raise SystemExit('all out-of-bounds requested pages succeeded unexpectedly')
            require(rendered_pages == [], f'out-of-bounds case rendered pages: {rendered_pages!r}')
    finally:
        restore_monkeypatches(originals)


def main() -> None:
    test_direct_manifest_clamps()
    test_cli_human_output_names_requested_and_rendered_pages()
    test_all_requested_pages_out_of_bounds_fails_closed()
    print('smoke_export_proof_range_clamp_static: ok')


if __name__ == '__main__':
    main()
