from __future__ import annotations

from dataclasses import dataclass
from threading import Thread

import pytest
import hashlib
import hmac
import json

from plane_mcp_karval.operator_broker import (
    BrokerBinding,
    BrokerError,
    BrokerState,
    AttestationVerifier,
    OperatorBroker,
    Provider as BrokerProvider,
)


@dataclass
class Clock:
    value: float = 100.0

    def __call__(self) -> float:
        return self.value


class Signer:
    def __init__(self, valid: bool = True):
        self.valid = valid

    def verify(self, attestation: str, payload: str) -> bool:
        return self.valid and attestation == "operator" and bool(payload)


class Provider:
    def __init__(self, updated_at: str = "v1"):
        self.updated_at = updated_at
        self.calls: list[str] = []

    def current_updated_at(self, binding: BrokerBinding) -> str:
        del binding
        self.calls.append("read")
        return self.updated_at

    def execute(self, binding: BrokerBinding) -> str:
        del binding
        self.calls.append("execute")
        return "ok"

    def conditional_execute(self, binding: BrokerBinding, expected_updated_at: str) -> str:
        self.calls.append("read")
        if expected_updated_at != self.updated_at:
            raise RuntimeError("version conflict")
        return self.execute(binding)

    def verify_readback(self, binding: BrokerBinding, result: str) -> bool:
        del binding, result
        return True


def binding(**changes: str) -> BrokerBinding:
    values = dict(
        provider="plane",
        tenant="tenant-a",
        workspace="workspace-a",
        project="project-a",
        work_item="item-a",
        expected_state="open",
        target_state="open",
        updated_at="v1",
        comment_hash="hash-a",
        policy="comment-v1",
        idempotency="idem-a",
        session="session-a",
        broker_boot="boot-a",
    )
    values.update(changes)
    return BrokerBinding(**values)


def broker(
    clock: Clock | None = None,
    signer: AttestationVerifier | None = None,
    provider: BrokerProvider | None = None,
):
    return OperatorBroker(clock=clock or Clock(), signer=signer or Signer(), provider=provider or Provider())


def test_prepare_confirm_execute_requires_explicit_operator_attestation():
    b = broker()
    request = b.prepare(binding())
    with pytest.raises(BrokerError):
        b.execute(request.request_id)
    b.confirm(request.request_id, request.nonce, "operator")
    receipt = b.execute(request.request_id)
    assert receipt.state is BrokerState.COMPLETED


@pytest.mark.parametrize("approval", [None, "forged", ""])
def test_missing_or_forged_approval_is_rejected_without_provider_io(approval):
    provider = Provider()
    b = broker(provider=provider)
    request = b.prepare(binding())
    with pytest.raises(BrokerError):
        if approval is None:
            b.execute(request.request_id)
        else:
            b.confirm(request.request_id, request.nonce, approval)
    assert provider.calls == []


def test_expired_and_replayed_and_substituted_approvals_fail():
    clock = Clock()
    provider = Provider()
    b = broker(clock=clock, provider=provider)
    first = b.prepare(binding())
    clock.value += 61
    with pytest.raises(BrokerError):
        b.confirm(first.request_id, first.nonce, "operator")
    second = b.prepare(binding(idempotency="idem-b"))
    with pytest.raises(BrokerError):
        b.confirm(first.request_id, second.nonce, "operator")
    b.confirm(second.request_id, second.nonce, "operator")
    b.execute(second.request_id)
    with pytest.raises(BrokerError):
        b.execute(second.request_id)
    assert provider.calls == ["read", "read", "execute"]


def test_drift_is_terminal_and_does_not_execute():
    provider = Provider(updated_at="v2")
    b = broker(provider=provider)
    request = b.prepare(binding())
    b.confirm(request.request_id, request.nonce, "operator")
    receipt = b.execute(request.request_id)
    assert receipt.state is BrokerState.DRIFTED
    assert provider.calls == ["read"]


def test_generic_state_mutation_is_denied_at_prepare():
    provider = Provider()
    b = broker(provider=provider)
    with pytest.raises(BrokerError):
        b.prepare(binding(policy="generic-state", target_state="closed"))
    assert provider.calls == []


