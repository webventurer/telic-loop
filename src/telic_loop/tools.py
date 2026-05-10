"""Tool schemas, validation, and structured output handlers.

V4 does NOT define execution tool schemas — the Claude Code SDK provides
Bash/Read/Write/Edit/Glob/Grep natively. V4 only defines structured output
tools called via tool CLI for state mutations.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import LoopState


# ---------------------------------------------------------------------------
# Structured output tool schemas
# ---------------------------------------------------------------------------

MANAGE_TASK_SCHEMA: dict = {
    "name": "manage_task",
    "description": "Add, modify, or remove a task in the implementation plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "modify", "remove"]},
            "task_id": {"type": "string"},
            "description": {"type": "string"},
            "value": {"type": "string"},
            "acceptance": {"type": "string"},
            "prd_section": {"type": "string"},
            "dependencies": {"type": "array", "items": {"type": "string"}},
            "phase": {"type": "string"},
            "files_expected": {"type": "array", "items": {"type": "string"}},
            "field": {
                "type": "string",
                "enum": ["description", "value", "acceptance", "dependencies",
                         "phase", "status", "blocked_reason", "files_expected"],
            },
            "new_value": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action", "task_id"],
    },
}

REPORT_TASK_COMPLETE_SCHEMA: dict = {
    "name": "report_task_complete",
    "description": "Signal that a task has been completed with its deliverables.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "files_created": {"type": "array", "items": {"type": "string"}},
            "files_modified": {"type": "array", "items": {"type": "string"}},
            "value_verified": {"type": "string"},
            "completion_notes": {"type": "string"},
        },
        "required": ["task_id", "files_created", "files_modified"],
    },
}

REPORT_DISCOVERY_SCHEMA: dict = {
    "name": "report_discovery",
    "description": "Report sprint context discovered from Vision, PRD, and codebase.",
    "input_schema": {
        "type": "object",
        "properties": {
            "deliverable_type": {"type": "string", "enum": ["software", "document", "data", "config", "hybrid"]},
            "project_type": {"type": "string"},
            "codebase_state": {"type": "string", "enum": ["greenfield", "brownfield", "non_code"]},
            "environment": {"type": "object"},
            "services": {"type": "object"},
            "verification_strategy": {"type": "object"},
            "value_proofs": {"type": "array", "items": {"type": "string"}},
            "unresolved_questions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["deliverable_type", "project_type", "codebase_state", "value_proofs"],
    },
}

REPORT_VRC_SCHEMA: dict = {
    "name": "report_vrc",
    "description": "Report Vision Reality Check result.",
    "input_schema": {
        "type": "object",
        "properties": {
            "value_score": {"type": "number", "minimum": 0, "maximum": 1},
            "deliverables_verified": {"type": "integer"},
            "deliverables_total": {"type": "integer"},
            "deliverables_blocked": {"type": "integer", "default": 0},
            "gaps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "description": {"type": "string"},
                        "severity": {"type": "string", "enum": ["critical", "blocking", "degraded", "polish"]},
                        "suggested_task": {"type": "string"},
                    },
                },
            },
            "recommendation": {"type": "string", "enum": ["CONTINUE", "COURSE_CORRECT", "DESCOPE", "SHIP_READY"]},
            "summary": {"type": "string"},
        },
        "required": ["value_score", "deliverables_verified", "deliverables_total", "recommendation", "summary"],
    },
}

REPORT_EVAL_FINDING_SCHEMA: dict = {
    "name": "report_eval_finding",
    "description": "Report a finding from critical evaluation of the deliverable.",
    "input_schema": {
        "type": "object",
        "properties": {
            "severity": {"type": "string", "enum": ["critical", "blocking", "degraded", "polish"]},
            "description": {"type": "string"},
            "user_impact": {"type": "string"},
            "suggested_fix": {"type": "string"},
            "evidence": {"type": "string"},
            "verdict": {
                "type": "string",
                "enum": ["SHIP_READY", "CONTINUE"],
                "description": "Overall evaluation verdict. SHIP_READY = all critical issues resolved.",
            },
        },
        "required": ["severity", "description", "user_impact"],
    },
}

REQUEST_EXIT_SCHEMA: dict = {
    "name": "request_exit",
    "description": "Builder signals readiness for evaluation. Called when all tasks are done and verifications pass.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why the builder believes work is complete"},
            "tasks_completed": {"type": "integer"},
            "verifications_passing": {"type": "integer"},
        },
        "required": ["reason"],
    },
}

# All structured tool schemas for convenience
ALL_STRUCTURED_SCHEMAS: list[dict] = [
    MANAGE_TASK_SCHEMA,
    REPORT_TASK_COMPLETE_SCHEMA,
    REPORT_DISCOVERY_SCHEMA,
    REPORT_VRC_SCHEMA,
    REPORT_EVAL_FINDING_SCHEMA,
    REQUEST_EXIT_SCHEMA,
]


# ---------------------------------------------------------------------------
# Task mutation guardrails
# ---------------------------------------------------------------------------

DUPLICATE_SIMILARITY_THRESHOLD = 0.75
MID_LOOP_TASK_CEILING = 20
VALID_RECOMMENDATIONS = ("CONTINUE", "COURSE_CORRECT", "DESCOPE", "SHIP_READY")
COMPLETE_STATUSES = ("done", "descoped")

# Fields that can be set directly on TaskState without special handling
_SIMPLE_TASK_FIELDS = {"description", "value", "acceptance", "phase"}

# Recommendation aliases from non-standard agent outputs
_RECOMMENDATION_MAP = {
    "proceed": "CONTINUE",
    "conditional_pass": "SHIP_READY",
}


# Meta-instruction and oversized-scope detection
# ---------------------------------------------------------------------------

_META_PATTERNS = [
    re.compile(r"^(continue|proceed|begin|start|resume)\b.*\b(with|execution|phase|tasks?)\b", re.I),
    re.compile(r"\btasks?\s+\d+\.\d+[\s-]+(through|to)\s+\d+\.\d+", re.I),
    re.compile(r"\brun\s+tasks?\s+\d+", re.I),
    re.compile(r"^no\s+(new\s+)?tasks?\s+(needed|required)", re.I),
    re.compile(r"\bmark(ed)?\s+(as\s+)?done\b", re.I),
    re.compile(r"\bsequentially\s+to\s+build\b", re.I),
    re.compile(r"\bcovered\s+by\s+existing\s+tasks?\b", re.I),
    re.compile(r"\balready\s+(covered|handled|addressed)\b", re.I),
    re.compile(r"\bexisting\s+tasks?\s+\d+\.\d+\s+covers\b", re.I),
    re.compile(r"\bno\s+new\s+task\s+(needed|required)\b", re.I),
]

_OVERSIZED_PATTERNS = [
    re.compile(r"\ball\s+(remaining|epic|planned|pending|unbuilt)\b", re.I),
    re.compile(r"\bentire\s+(epic|frontend|backend|app|project|system)\b", re.I),
    re.compile(r"\beverything\s+(for|in|from)\b", re.I),
]


def _is_meta_instruction(description: str) -> bool:
    """Detect descriptions that are execution instructions, not implementation specs."""
    return any(p.search(description) for p in _META_PATTERNS)


def _is_oversized_scope(description: str) -> bool:
    """Detect descriptions that bundle too many deliverables into one task."""
    return any(p.search(description) for p in _OVERSIZED_PATTERNS)


def _validate_add(task_id: str, input_data: dict, state: LoopState) -> str | None:
    """Validate a new task addition."""
    missing = [f for f in ["description", "value", "acceptance"] if not input_data.get(f)]
    if missing:
        return f"Task {task_id} missing: {', '.join(missing)}"

    desc = input_data["description"]
    new_desc_lower = desc.lower()
    for existing in state.tasks.values():
        if existing.status in COMPLETE_STATUSES:
            continue
        sim = _jaccard_similarity(new_desc_lower, existing.description.lower())
        if sim >= DUPLICATE_SIMILARITY_THRESHOLD:
            return f"Task {task_id} duplicates {existing.task_id} ({sim:.0%} similar)"

    mid_loop = [
        t for t in state.tasks.values()
        if t.source != "plan" and t.status not in COMPLETE_STATUSES
    ]
    if len(mid_loop) >= MID_LOOP_TASK_CEILING:
        return f"Mid-loop task ceiling ({MID_LOOP_TASK_CEILING}) reached"

    for dep_id in input_data.get("dependencies", []):
        if dep_id not in state.tasks:
            return f"Dependency '{dep_id}' doesn't exist"

    return _validate_granularity(task_id, input_data, state)


def _validate_granularity(task_id: str, input_data: dict, state: LoopState) -> str | None:
    """Validate task size constraints and content quality."""
    desc = input_data.get("description", "")
    max_desc = state.max_task_description_chars
    if len(desc) > max_desc:
        return (
            f"Description too long ({len(desc)} chars, max {max_desc}). "
            "Split this task into smaller, focused units."
        )

    files = input_data.get("files_expected", [])
    max_files = state.max_files_per_task
    if len(files) > max_files:
        return (
            f"Too many files_expected ({len(files)}, max {max_files}). "
            "Split into separate tasks, each touching fewer files."
        )

    if _is_meta_instruction(desc):
        return (
            "Task describes execution instructions, not implementation work. "
            "Each task must specify concrete code changes."
        )

    if _is_oversized_scope(desc):
        return (
            "Task scope too broad — describes multiple deliverables. "
            "Split into focused tasks, each addressing one concern."
        )

    return None


def _validate_modify(task_id: str, input_data: dict, state: LoopState) -> str | None:
    """Validate a task modification."""
    if task_id not in state.tasks:
        return f"Task {task_id} doesn't exist"
    if input_data.get("field") == "dependencies":
        raw = input_data.get("new_value", [])
        new_deps = json.loads(raw) if isinstance(raw, str) else raw
        for dep_id in (new_deps or []):
            if _creates_cycle(task_id, dep_id, state):
                return f"Circular dependency: {task_id} -> {dep_id}"
    return None


def _validate_remove(task_id: str, input_data: dict, state: LoopState) -> str | None:
    """Validate a task removal."""
    if task_id not in state.tasks:
        return f"Task {task_id} doesn't exist"
    dependents = [
        t.task_id for t in state.tasks.values()
        if task_id in (t.dependencies or [])
    ]
    if dependents:
        return f"Cannot remove {task_id} -- depended on by {', '.join(dependents)}"
    return None


_VALIDATORS = {
    "add": _validate_add,
    "modify": _validate_modify,
    "remove": _validate_remove,
}


def validate_task_mutation(action: str, input_data: dict, state: LoopState) -> str | None:
    """Deterministic validation. Returns error message or None."""
    task_id = input_data.get("task_id", "")
    validator = _VALIDATORS.get(action)
    if validator:
        return validator(task_id, input_data, state)
    return None


def _jaccard_similarity(a: str, b: str) -> float:
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / len(wa | wb) if (wa or wb) else 0.0


def _creates_cycle(task_id: str, new_dep_id: str, state: LoopState) -> bool:
    visited: set[str] = set()
    stack = [new_dep_id]
    while stack:
        current = stack.pop()
        if current == task_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        t = state.tasks.get(current)
        if t and t.dependencies:
            stack.extend(t.dependencies)
    return False


# ---------------------------------------------------------------------------
# Tool execution dispatch (transactional)
# ---------------------------------------------------------------------------

def execute_tool(name: str, input_data: dict, state: LoopState,
                 task_source: str = "agent") -> str:
    """Dispatch structured tool calls. State is mutated transactionally."""

    HANDLERS = {
        "manage_task": handle_manage_task,
        "report_task_complete": handle_task_complete,
        "report_discovery": handle_discovery,
        "report_vrc": handle_vrc,
        "report_eval_finding": handle_eval_finding,
        "request_exit": handle_request_exit,
    }

    handler = HANDLERS.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})

    # Snapshot mutable state for transactional safety
    snapshot = {
        "tasks": copy.deepcopy(state.tasks),
        "verifications": copy.deepcopy(state.verifications),
        "context": copy.deepcopy(state.context),
        "vrc_history": copy.deepcopy(state.vrc_history),
        "gates_passed": copy.deepcopy(state.gates_passed),
        "exit_requested": state.exit_requested,
        "evaluation_findings": copy.deepcopy(state.evaluation_findings),
    }
    try:
        result = handler(input_data, state, task_source=task_source)
        return json.dumps({"ok": True, "result": result})
    except Exception as e:
        state.tasks = snapshot["tasks"]
        state.verifications = snapshot["verifications"]
        state.context = snapshot["context"]
        state.vrc_history = snapshot["vrc_history"]
        state.gates_passed = snapshot["gates_passed"]
        state.exit_requested = snapshot["exit_requested"]
        state.evaluation_findings = snapshot["evaluation_findings"]
        return json.dumps({"error": f"Handler failed: {e}", "rolled_back": True})


# ---------------------------------------------------------------------------
# Structured output handlers
# ---------------------------------------------------------------------------

def _apply_status_change(
    task: Any, task_id: str, new_value: str,
    task_source: str, input_data: dict,
) -> str | None:
    """Apply a status change to a task. Returns error message or None."""
    if task.status == "descoped" and new_value != "descoped":
        return (
            f"Cannot change {task_id} from descoped to {new_value}. "
            f"Descoped tasks were intentionally removed from scope."
        )
    if new_value == "descoped":
        if task.source in ("plan", "agent"):
            if task_source not in ("course_correction", "exit_gate"):
                return (
                    f"Cannot descope {task_id}: planned deliverable. "
                    f"Only course correction can descope planned tasks."
                )
        task.blocked_reason = input_data.get("reason", "Descoped")
    task.status = new_value
    return None


def handle_manage_task(input_data: dict, state: LoopState, task_source: str = "agent") -> str:
    from .state import TaskState

    action = input_data["action"]
    task_id = input_data["task_id"]

    error = validate_task_mutation(action, input_data, state)
    if error:
        return f"REJECTED: {error}"

    if action == "add":
        task = TaskState(
            task_id=task_id,
            source=task_source,
            description=input_data.get("description", ""),
            value=input_data.get("value", ""),
            acceptance=input_data.get("acceptance", ""),
            prd_section=input_data.get("prd_section", ""),
            dependencies=input_data.get("dependencies", []),
            phase=input_data.get("phase", ""),
            files_expected=input_data.get("files_expected", []),
            created_at=datetime.now().isoformat(),
        )
        state.add_task(task)
        return f"Added task {task_id}"

    elif action == "modify":
        task = state.tasks[task_id]
        field_name = input_data.get("field", "")
        new_value = input_data.get("new_value", "")

        if field_name == "status":
            error = _apply_status_change(task, task_id, new_value, task_source, input_data)
            if error:
                return error
        elif field_name == "blocked_reason":
            task.blocked_reason = new_value
            if new_value and task.status != "blocked":
                task.status = "blocked"
        elif field_name in ("dependencies", "files_expected"):
            setattr(task, field_name, json.loads(new_value) if isinstance(new_value, str) else new_value)
        elif field_name in _SIMPLE_TASK_FIELDS:
            setattr(task, field_name, new_value)
        else:
            return f"Unknown field: {field_name}"

        return f"Modified {task_id}.{field_name}"

    elif action == "remove":
        del state.tasks[task_id]
        return f"Removed task {task_id}"

    return f"Unknown action: {action}"


def _normalize_paths(paths: list[str], sprint_prefix: str) -> list[str]:
    """Ensure file paths include the sprint directory prefix."""
    result = []
    for p in paths:
        if not p:
            continue
        if Path(p).is_absolute() or p.startswith("sprints/"):
            result.append(p)
        else:
            result.append(sprint_prefix + p)
    return result


def handle_task_complete(input_data: dict, state: LoopState, **_: Any) -> str:
    task_id = input_data["task_id"]
    task = state.tasks.get(task_id)
    if not task:
        return f"Task {task_id} not found"
    if task.status == "descoped":
        return f"Task {task_id} is descoped — cannot mark complete"
    task.status = "done"
    task.completed_at = datetime.now().isoformat()
    sprint_prefix = f"sprints/{state.sprint}/"
    task.files_created = _normalize_paths(input_data.get("files_created", []), sprint_prefix)
    task.files_modified = _normalize_paths(input_data.get("files_modified", []), sprint_prefix)
    task.completion_notes = input_data.get("completion_notes", "")
    return f"Task {task_id} marked complete"


def normalize_services(raw: Any) -> dict:
    """Convert list-of-dicts services format to the expected dict-of-dicts."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        result: dict = {}
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                name = item.pop("name")
                result[name] = item
        return result
    return {}


