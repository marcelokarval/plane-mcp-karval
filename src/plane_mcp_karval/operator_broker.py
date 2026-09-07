"""Fail-closed operator approval broker core.

This module deliberately contains no transport, socket, credential, or provider
configuration.  Deployments must place request and control interfaces behind
separate protected channels and provide an external operator attestation.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from enum import Enum
from threading import RLock
from typing import Callable, Protocol


class BrokerError(Exception):
    """Safe, non-sensitive rejection from the broker."""


class BrokerState(str, Enum):
    PREPARED = "PREPARED"
    CONFIRMED = "CONFIRMED"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    COMPLETED = "COMPLETED"
    FAILED_SAFE = "FAILED_SAFE"
    DRIFTED = "DRIFTED"
    UNKNOWN = "UNKNOWN"
    APPLIED_UNVERIFIED = "APPLIED_UNVERIFIED"


@dataclass(frozen=True)
class BrokerBinding:
    provider: str
    tenant: str
    workspace: str
    project: str
    work_item: str
    expected_state: str
    target_state: str
    updated_at: str
    comment_hash: str
    policy: str
    idempotency: str
    session: str
    broker_boot: str

    def canonical(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()


@dataclass(frozen=True)
class PreparedRequest:
    request_id: str
    nonce: str
    binding: BrokerBinding
    expires_at: float
    state: BrokerState = BrokerState.PREPARED


@dataclass(frozen=True)
class BrokerReceipt:
    request_id: str
    state: BrokerState
    detail: str


class Provider(Protocol):
    def current_updated_at(self, binding: BrokerBinding) -> str: ...

    def execute(self, binding: BrokerBinding) -> str: ...

    def conditional_execute(self, binding: BrokerBinding, expected_updated_at: str) -> str: ...

    def verify_readback(self, binding: BrokerBinding, result: str) -> bool: ...


class AttestationVerifier(Protocol):
    def verify(self, attestation: str, payload: str) -> bool: ...


class RequestInterface(Protocol):
    def prepare(self, binding: BrokerBinding) -> PreparedRequest: ...

    def execute(self, request_id: str) -> BrokerReceipt: ...


class ControlInterface(Protocol):
    def confirm(self, request_id: str, nonce: str, attestation: str) -> None: ...


class OperatorBroker:
    """In-memory broker core; persistence/transport belongs to deployment code."""

    TTL_SECONDS = 60.0
    MAX_PENDING = 1024
    MAX_IDENTIFIER = 256
    ATTESTATION_MAX = 4096

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        signer: AttestationVerifier,
        provider: Provider,
        request_id_factory: Callable[[], str] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._signer = signer
        self._provider = provider
        if not callable(getattr(provider, "conditional_execute", None)) or not callable(
            getattr(provider, "verify_readback", None)
        ):
            raise BrokerError("provider conditional capability required")
        self._request_id_factory = request_id_factory or (lambda: secrets.token_urlsafe(24))
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(32))
        self._requests: dict[str, PreparedRequest] = {}
        self._receipts: dict[str, BrokerReceipt] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._nonces: set[str] = set()
        self._targets: set[tuple[str, ...]] = set()
        self._owned = {
            "provider": "plane",
            "tenant": "tenant-a",
            "policy": "comment-v1",
            "session": secrets.token_urlsafe(16),
            "broker_boot": secrets.token_urlsafe(16),
        }
        self._lock = RLock()

    def prepare(self, binding: BrokerBinding) -> PreparedRequest:
        self._validate_binding(binding)
        binding = replace(binding, **self._owned)
        request = PreparedRequest(
            request_id=self._opaque(self._request_id_factory()),
            nonce=self._opaque(self._nonce_factory()),
            binding=binding,
            expires_at=self._clock() + self.TTL_SECONDS,
        )
        with self._lock:
            self._prune()
            existing = self._idempotency.get(binding.idempotency)
            if existing:
                existing_id, existing_digest = existing
                if existing_digest != binding.digest():
                    raise BrokerError("idempotency binding conflict")
                return self._requests[existing_id]
            pending = sum(
                request.state in {
                    BrokerState.PREPARED,
                    BrokerState.CONFIRMED,
                    BrokerState.ATTEMPT_STARTED,
                }
                for request in self._requests.values()
            )
            if pending >= self.MAX_PENDING:
                raise BrokerError("broker queue full")
            if request.request_id in self._requests or request.request_id in self._receipts:
                raise BrokerError("request identifier collision")
            if request.nonce in self._nonces:
                raise BrokerError("nonce collision")
            self._requests[request.request_id] = request
            self._idempotency[binding.idempotency] = (request.request_id, binding.digest())
            self._nonces.add(request.nonce)
        return request

    def confirm(self, request_id: str, nonce: str, attestation: str) -> None:
        with self._lock:
            request = self._get(request_id)
            self._live(request)
            if not secrets.compare_digest(request.nonce, nonce):
                raise BrokerError("approval rejected")
            if not isinstance(attestation, str) or len(attestation) > self.ATTESTATION_MAX:
                raise BrokerError("approval rejected")
            payload = self._attestation_payload(request)
        # Signature verification may involve a protected control interface and
        # must not hold the broker's state lock.
        if not self._signer.verify(attestation, payload):
            raise BrokerError("approval rejected")
        with self._lock:
            current = self._get(request_id)
            if current is not request or current.state is not BrokerState.PREPARED:
                raise BrokerError("approval already consumed")
            self._live(current)
            self._requests[request_id] = replace(current, state=BrokerState.CONFIRMED)

    def execute(self, request_id: str) -> BrokerReceipt:
        # The state transition is consumed while holding the lock, before any
        # provider method is called. This is the double-execution boundary.
        with self._lock:
            request = self._get(request_id)
            self._live(request)
            if request.state is not BrokerState.CONFIRMED:
                raise BrokerError("explicit confirmation required")
            target = self._target(request.binding)
            if target in self._targets:
                raise BrokerError("target already consumed")
            self._targets.add(target)
            started = PreparedRequest(
                request_id=request.request_id,
                nonce=request.nonce,
                binding=request.binding,
                expires_at=request.expires_at,
                state=BrokerState.ATTEMPT_STARTED,
            )
            self._requests[request_id] = started

        try:
            if self._provider.current_updated_at(started.binding) != started.binding.updated_at:
                receipt = BrokerReceipt(request_id, BrokerState.DRIFTED, "binding drift")
            else:
                result = self._provider.conditional_execute(
                    started.binding, started.binding.updated_at
                )
                if not self._provider.verify_readback(started.binding, result):
                    receipt = BrokerReceipt(
                        request_id, BrokerState.APPLIED_UNVERIFIED, "readback not verified"
                    )
                else:
                    receipt = BrokerReceipt(request_id, BrokerState.COMPLETED, "completed")
        except Exception:
            receipt = BrokerReceipt(request_id, BrokerState.UNKNOWN, "provider outcome unknown")
        with self._lock:
            self._receipts[request_id] = receipt
        return receipt

    def receipt(self, request_id: str) -> BrokerReceipt:
        with self._lock:
            try:
                return self._receipts[request_id]
            except KeyError as error:
                raise BrokerError("receipt unavailable") from error

    def _get(self, request_id: str) -> PreparedRequest:
        if not isinstance(request_id, str) or not request_id or request_id not in self._requests:
            raise BrokerError("unknown request")
        return self._requests[request_id]

    def _live(self, request: PreparedRequest) -> None:
        if self._clock() >= request.expires_at:
            raise BrokerError("request expired")

    def _prune(self) -> None:
        now = self._clock()
        expired = [
            request_id
            for request_id, request in self._requests.items()
            if request.expires_at <= now and request.state in {BrokerState.PREPARED, BrokerState.CONFIRMED}
        ]
        for request_id in expired:
            request = self._requests.pop(request_id)
            self._idempotency.pop(request.binding.idempotency, None)

    @staticmethod
    def _target(binding: BrokerBinding) -> tuple[str, ...]:
        return (binding.provider, binding.tenant, binding.workspace, binding.project, binding.work_item)

    @staticmethod
    def _attestation_payload(request: PreparedRequest) -> str:
        return json.dumps(
            {
                "protocol": "operator-broker/v2",
                "request_id": request.request_id,
                "nonce": request.nonce,
                "expires_at": request.expires_at,
                "binding": json.loads(request.binding.canonical()),
                "binding_digest": request.binding.digest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _opaque(value: str) -> str:
        if not isinstance(value, str) or not 16 <= len(value) <= OperatorBroker.MAX_IDENTIFIER or not value.isascii():
            raise BrokerError("broker identifier generation failed")
        return value

    @staticmethod
    def _validate_binding(binding: BrokerBinding) -> None:
        if not isinstance(binding, BrokerBinding) or any(
            not isinstance(value, str) or not value or len(value) > 512
            for value in binding.__dict__.values()
        ):
            raise BrokerError("invalid binding")
        if binding.policy != "comment-v1" or binding.target_state != binding.expected_state:
            raise BrokerError("generic state mutation denied")
