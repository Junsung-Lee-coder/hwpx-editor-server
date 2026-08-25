from __future__ import annotations

import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

settings = get_settings()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def desktop_session_time_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def observation_root() -> Path:
    return settings.spool_root / 'observation'


def viewer_session_path() -> Path:
    return observation_root() / 'viewer_session.json'


def latest_frame_path() -> Path:
    return observation_root() / 'latest_frame.png'


def latest_frame_metadata_path() -> Path:
    return observation_root() / 'latest_frame.json'


def _viewer_base_url() -> str:
    return f'http://{settings.api_host}:{settings.api_port}'


def load_viewer_session() -> dict[str, Any] | None:
    path = viewer_session_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def ensure_viewer_session() -> dict[str, Any]:
    base_url = _viewer_base_url()
    existing = load_viewer_session() or {}
    payload: dict[str, Any] = {
        'ok': True,
        'viewer_session_id': str(existing.get('viewer_session_id') or uuid.uuid4().hex),
        'started_at': str(existing.get('started_at') or utc_now_iso()),
        'updated_at': utc_now_iso(),
        'mode': 'strict_view_only',
        'remote_control_permitted': False,
        'public_exposure': False,
        'bind_host': settings.api_host,
        'bind_port': settings.api_port,
        'viewer_url': f'{base_url}/observation-viewer',
        'stream_url': f'{base_url}/observation-viewer/stream.mjpg',
        'latest_frame_url': f'{base_url}/observation-viewer/frame/latest.png',
        'latest_frame_metadata_url': f'{base_url}/observation-viewer/frame/latest.json',
        'session_metadata_url': f'{base_url}/observation-viewer/session',
        'scope': 'hwpx_editor_observer_v1',
        'policy': {
            'managed_mode': 'strict_view_only',
            'remote_control': False,
            'public_exposure': False,
            'counted_run_requires_in_frame': [
                'live_hancom_window',
                'job_id',
                'execution_run_id_or_run_label',
                'desktop_session_time',
            ],
        },
    }
    if isinstance(existing.get('last_frame'), dict):
        payload['last_frame'] = existing['last_frame']
    path = viewer_session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def update_viewer_session(**updates: Any) -> dict[str, Any]:
    payload = ensure_viewer_session()
    for key, value in updates.items():
        if value is not None:
            payload[key] = value
    payload['updated_at'] = utc_now_iso()
    path = viewer_session_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def _job_observation_status_path(job_dir: Path) -> Path:
    return job_dir / 'metadata' / 'observation_status.json'


def _job_observation_frame_metadata_path(job_dir: Path) -> Path:
    return job_dir / 'metadata' / 'observation_frame_latest.json'


def _job_observation_frame_image_path(job_dir: Path) -> Path:
    return job_dir / 'metadata' / 'observation_frame_latest.png'


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ''):
            return None
        return int(value)
    except Exception:
        return None


def _build_marker(job_dir: Path, marker: dict[str, Any] | None, window_snapshot: dict[str, Any]) -> dict[str, Any]:
    marker = dict(marker or {})
    job_id = str(marker.get('job_id') or job_dir.name)
    execution_run_id = str(marker.get('execution_run_id') or marker.get('run_label') or job_id)
    run_label = str(marker.get('run_label') or execution_run_id or job_id)
    return {
        'job_id': job_id,
        'execution_run_id': execution_run_id,
        'run_label': run_label,
        'window_handle': _safe_int(marker.get('window_handle')) or _safe_int(window_snapshot.get('window_handle')),
        'window_pid': _safe_int(marker.get('window_pid')) or _safe_int(window_snapshot.get('window_pid')),
        'window_title': str(marker.get('window_title') or window_snapshot.get('window_title') or ''),
        'window_class': str(marker.get('window_class') or window_snapshot.get('window_class') or ''),
    }


