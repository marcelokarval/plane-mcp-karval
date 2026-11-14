import asyncio
import hashlib
import json
import os
import subprocess
import sys

import pytest
from fastmcp import Client

from plane_mcp_karval import server


def bind_test_call(monkeypatch, tmp_path, operation, args):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".prepared-mutations"
    root.mkdir(exist_ok=True)
    fields = {
        "plane_operator_lifecycle_transition": ("comment", ("workspace_slug", "project_id", "work_item_id", "expected_current_state_id", "expected_updated_at", "target_state_id")),
        "plane_lifecycle_transition": ("comment", ("workspace_slug", "project_id", "work_item_id", "target_state_id", "expected_state_id", "expected_updated_at")),
        "plane_add_comment": ("comment_html", ("workspace_slug", "project_id", "work_item_id", "external_source", "external_id")),
        "plane_add_lifecycle_comment": ("comment", ("workspace_slug", "project_id", "work_item_id", "external_source", "external_id")),
    }
    args = dict(args)
    if operation == "plane_mutation_action":
        prepared = {k: args.get(k) for k in ("operation", "path_params", "payload", "idempotency_key", "query")}
    else:
        field, keys = fields[operation]
        target = {k: args.get(k, "plane-mcp-karval" if k == "external_source" else None) for k in keys}
        prepared = {"operation": operation, "path_params": target, "query": None, "payload": {field: args[field]}, "idempotency_key": args["idempotency_key"]}
    data = json.dumps(prepared, ensure_ascii=False).encode()
    (root / "test.json").write_bytes(data)
    args.update(prepared_file="test.json", prepared_sha256=hashlib.sha256(data).hexdigest())
    return args


@pytest.mark.parametrize("payload", [
    {"description_html": "<table>" + "".join(f"<tr><td>Imóvel {i}</td></tr>" for i in range(11)) + "</table>"},
    {"description_html": ""}, {"comment_html": None},
    {"nested": [{"comment_html": 42}]},
])
def test_any_inline_html_field_requires_binding_before_client(monkeypatch, payload):
    calls = []
    monkeypatch.setattr(server, "_client", lambda: calls.append(True))
    async def run():
        async with Client(server.create_server()) as client:
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {"operation": "issue__update_issue_detail", "path_params": {}, "payload": payload, "idempotency_key": "synthetic-key-123456"})
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("tool", ["plane_lifecycle_transition", "plane_operator_lifecycle_transition"])
def test_lifecycle_renders_once_before_client_and_reuses_html(tmp_path, monkeypatch, tool):
    from test_operator_lifecycle_v3 import OperatorClient, operator_call, lifecycle_comment
    if tool == "plane_operator_lifecycle_transition":
        args = operator_call()
    else:
        args = {"workspace_slug": "karval", "project_id": "project-1", "work_item_id": "issue-1", "target_state_id": "in-progress-state", "idempotency_key": "synthetic-key-123456", "comment": lifecycle_comment()}
    args = bind_test_call(monkeypatch, tmp_path, tool, args)
    fake = OperatorClient()
    events = []
    real_render = server.render_comment
    def render(*a, **kw):
        events.append("render")
        return real_render(*a, **kw)
    def client():
        events.append("client")
        return fake
    monkeypatch.setattr(server, "render_comment", render)
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_ledger_path", lambda: tmp_path / "ledger")
    async def run():
        async with Client(server.create_server()) as c:
            result = await c.call_tool(tool, args)
            assert result.data["comment"]["readback_verified"] is True
    asyncio.run(run())
    assert events[0] == "render"
    assert events.count("render") == 1
    comment_call = next(value for name, value in fake.calls if name == "issue_comment__add_issue_comment")
    assert "Requested" in comment_call["payload"]["comment_html"]


def test_long_inline_html_is_rejected_before_client(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_client", lambda: calls.append(True))

    async def run():
        async with Client(server.create_server()) as client:
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {
                    "operation": "issue__update_issue_detail",
                    "path_params": {"workspace_slug": "test", "project_id": "p", "resource_id": "i"},
                    "payload": {"description_html": "<p>" + "x" * 2100 + "</p>"},
                    "idempotency_key": "synthetic-key-123456",
                })
    asyncio.run(run())
    assert calls == []


