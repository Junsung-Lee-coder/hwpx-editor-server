from __future__ import annotations

from datetime import datetime
from typing import Any


VERIFY_EVIDENCE_POLICY_SCHEMA_VERSION = 'verify-evidence-trust-policy/v1'


def summarize_verify_evidence_policy(
    *,
    freshness_state: str,
    trust_state: str,
    frame_age_ms: int | None,
    warning_codes: list[str],
) -> str:
    age_text = f'{frame_age_ms}ms' if isinstance(frame_age_ms, int) else 'unknown-age'
    summary = f'freshness={freshness_state}; trust={trust_state}; age={age_text}'
    if warning_codes:
        summary += f"; warnings={','.join(warning_codes)}"
    return summary


def build_verify_evidence_policy(
    *,
    frame_age_ms: int | None,
    capture_relation: str,
    image_frozen: bool,
    soft_warn_age_ms: int,
    hard_stale_age_ms: int,
) -> dict[str, Any]:
    warning_codes: list[str] = []
    freshness_state = 'unknown'

    if not image_frozen:
        warning_codes.append('frame_not_frozen')

    if frame_age_ms is None:
        warning_codes.append('frame_age_unknown')
    elif frame_age_ms < 0 or capture_relation == 'captured_after_verify_step':
        freshness_state = 'temporal_mismatch'
        warning_codes.append('frame_captured_after_step')
    elif frame_age_ms <= soft_warn_age_ms:
        freshness_state = 'fresh'
    elif frame_age_ms <= hard_stale_age_ms:
        freshness_state = 'warning'
        warning_codes.append('frame_stale_soft')
    else:
        freshness_state = 'stale'
        warning_codes.append('frame_stale_hard')

    if not image_frozen or freshness_state in {'temporal_mismatch', 'stale'}:
        trust_state = 'low'
    elif freshness_state in {'warning', 'unknown'}:
        trust_state = 'caution'
    else:
        trust_state = 'trusted'

    review_required = trust_state != 'trusted'
    summary = summarize_verify_evidence_policy(
        freshness_state=freshness_state,
        trust_state=trust_state,
        frame_age_ms=frame_age_ms,
        warning_codes=warning_codes,
    )
    return {
        'schema_version': VERIFY_EVIDENCE_POLICY_SCHEMA_VERSION,
        'advisory_only': True,
        'freshness': {
            'state': freshness_state,
            'frame_age_ms': frame_age_ms,
            'capture_relation': capture_relation,
            'soft_warn_age_ms': soft_warn_age_ms,
            'hard_stale_age_ms': hard_stale_age_ms,
        },
        'trust': {
            'state': trust_state,
            'review_required': review_required,
            'image_frozen': image_frozen,
        },
        'warning_codes': warning_codes,
        'summary': summary,
    }


def resolve_verify_capture_timing(
    *,
    recorded_at: datetime | None,
    captured_at: datetime | None,
) -> tuple[int | None, str]:
    if recorded_at is None or captured_at is None:
        return None, 'unknown'

    frame_age_ms = int((recorded_at - captured_at).total_seconds() * 1000)
    if frame_age_ms > 0:
        return frame_age_ms, 'captured_before_verify_step'
    if frame_age_ms < 0:
        return frame_age_ms, 'captured_after_verify_step'
    return frame_age_ms, 'captured_at_verify_step'


def build_verify_step_binding(
    *,
    step_name: str,
    recorded_at: str,
    captured_at: str | None,
    source_image_path: str | None,
    source_metadata_path: str | None,
    observation_status: str | None,
    observation_reason_code: str | None,
    image_frozen: bool,
    frame_age_ms: int | None,
    capture_relation: str,
    soft_warn_age_ms: int,
    hard_stale_age_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = build_verify_evidence_policy(
        frame_age_ms=frame_age_ms,
        capture_relation=capture_relation,
        image_frozen=image_frozen,
        soft_warn_age_ms=soft_warn_age_ms,
        hard_stale_age_ms=hard_stale_age_ms,
    )
    binding = {
        'mode': 'step-bound-frozen-copy' if image_frozen else 'step-bound-metadata-only',
        'step': step_name,
        'verify_recorded_at': recorded_at,
        'frame_captured_at': captured_at,
        'frame_age_ms': frame_age_ms,
        'capture_relation': capture_relation,
        'image_frozen': image_frozen,
        'source_image_path': source_image_path,
        'source_metadata_path': source_metadata_path,
        'observation_status': observation_status,
        'observation_reason_code': observation_reason_code,
        'freshness_policy': policy['freshness'],
        'trust_policy': policy['trust'],
        'warning_codes': policy['warning_codes'],
        'policy_summary': policy['summary'],
    }
    binding['summary'] = (
        f"{step_name} bound at {recorded_at}; frame captured_at={captured_at or 'unknown'}; "
        f"age_ms={frame_age_ms if frame_age_ms is not None else 'unknown'}; "
        f"observation={observation_status or observation_reason_code or 'unknown'}; "
        f"policy={policy['summary']}"
    )
    return binding, policy