def normalize_gaps(gaps: Any) -> list[dict]:
    """Ensure each gap entry is a dict, not a bare string."""
    if not gaps:
        return []
    if isinstance(gaps[0], str):
        return [{"description": g, "severity": "degraded"} for g in gaps]
    return gaps


def normalize_recommendation(raw: Any) -> str:
    """Normalize recommendation to a valid enum value."""
    if not isinstance(raw, str):
        return "CONTINUE"
    mapped = _RECOMMENDATION_MAP.get(raw.lower(), raw.upper())
    return mapped if mapped in VALID_RECOMMENDATIONS else "CONTINUE"


def normalize_value_score(raw: Any) -> float:
    """Normalize value_score to 0.0-1.0 (agents sometimes use 0-10 or 0-100)."""
    score = float(raw or 0.0)
    if score > 1.0:
        score = score / 100.0 if score > 10.0 else score / 10.0
    return score


def handle_discovery(input_data: dict, state: LoopState, **_: Any) -> str:
    from .state import SprintContext
    state.context = SprintContext(
        deliverable_type=input_data.get("deliverable_type", "unknown"),
        project_type=input_data.get("project_type", "unknown"),
        codebase_state=input_data.get("codebase_state", "greenfield"),
        environment=input_data.get("environment", {}),
        services=normalize_services(input_data.get("services", {})),
        verification_strategy=input_data.get("verification_strategy", {}),
        value_proofs=input_data.get("value_proofs", []),
        unresolved_questions=input_data.get("unresolved_questions", []),
    )
    return "Discovery reported"