def test_bound_file_delivers_full_unicode_and_rejects_mismatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / ".prepared-mutations"
    directory.mkdir()
    html = "<table>" + "".join(f"<tr><td>Imóvel {i:03}</td><td>αβ</td></tr>" for i in range(71)) + "</table>"
    operation = {"operation": "issue__update_issue_detail", "path_params": {"workspace_slug": "w", "project_id": "p", "resource_id": "i"}, "query": None, "payload": {"description_html": html}, "idempotency_key": "synthetic-key-123456"}
    data = json.dumps(operation, ensure_ascii=False).encode()
    (directory / "sample.json").write_bytes(data)
    calls = []
    class Fake:
        def execute_native_mutation_action(self, action, **kwargs):
            calls.append((action, kwargs))
            return {"status": "fake"}
    monkeypatch.setattr(server, "_client", lambda: Fake())
    monkeypatch.setattr(server, "_ledger_path", lambda: tmp_path / "ledger")
    args = {key: operation[key] for key in ("operation", "path_params", "idempotency_key")}
    args.update(prepared_file="sample.json", prepared_sha256=hashlib.sha256(data).hexdigest())
    async def run():
        async with Client(server.create_server()) as client:
            assert (await client.call_tool("plane_mutation_action", args)).data == {"status": "fake"}
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {**args, "payload": {"description_html": html[:400]}})
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {**args, "idempotency_key": "different-key-12345"})
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {**args, "prepared_sha256": "0" * 64})
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {**args, "path_params": {**args["path_params"], "resource_id": "other"}})
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {**args, "query": {"flag": "other"}})
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {**args, "operation": "issue__create_issue"})
            (directory / "sample.json").write_bytes(data + b" ")
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", args)
    asyncio.run(run())
    assert len(calls) == 1
    assert calls[0][1]["payload"]["description_html"] == html
    assert calls[0][1]["payload"]["description_html"].count("<tr>") == 71


def test_prepared_reader_rejects_unsafe_files(tmp_path, monkeypatch):
    from plane_mcp_karval.prepared_mutation import load_prepared
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".prepared-mutations"
    root.mkdir()
    (root / "real.json").write_text("{}")
    (root / "link.json").symlink_to("real.json")
    os.link(root / "real.json", root / "hard.json")
    os.mkfifo(root / "fifo.json")
    (root / "big.json").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    for name in ("../real.json", "link.json", "hard.json", "fifo.json", "big.json", "missing.json"):
        with pytest.raises(ValueError):
            load_prepared(name, "0" * 64)


@pytest.mark.parametrize("data", [b"{broken", b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', b'\xff'])
def test_invalid_prepared_json_rejected_before_client(tmp_path, monkeypatch, data):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".prepared-mutations"
    root.mkdir()
    (root / "input.json").write_bytes(data)
    calls = []
    monkeypatch.setattr(server, "_client", lambda: calls.append(True))
    async def run():
        async with Client(server.create_server()) as client:
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {"operation": "issue__update_issue_detail", "path_params": {}, "idempotency_key": "synthetic-key-123456", "prepared_file": "input.json", "prepared_sha256": hashlib.sha256(data).hexdigest()})
    asyncio.run(run())
    assert calls == []


def test_changed_file_during_descriptor_read_rejected_before_client(tmp_path, monkeypatch):
    from plane_mcp_karval import prepared_mutation
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".prepared-mutations"
    root.mkdir()
    path = root / "input.json"
    data = b'{}'
    path.write_bytes(data)
    real_read = os.read
    def changing_read(fd, size):
        chunk = real_read(fd, size)
        if chunk:
            path.write_bytes(b'{ }')
        return chunk
    monkeypatch.setattr(prepared_mutation.os, "read", changing_read)
    calls = []
    monkeypatch.setattr(server, "_client", lambda: calls.append(True))
    async def run():
        async with Client(server.create_server()) as client:
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {"operation": "issue__update_issue_detail", "path_params": {}, "idempotency_key": "synthetic-key-123456", "prepared_file": "input.json", "prepared_sha256": hashlib.sha256(data).hexdigest()})
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("operation,payload", [
    ("issue__create_issue", {"description_html": "x" * 2001}),
    ("issue__update_issue_detail", {"nested": [{"description_html": "x" * 2001}]}),
    ("issue_comment__add_issue_comment", {"comment_html": "x" * 2001}),
])
def test_registry_long_html_paths_reject_before_client(monkeypatch, operation, payload):
    calls = []
    monkeypatch.setattr(server, "_client", lambda: calls.append(True))
    async def run():
        async with Client(server.create_server()) as client:
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {"operation": operation, "path_params": {}, "payload": payload, "idempotency_key": "synthetic-key-123456"})
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("tool,extra", [
    ("plane_add_comment", {"comment_html": "x" * 2001}),
    ("plane_add_lifecycle_comment", {"comment": {}}),
    ("plane_lifecycle_transition", {"comment": {}}),
    ("plane_operator_lifecycle_transition", {"comment": {}, "expected_current_state_id": "a", "expected_updated_at": "t", "target_state_id": "b", "approved_live_mutation": True, "approved_non_atomic_operator_transition": True, "authorization_basis": "explicit_human_operator_authorization", "authorized_workspace_slug": "w", "authorized_project_id": "p", "authorized_work_item_id": "i", "contract_version": 3}),
])
def test_long_comment_helpers_fail_before_any_client_or_partial_state(monkeypatch, tool, extra):
    monkeypatch.setattr(server, "render_comment", lambda *a, **kw: "x" * 2001)
    calls = []
    monkeypatch.setattr(server, "_client", lambda: calls.append(True))
    args = {"workspace_slug": "w", "project_id": "p", "work_item_id": "i", "idempotency_key": "synthetic-key-123456", **extra}
    async def run():
        async with Client(server.create_server()) as client:
            with pytest.raises(Exception):
                await client.call_tool(tool, args)
    asyncio.run(run())
    assert calls == []


