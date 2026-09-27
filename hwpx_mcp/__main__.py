"""Explicit opt-in entry point. Configuration comes from the environment."""
import sys
import uvicorn
from hwpx_mcp.server import Settings, build_app


def main():
    try:
        settings = Settings.from_env()
    except (ValueError, OSError):
        print('Invalid MCP configuration. Check the dedicated token, absolute input/output roots, port and backend origin.', file=sys.stderr)
        return 2
    uvicorn.run(build_app(settings), host='127.0.0.1', port=settings.port,
                access_log=False, log_level='warning', timeout_graceful_shutdown=10)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
