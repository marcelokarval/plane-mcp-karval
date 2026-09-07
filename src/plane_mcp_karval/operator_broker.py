"""Fail-closed operator approval broker core.

This module deliberately contains no transport, socket, credential, or provider
configuration.  Deployments must place request and control interfaces behind
separate protected channels and provide an external operator attestation.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import math
import os
import secrets
import stat
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
    authorization: tuple[str, str] | None = None


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
    """Explicitly test-only store; production callers must inject ProductionStateStore."""

    def __init__(self) -> None:
        self._state: dict[str, object] = {}

    def load(self) -> dict[str, object]:
        return json.loads(json.dumps(self._state))

    def save(self, state: dict[str, object]) -> None:
        self._state = json.loads(json.dumps(state))

    def close(self) -> None:
        return None


class JsonStateStore:
    """Test-only atomic JSON store; production uses the trusted adapter below."""

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


@dataclass(frozen=True)
class TrustedStoreConfig:
    root: Path
    filename: str
    metadata: BrokerMetadata
    authenticator: "StateAuthenticator"


class StateAuthenticator(Protocol):
    def authenticate(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, tag: str) -> bool: ...


class ProductionStateStore:
    """Trusted deployment store; it never creates its parent or accepts test stores."""

    SCHEMA_VERSION = 1
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(self, config: TrustedStoreConfig) -> None:
        if not isinstance(config, TrustedStoreConfig) or not callable(getattr(config.authenticator, "authenticate", None)) or not callable(getattr(config.authenticator, "verify", None)):
            raise BrokerError("trusted production store configuration required")
        self.root = Path(config.root)
        self.filename = config.filename
        self.metadata = config.metadata
        self.authenticator = config.authenticator
        if not self.filename or Path(self.filename).name != self.filename or self.filename in {".", ".."}:
            raise BrokerError("unsafe state filename")
        try:
            self._root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise BrokerError("trusted state root missing") from error
        self._check_root()
        self.path = self.root / self.filename
        self._lock_name = self.filename + ".lock"
        try:
            self._lock_fd = os.open(self._lock_name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self._root_fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise BrokerError("state store already locked") from error
        lock_info = os.fstat(self._lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1 or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077:
            raise BrokerError("unsafe state lock")

    def _check_root(self) -> None:
        try:
            info = os.lstat(self.root)
        except OSError as error:
            raise BrokerError("trusted state root missing") from error
        if not stat.S_ISDIR(info.st_mode) or info.st_nlink < 2 or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise BrokerError("unsafe state root")

    def load(self) -> dict[str, object]:
        try:
            fd = os.open(self.filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._root_fd)
            info = os.fstat(fd)
        except OSError as error:
            raise BrokerError("state store missing") from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > self.MAX_BYTES:
            os.close(fd)
            raise BrokerError("unsafe state file")
        try:
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(self.MAX_BYTES + 1)
            if len(raw) > self.MAX_BYTES:
                raise BrokerError("state file too large")
            envelope = json.loads(raw)
        except (OSError, ValueError) as error:
            raise BrokerError("state store unavailable") from error
        if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "metadata", "payload", "integrity"}:
            raise BrokerError("invalid state envelope")
        if envelope["schema_version"] != self.SCHEMA_VERSION or not isinstance(envelope["metadata"], dict) or not isinstance(envelope["payload"], dict) or not isinstance(envelope["integrity"], str):
            raise BrokerError("invalid state envelope")
        unsigned = {"schema_version": envelope["schema_version"], "metadata": envelope["metadata"], "payload": envelope["payload"]}
        payload = self._canonical(unsigned)
        if not self.authenticator.verify(payload, envelope["integrity"]):
            raise BrokerError("state integrity failure")
        trusted = asdict(self.metadata)
        stored = envelope["metadata"]
        stable_match = isinstance(stored, dict) and all(stored.get(key) == trusted[key] for key in trusted if key != "broker_boot")
        if envelope["payload"].get("schema_version") != self.SCHEMA_VERSION or not stable_match or envelope["payload"].get("metadata") != envelope["metadata"]:
            raise BrokerError("state metadata failure")
        return envelope["payload"]

    def save(self, state: dict[str, object], *, exclusive: bool = False) -> None:
        metadata = state.get("metadata")
        if not isinstance(metadata, dict) or metadata != asdict(self.metadata) or state.get("schema_version") != self.SCHEMA_VERSION:
            raise BrokerError("invalid state metadata")
        unsigned = {"schema_version": self.SCHEMA_VERSION, "metadata": metadata, "payload": state}
        payload = self._canonical(unsigned)
        envelope = {
            "schema_version": self.SCHEMA_VERSION,
            "metadata": metadata,
            "payload": state,
            "integrity": self.authenticator.authenticate(payload),
        }
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > self.MAX_BYTES:
            raise BrokerError("state exceeds size limit")
        if exclusive:
            try:
                fd = os.open(self.filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._root_fd)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise BrokerError("unsafe bootstrap file")
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
                os.fsync(self._root_fd)
                return
            except FileExistsError as error:
                raise BrokerError("state already bootstrapped") from error
        temporary_name = f".{self.filename}.{secrets.token_urlsafe(12)}.tmp"
        try:
            fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._root_fd)
        except OSError as error:
            raise BrokerError("temporary state creation failed") from error
        try:
            temporary_info = os.fstat(fd)
            if not stat.S_ISREG(temporary_info.st_mode) or temporary_info.st_nlink != 1 or temporary_info.st_uid != os.getuid():
                raise BrokerError("unsafe temporary state file")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary_name, self.filename, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            os.fsync(self._root_fd)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass

    def close(self) -> None:
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        os.close(self._root_fd)

    @classmethod
    def bootstrap(cls, config: TrustedStoreConfig, state: dict[str, object]) -> None:
        """Offline create-once bootstrap; normal startup never creates state."""
        expected = {
            "schema_version": cls.SCHEMA_VERSION,
            "metadata": asdict(config.metadata),
            "requests": {}, "receipts": {}, "idempotency": {}, "tombstones": {},
            "nonces": [], "inflight": [], "quarantine": [],
        }
        if state != expected:
            raise BrokerError("bootstrap must be canonical empty state")
        store = cls(config)
        try:
            if os.path.lexists(store.path):
                raise BrokerError("state already bootstrapped")
            store.save(state, exclusive=True)
        finally:
            store.close()

    @staticmethod
    def _canonical(value: dict[str, object]) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


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


class _BrokerCapability:
    pass


class _BrokerCore:
    """Shared broker state machine; only factories are production entry points."""

    TTL_SECONDS = 60.0
    MAX_PENDING = 1024
    MAX_NONCES = 2048
    MAX_TOMBSTONES = 2048
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
        capability: object,
        request_id_factory: Callable[[], str] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(capability, _BrokerCapability):
            raise BrokerError("private broker capability required")
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
        self._tombstones: dict[str, str] = {}
        self._nonces: dict[str, float] = {}
        self._inflight: set[tuple[str, ...]] = set()
        self._quarantine: set[tuple[str, ...]] = set()
        self._owned = metadata
        self._last_now = self._read_clock()
        self._lock = RLock()
        try:
            self._restore(self._store.load())
        except BrokerError:
            raise
        except Exception as error:
            raise BrokerError("invalid persisted state") from error

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
            self._compact_retention()
            existing = self._idempotency.get(binding.idempotency)
            if existing:
                existing_id, existing_digest = existing
                if existing_digest != binding.digest():
                    raise BrokerError("idempotency binding conflict")
                return self._requests[existing_id]
            if binding.idempotency in self._tombstones:
                raise BrokerError("idempotency replay tombstone")
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
            self._requests[request_id] = replace(current, state=BrokerState.CONFIRMED, authorization=(payload, attestation))
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
            if len(self._quarantine) >= self.MAX_PENDING:
                raise BrokerError("quarantine capacity exhausted")
            self._inflight.add(target)
            started = PreparedRequest(
                request_id=request.request_id,
                nonce=request.nonce,
                binding=request.binding,
                expires_at=request.expires_at,
                state=BrokerState.ATTEMPT_STARTED,
                authorization=request.authorization,
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

    def _compact_retention(self) -> None:
        terminal = [
            request for request in self._requests.values()
            if request.state in {BrokerState.COMPLETED, BrokerState.DRIFTED, BrokerState.EXPIRED}
        ]
        for request in terminal[: max(0, len(terminal) - self.MAX_PENDING)]:
            if len(self._tombstones) >= self.MAX_TOMBSTONES:
                raise BrokerError("tombstone capacity exhausted")
            self._tombstones[request.binding.idempotency] = request.binding.digest()
            self._requests.pop(request.request_id, None)
            self._receipts.pop(request.request_id, None)
            self._idempotency.pop(request.binding.idempotency, None)
            self._nonces.pop(request.nonce, None)

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
                        "authorization": list(request.authorization) if request.authorization else None,
                    }
                    for request_id, request in self._requests.items()
                },
                "receipts": {
                    request_id: {"request_id": receipt.request_id, "state": receipt.state.value, "detail": receipt.detail}
                    for request_id, receipt in self._receipts.items()
                },
                "idempotency": self._idempotency,
                "tombstones": self._tombstones,
                "nonces": sorted(self._nonces),
                "schema_version": 1,
                "metadata": asdict(self._owned),
                "inflight": [list(target) for target in self._inflight],
                "quarantine": [list(target) for target in self._quarantine],
            }
        )

    def _restore(self, state: dict[str, object]) -> None:
        if not state:
            return
        required = {"schema_version", "metadata", "requests", "receipts", "idempotency", "tombstones", "nonces", "inflight", "quarantine"}
        stored_metadata = state.get("metadata")
        current_metadata = asdict(self._owned)
        boot_changed = isinstance(stored_metadata, dict) and stored_metadata != current_metadata and all(
            stored_metadata.get(key) == current_metadata[key]
            for key in current_metadata if key != "broker_boot"
        )
        if set(state) != required or state.get("schema_version") != 1 or (stored_metadata != current_metadata and not boot_changed):
            raise BrokerError("invalid persisted state")
        for request_id, raw in dict(state.get("requests", {})).items():
            item = dict(raw)
            if set(item) != {"request_id", "nonce", "binding", "expires_at", "state", "authorization"}:
                raise BrokerError("invalid persisted request")
            if not isinstance(request_id, str) or not isinstance(item["request_id"], str) or request_id != item["request_id"] or not isinstance(item["nonce"], str) or len(item["nonce"]) > self.MAX_IDENTIFIER or not isinstance(item["binding"], dict) or set(item["binding"]) != set(BrokerBinding.__dataclass_fields__) or not isinstance(item["expires_at"], (int, float)) or not math.isfinite(item["expires_at"]):
                raise BrokerError("invalid persisted request")
            binding = BrokerBinding(**item["binding"])
            self._validate_binding(binding)
            if any(getattr(binding, key) != stored_metadata[key] for key in ("provider", "tenant", "policy", "session", "broker_boot")):
                if not (boot_changed and binding.broker_boot == stored_metadata["broker_boot"] and all(getattr(binding, key) == stored_metadata[key] for key in ("provider", "tenant", "policy", "session"))):
                    raise BrokerError("persisted binding metadata mismatch")
            original_state = BrokerState(item["state"])
            original_request = PreparedRequest(item["request_id"], item["nonce"], binding, float(item["expires_at"]), original_state)
            authorization = item.get("authorization")
            if item["state"] == BrokerState.CONFIRMED.value and (
                not isinstance(authorization, list) or len(authorization) != 2 or not all(isinstance(value, str) for value in authorization)
                or not self._signer.verify(authorization[1], authorization[0])
            ):
                raise BrokerError("confirmed authorization invalid")
            if item["state"] in {BrokerState.ATTEMPT_STARTED.value, BrokerState.UNKNOWN.value, BrokerState.APPLIED_UNVERIFIED.value, BrokerState.COMPLETED.value, BrokerState.DRIFTED.value, BrokerState.EXPIRED.value} and authorization is not None:
                if not isinstance(authorization, list) or len(authorization) != 2 or not all(isinstance(value, str) for value in authorization) or not self._signer.verify(authorization[1], authorization[0]):
                    raise BrokerError("confirmed authorization invalid")
            elif authorization is not None:
                raise BrokerError("unexpected persisted authorization")
            if authorization is not None and (len(authorization[0]) > self.ATTESTATION_MAX or not secrets.compare_digest(authorization[0], self._attestation_payload(original_request))):
                raise BrokerError("persisted authorization binding mismatch")
            restored_state = original_state
            authorization_value = tuple(authorization) if authorization else None
            if boot_changed and restored_state in {BrokerState.PREPARED, BrokerState.CONFIRMED}:
                restored_state = BrokerState.EXPIRED
                authorization_value = None
            if boot_changed and restored_state is BrokerState.ATTEMPT_STARTED:
                restored_state = BrokerState.UNKNOWN
            self._requests[request_id] = PreparedRequest(
                request_id=item["request_id"], nonce=item["nonce"],
                binding=binding,
                expires_at=float(item["expires_at"]), state=restored_state,
                authorization=authorization_value,
            )
        for request_id, raw in dict(state.get("receipts", {})).items():
            item = dict(raw)
            self._receipts[request_id] = BrokerReceipt(
                request_id=item["request_id"], state=BrokerState(item["state"]), detail=item["detail"]
            )
        self._idempotency = {
            key: (value[0], value[1]) for key, value in dict(state.get("idempotency", {})).items()
        }
        self._tombstones = {key: value for key, value in dict(state.get("tombstones", {})).items()}
        self._nonces = {
            nonce: float(index) for index, nonce in enumerate(state.get("nonces", []))
        }
        self._inflight = {tuple(target) for target in state.get("inflight", [])}
        self._quarantine = {tuple(target) for target in state.get("quarantine", [])}
        converted_unknown: list[PreparedRequest] = []
        if boot_changed:
            for request in self._requests.values():
                if request.state is BrokerState.UNKNOWN:
                    converted_unknown.append(request)
            for request in converted_unknown:
                self._inflight.discard(self._target(request.binding))
                self._quarantine.add(self._target(request.binding))
                self._receipts[request.request_id] = BrokerReceipt(request.request_id, BrokerState.UNKNOWN, "boot changed")
            for key, (request_id, _digest) in list(self._idempotency.items()):
                request = self._requests.get(request_id)
                if request is not None and request.state is BrokerState.EXPIRED:
                    self._idempotency.pop(key, None)
        if any(request_id != request.request_id for request_id, request in self._requests.items()):
            raise BrokerError("inconsistent persisted request")
        for request_id, receipt in self._receipts.items():
            if request_id not in self._requests or self._requests[request_id].state is not receipt.state:
                raise BrokerError("inconsistent persisted receipt")
        for key, (request_id, digest) in self._idempotency.items():
            request = self._requests.get(request_id)
            if request is None or request.binding.idempotency != key or request.binding.digest() != digest:
                raise BrokerError("inconsistent persisted idempotency")
        for request_id, request in self._requests.items():
            if request.state in {BrokerState.PREPARED, BrokerState.CONFIRMED, BrokerState.ATTEMPT_STARTED} and request.nonce not in self._nonces:
                raise BrokerError("inconsistent persisted nonce")
            if request.state is BrokerState.CONFIRMED and request.authorization is None:
                raise BrokerError("confirmed authorization missing")
        if any(self._target(request.binding) not in self._inflight for request in self._requests.values() if request.state is BrokerState.ATTEMPT_STARTED):
            raise BrokerError("inconsistent inflight state")
        if any(self._target(request.binding) not in self._quarantine for request in self._requests.values() if request.state in {BrokerState.UNKNOWN, BrokerState.APPLIED_UNVERIFIED}):
            raise BrokerError("inconsistent quarantine state")

    @staticmethod
    def _validate_metadata(metadata: BrokerMetadata) -> None:
        if not isinstance(metadata, BrokerMetadata) or any(
            not isinstance(value, str) or not value or len(value) > _BrokerCore.MAX_IDENTIFIER
            or value.lower() in {"default", "placeholder", "changeme", "example"}
            for value in asdict(metadata).values()
        ):
            raise BrokerError("immutable broker metadata required")
        if metadata.policy not in {"comment-v1"}:
            raise BrokerError("metadata policy not allowed")

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
        if not isinstance(value, str) or not 16 <= len(value) <= _BrokerCore.MAX_IDENTIFIER or not value.isascii():
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


def create_production_broker(
    config: TrustedStoreConfig,
    verifier: AttestationVerifier,
    provider: Provider,
    clock: Callable[[], float],
) -> _BrokerCore:
    """Construct the production broker with its trusted store; no store injection."""
    store = ProductionStateStore(config)
    try:
        return _BrokerCore(clock=clock, signer=verifier, provider=provider, metadata=config.metadata, store=store, capability=_BrokerCapability())
    except Exception:
        store.close()
        raise


def create_test_broker(
    *,
    metadata: BrokerMetadata,
    verifier: AttestationVerifier,
    provider: Provider,
    clock: Callable[[], float],
    store: MemoryStateStore | JsonStateStore,
    request_id_factory: Callable[[], str] | None = None,
    nonce_factory: Callable[[], str] | None = None,
) -> _BrokerCore:
    """Explicit test-only construction with deterministic isolated stores."""
    if not isinstance(store, (MemoryStateStore, JsonStateStore)):
        raise BrokerError("test store required")
    try:
        return _BrokerCore(
            clock=clock, signer=verifier, provider=provider, metadata=metadata, store=store, capability=_BrokerCapability(),
            request_id_factory=request_id_factory, nonce_factory=nonce_factory,
        )
    except Exception:
        store.close()
        raise
