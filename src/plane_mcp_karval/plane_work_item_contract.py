#!/usr/bin/env python3
"""Validate and render the governed Plane work-item lifecycle contract."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from plane_title_icons import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    ICON_CONTRACT_VERSION,
    PRESENTATION_CONTRACT_VERSION,
    semantic_icon_id,
    validate_frozen_snapshot,
    validate_title,
)

CONTRACT_VERSION = 1
PHASES = {"START", "PROGRESS", "BLOCKED", "REVIEW", "FINISH"}
ACTIVE_STATES = {"in_progress", "review", "review_qa", "qa", "done", "completed"}
REVIEW_STATES = {"review", "review_qa", "qa", "done", "completed"}
DONE_STATES = {"done", "completed"}
GATED_STATES = ACTIVE_STATES | {"ready"}
ALLOWED_STATES = {"backlog", "draft", "ready", "in_progress", "blocked", "review", "review_qa", "qa", "done", "completed", "canceled", "cancelled"}
UNIT_STATUSES = {"pending", "in_progress", "blocked", "delivered", "cancelled"}
WAVE_STATUSES = {"pending", "in_progress", "blocked", "completed", "cancelled"}
TIMESTAMP_SOURCES = {"provider", "runtime", "artifact", "reconstructed"}
PHASE_STATE_AFTER = {
    "START": {"in_progress"},
    "PROGRESS": {"in_progress"},
    "BLOCKED": {"blocked"},
    "REVIEW": {"review", "review_qa", "qa"},
    "FINISH": {"done", "completed"},
}
RUNTIMES = {"hermes", "codex", "opencode", "opendesign"}
SOURCES = {"cli", "tui", "telegram", "api", "scheduler", "codex", "external"}
HANDLERS = {"default/thor", "thor/default", "codex"}
GOVERNORS = {"default/thor", "thor/default"}
REASONING_TRACE_POLICIES = {
    "metadata_only_no_chain_of_thought",
    "external_runtime_policy",
    "not_exposed_to_runtime",
}
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{5,127}$")
STACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{5,199}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
HTTPS_RE = re.compile(r"^https://[^\s/]+/.+")
PROOF_RE = re.compile(
    r"^(?:test|cmd|api|artifact|runtime|review|static|provider-readback):\S"
)
SENSITIVE_RE = re.compile(
    r"(?i)(?:\.env(?:\b|/)|auth\.json|api[_ -]?key|password|passwd|secret\s*[:=]|"
    r"bearer\s+[A-Za-z0-9._~-]+|token\s*[:=]|postgres(?:ql)?://|/home/[^/]+/)"
)
INVALID_PLACEHOLDERS = {"", "none", "unknown", "n/a", "tbd", "unset"}
DISPOSITION_FIELDS = ("assignee", "module", "cycle", "estimate", "parent")
REQUIRED_COMMENT_TEXT = {
    "unit": 12,
    "requested": 24,
    "delivered": 24,
    "next_action": 24,
    "owner": 3,
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_non_placeholder(value: Any) -> bool:
    return _text(value).lower() not in INVALID_PLACEHOLDERS


def _is_proof_ref(value: Any) -> bool:
    return bool(PROOF_RE.match(_text(value)))


def _validate_proof_list(where: str, values: Any, *, required: bool = True) -> list[str]:
    if not isinstance(values, list) or (required and not values):
        return [f"{where} must contain at least one proof-class reference"]
    invalid = [item for item in values if not _is_proof_ref(item)]
    if invalid:
        return [
            f"{where} entries require a proof-class prefix: "
            "test:, cmd:, api:, artifact:, runtime:, review:, static:, or provider-readback:"
        ]
    return []


def _code_needle(key: str, value: str, output_format: str) -> str:
    literal = f"{key}={value}"
    return f"<code>{html.escape(literal)}</code>" if output_format == "html" else f"`{literal}`"


def _validate_trace_body(body: Any, output_format: Any, session_id: str, task_stack_id: str, where: str) -> list[str]:
    errors: list[str] = []
    fmt = _text(output_format).lower()
    content = _text(body)
    if fmt not in {"html", "markdown"}:
        return [f"{where}.body_format must be html or markdown"]
    for key, value in (("session_id", session_id), ("task_stack_id", task_stack_id)):
        needle = _code_needle(key, value, fmt)
        if needle not in content:
            errors.append(f"{where}.body must contain code-formatted {key}: {needle}")
    return errors


def _body_literal(value: Any, output_format: str) -> str:
    literal = _text(value)
    return html.escape(literal) if output_format == "html" else literal


def _validate_comment_body_values(
    comment: dict[str, Any],
    body: str,
    output_format: str,
    where: str,
) -> list[str]:
    errors: list[str] = []
    for field in ("unit", "requested", "delivered", "next_action", "owner"):
        value = _body_literal(comment.get(field), output_format)
        if value and value not in body:
            errors.append(f"{where}.body must contain {field}")
    agent_context = comment.get("agent_context")
    if isinstance(agent_context, dict):
        for field in (
            "agent_runtime",
            "model",
            "reasoning_effort",
            "reasoning_trace_policy",
            "tool_surface",
        ):
            value = _body_literal(agent_context.get(field), output_format)
            needle = _code_needle(field, value, output_format)
            if value and needle not in body:
                errors.append(f"{where}.body must contain code-formatted agent_context.{field}")
    for field in ("surfaces", "evidence", "decisions", "blockers", "residuals"):
        values = comment.get(field)
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            literal = _body_literal(value, output_format)
            if literal and literal not in body:
                errors.append(f"{where}.body must contain {field}[{index}]")
    if _text(comment.get("phase")) == "BLOCKED":
        for field in ("impact", "unblock_condition"):
            value = _body_literal(comment.get(field), output_format)
            if value and value not in body:
                errors.append(f"{where}.body must contain {field}")
    return errors


def _has_provider_evidence(values: Any) -> bool:
    return isinstance(values, list) and any(
        _text(item).startswith(("api:", "provider-readback:")) for item in values
    )


def _validate_disposition(name: str, value: Any) -> list[str]:
    if not isinstance(value, dict):
        return [f"work_item.{name} must be an explicit disposition object"]
    status = _text(value.get("status"))
    if status == "assigned":
        if not _is_non_placeholder(value.get("value")):
            return [f"work_item.{name} assigned disposition requires a concrete value"]
        evidence = value.get("evidence")
        errors = _validate_proof_list(f"work_item.{name}.evidence", evidence)
        if not _has_provider_evidence(evidence):
            errors.append(f"work_item.{name}.evidence requires provider evidence via api: or provider-readback:")
        return errors
    if status in {"not_applicable", "unassigned", "blocked"}:
        reason = _text(value.get("reason"))
        if len(reason) < 12:
            return [f"work_item.{name} {status} disposition requires a reason of at least 12 characters"]
        return []
    return [f"work_item.{name} disposition status must be assigned, not_applicable, unassigned, or blocked"]


def _valid_iso_date(value: Any) -> bool:
    try:
        date.fromisoformat(_text(value))
    except ValueError:
        return False
    return True


def _parse_iso_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _valid_iso_datetime(value: Any) -> bool:
    return _parse_iso_datetime(value) is not None


def _normalize_state(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _text(value).lower()).strip("_")


def _validate_comment(
    comment: Any,
    index: int,
    session_id: str,
    task_stack_id: str,
    *,
    require_body: bool = True,
) -> list[str]:
    where = f"comments[{index}]"
    if not isinstance(comment, dict):
        return [f"{where} must be an object"]
    errors: list[str] = []
    serialized_comment = json.dumps(comment, ensure_ascii=False, sort_keys=True)
    if SENSITIVE_RE.search(serialized_comment):
        errors.append(f"{where} contains a sensitive-looking value or absolute home path")
    phase = _text(comment.get("phase"))
    if phase not in PHASES:
        errors.append(f"{where}.phase must be one of {sorted(PHASES)}")
    if _text(comment.get("session_id")) != session_id:
        errors.append(f"{where}.session_id must match the work contract")
    if _text(comment.get("task_stack_id")) != task_stack_id:
        errors.append(f"{where}.task_stack_id must match the work contract")
    occurred_at = _text(comment.get("occurred_at"))
    if not _valid_iso_datetime(occurred_at):
        errors.append(f"{where}.occurred_at must be a timezone-aware ISO-8601 timestamp")
    timestamp_source = _text(comment.get("timestamp_source"))
    if timestamp_source not in TIMESTAMP_SOURCES:
        errors.append(f"{where}.timestamp_source must be one of {sorted(TIMESTAMP_SOURCES)}")
    if not _is_non_placeholder(comment.get("unit_id")):
        errors.append(f"{where}.unit_id is required")
    for field, minimum in REQUIRED_COMMENT_TEXT.items():
        if len(_text(comment.get(field))) < minimum:
            errors.append(f"{where}.{field} minimum length is {minimum}")
    agent_context = comment.get("agent_context")
    if not isinstance(agent_context, dict):
        errors.append(f"{where}.agent_context is required")
    else:
        for field in (
            "agent_runtime",
            "model",
            "reasoning_effort",
            "reasoning_trace_policy",
            "tool_surface",
        ):
            if not _is_non_placeholder(agent_context.get(field)):
                errors.append(f"{where}.agent_context.{field} is required")
        agent_runtime = _text(agent_context.get("agent_runtime"))
        if agent_runtime and agent_runtime not in RUNTIMES:
            errors.append(f"{where}.agent_context.agent_runtime must be one of {sorted(RUNTIMES)}")
        reasoning_policy = _text(agent_context.get("reasoning_trace_policy"))
        if reasoning_policy and reasoning_policy not in REASONING_TRACE_POLICIES:
            errors.append(
                f"{where}.agent_context.reasoning_trace_policy must be one of "
                f"{sorted(REASONING_TRACE_POLICIES)}"
            )
    for field in ("state_before", "state_after"):
        if not _is_non_placeholder(comment.get(field)):
            errors.append(f"{where}.{field} is required")
    state_before = _normalize_state(comment.get("state_before"))
    state_after = _normalize_state(comment.get("state_after"))
    if phase == "START" and state_before != "ready":
        errors.append("START.state_before must be ready")
    allowed_after = PHASE_STATE_AFTER.get(phase)
    if allowed_after is not None and state_after not in allowed_after:
        errors.append(
            f"{where} has an invalid semantic state transition for {phase}; "
            f"state_after must be one of {sorted(allowed_after)}"
        )
    surfaces = comment.get("surfaces")
    if not isinstance(surfaces, list) or not any(_is_non_placeholder(item) for item in surfaces):
        errors.append(f"{where}.surfaces must contain at least one concrete entry")
    errors.extend(_validate_proof_list(f"{where}.evidence", comment.get("evidence")))
    for field in ("decisions", "blockers", "residuals"):
        if not isinstance(comment.get(field), list):
            errors.append(f"{where}.{field} must be a list, even when empty")
    if require_body:
        errors.extend(
            _validate_trace_body(
                comment.get("body"),
                comment.get("body_format"),
                session_id,
                task_stack_id,
                where,
            )
        )
        body = _text(comment.get("body"))
        rich_tokens = ("Identidade", "Requested", "evid", "Decis", "Próxima")
        if not all(token.lower() in body.lower() for token in rich_tokens):
            errors.append(f"{where}.body must contain rich lifecycle sections")
        body_format = _text(comment.get("body_format")).lower()
        errors.extend(_validate_comment_body_values(comment, body, body_format, where))
        occurred_needle = _code_needle("occurred_at", occurred_at, body_format)
        if occurred_needle not in body:
            errors.append(f"{where}.body must contain code-formatted occurred_at")
        if _code_needle("timestamp_source", timestamp_source, body_format) not in body:
            errors.append(f"{where}.body must contain code-formatted timestamp_source")
        wave_id = _text(comment.get("wave_id"))
        if wave_id and _code_needle("wave_id", wave_id, body_format) not in body:
            errors.append(f"{where}.body must contain code-formatted wave_id")
        if phase != "START" and _code_needle("unit_id", _text(comment.get("unit_id")), body_format) not in body:
            errors.append(f"{where}.body must contain code-formatted unit_id")
    if phase == "BLOCKED":
        blockers = comment.get("blockers")
        if not isinstance(blockers, list) or not any(_is_non_placeholder(item) for item in blockers):
            errors.append(f"{where} BLOCKED packet requires a concrete blocker")
        if len(_text(comment.get("impact"))) < 12:
            errors.append(f"{where} BLOCKED.impact minimum length is 12")
        if len(_text(comment.get("unblock_condition"))) < 12:
            errors.append(f"{where} BLOCKED.unblock_condition minimum length is 12")
    return errors


def validate_comment(comment: Any, *, session_id: str, task_stack_id: str) -> list[str]:
    """Validate one rich lifecycle packet against the shared comment schema."""
    return _validate_comment(comment, 0, session_id, task_stack_id)


def _validate_provider_readback(
    value: Any,
    work_item: dict[str, Any],
    session_id: str,
    task_stack_id: str,
) -> list[str]:
    where = "provider_readback"
    if not isinstance(value, dict):
        return [f"{where} must be a verified provider receipt before Done"]
    errors: list[str] = []
    if _text(value.get("status")) != "verified":
        errors.append(f"{where}.status must be verified")
    if _text(value.get("provider")) != "plane":
        errors.append(f"{where}.provider must be plane")
    if _text(value.get("operation")) != "get_work_item+get_comment":
        errors.append(f"{where}.operation must be get_work_item+get_comment")
    if not _valid_iso_datetime(value.get("checked_at")):
        errors.append(f"{where}.checked_at must be an ISO timestamp with timezone")
    for field in ("work_item_id", "state_id", "finish_comment_id"):
        if not UUID_RE.fullmatch(_text(value.get(field))):
            errors.append(f"{where}.{field} must be a provider UUID")
    if _text(value.get("work_item_id")) != _text(work_item.get("id")):
        errors.append(f"{where}.work_item_id must match work_item.id")
    identifier = _text(work_item.get("identifier"))
    if _text(value.get("identifier")) != identifier:
        errors.append(f"{where}.identifier must match work_item.identifier")
    web_url = _text(value.get("web_url"))
    work_item_id = _text(work_item.get("id"))
    if not HTTPS_RE.fullmatch(web_url) or work_item_id not in web_url:
        errors.append(f"{where}.web_url must be a canonical HTTPS URL bound to work_item.id")
    if _normalize_state(value.get("state")) not in DONE_STATES:
        errors.append(f"{where}.state must prove Done/completed")
    if _text(value.get("state_id")) != _text(work_item.get("provider_state_id")):
        errors.append(f"{where}.state_id must match work_item.provider_state_id")
    finish_comment = value.get("finish_comment")
    if not isinstance(finish_comment, dict):
        errors.append(f"{where}.finish_comment must be a provider comment receipt")
    else:
        if _text(finish_comment.get("id")) != _text(value.get("finish_comment_id")):
            errors.append(f"{where}.finish_comment.id must match finish_comment_id")
        if _text(finish_comment.get("work_item_id")) != _text(value.get("work_item_id")):
            errors.append(f"{where}.finish_comment.work_item_id must match work_item_id")
        if _text(finish_comment.get("phase")) != "FINISH":
            errors.append(f"{where}.finish_comment.phase must be FINISH")
        if _text(finish_comment.get("session_id")) != session_id:
            errors.append(f"{where}.finish_comment.session_id must match the execution session")
        if _text(finish_comment.get("task_stack_id")) != task_stack_id:
            errors.append(f"{where}.finish_comment.task_stack_id must match the task stack")
    evidence = value.get("evidence")
    errors.extend(_validate_proof_list(f"{where}.evidence", evidence))
    if isinstance(evidence, list) and not any(_text(item).startswith("provider-readback:") for item in evidence):
        errors.append(f"{where}.evidence must include a provider-readback: receipt")
    evidence_text = " ".join(_text(item) for item in evidence) if isinstance(evidence, list) else ""
    if _text(value.get("work_item_id")) not in evidence_text or _text(value.get("finish_comment_id")) not in evidence_text:
        errors.append(f"{where}.evidence must name work_item_id and finish_comment_id")
    return errors


def _validate_independent_review(value: Any, handled_by: str, execution_session_id: str) -> list[str]:
    where = "independent_review"
    if not isinstance(value, dict):
        return [f"{where} must contain a passed independent reviewer receipt before Done"]
    errors: list[str] = []
    if _text(value.get("status")) != "passed":
        errors.append(f"{where}.status must be passed")
    reviewer = _text(value.get("reviewer"))
    if not _is_non_placeholder(reviewer) or reviewer.casefold() == handled_by.casefold():
        errors.append(f"{where}.reviewer must be distinct from handled_by")
    reviewer_session_id = _text(value.get("reviewer_session_id"))
    if not SESSION_RE.fullmatch(reviewer_session_id):
        errors.append(f"{where}.reviewer_session_id has an invalid shape")
    elif reviewer_session_id == execution_session_id:
        errors.append(f"{where}.reviewer_session_id must be distinct from the execution session")
    evidence = value.get("evidence")
    errors.extend(_validate_proof_list(f"{where}.evidence", evidence))
    if isinstance(evidence, list) and not any(_text(item).startswith("review:") for item in evidence):
        errors.append(f"{where}.evidence must include a review: receipt")
    return errors


def _validate_timing(value: Any, state: str) -> list[str]:
    if not isinstance(value, dict):
        return ["timing must be an object"]
    errors: list[str] = []
    mode = _text(value.get("registration_mode"))
    if mode not in {"prospective", "retroactive"}:
        errors.append("timing.registration_mode must be prospective or retroactive")
    parsed: dict[str, datetime | None] = {}
    for field in ("issue_opened_at", "implementation_started_at", "review_started_at", "closed_at"):
        raw = value.get(field)
        required = field == "issue_opened_at" or (
            field == "implementation_started_at" and state in ACTIVE_STATES
        ) or (field == "review_started_at" and state in REVIEW_STATES) or (
            field == "closed_at" and state in DONE_STATES
        )
        if raw in {None, ""}:
            parsed[field] = None
            if required:
                errors.append(f"timing.{field} must be a timezone-aware ISO-8601 timestamp")
            continue
        parsed[field] = _parse_iso_datetime(raw)
        if parsed[field] is None:
            errors.append(f"timing.{field} must be a timezone-aware ISO-8601 timestamp")
    opened = parsed.get("issue_opened_at")
    started = parsed.get("implementation_started_at")
    reviewed = parsed.get("review_started_at")
    closed = parsed.get("closed_at")
    if mode == "prospective" and opened and started and started < opened:
        errors.append("prospective registration requires issue_opened_at before implementation_started_at")
    if mode == "retroactive":
        if not _is_non_placeholder(value.get("registration_reason")):
            errors.append("timing.registration_reason is required for retroactive registration")
        errors.extend(_validate_proof_list("timing.registration_evidence", value.get("registration_evidence")))
        if opened and started and started >= opened:
            errors.append("retroactive registration requires implementation_started_at before issue_opened_at")
    if started and reviewed and reviewed < started:
        errors.append("timing.review_started_at cannot precede implementation_started_at")
    if reviewed and closed and closed < reviewed:
        errors.append("timing.closed_at cannot precede review_started_at")
    if opened and closed and closed < opened:
        errors.append("timing.closed_at cannot precede issue_opened_at")
    return errors


def _validate_waves(value: Any, unit_statuses: dict[str, str]) -> tuple[list[str], set[str]]:
    if not isinstance(value, list):
        return ["waves must be a list, even when empty"], set()
    errors: list[str] = []
    wave_ids: set[str] = set()
    for index, wave in enumerate(value):
        where = f"waves[{index}]"
        if not isinstance(wave, dict):
            errors.append(f"{where} must be an object")
            continue
        wave_id = _text(wave.get("id"))
        if not _is_non_placeholder(wave_id) or wave_id in wave_ids:
            errors.append(f"{where}.id must be unique and concrete")
        else:
            wave_ids.add(wave_id)
        status = _text(wave.get("status"))
        if status not in WAVE_STATUSES:
            errors.append(f"{where}.status must be one of {sorted(WAVE_STATUSES)}")
        members = wave.get("unit_ids")
        if not isinstance(members, list) or not members:
            errors.append(f"{where}.unit_ids must contain at least one execution unit")
        else:
            for unit_id in members:
                member_id = _text(unit_id)
                if member_id not in unit_statuses:
                    errors.append(f"{where}.unit_ids contains unknown execution unit: {_text(unit_id)}")
                elif status == "completed" and unit_statuses[member_id] != "delivered":
                    errors.append(f"{where} completed wave requires every member delivered: {member_id}")
        started = _parse_iso_datetime(wave.get("started_at")) if wave.get("started_at") else None
        completed = _parse_iso_datetime(wave.get("completed_at")) if wave.get("completed_at") else None
        if status in {"in_progress", "blocked", "completed"} and started is None:
            errors.append(f"{where}.started_at must be a timezone-aware ISO-8601 timestamp")
        if status == "completed" and completed is None:
            errors.append(f"{where}.completed_at must be a timezone-aware ISO-8601 timestamp")
        if started and completed and completed < started:
            errors.append(f"{where}.completed_at cannot precede started_at")
        if status in {"pending", "in_progress", "blocked"} and completed is not None:
            errors.append(f"{where}.completed_at must be empty until completed")
        errors.extend(_validate_proof_list(f"{where}.evidence", wave.get("evidence")))
    return errors, wave_ids


def _elapsed_seconds(start: Any, end: Any) -> int | None:
    start_at = _parse_iso_datetime(start)
    end_at = _parse_iso_datetime(end)
    if not start_at or not end_at:
        return None
    return int((end_at - start_at).total_seconds())


def calculate_timing_metrics(document: dict[str, Any]) -> dict[str, Any]:
    """Derive stable timing metrics from source timestamps; never store hand-entered durations."""
    timing_value = document.get("timing")
    timing: dict[str, Any] = timing_value if isinstance(timing_value, dict) else {}
    opened = timing.get("issue_opened_at")
    started = timing.get("implementation_started_at")
    reviewed = timing.get("review_started_at")
    closed = timing.get("closed_at")
    metrics: dict[str, Any] = {
        "registration_mode": timing.get("registration_mode"),
        "time_to_start_seconds": _elapsed_seconds(opened, started),
        "issue_cycle_seconds": _elapsed_seconds(opened, closed),
        "implementation_cycle_seconds": _elapsed_seconds(started, closed),
        "review_cycle_seconds": _elapsed_seconds(reviewed, closed),
        "units": {},
        "waves": {},
    }
    for unit in document.get("execution_units", []):
        if isinstance(unit, dict):
            duration = _elapsed_seconds(unit.get("started_at"), unit.get("completed_at"))
            if duration is not None:
                metrics["units"][_text(unit.get("id"))] = duration
    for wave in document.get("waves", []):
        if isinstance(wave, dict):
            duration = _elapsed_seconds(wave.get("started_at"), wave.get("completed_at"))
            if duration is not None:
                metrics["waves"][_text(wave.get("id"))] = duration
    return metrics


def validate_document(document: Any) -> list[str]:
    """Return deterministic validation errors; an empty list means PASS."""
    if not isinstance(document, dict):
        return ["document must be an object"]
    errors: list[str] = []
    if document.get("contract_version") != CONTRACT_VERSION:
        errors.append(f"contract_version must equal {CONTRACT_VERSION}")
    for field in ("runtime", "session_id", "task_stack_id", "source", "handled_by", "governed_by"):
        if not _is_non_placeholder(document.get(field)):
            errors.append(f"{field} is required")
    session_id = _text(document.get("session_id"))
    task_stack_id = _text(document.get("task_stack_id"))
    task_stack_reference = _text(document.get("task_stack_reference"))
    runtime = _text(document.get("runtime"))
    source = _text(document.get("source"))
    handled_by = _text(document.get("handled_by"))
    governed_by = _text(document.get("governed_by"))
    if runtime and runtime not in RUNTIMES:
        errors.append(f"runtime must be one of {sorted(RUNTIMES)}")
    if source and source not in SOURCES:
        errors.append(f"source must be one of {sorted(SOURCES)}")
    if handled_by and handled_by not in HANDLERS:
        errors.append(f"handled_by has an unsupported governed shape; expected one of {sorted(HANDLERS)}")
    if governed_by and governed_by not in GOVERNORS:
        errors.append("governed_by must identify default/thor or thor/default")
    if session_id and not SESSION_RE.fullmatch(session_id):
        errors.append("session_id has an invalid shape")
    if task_stack_id and not STACK_RE.fullmatch(task_stack_id):
        errors.append("task_stack_id has an invalid shape")
    if runtime == "hermes":
        expected_reference = f"thor-task-stack:{task_stack_id}"
        if task_stack_reference != expected_reference:
            errors.append(f"task_stack_reference must equal {expected_reference}")
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True)
    if SENSITIVE_RE.search(serialized):
        errors.append("document contains a sensitive-looking value or absolute home path")

    work_item = document.get("work_item")
    if not isinstance(work_item, dict):
        errors.append("work_item must be an object")
        return errors
    for field in (
        "id",
        "workspace_slug",
        "project_id",
        "project_rationale",
        "identifier",
        "title",
        "title_context",
        "icon_contract_version",
        "presentation_contract_version",
        "semantic_icon_id",
        "frozen_title_snapshot",
        "type_label",
        "objective",
        "context",
        "state",
        "provider_state_id",
        "priority",
        "priority_rationale",
        "start_date",
        "due_date",
    ):
        if not _is_non_placeholder(work_item.get(field)):
            errors.append(f"work_item.{field} is required")
    for field in ("id", "provider_state_id"):
        provider_uuid = _text(work_item.get(field))
        if provider_uuid and not UUID_RE.fullmatch(provider_uuid):
            errors.append(f"work_item.{field} must be a discovered provider UUID")
    type_label = _text(work_item.get("type_label"))
    labels = work_item.get("labels")
    if not isinstance(labels, list) or not any(_is_non_placeholder(item) for item in labels):
        errors.append("[E_LABELS_REQUIRED] work_item.labels must contain concrete labels")
        labels = []
    type_family = [str(item) for item in labels if str(item).startswith("tipo:")]
    supported_types = {
        "tipo:feature", "tipo:bug", "tipo:hardening", "tipo:melhoria",
        "tipo:tech-debt", "tipo:spike", "tipo:ops", "tipo:docs",
    }
    unsupported_types = sorted(item for item in type_family if item not in supported_types)
    if unsupported_types:
        errors.append(f"[E_TYPE_UNSUPPORTED] unsupported tipo:* label(s): {unsupported_types}")
    if len(type_family) != 1:
        errors.append(f"[E_TYPE_CARDINALITY] exactly one tipo:* label is required; found {len(type_family)}")
    if type_label not in supported_types:
        errors.append("[E_TYPE_UNSUPPORTED] work_item.type_label must be a supported canonical tipo:* label")
    if type_label and type_label not in labels:
        errors.append("[E_TYPE_BINDING] work_item.type_label must be present in work_item.labels")
    labels_evidence = work_item.get("labels_evidence")
    errors.extend(_validate_proof_list("work_item.labels_evidence", labels_evidence))
    if not _has_provider_evidence(labels_evidence):
        errors.append("work_item.labels_evidence requires provider evidence via api: or provider-readback:")
    qualifier = _text(work_item.get("qualifier")) or None
    if work_item.get("icon_contract_version") != ICON_CONTRACT_VERSION:
        errors.append(f"[E_ICON_VERSION] work_item.icon_contract_version must equal {ICON_CONTRACT_VERSION}")
    if work_item.get("presentation_contract_version") != PRESENTATION_CONTRACT_VERSION:
        errors.append(f"[E_PRESENTATION_VERSION] work_item.presentation_contract_version must equal {PRESENTATION_CONTRACT_VERSION}")
    expected_semantic_id: str | None = None
    try:
        expected_semantic_id = semantic_icon_id([str(item) for item in labels], qualifier)
    except ValueError as exc:
        errors.append(f"[E_ICON_SEMANTIC] work_item.semantic_icon_id cannot be derived: {exc}")
    if expected_semantic_id and _text(work_item.get("semantic_icon_id")) != expected_semantic_id:
        errors.append(
            f"[E_ICON_SEMANTIC] work_item.semantic_icon_id must equal {expected_semantic_id}"
        )
    title_result = validate_title(
        _text(work_item.get("title")),
        [str(item) for item in labels],
        _text(work_item.get("title_context")),
        qualifier,
    )
    if not title_result.valid:
        errors.append(f"[E_TITLE_CANONICAL] work_item.title must match the canonical title/icon contract: {'; '.join(title_result.errors)}")
    try:
        snapshot_errors = validate_frozen_snapshot(
            _text(work_item.get("title")),
            [str(item) for item in labels],
            _text(work_item.get("title_context")),
            work_item.get("frozen_title_snapshot"),
            qualifier,
        )
    except ValueError as exc:
        snapshot_errors = [f"frozen_title_snapshot cannot be validated: {exc}"]
    errors.extend(f"[E_TITLE_SNAPSHOT] work_item.{error}" for error in snapshot_errors)
    scope = work_item.get("scope")
    if not isinstance(scope, list) or not any(_is_non_placeholder(item) for item in scope):
        errors.append("work_item.scope must contain at least one bounded item")
    if not isinstance(work_item.get("non_goals"), list):
        errors.append("work_item.non_goals must be a list, even when empty")
    for field in DISPOSITION_FIELDS:
        if field not in work_item:
            errors.append(f"work_item.{field} must be explicitly resolved; silent omission is forbidden")
        else:
            errors.extend(_validate_disposition(field, work_item[field]))
    dependencies = work_item.get("dependencies")
    if not isinstance(dependencies, list):
        errors.append("work_item.dependencies must be a list, even when empty")
    start_date = work_item.get("start_date")
    due_date = work_item.get("due_date")
    if not _valid_iso_date(start_date):
        errors.append("work_item.start_date must be an ISO date")
    if not _valid_iso_date(due_date):
        errors.append("work_item.due_date must be an ISO date")
    if _valid_iso_date(start_date) and _valid_iso_date(due_date):
        if date.fromisoformat(_text(due_date)) < date.fromisoformat(_text(start_date)):
            errors.append("work_item.due_date cannot precede start_date")
    errors.extend(
        _validate_trace_body(
            work_item.get("body"),
            work_item.get("body_format"),
            session_id,
            task_stack_id,
            "work_item",
        )
    )
    body_format = _text(work_item.get("body_format")).lower()
    body = _text(work_item.get("body"))
    if "Rastreabilidade" not in body and "Traceability" not in body:
        errors.append("work_item.body must contain a creation traceability section")
    for key in ("source", "handled_by", "governed_by"):
        value = _text(document.get(key))
        if body_format in {"html", "markdown"} and _code_needle(key, value, body_format) not in body:
            errors.append(f"work_item.body must contain code-formatted {key}")
    if runtime == "hermes" and body_format in {"html", "markdown"}:
        if _code_needle("task_stack_reference", task_stack_reference, body_format) not in body:
            errors.append("work_item.body must contain code-formatted task_stack_reference")
    body_metadata = {
        "module": _text(work_item.get("module", {}).get("value"))
        if isinstance(work_item.get("module"), dict) and _text(work_item["module"].get("status")) == "assigned"
        else _text(work_item.get("module", {}).get("status")) if isinstance(work_item.get("module"), dict) else "",
        "cycle": _text(work_item.get("cycle", {}).get("status")) if isinstance(work_item.get("cycle"), dict) else "",
        "estimate": _text(work_item.get("estimate", {}).get("status")) if isinstance(work_item.get("estimate"), dict) else "",
        "start_date": _text(work_item.get("start_date")),
        "due_date": _text(work_item.get("due_date")),
    }
    if body_format in {"html", "markdown"}:
        for key, value in body_metadata.items():
            if _code_needle(key, value, body_format) not in body:
                errors.append(f"work_item.body must contain code-formatted {key}")

    units = document.get("execution_units")
    if not isinstance(units, list) or not units:
        errors.append("execution_units must contain at least one bounded unit")
        units = []
    unit_ids: set[str] = set()
    unit_statuses: dict[str, str] = {}
    delivered_units: set[str] = set()
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            errors.append(f"execution_units[{index}] must be an object")
            continue
        unit_id = _text(unit.get("id"))
        if not _is_non_placeholder(unit_id):
            errors.append(f"execution_units[{index}].id is required")
        elif unit_id in unit_ids:
            errors.append(f"execution_units[{index}].id must be unique")
        else:
            unit_ids.add(unit_id)
        if not _is_non_placeholder(unit.get("owner")):
            errors.append(f"execution_units[{index}].owner is required")
        if not _is_proof_ref(unit.get("proof")):
            errors.append(f"execution_units[{index}].proof requires a proof-class prefix")
        unit_status = _text(unit.get("status"))
        if unit_id:
            unit_statuses[unit_id] = unit_status
        if unit_status not in UNIT_STATUSES:
            errors.append(f"execution_units[{index}].status must be one of {sorted(UNIT_STATUSES)}")
        started_at = _parse_iso_datetime(unit.get("started_at")) if unit.get("started_at") else None
        completed_at = _parse_iso_datetime(unit.get("completed_at")) if unit.get("completed_at") else None
        if unit_status in {"in_progress", "blocked", "delivered"} and started_at is None:
            errors.append(f"execution_units[{index}].started_at must be a timezone-aware ISO-8601 timestamp")
        if unit_status == "delivered" and completed_at is None:
            errors.append(f"execution_units[{index}].completed_at must be a timezone-aware ISO-8601 timestamp")
        if started_at and completed_at and completed_at < started_at:
            errors.append(f"execution_units[{index}].completed_at cannot precede started_at")
        if unit_status in {"pending", "in_progress", "blocked"} and completed_at is not None:
            errors.append(f"execution_units[{index}].completed_at must be empty until delivered")
        if unit_status == "delivered":
            delivered_units.add(unit_id)

    wave_errors, wave_ids = _validate_waves(document.get("waves"), unit_statuses)
    errors.extend(wave_errors)
    comments = document.get("comments")
    if not isinstance(comments, list):
        errors.append("comments must be a list")
        comments = []
    for index, item in enumerate(comments):
        errors.extend(_validate_comment(item, index, session_id, task_stack_id))
        if isinstance(item, dict):
            wave_id = _text(item.get("wave_id"))
            if wave_id and wave_id not in wave_ids:
                errors.append(f"comments[{index}].wave_id must reference a declared wave")
    phases = [_text(item.get("phase")) for item in comments if isinstance(item, dict)]
    if comments and phases[0] != "START":
        errors.append("the first lifecycle comment must be START")
    if phases.count("START") > 1:
        errors.append("START must appear exactly once")
    for index in range(1, len(comments)):
        previous = comments[index - 1]
        current = comments[index]
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        previous_after = _normalize_state(previous.get("state_after"))
        current_before = _normalize_state(current.get("state_before"))
        if previous_after and current_before and previous_after != current_before:
            errors.append(
                f"comments[{index}].state_before must continue the previous state_after"
            )
        previous_at = _parse_iso_datetime(previous.get("occurred_at"))
        current_at = _parse_iso_datetime(current.get("occurred_at"))
        if previous_at and current_at and current_at < previous_at:
            errors.append(f"comments[{index}].occurred_at cannot precede the previous lifecycle event")
    state = _normalize_state(work_item.get("state"))
    if state not in ALLOWED_STATES:
        errors.append(f"work_item.state has unsupported semantic value: {state}")
    errors.extend(_validate_timing(document.get("timing"), state))
    if state in ACTIVE_STATES and "START" not in phases:
        errors.append(f"state {state} requires a START comment before execution")
    if state == "blocked":
        blocked_packets = [
            item for item in comments
            if isinstance(item, dict)
            and _text(item.get("phase")) == "BLOCKED"
            and _normalize_state(item.get("state_after")) == "blocked"
        ]
        if len(blocked_packets) != 1:
            errors.append("[E_BLOCKED_PACKET] state blocked requires a BLOCKED comment with state_after=blocked")
        elif not any(
            _text(unit.get("id")) == _text(blocked_packets[0].get("unit_id"))
            and _text(unit.get("status")) == "blocked"
            for unit in units if isinstance(unit, dict)
        ):
            errors.append("[E_BLOCKED_UNIT] state blocked requires the BLOCKED packet to match a blocked execution unit")
    progress_units = {
        _text(item.get("unit_id"))
        for item in comments
        if isinstance(item, dict) and _text(item.get("phase")) == "PROGRESS"
    }
    acceptance = document.get("acceptance")
    if not isinstance(acceptance, list):
        errors.append("acceptance must be a list")
        acceptance = []
    if state in GATED_STATES and not acceptance:
        errors.append(f"state {state} requires non-empty acceptance criteria")
    for index, item in enumerate(acceptance):
        if not isinstance(item, dict) or not _is_non_placeholder(item.get("criterion")):
            errors.append(f"acceptance[{index}].criterion is required")
    if state not in REVIEW_STATES and "REVIEW" in phases:
        errors.append("REVIEW is allowed only when entering or holding Review / QA or Done")
    if state in REVIEW_STATES:
        review_indices = [index for index, phase in enumerate(phases) if phase == "REVIEW"]
        if len(review_indices) != 1:
            errors.append(f"state {state} requires exactly one REVIEW comment")
        else:
            review_index = review_indices[0]
            progress_indices = [index for index, phase in enumerate(phases) if phase == "PROGRESS"]
            if any(index > review_index for index in progress_indices):
                errors.append("all PROGRESS comments must precede REVIEW")
            blocked_indices = [index for index, phase in enumerate(phases) if phase == "BLOCKED"]
            for blocked_index in blocked_indices:
                blocked_unit = _text(comments[blocked_index].get("unit_id"))
                resumed = any(
                    blocked_index < index < review_index
                    and _text(comments[index].get("unit_id")) == blocked_unit
                    for index in progress_indices
                )
                if not resumed:
                    errors.append("a BLOCKED packet requires a later PROGRESS resume for the same execution unit before REVIEW or FINISH")
        uncovered = sorted(delivered_units - progress_units)
        if uncovered:
            errors.append(f"every delivered execution unit requires PROGRESS coverage; missing execution unit(s): {uncovered}")
    if state not in DONE_STATES and "FINISH" in phases:
        errors.append("FINISH is allowed only for a Done/completed state")
    if state in DONE_STATES:
        if phases.count("FINISH") != 1 or not phases or phases[-1] != "FINISH":
            errors.append("state done requires exactly one FINISH as the final lifecycle comment")
        incomplete_units = sorted(
            _text(unit.get("id"))
            for unit in units
            if isinstance(unit, dict) and _text(unit.get("status")) != "delivered"
        )
        if incomplete_units:
            errors.append(f"state done requires all execution units delivered; incomplete: {incomplete_units}")
        errors.extend(
            _validate_provider_readback(
                document.get("provider_readback"), work_item, session_id, task_stack_id
            )
        )
        errors.extend(
            _validate_independent_review(document.get("independent_review"), handled_by, session_id)
        )
        for index, item in enumerate(acceptance):
            if not isinstance(item, dict) or _text(item.get("status")) != "passed":
                errors.append(f"acceptance[{index}] must be passed before Done")
                continue
            evidence = item.get("evidence")
            errors.extend(_validate_proof_list(f"acceptance[{index}].evidence", evidence))
        if document.get("blockers"):
            errors.append("state done requires blockers to be empty")
        residuals = document.get("residuals")
        if isinstance(residuals, list):
            for index, residual in enumerate(residuals):
                if not isinstance(residual, dict):
                    errors.append("state done residuals must be empty or explicitly accepted as non-blocking")
                    continue
                if len(_text(residual.get("description"))) < 12:
                    errors.append(f"residuals[{index}].description minimum length is 12")
                if _text(residual.get("disposition")) != "accepted_non_blocking":
                    errors.append(f"residuals[{index}].disposition must be accepted_non_blocking")
                if not _is_non_placeholder(residual.get("accepted_by")):
                    errors.append(f"residuals[{index}].accepted_by is required")
                errors.extend(_validate_proof_list(f"residuals[{index}].evidence", residual.get("evidence")))
        validation = document.get("validation")
        if not isinstance(validation, dict) or _text(validation.get("status")) != "passed":
            errors.append("state done requires validation.status=passed")
    for field in ("blockers", "residuals"):
        if not isinstance(document.get(field), list):
            errors.append(f"{field} must be a list")
    validation = document.get("validation")
    if not isinstance(validation, dict):
        errors.append("validation must be an object")
    else:
        errors.extend(_validate_proof_list("validation.checks", validation.get("checks")))
        if state in GATED_STATES and not _is_non_placeholder(validation.get("review")):
            errors.append(f"state {state} requires a review plan")
    return errors


def _html_list(values: Any) -> str:
    rows = values if isinstance(values, list) else []
    if not rows:
        return "<p><em>none</em></p>"
    return "<ul>" + "".join(f"<li>{html.escape(_text(item))}</li>" for item in rows) + "</ul>"


def _markdown_list(values: Any) -> str:
    rows = values if isinstance(values, list) else []
    return "\n".join(f"- {_text(item)}" for item in rows) if rows else "- none"


def _html_named_list(label: str, values: Any) -> str:
    rows = values if isinstance(values, list) else []
    prefix = f"<p><strong>{html.escape(label)}:</strong>"
    if not rows:
        return prefix + " none.</p>"
    return prefix + "</p>" + _html_list(rows)


def _markdown_named_list(label: str, values: Any) -> str:
    rows = values if isinstance(values, list) else []
    if not rows:
        return f"- **{label}:** none."
    return f"- **{label}:**\n" + "\n".join(f"  - {_text(item)}" for item in rows)


def render_comment(comment: dict[str, Any], *, output_format: str = "html") -> str:
    """Validate fields and render a complete lifecycle comment or fail closed."""
    fmt = output_format.lower()
    if fmt not in {"html", "markdown"}:
        raise ValueError("output_format must be html or markdown")
    session_id = _text(comment.get("session_id"))
    task_stack_id = _text(comment.get("task_stack_id"))
    errors = _validate_comment(
        comment,
        0,
        session_id,
        task_stack_id,
        require_body=False,
    )
    if errors:
        raise ValueError("invalid rich lifecycle comment: " + "; ".join(errors))
    phase = _text(comment.get("phase"))
    occurred_at = _text(comment.get("occurred_at"))
    timestamp_source = _text(comment.get("timestamp_source"))
    agent_context = comment.get("agent_context") if isinstance(comment.get("agent_context"), dict) else {}
    agent_runtime = _text(agent_context.get("agent_runtime"))
    model = _text(agent_context.get("model"))
    reasoning_effort = _text(agent_context.get("reasoning_effort"))
    reasoning_trace_policy = _text(agent_context.get("reasoning_trace_policy"))
    tool_surface = _text(agent_context.get("tool_surface"))
    wave_id = _text(comment.get("wave_id"))
    wave_html = f"<li><code>wave_id={html.escape(wave_id)}</code></li>" if wave_id else ""
    wave_markdown = f"- `wave_id={wave_id}`\n" if wave_id else ""
    blocked_html = ""
    blocked_markdown = ""
    if phase == "BLOCKED":
        blocked_html = (
            "<h4>Impacto e condição de desbloqueio</h4>"
            f"<p><strong>Impact:</strong> {html.escape(_text(comment.get('impact')))}</p>"
            f"<p><strong>Unblock condition:</strong> {html.escape(_text(comment.get('unblock_condition')))}</p>"
        )
        blocked_markdown = (
            f"- Impact: {_text(comment.get('impact'))}\n"
            f"- Unblock condition: {_text(comment.get('unblock_condition'))}\n"
        )
    else:
        blocked_html = "<p><strong>Unblock condition:</strong> not applicable; no blockers.</p>"
        blocked_markdown = "- **Unblock condition:** not applicable; no blockers.\n"
    if fmt == "html":
        return (
            f"<h2>{html.escape(phase)}</h2>"
            "<h3>Identidade e estado</h3>"
            f"<ul><li><code>session_id={html.escape(session_id)}</code></li>"
            f"<li><code>task_stack_id={html.escape(task_stack_id)}</code></li>"
            f"<li><code>agent_runtime={html.escape(agent_runtime)}</code></li>"
            f"<li><code>model={html.escape(model)}</code></li>"
            f"<li><code>reasoning_effort={html.escape(reasoning_effort)}</code></li>"
            f"<li><code>reasoning_trace_policy={html.escape(reasoning_trace_policy)}</code></li>"
            f"<li><code>tool_surface={html.escape(tool_surface)}</code></li>"
            f"<li><code>occurred_at={html.escape(occurred_at)}</code></li>"
            f"<li><code>timestamp_source={html.escape(timestamp_source)}</code></li>"
            + wave_html
            + f"<li>state: <code>{html.escape(_text(comment.get('state_before')))} → {html.escape(_text(comment.get('state_after')))}</code></li></ul>"
            "<h3>Unidade de trabalho</h3>"
            f"<p><code>unit_id={html.escape(_text(comment.get('unit_id')))}</code> — {html.escape(_text(comment.get('unit')))}</p>"
            "<h3>Requested vs. delivered</h3>"
            f"<p><strong>Requested:</strong> {html.escape(_text(comment.get('requested')))}</p>"
            f"<p><strong>Delivered:</strong> {html.escape(_text(comment.get('delivered')))}</p>"
            "<h3>Superfícies e evidência</h3>"
            + _html_list(comment.get("surfaces"))
            + _html_list(comment.get("evidence"))
            + "<h3>Decisões</h3>"
            + _html_named_list("Decisions", comment.get("decisions"))
            + "<h3>Blockers</h3>"
            + _html_named_list("Blockers", comment.get("blockers"))
            + blocked_html
            + "<h3>Residuals</h3>"
            + _html_named_list("Residuals", comment.get("residuals"))
            + "<h3>Próxima ação e owner</h3>"
            f"<p>{html.escape(_text(comment.get('next_action')))}</p>"
            f"<p>Owner: <code>{html.escape(_text(comment.get('owner')))}</code></p>"
        )
    return (
        f"## {phase}\n\n"
        "### Identidade e estado\n"
        f"- `session_id={session_id}`\n"
        f"- `task_stack_id={task_stack_id}`\n"
        f"- `agent_runtime={agent_runtime}`\n"
        f"- `model={model}`\n"
        f"- `reasoning_effort={reasoning_effort}`\n"
        f"- `reasoning_trace_policy={reasoning_trace_policy}`\n"
        f"- `tool_surface={tool_surface}`\n"
        f"- `occurred_at={occurred_at}`\n"
        f"- `timestamp_source={timestamp_source}`\n"
        + wave_markdown
        + f"- state: `{_text(comment.get('state_before'))} → {_text(comment.get('state_after'))}`\n\n"
        "### Unidade de trabalho\n"
        f"- `unit_id={_text(comment.get('unit_id'))}` — {_text(comment.get('unit'))}\n\n"
        "### Requested vs. delivered\n"
        f"- Requested: {_text(comment.get('requested'))}\n"
        f"- Delivered: {_text(comment.get('delivered'))}\n\n"
        "### Superfícies e evidência\n"
        + _markdown_list(comment.get("surfaces"))
        + "\n"
        + _markdown_list(comment.get("evidence"))
        + "\n\n### Decisões\n"
        + _markdown_named_list("Decisions", comment.get("decisions"))
        + "\n\n### Blockers\n"
        + _markdown_named_list("Blockers", comment.get("blockers"))
        + "\n"
        + blocked_markdown
        + "\n### Residuals\n"
        + _markdown_named_list("Residuals", comment.get("residuals"))
        + "\n\n### Próxima ação e owner\n"
        f"- {_text(comment.get('next_action'))}\n"
        f"- Owner: `{_text(comment.get('owner'))}`\n"
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--input", required=True, type=Path)
    render_parser = subparsers.add_parser("render-comment")
    render_parser.add_argument("--input", required=True, type=Path)
    render_parser.add_argument("--format", choices=("html", "markdown"), default="html")
    metrics_parser = subparsers.add_parser("metrics")
    metrics_parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload = _load_json(args.input)
        if args.command == "validate":
            errors = validate_document(payload)
            print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
            return 0 if not errors else 2
        if args.command == "metrics":
            errors = validate_document(payload)
            if errors:
                print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
                return 2
            print(json.dumps({"ok": True, "metrics": calculate_timing_metrics(payload)}, ensure_ascii=False, indent=2))
            return 0
        print(render_comment(payload, output_format=args.format))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
