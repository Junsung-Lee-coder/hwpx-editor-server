# Public open failure diagnostics

This is bounded failure reporting, not proof of a native open root cause.
A sanitized HTTP 500 can leave its inner SDK site and acquisition unknown.
Readiness, process absence, a null session ID and `session_held=false` do not
resolve that uncertainty.

Unexpected `/local-cli/open` errors now preserve a first fixed capsule:

    {"detail":{"message":"Local CLI open failed.","open_failure":{
      "code":"open_failed","stage":"session_layout",
      "exception_class":"permission_error"}}}

The HTTP status is unchanged. The real MCP error stays `BACKEND_REJECTED`, with
`error.details.reason="open_failed"` and `error.details.open_failure` carrying
the same strict capsule. Unknown labels/extra diagnostic fields are not forwarded.
No exception string, arbitrary class name, args/HRESULT, path, private frame,
traceback, token, document contents or new SDK getter is used to build it.

Stages describe the PRODUCT operation boundary, not a recovered inner SDK site.
`session_layout`, `upload_write`, `working_copy`, `interactive_record`,
`runtime_open` and `binding_commit` distinguish service steps. The runtime adds
`runtime_session_create`, `runtime_initialize`, `runtime_sdk_import`,
`runtime_preflight`, `runtime_process_baseline`, `runtime_constructor`,
`runtime_owner_binding`, `runtime_post_constructor`, `runtime_watchdog`,
`runtime_document_open`, `runtime_ready_report`, `runtime_location` and
`runtime_start_result`. Other unexpected pre-layout/dispatch failures receive
`open_dispatch`. Constructor/open helpers contain more than one operation;
these labels do not pretend to identify a particular COM invocation.

Exception classes are fixed groups: permission_error, file_not_found, os_error,
timeout_error, type_error, value_error, attribute_error, import_error,
runtime_error or other. Native/vendor-specific class names are intentionally not
public. The capsule follows the original runtime Future/wrapped exception; a
later captured wrapper/reporting context cannot replace the first capsule.
It is diagnostic information only, not mutation/effect/ownership/admission proof.

Expected input/admission errors and startup-timeout reconciliation contracts
remain unchanged. No constructor default, fallback, retry, cache/security setting,
path limit, positional ABI gate, SDK interface, close/save or custody decision
was changed. An entered UNKNOWN remains held with no SDK follow-on. Container
target, geometry and preservation contracts and qualification limits stand.

On an open failure, the first-call recorder retains the capsule before stopping.
Do not retry or treat a diagnostic label as authority for extra SDK queries or
close. A successful open alone is not a persistence or rendering test. The
bounded tested textbox workflow and its unqualified variants are described in
[NATIVE_TEXT_CONTAINERS.md](NATIVE_TEXT_CONTAINERS.md); diagnostic reporting by
itself does not establish any of those outcomes.
