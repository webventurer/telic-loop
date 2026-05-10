"""Entry point: plan → review → implement → evaluate → complete."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .agent import Agent, AgentRole, RateLimitError, load_prompt, parse_rate_limit_wait_seconds
from .config import LoopConfig
from .render import generate_delivery_report, render_plan_snapshot, render_value_checklist
from .state import LoopState


# ---------------------------------------------------------------------------
# Phase determination
# ---------------------------------------------------------------------------

def determine_phase(state: LoopState) -> str:
    """Compute current phase from gate state. Never stored — always derived."""
    if "plan_generated" not in state.gates_passed:
        return "plan"
    if "plan_reviewed" not in state.gates_passed:
        return "review"
    if _has_work_remaining(state):
        return "implement"
    if "critical_eval_passed" not in state.gates_passed:
        return "evaluate"
    if _value_gate_passes(state):
        return "complete"
    return "implement"  # Eval found gaps → back to building


def _has_work_remaining(state: LoopState) -> bool:
    """True if there are pending/in_progress tasks or failing verifications."""
    has_pending = any(
        t.status in ("pending", "in_progress") for t in state.tasks.values()
    )
    has_failing = any(
        v.status == "failed" for v in state.verifications.values()
    )
    return has_pending or has_failing


def _value_gate_passes(state: LoopState) -> bool:
    """True when all work is done and evaluation has passed."""
    all_done = all(
        t.status in ("done", "blocked", "descoped") for t in state.tasks.values()
    )
    all_pass = all(
        v.status in ("passed", "blocked") for v in state.verifications.values()
    )
    has_eval = "critical_eval_passed" in state.gates_passed
    return all_done and all_pass and has_eval


# ---------------------------------------------------------------------------
# Phase dispatchers
# ---------------------------------------------------------------------------

def _base_prompt_params(config: LoopConfig) -> dict[str, str]:
    """Common prompt template parameters shared across all phases."""
    return {
        "SPRINT": config.sprint,
        "SPRINT_DIR": str(config.sprint_dir),
        "PROJECT_DIR": str(config.effective_project_dir),
    }


def _run_plan_phase(config: LoopConfig, state: LoopState, agent: Agent) -> bool:
    """Planner discovers context and creates implementation tasks."""
    arch_section = ""
    if config.architecture_file.exists():
        arch_section = (
            "- **ARCHITECTURE**: Read `{dir}/ARCHITECTURE.md` "
            "— technical architecture decisions (follow these closely)\n"
        ).format(dir=config.sprint_dir)
    prompt = load_prompt("planner",
        **_base_prompt_params(config),
        MAX_TASK_DESC_CHARS=str(config.max_task_description_chars),
        MAX_FILES_PER_TASK=str(config.max_files_per_task),
        ARCHITECTURE_SECTION=arch_section,
    )
    session = agent.session(AgentRole.PLANNER, system_extra=prompt)
    session.send(
        "Read the Vision and PRD, discover the project context, "
        "then create a complete implementation plan as structured tasks.",
        task_source="plan",
    )
    if state.tasks:
        state.pass_gate("plan_generated")
        render_plan_snapshot(config, state)
        return True
    return False


def _run_review_phase(config: LoopConfig, state: LoopState, agent: Agent) -> bool:
    """Reviewer evaluates plan quality in a separate context."""
    arch_section = ""
    if config.architecture_file.exists():
        arch_section = (
            "- **ARCHITECTURE**: `{dir}/ARCHITECTURE.md` "
            "— verify plan aligns with architecture decisions\n"
        ).format(dir=config.sprint_dir)
    prompt = load_prompt("reviewer",
        **_base_prompt_params(config),
        PLAN_STATE=_format_plan_state(state),
        ARCHITECTURE_SECTION=arch_section,
    )
    session = agent.session(AgentRole.REVIEWER, system_extra=prompt)
    session.send(
        "Review the implementation plan. Report findings via report_eval_finding. "
        "Use verdict 'SHIP_READY' if the plan is ready, or list issues if it needs revision.",
    )

    # Check if reviewer approved (critical_eval_passed gate set by handle_eval_finding)
    if state.gate_passed("critical_eval_passed"):
        state.pass_gate("plan_reviewed")
        # Clear the critical_eval_passed gate — it will be re-used for the real eval
        state.gates_passed.discard("critical_eval_passed")
        return True

    # Reviewer found issues — planner needs to revise
    state.exit_gate_attempts += 1
    if state.exit_gate_attempts >= config.max_plan_review_attempts:
        # Accept the plan after max review attempts
        state.pass_gate("plan_reviewed")
        state.exit_gate_attempts = 0
        return True

    # Remove plan_generated to trigger re-planning
    state.gates_passed.discard("plan_generated")
    return False


def _run_implement_phase(config: LoopConfig, state: LoopState, agent: Agent) -> bool:
    """Builder implements, verifies, and fixes."""
    prompt = load_prompt("builder",
        **_base_prompt_params(config),
        ITERATION=str(state.iteration),
        MAX_ITERATIONS=str(config.max_iterations),
        STATE_SUMMARY=_format_state_summary(state),
        MAX_FIX_ATTEMPTS=str(config.max_fix_attempts),
        BUDGET_WARNING=_format_budget_warning(config, state),
    )
    session = agent.session(AgentRole.BUILDER, system_extra=prompt)

    # Reset exit request flag before each builder session
    state.exit_requested = False

    session.send(
        "Review the current state and work through your priorities. "
        "Fix failures first, then execute pending tasks, then generate verifications.",
        task_source="agent",
    )

    # Run verification scripts independently (builder doesn't grade own work)
    _run_verifications(config, state)

    return True


def _detect_start_command(config: LoopConfig) -> str | None:
    """Scan project dir for the most likely server start command."""
    proj = config.effective_project_dir
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        if (proj / name).exists():
            return "docker compose up -d --build"
    pkg = proj / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if "start" in scripts:
                return "npm start"
            if "dev" in scripts:
                return "npm run dev"
        except (json.JSONDecodeError, OSError):
            pass
    for entry in ("app.py", "main.py", "server.py", "run.py"):
        if (proj / entry).exists():
            return f"python {entry}"
    return None


def _needs_browser_eval(state: LoopState) -> bool:
    """True if the deliverable likely has a UI that needs browser evaluation."""
    from .tools import _requires_browser_evidence
    return _requires_browser_evidence(state)


def _run_evaluate_phase(config: LoopConfig, state: LoopState, agent: Agent) -> bool:
    """Evaluator judges quality adversarially."""
    # Clear evaluation findings for a fresh evaluation pass
    state.evaluation_findings = []

    prompt = load_prompt("evaluator",
        **_base_prompt_params(config),
        STATE_SUMMARY=_format_state_summary(state),
    )

    # Add Playwright MCP for UI evaluation
    mcp_servers = {}
    extra_tools: list[str] = []
    if _needs_browser_eval(state):
        from .agent import PLAYWRIGHT_MCP_TOOLS
        mcp_servers = {
            "playwright": {
                "command": "npx",
                "args": ["@anthropic-ai/mcp-server-playwright"],
            }
        }
        extra_tools = list(PLAYWRIGHT_MCP_TOOLS)

    session = agent.session(
        AgentRole.EVALUATOR,
        system_extra=prompt,
        mcp_servers=mcp_servers,
        extra_tools=extra_tools,
    )

    # Build user message with start command hint and rejection warning
    user_msg = (
        "Evaluate the deliverable against the Vision and PRD. "
        "Report findings via report_eval_finding. "
        "Use verdict 'SHIP_READY' if ready to ship, or list issues to fix."
    )
    if _needs_browser_eval(state):
        start_cmd = _detect_start_command(config)
        if start_cmd:
            user_msg += (
                f"\n\nHint: The server can likely be started with: `{start_cmd}` "
                f"(run from `{config.effective_project_dir}`)"
            )
        user_msg += (
            "\n\nIMPORTANT: SHIP_READY will be programmatically REJECTED unless "
            "your prior findings include browser evidence (screenshots, navigation, "
            "viewport testing). You MUST open the app in a browser and test it."
        )

    state.exit_gate_attempts += 1

    # Cap evaluation attempts — force completion if evaluator keeps saying CONTINUE
    if state.exit_gate_attempts > config.max_exit_gate_attempts:
        print(f"\n  EVAL CAP REACHED: {state.exit_gate_attempts - 1} evaluations without "
              f"SHIP_READY (max {config.max_exit_gate_attempts}). Forcing completion.")
        state.pass_gate("critical_eval_passed")
        state.exit_gate_attempts = 0
        return True

    session.send(user_msg)
    return True


def _run_verifications(config: LoopConfig, state: LoopState) -> None:
    """Run verification scripts and update state with results."""
    from .testing import run_tests_parallel

    pending = [
        v for v in state.verifications.values()
        if v.status in ("pending", "failed") and v.script_path
    ]
    if not pending:
        return

    results = run_tests_parallel(pending, timeout=120)

    from .state import FailureRecord
    from datetime import datetime

    for vid, (exit_code, stdout, stderr) in results.items():
        v = state.verifications.get(vid)
        if not v:
            continue
        v.attempts += 1
        if exit_code == 0:
            v.status = "passed"
            state.regression_baseline.add(vid)
        else:
            v.status = "failed"
            v.failures.append(FailureRecord(
                timestamp=datetime.now().isoformat(),
                attempt=v.attempts,
                exit_code=exit_code,
                stdout=stdout[:2000],
                stderr=stderr[:2000],
            ))


# ---------------------------------------------------------------------------
# State formatting helpers
# ---------------------------------------------------------------------------

def _format_plan_state(state: LoopState) -> str:
    """Format task list for reviewer prompt."""
    lines = []
    for t in sorted(state.tasks.values(), key=lambda x: x.task_id):
        deps = f" (deps: {', '.join(t.dependencies)})" if t.dependencies else ""
        lines.append(f"- {t.task_id} [{t.status}]: {t.description}")
        lines.append(f"  Value: {t.value}")
        lines.append(f"  Acceptance: {t.acceptance}{deps}")
        if t.files_expected:
            lines.append(f"  Files: {', '.join(t.files_expected)}")
    return "\n".join(lines) or "(no tasks)"


def _format_state_summary(state: LoopState) -> str:
    """Format full state for builder/evaluator prompts."""
    lines = []

    # Tasks
    done = sum(1 for t in state.tasks.values() if t.status == "done")
    total = len(state.tasks)
    lines.append(f"## Tasks: {done}/{total} complete")
    for t in sorted(state.tasks.values(), key=lambda x: x.task_id):
        lines.append(f"- {t.task_id} [{t.status}]: {t.description}")
        if t.status == "blocked":
            lines.append(f"  Blocked: {t.blocked_reason}")
    lines.append("")

    # Verifications
    if state.verifications:
        passed = sum(1 for v in state.verifications.values() if v.status == "passed")
        lines.append(f"## Verifications: {passed}/{len(state.verifications)} passing")
        for v in state.verifications.values():
            lines.append(f"- {v.verification_id} [{v.status}] ({v.category})")
            if v.status == "failed" and v.last_error:
                error_preview = v.last_error[:500]
                lines.append(f"  Last error: {error_preview}")
        lines.append("")

    # VRC
    vrc = state.latest_vrc
    if vrc:
        lines.append(f"## Latest VRC: {vrc.value_score:.0%} value | {vrc.recommendation}")
        lines.append(f"  {vrc.summary}")
        if vrc.gaps:
            for gap in vrc.gaps[:5]:
                lines.append(f"  Gap: {gap.get('description', '')}")
        lines.append("")

    return "\n".join(lines)


def _format_budget_warning(config: LoopConfig, state: LoopState) -> str:
    """Generate budget warning text for builder prompt."""
    remaining = config.max_iterations - state.iteration
    if remaining <= config.max_iterations * 0.2:
        return f"WARNING: Only {remaining} iterations remaining out of {config.max_iterations}. Focus on completing critical tasks and generating verifications."
    if config.token_budget and state.total_tokens_used > config.token_budget * 0.8:
        pct = state.total_tokens_used / config.token_budget * 100
        return f"WARNING: {pct:.0f}% of token budget consumed. Economize."
    return "Budget is healthy. Proceed normally."


# ---------------------------------------------------------------------------
# Post-delivery documentation
# ---------------------------------------------------------------------------

def _precompute_doc_context(config: LoopConfig) -> str:
    """Scan project dir for existing docs, package metadata, and source tree."""
    proj = config.effective_project_dir
    lines: list[str] = []

    # Existing documentation files
    doc_files = []
    for name in ("README.md", "README.rst", "README.txt"):
        p = proj / name
        if p.exists():
            doc_files.append(str(p))
    docs_dir = proj / "docs"
    if docs_dir.is_dir():
        for f in sorted(docs_dir.rglob("*.md")):
            doc_files.append(str(f))

    if doc_files:
        lines.append("### Existing doc files (read these before writing):")
        for f in doc_files:
            lines.append(f"- `{f}`")
    else:
        lines.append("No existing documentation found.")
    lines.append("")

    # Package metadata
    for meta in ("package.json", "pyproject.toml", "setup.py", "Cargo.toml"):
        p = proj / meta
        if p.exists():
            lines.append(f"### Package metadata: `{meta}`")
            lines.append("```")
            lines.append(p.read_text(encoding="utf-8")[:2000])
            lines.append("```")
            lines.append("")
            break

    # Source file tree (max 100 entries)
    lines.append("### Source file tree:")
    lines.append("```")
    count = 0
    for f in sorted(proj.rglob("*")):
        if f.is_file() and ".loop" not in f.parts and "node_modules" not in f.parts:
            rel = f.relative_to(proj)
            lines.append(str(rel))
            count += 1
            if count >= 100:
                lines.append("... (truncated)")
                break
    lines.append("```")

    return "\n".join(lines)


def _generate_project_docs(
    config: LoopConfig, state: LoopState, agent: Agent,
) -> None:
    """Generate README, ARCHITECTURE, and ADRs after delivery. Non-blocking."""
    if not config.generate_docs:
        return
    if "docs_generated" in state.gates_passed:
        return

    print("\n  Generating project documentation...")
    try:
        doc_context = _precompute_doc_context(config)
        prompt = load_prompt("generate_docs",
            SPRINT_DIR=str(config.sprint_dir),
            PROJECT_DIR=str(config.effective_project_dir),
            DOC_CONTEXT=doc_context,
        )
        session = agent.session(AgentRole.BUILDER, system_extra=prompt)
        session.send(
            "Read the project source code and generate documentation: "
            "README.md, docs/ARCHITECTURE.md, and docs/adr/ decision records.",
        )

        state.pass_gate("docs_generated")
        state.save(config.state_file)

        from .git import git_commit
        git_commit(config, state, f"telic-loop({config.sprint}): docs generated")

        print("  Project documentation generated successfully.")

    except Exception as exc:
        print(f"  WARNING: Doc generation failed (non-blocking): {exc}")


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

_PHASE_HANDLERS = {
    "plan": _run_plan_phase,
    "review": _run_review_phase,
    "implement": _run_implement_phase,
    "evaluate": _run_evaluate_phase,
}


def _handle_phase_crash_budget(
    config: LoopConfig, state: LoopState, phase: str, crash_record: dict,
) -> bool:
    """Handle per-phase crash budget exhaustion. Returns True if loop should halt."""
    phase_crashes = state.phase_crash_counts.get(phase, 0)
    if phase_crashes < config.max_phase_crashes:
        return False

    error_kind = crash_record.get("error_kind", "retryable")
    print(f"\n  PHASE CRASH BUDGET EXHAUSTED: {phase} crashed "
          f"{phase_crashes}x consecutively ({error_kind})")

    if error_kind == "non_retryable":
        print("  Non-retryable error — halting loop")
        return True

    if phase == "review":
        print("  Forcing plan_reviewed gate (accepting plan as-is)")
        state.pass_gate("plan_reviewed")
    elif phase == "evaluate":
        print("  Forcing critical_eval_passed gate (accepting deliverable)")
        state.pass_gate("critical_eval_passed")
    elif phase == "plan" and not state.tasks:
        print("  Cannot skip empty plan — halting loop")
        return True

    state.phase_crash_counts[phase] = 0
    return False


def _run_iteration(
    config: LoopConfig, state: LoopState, agent: Agent,
    phase: str, iteration: int,
) -> bool:
    """Execute one loop iteration. Returns True if loop should halt (deliver + exit)."""
    from .git import git_commit

    print(f"\n── Iteration {iteration} ── Phase: {phase}")

    # Timing + token tracking
    inp_before = state.total_input_tokens
    out_before = state.total_output_tokens
    t0 = time.perf_counter()

    crash_record = None
    progress = False
    try:
        handler = _PHASE_HANDLERS[phase]
        progress = handler(config, state, agent)

    except RateLimitError as rle:
        # Persist the pause signal to .loop_state.json so the outer driver
        # can pick it up via the state file even if the re-raised exception
        # gets wrapped on the way out. Commit partial work to the sprint
        # branch so resume picks up cleanly. Then re-raise — the outer
        # loop catches RateLimitError, converts to QuotaExceededError, and
        # pauses the project durably (instead of the old in-process sleep
        # which blocks the driver and prevents operator-driven early resume).
        _record_quota_pause(config, state, rle)
        raise

    except Exception as exc:
        crash_record = _log_iteration_crash(config, state, phase, exc, iteration)

    # Record progress metrics
    elapsed = round(time.perf_counter() - t0, 1)
    state.record_progress(
        phase,
        "crash" if crash_record else ("progress" if progress else "no_progress"),
        progress,
        input_tokens=state.total_input_tokens - inp_before,
        output_tokens=state.total_output_tokens - out_before,
        duration_sec=elapsed,
        crash_type=crash_record.get("crash_type", "") if crash_record else "",
    )

    if progress and not crash_record:
        state.phase_crash_counts[phase] = 0

    # Check crash budget
    if crash_record and _handle_phase_crash_budget(config, state, phase, crash_record):
        generate_delivery_report(config, state)
        state.save(config.state_file)
        return True  # halt

    # Git commit after implement phases
    if phase == "implement" and progress:
        git_commit(config, state, f"telic-loop({config.sprint}): iter {iteration}")

    # Render artifacts
    render_value_checklist(config, state)
    if state.tasks:
        render_plan_snapshot(config, state)

    state.save(config.state_file)
    return False


def run_loop(config: LoopConfig, state: LoopState, agent: Agent) -> None:
    """The main loop: iterate phases until value is delivered or budget exhausted."""
    lock_path = config.sprint_dir / ".loop.lock"
    if not _acquire_lock(lock_path):
        raise RuntimeError(f"Another loop instance is running (lock: {lock_path})")

    try:
        print("\n" + "=" * 60)
        print("  TELIC LOOP V4")
        print("=" * 60)

        # Clear any stale quota-pause signal from a prior interrupted run.
        # If we're entering run_loop, the pause has been handled (either by
        # the auto-wake timer in the watchdog or by `telic-project resume`).
        _clear_quota_pause_signal(state)

        start_iter = max(state.iteration + 1, 1)
        for iteration in range(start_iter, start_iter + config.max_iterations):
            state.iteration = iteration

            if config.token_budget and state.total_tokens_used > config.token_budget:
                print(f"  TOKEN BUDGET EXHAUSTED ({state.total_tokens_used:,} tokens)")
                break

            phase = determine_phase(state)
            if phase == "complete":
                print("\n  VALUE DELIVERED — all gates passed")
                state.pass_gate("exit_gate")
                generate_delivery_report(config, state)
                _generate_project_docs(config, state, agent)
                state.save(config.state_file)
                return

            if _run_iteration(config, state, agent, phase, iteration):
                return  # halt requested by crash budget

        # Max iterations reached
        print("\n  MAX ITERATIONS REACHED — generating partial delivery report")
        generate_delivery_report(config, state)
        _generate_project_docs(config, state, agent)

    finally:
        _release_lock(lock_path)


def _record_quota_pause(
    config: LoopConfig, state: LoopState, rle: "RateLimitError",
) -> None:
    """Persist a quota-pause signal to .loop_state.json + commit partial work.

    Called when the inner loop's agent call hits an Anthropic rate limit /
    quota error. Writes the signal so the outer telic-project driver can
    convert it into a QuotaExceededError after the inner loop re-raises.

    Crucially does NOT:
      - log to .crash_log.jsonl (quota isn't a crash, it's environmental)
      - increment phase_crash_counts (would trip the per-phase crash budget)
      - increment state.iteration (no progress was made this iteration)
    """
    from datetime import datetime, timedelta

    from .git import git_commit

    # Compute reset_at from the existing parser, which already understands
    # "resets 9pm" / "resets at 21:30" patterns. Cap is enforced inside.
    wait_secs = parse_rate_limit_wait_seconds(rle)
    reset_at = datetime.now() + timedelta(seconds=wait_secs)

    # Categorise — for now we treat all RateLimitError as 'quota'; future
    # SDK error shapes (overloaded / 429 with retry-after) can map to
    # 'rate_limit' / 'overloaded' here.
    text = str(rle)
    state.crash_kind = "quota_exceeded"
    state.crash_signature_kind = "quota"
    state.quota_reset_at = reset_at.isoformat(timespec="seconds")
    state.crash_source_text = text[:1000]

    print(f"\n  API quota exhausted — pausing inner loop. "
          f"Reset at {state.quota_reset_at}.")

    state.save(config.state_file)

    # Commit any partial in-progress work on the sprint branch so resume
    # picks up cleanly. Best-effort — if there's nothing to commit (clean
    # tree) git_commit handles it.
    try:
        git_commit(
            config, state,
            f"telic-loop({config.sprint}): paused on api quota",
        )
    except Exception:
        # Don't let a commit failure mask the original quota signal.
        pass


def _clear_quota_pause_signal(state: LoopState) -> None:
    """Clear stale quota-pause fields at the start of a fresh inner-loop run.

    Once the inner loop is running again, by definition the prior pause has
    been handled (either by reset-time auto-wake or operator-driven resume).
    Clear the fields so a successful completion's .loop_state.json doesn't
    carry a misleading signal that the outer loop could mis-interpret.
    """
    if state.crash_kind == "quota_exceeded":
        state.crash_kind = ""
        state.crash_signature_kind = ""
        state.quota_reset_at = ""
        state.crash_source_text = ""


def _log_iteration_crash(
    config: LoopConfig, state: LoopState,
    phase: str, exc: Exception, iteration: int,
) -> dict:
    """Log a crash during a loop iteration and reset in_progress tasks."""
    from datetime import datetime
    import traceback
    from .agent import _sync_state
    from .errors import FailureTrail, classify_error, log_crash_jsonl

    error_kind = classify_error(exc)
    print(f"\n  CRASH in {phase} [{error_kind}]: {type(exc).__name__}: {exc}")
    traceback.print_exc()

    # Reload state from disk first — tool CLI may have written progress
    # that the in-memory state doesn't have (sync skipped on crash)
    if config.state_file.exists():
        updated = LoopState.load(config.state_file)
        saved_tokens = state.total_tokens_used
        saved_input = state.total_input_tokens
        saved_output = state.total_output_tokens
        _sync_state(state, updated)
        state.total_tokens_used = saved_tokens
        state.total_input_tokens = saved_input
        state.total_output_tokens = saved_output

    # Extract failure trail if attached by agent.py send()
    trail: FailureTrail | None = getattr(exc, "_failure_trail", None)

    # Persist full crash record to JSONL
    crash_file = config.sprint_dir / ".crash_log.jsonl"
    log_crash_jsonl(
        crash_file,
        error=exc,
        phase=phase,
        iteration=iteration,
        error_kind=error_kind,
        failure_trail=trail,
    )

    # Condensed record for in-state crash log
    crash_record = {
        "timestamp": datetime.now().isoformat(),
        "crash_type": type(exc).__name__,
        "error_kind": error_kind,
        "phase": phase,
        "iteration": iteration,
        "error": f"{type(exc).__name__}: {str(exc)[:200]}",
    }
    state.crash_log.append(crash_record)

    # Increment per-phase crash count
    state.phase_crash_counts[phase] = state.phase_crash_counts.get(phase, 0) + 1

    # Reset any in_progress tasks back to pending
    for task in state.tasks.values():
        if task.status == "in_progress":
            task.status = "pending"
    state.save(config.state_file)

    return crash_record


# ---------------------------------------------------------------------------
# Infrastructure helpers
# ---------------------------------------------------------------------------

_lock_refcount: dict[str, int] = {}


def _acquire_lock(lock_path: Path) -> bool:
    """Acquire a file-based lock. Re-entrant for the same process."""
    key = str(lock_path.resolve())
    if key in _lock_refcount:
        _lock_refcount[key] += 1
        return True

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        _lock_refcount[key] = 1
        return True
    except FileExistsError:
        try:
            pid = int(lock_path.read_text(encoding="utf-8").strip())
            if pid == os.getpid():
                _lock_refcount[key] = 1
                return True
            try:
                os.kill(pid, 0)
                return False
            except OSError:
                lock_path.unlink(missing_ok=True)
                return _acquire_lock(lock_path)
        except (ValueError, OSError):
            lock_path.unlink(missing_ok=True)
            return _acquire_lock(lock_path)


def _release_lock(lock_path: Path) -> None:
    """Release file-based lock."""
    key = str(lock_path.resolve())
    count = _lock_refcount.get(key, 0)
    if count > 1:
        _lock_refcount[key] = count - 1
        return
    _lock_refcount.pop(key, None)
    lock_path.unlink(missing_ok=True)


def _ensure_git_repo(config: LoopConfig) -> None:
    """Ensure we're in a git repository."""
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("ERROR: Not inside a git repository. Initialize one with 'git init'.")
        sys.exit(1)


