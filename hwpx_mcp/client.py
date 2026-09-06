"""Small official-SDK client for localhost setup and explicit tool calls."""
import argparse
import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from hwpx_mcp.server import PROTOCOL_VERSION


async def run(url, tool=None, arguments=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost'} or parsed.path != '/mcp' or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('This setup client accepts only an explicit localhost http://.../mcp URL.')
    token = os.environ['HWPX_MCP_TOKEN']
    async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + token}, trust_env=False) as http:
        async with Client(streamable_http_client(url, http_client=http), mode=PROTOCOL_VERSION) as client:
            result = await client.call_tool(tool, arguments or {}) if tool else await client.list_tools()
            print(result.model_dump_json(by_alias=True, indent=2))
            return int(bool(getattr(result, 'is_error', False)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18766/mcp')
    parser.add_argument('--tool')
    parser.add_argument('--arguments', default='{}', help='JSON object; source paths refer to the adapter host')
    args = parser.parse_args()
    return asyncio.run(run(args.url, args.tool, json.loads(args.arguments)))


if __name__ == '__main__':
    raise SystemExit(main())
