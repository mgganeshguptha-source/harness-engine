"""
resume.py — re-enter a halted run at a chosen phase, without redoing the phases
that already passed.

THE PROBLEM THIS SOLVES
RunState has always been resumable: it persists to .harness/run-state.json and
records completed_phases + current_phase, which is exactly how the coverage and
review loopbacks already work mid-run. But every CI run starts on a clean runner
with a fresh checkout and calls `run.py init` unconditionally, which overwrites
that state. So the resume capability was built and then discarded on every run.

The visible cost: a halt at code_review forced a full restart, re-running context
(~120K tokens) and prompt_steps (~474K tokens, ~4 credits) to arrive back at the
same failure — the two phases LEAST likely to need redoing. And because those
phases are non-deterministic, the restart could produce a different plan, so the
human's fix might no longer even apply.

WHAT RESUMING MEANS
The working branch (harness-wip/<feature>) carries the source the agent wrote,
.harness/run-state.json, and the artifacts. A human fixes the code on that branch
and pushes. Re-running with resume=true checks that branch out and calls this
module, which rewinds the state to the requested phase and hands back to autorun.

WHY THE COUNTERS MUST BE RESET
A run halts at code_review precisely because review_attempts hit its cap. Resuming
without clearing that counter would re-halt on the first gate evaluation, before
the agent looked at the human's fix at all. The same applies to the per-phase run
cap. A human intervention is a genuinely new attempt, and the budgets should
reflect that.

WHY last_feedback IS CLEARED
It holds the reviewer's complaint about code that no longer exists. Feeding a
stale objection into a phase working on fixed code sends it chasing a defect that
has already been repaired.

RESUME IS NOT ALWAYS RIGHT
If the defect was upstream — an ambiguous context or a wrong plan — resuming
preserves the bad plan and the run will fail again further down. Restart is
correct when the problem is in the specification; resume is correct when it is
local to the code. That is a human judgement, which is why this is an explicit
choice at trigger time and never a default.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from phases import PHASES
from state import RunState

_PHASE_IDS = [p.id for p in PHASES]


def _phase_index(phase_id: str) -> int:
    try:
        return _PHASE_IDS.index(phase_id)
    except ValueError:
        return -1


def rewind(run: RunState, start_phase: str, log=print) -> RunState:
    """Rewind `run` so the next autorun begins at `start_phase`.

    Everything strictly BEFORE start_phase keeps its completed status; the target
    phase and everything after it are cleared, so they genuinely re-run rather
    than being skipped as already-done.
    """
    idx = _phase_index(start_phase)
    if idx < 0:
        raise SystemExit(
            f"Unknown phase '{start_phase}'. Valid phases: {', '.join(_PHASE_IDS)}"
        )

    keep = set(_PHASE_IDS[:idx])
    dropped = [p for p in run.completed_phases if p not in keep]

    run.completed_phases = [p for p in run.completed_phases if p in keep]
    run.current_phase = start_phase
    run.status = "running"

    # A human intervention is a fresh attempt: the caps that stopped the run must
    # not immediately stop it again before the agent sees the fix.
    run.review_attempts = 0
    run.validation_attempts = 0
    run.coverage_attempts = 0
    run.scope_attempts = 0

    # Per-phase iteration budgets and the global per-phase run cap, for the target
    # phase and everything after it. Phases before it keep their history.
    for key in list(run.iterations.keys()):
        pid = key.split(":", 1)[1] if key.startswith("__runs__:") else key
        if pid not in keep:
            run.iterations[key] = 0

    # Approvals for re-run phases are stale — the artifact they approved is about
    # to be regenerated.
    for pid in list(run.approvals.keys()):
        if pid not in keep:
            run.approvals.pop(pid, None)

    # Stale objection about code that may no longer exist.
    run.last_feedback = None

    log(f"  [resume] re-entering at '{start_phase}'")
    log(f"  [resume] keeping completed: {run.completed_phases or '(none)'}")
    if dropped:
        log(f"  [resume] will re-run: {dropped}")
    log("  [resume] retry budgets reset (review, validation, coverage, scope)")
    return run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Rewind a halted harness run so it resumes at a chosen phase.")
    ap.add_argument("--repo", required=True,
                    help="Path to the service repo (the one holding .harness/)")
    ap.add_argument("--phase", default=None,
                    help="Phase id to re-enter at. Defaults to where the run "
                         "halted, which is the common case after a human fixes "
                         "code and wants the same gate re-evaluated.")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    harness_dir = repo / ".harness"
    run = RunState.load(harness_dir)

    if run is None:
        print(f"  [resume] no run-state.json under {harness_dir}", file=sys.stderr)
        print("  [resume] nothing to resume — this looks like a first run. "
              "Re-trigger without resume, or check that the working branch "
              "was checked out.", file=sys.stderr)
        return 2

    print(f"  [resume] loaded state: feature={run.feature_id!r} "
          f"status={run.status!r} halted_at={run.current_phase!r}")

    target = args.phase or run.current_phase
    rewind(run, target)
    run.save(harness_dir)
    print(f"  [resume] state saved — autorun will start at '{run.current_phase}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
