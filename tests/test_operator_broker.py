from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Event, Thread
from typing import Callable

import pytest
import hashlib
import hmac
import json

from plane_mcp_karval.operator_broker import (
    BrokerBinding,
    BrokerError,
    BrokerState,
    AttestationVerifier,
    BrokerMetadata,
    JsonStateStore,
    MemoryStateStore,
    ProductionStateStore,
    TrustedStoreConfig,
    create_production_broker,
    create_test_broker,
    Provider as BrokerProvider,
)
import plane_mcp_karval.operator_broker as broker_module


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

    def conditional_execute(self, binding: BrokerBinding, expected_updated_at: str) -> str:
        self.calls.append("read")
        if expected_updated_at != self.updated_at:
            raise RuntimeError("version conflict")
        del binding
        self.calls.append("execute")
        return "ok"

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
    clock: Callable[[], float] | None = None,
    signer: AttestationVerifier | None = None,
    provider: BrokerProvider | None = None,
):
    return OperatorBroker(
        clock=clock or Clock(),
        signer=signer or Signer(),
        provider=provider or Provider(),
        metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"),
        store=MemoryStateStore(),
    )


def make_test_broker(*, clock, signer, provider, metadata, store, request_id_factory=None, nonce_factory=None):
    return create_test_broker(
        metadata=metadata,
        verifier=signer,
        provider=provider,
        clock=clock,
        store=store,
        request_id_factory=request_id_factory,
        nonce_factory=nonce_factory,
    )


OperatorBroker = make_test_broker


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
        metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"),
        store=MemoryStateStore(),
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
        metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"),
        store=MemoryStateStore(),
    )
    b.prepare(binding())
    with pytest.raises(BrokerError):
        b.prepare(binding(idempotency="idem-c"))


def test_two_confirmed_requests_for_one_target_cannot_both_execute():
    class BlockingProvider(Provider):
        started = Event()
        release = Event()

        def conditional_execute(self, binding: BrokerBinding, expected_updated_at: str) -> str:
            self.started.set()
            self.release.wait()
            return super().conditional_execute(binding, expected_updated_at)

    provider = BlockingProvider()
    b = broker(provider=provider)
    first = confirmed(b, b.prepare(binding()))
    second = confirmed(b, b.prepare(binding(idempotency="idem-b")))
    worker = Thread(target=lambda: b.execute(first.request_id))
    worker.start()
    provider.started.wait()
    with pytest.raises(BrokerError):
        b.execute(second.request_id)
    provider.release.set()
    worker.join()
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


def test_terminal_state_is_on_request_and_verified_target_is_released():
    b = broker()
    first = confirmed(b, b.prepare(binding()))
    assert b.execute(first.request_id).state is BrokerState.COMPLETED
    assert b.request(first.request_id).state is BrokerState.COMPLETED
    second = confirmed(b, b.prepare(binding(idempotency="idem-next")))
    assert b.execute(second.request_id).state is BrokerState.COMPLETED


def test_unknown_quarantines_target_and_blocks_later_request():
    b = broker(provider=MutatesThenRaises())
    first = confirmed(b, b.prepare(binding()))
    assert b.execute(first.request_id).state is BrokerState.UNKNOWN
    second = confirmed(b, b.prepare(binding(idempotency="idem-next")))
    with pytest.raises(BrokerError):
        b.execute(second.request_id)


def test_clock_rejects_nan_and_regression_and_rechecks_after_read():
    import math

    with pytest.raises(BrokerError):
        broker(clock=lambda: math.nan)
    clock = Clock()
    b = broker(clock=clock)
    request = confirmed(b, b.prepare(binding()))
    clock.value = 99
    with pytest.raises(BrokerError):
        b.execute(request.request_id)

    class ExpiringProvider(Provider):
        def current_updated_at(self, binding: BrokerBinding) -> str:
            result = super().current_updated_at(binding)
            clock.value += 61
            return result

    clock = Clock()
    provider = ExpiringProvider()
    b = broker(clock=clock, provider=provider)
    request = confirmed(b, b.prepare(binding()))
    assert b.execute(request.request_id).state is BrokerState.EXPIRED
    assert "execute" not in provider.calls


