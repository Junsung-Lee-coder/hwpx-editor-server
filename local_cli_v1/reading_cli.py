"""Local read-only HWPX reading subcommands (no Hancom, no server call).

Integration into ``local_cli_v1/main.py`` (the integrator adds exactly this):

1. In ``build_parser()``, right after ``subparsers = parser.add_subparsers(...)``::

       reading_cli.register(subparsers)

2. At the top of the ``try:`` block in ``main()``, before the other
   ``if args.command == ...`` branches::

       if args.command in reading_cli.READING_COMMANDS:
           return reading_cli.dispatch(args)

   plus the import ``from . import reading_cli`` next to the other local imports.

Optional: ``LOCAL_METADATA_NOTES.update(reading_cli.READING_COMMAND_NOTES)`` makes
``hwpx command-status`` classify these as local read-only commands instead of
``direct-backlog``.

Every output states ``evidence: local-static-hwpx``: it is static XML reading,
not Hancom-rendered proof (no page numbers or layout).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .hwpx_reader import (
    EVIDENCE,
    EVIDENCE_NOTE,
    HwpxReadError,
    chunk_document,
    export_html,
    export_text,
    load_document,
    outline,
    position_index,
    search_chunks,
    word_count,
)

READING_COMMAND_NOTES: dict[str, str] = {
    'doc-chunks': 'local read-only HWPX paragraph chunking; static evidence only, not Hancom-rendered proof',
    'doc-search': 'local read-only HWPX chunk search; static evidence only, not Hancom-rendered proof',
    'doc-index': 'local read-only HWPX paragraph position index; static evidence only, no page numbers',
    'doc-outline': 'local read-only HWPX outline/table of contents; static evidence only',
    'doc-export': 'local read-only HWPX text/HTML export; never writes the source document',
    'doc-stats': 'local read-only HWPX word/character counts; static evidence only',
}
READING_COMMANDS = frozenset(READING_COMMAND_NOTES)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be >= 1')
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be >= 0')
    return number


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add the doc-* reading subcommands to an existing subparsers action."""
    chunks = subparsers.add_parser('doc-chunks', help='Local read-only: split a .hwpx into ordered paragraph chunks')
    chunks.add_argument('file', type=Path)
    chunks.add_argument('--max-chars', type=_positive_int, default=2000)
    chunks.add_argument('--overlap', type=_non_negative_int, default=1, help='Paragraphs repeated between chunks')
    chunks.add_argument('--json', action='store_true')
    chunks.set_defaults(reading_handler=handle_doc_chunks)

    search = subparsers.add_parser('doc-search', help='Local read-only: search .hwpx chunks (quote for exact phrase)')
    search.add_argument('file', type=Path)
    search.add_argument('query')
    search.add_argument('--limit', type=_positive_int, default=10)
    search.add_argument('--max-chars', type=_positive_int, default=2000)
    search.add_argument('--json', action='store_true')
    search.set_defaults(reading_handler=handle_doc_search)

    index = subparsers.add_parser('doc-index', help='Local read-only: per-paragraph position index of a .hwpx')
    index.add_argument('file', type=Path)
    index.add_argument('--json', action='store_true')
    index.set_defaults(reading_handler=handle_doc_index)

    toc = subparsers.add_parser('doc-outline', help='Local read-only: outline headings (table of contents) of a .hwpx')
    toc.add_argument('file', type=Path)
    toc.add_argument('--json', action='store_true')
    toc.set_defaults(reading_handler=handle_doc_outline)

    export = subparsers.add_parser('doc-export', help='Local read-only: export .hwpx body as plain text or escaped HTML')
    export.add_argument('file', type=Path)
    export.add_argument('--format', choices=('text', 'html'), required=True)
    export.add_argument('--out', type=Path, help='Write to this path instead of stdout')
    export.set_defaults(reading_handler=handle_doc_export)

    stats = subparsers.add_parser('doc-stats', help='Local read-only: word/character/paragraph counts of a .hwpx')
    stats.add_argument('file', type=Path)
    stats.add_argument('--json', action='store_true')
    stats.set_defaults(reading_handler=handle_doc_stats)


def dispatch(args: argparse.Namespace) -> int:
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, 'reading_handler', None)
    if handler is None:
        print(f'error: unknown reading command {getattr(args, "command", None)!r}', file=sys.stderr)
        return 2
    return handler(args)


def _fail(message: str) -> int:
    print(f'error: {message}', file=sys.stderr)
    return 2


def _envelope(command: str, doc: dict[str, Any], **payload: Any) -> dict[str, Any]:
    return {
        'command': command,
        'evidence': EVIDENCE,
        'evidence_note': EVIDENCE_NOTE,
        'file': doc['path'],
        **payload,
    }


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _banner(command: str, doc: dict[str, Any]) -> None:
    print(f'{command}: {doc["path"]}')
    print(f'evidence: {EVIDENCE} ({EVIDENCE_NOTE})')


