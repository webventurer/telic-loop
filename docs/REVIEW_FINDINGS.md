# Review findings: telic-loop v4

**Date:** 2026-09-30
**Author:** Mike Mindel + Claude (read-only analysis session)
**Scope:** `src/telic_loop/` (v4), its prompts, and the saved sprint runs (recipe-mgr-v4, recipe-mgr-v5, bookshelf, and `telic-test-projects/foo` sprints 1–3). `archive/v3/` is frozen and not covered.
**Status:** Open. To follow up when telic-loop is reviewed properly.

Found by reading code, not by running it. **Confirmed** means the cited lines were checked by hand. **Reported** means the analysis traced it but it hasn't been re-checked or reproduced. Sibling report for the outer loop: `telic-project/docs/REVIEW_FINDINGS.md`.

**Branch.** Read on `feature/stride-integration`, which is `master` plus three commits by Mike Mindel ("Load stride skills and Linear MCP", "Enforce /commit", "Emit iteration progress"). The branch column says where each finding lives. "master" means it is in Mike Jones's code on `master` and carried into the branch.

## Summary

| ID | Severity | Finding | Status | Branch |
|:--|:--|:--|:--|:--|
| TL-1 | High | Crash handler imports a renamed function | Confirmed | feature/stride-integration only ("Load stride skills and Linear MCP") |
| TL-2 | High | A project `.mcp.json` removes the evaluator's browser | Confirmed | feature/stride-integration only ("Load stride skills and Linear MCP") |
| TL-3 | High | Independent verification never runs | Confirmed | master |
| TL-4 | Medium | Task source is lost, so planned tasks count as agent tasks | Confirmed | master |
| TL-5 | Medium | Implement and evaluate always report progress | Confirmed | master |
| TL-6 | Medium | Browser-evidence check passes on keywords alone | Reported | master |
| TL-7 | Medium | Review rejections eat into the evaluation budget | Reported | master |
| TL-8 | Low | SHIP_READY with blocking findings skips re-evaluation | Reported | master |
| TL-9 | Low | VRC gaps still create tasks | Reported | master |
| TL-10 | Low | Dead signals, config and code paths | Reported | master |
| TL-11 | Low | `on_event` misses the terminal iterations | Reported | feature/stride-integration only ("Emit iteration progress") |
| TL-12 | Low | `/commit` is mandated even without the stride skill | Reported | feature/stride-integration only ("Enforce /commit") |
| TL-13 | Note | "Read-only" roles have Bash under `bypassPermissions` | Confirmed | master; Bash roles predate the branch |
| TL-14 | Docs | Docs describe v3 or claim unbuilt features | Reported | master |
| TL-15 | Design | V4 principles not yet realised | Reported | master |

## Findings

### TL-1 · High · Crash handler imports a renamed function

**Where:** `src/telic_loop/main.py:696`; `src/telic_loop/agent.py:433`
**Introduced:** "Load stride skills and Linear MCP" (2026-05-03), which renamed `_sync_state` to `sync_state`.

`_log_iteration_crash` does `from .agent import _sync_state`, but `agent.py` now defines only `sync_state`. Every phase crash raises `ImportError` inside the crash path. That disables:

- crash logging;
- the per-phase crash budget;
- the reset of in-progress tasks.

It also escalates to a full process restart. Under telic-project the error escapes `_execute_inner_loop`, which only retries `CLINotFoundError`.

**Fix:** import `sync_state`. Add a test that forces a phase crash.

### TL-2 · High · A project `.mcp.json` removes the evaluator's browser

**Where:** `src/telic_loop/agent.py`, in the `mcp_config` expression before `ClaudeAgentOptions`
**Introduced:** "Load stride skills and Linear MCP"

```python
mcp_config = str(mcp_path) if mcp_path.exists() else (self.mcp_servers if self.mcp_servers else {})
```