def test_json_store_restart_retains_quarantine(tmp_path: Path):
    path = tmp_path / "broker-state.json"
    store = JsonStateStore(path)
    b = OperatorBroker(
        clock=Clock(), signer=Signer(), provider=MutatesThenRaises(),
        metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"),
        store=store,
    )
    request = confirmed(b, b.prepare(binding()))
    assert b.execute(request.request_id).state is BrokerState.UNKNOWN
    store.close()
    restarted = OperatorBroker(
        clock=Clock(), signer=Signer(), provider=Provider(),
        metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"),
        store=JsonStateStore(path),
    )
    assert restarted.request(request.request_id).state is BrokerState.UNKNOWN
    second = confirmed(restarted, restarted.prepare(binding(idempotency="idem-restart")))
    with pytest.raises(BrokerError):
        restarted.execute(second.request_id)


def test_metadata_is_required_and_store_lock_is_singleton(tmp_path: Path):
    with pytest.raises(BrokerError):
        OperatorBroker(
            clock=Clock(), signer=Signer(), provider=Provider(),
            metadata=BrokerMetadata("default", "tenant", "policy", "session", "boot"),
            store=MemoryStateStore(),
        )
    path = tmp_path / "locked.json"
    first = JsonStateStore(path)
    with pytest.raises(BrokerError):
        JsonStateStore(path)
    first.close()


def test_terminal_receipts_do_not_consume_pending_capacity_and_are_prunable():
    b = broker()
    b.MAX_PENDING = 1
    b.MAX_NONCES = 1
    first = confirmed(b, b.prepare(binding()))
    b.execute(first.request_id)
    second = confirmed(b, b.prepare(binding(idempotency="idem-next")))
    b.execute(second.request_id)
    assert b.request(first.request_id).state is BrokerState.COMPLETED


class TestStateAuthenticator:
    def authenticate(self, payload: bytes) -> str:
        return hashlib.sha256(b"test-only" + payload).hexdigest()

    def verify(self, payload: bytes, tag: str) -> bool:
        return hmac.compare_digest(self.authenticate(payload), tag)


