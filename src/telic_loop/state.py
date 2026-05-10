"""LoopState, TaskState, VerificationState, VRCSnapshot and related dataclasses."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Leaf dataclasses (no forward refs)
# ---------------------------------------------------------------------------

@dataclass
class FailureRecord:
    timestamp: str
    attempt: int
    exit_code: int
    stdout: str
    stderr: str
    fix_applied: str | None = None
    files_changed: list[str] = field(default_factory=list)


@dataclass
class VRCSnapshot:
    """A single Vision Reality Check result."""
    iteration: int
    timestamp: str
    deliverables_total: int
    deliverables_verified: int
    deliverables_blocked: int
    value_score: float  # 0.0 – 1.0
    gaps: list[dict] = field(default_factory=list)
    recommendation: str = "CONTINUE"  # CONTINUE | COURSE_CORRECT | DESCOPE | SHIP_READY
    summary: str = ""


@dataclass
class TaskState:
    """A single implementation task."""
    task_id: str
    status: Literal["pending", "in_progress", "done", "blocked", "descoped"] = "pending"
    source: str = ""  # plan | critical_eval | vrc | exit_gate
    created_at: str = ""
    completed_at: str = ""
    blocked_reason: str = ""
    # Rich fields
    description: str = ""
    value: str = ""
    prd_section: str = ""
    acceptance: str = ""
    dependencies: list[str] = field(default_factory=list)
    phase: str = ""
    epic_id: str = ""
    files_expected: list[str] = field(default_factory=list)
    # Execution tracking
    retry_count: int = 0
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    completion_notes: str = ""


@dataclass
class VerificationState:
    """Tracks a single verification (test, criteria check, integrity validation)."""
    verification_id: str
    category: str
    status: Literal["pending", "passed", "failed", "blocked"] = "pending"
    script_path: str | None = None
    attempts: int = 0
    failures: list[FailureRecord] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)

    @property
    def last_error(self) -> str | None:
        if not self.failures:
            return None
        f = self.failures[-1]
        return f"{f.stdout}\n{f.stderr}".strip()

    @property
    def attempt_history(self) -> str:
        lines: list[str] = []
        for f in self.failures:
            lines.append(f"--- Attempt {f.attempt} ---")
            if f.stderr:
                lines.append(f"Error: {f.stderr[:800]}")
            if f.stdout:
                lines.append(f"Output: {f.stdout[:400]}")
            if f.fix_applied:
                lines.append(f"Fix tried: {f.fix_applied}")
        return "\n".join(lines)


@dataclass
class GitCheckpoint:
    """A known-good git commit that the loop can roll back to."""
    commit_hash: str
    timestamp: str
    label: str
    tasks_completed: list[str] = field(default_factory=list)
    verifications_passing: list[str] = field(default_factory=list)
    value_score: float = 0.0


@dataclass
class GitState:
    """Git operations state."""
    branch_name: str = ""
    original_branch: str = ""
    had_stashed_changes: bool = False
    stash_ref: str = ""
    checkpoints: list[GitCheckpoint] = field(default_factory=list)
    last_commit_hash: str = ""
    rollbacks: list[dict] = field(default_factory=list)
    sensitive_patterns: list[str] = field(default_factory=lambda: [
        ".env", ".env.*", "*.pem", "*.key", "*secret*",
        "*credential*", "*password*", "*.p12", "*.pfx",
    ])
    protected_branches: list[str] = field(default_factory=lambda: [
        "main", "master", "develop", "production", "staging",
    ])

    @property
    def latest_checkpoint(self) -> GitCheckpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def checkpoint_for_task(self, task_id: str) -> GitCheckpoint | None:
        for cp in reversed(self.checkpoints):
            if task_id in cp.tasks_completed:
                return cp
        return None

    def best_rollback_target(self, failed_task_ids: list[str]) -> GitCheckpoint | None:
        for cp in reversed(self.checkpoints):
            if not any(tid in cp.tasks_completed for tid in failed_task_ids):
                return cp
        return None


@dataclass
class SprintContext:
    """Derived by Context Discovery — never human-provided."""
    deliverable_type: str = "unknown"
    project_type: str = "unknown"
    codebase_state: str = "greenfield"
    environment: dict = field(default_factory=dict)
    services: dict = field(default_factory=dict)
    verification_strategy: dict = field(default_factory=dict)
    value_proofs: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# LoopState — the single source of truth
# ---------------------------------------------------------------------------

@dataclass
class LoopState:
    sprint: str
    iteration: int = 0

    # Gates (only 3: plan_generated, plan_reviewed, critical_eval_passed)
    gates_passed: set[str] = field(default_factory=set)

    # Sprint context
    context: SprintContext = field(default_factory=SprintContext)

    # Task tracking
    tasks: dict[str, TaskState] = field(default_factory=dict)

    # Verification tracking
    verifications: dict[str, VerificationState] = field(default_factory=dict)

    # Regression baseline
    regression_baseline: set[str] = field(default_factory=set)

    # VRC history
    vrc_history: list[VRCSnapshot] = field(default_factory=list)

    # Evaluation findings audit trail (populated by handle_eval_finding)
    evaluation_findings: list[dict] = field(default_factory=list)

    # Progress tracking
    progress_log: list[dict] = field(default_factory=list)

    # Git operations
    git: GitState = field(default_factory=GitState)

    # Exit gate
    exit_gate_attempts: int = 0

    # Task granularity limits (populated from config at loop start)
    max_task_description_chars: int = 600
    max_files_per_task: int = 5

    # Token tracking
    total_tokens_used: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # Crash history (condensed summaries — full tracebacks in .crash_log.jsonl)
    crash_log: list[dict] = field(default_factory=list)

    # Per-phase consecutive crash counts (reset on success)
    phase_crash_counts: dict[str, int] = field(default_factory=dict)

    # Builder exit request flag
    exit_requested: bool = False

    # Quota / rate-limit pause signal — populated when the inner loop exits
    # cleanly because the Anthropic API is exhausted. Outer loop reads these
    # to convert the inner-loop exit into a typed QuotaExceededError without
    # needing to reparse the SDK error text. crash_kind="quota_exceeded" is
    # the only value used today; the field is kept generic for future
    # categories.
    crash_kind: str = ""
    crash_signature_kind: str = ""  # "quota" / "rate_limit" / "overloaded"
    quota_reset_at: str = ""  # ISO timestamp; empty means "use default"
    crash_source_text: str = ""  # short SDK error excerpt, capped on write

    # ----- Properties -----

    @property
    def latest_vrc(self) -> VRCSnapshot | None:
        return self.vrc_history[-1] if self.vrc_history else None

    @property
    def value_delivered(self) -> bool:
        if self.gate_passed("exit_gate"):
            return True
        vrc = self.latest_vrc
        return vrc is not None and vrc.recommendation == "SHIP_READY"

    # ----- Methods -----

    def record_progress(
        self, action: str, result: str, made_progress: bool,
        input_tokens: int = 0, output_tokens: int = 0,
        duration_sec: float = 0.0,
        crash_type: str = "",
    ) -> None:
        entry: dict[str, Any] = {
            "iteration": self.iteration,
            "action": action,
            "result": result,
            "timestamp": datetime.now().isoformat(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "duration_sec": duration_sec,
        }
        if crash_type:
            entry["crash_type"] = crash_type
        self.progress_log.append(entry)

    def gate_passed(self, name: str) -> bool:
        return name in self.gates_passed

    def pass_gate(self, name: str) -> None:
        self.gates_passed.add(name)

    def add_task(self, task: TaskState) -> None:
        existing = self.tasks.get(task.task_id)
        if existing and existing.status in ("done", "in_progress", "descoped"):
            return
        self.tasks[task.task_id] = task

    def invalidate_failed_tests(self) -> None:
        """Clear only failed/blocked verifications, keep passing ones."""
        to_remove = [
            vid for vid, v in self.verifications.items()
            if v.status in ("failed", "blocked")
        ]
        for vid in to_remove:
            del self.verifications[vid]
            self.regression_baseline.discard(vid)

    # ----- Persistence -----

    def save(self, path: Path) -> None:
        """Atomic state save: write to .tmp then rename."""
        data = asdict(self)
        # Convert sets to sorted lists for JSON
        data["gates_passed"] = sorted(data["gates_passed"])
        data["regression_baseline"] = sorted(data["regression_baseline"])

        def _serialize(obj: object) -> Any:
            if isinstance(obj, datetime):
                return obj.isoformat()
            if isinstance(obj, Path):
                return str(obj)
            if isinstance(obj, set):
                return sorted(obj)
            raise TypeError(f"Cannot serialize {type(obj).__name__}: {obj!r}")

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, default=_serialize), encoding="utf-8")
        tmp_path.replace(path)  # atomic on POSIX; near-atomic on Windows NTFS

    @classmethod
    def load(cls, path: Path) -> LoopState:
        from dacite import Config, from_dict
        from .tools import normalize_gaps, normalize_services
        data = json.loads(path.read_text(encoding="utf-8"))
        # Normalize VRC gaps and context.services (agents produce variant formats)
        for vrc in data.get("vrc_history", []):
            vrc["gaps"] = normalize_gaps(vrc.get("gaps", []))
        ctx = data.get("context", {})
        if "services" in ctx:
            ctx["services"] = normalize_services(ctx["services"])
        return from_dict(
            data_class=cls,
            data=data,
            config=Config(
                cast=[Literal, set, tuple],
                strict=False,
            ),
        )