def test_concurrent_execute_consumes_once_before_provider_io():
    provider = Provider()
    b = broker(provider=provider)
    request = b.prepare(binding())
    b.confirm(request.request_id, request.nonce, "operator")
    results: list[object] = []

    def run():
        try:
            results.append(b.execute(request.request_id))
        except BrokerError as error:
            results.append(error)

    first, second = Thread(target=run), Thread(target=run)
    first.start(); second.start(); first.join(); second.join()
    assert sum(isinstance(result, BrokerError) for result in results) == 1
    assert provider.calls.count("execute") == 1


class MutatesThenRaises(Provider):
    def conditional_execute(self, binding: BrokerBinding, expected_updated_at: str) -> str:
        del binding, expected_updated_at
        self.updated_at = "v2"
        self.calls.append("execute")
        raise RuntimeError("lost response")


class NoExactReadback(Provider):
    def verify_readback(self, binding: BrokerBinding, result: str) -> bool:
        del binding, result
        return False


def confirmed(b: OperatorBroker, request):
    b.confirm(request.request_id, request.nonce, "operator")
    return request


def test_provider_mutation_then_exception_is_unknown_not_completed():
    provider = MutatesThenRaises()
    b = broker(provider=provider)
    request = confirmed(b, b.prepare(binding()))
    receipt = b.execute(request.request_id)
    assert receipt.state is BrokerState.UNKNOWN
    assert b.receipt(request.request_id).state is receipt.state


def test_success_without_exact_verified_readback_is_not_completed_and_is_retained():
    provider = NoExactReadback()
    b = broker(provider=provider)
    request = confirmed(b, b.prepare(binding()))
    receipt = b.execute(request.request_id)
    assert receipt.state is BrokerState.APPLIED_UNVERIFIED
    assert b.receipt(request.request_id) == receipt


def test_idempotency_reuses_identical_binding_and_rejects_different_binding():
    b = broker()
    first = b.prepare(binding())
    same = b.prepare(binding())
    assert same.request_id == first.request_id
    with pytest.raises(BrokerError):
        b.prepare(binding(comment_hash="different"))


def test_request_id_and_nonce_collisions_are_rejected():
    b = OperatorBroker(
        clock=Clock(),
        signer=Signer(),
        provider=Provider(),
        request_id_factory=lambda: "request-collision-1234",
        nonce_factory=lambda: "nonce-collision-1234",
    )
    b.prepare(binding())
    with pytest.raises(BrokerError):
        b.prepare(binding(idempotency="idem-b"))
    b = OperatorBroker(
        clock=Clock(),
        signer=Signer(),
        provider=Provider(),
        request_id_factory=lambda: "request-unique-1234",
        nonce_factory=lambda: "nonce-collision-1234",
    )
    b.prepare(binding())
    with pytest.raises(BrokerError):
        b.prepare(binding(idempotency="idem-c"))


def test_two_confirmed_requests_for_one_target_cannot_both_execute():
    provider = Provider()
    b = broker(provider=provider)
    first = confirmed(b, b.prepare(binding()))
    second = confirmed(b, b.prepare(binding(idempotency="idem-b")))
    b.execute(first.request_id)
    with pytest.raises(BrokerError):
        b.execute(second.request_id)
    assert provider.calls.count("execute") == 1


class TestOnlyCryptoVerifier:
    secret = b"test-only-key"

    def verify(self, attestation: str, payload: str) -> bool:
        expected = hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(attestation, expected)


def test_test_only_verifier_binds_full_protocol_expiry_and_binding_payload():
    verifier = TestOnlyCryptoVerifier()
    b = broker(signer=verifier)
    request = b.prepare(binding())
    payload = b._attestation_payload(request)
    decoded = json.loads(payload)
    assert decoded["protocol"] == "operator-broker/v2"
    assert decoded["expires_at"] == request.expires_at
    assert decoded["binding_digest"] == request.binding.digest()
    attestation = hmac.new(verifier.secret, payload.encode(), hashlib.sha256).hexdigest()
    b.confirm(request.request_id, request.nonce, attestation)
    with pytest.raises(BrokerError):
        b.confirm(request.request_id, request.nonce, attestation)