def _create_tasks_from_gaps(
    gaps: list[dict], rec: str, state: LoopState,
) -> int:
    """Auto-create tasks from VRC gap suggestions. Returns count created."""
    from .state import TaskState
    created = 0
    existing_descs = {
        t.description for t in state.tasks.values()
        if t.status not in COMPLETE_STATUSES
    }
    for gap in gaps:
        suggested = gap.get("suggested_task", "")
        if not suggested or suggested in existing_descs:
            continue
        if gap.get("severity", "degraded") == "polish" and rec == "SHIP_READY":
            continue

        gap_id = gap.get("id", f"gap-{created}")
        task_id = f"VRC-{state.iteration}-{gap_id}"
        candidate = {
            "task_id": task_id,
            "description": suggested,
            "value": gap.get("description", ""),
            "acceptance": f"Gap '{gap_id}' resolved: {gap.get('description', '')}",
        }
        error = validate_task_mutation("add", candidate, state)
        if error:
            print(f"  VRC task {task_id} rejected: {error}")
            continue

        state.tasks[task_id] = TaskState(
            task_id=task_id,
            source="vrc",
            description=suggested,
            value=gap.get("description", ""),
            acceptance=candidate["acceptance"],
            created_at=datetime.now().isoformat(),
        )
        existing_descs.add(suggested)
        created += 1
    return created


