from __future__ import annotations

import argparse
import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from local_cli_v1 import bundles  # noqa: E402
from local_cli_v1.bundles import build_named_bundle, bundle_help  # noqa: E402
from local_cli_v1.main import NO_MUTATION_TEXT, _print_command_payload, build_command_status, build_parser, main as cli_main  # noqa: E402


ALLOWED_STATUSES = {'bundle-backed', 'direct-backlog', 'disabled'}


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = cli_main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


def parser_command_names() -> set[str]:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise SystemExit('parser has no subparsers')


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def require_cli_help_workflow() -> None:
    rc, stdout, stderr = run_cli(['help', 'workflow'])
    require(rc == 0, f'help workflow returned {rc}: {stderr}')
    require('status -> open -> find/where/select -> edit -> screenshot/export-proof -> save -> close' in stdout, 'workflow loop missing')
    require('Target pipeline:' in stdout, 'target pipeline section missing')
    require('Proof rules:' in stdout, 'proof rules section missing')
    require(NO_MUTATION_TEXT in stdout, 'disabled no-mutation wording missing from workflow help')


def require_command_status() -> None:
    public_commands = parser_command_names()
    command_status = build_command_status(build_parser())
    missing = public_commands - set(command_status)
    require(not missing, f'command-status missing parser commands: {sorted(missing)}')
    recipe_names = {recipe.name for recipe in bundles.iter_bundle_recipes()}
    missing_recipes = recipe_names - set(command_status)
    require(not missing_recipes, f'command-status missing bundle recipes: {sorted(missing_recipes)}')
    blocked_names = set(bundles.blocked_bundle_names())
    missing_blocked = blocked_names - set(command_status)
    require(not missing_blocked, f'command-status missing blocked bundle names: {sorted(missing_blocked)}')
    bad_status = {name: meta.get('status') for name, meta in command_status.items() if meta.get('status') not in ALLOWED_STATUSES}
    require(not bad_status, f'command-status has invalid statuses: {bad_status!r}')

    rc, stdout, stderr = run_cli(['command-status'])
    require(rc == 0, f'command-status returned {rc}: {stderr}')
    require('command status:' in stdout, 'command-status header missing')
    require('derived from: argparse parser + local bundle registry' in stdout, 'derived-source line missing')
    require('runtime: unknown' in stdout, 'runtime unknown line missing')
    for name in sorted(public_commands):
        require(f'- {name}: ' in stdout, f'command-status output missing public command {name}')
    for name in sorted(recipe_names | blocked_names):
        require(f'- {name}: ' in stdout, f'command-status output missing registry/blocked name {name}')
    for name in ('section-frame-fill', 'section-graphic-remove-or-hide', 'style-apply', 'style-clone', 'replace'):
        line = next((line for line in stdout.splitlines() if line.startswith(f'- {name}: ')), '')
        require('disabled' in line and NO_MUTATION_TEXT in line, f'disabled no-mutation wording missing for {name}: {line!r}')
    bold_line = next((line for line in stdout.splitlines() if line.startswith('- bold: ')), '')
    require('direct-backlog' in bold_line and 'selection-required' in bold_line and 'selected-text proof' in bold_line, f'bold status must declare selection-required direct route: {bold_line!r}')


def require_bold_envelope_output() -> None:
    stdout = io.StringIO()
    payload = {
        'summary': 'turned bold on for the proven current selection',
        'selection_summary': 'active selection (mode=0)',
        'where': 'Current active selected text in the live Hancom working copy.',
        'how': 'Selection-required direct-backlog route using documented pyhwpx `hwp.set_font(Bold=True|False)`; `CharShapeBold` toggle is not used.',
        'changed': 'document modified flag no -> yes; selected text length 5',
        'next': 'Run rendered proof.',
        'proof': {
            'selected_text_preview': '증빙텍스트',
            'selected_text_len': 5,
            'document_is_modified_before': False,
            'document_is_modified_after': True,
            'method': 'hwp.set_font',
        },
    }
    with contextlib.redirect_stdout(stdout):
        _print_command_payload(payload)
    output = stdout.getvalue()
    for needle in ('where: Current active selected text', 'how: Selection-required', 'changed: document modified flag no -> yes', 'next: Run rendered proof.', 'proof: selected_text_preview=증빙텍스트'):
        require(needle in output, f'bold envelope output missing {needle!r}:\n{output}')


def require_disabled_bundle_wording() -> None:
    for name in ('section-frame-fill', 'section-graphic-remove-or-hide', 'style-apply', 'style-clone', 'delete', 'erase', 'table-delete', 'cell-clear-contents'):
        help_text = bundle_help(name)
        require(NO_MUTATION_TEXT in help_text, f'bundle help missing no-mutation wording for {name}')

    with contextlib.ExitStack() as stack:
        import tempfile

        tmp_raw = stack.enter_context(tempfile.TemporaryDirectory(prefix='hwpx-disabled-smoke-'))
        body = Path(tmp_raw) / 'body.txt'
        body.write_text('body', encoding='utf-8')
        frame = build_named_bundle(
            'section-frame-fill',
            ['--page-from', '1', '--target-id', 'ctrl/1/gso/no-inst', '--text-file', str(body), '--style-source', 'anchor'],
        ).debug_payload()
        graphic = build_named_bundle(
            'section-graphic-remove-or-hide',
            ['--page-from', '1', '--target-id', 'ctrl/1/gso/no-inst', '--expected-hash', 'sha256:fixture', '--hide-only'],
        ).debug_payload()
        style = build_named_bundle('style-apply', ['--from', 'source', '--to', 'target']).debug_payload()
        style_clone = build_named_bundle('style-clone', ['--from', 'source', '--to', 'target']).debug_payload()
    for name, payload in (('section-frame-fill', frame), ('section-graphic-remove-or-hide', graphic), ('style-apply', style), ('style-clone', style_clone)):
        require(NO_MUTATION_TEXT in str(payload.get('changed')), f'{name} debug payload changed text missing no-mutation wording')
    require(style.get('name') == 'style-apply', f'style-apply debug name mismatch: {style.get("name")!r}')
    require(style_clone.get('name') == 'style-clone', f'style-clone debug name mismatch: {style_clone.get("name")!r}')


def require_bundle_op_drift() -> None:
    step_keys = set(bundles._STEP_KEYS)  # Static smoke: keep bundle op allowlist and schema keys in lockstep.
    server_ops = set(bundles.BUNDLE_SERVER_OPS)
    require(server_ops == step_keys, f'BUNDLE_SERVER_OPS/_STEP_KEYS drift: server_only={sorted(server_ops-step_keys)} schema_only={sorted(step_keys-server_ops)}')


def main() -> int:
    require_cli_help_workflow()
    require_command_status()
    require_bold_envelope_output()
    require_disabled_bundle_wording()
    require_bundle_op_drift()
    print('ok: cli workflow/status static smoke')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
