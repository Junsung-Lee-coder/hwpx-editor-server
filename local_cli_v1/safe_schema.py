from __future__ import annotations

from typing import Any, Mapping


SAFE_OPERATION_GROUPS: dict[str, dict[str, Any]] = {
    'read_info': {
        'purpose': 'Read-only document/session/status inspection and static diagnostics.',
        'commands': [
            'help',
            'command-status',
            'status',
            'session-health',
            'bundle-health',
            'state',
            'find',
            'info',
            'where',
            'context',
            'readback',
            'read-context',
            'read-manifest',
            'readback-diff',
            'selection-proof',
            'selected-text-proof',
            'section-control-inventory',
            'section-table-frame-inventory',
            'table-cell-structure-exact',
            'static-info',
            'static-read',
            'static-compare',
        ],
    },
    'render_secondary_or_proof': {
        'purpose': 'Render/proof artifact creation; static-render is secondary only, Hancom render is required for final QA.',
        'commands': ['static-render', 'screenshot', 'page-screenshot', 'export', 'export-proof-range'],
    },
    'planning_only': {
        'purpose': 'Local planning/specification helpers that do not execute document writes.',
        'commands': [
            'bundle-list',
            'bundles',
            'bundle-help',
            'bundle-dump',
            'create-bundle',
            'bundle-create',
            'bundle-compose',
            'tx-preview',
            'field-fill-plan',
            'output-format-policy',
        ],
    },
    'gate': {
        'purpose': 'Gate/verdict fan-in helpers; they never authorize external send by themselves.',
        'commands': ['gate-verdict'],
    },
}

DENIED_OPERATION_CLASSES = [
    'production_mutation',
    'hancom_live_write',
    'tx_commit',
    'command_bundle_execute',
    'xml_patch',
    'zip_repack',
    'bindata_replace',
    'executable_patch',
    'third_party_hwp_mcp_activation',
    'raw_hwpx_mutate',
]


def _status_dict(value: Mapping[str, Mapping[str, str]] | None) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    return {str(command): dict(meta) for command, meta in value.items()}


def build_safe_agent_schema(command_status: Mapping[str, Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Build an agent-facing allowlist for non-mutating HWPX operations.

    The schema is intentionally conservative: it exposes only read/info,
    render/proof, planning, and gate helpers. Live mutation commands, direct
    XML/ZIP/BinData edits, and command-bundle execution paths are denied.
    """

    status = _status_dict(command_status)
    operation_groups: dict[str, dict[str, Any]] = {}
    allowed: set[str] = set()
    for group_name, raw_group in SAFE_OPERATION_GROUPS.items():
        commands = [command for command in raw_group['commands'] if not status or command in status]
        allowed.update(commands)
        operation_groups[group_name] = {
            'purpose': raw_group['purpose'],
            'commands': commands,
        }

    denied_parser_commands = sorted(command for command in status if command not in allowed)
    return {
        'schema_version': 'local-cli/safe-agent-schema/v1',
        'ok': True,
        'read_only': True,
        'authority': 'planning_only',
        'mutation_allowed': False,
        'direct_package_mutation_allowed': False,
        'production_write_path': 'hancom_native_only',
        'qa_claim_allowed': False,
        'not_final_qa_evidence': True,
        'operation_groups': operation_groups,
        'denied_operation_classes': DENIED_OPERATION_CLASSES,
        'denied_parser_commands': denied_parser_commands,
        'safety_contract': [
            'Use allowed commands only for read/render/info/planning/gate work.',
            'Do not execute live mutation, tx-commit, raw XML patch, ZIP repack, BinData replacement, or third-party hwp-mcp activation through this schema.',
            'Static/rhwp/XML evidence remains secondary diagnostic evidence and cannot produce final QA PASS without Hancom-native readback/render proof.',
        ],
    }
