"""Real MCP process -> real PlaneClient -> isolated HTTP fake, never Plane live."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import sys

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from test_plane_client_failure_receipts import ISSUE


def unpack(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_stdio_create_state_duplicate_delete(tmp_path):
    calls = []
    current = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, value):
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if status != 204:
                self.wfile.write(json.dumps(value).encode())

        def dispatch(self):
            calls.append((self.command, self.path))
            assert self.headers.get('X-Api-Key') == 'synthetic-test-token'
            size = int(self.headers.get('Content-Length', '0'))
            payload = json.loads(self.rfile.read(size)) if size else {}
            if self.command == 'POST':
                current.update({**ISSUE, **payload})
                self.respond(201, current)
            elif self.command == 'PATCH':
                current.update(payload)
                self.respond(200, current)
            elif self.command == 'DELETE':
                current.clear()
                self.respond(204, None)
            else:
                self.respond(200 if current else 404, current)

        do_POST = dispatch
        do_PATCH = dispatch
        do_DELETE = dispatch
        do_GET = dispatch

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=http.serve_forever, daemon=True)
    worker.start()

    async def exercise():
        transport = StdioTransport(
            command=sys.executable,
            args=['-I', '-c', 'from plane_mcp_karval.cli import main; main()', 'stdio'],
            env={'PLANE_BASE_URL': f'http://127.0.0.1:{http.server_port}',
                 'PLANE_API_KEY': 'synthetic-test-token',
                 'PLANE_MUTATION_LEDGER': str(tmp_path / 'ledger.json')},
        )
        async with Client(transport) as client:
            base = {'workspace_slug': 'workspace', 'project_id': 'project'}
            create = {'operation': 'issue__add_issue', 'path_params': base,
                      'payload': {'name': ISSUE['name']}, 'idempotency_key': 'stdio-create-00001'}
            result = unpack(await client.call_tool('plane_mutation_action', create))
            assert result['mutation_applied'] and result['readback_verified']
            target = {**base, 'resource_id': ISSUE['id']}
            update = {'operation': 'issue__update_issue_detail', 'path_params': target,
                      'payload': {'state': 'done'}, 'idempotency_key': 'stdio-update-00001'}
            result = unpack(await client.call_tool('plane_mutation_action', update))
            assert result['readback_verified']
            result = unpack(await client.call_tool('plane_mutation_action', update))
            assert result['duplicate'] and result['readback_verified']
            result = unpack(await client.call_tool('plane_mutation_action', {
                'operation': 'issue__delete_issue', 'path_params': target,
                'payload': None, 'idempotency_key': 'stdio-delete-00001'}))
            assert result['mutation_applied'] and result['readback_verified']

    try:
        asyncio.run(exercise())
    finally:
        http.shutdown()
        http.server_close()
        worker.join(timeout=5)
    assert [method for method, _ in calls] == ['POST', 'GET', 'PATCH', 'GET', 'GET', 'DELETE', 'GET']
    assert not current
