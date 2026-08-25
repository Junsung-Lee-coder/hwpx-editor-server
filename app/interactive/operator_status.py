from __future__ import annotations

from typing import Any, Sequence


def build_command_progress(
    session: dict[str, Any],
    *,
    progress_command_sequence: Sequence[str],
    terminal_session_states: set[str],
    now_iso: str,
) -> dict[str, Any]:
    history = session.get('command_history') if isinstance(session.get('command_history'), list) else []
    completed = [
        item.get('command')
        for item in history
        if isinstance(item, dict)
        and item.get('state') == 'succeeded'
        and isinstance(item.get('command'), str)
        and item.get('command') in progress_command_sequence
    ]
    current_command = session.get('current_command')
    current_state = 'idle'
    if history and isinstance(history[-1], dict):
        current_state = str(history[-1].get('state') or 'idle')
    next_expected = None
    for command_name in progress_command_sequence:
        if command_name not in completed:
            next_expected = command_name
            break
    if session.get('state') in terminal_session_states:
        next_expected = None
    return {
        'command': current_command,
        'state': current_state,
        'completed_count': len(completed),
        'total_count': len(progress_command_sequence),
        'next_expected_command': next_expected,
        'last_updated_at': now_iso,
    }


def build_verify_gui_brief(verify_state: dict[str, Any]) -> str | None:
    gui = verify_state.get('gui') if isinstance(verify_state.get('gui'), dict) else {}
    binding = gui.get('step_binding') if isinstance(gui.get('step_binding'), dict) else {}
    if not binding:
        return None
    age_ms = binding.get('frame_age_ms')
    age_text = f'{age_ms}ms' if isinstance(age_ms, int) else 'unknown-age'
    relation = binding.get('capture_relation') or 'unknown'
    mode = binding.get('mode') or 'unknown'
    freshness_policy = binding.get('freshness_policy') if isinstance(binding.get('freshness_policy'), dict) else {}
    trust_policy = binding.get('trust_policy') if isinstance(binding.get('trust_policy'), dict) else {}
    freshness = freshness_policy.get('state') or 'unknown'
    trust = trust_policy.get('state') or 'unknown'
    observation = binding.get('observation_status') or binding.get('observation_reason_code') or 'unknown'
    return f'{mode} | {relation} | age={age_text} | freshness={freshness} | trust={trust} | observation={observation}'


def render_operator_status(
    session: dict[str, Any],
    *,
    progress_command_sequence: Sequence[str],
    terminal_session_states: set[str],
    now_iso: str,
) -> tuple[dict[str, Any], list[str], str]:
    find_result = session.get('find_result') if isinstance(session.get('find_result'), dict) else {}
    choose_result = session.get('choose_result') if isinstance(session.get('choose_result'), dict) else {}
    runtime_preparation = session.get('runtime_preparation') if isinstance(session.get('runtime_preparation'), dict) else {}
    verify_pre = session.get('verify_pre') if isinstance(session.get('verify_pre'), dict) else {}
    verify_post = session.get('verify_post') if isinstance(session.get('verify_post'), dict) else {}
    popup = session.get('popup_status') if isinstance(session.get('popup_status'), dict) else {}
    failure = session.get('failure_reason') if isinstance(session.get('failure_reason'), dict) else None
    readiness = session.get('readiness') if isinstance(session.get('readiness'), dict) else {}
    live_runtime = session.get('live_runtime') if isinstance(session.get('live_runtime'), dict) else {}

    progress = build_command_progress(
        session,
        progress_command_sequence=progress_command_sequence,
        terminal_session_states=terminal_session_states,
        now_iso=now_iso,
    )

    source_path = str(session.get('source_path') or '-')
    failure_summary = 'none'
    if failure:
        failure_summary = str(failure.get('message') or failure.get('code') or 'reported')
    candidate_count = int(find_result.get('candidate_count') or 0)
    selected_target_id = (
        runtime_preparation.get('resolved_target_id')
        or (runtime_preparation.get('selected_target_variant') or {}).get('resolved_target_id')
        or (choose_result.get('selected_candidate') or {}).get('resolved_target_id')
        or '-'
    )
    bridge_state = runtime_preparation.get('state') or 'not_prepared'

    lines = [
        f"Session state : {session.get('state', 'unknown')}",
        f"Current cmd   : {progress.get('command') or '-'} [{progress.get('state')}]",
        f"Progress      : {progress.get('completed_count', 0)}/{progress.get('total_count', 0)} complete; next={progress.get('next_expected_command') or '-'}",
        f"Source        : {source_path}",
        f"Find/choose   : candidates={candidate_count} | selected={selected_target_id}",
        f"Bridge        : {bridge_state} | next={runtime_preparation.get('next_step') or '-'}",
        f"Live runtime  : {live_runtime.get('mode', 'telemetry-only')} | doc={live_runtime.get('document_state', '-')} | last={live_runtime.get('last_command') or '-'}",
        f"Readiness     : {readiness.get('status', 'unknown')} | ready={readiness.get('ready')} | worker={readiness.get('worker_name', '-')}",
        f"Verify-pre    : {verify_pre.get('state', 'pending')} | {verify_pre.get('summary') or '-'}",
        f"Verify-post   : {verify_post.get('state', 'pending')} | {verify_post.get('summary') or '-'}",
        f"Popup/module  : {popup.get('state', 'unknown')} | module={popup.get('security_module_name') or '-'} | popup_detected={popup.get('popup_detected')}",
        f"Failure       : {failure_summary}",
    ]
    verify_pre_gui_brief = build_verify_gui_brief(verify_pre)
    if verify_pre_gui_brief:
        lines.insert(7, f'Verify-pre GUI: {verify_pre_gui_brief}')
    verify_post_gui_brief = build_verify_gui_brief(verify_post)
    if verify_post_gui_brief:
        insertion_index = 9 if verify_pre_gui_brief else 8
        lines.insert(insertion_index, f'Verify-post GUI: {verify_post_gui_brief}')
    return progress, lines, '\n'.join(lines)
