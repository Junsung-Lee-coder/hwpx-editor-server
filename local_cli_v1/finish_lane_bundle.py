from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import load_state, update_state
from .transport import ApiError, DEFAULT_BASE_URL, download_to_path, get_json, post_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='hwpx-finish-lane-bundle',
        description='Capture a thin local-cli save/export proof bundle for the active HWPX session',
    )
    parser.add_argument(
        '--base-url',
        default=None,
        help='HWPX server base URL (defaults to cached session base URL, then HWPX_BASE_URL, then http://127.0.0.1:8765)',
    )
    parser.add_argument('--bundle-dir', type=Path, help='Optional output directory for the proof bundle')
    parser.add_argument('--close', action='store_true', help='Close the live session after bundling')
    return parser


def _require_session_state() -> dict[str, Any]:
    state = load_state()
    session_id = str(state.get('session_id') or '').strip()
    if not session_id:
        raise ApiError('No active local CLI session state was found. Open a document first.')
    return state


def _resolve_base_url(explicit_base_url: str | None, state: dict[str, Any]) -> str:
    if explicit_base_url and explicit_base_url.strip():
        return explicit_base_url.rstrip('/')
    cached_base_url = str(state.get('base_url') or '').strip()
    if cached_base_url:
        return cached_base_url.rstrip('/')
    return DEFAULT_BASE_URL


def _bundle_dir(state: dict[str, Any], requested: Path | None) -> Path:
    if requested is not None:
        requested.mkdir(parents=True, exist_ok=True)
        return requested

    source_path_raw = str(state.get('source_path') or '').strip()
    if source_path_raw:
        source_path = Path(source_path_raw).expanduser()
        parent = source_path.parent
        stem = source_path.stem
    else:
        parent = Path.cwd()
        stem = Path(str(state.get('source_filename') or 'document.hwpx')).stem or 'document'

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    bundle_dir = parent / f'{stem}-local-cli-proof-{stamp}'
    bundle_dir.mkdir(parents=True, exist_ok=True)
    return bundle_dir


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def _write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding='utf-8')
    return path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        state = _require_session_state()
        base_url = _resolve_base_url(args.base_url, state)
        session_id = str(state.get('session_id') or '').strip()
        bundle_dir = _bundle_dir(state, args.bundle_dir)

        status_before = get_json(base_url, '/local-cli/status')
        where_before = post_json(base_url, '/local-cli/where', {'session_id': session_id})
        save_result = post_json(base_url, '/local-cli/save', {'session_id': session_id})
        status_after_save = get_json(base_url, '/local-cli/status')
        screenshot_result = post_json(base_url, '/local-cli/screenshot', {'session_id': session_id})
        export_result = post_json(base_url, '/local-cli/export', {'session_id': session_id})

        edited_filename = str(save_result.get('filename') or 'edited.hwpx')
        edited_copy_path = download_to_path(
            base_url,
            str(save_result.get('download_path') or ''),
            bundle_dir / edited_filename,
        )
        screenshot_path = download_to_path(
            base_url,
            str(screenshot_result.get('download_path') or ''),
            bundle_dir / 'editor-screenshot.png',
        )
        pdf_path = download_to_path(
            base_url,
            str(export_result.get('download_path') or ''),
            bundle_dir / 'exported.pdf',
        )

        close_result: dict[str, Any] | None = None
        if args.close:
            close_result = post_json(base_url, '/local-cli/close', {'session_id': session_id})
        closed_ok = bool(close_result and close_result.get('ok'))

        _write_json(bundle_dir / 'state.json', state)
        _write_json(bundle_dir / 'status-before.json', status_before)
        _write_json(bundle_dir / 'where-before.json', where_before)
        _write_json(bundle_dir / 'save-result.json', save_result)
        _write_json(bundle_dir / 'status-after-save.json', status_after_save)
        _write_json(bundle_dir / 'screenshot-result.json', screenshot_result)
        _write_json(bundle_dir / 'export-result.json', export_result)
        if close_result is not None:
            _write_json(bundle_dir / 'close-result.json', close_result)

        summary_lines = [
            f"session_id: {session_id}",
            f"source_file: {state.get('source_path') or state.get('source_filename') or 'unknown'}",
            f"bundle_dir: {bundle_dir}",
            f"saved_working_copy_on_server: {'yes' if save_result.get('ok') else 'no'}",
            f"edited_working_copy: {edited_copy_path}",
            f"pdf: {pdf_path}",
            f"screenshot: {screenshot_path}",
            f"closed: {'yes' if closed_ok else 'no'}",
        ]
        _write_text(bundle_dir / 'SUMMARY.txt', '\n'.join(summary_lines) + '\n')

        def _update_finished_state(current: dict[str, Any]) -> dict[str, Any]:
            current.update({
                'base_url': base_url,
                'last_saved_working_copy_path': str(edited_copy_path),
                'last_screenshot_path': str(screenshot_path),
                'last_export_path': str(pdf_path),
                'last_finish_lane_bundle_path': str(bundle_dir),
            })
            if closed_ok:
                current.pop('session_id', None)
                current.pop('last_find_query', None)
            return current

        update_state(_update_finished_state)

        manifest = {
            'schema_version': 'local-cli-finish-lane-bundle/v1',
            'created_at_local': datetime.now().isoformat(timespec='seconds'),
            'base_url': base_url,
            'session_id': session_id,
            'source_filename': state.get('source_filename'),
            'source_path': state.get('source_path'),
            'bundle_dir': str(bundle_dir),
            'saved_working_copy_on_server': bool(save_result.get('ok')),
            'artifacts': {
                'edited_working_copy_path': str(edited_copy_path),
                'screenshot_path': str(screenshot_path),
                'pdf_path': str(pdf_path),
            },
            'records': {
                'state': str(bundle_dir / 'state.json'),
                'status_before': str(bundle_dir / 'status-before.json'),
                'where_before': str(bundle_dir / 'where-before.json'),
                'save_result': str(bundle_dir / 'save-result.json'),
                'status_after_save': str(bundle_dir / 'status-after-save.json'),
                'screenshot_result': str(bundle_dir / 'screenshot-result.json'),
                'export_result': str(bundle_dir / 'export-result.json'),
                'summary': str(bundle_dir / 'SUMMARY.txt'),
            },
            'closed_after_bundle': closed_ok,
        }
        if close_result is not None:
            manifest['records']['close_result'] = str(bundle_dir / 'close-result.json')
        _write_json(bundle_dir / 'manifest.json', manifest)

        print(bundle_dir)
        return 0
    except ApiError as exc:
        print(f'error: {exc.message}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
