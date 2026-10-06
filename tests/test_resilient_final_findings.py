"""Regression proof for successor review: 503 pacing and abrupt-crash audit."""
import io
import json
from email.message import Message
from urllib.error import HTTPError

import pytest
from plane_api import resilience
from test_resilient_mutations import ACTION, Transport, setup, persisted


@pytest.mark.parametrize("correlation", [False, True])
def test_native_get_503_respects_retry_after(tmp_path, monkeypatch, correlation):
    class Overloaded(Transport):
        def __call__(self, request, timeout):
            if request.get_method() == "GET":
                self.calls.append("GET")
                headers = Message()
                headers["Retry-After"] = "30"
                raise HTTPError(request.full_url, 503, "fixture overload",
                                headers, io.BytesIO(b"{}"))
            return super().__call__(request, timeout)
    transport = Overloaded(lost=correlation)
    client, kwargs = setup(tmp_path, transport)
    sleeps = []
    monkeypatch.setattr(resilience.time, "sleep", sleeps.append)
    first = getattr(client, "execute_native_mutation_action")(ACTION, **kwargs)
    if correlation:
        assert first["status"] == "outcome_unknown"
        client.reconcile_native_mutation(ACTION, **kwargs)
    else:
        assert first["status"] == "write_succeeded_readback_validation_failed"
    assert transport.calls == ["POST", "GET"]
    assert sleeps == []


@pytest.mark.parametrize("boundary", ["sending", "reserved"])
def test_crash_boundary_survives_long_scan_and_authorization(tmp_path, boundary):
    transport = Transport(lost=True)
    client, kwargs = setup(tmp_path, transport)
    getattr(client, "execute_native_mutation_action")(ACTION, **kwargs)
    store = kwargs["ledger_path"]
    ledger = json.loads(store.read_text())
    receipt = next(iter(ledger["receipts"].values()))
    send_id = receipt["durable_send_attempt"]["send_attempt_id"]
    receipt.update(status=boundary, lease_until=0, attempt_history=[])
    receipt.pop("uncertain_write_attempts", None)
    if boundary == "reserved":
        receipt.pop("durable_send_attempt", None)
    store.write_text(json.dumps(ledger))  # Only this disposable crash-state fixture.
    transport.pages = [{"results": [{"id": f"unrelated-{index}"}], "count": 1,
        "total_results": 34, "total_count": 34, "total_pages": 34,
        "next_page_results": index < 33, "next_cursor": f"scan:{index + 1}" if index < 33 else None}
        for index in range(34)]
    result = client.reconcile_native_mutation(ACTION, **kwargs)
    assert result["recovery_evidence"]["complete"] is True
    current = persisted(kwargs)
    assert len(current["attempt_history"]) == resilience.MAX_HISTORY
    assert all(not entry["phase"].startswith("write:") for entry in current["attempt_history"])
    if boundary == "sending":
        writes = current["uncertain_write_attempts"]
        assert len(writes) == 1
        assert writes[0]["send_attempt_id"] == send_id
        assert writes[0]["phase"] == f"write:{ACTION}"
        assert writes[0]["observation"] == "expired_sending_lease"
    else:
        assert not current.get("uncertain_write_attempts")
        assert current["reserved_crash_boundary"]["boundary"] == "reserved_before_durable_send"
    resilience.authorize_reattempt(client, ACTION, operator_acknowledged_duplicate_risk=True,
                                  reason="Fixture authorizes bounded duplicate risk", **kwargs)
    archived = persisted(kwargs)["uncertain_attempts"][0]["uncertain_write_attempts"]
    assert archived == current.get("uncertain_write_attempts", [])
    assert transport.calls.count("POST") == 1