When the project has a `.mcp.json`, it *replaces* the role's MCP servers, including the evaluator's Playwright server. telic-project syncs in a `.mcp.json` that contains only Linear servers, so under telic-project the evaluator has no browser. The `mcp__playwright__*` names are still in `allowed_tools`, and the evidence check (TL-6) still passes on curl output.

**Fix:** merge the servers from `.mcp.json` with the role's `mcp_servers` instead of choosing one.

### TL-3 · High · Independent verification never runs

**Where:** no call site constructs `VerificationState(` (grep finds only the dataclass); `main.py:258-290` (`_run_verifications`); `prompts/builder.md:98`

The builder writes `.loop/verifications/*.sh` and is told to "register it via `manage_task` … OR note it in your completion report". That creates a *task*, not a verification. So `state.verifications` stays `{}` and `_run_verifications` runs nothing.

Every saved v4 delivery report shows **"QC checks: 0/0 passing"**. The README's "the builder never grades its own work" is not true today.

**Fix:** after each implement iteration, discover `.loop/verifications/*.sh` and register them. Alternatively, add a `register_verification` tool.

### TL-4 · Medium · Task source is lost, so planned tasks count as agent tasks

**Where:** `main.py:91` passes `task_source="plan"`; `agent.py:237` stores it as `state._current_task_source`, which is not a dataclass field; `tool_cli.py:35` defaults to `"agent"`; the tool instructions never pass `--task-source`

Every planned task is saved with `source: "agent"`, as the `foo` runs confirm. Two consequences:

- **The mid-loop ceiling applies to the plan itself.** The open-task ceiling (`MID_LOOP_TASK_CEILING`, `tools.py:166, 232-233`) exists to stop mid-loop growth, but it caps the plan. Upstream `3905439f` raised it from 15 to 20, which eases the symptom without fixing the cause. Bookshelf's plan had exactly 15 tasks under the old limit.
- **Descoping a planned task can never succeed.** Descoping requires source `course_correction` or `exit_gate` (`tools.py:397-403`), and nothing produces those sources.

The same bug exists in v3.

**Fix:** pass the source through an environment variable that `tool_cli` reads, or add `--task-source` to the tool CLI instructions for each role.

### TL-5 · Medium · Implement and evaluate always report progress

**Where:** `main.py:163`, `main.py:255`

Both handlers return `True` unconditionally. There is no stuck detection, so the implement phase is bounded only by `max_iterations` (200). This contradicts the v3 post-mortem's lesson 3: "'Progress' must mean actual progress". The `progress` field emitted through `on_event` therefore carries no signal.

**Fix:** derive progress from state changes, such as tasks completed or verifications newly passing. Add a no-progress bound.

### TL-6 · Medium · Browser-evidence check passes on keywords alone

**Where:** `tools.py`, `_validate_ship_ready`

SHIP_READY needs at least 2 findings with evidence longer than 20 characters, and at least 1 whose evidence contains 2 keywords from a set that includes `localhost`, `http://`, `browser`, `ui` and `visible`. In `foo` sprint 3 this evidence passed: "Browser evidence: Fetched http://localhost:5173/ via curl". The check is also applied to the reviewer's SHIP_READY, but `reviewer.md` never mentions evidence.

**Fix:** require a screenshot artefact or a Playwright tool call in the session, and exempt the review phase.

### TL-7 · Medium · Review rejections eat into the evaluation budget

**Where:** `main.py:119-132`; the evaluate path increments the same `exit_gate_attempts`

Plan review and critical evaluation share one counter. In `foo` sprint 1, one evaluation ran but `exit_gate_attempts` ended at 2. Review also borrows the `critical_eval_passed` gate to detect approval.

**Fix:** give each its own counter and gate.

### TL-8 · Low · SHIP_READY with blocking findings skips re-evaluation

When the evaluator returns SHIP_READY while also filing critical or blocking findings, the gate passes. The builder then fixes the resulting CE tasks, and the sprint completes without anyone evaluating those fixes.

**Fix:** reject SHIP_READY while critical or blocking findings are open, or clear the gate when CE tasks are added.

