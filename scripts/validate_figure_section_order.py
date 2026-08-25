#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_text(path: Path) -> str:
    if path.suffix.lower() == '.json':
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            for key in ('text', 'pdf_text', 'exported_text', 'plain_text'):
                value = data.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(data, ensure_ascii=False)
    return path.read_text(encoding='utf-8', errors='replace')


def _index_or_none(text: str, token: str | None) -> int | None:
    if not token:
        return None
    found = text.find(token)
    return found if found >= 0 else None


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    text = str(payload.get('text') or '')
    section_heading = str(payload.get('section_heading') or '')
    figure_caption = str(payload.get('figure_caption') or '')
    next_heading = str(payload.get('next_heading') or '')
    image_marker = str(payload.get('image_marker') or '')

    positions = {
        'section_heading': _index_or_none(text, section_heading),
        'figure_caption': _index_or_none(text, figure_caption),
        'next_heading': _index_or_none(text, next_heading),
        'image_marker': _index_or_none(text, image_marker) if image_marker else None,
    }
    errors: list[str] = []
    for key in ('section_heading', 'figure_caption', 'next_heading'):
        if positions[key] is None:
            errors.append(f'missing required token: {key}')
    if not errors:
        if not (positions['section_heading'] < positions['figure_caption'] < positions['next_heading']):  # type: ignore[operator]
            errors.append('token order failed: expected section_heading < figure_caption < next_heading')
    if image_marker and positions['image_marker'] is None:
        errors.append('missing optional image_marker supplied for adjacency check')
    if image_marker and positions['image_marker'] is not None and positions['figure_caption'] is not None:
        if not (positions['section_heading'] is None or positions['section_heading'] < positions['image_marker'] < positions['figure_caption']):  # type: ignore[operator]
            errors.append('image_marker adjacency/order failed: expected section_heading < image_marker < figure_caption')

    return {
        'ok': not errors,
        'positions': positions,
        'errors': errors,
        'checked': {
            'section_heading': section_heading,
            'figure_caption': figure_caption,
            'next_heading': next_heading,
            'image_marker': image_marker or None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Validate figure-section token order from exported text or proof JSON.')
    parser.add_argument('text_or_json', type=Path)
    parser.add_argument('--section-heading', required=True)
    parser.add_argument('--figure-caption', required=True)
    parser.add_argument('--next-heading', required=True)
    parser.add_argument('--image-marker', help='Optional textual/control marker expected between heading and caption')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    text = _load_text(args.text_or_json)
    result = validate(
        {
            'text': text,
            'section_heading': args.section_heading,
            'figure_caption': args.figure_caption,
            'next_heading': args.next_heading,
            'image_marker': args.image_marker,
        }
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('PASS' if result['ok'] else 'FAIL')
        for error in result['errors']:
            print(f'- {error}')
        print(f"positions: {result['positions']}")
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
