"""Fail-closed operator approval broker core.

This module deliberately contains no transport, socket, credential, or provider
configuration.  Deployments must place request and control interfaces behind
separate protected channels and provide an external operator attestation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Callable, Protocol


class BrokerError(Exception):
    """Safe, non-sensitive rejection from the broker."""


class BrokerState(str, Enum):
    PREPARED = "PREPARED"
    CONFIRMED = "CONFIRMED"
    ATTEMPT_STARTED = "ATTEMPT_STARTED"
    COMPLETED = "COMPLETED"
    DRIFTED = "DRIFTED"
    UNKNOWN = "UNKNOWN"
    APPLIED_UNVERIFIED = "APPLIED_UNVERIFIED"
    EXPIRED = "EXPIRED"


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


@dataclass(frozen=True)
class BrokerMetadata:
    provider: str
    tenant: str
    policy: str
    session: str
    broker_boot: str


class StateStore(Protocol):
    def load(self) -> dict[str, object]: ...

    def save(self, state: dict[str, object]) -> None: ...

    def close(self) -> None: ...


class MemoryStateStore:
    """Explicitly test-only store; production callers must inject JsonStateStore."""

    def __init__(self) -> None:
        self._state: dict[str, object] = {}

    def load(self) -> dict[str, object]:
        return json.loads(json.dumps(self._state))

    def save(self, state: dict[str, object]) -> None:
        self._state = json.loads(json.dumps(state))

    def close(self) -> None:
        return None


class JsonStateStore:
    """Atomic JSON store with a singleton process lock for a future root path."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise BrokerError("state path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.path.with_name(self.path.name + ".lock")
        try:
            self._lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise BrokerError("state store already locked") from error

    def load(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError) as error:
            raise BrokerError("state store unavailable") from error
        if not isinstance(value, dict):
            raise BrokerError("invalid state store")
        return value

    def save(self, state: dict[str, object]) -> None:
        payload = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def close(self) -> None:
        os.close(self._lock_fd)
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass


