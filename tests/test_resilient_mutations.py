import errno
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any
from urllib.error import URLError

import pytest

from plane_api.client import PlaneClient
from plane_api import resilience
from test_plane_client_failure_receipts import FakeResponse, ISSUE

ACTION = "issue__add_issue"
PARAMS = {"workspace_slug": "workspace", "project_id": "project"}


class Transport:
    __hermes_test_fake_transport__ = True

    def __init__(self, failures=0, *, lost=False, pages=None):
        self.failures, self.lost, self.pages = failures, lost, pages
        self.calls, self.row = [], None

    def __call__(self, request, timeout):
        method = request.get_method()
        self.calls.append(method)
        if method == "POST":
            if self.failures:
                self.failures -= 1
                raise URLError(socket.gaierror(socket.EAI_AGAIN, "SECRET DNS hostname"))
            self.row = {**ISSUE, **json.loads(request.data)}
            if self.lost:
                raise URLError("SECRET response lost")
            return FakeResponse(201, self.row)
        if request.full_url.endswith("work-items/") or "cursor=" in request.full_url:
            page = self.pages.pop(0) if self.pages is not None else {"results": [self.row], "next_page_results": False}
            if isinstance(page, Exception):
                raise page
            return FakeResponse(200, page)
        return FakeResponse(200, self.row)


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    monkeypatch.setattr(resilience.time, "sleep", lambda _: None)


def setup(tmp_path, transport):
    client = PlaneClient({"base_url": "https://plane.invalid", "token": "SECRET TOKEN"}, transport)
    kwargs: dict[str, Any] = dict(path_params=PARAMS, payload={"name": ISSUE["name"]},
                  idempotency_key="resilient-operation-key", ledger_path=tmp_path / "ledger.json")
    return client, kwargs


def persisted(kwargs):
    return next(iter(json.loads(kwargs["ledger_path"].read_text())["receipts"].values()))


def test_dns_recovery_and_completed_repeat(tmp_path):
    transport = Transport(failures=2)
    client, kwargs = setup(tmp_path, transport)
    result = client.execute_native_mutation_action(ACTION, **kwargs)
    assert result["status"] == "verified"
    assert transport.calls == ["POST", "POST", "POST", "GET"]
    assert transport.row["external_id"] == "plane-mcp:" + result["operation_id"]
    assert result["attempt_history"][0]["errno"] == socket.EAI_AGAIN
    assert "SECRET" not in json.dumps(persisted(kwargs))
    client.execute_native_mutation_action(ACTION, **kwargs)
    assert len(transport.calls) == 4


def test_same_key_resumes_only_proven_not_sent(tmp_path):
    transport = Transport(failures=3)
    client, kwargs = setup(tmp_path, transport)
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "not_sent_retryable"
    assert client.reconcile_native_mutation(ACTION, **kwargs)["status"] == "not_sent_retryable"
    assert len(transport.calls) == 3
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "verified"
    assert transport.calls.count("POST") == 4


def test_response_lost_correlated_without_post(tmp_path):
    transport = Transport(lost=True)
    client, kwargs = setup(tmp_path, transport)
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "outcome_unknown"
    assert client.reconcile_native_mutation(ACTION, **kwargs)["status"] == "verified"
    assert transport.calls == ["POST", "GET"]


@pytest.mark.parametrize("pages", [
    [{"results": [], "next_page_results": False}],
    [{"results": [], "next_page_results": True}],
    [{"results": []}],
    [URLError("SECRET failed lookup")] * 3,
])
def test_incomplete_empty_failed_lookup_never_replays(tmp_path, pages):
    transport = Transport(lost=True, pages=list(pages))
    client, kwargs = setup(tmp_path, transport)
    client.execute_native_mutation_action(ACTION, **kwargs)
    assert client.reconcile_native_mutation(ACTION, **kwargs)["status"] == "outcome_unknown"
    assert transport.calls.count("POST") == 1


