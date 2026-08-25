from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping


class CommandPackageError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandPackage:
    op: str
    root: Path
    manifest: dict[str, Any]
    module: ModuleType

    @property
    def allowed_keys(self) -> set[str]:
        keys = self.manifest.get('allowed_keys')
        if not isinstance(keys, list) or not all(isinstance(item, str) for item in keys):
            raise CommandPackageError(f'command package {self.op!r} manifest allowed_keys must be a string array')
        return set(keys)

    @property
    def read_only(self) -> bool:
        return bool(self.manifest.get('read_only', False))

    @property
    def version(self) -> str:
        return str(self.manifest.get('version') or 'v0')

    @property
    def dirty_default(self) -> bool:
        return bool(self.manifest.get('dirty_default', not self.read_only))

    def validate(self, *, service: Any, index: int, step: dict[str, Any], error_type: type[Exception]) -> dict[str, Any]:
        validate_step: Callable[..., dict[str, Any]] | None = getattr(self.module, 'validate_step', None)
        if not callable(validate_step):
            return step
        return validate_step(service=service, index=index, step=step, manifest=self.manifest, error_type=error_type)

    def run(self, *, service: Any, handle: Any, step: dict[str, Any], binding: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool, list[str]]:
        run_step: Callable[..., tuple[dict[str, Any], bool, list[str]]] | None = getattr(self.module, 'run_step', None)
        if not callable(run_step):
            raise CommandPackageError(f'command package {self.op!r} has no run_step()')
        result, dirty, warnings = run_step(service=service, handle=handle, step=step, binding=binding, manifest=self.manifest)
        if not isinstance(result, dict):
            raise CommandPackageError(f'command package {self.op!r} returned non-object result')
        if not isinstance(warnings, list):
            warnings = [str(warnings)]
        return result, bool(dirty), [str(item) for item in warnings]


class CommandPackageRegistry:
    def __init__(self, *, root: Path | None = None):
        self.root = root or Path(__file__).with_name('commands')
        self._packages: dict[str, CommandPackage] | None = None

    def _load(self) -> dict[str, CommandPackage]:
        packages: dict[str, CommandPackage] = {}
        if not self.root.exists():
            return packages
        for manifest_path in sorted(self.root.glob('*/manifest.json')):
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            op = str(manifest.get('op') or manifest_path.parent.name).strip()
            if not op:
                raise CommandPackageError(f'command package manifest missing op: {manifest_path}')
            module_name = str(manifest.get('module') or f'app.command_packages.commands.{manifest_path.parent.name}.run')
            module = importlib.import_module(module_name)
            package = CommandPackage(op=op, root=manifest_path.parent, manifest=manifest, module=module)
            # Validate shape during load so server startup fails early, not mid-edit.
            _ = package.allowed_keys
            if op in packages:
                raise CommandPackageError(f'duplicate command package op: {op}')
            packages[op] = package
        return packages

    @property
    def packages(self) -> dict[str, CommandPackage]:
        if self._packages is None:
            self._packages = self._load()
        return self._packages

    def get(self, op: str) -> CommandPackage | None:
        return self.packages.get(op)

    def ops(self) -> set[str]:
        return set(self.packages)

    def allowed_keys(self, op: str) -> set[str] | None:
        package = self.get(op)
        if package is None:
            return None
        return package.allowed_keys

    def revision(self) -> str:
        parts: list[str] = []
        for op, package in sorted(self.packages.items()):
            for path in (package.root / 'manifest.json', package.root / 'run.py'):
                try:
                    parts.append(f'{op}/{path.name}:{path.stat().st_mtime_ns}')
                except FileNotFoundError:
                    parts.append(f'{op}/{path.name}:missing')
        return '|'.join(parts)


_REGISTRY = CommandPackageRegistry()


def get_command_package_registry() -> CommandPackageRegistry:
    return _REGISTRY
