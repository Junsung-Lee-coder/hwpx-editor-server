from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SERVER_PRIMITIVE_VERSION = 'local-cli-command-bundle/v2-style-inspect'


def scan_command_package_manifest_status(root: Path | None = None) -> dict[str, Any]:
    """Return command-package inventory without importing runtime modules."""

    commands_root = root or Path(__file__).with_name('command_packages') / 'commands'
    ops: list[str] = []
    revision_parts: list[str] = []
    if not commands_root.exists():
        return {'ops': [], 'op_count': 0, 'revision': 'missing'}
    for manifest_path in sorted(commands_root.glob('*/manifest.json')):
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except Exception as exc:
            revision_parts.append(f'{manifest_path.parent.name}/manifest.json:error:{type(exc).__name__}')
            continue
        op = str(manifest.get('op') or manifest_path.parent.name).strip()
        if op:
            ops.append(op)
        run_path = manifest_path.parent / 'run.py'
        try:
            revision_parts.append(f'{op}/manifest.json:{manifest_path.stat().st_mtime_ns}')
        except FileNotFoundError:
            revision_parts.append(f'{op}/manifest.json:missing')
        try:
            revision_parts.append(f'{op}/run.py:{run_path.stat().st_mtime_ns}')
        except FileNotFoundError:
            revision_parts.append(f'{op}/run.py:missing')
    ops = sorted(set(ops))
    return {'ops': ops, 'op_count': len(ops), 'revision': '|'.join(revision_parts)}


def build_local_cli_status_payload(
    *,
    snapshot: dict[str, Any] | None,
    active_binding: dict[str, Any] | None,
    live_bound: bool,
    command_package_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the public /local-cli/status payload from runtime/binding state.

    This helper is intentionally pure: LocalCliService owns probing, stale-binding
    cleanup, and persistence; this module only turns already-collected state into
    the stable operator-visible status shape.
    """

    snapshot_data: dict[str, Any] = snapshot if isinstance(snapshot, dict) else {}
    binding_data: dict[str, Any] = active_binding if isinstance(active_binding, dict) else {}
    raw_errors = snapshot_data.get('errors')
    errors = raw_errors if isinstance(raw_errors, list) else []
    raw_checks = snapshot_data.get('checks')
    checks = raw_checks if isinstance(raw_checks, dict) else {}
    raw_hancom_check = checks.get('hancom_automation')
    hancom_check = raw_hancom_check if isinstance(raw_hancom_check, dict) else {}
    ready = bool(snapshot_data.get('ready'))
    blocked_reason = str(errors[0]).strip() if errors else None
    session_id = str(binding_data.get('session_id') or '').strip() or None
    raw_artifacts = binding_data.get('artifacts')
    artifacts = raw_artifacts if isinstance(raw_artifacts, dict) else {}
    package_status = command_package_status if isinstance(command_package_status, dict) else scan_command_package_manifest_status()
    raw_package_ops = package_status.get('ops')
    package_ops: list[Any] = raw_package_ops if isinstance(raw_package_ops, list) else []
    last_proof_artifact = artifacts.get('latest_screenshot_path') or artifacts.get('latest_export_path')
    working_copy_dirty = bool(binding_data.get('working_copy_dirty'))
    if not live_bound:
        proof_fresh = None
        proof_fresh_reason = 'no live session is open'
    elif not last_proof_artifact:
        proof_fresh = False
        proof_fresh_reason = 'no rendered proof or export artifact is recorded'
    elif working_copy_dirty:
        proof_fresh = False
        proof_fresh_reason = 'working copy has unsaved changes after the last known proof'
    else:
        proof_fresh = True
        proof_fresh_reason = 'latest proof is not known stale'

    return {
        'ok': True,
        'runtime_up': ready,
        'hancom_attached': bool(hancom_check.get('ok')),
        'api_ready': True,
        'blocked_reason': blocked_reason,
        'next_action': (
            'open a file'
            if ready and not live_bound
            else 'continue with find/where/select or capture rendered proof before saving/reporting'
            if ready and live_bound
            else 'restore runtime readiness on the Windows Hancom worker'
        ),
        'session_id': session_id,
        'active_document': binding_data.get('source_filename'),
        'working_copy_path': binding_data.get('working_copy_path'),
        'artifacts': artifacts,
        'last_proof_artifact': last_proof_artifact,
        'proof_fresh': proof_fresh,
        'proof_fresh_reason': proof_fresh_reason,
        'live_session_bound': live_bound,
        'working_copy_dirty': working_copy_dirty,
        'command_bundle_route_active': True,
        'server_primitive_version': SERVER_PRIMITIVE_VERSION,
        'command_package_ops': package_ops,
        'command_package_op_count': int(package_status.get('op_count') or len(package_ops)),
        'command_package_revision': str(package_status.get('revision') or ''),
    }