def test_historical_reconciliation_accepts_long_html(monkeypatch, tmp_path):
    calls = []
    class Fake:
        def reconcile_native_mutation(self, operation, **kwargs):
            calls.append(kwargs["payload"])
            return {"status": "read_only"}
    monkeypatch.setattr(server, "_client", lambda: Fake())
    monkeypatch.setattr(server, "_ledger_path", lambda: tmp_path / "ledger")
    payload = {"description_html": "x" * 2100}
    async def run():
        async with Client(server.create_server()) as client:
            result = await client.call_tool("plane_reconcile_mutation", {"operation": "issue__update_issue_detail", "path_params": {}, "payload": payload, "idempotency_key": "synthetic-key-123456"})
            assert result.data == {"status": "read_only"}
    asyncio.run(run())
    assert calls == [payload]


def test_fresh_process_dummy_provider_valid_once_invalid_zero(tmp_path):
    env = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "RAYON_NUM_THREADS": "2", "MALLOC_ARENA_MAX": "2"}
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_prepared_mutation.py::test_bound_file_delivers_full_unicode_and_rejects_mismatch"], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_v3_bound_comment_reaches_operator_precondition_without_retyping(tmp_path, monkeypatch):
    from test_operator_lifecycle_v3 import OperatorClient, operator_call
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".prepared-mutations"
    root.mkdir()
    args = operator_call(expected_current_state_id="other-state")
    comment = args.pop("comment")
    path_params = {key: args[key] for key in ("workspace_slug", "project_id", "work_item_id", "expected_current_state_id", "expected_updated_at", "target_state_id")}
    prepared = {"operation": "plane_operator_lifecycle_transition", "path_params": path_params, "query": None, "payload": {"comment": comment}, "idempotency_key": args["idempotency_key"]}
    data = json.dumps(prepared).encode()
    (root / "v3.json").write_bytes(data)
    args.update(prepared_file="v3.json", prepared_sha256=hashlib.sha256(data).hexdigest())
    fake = OperatorClient()
    monkeypatch.setattr(server, "_client", lambda: fake)
    async def run():
        async with Client(server.create_server()) as client:
            result = await client.call_tool("plane_operator_lifecycle_transition", args)
            assert result.data["status"] == "precondition_failed"
    asyncio.run(run())
    assert [name for name, _ in fake.calls] == ["get"]


