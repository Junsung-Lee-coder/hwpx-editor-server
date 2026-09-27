from __future__ import annotations

import argparse
import importlib.util
import inspect
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast


_SUPPORTED_PARAMETERS = {"tmp_path"}

# This gate intentionally has an allow-list rather than trusting discovery
# alone.  A moved/renamed function-style test must fail the release instead of
# silently reducing the coverage to zero (or to an unexpected subset).
EXPECTED_TEST_IDS = (
    "test_typography_overview.py:test_parse_hwpml_typography_counts_font_size_and_bold",
    "test_typography_overview.py:test_parse_hwpml_typography_reports_font_size_distribution_per_font",
    "test_typography_overview.py:test_run_step_writes_remote_program_artifact",
    "test_typography_overview.py:test_typography_overview_output_parser_surfaces_font_size_distribution",
    "test_typography_overview.py:test_validate_step_caps_positive_limits",
)
EXPECTED_TEST_COUNT = len(EXPECTED_TEST_IDS)


def _load_module(path: Path) -> ModuleType:
    module_name = f"_function_style_tests_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load test module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _test_functions(module: ModuleType) -> list[tuple[str, Callable[..., object]]]:
    return [
        (name, cast(Callable[..., object], value))
        for name, value in sorted(vars(module).items())
        if name.startswith("test_")
        and inspect.isfunction(value)
        and value.__module__ == module.__name__
    ]


def _run_function(name: str, function: Callable[..., object]) -> None:
    signature = inspect.signature(function)
    unsupported = [
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and parameter.default is inspect.Parameter.empty
        and parameter.name not in _SUPPORTED_PARAMETERS
    ]
    if unsupported:
        raise TypeError(f"unsupported required test parameters: {', '.join(unsupported)}")
    arguments: dict[str, object] = {}
    if "tmp_path" in signature.parameters:
        with tempfile.TemporaryDirectory(prefix="function-style-test-") as temporary_root:
            arguments["tmp_path"] = Path(temporary_root)
            function(**arguments)
        return
    function()


def run(root: Path) -> int:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    failures: list[tuple[str, BaseException]] = []
    discovered = 0
    qualified_names: list[str] = []
    for path in sorted((root / "tests").glob("test_*.py")):
        try:
            module = _load_module(path)
        except BaseException as exc:  # noqa: BLE001 - report every test module failure
            failures.append((f"{path.name}: import", exc))
            continue
        for name, function in _test_functions(module):
            discovered += 1
            qualified_name = f"{path.name}:{name}"
            qualified_names.append(qualified_name)
            try:
                _run_function(qualified_name, function)
            except BaseException as exc:  # noqa: BLE001 - continue to report all failures
                failures.append((qualified_name, exc))

    if discovered == 0:
        failures.append(("function-style manifest", RuntimeError("no function-style tests discovered")))
    elif discovered != EXPECTED_TEST_COUNT or tuple(qualified_names) != EXPECTED_TEST_IDS:
        failures.append((
            "function-style manifest",
            RuntimeError(
                "discovered test IDs do not match the authoritative manifest: "
                f"expected={EXPECTED_TEST_IDS!r}, actual={tuple(qualified_names)!r}"
            ),
        ))

    if failures:
        for name, exc in failures:
            print(f"FAIL {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            traceback.print_exception(exc, file=sys.stderr)
        print(f"function-style tests: {discovered} discovered, {len(failures)} failed", file=sys.stderr)
        return 1
    print(f"function-style tests: {discovered} discovered, all passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run top-level test_* functions without pytest.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    return run(arguments.root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