def _preview(text: str, width: int = 80) -> str:
    compact = ' '.join(text.split())
    return compact if len(compact) <= width else compact[: width - 1] + '…'


def _load(args: argparse.Namespace) -> dict[str, Any]:
    return load_document(Path(args.file).expanduser())


def handle_doc_chunks(args: argparse.Namespace) -> int:
    try:
        doc = _load(args)
        chunks = chunk_document(doc, max_chars=args.max_chars, overlap_paragraphs=args.overlap)
    except (HwpxReadError, ValueError, OSError) as exc:
        return _fail(str(exc))
    if args.json:
        _print_json(_envelope('doc-chunks', doc, max_chars=args.max_chars, overlap_paragraphs=args.overlap,
                              chunk_count=len(chunks), chunks=chunks))
        return 0
    _banner('doc-chunks', doc)
    print(f'chunks: {len(chunks)} (max_chars={args.max_chars}, overlap={args.overlap})')
    for chunk in chunks:
        path = ' > '.join(chunk['heading_path'])
        print(f'{chunk["id"]} p{chunk["paragraph_start"]}-{chunk["paragraph_end"]} '
              f'chars {chunk["char_start"]}-{chunk["char_end"]}' + (f' [{path}]' if path else ''))
        print(f'  {_preview(chunk["text"])}')
    return 0


def handle_doc_search(args: argparse.Namespace) -> int:
    try:
        doc = _load(args)
        chunks = chunk_document(doc, max_chars=args.max_chars, overlap_paragraphs=0)
        hits = search_chunks(chunks, args.query, limit=args.limit)
    except (HwpxReadError, ValueError, OSError) as exc:
        return _fail(str(exc))
    if args.json:
        _print_json(_envelope('doc-search', doc, query=args.query, hit_count=len(hits), hits=hits))
        return 0
    _banner('doc-search', doc)
    print(f'query: {args.query!r} hits: {len(hits)}')
    for hit in hits:
        print(f'{hit["id"]} score={hit["score"]} p{hit["paragraph_start"]}-{hit["paragraph_end"]}: {hit["snippet"]}')
    return 0


def handle_doc_index(args: argparse.Namespace) -> int:
    try:
        doc = _load(args)
    except (HwpxReadError, OSError) as exc:
        return _fail(str(exc))
    rows = position_index(doc)
    if args.json:
        _print_json(_envelope('doc-index', doc, paragraph_count=len(rows), paragraphs=rows))
        return 0
    _banner('doc-index', doc)
    for row in rows:
        where = f'tbl{row["table_index"]}[r{row["row"]},c{row["col"]}]' if row['in_table'] else 'body'
        heading = f' H{row["outline_level"]}' if row['outline_level'] else ''
        text = doc['paragraphs'][row['global_index']]['text']
        print(f'#{row["global_index"]} s{row["section_index"]}:{row["section_paragraph_index"]} '
              f'{row["char_start"]}-{row["char_end"]} {where}{heading} {_preview(text, 60)}')
    return 0


def handle_doc_outline(args: argparse.Namespace) -> int:
    try:
        doc = _load(args)
    except (HwpxReadError, OSError) as exc:
        return _fail(str(exc))
    items = outline(doc)
    if args.json:
        _print_json(_envelope('doc-outline', doc, heading_count=len(items), headings=items))
        return 0
    _banner('doc-outline', doc)
    if not items:
        print('(no outline headings found)')
    for item in items:
        print(f'{"  " * (item["level"] - 1)}{item["text"]}  (H{item["level"]}, p{item["paragraph_index"]})')
    return 0


def handle_doc_export(args: argparse.Namespace) -> int:
    try:
        doc = _load(args)
        content = export_html(doc) if args.format == 'html' else export_text(doc)
    except (HwpxReadError, OSError) as exc:
        return _fail(str(exc))
    if args.out is None:
        if args.format == 'text':
            print(f'# evidence: {EVIDENCE} ({EVIDENCE_NOTE})')
        sys.stdout.write(content)
        return 0
    out = Path(args.out).expanduser()
    try:
        if out.resolve() == Path(doc['path']).resolve():
            return _fail('refusing to overwrite the source document')
        out.write_text(content, encoding='utf-8')
    except OSError as exc:
        return _fail(f'cannot write {str(out)!r}: {exc.strerror or exc}')
    _print_json(_envelope('doc-export', doc, format=args.format, output=str(out), characters=len(content)))
    return 0


def handle_doc_stats(args: argparse.Namespace) -> int:
    try:
        doc = _load(args)
    except (HwpxReadError, OSError) as exc:
        return _fail(str(exc))
    stats = word_count(doc)
    if args.json:
        _print_json(_envelope('doc-stats', doc, stats=stats))
        return 0
    _banner('doc-stats', doc)
    for key, value in stats.items():
        if isinstance(value, dict):
            value = ', '.join(f'{k}={v}' for k, v in value.items())
        print(f'{key}: {value}')
    return 0