### TL-9 · Low · VRC gaps still create tasks

**Where:** `tools.py:547-612` (`handle_vrc`)

The v4 analysis ("VRC scores, doesn't create work") says the VRC should score gaps, not create work. It still turns each gap into a `VRC-*` task.

**Fix:** decide which behaviour is intended, then either remove the task creation or update the principle.

### TL-10 · Low · Dead signals, config and code paths

| Item | Where | Issue |
|:--|:--|:--|
| `request_exit` | set by the tool; `main.py:152` only resets it | Nothing reads it |
| `--no-docs` | README, skill; parsed only in `run_e2e.py:28` | The CLI ignores it |
| `max_crash_restarts` | `config.py` | Unused; `main.py:783` hard-codes 3 |
| `max_fix_attempts` | `config.py` | Enforced only as prompt text |
| `create_checkpoint`, `execute_rollback` | `git.py:99-193` | No callers, so WAL recovery (`main.py:739-760`) guards a path that can't start |
| `TaskState.retry_count` | `state.py` | Never used; the V4 per-task cap of 5 isn't built |
| `context.unresolved_questions`, `value_proofs`, `verification_strategy` | discovery | Recorded, never read |

### TL-11 · Low · `on_event` misses the terminal iterations

**Where:** `main.py:495-571`

No `task_iteration_complete` fires on:

- a rate-limit return;
- a crash-budget halt;
- the complete phase;
- `max_iterations`.

The CLI doesn't pass `on_event`, so it only works when telic-project drives the loop.

### TL-12 · Low · `/commit` is mandated even without the stride skill

**Where:** `prompts/builder.md:217`; `agent.py:63-81` (the `block_bare_git_commit` hook)

When the target project doesn't have the stride `commit` skill, the builder is told to use a skill it can't load, and bare `git commit` is blocked. The orchestrator's own `iter N`, delivery and docs commits still go through subprocess `git` (`git.py:89-96`), so there are two commit streams.

**Fix:** check for the skill and fall back, or document the dependency.

### TL-13 · Note · "Read-only" roles have Bash under `bypassPermissions`

**Where:** `agent.py:36-38, 322`

The reviewer and evaluator are described as read-only but have Bash, and every session runs with `permission_mode="bypassPermissions"`. Read-only is an instruction, not an enforcement. The evaluator is told to run `docker compose up` itself. This is worth a deliberate decision rather than a fix.

### TL-14 · Docs · Docs describe v3 or claim unbuilt features

- `docs/LOOP_FLOW.md` and `docs/FUTURE_IMPROVEMENTS.md` still describe v3: the pre-loop, `phases/*.py` and `--docker-mode`.
- README: "The builder never grades its own work". See TL-3.
- `docs/OUTER_LOOP_DESIGN.md` lists "Verification scripts | Implemented" (see TL-3) and says the design is "not implemented", but `telic-project` builds its phases 1 and 3.
- `CLAUDE.md` role table: says the builder has "Full" tools; it also has web tools.

### TL-15 · Design · V4 principles not yet realised

From `docs/V4_DESIGN_PRINCIPLES.md`:

- **P2:** the "10 tasks and 0 verifications" guardrail. Relates to TL-3.
- **P4:** the four-way value-gated termination. In practice it is one gate.
- **P6:** `scaffolding_level`.
- **P7:** the per-task iteration cap of 5 and the 2M token default. The critical evaluation is force-passed after 3 attempts.
- **P9:** `request_help`. `system.md:47` invites escalation, but session text replies are discarded.

v4 has also grown from 2,595 to about 3,400 lines of Python since `c6db333e`, against P10's "Earn Every Line of Code".

## Suggested order

1. TL-1, TL-2 and TL-3. These three break crash handling, browser evaluation and verification today.
2. TL-4 and TL-5, which are small fixes with wide effects.
3. TL-6 and TL-7, before trusting any SHIP_READY.
4. The rest during the full review.
