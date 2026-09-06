"""Bounded page rendering with the existing verified Poppler resolver."""
from pathlib import Path
import shutil
import subprocess
import tempfile

from app.poppler import resolve_pdftoppm, PopplerResolutionError


class ProofError(ValueError):
    pass


def render_page(pdf: Path, destination: Path, *, page: int, dpi: int) -> None:
    try:
        renderer = resolve_pdftoppm()
        if not renderer.ok or renderer.path is None:
            raise ProofError('Poppler is unavailable.')
        with tempfile.TemporaryDirectory(prefix='hwpx-mcp-render-') as raw:
            prefix = Path(raw) / 'page'
            subprocess.run([str(renderer.path), '-r', str(dpi), '-f', str(page), '-l', str(page),
                            '-png', str(pdf), str(prefix)], check=True, timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rendered = list(prefix.parent.glob('page-*.png'))
            if len(rendered) != 1:
                raise ProofError('Renderer did not produce exactly the requested page.')
            destination.parent.mkdir(parents=True, exist_ok=True)
            with rendered[0].open('rb') as source, destination.open('xb') as target:
                shutil.copyfileobj(source, target)
    except (OSError, subprocess.SubprocessError, PopplerResolutionError) as exc:
        raise ProofError('Native PDF page rendering failed or exceeded its 60-second limit.') from exc