def test_full_pagination_and_conflicting_matches(tmp_path):
    transport = Transport(lost=True)
    client, kwargs = setup(tmp_path, transport)
    client.execute_native_mutation_action(ACTION, **kwargs)
    transport.pages = [{"results": [transport.row], "next_page_results": True, "next_cursor": "second"},
                       {"results": [{**transport.row, "id": "different"}], "next_page_results": False}]
    assert client.reconcile_native_mutation(ACTION, **kwargs)["status"] == "correlation_conflict"
    assert transport.calls == ["POST", "GET", "GET"]


def test_concurrent_clients_one_post(tmp_path):
    entered, finish = Event(), Event()
    transport = Transport()

    def blocking(request, timeout):
        if request.get_method() == "POST":
            entered.set()
            assert finish.wait(5)
        return transport(request, timeout)
    blocking.__hermes_test_fake_transport__ = True
    client, kwargs = setup(tmp_path, blocking)
    with ThreadPoolExecutor(2) as executor:
        future = executor.submit(client.execute_native_mutation_action, ACTION, **kwargs)
        assert entered.wait(5)
        second = PlaneClient(client.config, blocking).execute_native_mutation_action(ACTION, **kwargs)
        assert second["status"] == "in_progress"
        finish.set()
        assert future.result()["status"] == "verified"
    assert transport.calls.count("POST") == 1


@pytest.mark.parametrize("phase", ["reserved", "sending"])
def test_expired_crash_lease_enters_recovery_not_post(tmp_path, phase):
    transport = Transport(failures=3)
    client, kwargs = setup(tmp_path, transport)
    client.execute_native_mutation_action(ACTION, **kwargs)
    ledger = json.loads(kwargs["ledger_path"].read_text())
    row = next(iter(ledger["receipts"].values()))
    row.update(status=phase, lease_until=0)
    kwargs["ledger_path"].write_text(json.dumps(ledger))
    transport.pages = [{"results": [], "next_page_results": False}]
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "outcome_unknown"
    assert transport.calls.count("POST") == 3


def test_acknowledged_write_failed_readback_never_resends(tmp_path):
    transport = Transport()
    client, kwargs = setup(tmp_path, transport)
    actual = transport.__call__
    failures = [3]
    def flaky(request, timeout):
        if request.get_method() == "GET" and failures[0]:
            failures[0] -= 1
            raise URLError("lost read")
        return actual(request, timeout)
    flaky.__hermes_test_fake_transport__ = True
    client._transport = flaky
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "write_succeeded_readback_validation_failed"
    assert client.reconcile_native_mutation(ACTION, **kwargs)["status"] == "verified"
    assert transport.calls.count("POST") == 1


@pytest.mark.parametrize("exception,expected", [
    (ConnectionRefusedError(errno.ECONNREFUSED, "SECRET"), "not_sent"),
    (socket.gaierror(socket.EAI_NONAME, "SECRET"), "uncertain"),
    (TimeoutError("SECRET"), "uncertain"),
])
def test_transport_classification_is_narrow(exception, expected):
    def fail(request, timeout):
        raise URLError(exception)
    fail.__hermes_test_fake_transport__ = True
    client = PlaneClient({"base_url": "https://plane.invalid"}, fail)
    with pytest.raises(Exception) as error:
        client._request("POST", "/test")
    assert error.value.detail["delivery"] == expected
    assert "SECRET" not in json.dumps(error.value.detail)


