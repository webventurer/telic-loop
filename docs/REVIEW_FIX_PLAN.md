# Fix plan for the review findings

**Status:** Parked. Pick up when telic-loop is reviewed properly.
**Pairs with:** [REVIEW_FINDINGS.md](REVIEW_FINDINGS.md), which describes each finding. This doc says how to work through them. The same plan for the outer loop is in `telic-project/docs/REVIEW_FIX_PLAN.md`.

## Where things stand

- `master` is `upstream/master` (Mike Jones, to `3905439f`, 2026-05-10) with the review docs on top. It was brought up to date on 2026-09-30, picking up two commits: a quota pause that signals the outer loop (`9338e82e`), and a raised task ceiling (`3905439f`).
- `feature/stride-integration` is `master` plus three commits by Mike Mindel: "Load stride skills and Linear MCP", "Enforce /commit" and "Emit iteration progress". It was rebased onto the current `master` on 2026-09-30 with no conflicts. **It isn't pushed**, so rebasing it rewrites nothing anyone else has.
- Two remotes: `origin` is the webventurer fork, and `upstream` is Mike Jones's `memyselfmike/telic-loop`.

The findings split by where they live:

| Where | Findings |
|:--|:--|
| Already on `master` | TL-3 to TL-10, TL-13 (a decision, not a fix), TL-14, TL-15 (design) |
| Only on `feature/stride-integration` | TL-1, TL-2, TL-11, TL-12 |

**Do telic-loop before telic-project.** telic-project runs this loop, and TL-1 and TL-2 affect every telic-project run on the branch.

## Before you start

1. **Check upstream again.** Fetch it and see whether Mike Jones has fixed any findings since 2026-09-30:

   ```bash
   git fetch upstream
   git log --oneline master..upstream/master
   ```

   If there are new commits, rebase `master` onto `upstream/master`, then the stride branch onto `master`, and re-check the findings.

2. **Decide where the `master` fixes go.** If you want to offer them back to Mike Jones, they go as a PR against `memyselfmike/telic-loop`. If the fork is going its own way, merge them into the fork's `master`. The steps below work either way. Only step 4 changes.

## The process

### 1. Start a fix branch from `master`

```bash
git checkout master
git pull            # or: git merge upstream/master, if you took upstream changes
git checkout -b fix/review-findings
```

### 2. Fix the `master` findings, one commit each

Work in the order `REVIEW_FINDINGS.md` suggests. Use the `/commit` skill for each fix.

1. **TL-3:** register verification scripts, so QC stops reporting 0/0.
2. **TL-4 and TL-5:** task source; real progress signals.
3. **TL-6 and TL-7:** the evidence check; separate counters for review and evaluation.
4. **TL-8 to TL-10:** tidy-ups.
5. **TL-14:** make the docs match the code.
6. **TL-13 and TL-15:** decide rather than fix. Record each decision in `REVIEW_FINDINGS.md`.

### 3. Verify on the fix branch

Run a small sprint such as `sprints/temp-calc` in a scratch project directory. Check that `DELIVERY_REPORT.md` shows real QC counts, not 0/0, and that planned tasks are saved with `source: "plan"`.

### 4. Land the fixes

Either merge `fix/review-findings` into `master`, or open a PR against `memyselfmike/telic-loop` and merge it into the fork's `master` once it's accepted.

### 5. Rebase the stride branch on the fixed `master`

```bash
git checkout feature/stride-integration
git rebase master
```

This replays the three stride commits on top of the fixes. There's no need to redo them by hand.

Expect conflicts where both touch the same files:

- `src/telic_loop/agent.py` (session options, tool wiring);
- `src/telic_loop/main.py` (crash handler, progress, `on_event`);
- `src/telic_loop/prompts/builder.md` (verification registration and `/commit`).

Resolve each conflict, then run `git rebase --continue`.

### 6. Fix the branch-only findings as new commits on the branch

1. **TL-1:** import `sync_state`, not `_sync_state`, in `main.py`. Add a test that forces a phase crash.
2. **TL-2:** merge `.mcp.json` servers with the role's servers, so the evaluator keeps Playwright.
3. **TL-11:** emit `task_iteration_complete` on the terminal paths too.
4. **TL-12:** fall back when the project has no stride `commit` skill, or document the dependency.

### 7. Verify again, then close out

- Run the same small sprint on the branch.
- Run telic-project's `scripts/dry_run_stride.py` against it.
- Update each finding's status in `REVIEW_FINDINGS.md`.

Then move on to `telic-project/docs/REVIEW_FIX_PLAN.md`.

## The simpler alternative

You could fix everything directly on `feature/stride-integration` and skip steps 1 to 5. It's fewer steps, but the upstream fixes end up tangled with the stride work and can't be offered back cleanly. Only do this if the fork is going its own way for good.
