"""Fail-closed operator approval broker core.

This module deliberately contains no transport, socket, credential, or provider
configuration.  Deployments must place request and control interfaces behind
separate protected channels and provide an external operator attestation.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
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
        self._request_id_factory = request_id_factory or (lambda: secrets.token_urlsafe(24))
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(32))
        self._requests: dict[str, PreparedRequest] = {}
        self._lock = RLock()

    def prepare(self, binding: BrokerBinding) -> PreparedRequest:
        self._validate_binding(binding)
        request = PreparedRequest(
            request_id=self._opaque(self._request_id_factory()),
            nonce=self._opaque(self._nonce_factory()),
            binding=binding,
            expires_at=self._clock() + self.TTL_SECONDS,
        )
        with self._lock:
            self._requests[request.request_id] = request
        return request

    def confirm(self, request_id: str, nonce: str, attestation: str) -> None:
        with self._lock:
            request = self._get(request_id)
            self._live(request)
            if not secrets.compare_digest(request.nonce, nonce):
                raise BrokerError("approval rejected")
            payload = f"{request.request_id}:{request.nonce}:{request.binding.digest()}"
            if not isinstance(attestation, str) or not self._signer.verify(attestation, payload):
                raise BrokerError("approval rejected")
            if request.state is not BrokerState.PREPARED:
                raise BrokerError("approval already consumed")
            self._requests[request_id] = PreparedRequest(
                request_id=request.request_id,
                nonce=request.nonce,
                binding=request.binding,
                expires_at=request.expires_at,
                state=BrokerState.CONFIRMED,
            )

    def execute(self, request_id: str) -> BrokerReceipt:
        # The state transition is consumed while holding the lock, before any
        # provider method is called. This is the double-execution boundary.
        with self._lock:
            request = self._get(request_id)
            self._live(request)
            if request.state is not BrokerState.CONFIRMED:
                raise BrokerError("explicit confirmation required")
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
                self._provider.execute(started.binding)
                receipt = BrokerReceipt(request_id, BrokerState.COMPLETED, "completed")
        except Exception:
            receipt = BrokerReceipt(request_id, BrokerState.FAILED_SAFE, "provider failure")
        return receipt

    def _get(self, request_id: str) -> PreparedRequest:
        if not isinstance(request_id, str) or not request_id or request_id not in self._requests:
            raise BrokerError("unknown request")
        return self._requests[request_id]

    def _live(self, request: PreparedRequest) -> None:
        if self._clock() >= request.expires_at:
            raise BrokerError("request expired")

    @staticmethod
    def _opaque(value: str) -> str:
        if not isinstance(value, str) or len(value) < 16 or not value.isascii():
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