def handle_vrc(input_data: dict, state: LoopState, **_: Any) -> str:
    from .state import VRCSnapshot

    score = normalize_value_score(input_data.get("value_score", 0.0))
    rec = normalize_recommendation(input_data.get("recommendation", "CONTINUE"))
    gaps = normalize_gaps(input_data.get("gaps", []))

    snapshot = VRCSnapshot(
        iteration=state.iteration,
        timestamp=datetime.now().isoformat(),
        deliverables_total=input_data.get("deliverables_total", 0),
        deliverables_verified=input_data.get("deliverables_verified", 0),
        deliverables_blocked=input_data.get("deliverables_blocked", 0),
        value_score=score,
        gaps=gaps,
        recommendation=rec,
        summary=input_data.get("summary", ""),
    )
    state.vrc_history.append(snapshot)

    created = _create_tasks_from_gaps(gaps, rec, state)
    task_msg = f", {created} task(s) created" if created else ""
    return f"VRC recorded: {score:.0%} value ({rec}){task_msg}"


# ---------------------------------------------------------------------------
# Browser evidence validation for SHIP_READY
# ---------------------------------------------------------------------------

_BROWSER_EVIDENCE_TERMS = {
    "screenshot", "browser", "navigate", "viewport", "playwright",
    "browser_navigate", "browser_click", "browser_snapshot",
    "browser_take_screenshot", "browser_fill", "browser_select_option",
    "browser_resize", "localhost", "http://", "https://",
    "landing page", "ui", "rendered", "displayed", "visible",
    "layout", "responsive", "mobile", "hover", "clicked",
}