def test_production_store_requires_existing_trusted_root_and_authenticator(tmp_path: Path):
    missing = tmp_path / "not-created"
    with pytest.raises(BrokerError):
        ProductionStateStore(TrustedStoreConfig(missing, "state.json", BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"), TestStateAuthenticator()))
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0o700)
    with pytest.raises(BrokerError):
        ProductionStateStore(TrustedStoreConfig(root, "state.json", BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"), None))


def test_production_store_rejects_missing_or_tampered_state(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0o700)
    config = TrustedStoreConfig(root, "state.json", BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"), TestStateAuthenticator())
    store = ProductionStateStore(config)
    with pytest.raises(BrokerError):
        store.load()
    store.close()
    (root / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BrokerError):
        ProductionStateStore(config).load()


def test_hostile_restored_confirmed_state_requires_valid_persisted_authorization(tmp_path: Path):
    path = tmp_path / "state.json"
    store = JsonStateStore(path)
    state = {
        "schema_version": 1,
        "metadata": {"provider": "provider-test", "tenant": "tenant-test", "policy": "comment-v1", "session": "session-test", "broker_boot": "boot-test"},
        "requests": {"request-1234567890123456": {"request_id": "request-1234567890123456", "nonce": "nonce-1234567890123456", "binding": asdict(binding()), "expires_at": 200.0, "state": "CONFIRMED", "authorization": None}},
        "receipts": {}, "idempotency": {}, "nonces": ["nonce-1234567890123456"], "inflight": [], "quarantine": [],
    }
    store.save(state)
    store.close()
    with pytest.raises(BrokerError):
        OperatorBroker(clock=Clock(), signer=Signer(), provider=Provider(), metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"), store=JsonStateStore(path))


def test_boot_identity_invalidates_prepared_but_preserves_unknown_evidence(tmp_path: Path):
    path = tmp_path / "boot-state.json"
    metadata = BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-one")
    store = JsonStateStore(path)
    broker_one = OperatorBroker(clock=Clock(), signer=Signer(), provider=MutatesThenRaises(), metadata=metadata, store=store)
    prepared = broker_one.prepare(binding())
    store.close()
    restarted = OperatorBroker(clock=Clock(), signer=Signer(), provider=Provider(), metadata=replace(metadata, broker_boot="boot-two"), store=JsonStateStore(path))
    assert restarted.request(prepared.request_id).state is BrokerState.EXPIRED


def test_factories_select_store_boundary_and_production_bootstrap_once(tmp_path: Path):
    metadata = BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test")
    test_broker = create_test_broker(metadata=metadata, verifier=Signer(), provider=Provider(), clock=Clock(), store=MemoryStateStore())
    assert test_broker.prepare(binding()).state is BrokerState.PREPARED
    root = tmp_path / "trusted"
    root.mkdir(); root.chmod(0o700)
    config = TrustedStoreConfig(root, "state.json", metadata, TestStateAuthenticator())
    initial = {"schema_version": 1, "metadata": asdict(metadata), "requests": {}, "receipts": {}, "idempotency": {}, "tombstones": {}, "nonces": [], "inflight": [], "quarantine": []}
    ProductionStateStore.bootstrap(config, initial)
    with pytest.raises(BrokerError):
        ProductionStateStore.bootstrap(config, initial)
    production = create_production_broker(config, Signer(), Provider(), Clock())
    assert production.prepare(binding()).state is BrokerState.PREPARED
    production._store.close()


def test_restored_authorization_payload_substitution_fails_constant_time_binding(tmp_path: Path):
    path = tmp_path / "substitution.json"
    store = JsonStateStore(path)
    first = OperatorBroker(clock=Clock(), signer=Signer(), provider=Provider(), metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"), store=store)
    request = confirmed(first, first.prepare(binding()))
    store.close()
    tampered = JsonStateStore(path)
    state = tampered.load()
    state["requests"][request.request_id]["authorization"][0] = "forged-payload"
    tampered.save(state)
    tampered.close()
    with pytest.raises(BrokerError):
        OperatorBroker(clock=Clock(), signer=Signer(), provider=Provider(), metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"), store=JsonStateStore(path))


def test_compacted_terminal_retains_restart_replay_tombstone(tmp_path: Path):
    path = tmp_path / "tombstone.json"
    store = JsonStateStore(path)
    b = broker()
    # Swap in an isolated durable test store through the explicit test factory.
    b = create_test_broker(metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"), verifier=Signer(), provider=Provider(), clock=Clock(), store=store)
    b.MAX_PENDING = 1
    first = confirmed(b, b.prepare(binding(idempotency="idem-one")))
    b.execute(first.request_id)
    second = confirmed(b, b.prepare(binding(idempotency="idem-two")))
    b.execute(second.request_id)
    b.prepare(binding(idempotency="idem-three"))
    store.close()
    restarted = create_test_broker(metadata=BrokerMetadata("provider-test", "tenant-test", "comment-v1", "session-test", "boot-test"), verifier=Signer(), provider=Provider(), clock=Clock(), store=JsonStateStore(path))
    with pytest.raises(BrokerError):
        restarted.prepare(binding(idempotency="idem-one"))


def test_unrestricted_public_operator_broker_constructor_is_removed():
    assert not hasattr(broker_module, "OperatorBroker")


def test_documented_production_api_exports_only_factory():
    import plane_mcp_karval.operator_broker_api as api

    assert api.__all__ == ("create_production_broker",)
    assert not hasattr(api, "create_test_broker")
    assert not hasattr(api, "MemoryStateStore")
