from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Callable


class DependencyCheckError(RuntimeError):
    """Raised when a Windows dependency lock cannot be validated."""


# These are the import surfaces used by the packaged application or its
# publication gate. The distribution-version check below covers every locked
# distribution, including packages without a Python import surface such as
# tzdata.
_DISTRIBUTION_IMPORTS = {
    "annotated-doc": "annotated_doc",
    "annotated-types": "annotated_types",
    "anyio": "anyio",
    "certifi": "certifi",
    "charset-normalizer": "charset_normalizer",
    "click": "click",
    "colorama": "colorama",
    "et-xmlfile": "et_xmlfile",
    "fastapi": "fastapi",
    "h11": "h11",
    "httptools": "httptools",
    "idna": "idna",
    "numpy": "numpy",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "pillow": "PIL",
    "pydantic": "pydantic",
    "pydantic-core": "pydantic_core",
    "pydantic-settings": "pydantic_settings",
    "pyhwpx": "pyhwpx",
    "pyperclip": "pyperclip",
    "python-dateutil": "dateutil",
    "python-dotenv": "dotenv",
    "python-multipart": "multipart",
    "pywin32": (
        "pythoncom",
        "win32com",
        "win32gui",
        "win32ui",
        "win32process",
        "win32con",
    ),
    "pyyaml": "yaml",
    "requests": "requests",
    "six": "six",
    "starlette": "starlette",
    "typing-extensions": "typing_extensions",
    "typing-inspection": "typing_inspection",
    "urllib3": "urllib3",
    "uvicorn": "uvicorn",
    "watchfiles": "watchfiles",
    "websockets": "websockets",
}
_DISCOVERY_ONLY_IMPORTS = frozenset({"pyhwpx"})
_LOCKED_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)==([^\s\\#]+)"
)


def normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_locked_requirements(lock_path: Path) -> dict[str, str]:
    path = Path(lock_path).expanduser().resolve()
    if not path.is_file():
        raise DependencyCheckError(f"dependency lock is missing: {path}")
    packages: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("--"):
            continue
        match = _LOCKED_REQUIREMENT.match(line)
        if match is None:
            continue
        name = normalize_distribution_name(match.group(1))
        version = match.group(2)
        previous = packages.get(name)
        if previous is not None and previous != version:
            raise DependencyCheckError(
                f"dependency lock declares conflicting versions for {name} at line {line_number}"
            )
        packages[name] = version
    if not packages:
        raise DependencyCheckError(f"dependency lock contains no pinned distributions: {path}")
    return dict(sorted(packages.items()))


def verify_dependencies(
    lock_path: Path,
    *,
    version_lookup: Callable[[str], str] = importlib.metadata.version,
    importer: Callable[[str], object] = importlib.import_module,
    module_finder: Callable[[str], object | None] = importlib.util.find_spec,
) -> dict[str, object]:
    path = Path(lock_path).expanduser().resolve()
    locked = parse_locked_requirements(path)
    missing_distributions: list[str] = []
    version_mismatches: list[dict[str, str]] = []
    for name, expected in locked.items():
        try:
            installed = str(version_lookup(name))
        except importlib.metadata.PackageNotFoundError:
            missing_distributions.append(name)
            continue
        if installed != expected:
            version_mismatches.append(
                {"name": name, "expected": expected, "installed": installed}
            )

    import_failures: list[str] = []
    import_errors: dict[str, str] = {}
    checked_imports = sorted(
        {
            module
            for name, module in _DISTRIBUTION_IMPORTS.items()
            if name in locked
            for module in (module if isinstance(module, (tuple, list)) else (module,))
        }
    )
    for module in checked_imports:
        try:
            if module in _DISCOVERY_ONLY_IMPORTS:
                if module_finder(module) is None:
                    raise ModuleNotFoundError(module)
            else:
                importer(module)
        except Exception as exc:  # pragma: no cover - exact exceptions vary by platform
            import_failures.append(module)
            import_errors[module] = type(exc).__name__

    return {
        "schema_version": "hwpx/windows-dependency-check/v1",
        "ok": not (missing_distributions or version_mismatches or import_failures),
        "lock_path": str(path),
        "locked_distribution_count": len(locked),
        "checked_import_count": len(checked_imports),
        "discovery_only_imports": sorted(_DISCOVERY_ONLY_IMPORTS.intersection(checked_imports)),
        "missing_distributions": missing_distributions,
        "version_mismatches": version_mismatches,
        "import_failures": import_failures,
        "import_errors": import_errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the final Windows venv against its hash-pinned lock"
    )
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--json", action="store_true", help="kept for explicit caller readability")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = verify_dependencies(args.lock)
    except (DependencyCheckError, OSError) as exc:
        report = {
            "schema_version": "hwpx/windows-dependency-check/v1",
            "ok": False,
            "lock_path": str(Path(args.lock).expanduser().resolve()),
            "locked_distribution_count": 0,
            "checked_import_count": 0,
            "missing_distributions": [],
            "version_mismatches": [],
            "import_failures": [],
            "import_errors": {},
            "error": str(exc),
        }
        print(json.dumps(report, ensure_ascii=True, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