def _merge_same_run_visible_window_binding(job_dir: Path, *, marker: dict[str, Any], window_snapshot: dict[str, Any]) -> dict[str, Any]:
    resolved = dict(window_snapshot)
    current_handle = _safe_int(resolved.get('window_handle'))
    if current_handle and bool(resolved.get('window_visible')):
        return resolved

    frame_meta = _load_json(_job_observation_frame_metadata_path(job_dir)) or {}
    if not frame_meta:
        return resolved
    if not bool(frame_meta.get('ok')):
        return resolved
    if str(frame_meta.get('job_id') or '') != str(marker.get('job_id') or ''):
        return resolved
    frame_run_id = str(frame_meta.get('execution_run_id') or frame_meta.get('run_label') or '')
    marker_run_id = str(marker.get('execution_run_id') or marker.get('run_label') or '')
    if frame_run_id != marker_run_id:
        return resolved

    fallback_handle = _safe_int(frame_meta.get('window_handle'))
    if not fallback_handle:
        return resolved

    resolved['window_handle'] = fallback_handle
    if _safe_int(frame_meta.get('window_pid')) is not None:
        resolved['window_pid'] = _safe_int(frame_meta.get('window_pid'))
    if frame_meta.get('window_title'):
        resolved['window_title'] = frame_meta.get('window_title')
    if frame_meta.get('window_class'):
        resolved['window_class'] = frame_meta.get('window_class')
    resolved['window_visible'] = bool(frame_meta.get('window_visible'))
    resolved['resolved_via_same_run_frame_fallback'] = True
    return resolved


def _rect_payload(left: int, top: int, right: int, bottom: int) -> dict[str, int]:
    return {
        'left': int(left),
        'top': int(top),
        'right': int(right),
        'bottom': int(bottom),
        'width': max(1, int(right) - int(left)),
        'height': max(1, int(bottom) - int(top)),
    }


def _set_process_dpi_awareness() -> dict[str, Any]:
    if sys.platform != 'win32':
        return {'platform': sys.platform, 'attempted': False}

    attempts: list[dict[str, Any]] = []
    try:
        import ctypes

        user32 = ctypes.windll.user32
        # Prefer per-monitor v2 so Win32 coordinates match physical pixels used
        # by ImageGrab. If the process already has a DPI context, Windows returns
        # access denied; record that rather than failing screenshot capture.
        try:
            ok = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
            attempts.append({'method': 'SetProcessDpiAwarenessContext', 'context': 'PER_MONITOR_AWARE_V2', 'ok': ok})
            if ok:
                return {'attempted': True, 'ok': True, 'method': 'SetProcessDpiAwarenessContext', 'attempts': attempts}
        except Exception as exc:
            attempts.append({'method': 'SetProcessDpiAwarenessContext', 'ok': False, 'error': repr(exc)})

        try:
            shcore = ctypes.windll.shcore
            result = int(shcore.SetProcessDpiAwareness(2))
            attempts.append({'method': 'SetProcessDpiAwareness', 'awareness': 'PROCESS_PER_MONITOR_DPI_AWARE', 'ok': result == 0, 'result': result})
            if result == 0:
                return {'attempted': True, 'ok': True, 'method': 'SetProcessDpiAwareness', 'attempts': attempts}
        except Exception as exc:
            attempts.append({'method': 'SetProcessDpiAwareness', 'ok': False, 'error': repr(exc)})

        try:
            ok = bool(user32.SetProcessDPIAware())
            attempts.append({'method': 'SetProcessDPIAware', 'ok': ok})
            if ok:
                return {'attempted': True, 'ok': True, 'method': 'SetProcessDPIAware', 'attempts': attempts}
        except Exception as exc:
            attempts.append({'method': 'SetProcessDPIAware', 'ok': False, 'error': repr(exc)})
    except Exception as exc:
        attempts.append({'method': 'dpi_awareness_setup', 'ok': False, 'error': repr(exc)})
    return {'attempted': True, 'ok': False, 'attempts': attempts}