def test_v3_binding_mismatch_never_reads_or_writes(tmp_path, monkeypatch):
    from test_operator_lifecycle_v3 import OperatorClient, operator_call
    fake = OperatorClient()
    monkeypatch.setattr(server, "_client", lambda: fake)
    args = bind_test_call(monkeypatch, tmp_path, "plane_operator_lifecycle_transition", operator_call())
    async def run():
        async with Client(server.create_server()) as client:
            for override in ({"expected_updated_at": "changed"}, {"target_state_id": "other"}, {"idempotency_key": "other-synthetic-key"}, {"comment": {"phase": "START"}}, {"prepared_sha256": "0" * 64}):
                with pytest.raises(Exception):
                    await client.call_tool("plane_operator_lifecycle_transition", {**args, **override})
    asyncio.run(run())
    assert fake.calls == []


def test_state_and_labels_without_html_remain_inline(monkeypatch, tmp_path):
    calls = []
    class Fake:
        def execute_native_mutation_action(self, operation, **kwargs):
            calls.append(kwargs["payload"])
            return {"status": "fake"}
    monkeypatch.setattr(server, "_client", lambda: Fake())
    monkeypatch.setattr(server, "_ledger_path", lambda: tmp_path / "ledger")
    async def run():
        async with Client(server.create_server()) as client:
            await client.call_tool("plane_mutation_action", {"operation": "issue__update_issue_detail", "path_params": {}, "payload": {"state": "ready", "labels": ["label"]}, "idempotency_key": "synthetic-key-123456"})
    asyncio.run(run())
    assert calls == [{"state": "ready", "labels": ["label"]}]


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_non_utf8_prepared_json_rejected_before_client(tmp_path, monkeypatch, encoding):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / ".prepared-mutations"
    root.mkdir()
    obj = {"operation": "issue__update_issue_detail", "path_params": {}, "query": None, "payload": {"description_html": "<p>Imóvel α</p>"}, "idempotency_key": "synthetic-key-123456"}
    data = json.dumps(obj, ensure_ascii=False).encode(encoding)
    (root / "input.json").write_bytes(data)
    calls = []
    monkeypatch.setattr(server, "_client", lambda: calls.append(True))
    async def run():
        async with Client(server.create_server()) as client:
            with pytest.raises(Exception):
                await client.call_tool("plane_mutation_action", {"operation": obj["operation"], "path_params": {}, "idempotency_key": obj["idempotency_key"], "prepared_file": "input.json", "prepared_sha256": hashlib.sha256(data).hexdigest()})
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("tool", ["plane_lifecycle_transition", "plane_operator_lifecycle_transition"])
def test_invalid_bound_lifecycle_comment_fails_before_client(tmp_path, monkeypatch, tool):
    from test_operator_lifecycle_v3 import operator_call
    if tool == "plane_operator_lifecycle_transition":
        args = operator_call()
        del args["comment"]["requested"]
        args = bind_test_call(monkeypatch, tmp_path, tool, args)
    else:
        monkeypatch.chdir(tmp_path)
        root = tmp_path / ".prepared-mutations"
        root.mkdir()
        args = {"workspace_slug": "w", "project_id": "p", "work_item_id": "i", "target_state_id": "new", "idempotency_key": "synthetic-key-123456"}
        target = {**{k: args[k] for k in ("workspace_slug", "project_id", "work_item_id", "target_state_id")}, "expected_state_id": None, "expected_updated_at": None}
        data = json.dumps({"operation": tool, "path_params": target, "query": None, "payload": {"comment": {}}, "idempotency_key": args["idempotency_key"]}).encode()
        (root / "test.json").write_bytes(data)
        args.update(prepared_file="test.json", prepared_sha256=hashlib.sha256(data).hexdigest())
    calls = []
    class Fake:
        def get_work_item(self, *args):
            calls.append("get")
            return {"id": "issue-1", "state": "ready-state", "updated_at": "2026-09-15T04:09:00Z"}
        def execute_native_mutation_action(self, operation, **kwargs):
            calls.append(operation)
            return {"readback_verified": True}
    monkeypatch.setattr(server, "_client", lambda: (calls.append("client"), Fake())[1])
    monkeypatch.setattr(server, "_ledger_path", lambda: tmp_path / "ledger")
    async def run():
        async with Client(server.create_server()) as client:
            with pytest.raises(Exception, match="invalid rich lifecycle comment"):
                await client.call_tool(tool, args)
    asyncio.run(run())
    assert calls == []
