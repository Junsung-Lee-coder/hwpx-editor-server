"""Render a Mermaid diagram to PNG with mermaid-cli (``mmdc``) for insertion as an image.

The renderer is an external program: set ``HWPX_MMDC`` to its absolute path or
put ``mmdc`` on ``PATH``. It runs without a shell, under a timeout, with
Mermaid's ``securityLevel: strict``, and its output is accepted only if it is
a real, bounded PNG. Inserting the image goes through the existing image
upload route, so the rendered file is the only thing sent to the server.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any

MAX_SOURCE_BYTES = 100 * 1024
MAX_PNG_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 90
_PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


class MermaidRenderError(RuntimeError):
    """The diagram could not be rendered into an acceptable PNG."""


def find_renderer(explicit: str | None = None) -> str:
    candidate = explicit or os.environ.get('HWPX_MMDC') or shutil.which('mmdc')
    if not candidate:
        raise MermaidRenderError('mermaid-cli not found: install @mermaid-js/mermaid-cli and put mmdc on PATH, or set HWPX_MMDC')
    path = Path(candidate).expanduser()
    if not path.is_file():
        raise MermaidRenderError(f'mermaid renderer is not a file: {path}')
    return str(path)


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or not data.startswith(_PNG_MAGIC) or data[12:16] != b'IHDR':
        raise MermaidRenderError('renderer output is not a PNG image')
    width, height = struct.unpack('>II', data[16:24])
    if width <= 0 or height <= 0:
        raise MermaidRenderError('renderer produced an empty PNG')
    return width, height


def render_png(source: str | Path, out_path: str | Path, *, scale: float = 2.0, width: int | None = None,
               background: str = 'white', renderer: str | None = None,
               timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    source_path = Path(source).expanduser()
    if not source_path.is_file():
        raise MermaidRenderError(f'Mermaid source not found: {source_path}')
    raw = source_path.read_bytes()
    if not raw.strip():
        raise MermaidRenderError('Mermaid source is empty')
    if len(raw) > MAX_SOURCE_BYTES:
        raise MermaidRenderError(f'Mermaid source exceeds {MAX_SOURCE_BYTES} bytes')
    try:
        raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise MermaidRenderError('Mermaid source must be UTF-8 text') from exc
    target = Path(out_path).expanduser()
    if target.suffix.lower() != '.png':
        raise MermaidRenderError('output must be a .png file')
    if not 0.5 <= float(scale) <= 5.0:
        raise MermaidRenderError('scale must be 0.5..5')
    if width is not None and not 100 <= int(width) <= 8000:
        raise MermaidRenderError('width must be 100..8000 px')
    if background not in ('white', 'transparent'):
        raise MermaidRenderError('background must be white or transparent')
    executable = find_renderer(renderer)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hwpx-mermaid-') as work:
        work_dir = Path(work)
        input_path = work_dir / 'diagram.mmd'
        input_path.write_bytes(raw)
        config_path = work_dir / 'config.json'
        config_path.write_text(json.dumps({'securityLevel': 'strict'}), encoding='utf-8')
        output_path = work_dir / 'diagram.png'
        command = [executable, '-i', str(input_path), '-o', str(output_path), '-b', background, '-s', str(scale), '-c', str(config_path)]
        if width is not None:
            command += ['-w', str(int(width))]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise MermaidRenderError(f'mermaid renderer timed out after {timeout}s') from exc
        except OSError as exc:
            raise MermaidRenderError(f'mermaid renderer could not start: {exc}') from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or b'').decode('utf-8', errors='replace').strip()[-500:]
            raise MermaidRenderError(f'mermaid renderer failed (exit {completed.returncode}): {detail}')
        if not output_path.is_file():
            raise MermaidRenderError('mermaid renderer reported success but wrote no PNG')
        data = output_path.read_bytes()
        if len(data) > MAX_PNG_BYTES:
            raise MermaidRenderError(f'rendered PNG exceeds {MAX_PNG_BYTES} bytes')
        png_width, png_height = png_size(data)
        staged = target.with_name(f'.{target.name}.partial')
        staged.write_bytes(data)
        os.replace(staged, target)
    return {
        'ok': True,
        'path': str(target),
        'width_px': png_width,
        'height_px': png_height,
        'size_bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
        'source_sha256': hashlib.sha256(raw).hexdigest(),
        'renderer': Path(executable).name,
    }
