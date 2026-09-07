#!/usr/bin/env python3
"""Normalize, validate and present canonical Plane title icons.

The registry is the single machine-readable source of truth. Unicode v1 remains
part of persisted Plane titles; Tabler icon names are presentation metadata.
This module is deterministic, provider-independent and performs no mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

REGISTRY_PATH = Path(__file__).parent / "assets" / "icon-registry.v1.json"


def _validate_registry(registry: dict[str, Any]) -> dict[str, Any]:
    if registry.get("contract_version") != 1:
        raise RuntimeError("icon registry contract_version must equal 1")
    if registry.get("presentation_contract_version") != 2:
        raise RuntimeError("icon registry presentation_contract_version must equal 2")
    icons = registry.get("icons")
    if not isinstance(icons, list) or not icons:
        raise RuntimeError("icon registry icons must be a non-empty list")
    required = {
        "semantic_icon_id", "kind", "machine_value", "unicode_token_v1", "tabler_icon",
        "label_pt_br", "fallback_text", "description_pt_br",
    }
    semantic_ids: list[str] = []
    bindings: list[tuple[str, str]] = []
    allowed_kinds = {"type", "qualifier", "ui", "lifecycle", "compliance"}
    for index, item in enumerate(icons):
        if not isinstance(item, dict) or not required.issubset(item):
            raise RuntimeError(f"icon registry entry {index} is incomplete")
        kind = str(item["kind"])
        machine_value = str(item["machine_value"])
        if kind not in allowed_kinds:
            raise RuntimeError(f"icon registry entry {index} has unsupported kind: {kind}")
        if kind in {"type", "qualifier"} and not item["unicode_token_v1"]:
            raise RuntimeError(f"icon registry entry {index} requires a Unicode v1 token")
        semantic_ids.append(str(item["semantic_icon_id"]))
        bindings.append((kind, machine_value))
    if len(semantic_ids) != len(set(semantic_ids)):
        raise RuntimeError("icon registry semantic_icon_id values must be unique")
    if len(bindings) != len(set(bindings)):
        raise RuntimeError("icon registry kind/machine_value bindings must be unique")
    return registry


def _load_registry() -> dict[str, Any]:
    return _validate_registry(json.loads(REGISTRY_PATH.read_text(encoding="utf-8")))


ICON_REGISTRY = _load_registry()
ICON_CONTRACT_VERSION = int(ICON_REGISTRY["contract_version"])
PRESENTATION_CONTRACT_VERSION = int(ICON_REGISTRY["presentation_contract_version"])
REGISTRY_ENTRIES = {
    item["semantic_icon_id"]: item for item in ICON_REGISTRY["icons"]
}
TYPE_ENTRIES = {
    item["machine_value"]: item
    for item in ICON_REGISTRY["icons"]
    if item.get("kind") == "type"
}
QUALIFIER_ENTRIES = {
    item["machine_value"]: item
    for item in ICON_REGISTRY["icons"]
    if item.get("kind") == "qualifier"
}
TYPE_ICONS = {key: item["unicode_token_v1"] for key, item in TYPE_ENTRIES.items()}
QUALIFIER_ICONS = {
    key: item["unicode_token_v1"] for key, item in QUALIFIER_ENTRIES.items()
}
QUALIFIER_TEXT = {"umbrella": "Umbrella", "canary": "CANARY"}
QUALIFIER_CONTEXT_TOKENS = {"umbrella", "canary"}
KNOWN_ICONS = tuple(
    sorted(
        {item["unicode_token_v1"] for item in ICON_REGISTRY["icons"] if item["unicode_token_v1"]},
        key=len,
        reverse=True,
    )
)
CONTEXT_RE = re.compile(r"^\[([^\[\]]+)\]\s+(.+)$", re.DOTALL)


@dataclass(frozen=True)
class TitleValidation:
    valid: bool
    errors: tuple[str, ...]
    expected: str | None


@dataclass(frozen=True)
class FrozenTitleSnapshot:
    contract_version: int
    semantic_icon_id: str
    title_context: str
    title_sha256: str
    title: str


def _type_entry(labels: Sequence[str]) -> dict[str, Any]:
    type_labels = [str(label).strip() for label in labels if str(label).strip().startswith("tipo:")]
    unknown = sorted({label for label in type_labels if label not in TYPE_ENTRIES})
    if unknown:
        raise ValueError(f"[E_TYPE_UNSUPPORTED] unsupported tipo:* label(s): {', '.join(unknown)}")
    recognized = [label for label in type_labels if label in TYPE_ENTRIES]
    if len(recognized) != 1 or len(type_labels) != 1:
        raise ValueError("[E_TYPE_CARDINALITY] expected exactly one supported tipo:* label")
    return TYPE_ENTRIES[recognized[0]]


def _selected_entry(labels: Sequence[str], qualifier: str | None = None) -> dict[str, Any]:
    type_entry = _type_entry(labels)
    if qualifier is None:
        return type_entry
    if qualifier not in QUALIFIER_ENTRIES:
        raise ValueError(f"[E_QUALIFIER] unknown qualifier: {qualifier}")
    return QUALIFIER_ENTRIES[qualifier]


def semantic_icon_id(labels: Sequence[str], qualifier: str | None = None) -> str:
    return str(_selected_entry(labels, qualifier)["semantic_icon_id"])


def presentation_descriptor(
    labels: Sequence[str] | None = None,
    qualifier: str | None = None,
    *,
    semantic_id: str | None = None,
) -> dict[str, Any]:
    if semantic_id:
        if semantic_id not in REGISTRY_ENTRIES:
            raise ValueError(f"[E_ICON_LOOKUP] unknown semantic_icon_id: {semantic_id}")
        entry = REGISTRY_ENTRIES[semantic_id]
    elif labels is not None:
        entry = _selected_entry(labels, qualifier)
    else:
        raise ValueError("[E_ICON_LOOKUP] provide labels/qualifier or semantic_id")
    return {
        "contract_version": ICON_CONTRACT_VERSION,
        "presentation_contract_version": PRESENTATION_CONTRACT_VERSION,
        "semantic_icon_id": entry["semantic_icon_id"],
        "unicode_token_v1": entry["unicode_token_v1"],
        "tabler_icon": entry["tabler_icon"],
        "label_pt_br": entry["label_pt_br"],
        "fallback_text": entry["fallback_text"],
    }


def _strip_known_icon_prefix(title: str) -> str:
    stripped = title.strip()
    for icon in KNOWN_ICONS:
        if stripped.startswith(icon):
            return stripped[len(icon) :].lstrip()
    return stripped


def _looks_like_emoji_token(token: str) -> bool:
    if not token:
        return False
    if any(char in token for char in ("\ufe0e", "\ufe0f", "\u200d", "\u20e3")):
        return True
    for char in token:
        codepoint = ord(char)
        if unicodedata.category(char) == "So" or 0x1F1E6 <= codepoint <= 0x1F1FF:
            return True
    return False


def _starts_with_unicode_symbol(token: str) -> bool:
    return bool(token) and unicodedata.category(token[0]).startswith("S")


def _strip_unknown_leading_icon(title: str) -> str:
    stripped = title.strip()
    if not stripped:
        return stripped
    first, separator, remainder = stripped.partition(" ")
    if separator and _looks_like_emoji_token(first):
        return remainder.lstrip()
    return stripped


def _strip_bracket_prefix(text: str) -> str:
    stripped = text.strip()
    while stripped.startswith("["):
        match = re.match(r"^\[[^\]]+\]\s*(.*)$", stripped, re.DOTALL)
        if not match:
            break
        stripped = match.group(1).strip()
    return stripped


def _validate_context(context: str) -> str:
    value = context.strip()
    if value != context or not value or "[" in value or "]" in value or any(char in value for char in "\r\n"):
        raise ValueError("[E_TITLE_CONTEXT] title context must be non-empty and must not contain brackets")
    if value.lower() in QUALIFIER_CONTEXT_TOKENS:
        raise ValueError("[E_TITLE_CONTEXT_RESERVED] title context cannot be Umbrella/CANARY")
    return value


def _canonical_body(title: str, qualifier: str | None) -> str:
    body = _strip_known_icon_prefix(title)
    first_token = body.partition(" ")[0]
    if _looks_like_emoji_token(first_token):
        raise ValueError("[E_TITLE_GRAPHEME] unrecognized leading Unicode grapheme")
    if _starts_with_unicode_symbol(first_token):
        raise ValueError("[E_TITLE_SYMBOL] unrecognized leading Unicode symbol")

    bracket_tokens: list[str] = []
    while body.startswith("["):
        match = re.match(r"^\[([^\]]+)\]\s*(.*)$", body, re.DOTALL)
        if not match:
            break
        bracket_tokens.append(match.group(1))
        body = match.group(2).strip()
    structural = [token for token in bracket_tokens if token.lower() in QUALIFIER_CONTEXT_TOKENS]
    if structural:
        expected = QUALIFIER_TEXT.get(qualifier or "")
        if len(structural) != 1 or structural[0] != expected:
            raise ValueError("[E_TITLE_QUALIFIER] title structural qualifier conflicts with declared qualifier")

    tokens = body.split()
    if not tokens:
        raise ValueError("[E_TITLE_BODY] title body is empty after normalization")
    trailing = tokens[-1]
    if trailing in KNOWN_ICONS:
        body = " ".join(tokens[:-1]).strip()
        if not body or (body.split() and body.split()[-1] in KNOWN_ICONS):
            raise ValueError("[E_TITLE_TRAILING] multiple trailing icons are not allowed")
    elif _looks_like_emoji_token(trailing) or trailing in {"$", "+"}:
        raise ValueError("[E_TITLE_TRAILING] unrecognized trailing symbol")
    return body


def normalize_title(
    title: str,
    labels: Sequence[str],
    context: str,
    qualifier: str | None = None,
) -> str:
    entry = _selected_entry(labels, qualifier)
    icon = str(entry["unicode_token_v1"])
    normalized_context = _validate_context(context)
    body = _canonical_body(title, qualifier)
    if not body:
        raise ValueError("[E_TITLE_BODY] title body is empty after normalization")
    qualifier_part = f" [{QUALIFIER_TEXT[qualifier]}]" if qualifier else ""
    return f"{icon} [{normalized_context}]{qualifier_part} {body}"


def validate_title(
    title: str,
    labels: Sequence[str],
    context: str,
    qualifier: str | None = None,
) -> TitleValidation:
    errors: list[str] = []
    try:
        entry = _selected_entry(labels, qualifier)
        normalized_context = _validate_context(context)
    except ValueError as exc:
        return TitleValidation(False, (str(exc),), None)

    expected_icon = str(entry["unicode_token_v1"])
    actual = title.strip()
    if title != actual:
        errors.append("[E_TITLE_WHITESPACE] title must not contain leading or trailing whitespace")
    if not actual.startswith(expected_icon):
        first_token = actual.partition(" ")[0]
        if _looks_like_emoji_token(first_token):
            errors.append("[E_TITLE_GRAPHEME] title starts with an unsupported or non-canonical Unicode grapheme")
        else:
            errors.append(f"[E_TITLE_ICON] title must start with canonical icon {expected_icon}")
        return TitleValidation(False, tuple(errors), None)

    remainder = actual[len(expected_icon) :].lstrip()
    match = CONTEXT_RE.match(remainder)
    if not match:
        errors.append("[E_TITLE_SHAPE] title must place one non-empty [context] immediately after the icon")
        try:
            expected = normalize_title(remainder, labels, normalized_context, qualifier)
        except ValueError:
            expected = None
        return TitleValidation(False, tuple(errors), expected)

    actual_context = match.group(1).strip()
    body = match.group(2).strip()
    if actual_context != normalized_context:
        errors.append(f"[E_TITLE_CONTEXT_MISMATCH] title context must equal {normalized_context!r}")
    if actual_context.lower() in QUALIFIER_CONTEXT_TOKENS:
        errors.append("[E_TITLE_CONTEXT_RESERVED] title context cannot be Umbrella/CANARY")

    if qualifier:
        expected_prefix = f"[{QUALIFIER_TEXT[qualifier]}] "
        if not body.startswith(expected_prefix):
            errors.append(f"[E_TITLE_QUALIFIER] title must place {expected_prefix.strip()} after the context")
        else:
            body = body[len(expected_prefix) :].strip()
    elif re.match(r"^\[(?:Umbrella|CANARY)\]\s+", body):
        errors.append("[E_TITLE_QUALIFIER] structural qualifier exists in title but qualifier was not declared")

    first_body_token = body.partition(" ")[0]
    if _starts_with_unicode_symbol(first_body_token):
        errors.append("[E_TITLE_SYMBOL] title outcome must not start with an unrecognized Unicode symbol")

    if not body:
        errors.append("[E_TITLE_BODY] title outcome must not be empty")

    expected = None
    try:
        expected = normalize_title(body, labels, normalized_context, qualifier)
    except ValueError:
        pass
    if expected and actual != expected:
        errors.append("[E_TITLE_CANONICAL] title is not in canonical normalized form")
    return TitleValidation(not errors, tuple(errors), expected)


def freeze_title(
    title: str,
    labels: Sequence[str],
    context: str,
    qualifier: str | None = None,
) -> dict[str, Any]:
    result = validate_title(title, labels, context, qualifier)
    if not result.valid:
        raise ValueError("[E_TITLE_FREEZE] cannot freeze invalid title: " + "; ".join(result.errors))
    exact = title.strip()
    return asdict(FrozenTitleSnapshot(
        contract_version=ICON_CONTRACT_VERSION,
        semantic_icon_id=semantic_icon_id(labels, qualifier),
        title_context=_validate_context(context),
        title_sha256=hashlib.sha256(exact.encode("utf-8")).hexdigest(),
        title=exact,
    ))


def validate_frozen_snapshot(*args: Any) -> list[str]:
    if len(args) not in {4, 5}:
        return ["[E_TITLE_SNAPSHOT] frozen title validation requires snapshot, title, labels and context"]
    if isinstance(args[0], (dict, FrozenTitleSnapshot)):
        snapshot, title, labels, context = args[:4]
        qualifier = args[4] if len(args) == 5 else None
    elif len(args) == 5 and isinstance(args[4], (dict, FrozenTitleSnapshot)):
        title, labels, context, qualifier, snapshot = args
    elif len(args) == 5 and isinstance(args[3], (dict, FrozenTitleSnapshot)):
        title, labels, context, snapshot, qualifier = args
    else:
        title, labels, snapshot, context = args[:4]
        qualifier = args[4] if len(args) == 5 else None
    if isinstance(snapshot, FrozenTitleSnapshot):
        snapshot = asdict(snapshot)
    if not isinstance(snapshot, dict):
        return ["[E_TITLE_SNAPSHOT] frozen_title_snapshot must be an object"]
    expected_semantic_id = semantic_icon_id(labels, qualifier)
    expected_hash = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()
    errors: list[str] = []
    if snapshot.get("contract_version") != ICON_CONTRACT_VERSION:
        errors.append(f"[E_ICON_VERSION] frozen_title_snapshot.contract_version must equal {ICON_CONTRACT_VERSION}")
    if snapshot.get("semantic_icon_id") != expected_semantic_id:
        errors.append(f"[E_ICON_SEMANTIC] frozen_title_snapshot.semantic_icon_id must equal {expected_semantic_id}")
    if snapshot.get("title_context") != context.strip():
        errors.append("[E_TITLE_CONTEXT_DRIFT] frozen_title_snapshot.title_context must match title_context")
    if snapshot.get("title_sha256") != expected_hash:
        errors.append("[E_TITLE_HASH_DRIFT] frozen_title_snapshot.title_sha256 must match the exact UTF-8 title")
    if snapshot.get("title") not in (None, title.strip()):
        errors.append("[E_TITLE_TEXT_DRIFT] frozen_title_snapshot.title must match the exact title")
    return errors


def recontextualize_title(
    title: str,
    labels: Sequence[str],
    snapshot: dict[str, Any] | FrozenTitleSnapshot,
    new_context: str,
    *,
    qualifier: str | None = None,
    allow_material_recontextualization: bool = False,
) -> str:
    if isinstance(snapshot, FrozenTitleSnapshot):
        snapshot = asdict(snapshot)
    if snapshot.get("contract_version") != ICON_CONTRACT_VERSION:
        raise ValueError("[E_ICON_VERSION] frozen snapshot contract version is unsupported")
    if snapshot.get("semantic_icon_id") != semantic_icon_id(labels, qualifier):
        raise ValueError("[E_ICON_SEMANTIC] frozen snapshot semantic_icon_id does not match type/qualifier")
    if snapshot.get("title_sha256") != hashlib.sha256(title.encode("utf-8")).hexdigest():
        raise ValueError("[E_TITLE_HASH_DRIFT] frozen title snapshot does not match the exact title")
    normalized_context = _validate_context(new_context)
    if normalized_context != snapshot.get("title_context") and not allow_material_recontextualization:
        raise ValueError("[E_MATERIAL_RECONTEXT] material recontextualization requires explicit authorization")
    body = _strip_bracket_prefix(_strip_known_icon_prefix(title))
    return normalize_title(body, labels, normalized_context, qualifier)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("normalize", "validate", "presentation"):
        sub = subparsers.add_parser(command)
        if command != "presentation":
            sub.add_argument("--title", required=True)
        sub.add_argument("--type", required=True, dest="type_label")
        sub.add_argument("--context", required=command != "presentation")
        sub.add_argument("--qualifier", choices=sorted(QUALIFIER_ENTRIES))
    subparsers.add_parser("map")
    return parser


def _error_code(message: str) -> str:
    match = re.match(r"^\[([A-Z0-9_]+)\]", message)
    return match.group(1) if match else "E_TITLE_CONTRACT"


def _error_payload(message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": _error_code(message), "message": message}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "map":
            print(json.dumps({"contract_version": ICON_CONTRACT_VERSION, "presentation_contract_version": PRESENTATION_CONTRACT_VERSION, "types": TYPE_ICONS, "qualifiers": QUALIFIER_ICONS}, ensure_ascii=False, sort_keys=True))
            return 0
        labels = [args.type_label]
        if args.command == "presentation":
            print(json.dumps(presentation_descriptor(labels, args.qualifier), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "normalize":
            print(normalize_title(args.title, labels, args.context, args.qualifier))
            return 0
        result = validate_title(args.title, labels, args.context, args.qualifier)
        payload: dict[str, Any] = {"valid": result.valid, "errors": result.errors, "expected": result.expected}
        if not result.valid:
            payload.update(_error_payload(result.errors[0]))
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if result.valid else 1
    except ValueError as exc:
        print(json.dumps(_error_payload(str(exc)), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