def test_mutation_redirect_never_retries_original_post(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            calls.append("POST")
            self.send_response(302)
            self.send_header("Location", "http://unreachable.invalid/redirect-target")
            self.end_headers()
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        client = PlaneClient({"base_url": f"http://127.0.0.1:{http.server_port}"})
        kwargs: dict[str, Any] = dict(path_params=PARAMS, payload={"name": ISSUE["name"]},
                      idempotency_key="redirect-uncertain-operation", ledger_path=tmp_path / "ledger.json")
        outcome = client.execute_native_mutation_action(ACTION, **kwargs)
        assert outcome["status"] == "outcome_unknown"
        assert outcome["attempt_history"][0]["http_status"] == 302
        assert calls == ["POST"]
    finally:
        http.shutdown()
        http.server_close()
        worker.join(5)


def test_ordinary_read_retries_and_respects_large_retry_after(monkeypatch):
    from urllib.error import HTTPError
    from io import BytesIO
    calls, sleeps = [], []
    errors = [URLError("read lost"), HTTPError("https://plane.invalid", 429, "slow", {"Retry-After": "0.25"}, BytesIO(b"{}"))]
    def transport(request, timeout):
        calls.append(request.get_method())
        if errors:
            raise errors.pop(0)
        return FakeResponse(200, {"id": "user"})
    transport.__hermes_test_fake_transport__ = True
    monkeypatch.setattr(resilience.time, "sleep", sleeps.append)
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    assert client.get_current_user() == {"id": "user"}
    assert calls == ["GET"] * 3
    assert sleeps[-1] >= .25
    errors.append(HTTPError("https://plane.invalid", 429, "slow", {"Retry-After": "30"}, BytesIO(b"{}")))
    with pytest.raises(Exception):
        client.get_current_user()
    assert len(calls) == 4


def test_shared_provider_cooldown_blocks_other_key_before_transport(tmp_path):
    from urllib.error import HTTPError
    from io import BytesIO
    def limited(request, timeout):
        raise HTTPError("https://plane.invalid", 429, "slow", {"Retry-After": "30"}, BytesIO(b"{}"))
    limited.__hermes_test_fake_transport__ = True
    client, kwargs = setup(tmp_path, limited)
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "outcome_unknown"
    def forbidden(request, timeout):
        raise AssertionError("shared cooldown must prevent request")
    forbidden.__hermes_test_fake_transport__ = True
    client._transport = forbidden
    kwargs["idempotency_key"] = "another-key-same-provider"
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "not_sent_retryable"


def test_stale_owner_fenced_cannot_overwrite_recovery(tmp_path):
    transport = Transport()
    client, kwargs = setup(tmp_path, transport)
    def fenced(request, timeout):
        response = transport(request, timeout)
        ledger = json.loads(kwargs["ledger_path"].read_text())
        row = next(iter(ledger["receipts"].values()))
        row.update(owner="replacement", fence=row["fence"] + 1, status="recovery_required")
        kwargs["ledger_path"].write_text(json.dumps(ledger))
        return response
    fenced.__hermes_test_fake_transport__ = True
    client._transport = fenced
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "recovery_required"
    assert persisted(kwargs)["owner"] == "replacement"
    assert persisted(kwargs)["status"] == "recovery_required"


def test_inventory_covers_registry_without_generic_patch_replay():
    from plane_api.registry import OPERATIONS, MUTATION_COUNT
    policies = {item.action: resilience.recovery_policy(item.action) for item in OPERATIONS if item.mutation}
    assert len(policies) == MUTATION_COUNT
    assert set(policies.values()) == {"exact_correlated_create", "acknowledged_readback_only"}


def test_effect_proof_is_separate_from_exact_correlation(tmp_path):
    transport = Transport(lost=True)
    client, kwargs = setup(tmp_path, transport)
    kwargs["payload"] = {"name": ISSUE["name"], "description_html": "<p>text</p>", "labels": ["label"]}
    client.execute_native_mutation_action(ACTION, **kwargs)
    transport.row.update(description_html="<p data-editor='yes'>text</p>", labels=[{"id": "label"}],
                         workspace="279a5ed3-7ed4-4310-8fbd-8c10c7eb8645")
    outcome = client.reconcile_native_mutation(ACTION, **kwargs)
    assert outcome["status"] == "verified"
    assert outcome["desired_state_evidence"]["description_html"] == "text_equivalent"
    assert outcome["desired_state_evidence"]["labels"] == "verified"
    assert outcome["desired_state_verified"] is False
    assert outcome["recovery_evidence"]["complete"] is True
    assert outcome["recovery_evidence"]["pages"] == 1
    assert outcome["recovery_evidence"]["rows"] == 1
    assert outcome["recovery_evidence"]["match_count"] == 1
    assert outcome["recovery_evidence"]["failure_code"] is None


@pytest.mark.parametrize("failure", [socket.gaierror(socket.EAI_AGAIN, "late DNS"),
                                    ConnectionRefusedError(errno.ECONNREFUSED, "late refusal")])
def test_response_body_error_is_never_not_sent(tmp_path, failure):
    calls = []
    class Response:
        def getcode(self):
            return 201
        def read(self):
            raise failure
    def transport(request, timeout):
        calls.append(request.get_method())
        return Response()
    transport.__hermes_test_fake_transport__ = True
    client, kwargs = setup(tmp_path, transport)
    outcome = client.execute_native_mutation_action(ACTION, **kwargs)
    assert outcome["status"] == "outcome_unknown"
    assert calls == ["POST"]
    assert outcome["attempt_history"][0]["delivery"] == "uncertain"


def test_legacy_unknown_correlated_recovery_preserves_original(tmp_path):
    transport = Transport(lost=True)
    client, kwargs = setup(tmp_path, transport)
    kwargs["payload"].update(external_id="legacy-explicit-id", external_source="legacy-source")
    client.execute_native_mutation_action(ACTION, **kwargs)
    ledger = json.loads(kwargs["ledger_path"].read_text())
    original = next(iter(ledger["receipts"].values()))
    original.pop("executor_version")
    original.update(status="attempt_started")
    snapshot = json.loads(json.dumps(original))
    kwargs["ledger_path"].write_text(json.dumps(ledger))
    assert client.reconcile_native_mutation(ACTION, **kwargs)["status"] == "verified"
    ledger = json.loads(kwargs["ledger_path"].read_text())
    assert next(iter(ledger["receipts"].values())) == snapshot
    assert next(iter(ledger["native_recoveries"].values()))["legacy_read_only_recovery"] is True
    assert transport.calls.count("POST") == 1


@pytest.mark.parametrize("invalid", ["no_ack", "incomplete", "failed", "active", "different", "stale"])
def test_operator_reattempt_requires_complete_bound_absence(tmp_path, invalid):
    transport = Transport(lost=True, pages=[{"results": [], "next_page_results": False}])
    client, kwargs = setup(tmp_path, transport)
    client.execute_native_mutation_action(ACTION, **kwargs)
    client.reconcile_native_mutation(ACTION, **kwargs)
    ledger = json.loads(kwargs["ledger_path"].read_text())
    row = next(iter(ledger["receipts"].values()))
    if invalid == "incomplete":
        row["recovery_evidence"]["complete"] = False
    if invalid == "failed":
        row["recovery_evidence"]["failure_code"] = "lookup_failed"
    if invalid == "active":
        row["lease_until"] = resilience.time.time() + 60
    if invalid == "stale":
        row["recovery_evidence"]["observed_at_epoch"] = 0
    kwargs["ledger_path"].write_text(json.dumps(ledger))
    if invalid == "different":
        kwargs["payload"] = {"name": "different"}
    with pytest.raises(PermissionError):
        resilience.authorize_reattempt(client, ACTION, **kwargs,
            operator_acknowledged_duplicate_risk=invalid != "no_ack", reason="operator accepts duplicate risk")
    assert transport.calls.count("POST") == 1


def test_operator_reattempt_preserves_ambiguity_and_is_one_shot(tmp_path):
    transport = Transport(lost=True, pages=[{"results": [], "next_page_results": False}])
    client, kwargs = setup(tmp_path, transport)
    initial = client.execute_native_mutation_action(ACTION, **kwargs)
    original_history = persisted(kwargs)["attempt_history"]
    client.reconcile_native_mutation(ACTION, **kwargs)
    auth = resilience.authorize_reattempt(client, ACTION, **kwargs,
        operator_acknowledged_duplicate_risk=True, reason="operator accepts duplicate risk")
    assert client.reconcile_native_mutation(ACTION, **kwargs)["status"] == "authorized_retry"
    assert transport.calls.count("POST") == 1
    transport.lost = False
    outcome = client.execute_native_mutation_action(ACTION, **kwargs)
    assert outcome["status"] == "verified"
    assert outcome["operation_id"] == initial["operation_id"] == auth["operation_id"]
    row = persisted(kwargs)
    assert row["reattempt_authorizations"][0]["consumed_at"]
    assert row["uncertain_attempts"][0]["status"] == "outcome_unknown"
    assert row["uncertain_attempts"][0]["attempt_history"][:len(original_history)] == original_history
    assert row["active_authorization_id"] == auth["authorization_id"]
    client.execute_native_mutation_action(ACTION, **kwargs)
    assert transport.calls.count("POST") == 2


@pytest.mark.parametrize("pages", [
    [[]],
    [{"results": [], "next_page_results": False, "next_cursor": "hidden"}],
    [{"results": [], "next_page_results": False, "total_results": 0, "total_count": 1}],
    [{"results": [], "next_page_results": False, "total_pages": 2}],
    [{"results": [], "next_page_results": False, "next_cursor": None, "count": True}],
    [{"results": [{"id": "one"}], "next_page_results": False, "total_results": True}],
    [{"results": [{"id": "one"}], "next_page_results": False, "total_count": True}],
    [{"results": [], "next_page_results": False, "total_pages": True}],
    [{"results": [], "next_cursor": None}],
    [{"results": [], "next_page_results": 0}],
    [{"results": [], "next_page_results": True, "next_cursor": "two", "total_results": 0},
     {"results": [], "next_page_results": False, "total_results": 1}],
    [{"results": [], "next_page_results": True, "next_cursor": "two", "total_count": 1},
     {"results": [], "next_page_results": False}],
    [{"results": [{"id": "same"}], "next_page_results": True, "next_cursor": "two"},
     {"results": [{"id": "same"}], "next_page_results": False, "total_results": 2}],
    [{"results": [], "next_page_results": True, "next_cursor": "two", "total_pages": 2},
     {"results": [], "next_page_results": False, "total_pages": 3}],
    [{"results": [], "next_page_results": False, "count": 1}],
])
def test_invalid_scan_cannot_authorize_even_with_risk_ack(tmp_path, pages):
    transport = Transport(lost=True, pages=list(pages))
    client, kwargs = setup(tmp_path, transport)
    client.execute_native_mutation_action(ACTION, **kwargs)
    result = client.reconcile_native_mutation(ACTION, **kwargs)
    assert result["status"] == "outcome_unknown"
    assert result["recovery_evidence"]["complete"] is False
    assert result["recovery_evidence"]["failure_code"]
    with pytest.raises(PermissionError):
        resilience.authorize_reattempt(client, ACTION, **kwargs,
            operator_acknowledged_duplicate_risk=True, reason="operator accepts duplicate risk")
    assert transport.calls.count("POST") == 1


def test_valid_multipage_scan_checks_global_and_page_counts(tmp_path):
    transport = Transport(lost=True)
    client, kwargs = setup(tmp_path, transport)
    client.execute_native_mutation_action(ACTION, **kwargs)
    transport.pages = [
        {"results": [{"id": "other"}], "count": 1, "total_results": 2, "total_count": 2,
         "total_pages": 2, "next_page_results": True, "next_cursor": "two"},
        {"results": [transport.row], "count": 1, "total_results": 2, "total_count": 2,
         "total_pages": 2, "next_page_results": False, "next_cursor": None}]
    result = client.reconcile_native_mutation(ACTION, **kwargs)
    assert result["status"] == "verified"
    assert result["recovery_evidence"]["complete"] is True
    assert result["recovery_evidence"]["rows"] == 2
    assert transport.calls == ["POST", "GET", "GET"]


def legacy_fixture(tmp_path, *, absent=False):
    transport = Transport(lost=True)
    client, kwargs = setup(tmp_path, transport)
    kwargs["payload"].update(external_id="legacy-explicit-id", external_source="legacy-source")
    client.execute_native_mutation_action(ACTION, **kwargs)
    ledger = json.loads(kwargs["ledger_path"].read_text())
    original = next(iter(ledger["receipts"].values()))
    original.pop("executor_version")
    original.update(status="attempt_started")
    snapshot = json.loads(json.dumps(original))
    kwargs["ledger_path"].write_text(json.dumps(ledger))
    if absent:
        transport.pages = [{"results": [], "next_page_results": False}]
    return transport, client, kwargs, snapshot


def test_legacy_recovered_repeat_returns_bound_completion_without_post(tmp_path):
    transport, client, kwargs, snapshot = legacy_fixture(tmp_path)
    recovered = client.reconcile_native_mutation(ACTION, **kwargs)
    repeated = client.execute_native_mutation_action(ACTION, **kwargs)
    assert recovered["status"] == repeated["status"] == "verified"
    assert repeated["operation_id"] == recovered["operation_id"]
    assert repeated["duplicate"] is True
    assert persisted(kwargs) == snapshot
    assert transport.calls == ["POST", "GET"]


@pytest.mark.parametrize("invalid", ["no_ack", "incomplete", "stale", "target", "provider", "key"])
def test_legacy_successor_authorization_fail_closed(tmp_path, invalid):
    transport, client, kwargs, snapshot = legacy_fixture(tmp_path, absent=True)
    client.reconcile_native_mutation(ACTION, **kwargs)
    ledger = json.loads(kwargs["ledger_path"].read_text())
    successor = next(iter(ledger["native_recoveries"].values()))
    if invalid == "incomplete":
        successor["recovery_evidence"]["complete"] = False
    if invalid == "stale":
        successor["recovery_evidence"]["observed_at_epoch"] = 0
    if invalid == "provider":
        successor["provider_instance_hash"] = "wrong"
    if invalid == "key":
        successor["legacy_original_key_hash"] = "wrong"
    kwargs["ledger_path"].write_text(json.dumps(ledger))
    if invalid == "target":
        kwargs["payload"]["external_id"] = "different"
    with pytest.raises(PermissionError):
        resilience.authorize_reattempt(client, ACTION, **kwargs,
            operator_acknowledged_duplicate_risk=invalid != "no_ack", reason="operator accepts duplicate risk")
    assert persisted(kwargs) == snapshot
    assert transport.calls.count("POST") == 1


def test_legacy_authorized_successor_preserves_original_and_repeat_no_post(tmp_path):
    transport, client, kwargs, snapshot = legacy_fixture(tmp_path, absent=True)
    initial = client.reconcile_native_mutation(ACTION, **kwargs)
    # Ordinary execute remains read-only until explicit authorization.
    transport.pages = [{"results": [], "next_page_results": False}]
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "outcome_unknown"
    auth = resilience.authorize_reattempt(client, ACTION, **kwargs,
        operator_acknowledged_duplicate_risk=True, reason="operator accepts duplicate risk")
    assert client.reconcile_native_mutation(ACTION, **kwargs)["status"] == "authorized_retry"
    transport.lost = False
    result = client.execute_native_mutation_action(ACTION, **kwargs)
    assert result["status"] == "verified"
    assert result["operation_id"] == initial["operation_id"] == auth["operation_id"]
    assert transport.row["external_id"] == kwargs["payload"]["external_id"]
    assert transport.row["external_source"] == kwargs["payload"]["external_source"]
    ledger = json.loads(kwargs["ledger_path"].read_text())
    successor = next(iter(ledger["native_recoveries"].values()))
    assert successor["reattempt_authorizations"][0]["consumed_at"]
    assert successor["reattempt_authorizations"][0]["duplicate_risk_acknowledged"] is True
    assert successor["uncertain_attempts"][0]["status"] == "outcome_unknown"
    assert successor["ack_validated"] is True
    assert persisted(kwargs) == snapshot
    calls = list(transport.calls)
    assert client.execute_native_mutation_action(ACTION, **kwargs)["status"] == "verified"
    assert transport.calls == calls
    assert transport.calls.count("POST") == 2
