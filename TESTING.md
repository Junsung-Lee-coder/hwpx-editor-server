# Testing

The runtime has two distinct test surfaces:

1. Local checks that do not start Hancom.
2. Windows checks that exercise the native desktop runtime.

## Local syntax check

```bash
python -m compileall -q app local_cli_v1 scripts tests
```

## Unit tests

After installing the dependencies in a Windows virtual environment:

```bash
python -m unittest discover -s tests -v
```

These tests cover local planners, parsers, readback helpers, command-package contracts, and service helpers. Tests that import the live runtime still require the matching dependency set.

## Static smoke checks

The following scripts validate local CLI and command-bundle contracts without opening a document:

```bash
python scripts/smoke_cli_workflow_status_static.py
python scripts/smoke_local_cli_health_static.py
python scripts/smoke_output_parser_static.py
python scripts/smoke_command_bundle_static.py
python scripts/smoke_command_packages_static.py
python scripts/smoke_export_proof_range_clamp_static.py
python scripts/smoke_find_context_static.py
python scripts/smoke_readback_diff_static.py
python scripts/smoke_readback_schema_static.py
python scripts/smoke_selection_proof_static.py
python scripts/smoke_text_table_cleanup_static.py
```

## Publication gate

For this source-only snapshot, the deterministic publication gate is the syntax check, all eleven static smoke checks above, and these dependency-light unit modules:

```bash
python -m unittest \
  tests.test_local_cli_status_payload \
  tests.test_raw_target_readback \
  tests.test_readback_command_status \
  tests.test_readback_diff \
  tests.test_table_scoped_cell_and_list_primitives \
  tests.test_table4_anchor_range_replace_bundle
```

The repository also retains broader tests for native and in-progress CLI features. Full discovery can include tests for commands that are not present in this source frontier; those tests are kept as implementation backlog and are not silently removed from the tree.

## Native checks

Native checks must run on a Windows desktop session with Hancom available. Use a disposable or managed copy of a document, capture rendered proof, and keep all outputs outside the repository. Do not run these checks against customer or submission documents.

The server is local by default. Verify health and readiness before relying on an edit command:

```text
GET /health
GET /runtime-readiness
GET /capabilities
```

Do not add the resulting documents, logs, screenshots, or runtime state to Git.
