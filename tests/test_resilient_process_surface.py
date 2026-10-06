"""Single-flight proof with separate OS processes and a loopback fixture."""
import json
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from test_plane_client_failure_receipts import ISSUE


@pytest.mark.parametrize("crash", [False, True])
def test_separate_processes_share_one_mutation_lease(tmp_path, crash):
    entered, release = Event(), Event()
    writes, rows = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # The crash case intentionally kills its client after the write.
                pass

        def do_POST(self):
            writes.append(self.path)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            rows.append({**ISSUE, **payload})
            entered.set()
            if not release.wait(15):
                self.respond(503, {"error": "fixture timeout"})
                return
            self.respond(201, rows[0])

        def do_GET(self):
            if self.path.rstrip("/").endswith("work-items"):
                self.respond(200, {"results": rows, "next_page_results": False, "next_cursor": "",
                                   "total_results": len(rows), "total_count": len(rows),
                                   "count": len(rows), "total_pages": 1})
            else:
                self.respond(200, rows[0])

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    script = """
import json,sys
from plane_api.client import PlaneClient
client=PlaneClient({'base_url':sys.argv[1]})
receipt=client.execute_native_mutation_action('issue__add_issue',
 path_params={'workspace_slug':'workspace','project_id':'project'},
 payload={'name':'INTERNAL-REFERENCE fixture'},
 idempotency_key='process-single-flight-key',ledger_path=sys.argv[2])
print(json.dumps(receipt))
"""
    command = [sys.executable, "-c", script, f"http://127.0.0.1:{fixture.server_port}", str(tmp_path / "ledger.json")]
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(root / "src")}
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=root)
    try:
        assert entered.wait(10), "first process never reached loopback POST"
        if crash:
            first.terminate()
            first.wait(timeout=5)
            # Only the dead process's test ledger is edited to advance lease expiry.
            ledger_path = tmp_path / "ledger.json"
            ledger = json.loads(ledger_path.read_text())
            receipt = next(iter(ledger["receipts"].values()))
            assert receipt["status"] == "sending"
            receipt["lease_until"] = 0
            ledger_path.write_text(json.dumps(ledger))
            release.set()
        second = subprocess.run(command, capture_output=True, text=True, env=env, cwd=root, timeout=10)
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout)["status"] == ("verified" if crash else "in_progress")
        assert len(writes) == 1
        release.set()
        stdout, stderr = first.communicate(timeout=10)
        if not crash:
            assert first.returncode == 0, stderr
            assert json.loads(stdout)["status"] == "verified"
        third = subprocess.run(command, capture_output=True, text=True, env=env, cwd=root, timeout=10)
        assert third.returncode == 0, third.stderr
        assert json.loads(third.stdout)["duplicate"] is True
        assert len(writes) == 1
        assert len(rows) == 1
        if crash:
            receipt = next(iter(json.loads((tmp_path / "ledger.json").read_text())["receipts"].values()))
            uncertain = receipt["uncertain_write_attempts"]
            assert len(uncertain) == 1
            assert uncertain[0]["send_attempt_id"] == receipt["durable_send_attempt"]["send_attempt_id"]
            assert uncertain[0]["observation"] == "expired_sending_lease"
            assert uncertain[0]["phase"] == "write:issue__add_issue"
    finally:
        release.set()
        if first.poll() is None:
            first.terminate()
            first.wait(timeout=5)
        fixture.shutdown()
        fixture.server_close()
        thread.join(timeout=5)