def _dwm_extended_frame_rect(window_handle: int) -> dict[str, int] | None:
    if sys.platform != 'win32':
        return None
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ('left', ctypes.c_long),
                ('top', ctypes.c_long),
                ('right', ctypes.c_long),
                ('bottom', ctypes.c_long),
            ]

        rect = RECT()
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.wintypes.HWND(int(window_handle)) if hasattr(ctypes, 'wintypes') else ctypes.c_void_p(int(window_handle)),
            ctypes.c_uint(9),  # DWMWA_EXTENDED_FRAME_BOUNDS
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if int(result) != 0:
            return None
        if int(rect.right) <= int(rect.left) or int(rect.bottom) <= int(rect.top):
            return None
        payload = _rect_payload(rect.left, rect.top, rect.right, rect.bottom)
        payload['source'] = 'dwm_extended_frame_bounds'
        return payload
    except Exception:
        return None


def _win32_window_rect(window_handle: int) -> dict[str, int] | None:
    try:
        import win32gui  # type: ignore

        left, top, right, bottom = win32gui.GetWindowRect(window_handle)
        payload = _rect_payload(left, top, right, bottom)
        payload['source'] = 'get_window_rect'
        return payload
    except Exception:
        return None


def _virtual_screen_rect() -> dict[str, int] | None:
    if sys.platform != 'win32':
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        left = int(user32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
        top = int(user32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
        width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
        height = int(user32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            return None
        payload = _rect_payload(left, top, left + width, top + height)
        payload['source'] = 'virtual_screen_metrics'
        return payload
    except Exception:
        return None


def _rect_from_payload(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        left = int(value['left'])
        top = int(value['top'])
        right = int(value['right'])
        bottom = int(value['bottom'])
    except Exception:
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _clip_rect_to_bounds(rect: tuple[int, int, int, int], bounds: tuple[int, int, int, int] | None) -> tuple[int, int, int, int]:
    if bounds is None:
        return rect
    left, top, right, bottom = rect
    b_left, b_top, b_right, b_bottom = bounds
    clipped = (max(left, b_left), max(top, b_top), min(right, b_right), min(bottom, b_bottom))
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        return rect
    return clipped


def _resolve_full_frame_capture_rect(window_handle: int, window_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    dpi_meta = _set_process_dpi_awareness()
    candidates: list[dict[str, Any]] = []
    window_snapshot = dict(window_snapshot or {})
    # Live CLI screenshots are proof of the operator-visible Hancom editor, not
    # just the child editing pane. Start from the top-level/DWM frame so title
    # bar controls, ribbon, scrollbars, status bar, and zoom bar stay in frame.
    for key in ('full_frame_rect', 'dwm_extended_frame_rect', 'window_rect', 'resolved_window_rect'):
        rect = _rect_from_payload(window_snapshot.get(key))
        if rect:
            payload = _rect_payload(*rect)
            payload['source'] = f'window_snapshot.{key}'
            candidates.append(payload)

    dwm_rect = _dwm_extended_frame_rect(window_handle)
    if dwm_rect:
        candidates.insert(0, dwm_rect)
    win32_rect = _win32_window_rect(window_handle)
    if win32_rect:
        candidates.append(win32_rect)
    if not candidates:
        raise RuntimeError('No usable Hancom full-frame rectangle was available for capture.')

    full_frame = candidates[0]
    full_tuple = _rect_from_payload(full_frame)
    if full_tuple is None:
        raise RuntimeError('Resolved Hancom full-frame rectangle was invalid.')
    virtual_rect_payload = _virtual_screen_rect()
    virtual_tuple = _rect_from_payload(virtual_rect_payload)
    capture_tuple = _clip_rect_to_bounds(full_tuple, virtual_tuple)
    capture_rect = _rect_payload(*capture_tuple)
    capture_rect['source'] = 'full_frame_rect_clipped_to_virtual_screen' if capture_tuple != full_tuple else 'full_frame_rect'
    return {
        'dpi_awareness': dpi_meta,
        'full_frame_rect': full_frame,
        'capture_rect': capture_rect,
        'virtual_screen_rect': virtual_rect_payload,
        'rect_candidates': candidates,
    }


def _capture_window_image(window_handle: int, *, window_snapshot: dict[str, Any] | None = None):
    import win32gui  # type: ignore
    from PIL import Image, ImageGrab

    rect_meta = _resolve_full_frame_capture_rect(window_handle, window_snapshot=window_snapshot)
    capture_rect = rect_meta['capture_rect']
    rect = _rect_from_payload(capture_rect)
    if rect is None:
        raise RuntimeError(f'Invalid capture rectangle: {capture_rect}')
    left, top, right, bottom = rect
    width = max(1, right - left)
    height = max(1, bottom - top)
    capture_meta: dict[str, Any] = {
        'capture_method': None,
        'requested_window_handle': int(window_handle),
        'requested_rect': rect_meta.get('full_frame_rect') or _rect_payload(left, top, right, bottom),
        'full_frame_rect': rect_meta.get('full_frame_rect'),
        'capture_rect': capture_rect,
        'virtual_screen_rect': rect_meta.get('virtual_screen_rect'),
        'dpi_awareness': rect_meta.get('dpi_awareness'),
        'rect_candidates': rect_meta.get('rect_candidates'),
        'capture_attempts': [],
    }

    # Prefer the actual desktop pixels for Hancom. PrintWindow can report success
    # while rendering only a child/editing surface into the top-left of the target
    # bitmap. A visible screen-region grab matches what the operator sees and
    # avoids that clipped Hancom artifact. Keep PrintWindow as a fallback for
    # environments where screen capture is blocked.
    try:
        try:
            image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True, include_layered_windows=True).copy()
            grab_kwargs = {'all_screens': True, 'include_layered_windows': True}
        except TypeError:
            image = ImageGrab.grab(bbox=(left, top, right, bottom)).copy()
            grab_kwargs = {'all_screens': 'unsupported', 'include_layered_windows': 'unsupported'}
        capture_meta['capture_attempts'].append({'method': 'screen_region', 'ok': True, 'grab_kwargs': grab_kwargs})
        capture_meta['capture_method'] = 'screen_region'
        capture_meta['capture_rect'] = _rect_payload(left, top, right, bottom)
        capture_meta['capture_rect']['source'] = capture_rect.get('source') or 'full_frame_rect'
        capture_meta['image_size'] = {'width': int(image.width), 'height': int(image.height)}
        return image, capture_meta
    except Exception as exc:
        capture_meta['capture_attempts'].append({'method': 'screen_region', 'ok': False, 'error': repr(exc)})

    import win32ui  # type: ignore
    from ctypes import windll

    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        hwnd_dc = win32gui.GetWindowDC(window_handle)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        result = windll.user32.PrintWindow(window_handle, save_dc.GetSafeHdc(), 3)
        if result == 1:
            bmpinfo = bitmap.GetInfo()
            bmpstr = bitmap.GetBitmapBits(True)
            image = Image.frombuffer(
                'RGB',
                (bmpinfo['bmWidth'], bmpinfo['bmHeight']),
                bmpstr,
                'raw',
                'BGRX',
                0,
                1,
            ).copy()
            capture_meta['capture_attempts'].append({'method': 'print_window', 'ok': True, 'flags': 3})
            capture_meta['capture_method'] = 'print_window_fallback'
            capture_meta['capture_rect'] = _rect_payload(left, top, right, bottom)
            capture_meta['capture_rect']['source'] = capture_rect.get('source') or 'full_frame_rect'
            capture_meta['image_size'] = {'width': int(image.width), 'height': int(image.height)}
            return image, capture_meta
        capture_meta['capture_attempts'].append({'method': 'print_window', 'ok': False, 'result': int(result)})
    finally:
        try:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
        except Exception:
            pass
        try:
            if save_dc is not None:
                save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            if hwnd_dc is not None:
                win32gui.ReleaseDC(window_handle, hwnd_dc)
        except Exception:
            pass

    raise RuntimeError(f'Unable to capture window image: {capture_meta}')


def _query_caret_marker(window_handle: int, *, capture_rect: dict[str, Any]) -> dict[str, Any]:
    # PDF/page proofs cannot prove the live insertion point. Overlay the OS caret
    # location when Windows exposes it so a default screenshot can show where the
    # next edit will land even when Hancom's caret blink is between frames.
    marker: dict[str, Any] = {
        'status': 'unavailable',
        'source': 'GetGUIThreadInfo',
        'attempts': [],
    }
    rect_tuple = _rect_from_payload(capture_rect)
    if sys.platform != 'win32' or rect_tuple is None:
        marker['reason'] = 'windows_required_or_invalid_capture_rect'
        return marker
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ('left', wintypes.LONG),
                ('top', wintypes.LONG),
                ('right', wintypes.LONG),
                ('bottom', wintypes.LONG),
            ]

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ('cbSize', wintypes.DWORD),
                ('flags', wintypes.DWORD),
                ('hwndActive', wintypes.HWND),
                ('hwndFocus', wintypes.HWND),
                ('hwndCapture', wintypes.HWND),
                ('hwndMenuOwner', wintypes.HWND),
                ('hwndMoveSize', wintypes.HWND),
                ('hwndCaret', wintypes.HWND),
                ('rcCaret', RECT),
            ]

        user32 = ctypes.windll.user32
        target_thread = int(user32.GetWindowThreadProcessId(wintypes.HWND(int(window_handle)), None))
        thread_ids = [target_thread, 0] if target_thread else [0]
        cap_left, cap_top, cap_right, cap_bottom = rect_tuple
        for thread_id in thread_ids:
            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(info)
            ok = bool(user32.GetGUIThreadInfo(wintypes.DWORD(thread_id), ctypes.byref(info)))
            attempt: dict[str, Any] = {'thread_id': int(thread_id), 'ok': ok}
            marker['attempts'].append(attempt)
            if not ok:
                continue
            hwnd_caret = int(info.hwndCaret or 0)
            attempt['hwndCaret'] = hwnd_caret
            attempt['hwndFocus'] = int(info.hwndFocus or 0)
            if not hwnd_caret:
                attempt['reason'] = 'hwndCaret_missing'
                continue

            left = int(info.rcCaret.left)
            top = int(info.rcCaret.top)
            right = int(info.rcCaret.right)
            bottom = int(info.rcCaret.bottom)
            if right <= left:
                right = left + 2
            if bottom <= top:
                bottom = top + 18

            pt1 = wintypes.POINT(left, top)
            pt2 = wintypes.POINT(right, bottom)
            if not user32.ClientToScreen(wintypes.HWND(hwnd_caret), ctypes.byref(pt1)):
                attempt['reason'] = 'client_to_screen_failed_top_left'
                continue
            if not user32.ClientToScreen(wintypes.HWND(hwnd_caret), ctypes.byref(pt2)):
                attempt['reason'] = 'client_to_screen_failed_bottom_right'
                continue
            screen_rect = _rect_payload(pt1.x, pt1.y, pt2.x, pt2.y)
            image_rect = _rect_payload(pt1.x - cap_left, pt1.y - cap_top, pt2.x - cap_left, pt2.y - cap_top)
            in_capture = screen_rect['right'] >= cap_left and screen_rect['left'] <= cap_right and screen_rect['bottom'] >= cap_top and screen_rect['top'] <= cap_bottom
            marker.update(
                {
                    'status': 'visible' if in_capture else 'outside_capture',
                    'thread_id': int(thread_id),
                    'hwndCaret': hwnd_caret,
                    'hwndFocus': int(info.hwndFocus or 0),
                    'screen_rect': screen_rect,
                    'image_rect': image_rect,
                    'in_capture_rect': bool(in_capture),
                }
            )
            return marker
        marker['reason'] = 'hwndCaret_missing'
    except Exception as exc:
        marker['reason'] = repr(exc)
    return marker


def _draw_caret_marker(image, caret_marker: dict[str, Any]) -> None:
    if caret_marker.get('status') not in {'visible', 'outside_capture'}:
        return
    rect = _rect_from_payload(caret_marker.get('image_rect'))
    if rect is None:
        return
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    left, top, right, bottom = rect
    left = max(0, min(int(image.width) - 1, left))
    right = max(0, min(int(image.width) - 1, right))
    top = max(0, min(int(image.height) - 1, top))
    bottom = max(0, min(int(image.height) - 1, bottom))
    if right <= left:
        right = min(int(image.width) - 1, left + 3)
    if bottom <= top:
        bottom = min(int(image.height) - 1, top + 18)
    pad = 10
    box = (max(0, left - pad), max(0, top - pad), min(int(image.width) - 1, right + pad), min(int(image.height) - 1, bottom + pad))
    draw.rectangle(box, outline=(255, 48, 48), width=4)
    center_x = (left + right) // 2
    draw.line((center_x, max(0, top - 18), center_x, min(int(image.height) - 1, bottom + 18)), fill=(255, 48, 48), width=3)
    draw.text((box[0] + 4, max(0, box[1] - 14)), 'CARET', fill=(255, 48, 48))


def _write_overlay_frame(image, *, marker: dict[str, Any], window_snapshot: dict[str, Any], session: dict[str, Any], capture_meta: dict[str, Any] | None = None, caret_marker: dict[str, Any] | None = None) -> dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont

    desktop_time = desktop_session_time_iso()
    capture_meta = dict(capture_meta or {})
    caret_marker = dict(caret_marker or {})
    capture_method = str(capture_meta.get('capture_method') or 'unknown')
    # Keep the banner outside the captured pixels. The captured area should stay
    # recognizable as the live Hancom frame; metadata is added above it.
    banner_height = 140
    canvas = Image.new('RGB', (image.width, image.height + banner_height), (15, 23, 42))
    image = image.convert('RGB')
    _draw_caret_marker(image, caret_marker)
    canvas.paste(image, (0, banner_height))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    line1 = f"job_id={marker['job_id']} run={marker['execution_run_id']} label={marker['run_label']}"
    line2 = (
        f"desktop_session_time={desktop_time} window_handle={marker.get('window_handle') or 'missing'} "
        f"window_pid={marker.get('window_pid') or 'missing'}"
    )
    line3 = (
        f"window_title={str(window_snapshot.get('window_title') or marker.get('window_title') or '').strip()[:120]} "
        f"mode={session.get('mode')}"
    )
    line4 = f"capture_method={capture_method} rect={capture_meta.get('capture_rect') or window_snapshot.get('window_rect') or 'missing'}"
    cursor_context = window_snapshot.get('cursor_context') if isinstance(window_snapshot.get('cursor_context'), dict) else {}
    paragraph_preview = str(window_snapshot.get('current_paragraph_preview') or '').strip()
    if len(paragraph_preview) > 80:
        paragraph_preview = paragraph_preview[:77] + '...'
    line5 = (
        f"caret_marker={caret_marker.get('status') or 'unavailable'} "
        f"caret_rect={caret_marker.get('screen_rect') or 'missing'} "
        f"cursor={cursor_context.get('pos') or cursor_context.get('cell_addr') or 'unknown'} preview={paragraph_preview or 'none'}"
    )
    draw.text((10, 10), line1, fill=(255, 255, 255), font=font)
    draw.text((10, 32), line2, fill=(196, 230, 255), font=font)
    draw.text((10, 54), line3, fill=(196, 230, 255), font=font)
    draw.text((10, 76), line4, fill=(196, 230, 255), font=font)
    draw.text((10, 98), line5, fill=(255, 220, 160), font=font)

    frame_path = latest_frame_path()
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(frame_path, format='PNG')

    frame_meta: dict[str, Any] = {
        'ok': True,
        'captured_at': utc_now_iso(),
        'desktop_session_time': desktop_time,
        'job_id': marker['job_id'],
        'execution_run_id': marker['execution_run_id'],
        'run_label': marker['run_label'],
        'window_handle': marker.get('window_handle'),
        'window_pid': marker.get('window_pid'),
        'window_title': window_snapshot.get('window_title') or marker.get('window_title'),
        'window_class': window_snapshot.get('window_class') or marker.get('window_class'),
        'window_visible': bool(window_snapshot.get('window_visible')),
        'capture_method': capture_method,
        'full_frame_rect': capture_meta.get('full_frame_rect') or window_snapshot.get('full_frame_rect') or window_snapshot.get('dwm_extended_frame_rect') or window_snapshot.get('window_rect'),
        'capture_rect': capture_meta.get('capture_rect'),
        'capture_metadata': capture_meta,
        'caret_marker': caret_marker,
        'requested_window_handle': window_snapshot.get('requested_window_handle') or capture_meta.get('requested_window_handle'),
        'requested_window_rect': window_snapshot.get('requested_window_rect') or capture_meta.get('requested_rect'),
        'resolved_window_rect': window_snapshot.get('window_rect') or capture_meta.get('capture_rect'),
        'window_handle_resolution': window_snapshot.get('window_handle_resolution'),
        'window_is_maximized': window_snapshot.get('window_is_maximized'),
        'viewer_session_id': session.get('viewer_session_id'),
        'mode': session.get('mode'),
        'remote_control_permitted': False,
        'public_exposure': False,
        'frame_path': str(frame_path),
        'frame_url': session.get('latest_frame_url'),
        'stream_url': session.get('stream_url'),
        'session_metadata_url': session.get('session_metadata_url'),
        'marker_visible_in_frame': True,
        'desktop_session_time_visible_in_frame': True,
        'caret_marker_visible_in_frame': caret_marker.get('status') == 'visible',
        'same_window_binding': {
            'window_handle_match': _safe_int(marker.get('window_handle')) == _safe_int(window_snapshot.get('window_handle')),
            'window_pid_match': _safe_int(marker.get('window_pid')) == _safe_int(window_snapshot.get('window_pid')),
        },
    }
    _write_json(latest_frame_metadata_path(), frame_meta)
    update_viewer_session(
        last_frame={
            'job_id': frame_meta['job_id'],
            'execution_run_id': frame_meta['execution_run_id'],
            'captured_at': frame_meta['captured_at'],
            'frame_path': frame_meta['frame_path'],
            'capture_method': frame_meta['capture_method'],
            'capture_rect': frame_meta['capture_rect'],
            'window_handle': frame_meta['window_handle'],
            'window_pid': frame_meta['window_pid'],
        }
    )
    return frame_meta


def _capture_frame(job_dir: Path, *, window_snapshot: dict[str, Any], marker: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    frame_meta: dict[str, Any] = {
        'ok': False,
        'captured_at': utc_now_iso(),
        'job_id': marker['job_id'],
        'execution_run_id': marker['execution_run_id'],
        'run_label': marker['run_label'],
        'window_handle': marker.get('window_handle'),
        'window_pid': marker.get('window_pid'),
        'viewer_session_id': session.get('viewer_session_id'),
        'frame_url': session.get('latest_frame_url'),
        'stream_url': session.get('stream_url'),
        'session_metadata_url': session.get('session_metadata_url'),
    }
    if sys.platform != 'win32':
        frame_meta['reason_code'] = 'windows_required'
        return frame_meta

    window_handle = _safe_int(window_snapshot.get('window_handle'))
    if not window_handle:
        frame_meta['reason_code'] = 'visible_hancom_window_missing'
        return frame_meta

    try:
        import win32gui  # type: ignore
    except Exception as exc:
        frame_meta['reason_code'] = 'screenshot_stream_missing'
        frame_meta['detail'] = str(exc)
        return frame_meta

    if not win32gui.IsWindow(window_handle) or not win32gui.IsWindowVisible(window_handle):
        frame_meta['reason_code'] = 'visible_hancom_window_missing'
        return frame_meta

    try:
        image, capture_meta = _capture_window_image(window_handle, window_snapshot=window_snapshot)
        caret_marker = _query_caret_marker(window_handle, capture_rect=capture_meta.get('capture_rect') or {})
        frame_meta = _write_overlay_frame(image, marker=marker, window_snapshot=window_snapshot, session=session, capture_meta=capture_meta, caret_marker=caret_marker)
        shutil.copyfile(latest_frame_path(), _job_observation_frame_image_path(job_dir))
        _write_json(_job_observation_frame_metadata_path(job_dir), frame_meta)
        return frame_meta
    except Exception as exc:
        frame_meta['reason_code'] = 'screenshot_stream_missing'
        frame_meta['detail'] = repr(exc)
        _write_json(_job_observation_frame_metadata_path(job_dir), frame_meta)
        return frame_meta


def _build_observation_status(*, marker: dict[str, Any], window_snapshot: dict[str, Any], frame_meta: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    visible_window = bool(window_snapshot.get('window_visible')) and bool(window_snapshot.get('window_handle'))
    marker_present = bool(marker.get('job_id')) and bool(marker.get('execution_run_id') or marker.get('run_label'))
    desktop_time_present = bool(frame_meta.get('desktop_session_time_visible_in_frame')) and bool(frame_meta.get('desktop_session_time'))
    stream_present = bool(session.get('viewer_session_id')) and bool(frame_meta.get('frame_url')) and bool(frame_meta.get('stream_url')) and bool(frame_meta.get('ok'))
    window_handle_match = bool(frame_meta.get('same_window_binding', {}).get('window_handle_match'))
    window_pid_match = bool(frame_meta.get('same_window_binding', {}).get('window_pid_match')) or frame_meta.get('window_pid') in (None, '')
    same_window_binding = window_handle_match and window_pid_match

    if not visible_window:
        reason_code = 'visible_hancom_window_missing'
    elif not marker_present:
        reason_code = 'run_local_marker_missing'
    elif not desktop_time_present:
        reason_code = 'desktop_session_time_missing'
    elif not stream_present:
        reason_code = 'screenshot_stream_missing'
    elif not same_window_binding:
        reason_code = 'same_run_window_binding_mismatch'
    else:
        reason_code = 'trust_complete'

    trust_complete = reason_code == 'trust_complete'
    return {
        'observed_at': utc_now_iso(),
        'observation_status': 'trust_complete' if trust_complete else 'not_trust_complete',
        'reason_code': reason_code,
        'counted_toward_observation_bar': trust_complete,
        'trust_complete_on_observation_surface': trust_complete,
        'job_id': marker.get('job_id'),
        'execution_run_id': marker.get('execution_run_id'),
        'run_label': marker.get('run_label'),
        'window_handle': window_snapshot.get('window_handle'),
        'window_pid': window_snapshot.get('window_pid'),
        'window_title': window_snapshot.get('window_title'),
        'window_class': window_snapshot.get('window_class'),
        'window_visible': bool(window_snapshot.get('window_visible')),
        'viewer_session_id': session.get('viewer_session_id'),
        'viewer_mode': session.get('mode'),
        'frame_path': frame_meta.get('frame_path'),
        'frame_url': frame_meta.get('frame_url'),
        'stream_url': frame_meta.get('stream_url'),
        'session_metadata_url': frame_meta.get('session_metadata_url') or session.get('session_metadata_url'),
        'desktop_session_time': frame_meta.get('desktop_session_time'),
    }


def observe_job(job_dir: Path, *, window_snapshot: dict[str, Any], marker: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    session = ensure_viewer_session()
    preliminary_marker = _build_marker(job_dir, marker, window_snapshot)
    resolved_window_snapshot = _merge_same_run_visible_window_binding(
        job_dir,
        marker=preliminary_marker,
        window_snapshot=window_snapshot,
    )
    resolved_marker = _build_marker(job_dir, marker, resolved_window_snapshot)
    frame_meta = _capture_frame(job_dir, window_snapshot=resolved_window_snapshot, marker=resolved_marker, session=session)
    payload = _build_observation_status(
        marker=resolved_marker,
        window_snapshot=resolved_window_snapshot,
        frame_meta=frame_meta,
        session=session,
    )
    _write_json(_job_observation_status_path(job_dir), payload)
    update_viewer_session(
        active_job_id=resolved_marker.get('job_id'),
        active_execution_run_id=resolved_marker.get('execution_run_id'),
        last_observation_status=payload,
    )
    return payload, frame_meta
