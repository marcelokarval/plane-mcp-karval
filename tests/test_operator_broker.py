from __future__ import annotations

from dataclasses import dataclass
from threading import Thread

import pytest

from plane_mcp_karval.operator_broker import (
    BrokerBinding,
    BrokerError,
    BrokerState,
    OperatorBroker,
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


def broker(clock: Clock | None = None, signer: Signer | None = None, provider: Provider | None = None):
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
    assert provider.calls == ["read", "execute"]


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
