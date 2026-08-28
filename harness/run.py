"""
run.py — the harness CLI.

Usage:
  python run.py init   --repo <path> --feature PC-1-fullname --story "Add getFullName() to Owner"
  python run.py run    --repo <path>            # drive until a gate or completion
  python run.py approve --repo <path>           # approve current gate, continue
  python run.py reject  --repo <path> --feedback "..."   # reject, re-run phase
  python run.py status --repo <path>

Phase 3: uses FakeAgentRunner (no SDK, no credits). Add --misbehave <phase_id> to
watch the interlock halt the run on an out-of-bounds write.

Phase 4 will add  --real  to swap in the Copilot SDK runner.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from state import RunState
from state_machine import StateMachine
from executor import PhaseExecutor
from fake_runner import FakeAgentRunner


def _harness_dir(repo: Path) -> Path:
    return repo / ".harness"


def _build_machine(repo: Path, misbehave: str | None = None,
                   real: bool = False, model: str | None = None) -> StateMachine:
    if real:
        from sdk_runner import SdkAgentRunner, DEFAULT_MODEL
        runner = SdkAgentRunner(model=model or DEFAULT_MODEL)
    else:
        runner = FakeAgentRunner(misbehave_in=misbehave)
    executor = PhaseExecutor(runner, repo_root=repo, harness_dir=_harness_dir(repo))
    return StateMachine(executor, harness_dir=_harness_dir(repo))


def cmd_init(args):
    repo = Path(args.repo).resolve()
    hd = _harness_dir(repo)
    hd.mkdir(parents=True, exist_ok=True)
    from phases import PHASES

    # Story precedence: explicit --story flag, else read from the configured file
    # (which in production an MCP/Jira step would populate).
    story = getattr(args, "story", None)
    if not story:
        from config import HarnessConfig
        from story_source import FileStorySource
        cfg = HarnessConfig.load(hd)
        src = FileStorySource(repo / cfg.story_file)
        story = src.get_story()
        print(f"Read story from {cfg.story_file}")

    run = RunState(feature_id=args.feature, story=story, current_phase=PHASES[0].id)
    # Stamped once, at init, so duration measures the whole run rather than the
    # last resumed segment. GitHub LOGIN only — never the email address.
    import os
    from datetime import datetime, timezone
    run.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run.actor = os.environ.get("GITHUB_ACTOR")
    run.save(hd)
    print(f"Initialized run for '{args.feature}' at {hd}")
    print(f"First phase: {run.current_phase}")


def _load(repo: Path) -> RunState:
    run = RunState.load(_harness_dir(repo))
    if run is None:
        print("No run found. Run `init` first.")
        sys.exit(2)
    return run


def cmd_run(args):
    repo = Path(args.repo).resolve()
    run = _load(repo)
    sm = _build_machine(repo, misbehave=args.misbehave,
                        real=getattr(args, "real", False), model=getattr(args, "model", None))
    run = sm.run_until_pause(run)
    _report(run)


def cmd_autorun(args):
    """CI driver: run all phases end to end, auto-approving human gates.
    The deterministic guarantees (boundaries, validation, coverage, retry cap)
    still fully apply — only the *human* approval is automated here."""
    repo = Path(args.repo).resolve()
    run = _load(repo)
    sm = _build_machine(repo, real=True, model=getattr(args, "model", None))

    # ACTUAL credit consumption: one read before the first phase, one after the
    # last. The delta is the only trustworthy figure available — GitHub exposes an
    # account-level counter, not per-request costs, so per-phase attribution stays
    # an estimate. The delta is valid only while this account is the sole consumer
    # during the run; concurrent Copilot use elsewhere would inflate it.
    credits_before = None
    try:
        from ai_credits import read_credits_used
        credits_before = read_credits_used(log=print)
    except Exception:
        credits_before = None

    max_cycles = 50
    for _ in range(max_cycles):
        run = sm.run_until_pause(run)
        if run.status == "awaiting_approval":
            print(f"  [auto-approve] gate: {run.current_phase}")
            run = sm.resolve_gate(run, approved=True)
        elif run.status in ("done", "halted", "needs_input"):
            break

    credits_after = None
    if credits_before is not None:
        try:
            from ai_credits import read_credits_used
            credits_after = read_credits_used(log=print)
        except Exception:
            credits_after = None

    _report(run)
    _report_actual_credits(credits_before, credits_after)

    # Persist the actual credit delta onto the state so the metrics record can
    # carry it — it is computed here and nowhere else.
    if credits_before is not None and credits_after is not None:
        _d = round(credits_after - credits_before, 2)
        run.credits_actual = _d if not (credits_before == 0 and credits_after == 0) else None

    # One metrics record per run, written last and failing silently by design:
    # observability must never be the reason a build goes red.
    try:
        from config import HarnessConfig as _HCm
        import metrics as _metrics
        _metrics.emit(run, repo, _HCm.load(_harness_dir(repo)))
    except Exception as _e:
        print(f"  [metrics] not emitted ({type(_e).__name__})")
    # exit non-zero if the harness halted or needs human input (CI job fails visibly)
    if run.status in ("halted", "needs_input"):
        import sys as _sys
        _sys.exit(1)


def _report_actual_credits(before, after):
    """Print the REAL credit delta and its cost, or say plainly that it failed.

    This is the ONLY cost figure the harness reports. The per-token estimate was
    removed: two cost numbers in one report invite the wrong one being quoted, and
    this one is GitHub's own.
    """
    print()
    if before is None or after is None:
        print("  Unable to fetch the ai credits usage due to api failure, "
              "refer to github.com for actual credit used")
        print("  (github.com/settings/copilot/features -> Usage / 'Included usage'.")
        print("   Needs a fine-grained PAT with 'Plan' user permission (read);")
        print("   unavailable where Copilot is billed via an org/enterprise.)")
        return

    delta = after - before
    # A run that consumed nothing is not a real outcome — every phase calls a model.
    # Two readings of exactly 0.00 therefore mean the billing endpoint answered but
    # reported no usage for this account, not that the run was free. That happens
    # when the Copilot licence is billed through an org/enterprise (user-level usage
    # is not reported), or the plan does not itemise AI credits. Printing "0.00
    # credits" as though it were a cost figure would be actively misleading, so say
    # what it actually means.
    if before == 0 and after == 0:
        print("  Unable to fetch the ai credits usage due to api failure, "
              "refer to github.com for actual credit used")
        print("  (The billing API responded but reported no usage for this account.")
        print("   Expected when the Copilot licence is billed through an organization")
        print("   or enterprise — user-level credit reporting is unavailable there.")
        print("   The run itself was unaffected.)")
        return

    print(f"  ACTUAL AI CREDITS USED THIS RUN: {delta:.2f} "
          f"(≈ ${delta * 0.01:.4f})   [1 credit = $0.01]")
    print(f"  billing counter {before:.2f} -> {after:.2f}")
    if delta < 0:
        # Only happens across a billing-period reset mid-run.
        print("  ! negative delta — the billing period reset during this run;")
        print("    treat the figure above as unreliable and check GitHub Billing.")
    elif delta == 0:
        print("  ! zero delta — GitHub's counter may not have caught up yet.")
        print("    Re-check github.com/settings/copilot/features in a few minutes.")
    print("  Source: GitHub billing API (consumed credits, gross of the included allowance).")
    print("  Valid only if this account made no other Copilot requests during the run.")


def cmd_approve(args):
    repo = Path(args.repo).resolve()
    run = _load(repo)
    sm = _build_machine(repo)  # no execution here; runner choice irrelevant
    if run.status != "awaiting_approval":
        print(f"Nothing to approve (status: {run.status})")
        return
    run = sm.resolve_gate(run, approved=True)
    print(f"Approved. Advanced to: {run.current_phase} ({run.status})")
    print(f">>> Execute it with:  python run.py run --repo {args.repo} --real --model <model>")


def cmd_reject(args):
    repo = Path(args.repo).resolve()
    run = _load(repo)
    sm = _build_machine(repo)
    if run.status != "awaiting_approval":
        print(f"Nothing to reject (status: {run.status})")
        return
    run = sm.resolve_gate(run, approved=False, feedback=args.feedback or "")
    print(f"Rejected '{run.current_phase}'. It will re-run with your feedback on next `run`.")


def _run_id() -> str:
    """A unique-per-run identifier for the audit subfolder. In CI, GitHub's
    run id is stable and collision-free; locally we fall back to a UTC
    timestamp. This makes audit/<feature>/<run_id>/ unique per run so context,
    run-summary, and every other artifact are retained rather than overwritten."""
    import os
    from datetime import datetime, timezone
    rid = os.environ.get("GITHUB_RUN_ID")
    if rid:
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT")
        return f"{rid}-{attempt}" if attempt and attempt != "1" else rid
    return datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S")


def cmd_collect_audit(args):
    """Collect the run's audit artifacts into audit/<feature>/<run_id>/ for
    permanent retention in the repo. Gathers: the context file(s), prompt-steps.md
    (which includes the appended EXECUTION RECORD), validation-report.txt,
    pr-body.md, and a run-summary with token/cost totals. The PR step then commits
    this folder. Each run writes to its OWN run_id subfolder, so re-running the
    same feature never overwrites a prior run's trail."""
    import shutil, json
    repo = Path(args.repo).resolve()
    run = _load(repo)
    feature = run.feature_id
    run_id = _run_id()
    audit_dir = repo / "audit" / feature / run_id
    audit_dir.mkdir(parents=True, exist_ok=True)

    hd = _harness_dir(repo)
    ctx_dir = repo / ".github" / "story-context-files"

    copied = []
    # newest context file (the agent may write a timestamped name)
    if ctx_dir.is_dir():
        ctxs = sorted(ctx_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if ctxs:
            dst = audit_dir / "context.md"
            shutil.copy2(ctxs[0], dst); copied.append("context.md")
    # planning + audit files from the workspace
    for name in ("prompt-steps.md", "review.md", "validation-report.txt",
                 "pr-body.md", "capability-manifest.json"):
        src = hd / name
        if src.exists():
            shutil.copy2(src, audit_dir / name); copied.append(name)

    # a machine-readable run summary (tokens, cost, phases, status)
    summary = {
        "feature": feature,
        "run_id": run_id,
        "status": run.status,
        "completed_phases": run.completed_phases,
        "total_tokens": run.total_tokens,
        "phase_token_log": run.phase_token_log,
        # Cost is NOT derived from tokens. The authoritative figure is the
        # GitHub billing-API credit delta printed at the end of the run; token
        # counts are retained only to show which phase consumed what.
        "cost_source": {
            "is_estimate": False,
            "basis": "GitHub billing API — consumed AI credits (grossQuantity), "
                     "read once before and once after the run. 1 credit = $0.01.",
            "endpoint": "GET /users/{username}/settings/billing/ai_credit/usage",
            "caveat": "The delta is account-wide: it is accurate only while this "
                      "account makes no other Copilot requests during the run.",
            "unavailable_when": "Copilot licence billed via an org/enterprise "
                                "(user-level endpoint returns no usage), or the "
                                "token lacks 'Plan' user permission (read).",
        },
    }
    (audit_dir / "run-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    copied.append("run-summary.json")

    print(f"Collected audit trail into audit/{feature}/{run_id}/:")
    for c in copied:
        print(f"  - {c}")


def cmd_status(args):
    repo = Path(args.repo).resolve()
    run = _load(repo)
    _report(run)


def _report(run: RunState):
    print("\n--- HARNESS STATUS ---")
    print(f"feature : {run.feature_id}")
    print(f"phase   : {run.current_phase}")
    print(f"status  : {run.status}")
    print(f"done    : {run.completed_phases}")

    # Per-phase TOKEN breakdown. No cost estimate: actual credits are read from
    # GitHub's billing API before/after the run (see _report_actual_credits), so a
    # second, token-priced guess would only invite the two figures to be confused.
    # Tokens are still shown per phase because they are what makes a phase runaway
    # visible — which loop burned the budget — and that is independent of pricing.
    if run.phase_token_log:
        print("\n  token usage by phase:")
        for e in run.phase_token_log:
            model = f"[{e.get('model','')}]"
            print(f"    {e['phase']:<14}{model:<22} "
                  f"{e['phase_tokens']:>7} tok")

    # Aggregate token counts. Cost is NOT derived from these — the billed figure
    # comes from GitHub's billing API delta printed below.
    tk = run.total_tokens or {}
    tin, tout = tk.get("input", 0), tk.get("output", 0)
    if tin or tout:
        print(f"\n  totals: input={tin} output={tout} "
              f"cache_read={tk.get('cache_read', 0)} "
              f"cache_write={tk.get('cache_write', 0)} "
              f"reasoning={tk.get('reasoning', 0)}")
        print(f"          total tokens (in+out) = {tin + tout}")

    if run.status == "awaiting_approval":
        print(f"\n>>> Phase '{run.current_phase}' awaits your review.")
        print(">>> Run:  python run.py approve --repo <path>")
        print(">>>   or: python run.py reject  --repo <path> --feedback \"...\"")
    elif run.status == "halted":
        print("\n>>> HALTED by an interlock. Inspect the log above for the reason.")
    elif run.status == "needs_input":
        print("\n>>> NEEDS INPUT: the context has unresolved [NEEDS CLARIFICATION] items.")
        print(">>> Resolve them in the story, then re-run from the context phase.")
    elif run.status == "done":
        print("\n>>> All phases complete.")


def main():
    p = argparse.ArgumentParser(prog="harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init"); pi.add_argument("--repo", required=True)
    pi.add_argument("--feature", required=True)
    pi.add_argument("--story", default=None, help="story text; if omitted, read from config.story_file")
    pi.set_defaults(func=cmd_init)

    pr = sub.add_parser("run"); pr.add_argument("--repo", required=True)
    pr.add_argument("--misbehave", default=None, help="phase id to inject an out-of-bounds write")
    pr.add_argument("--real", action="store_true", help="use the live Copilot SDK (spends credits)")
    pr.add_argument("--model", default=None, help="override the model string")
    pr.set_defaults(func=cmd_run)

    pa = sub.add_parser("approve"); pa.add_argument("--repo", required=True)
    pa.add_argument("--misbehave", default=None, help="phase id to inject an out-of-bounds write on the auto-continued phase")
    pa.add_argument("--real", action="store_true", help="use the live Copilot SDK (spends credits)")
    pa.add_argument("--model", default=None, help="override the model string")
    pa.set_defaults(func=cmd_approve)

    prj = sub.add_parser("reject"); prj.add_argument("--repo", required=True)
    prj.add_argument("--feedback", default=""); prj.set_defaults(func=cmd_reject)

    ps = sub.add_parser("status"); ps.add_argument("--repo", required=True)
    ps.set_defaults(func=cmd_status)

    par = sub.add_parser("autorun"); par.add_argument("--repo", required=True)
    par.add_argument("--model", default=None, help="override the model string")
    par.set_defaults(func=cmd_autorun)

    pca = sub.add_parser("collect-audit"); pca.add_argument("--repo", required=True)
    pca.set_defaults(func=cmd_collect_audit)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