class Provider(Protocol):
    def current_updated_at(self, binding: BrokerBinding) -> str: ...

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
    MAX_NONCES = 2048
    MAX_IDENTIFIER = 256
    ATTESTATION_MAX = 4096

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        signer: AttestationVerifier,
        provider: Provider,
        metadata: BrokerMetadata,
        store: StateStore,
        request_id_factory: Callable[[], str] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._signer = signer
        self._provider = provider
        self._validate_metadata(metadata)
        self._store = store
        if not callable(getattr(provider, "conditional_execute", None)) or not callable(
            getattr(provider, "verify_readback", None)
        ):
            raise BrokerError("provider conditional capability required")
        self._request_id_factory = request_id_factory or (lambda: secrets.token_urlsafe(24))
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(32))
        self._requests: dict[str, PreparedRequest] = {}
        self._receipts: dict[str, BrokerReceipt] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._nonces: dict[str, float] = {}
        self._inflight: set[tuple[str, ...]] = set()
        self._quarantine: set[tuple[str, ...]] = set()
        self._owned = metadata
        self._last_now = self._read_clock()
        self._lock = RLock()
        self._restore(self._store.load())

    def prepare(self, binding: BrokerBinding) -> PreparedRequest:
        self._validate_binding(binding)
        binding = replace(binding, **asdict(self._owned))
        request = PreparedRequest(
            request_id=self._opaque(self._request_id_factory()),
            nonce=self._opaque(self._nonce_factory()),
            binding=binding,
            expires_at=self._now() + self.TTL_SECONDS,
        )
        with self._lock:
            self._prune()
            self._prune_nonces()
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
            self._nonces[request.nonce] = self._last_now
            self._persist()
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
            self._persist()

    def execute(self, request_id: str) -> BrokerReceipt:
        # The state transition is consumed while holding the lock, before any
        # provider method is called. This is the double-execution boundary.
        with self._lock:
            request = self._get(request_id)
            self._live(request)
            if request.state is not BrokerState.CONFIRMED:
                raise BrokerError("explicit confirmation required")
            target = self._target(request.binding)
            if target in self._inflight or target in self._quarantine:
                raise BrokerError("target already consumed")
            self._inflight.add(target)
            started = PreparedRequest(
                request_id=request.request_id,
                nonce=request.nonce,
                binding=request.binding,
                expires_at=request.expires_at,
                state=BrokerState.ATTEMPT_STARTED,
            )
            self._requests[request_id] = started
            self._persist()

        try:
            if self._provider.current_updated_at(started.binding) != started.binding.updated_at:
                if self._now() >= started.expires_at:
                    receipt = BrokerReceipt(request_id, BrokerState.EXPIRED, "request expired")
                else:
                    receipt = BrokerReceipt(request_id, BrokerState.DRIFTED, "binding drift")
            elif self._now() >= started.expires_at:
                receipt = BrokerReceipt(request_id, BrokerState.EXPIRED, "request expired")
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
        except BrokerError:
            raise
        except Exception:
            receipt = BrokerReceipt(request_id, BrokerState.UNKNOWN, "provider outcome unknown")
        with self._lock:
            terminal = replace(started, state=receipt.state)
            self._requests[request_id] = terminal
            self._receipts[request_id] = receipt
            self._inflight.discard(self._target(started.binding))
            if receipt.state in {BrokerState.UNKNOWN, BrokerState.APPLIED_UNVERIFIED}:
                self._quarantine.add(self._target(started.binding))
            self._prune_receipts()
            self._prune_nonces()
            self._persist()
        return receipt

    def receipt(self, request_id: str) -> BrokerReceipt:
        with self._lock:
            try:
                return self._receipts[request_id]
            except KeyError as error:
                raise BrokerError("receipt unavailable") from error

    def request(self, request_id: str) -> PreparedRequest:
        with self._lock:
            return self._get(request_id)

    def _get(self, request_id: str) -> PreparedRequest:
        if not isinstance(request_id, str) or not request_id or request_id not in self._requests:
            raise BrokerError("unknown request")
        return self._requests[request_id]

    def _live(self, request: PreparedRequest) -> None:
        if self._now() >= request.expires_at:
            raise BrokerError("request expired")

    def _prune(self) -> None:
        now = self._now()
        expired = [
            request_id
            for request_id, request in self._requests.items()
            if request.expires_at <= now and request.state in {BrokerState.PREPARED, BrokerState.CONFIRMED}
        ]
        for request_id in expired:
            request = self._requests.pop(request_id)
            self._idempotency.pop(request.binding.idempotency, None)

    def _prune_receipts(self) -> None:
        if len(self._receipts) <= self.MAX_PENDING:
            return
        for request_id in list(self._receipts)[: len(self._receipts) - self.MAX_PENDING]:
            self._receipts.pop(request_id, None)

    def _prune_nonces(self) -> None:
        if len(self._nonces) <= self.MAX_NONCES:
            return
        active = {
            request.nonce
            for request in self._requests.values()
            if request.state in {BrokerState.PREPARED, BrokerState.CONFIRMED, BrokerState.ATTEMPT_STARTED}
        }
        for nonce, _created in sorted(self._nonces.items(), key=lambda item: item[1]):
            if len(self._nonces) <= self.MAX_NONCES:
                break
            if nonce not in active:
                self._nonces.pop(nonce, None)

    def _persist(self) -> None:
        self._store.save(
            {
                "requests": {
                    request_id: {
                        "request_id": request.request_id,
                        "nonce": request.nonce,
                        "binding": asdict(request.binding),
                        "expires_at": request.expires_at,
                        "state": request.state.value,
                    }
                    for request_id, request in self._requests.items()
                },
                "receipts": {
                    request_id: {"request_id": receipt.request_id, "state": receipt.state.value, "detail": receipt.detail}
                    for request_id, receipt in self._receipts.items()
                },
                "idempotency": self._idempotency,
                "nonces": sorted(self._nonces),
                "metadata": asdict(self._owned),
                "inflight": [list(target) for target in self._inflight],
                "quarantine": [list(target) for target in self._quarantine],
            }
        )

    def _restore(self, state: dict[str, object]) -> None:
        for request_id, raw in dict(state.get("requests", {})).items():
            item = dict(raw)
            self._requests[request_id] = PreparedRequest(
                request_id=item["request_id"], nonce=item["nonce"],
                binding=BrokerBinding(**dict(item["binding"])),
                expires_at=float(item["expires_at"]), state=BrokerState(item["state"]),
            )
        for request_id, raw in dict(state.get("receipts", {})).items():
            item = dict(raw)
            self._receipts[request_id] = BrokerReceipt(
                request_id=item["request_id"], state=BrokerState(item["state"]), detail=item["detail"]
            )
        self._idempotency = {
            key: (value[0], value[1]) for key, value in dict(state.get("idempotency", {})).items()
        }
        stored_metadata = state.get("metadata")
        if stored_metadata is not None and stored_metadata != asdict(self._owned):
            raise BrokerError("state metadata mismatch")
        self._nonces = {
            nonce: float(index) for index, nonce in enumerate(state.get("nonces", []))
        }
        self._inflight = {tuple(target) for target in state.get("inflight", [])}
        self._quarantine = {tuple(target) for target in state.get("quarantine", [])}

    @staticmethod
    def _validate_metadata(metadata: BrokerMetadata) -> None:
        if not isinstance(metadata, BrokerMetadata) or any(
            not isinstance(value, str) or not value or len(value) > OperatorBroker.MAX_IDENTIFIER
            or value.lower() in {"default", "placeholder", "changeme", "example"}
            for value in asdict(metadata).values()
        ):
            raise BrokerError("immutable broker metadata required")

    def _read_clock(self) -> float:
        value = self._clock()
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise BrokerError("clock must be finite")
        return float(value)

    def _now(self) -> float:
        value = self._read_clock()
        if value < self._last_now:
            raise BrokerError("clock regressed")
        self._last_now = value
        return value

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