# Deliverable types that clearly have no browser UI
_NON_UI_TYPES = {"document", "data", "config"}

# Project types that clearly have no browser UI
_NON_UI_PROJECT_TYPES = {
    "cli", "library", "api", "rest-api", "graphql-api", "microservice",
    "script", "automation", "etl", "pipeline", "sdk", "package",
    "backend", "daemon", "worker", "bot", "plugin", "extension",
}

# Deliverable types / project types that imply a browser UI
_UI_TYPES = {"hybrid", "web", "webapp", "website", "frontend", "fullstack"}


def _requires_browser_evidence(state: LoopState) -> bool:
    """True if the deliverable likely has a UI that should be browser-tested."""
    dt = state.context.deliverable_type.lower()
    pt = state.context.project_type.lower()

    # Explicit non-UI types
    if dt in _NON_UI_TYPES or pt in _NON_UI_PROJECT_TYPES:
        return False

    # Explicit UI types
    if dt in _UI_TYPES or pt in _UI_TYPES:
        return True

    # Services with a port hint at something browsable
    if state.context.services:
        return True

    # Default: no positive signal of a UI, don't require browser evidence
    return False


def _has_browser_evidence(text: str) -> bool:
    """Check if evidence text contains sufficient browser-related terms."""
    if not text:
        return False
    text_lower = text.lower()
    matches = sum(1 for term in _BROWSER_EVIDENCE_TERMS if term in text_lower)
    return matches >= 2


