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


def render_pages(pdf: Path, destination: Path, *, dpi: int, max_pages: int) -> list[Path]:
    """Every page up to the limit as page-N.png in a new directory; exceeding the limit is reported, never silent."""
    try:
        renderer = resolve_pdftoppm()
        if not renderer.ok or renderer.path is None:
            raise ProofError('Poppler is unavailable.')
        destination.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='hwpx-mcp-render-') as raw:
            prefix = Path(raw) / 'page'
            subprocess.run([str(renderer.path), '-r', str(dpi), '-f', '1', '-l', str(max_pages), '-png', str(pdf), str(prefix)],
                           check=True, timeout=180, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            rendered = sorted(prefix.parent.glob('page-*.png'), key=lambda path: int(path.stem.rsplit('-', 1)[1]))
            if not rendered:
                raise ProofError('Renderer produced no page.')
            pages = []
            for number, item in enumerate(rendered, start=1):
                target = destination / f'page-{number}.png'
                with item.open('rb') as source, target.open('xb') as stream:
                    shutil.copyfileobj(source, stream)
                pages.append(target)
            return pages
    except (OSError, ValueError, subprocess.SubprocessError, PopplerResolutionError) as exc:
        raise ProofError('Native PDF page rendering failed or exceeded its time limit.') from exc


def pdf_page_texts(pdf: Path, *, max_pages: int) -> list[str] | None:
    """Text per page with Poppler's pdftotext beside the resolved pdftoppm; None when that tool is unavailable."""
    try:
        renderer = resolve_pdftoppm()
        if not renderer.ok or renderer.path is None:
            return None
        tool = renderer.path.with_name('pdftotext' + renderer.path.suffix)
        if not tool.is_file():
            return None
        done = subprocess.run([str(tool), '-enc', 'UTF-8', '-layout', '-f', '1', '-l', str(max_pages), str(pdf), '-'],
                              check=True, timeout=60, capture_output=True)
        pages = done.stdout.decode('utf-8', 'replace').split('\f')
        return pages[:-1] if pages and not pages[-1].strip() else pages
    except (OSError, subprocess.SubprocessError, PopplerResolutionError):
        return None
