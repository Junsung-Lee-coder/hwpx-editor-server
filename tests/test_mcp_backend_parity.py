"""Backend-to-MCP parity regression.

The backend surface is computed from source here (command-package manifests,
the server bundle allowlist, router decorators and the queued-operation
dispatcher), never copied from the MCP side. Every backend editing capability
must be classified by the MCP parity contract; an exposed one must be reachable
through the MCP schema with the backend's own fields, enums and validators.
A new backend op, route, field or queued-operation read fails this test until
MCP covers it or the contract records an explicit reason.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import types
import typing
import unittest
from pathlib import Path

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'app'
_spec = importlib.util.spec_from_file_location('mcp_parity_samples', ROOT / 'mcp_tests' / 'parity_samples.py')
samples = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(samples)

STATUSES = {'exposed', 'equivalent', 'excluded', 'backend_unsupported'}
META_KEYS = {'op', 'operation', 'label'}


def _parity():
    try:
        return importlib.import_module('hwpx_mcp.parity')
    except ModuleNotFoundError as exc:
        if exc.name != 'hwpx_mcp.parity':
            raise
        return None


def _contract(name):
    module = _parity()
    return getattr(module, name, {}) if module is not None else {}


def _literal_set(tree, name):
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            value = node.value
            if isinstance(value, ast.Call):  # frozenset({...})
                value = value.args[0]
            return {elt.value for elt in value.elts}
    raise AssertionError(f'{name} not found')


def backend_bundle_ops():
    tree = ast.parse((APP / 'local_cli_service_support.py').read_text(encoding='utf-8'))
    allowed = _literal_set(tree, '_BUNDLE_ALLOWED_OPS')
    manifests = {}
    for path in sorted((APP / 'command_packages' / 'commands').glob('*/manifest.json')):
        manifest = json.loads(path.read_text(encoding='utf-8'))
        manifests[manifest.get('op') or path.parent.name] = manifest
    return allowed, manifests


def backend_routes():
    routes = {}
    for path, owner in ((APP / 'local_cli_router.py', 'router'), (APP / 'api_server.py', 'app')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == owner
                        and decorator.func.attr in {'get', 'post', 'put', 'delete'}):
                    routes[f'{decorator.func.attr.upper()} {decorator.args[0].value}'] = node
    return routes


def _queued_reads():
    """Fields each queued operation reads from its op dict, through helper calls."""
    tree = ast.parse((APP / 'edit_ops.py').read_text(encoding='utf-8'))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    readers = {'_require_text', '_bool', '_number', '_scope', '_normalize_list_level'}

    def reads(node, seen):
        keys = set()
        for item in ast.walk(node):
            if isinstance(item, ast.Call):
                func = item.func
                if (isinstance(func, ast.Attribute) and func.attr == 'get' and isinstance(func.value, ast.Name)
                        and func.value.id == 'op' and item.args and isinstance(item.args[0], ast.Constant)):
                    keys.add(item.args[0].value)
                if isinstance(func, ast.Name) and func.id in readers and len(item.args) >= 2 and isinstance(item.args[1], ast.Constant):
                    keys.add(item.args[1].value)
                if (isinstance(func, ast.Name) and func.id in functions and func.id not in seen
                        and any(isinstance(arg, ast.Name) and arg.id == 'op' for arg in item.args)):
                    keys |= reads(functions[func.id], seen | {func.id})
            if isinstance(item, ast.Subscript) and isinstance(item.value, ast.Name) and item.value.id == 'op' and isinstance(item.slice, ast.Constant):
                keys.add(item.slice.value)
            if (isinstance(item, ast.Compare) and isinstance(item.left, ast.Constant) and isinstance(item.ops[0], ast.In)
                    and isinstance(item.comparators[0], ast.Name) and item.comparators[0].id == 'op'):
                keys.add(item.left.value)
            if isinstance(item, ast.Assign) and isinstance(item.value, ast.Dict) and any(
                    isinstance(t, ast.Name) and t.id.endswith('_fields') for t in item.targets):
                keys |= {key.value for key in item.value.keys if isinstance(key, ast.Constant)}
        return keys

    dispatch = functions['apply_edit_operations']
    per_op = {}
    for item in ast.walk(dispatch):
        if not isinstance(item, ast.If):
            continue
        test = item.test
        ops = []
        if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == 'op_type':
            comparator = test.comparators[0]
            if isinstance(comparator, ast.Constant):
                ops = [comparator.value]
            elif isinstance(comparator, ast.Set):
                ops = [elt.value for elt in comparator.elts]
        for op in ops:
            body = ast.Module(body=item.body, type_ignores=[])
            per_op[op] = reads(body, set())
    summary = reads(functions['_extract_step_summary_metadata'], set())
    return per_op, summary


def _union_members(annotation):
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return [arg for arg in typing.get_args(annotation)] or [annotation]


def _op_models(model, field='request'):
    if model is None or field not in getattr(model, 'model_fields', {}):
        return {}
    members = _union_members(model.model_fields[field].annotation)
    result = {}
    for member in members:
        op_field = getattr(member, 'model_fields', {}).get('op')
        if op_field is None:
            continue
        for value in typing.get_args(op_field.annotation):
            result[value] = member
    return result


def command_models():
    from hwpx_mcp import schema
    return _op_models(schema.Command)


def bundle_models():
    from hwpx_mcp import schema
    bundle = schema.MODELS.get('hwpx_bundle')
    if bundle is None:
        return {}
    request = bundle.model_fields['request'].annotation
    steps = request.model_fields['steps'].annotation
    item = typing.get_args(_union_members(steps)[0])[0] if typing.get_origin(_union_members(steps)[0]) is list else steps
    while typing.get_origin(item) is typing.Annotated:
        item = typing.get_args(item)[0]
    if typing.get_origin(item) is list:
        item = typing.get_args(item)[0]
    result = {}
    for member in _union_members(item):
        op_field = getattr(member, 'model_fields', {}).get('op')
        for value in typing.get_args(op_field.annotation) if op_field else ():
            result[value] = member
    return result


def _fields(model):
    return set(model.model_fields) - META_KEYS


class _BackendRefusal(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _stub_service():
    from app.local_cli_service import LocalCliService

    class Stub:
        pass

    for name in ('_normalize_anchor_insert_position', '_validate_action_name', '_validate_macro_path',
                 '_validate_macro_args', '_validate_macro_json_value'):
        setattr(Stub, name, getattr(LocalCliService, name))
    return Stub()


class BundleParity(unittest.TestCase):
    def test_every_backend_bundle_op_is_classified(self):
        allowed, manifests = backend_bundle_ops()
        self.assertEqual(allowed, set(manifests), 'server allowlist and command packages disagree')
        contract = _contract('BUNDLE_OPS')
        missing = sorted(set(allowed) - set(contract))
        self.assertEqual(missing, [], f'backend bundle ops without an MCP parity classification: {missing}')
        stale = sorted(set(contract) - set(allowed))
        self.assertEqual(stale, [], f'MCP parity contract names ops the backend no longer has: {stale}')
        for op, row in contract.items():
            self.assertIn(row.get('status'), STATUSES, op)
            if row['status'] != 'exposed':
                self.assertTrue(row.get('reason'), f'{op} needs a concrete reason')

    def test_exposed_bundle_ops_are_reachable_through_command_and_bundle(self):
        expected = set(samples.BUNDLE_SAMPLES)
        commands, bundles = command_models(), bundle_models()
        self.assertEqual(sorted(expected - set(commands)), [], 'not reachable through hwpx_command')
        self.assertEqual(sorted(expected - set(bundles)), [], 'not reachable through hwpx_bundle')
        contract = _contract('BUNDLE_OPS')
        not_marked = sorted(op for op in expected if contract.get(op, {}).get('status') != 'exposed')
        self.assertEqual(not_marked, [], f'exposed ops not classified exposed: {not_marked}')
        exposed = sorted(op for op, row in contract.items() if row.get('status') == 'exposed')
        self.assertEqual(exposed, sorted(expected), 'exposed classification must match the tested sample set')

    def test_exposed_bundle_op_fields_match_backend_manifest(self):
        _allowed, manifests = backend_bundle_ops()
        commands = command_models()
        excluded = _contract('EXCLUDED_FIELDS')
        problems = []
        for op in sorted(samples.BUNDLE_SAMPLES):
            model = commands.get(op)
            if model is None:
                problems.append(f'{op}: no MCP model')
                continue
            backend = set(manifests[op]['allowed_keys']) - META_KEYS
            mcp = _fields(model)
            dropped = backend - mcp - set(excluded.get(op, {}))
            invented = mcp - backend
            if dropped or invented:
                problems.append(f'{op}: backend-only fields {sorted(dropped)}, MCP-only fields {sorted(invented)}')
        self.assertEqual(problems, [])

    def test_backend_validators_accept_every_mcp_sample_unchanged(self):
        from app.command_packages.runtime import CommandPackageRegistry
        registry = CommandPackageRegistry()
        service = _stub_service()
        commands = command_models()
        problems = []
        for op, variants in sorted(samples.BUNDLE_SAMPLES.items()):
            model = commands.get(op)
            for sample in variants:
                if model is None:
                    problems.append(f'{op}: no MCP model')
                    break
                try:
                    request = model.model_validate({'op': op, **sample}).model_dump(exclude_none=True)
                except ValidationError as exc:
                    problems.append(f'{op}: MCP rejected a valid backend request: {exc.errors()[:2]}')
                    continue
                fields = {key: value for key, value in request.items() if key not in META_KEYS}
                step = {'op': op, 'label': request.get('label', 'mcp'), **json.loads(json.dumps(fields))}
                try:
                    validated = registry.get(op).validate(service=service, index=1, step=dict(step), error_type=_BackendRefusal)
                except _BackendRefusal as exc:
                    problems.append(f'{op}: backend validator refused the MCP payload: {exc}')
                    continue
                for key, value in fields.items():
                    if validated.get(key) != json.loads(json.dumps(value)):
                        problems.append(f'{op}.{key}: backend normalized {value!r} to {validated.get(key)!r}')
        self.assertEqual(problems, [])

    def test_mcp_refuses_what_the_backend_allowlists_refuse(self):
        commands = command_models()
        accepted = []
        for op, variants in sorted(samples.BUNDLE_REFUSALS.items()):
            model = commands.get(op)
            if model is None:
                accepted.append(f'{op}: no MCP model to refuse with')
                continue
            for sample in variants:
                try:
                    model.model_validate({'op': op, **sample})
                except ValidationError:
                    continue
                accepted.append(f'{op}: {sample}')
        self.assertEqual(accepted, [])

    def test_cli_client_accepts_every_exposed_bundle_op(self):
        from local_cli_v1.bundles import BundleError, CommandStep
        rejected = []
        for op, variants in sorted(samples.BUNDLE_SAMPLES.items()):
            for sample in variants:
                try:
                    CommandStep(op=op, label='parity', fields=dict(sample))
                except BundleError as exc:
                    rejected.append(f'{op}: {exc}')
        self.assertEqual(rejected, [])

    def test_enums_follow_backend_constants(self):
        from app import layout_ops, object_insert, table_structure
        from app.command_packages import common
        from app.command_packages.commands.readback import run as readback_run
        commands = command_models()

        def literal(op, field):
            model = commands.get(op)
            if model is None or field not in model.model_fields:
                return None
            values, stack = set(), [model.model_fields[field].annotation]
            while stack:
                item = stack.pop()
                origin = typing.get_origin(item)
                if origin is typing.Literal:
                    values |= set(typing.get_args(item))
                elif origin in (typing.Union, types.UnionType, typing.Annotated):
                    stack.extend(typing.get_args(item))
            return values

        checks = {
            ('layout_exact', 'kind'): set(layout_ops.KINDS),
            ('layout_exact', 'apply_to'): set(layout_ops.PAGE_APPLY_TO) | set(layout_ops.COLUMN_APPLY_TO),
            ('object_insert_exact', 'kind'): set(object_insert.KINDS),
            ('object_insert_exact', 'apply_to'): set(object_insert.APPLY_PAGE_TYPES),
            ('table_structure_exact', 'action'): set(table_structure.ACTIONS),
            ('hwp_action', 'action_name'): set(common.BUNDLE_SAFE_HACTION_NAMES),
            ('pyhwpx_call', 'method_path'): set(common.BUNDLE_SAFE_PYHWPX_CALLS),
            ('readback', 'scope'): set(readback_run._SCOPES),
            ('anchor_insert', 'position'): {'before-anchor', 'after-anchor', 'after-paragraph', 'before-heading'},
        }
        mismatches = {f'{op}.{field}': (sorted(literal(op, field) or []), sorted(expected))
                      for (op, field), expected in checks.items() if literal(op, field) != expected}
        self.assertEqual(mismatches, {})
        page_model = commands.get('layout_exact')
        self.assertIsNotNone(page_model)
        self.assertTrue(set(layout_ops.PAGE_FIELDS) <= set(page_model.model_fields))


class RouteParity(unittest.TestCase):
    def test_every_backend_route_is_classified(self):
        contract = _contract('REST_ROUTES')
        routes = backend_routes()
        missing = sorted(set(routes) - set(contract))
        self.assertEqual(missing, [], f'backend routes without an MCP parity classification: {missing}')
        stale = sorted(set(contract) - set(routes))
        self.assertEqual(stale, [])
        for route, row in contract.items():
            self.assertIn(row.get('status'), STATUSES, route)
            self.assertTrue(row.get('mcp') or row.get('reason'), route)

    def test_exposed_route_parameters_are_mapped_or_explicitly_excluded(self):
        contract = _contract('REST_ROUTES')
        routes = backend_routes()
        problems = []
        for route, node in sorted(routes.items()):
            row = contract.get(route, {})
            if row.get('status') != 'exposed':
                continue
            params = set()
            for arg in node.args.args:
                annotation = ast.unparse(arg.annotation) if arg.annotation else ''
                if annotation.endswith('Request') and annotation[0].isupper():
                    module = importlib.import_module('app.local_cli_router' if route.split()[1].startswith('/local-cli') else 'app.models')
                    model = getattr(module, annotation, None) or getattr(importlib.import_module('app.models'), annotation)
                    params |= set(model.model_fields)
                else:
                    params.add(arg.arg)
            path_params = {part[1:-1] for part in route.split()[1].split('/') if part.startswith('{')}
            mapped = set(row.get('fields', {})) | set(row.get('excluded_fields', {})) | path_params
            unmapped = sorted(params - mapped)
            if unmapped:
                problems.append(f'{route}: backend parameters with no MCP mapping {unmapped}')
            for reason in row.get('excluded_fields', {}).values():
                if not reason:
                    problems.append(f'{route}: excluded field without reason')
        self.assertEqual(problems, [])

    def test_direct_session_routes_are_exposed_as_typed_command_ops(self):
        commands = command_models()
        contract = _contract('REST_ROUTES')
        problems = []
        for op, (sample, path) in sorted(samples.REST_SAMPLES.items()):
            model = commands.get(op)
            if model is None:
                problems.append(f'{op}: not reachable through hwpx_command')
                continue
            try:
                model.model_validate({'op': op, **sample})
            except ValidationError as exc:
                problems.append(f'{op}: {exc.errors()[:2]}')
            row = contract.get(f'POST {path}', {})
            if row.get('status') != 'exposed' or row.get('mcp') != f'hwpx_command:{op}':
                problems.append(f'{op}: route {path} not classified as hwpx_command:{op}')
        self.assertEqual(problems, [])

    def test_unbounded_escape_hatches_stay_unreachable(self):
        contract = _contract('REST_ROUTES')
        for route in ('POST /local-cli/action', 'POST /local-cli/pycall'):
            self.assertEqual(contract.get(route, {}).get('status'), 'excluded', route)
        self.assertEqual(contract.get('POST /local-cli/replace', {}).get('status'), 'backend_unsupported')
        commands = command_models()
        self.assertFalse({'action', 'pycall', 'replace', 'native_action'} & set(commands))


class QueuedParity(unittest.TestCase):
    def _job_operation_models(self):
        from hwpx_mcp import schema
        job = schema.MODELS.get('hwpx_job')
        if job is None:
            return {}
        result = {}
        for member in _union_members(job.model_fields['request'].annotation):
            if 'instructions' in getattr(member, 'model_fields', {}):
                instructions = member.model_fields['instructions'].annotation
                for candidate in _union_members(instructions):
                    if 'operations' in getattr(candidate, 'model_fields', {}):
                        item = typing.get_args(_union_members(candidate.model_fields['operations'].annotation)[0])
                        item = item[0] if item else None
                        while typing.get_origin(item) is typing.Annotated:
                            item = typing.get_args(item)[0]
                        for op_model in _union_members(item):
                            op_field = getattr(op_model, 'model_fields', {}).get('op')
                            for value in typing.get_args(op_field.annotation) if op_field else ():
                                result[value] = op_model
        return result

    def test_every_queued_op_is_classified(self):
        tree = ast.parse((APP / 'edit_ops.py').read_text(encoding='utf-8'))
        supported = _literal_set(tree, 'SUPPORTED_OPS')
        contract = _contract('QUEUED_OPS')
        self.assertEqual(sorted(supported - set(contract)), [], 'queued ops without classification')
        self.assertEqual(sorted(set(contract) - supported), [])
        exposed = {op for op, row in contract.items() if row.get('status') == 'exposed'}
        self.assertEqual(exposed, set(samples.QUEUED_SAMPLES))
        for op in samples.QUEUED_REFUSALS:
            self.assertIn(contract.get(op, {}).get('status'), {'excluded', 'backend_unsupported'}, op)

    def test_queued_models_cover_every_field_the_runtime_reads(self):
        reads, summary = _queued_reads()
        models = self._job_operation_models()
        excluded = _contract('QUEUED_EXCLUDED_FIELDS')
        problems = []
        for op in sorted(samples.QUEUED_SAMPLES):
            model = models.get(op)
            if model is None:
                problems.append(f'{op}: not reachable through hwpx_job')
                continue
            backend = (reads.get(op, set()) | summary | {'allow_zero_match'}) - {'op'}
            mcp = set(model.model_fields) - {'op'}
            dropped = backend - mcp - set(excluded.get(op, {}))
            invented = mcp - backend
            if dropped or invented:
                problems.append(f'{op}: runtime-read fields missing {sorted(dropped)}, MCP-only {sorted(invented)}')
            try:
                model.model_validate({'op': op, **samples.QUEUED_SAMPLES[op]})
            except ValidationError as exc:
                problems.append(f'{op}: sample refused {exc.errors()[:2]}')
        self.assertEqual(problems, [])
        self.assertFalse(set(samples.QUEUED_REFUSALS) & set(models), 'escape hatch or queued-refused op reachable')


if __name__ == '__main__':
    unittest.main()
