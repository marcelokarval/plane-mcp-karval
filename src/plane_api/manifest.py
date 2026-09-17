"""Immutable, source-derived identity for the Plane mutation contract."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


CONTRACT_VERSION = 2
_REGISTRY_PATH = Path(__file__).with_name("operation_registry.json")
_SOURCE_CONTRACT_PATH = Path(__file__).with_name("source_contract.json")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def registry_manifest_hash(path: str | Path = _REGISTRY_PATH) -> str:
    """Return the stable digest of a decoded registry, independent of formatting."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def validate_persisted_manifest(
    registry_path: str | Path = _REGISTRY_PATH,
    source_contract_path: str | Path = _SOURCE_CONTRACT_PATH,
) -> Mapping[str, Any]:
    """Validate the checked-in manifest against the registry it describes.

    Recomputing a digest at import time is not provenance: a modified registry
    would simply become the new runtime identity.  The source contract is the
    persisted pin, so both its byte hash and every manifest denominator must
    agree with the independently loaded registry before the package starts.
    """
    registry_file = Path(registry_path)
    contract_file = Path(source_contract_path)
    try:
        registry = json.loads(registry_file.read_text(encoding="utf-8"))
        source_contract = json.loads(contract_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Plane registry manifest is unreadable") from exc
    if not isinstance(registry, Mapping) or not isinstance(source_contract, Mapping):
        raise ValueError("Plane registry manifest has an invalid shape")
    if registry.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("registry contract version does not match runtime")

    persisted = source_contract.get("registry_manifest")
    if not isinstance(persisted, Mapping):
        raise ValueError("persisted registry manifest is missing")
    if source_contract.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("persisted contract version does not match runtime")
    if source_contract.get("registry_path") != "src/plane_api/operation_registry.json":
        raise ValueError("persisted registry path does not match runtime")
    if source_contract.get("registry_sha256") != hashlib.sha256(registry_file.read_bytes()).hexdigest():
        raise ValueError("persisted registry byte hash does not match registry")

    operations = registry.get("operations")
    contracts = registry.get("mutation_contracts")
    if not isinstance(operations, list) or not isinstance(contracts, list):
        raise ValueError("registry manifest source collections are invalid")
    operation_actions = [item.get("action") for item in operations if isinstance(item, Mapping)]
    mutation_operations = [item for item in operations if isinstance(item, Mapping) and item.get("mutation") is True]
    mutation_actions = {str(item.get("action")) for item in mutation_operations}
    operation_by_action = {str(item.get("action")): item for item in operations if isinstance(item, Mapping)}
    contract_actions = [item.get("action") for item in contracts if isinstance(item, Mapping)]
    if len(operation_actions) != len(set(operation_actions)):
        raise ValueError("registry manifest contains duplicate operation actions")
    if len(contract_actions) != len(set(contract_actions)):
        raise ValueError("registry manifest contains duplicate mutation contract actions")
    mutation_count = len(mutation_operations)
    if set(contract_actions) != mutation_actions or len(contract_actions) != mutation_count:
        raise ValueError("registry mutation contract action set does not match the mutation operations")
    terminals = {"verified", "verified_absence", "provider_acknowledged"}
    strategies = {"detail_state", "membership_state", "provider_ack"}
    for contract in contracts:
        if not isinstance(contract, Mapping):
            raise ValueError("registry mutation contract has an invalid shape")
        if contract.get("write_policy") != "allow":
            raise ValueError("registry mutation contract policy is not live")
        if contract.get("blocked_reason") is not None:
            raise ValueError("registry mutation contract is blocked")
        if contract.get("expected_terminal") not in terminals:
            raise ValueError("registry mutation contract has an invalid terminal")
        postcondition = contract.get("postcondition")
        if not isinstance(postcondition, Mapping) or postcondition.get("strategy") not in strategies:
            raise ValueError("registry mutation contract has an invalid postcondition strategy")
        if postcondition.get("kind") != postcondition.get("strategy"):
            raise ValueError("registry mutation contract postcondition kind/strategy mismatch")
        get_action = postcondition.get("get_action")
        if get_action is not None and not isinstance(get_action, str):
            raise ValueError("registry mutation contract read action is invalid")
        if postcondition.get("strategy") == "provider_ack" and get_action is not None:
            raise ValueError("provider acknowledgement must not carry a read action")
        if postcondition.get("strategy") != "provider_ack":
            read_operation = operation_by_action.get(str(get_action))
            if not isinstance(read_operation, Mapping) or read_operation.get("method") != "GET":
                raise ValueError("registry mutation contract read action is not a registered GET operation")
            targets = postcondition.get("targets")
            if not isinstance(targets, Mapping):
                raise ValueError("registry mutation contract targets are invalid")
            selector = targets.get("selector")
            if not isinstance(selector, Mapping) or not selector.get("source") or not selector.get("field"):
                raise ValueError("registry mutation contract has no explicit affected-resource selector")
            source = str(selector.get("source"))
            field = str(selector.get("field"))
            if source not in {"path", "payload", "write_response"}:
                raise ValueError("registry mutation contract has an invalid target selector source")
            expected_fields = {
                "path": targets.get("path_fields") or (),
                "payload": targets.get("payload_fields") or (),
                "write_response": targets.get("write_response_fields") or (),
            }[source]
            if list(expected_fields) != [field]:
                raise ValueError("registry mutation contract target selector is not exact")
            if postcondition.get("strategy") == "detail_state":
                schema = read_operation.get("response_schema") or {}
                json_schema = schema.get("json_schema") if isinstance(schema, Mapping) else None
                readable = set((json_schema or {}).get("properties") or {}) if isinstance(json_schema, Mapping) else set()
                effects = set((postcondition.get("detail") or {}).get("effect_fields") or ())
                if not effects <= readable:
                    raise ValueError("registry detail-state effects are not all observable in the GET schema")
                write_operation = operation_by_action.get(str(contract.get("action")))
                write_schema = write_operation.get("request_schema") if isinstance(write_operation, Mapping) else None
                write_json_schema = write_schema.get("json_schema") if isinstance(write_schema, Mapping) else None
                write_properties = write_json_schema.get("properties") if isinstance(write_json_schema, Mapping) else {}
                for field in effects:
                    read_field = (json_schema or {}).get("properties", {}).get(field, {}) if isinstance(json_schema, Mapping) else {}
                    write_field = write_properties.get(field, {}) if isinstance(write_properties, Mapping) else {}
                    read_type = read_field.get("type") if isinstance(read_field, Mapping) else None
                    write_type = write_field.get("type") if isinstance(write_field, Mapping) else None
                    if read_type != write_type and {read_type, write_type} != {"integer", "number"}:
                        raise ValueError("registry detail-state effect schema is not independently comparable")
        if postcondition.get("strategy") == "detail_state" and contract.get("expected_terminal") != "verified":
            raise ValueError("detail state contract has an invalid terminal")
        if postcondition.get("strategy") == "provider_ack" and contract.get("expected_terminal") != "provider_acknowledged":
            raise ValueError("provider acknowledgement contract has an invalid terminal")
        if postcondition.get("strategy") == "membership_state":
            presence = (postcondition.get("membership") or {}).get("target_presence")
            expected_terminal = "verified_absence" if presence == "absent" else "verified"
            if contract.get("expected_terminal") != expected_terminal:
                raise ValueError("membership state contract has an invalid terminal")
    expected = {
        "version": CONTRACT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "contract_hash": hashlib.sha256(_canonical_json(registry).encode("utf-8")).hexdigest(),
        "mutation_count": len(mutation_operations),
        "operation_count": len(operations),
        "http_operation_count": int(registry.get("http_operation_count", -1)),
        "method_counts": dict(registry.get("method_counts") or {}),
    }
    for name, value in expected.items():
        if persisted.get(name) != value:
            raise ValueError(f"persisted registry manifest {name} does not match registry")
    inventory = registry.get("mutation_inventory")
    if not isinstance(inventory, Mapping):
        raise ValueError("registry mutation inventory is missing")
    if inventory.get("denominator") != mutation_count or inventory.get("supported_count") != mutation_count or inventory.get("unsupported_count") != 0:
        raise ValueError("registry mutation inventory counts do not match live mutations")
    if set(inventory.get("supported_actions") or ()) != mutation_actions or inventory.get("unsupported_actions") != []:
        raise ValueError("registry mutation inventory action sets do not match")
    if inventory.get("contract_policy_counts") != {"allow": mutation_count, "deny": 0}:
        raise ValueError("registry mutation policy counts do not match")
    if sum((inventory.get("terminal_counts") or {}).values()) != mutation_count or sum((inventory.get("strategy_counts") or {}).values()) != mutation_count:
        raise ValueError("registry persisted computed counts do not sum to the mutation denominator")
    computed = source_contract.get("computed_counts")
    if not isinstance(computed, Mapping) or computed.get("policy") != inventory.get("contract_policy_counts") or computed.get("terminal") != inventory.get("terminal_counts") or computed.get("strategy") != inventory.get("strategy_counts") or computed.get("sum") != mutation_count:
        raise ValueError("persisted computed contract counts do not match registry")
    counts = source_contract.get("counts")
    if not isinstance(counts, Mapping):
        raise ValueError("persisted registry counts are missing")
    for name, value in {
        "operations": expected["operation_count"],
        "http_operations": expected["http_operation_count"],
        "mutations": expected["mutation_count"],
    }.items():
        if counts.get(name) != value:
            raise ValueError(f"persisted registry count {name} does not match manifest")
    persisted_contracts = source_contract.get("mutation_contracts")
    if not isinstance(persisted_contracts, list) or {item.get("action") for item in persisted_contracts if isinstance(item, Mapping)} != mutation_actions:
        raise ValueError("persisted mutation contract action set does not match registry")
    by_action = {item.get("action"): item for item in persisted_contracts if isinstance(item, Mapping)}
    for contract in contracts:
        action = contract.get("action")
        pinned = by_action.get(action)
        if not isinstance(pinned, Mapping) or pinned.get("contract_sha256") != hashlib.sha256(_canonical_json(contract).encode("utf-8")).hexdigest():
            raise ValueError(f"persisted mutation contract hash does not match registry: {action}")
    return MappingProxyType(dict(expected))


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


_PAYLOAD = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
_PERSISTED_MANIFEST = validate_persisted_manifest()
CONTRACT_HASH = str(_PERSISTED_MANIFEST["contract_hash"])
MUTATION_COUNT = int(_PERSISTED_MANIFEST["mutation_count"])


@dataclass(frozen=True, slots=True)
class RegistryManifest:
    version: int
    contract_hash: str
    mutation_count: int
    operation_count: int
    metadata: Mapping[str, Any]


MANIFEST = RegistryManifest(
    version=CONTRACT_VERSION,
    contract_hash=CONTRACT_HASH,
    mutation_count=MUTATION_COUNT,
    operation_count=len(_PAYLOAD.get("operations", [])),
    metadata=MappingProxyType(
        {
            "contract_version": CONTRACT_VERSION,
            "contract_hash": CONTRACT_HASH,
            "mutation_count": MUTATION_COUNT,
            "operation_count": int(_PERSISTED_MANIFEST["operation_count"]),
            "http_operation_count": int(_PERSISTED_MANIFEST["http_operation_count"]),
        }
    ),
)

# Public aliases make the identity explicit to callers without exposing the
# mutable JSON registry object.
CONTRACT_MANIFEST = MANIFEST
REGISTRY_MANIFEST = MANIFEST
REGISTRY_CONTRACT_VERSION = CONTRACT_VERSION
REGISTRY_CONTRACT_HASH = CONTRACT_HASH
MUTATION_DENOMINATOR = MUTATION_COUNT
MUTATION_CONTRACT_COUNT = MUTATION_COUNT


def manifest_metadata() -> Mapping[str, Any]:
    """Return immutable metadata used to bind registry cursors."""
    return MANIFEST.metadata