def _recover_interrupted_rollback(config: LoopConfig, state: LoopState) -> None:
    """Recover from WAL if a rollback was interrupted."""
    wal_path = config.state_file.with_suffix(".rollback_wal")
    if not wal_path.exists():
        return

    print("  WARNING: Recovering from interrupted rollback...")
    try:
        wal_data = json.loads(wal_path.read_text(encoding="utf-8"))
        if wal_data.get("status") == "started":
            import subprocess
            subprocess.run(
                ["git", "reset", "--hard", wal_data["to_hash"]],
                check=True,
            )
            subprocess.run(["git", "clean", "-fd"], check=True)
            state.git.last_commit_hash = wal_data["to_hash"]
            print(f"  Recovered: reset to {wal_data['to_hash'][:8]}")
    except Exception as e:
        print(f"  WARNING: Could not recover from WAL: {e}")
    finally:
        wal_path.unlink(missing_ok=True)


def _guess_sprint_dir() -> Path | None:
    """Best-effort sprint dir from sys.argv for top-level crash logging."""
    if len(sys.argv) < 2:
        return None
    sprint = sys.argv[1]
    if "--sprint-dir" in sys.argv:
        idx = sys.argv.index("--sprint-dir")
        if idx + 1 < len(sys.argv):
            return Path(sys.argv[idx + 1])
    return Path(f"sprints/{sprint}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point with crash recovery."""
    from .errors import backoff_seconds, classify_error, log_crash_jsonl

    max_restarts = 3
    for attempt in range(1, max_restarts + 1):
        try:
            _run_main()
            return
        except SystemExit:
            raise
        except Exception as exc:
            error_kind = classify_error(exc)
            print(f"\n{'=' * 60}")
            print(f"  LOOP CRASHED (attempt {attempt}/{max_restarts}) [{error_kind}]")
            import traceback
            traceback.print_exc()
            print(f"{'=' * 60}")

            # Persist crash to JSONL if we can determine the sprint dir
            sprint_dir = _guess_sprint_dir()
            if sprint_dir:
                log_crash_jsonl(
                    sprint_dir / ".crash_log.jsonl",
                    error=exc,
                    phase="main",
                    iteration=0,
                    error_kind=error_kind,
                    extra={"restart_attempt": attempt},
                )

            if attempt < max_restarts:
                wait = backoff_seconds(attempt - 1, base=5.0, cap=60.0)
                print(f"  Restarting in {wait:.0f} seconds...")
                time.sleep(wait)
            else:
                print("  Max restarts reached.")
                sys.exit(1)


def _run_main() -> None:
    """Core loop logic — called by main() with crash recovery."""
    from .git import ensure_gitignore, setup_sprint_branch

    if len(sys.argv) < 2:
        print("Usage: telic-loop <sprint-name> [--sprint-dir <path>] [--project-dir <path>]")
        sys.exit(1)

    sprint = sys.argv[1]
    sprint_dir = Path(f"sprints/{sprint}")
    project_dir: Path | None = None

    if "--sprint-dir" in sys.argv:
        idx = sys.argv.index("--sprint-dir")
        if idx + 1 < len(sys.argv):
            sprint_dir = Path(sys.argv[idx + 1])

    if "--project-dir" in sys.argv:
        idx = sys.argv.index("--project-dir")
        if idx + 1 < len(sys.argv):
            project_dir = Path(sys.argv[idx + 1])

    config = LoopConfig(
        sprint=sprint, sprint_dir=sprint_dir, project_dir=project_dir,
    )

    config.sprint_dir.mkdir(parents=True, exist_ok=True)

    lock_path = config.sprint_dir / ".loop.lock"
    lock = _acquire_lock(lock_path)
    if not lock:
        print(f"ERROR: Another loop instance is running (lock: {lock_path})")
        sys.exit(1)

    try:
        _ensure_git_repo(config)
        ensure_gitignore(config.sprint_dir)

        if config.state_file.exists():
            state = LoopState.load(config.state_file)
            print(f"Resuming sprint '{sprint}' at iteration {state.iteration}")
        else:
            state = LoopState(sprint=sprint)
            print(f"Starting new sprint: '{sprint}'")

        _recover_interrupted_rollback(config, state)

        state.max_task_description_chars = config.max_task_description_chars
        state.max_files_per_task = config.max_files_per_task

        agent = Agent(config, state)

        if not state.git.branch_name:
            setup_sprint_branch(config, state)

        state.save(config.state_file)
        run_loop(config, state, agent)

        if state.value_delivered:
            sys.exit(0)
        elif state.latest_vrc and state.latest_vrc.value_score > 0.5:
            sys.exit(2)
        else:
            sys.exit(1)

    finally:
        _release_lock(lock_path)


if __name__ == "__main__":
    main()
