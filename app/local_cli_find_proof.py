from __future__ import annotations

from typing import Any


def _normalize_visible_text(value: Any) -> str:
    return ' '.join(str(value or '').split()).strip()


def resolve_find_proof_match_target(matches: list[dict[str, Any]], *, proof_match: int) -> dict[str, Any]:
    if proof_match <= 0:
        raise ValueError('proof match number must be 1 or greater')
    if proof_match > len(matches):
        raise LookupError(f'No find match number {proof_match}.')
    match = matches[proof_match - 1]
    query = _normalize_visible_text(match.get('text') or match.get('excerpt'))
    if not query:
        raise ValueError(f'find match {proof_match} has no searchable text')
    normalized_query = query.casefold()
    occurrence = 1
    for prior_match in matches[: proof_match - 1]:
        prior_query = _normalize_visible_text(prior_match.get('text') or prior_match.get('excerpt'))
        if prior_query and prior_query.casefold() == normalized_query:
            occurrence += 1
    return {
        'number': proof_match,
        'query': query,
        'occurrence': occurrence,
        'match': dict(match),
    }