def _validate_ship_ready(state: LoopState, input_data: dict) -> str | None:
    """Validate that SHIP_READY verdict has sufficient browser evidence.

    Returns error message if validation fails, None if it passes.
    Skips validation for non-UI deliverables.
    """
    if not _requires_browser_evidence(state):
        return None

    findings = state.evaluation_findings
    # Need at least 2 prior findings with substantive evidence
    substantive = [f for f in findings if len(f.get("evidence", "")) > 20]
    if len(substantive) < 2:
        return (
            "SHIP_READY REJECTED: Insufficient evaluation evidence. "
            f"Only {len(substantive)} finding(s) with evidence recorded. "
            "You must report at least 2 findings with detailed evidence "
            "before issuing SHIP_READY. Open the app in the browser, "
            "test user flows, and report what you observe."
        )

    # Need at least 1 finding with browser-related evidence
    has_browser = any(_has_browser_evidence(f.get("evidence", "")) for f in findings)
    if not has_browser:
        return (
            "SHIP_READY REJECTED: No browser evidence found. "
            "This deliverable has a UI but none of your findings "
            "reference browser testing (screenshots, navigation, viewport, etc.). "
            "You MUST open the app with browser_navigate, test the UI, "
            "and report findings with browser evidence before issuing SHIP_READY."
        )

    return None


def handle_eval_finding(input_data: dict, state: LoopState, **_: Any) -> str:
    from .state import TaskState
    severity = input_data["severity"]

    # Always record finding to audit trail
    state.evaluation_findings.append({
        "severity": severity,
        "description": input_data.get("description", ""),
        "evidence": input_data.get("evidence", ""),
        "user_impact": input_data.get("user_impact", ""),
        "verdict": input_data.get("verdict", ""),
        "timestamp": datetime.now().isoformat(),
    })

    # Handle verdict for exit gate decisions
    verdict = input_data.get("verdict", "")
    if verdict == "SHIP_READY":
        error = _validate_ship_ready(state, input_data)
        if error:
            return error
        state.pass_gate("critical_eval_passed")

    if severity in ("critical", "blocking"):
        task_id = f"CE-{state.iteration}-{len(state.tasks)}"
        state.add_task(TaskState(
            task_id=task_id,
            source="critical_eval",
            description=input_data.get("suggested_fix", input_data["description"]),
            value=input_data["user_impact"],
            acceptance=f"Fix: {input_data['description']}",
            created_at=datetime.now().isoformat(),
        ))
    return f"Finding recorded: [{severity}] {input_data['description']}"


def handle_request_exit(input_data: dict, state: LoopState, **_: Any) -> str:
    state.exit_requested = True
    return f"Exit requested: {input_data.get('reason', 'no reason given')}"
