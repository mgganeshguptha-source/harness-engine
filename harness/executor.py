"""
executor.py — the PhaseExecutor that the state machine drives.

This is where the harness's guarantees get ENFORCED around the (probabilistic)
agent runner:

  1. It builds the per-phase prompt (capability layer: your skills/instructions).
  2. It runs the agent via an injected AgentRunner (fake OR real SDK).
  3. The runner reports every file write it attempted; the executor checks each
     against is_write_allowed() for the CURRENT phase. Any out-of-bounds write =>
     BOUNDARY_VIOLATION (the interlock).
  4. It enforces the iteration cap (credit guard).
  5. It checks the phase produced its required artifact => else ARTIFACT_MISSING.

The AgentRunner is a Protocol so Phase 3 uses FakeAgentRunner (no SDK) and Phase 4
swaps in SdkAgentRunner (real Copilot) with NO change to this file or the machine.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from contracts import ExitCode
from phases import Phase
from state import RunState
from boundaries import is_write_allowed, deny_reason


@dataclass
class AgentResult:
    """What a runner reports back after attempting a phase."""
    attempted_writes: list = field(default_factory=list)  # paths it tried to write
    iterations_used: int = 1
    errored: bool = False
    error_msg: str = ""
    tokens: dict = field(default_factory=dict)  # token usage for this phase
    skills_loaded: list = field(default_factory=list)   # skills the SDK loaded
    tools_invoked: list = field(default_factory=list)    # tools/skills actually called


class AgentRunner(Protocol):
    """Runs one phase's work. Fake or real SDK both satisfy this."""
    def run(self, phase: Phase, prompt: str, run: RunState, repo_root: Path) -> AgentResult: ...


class PhaseExecutor:
    def __init__(self, runner: AgentRunner, repo_root: Path, harness_dir: Path, log=print):
        self.runner = runner
        self.repo_root = repo_root
        self.harness_dir = harness_dir
        self.log = log

    def _build_prompt(self, phase: Phase, run: RunState) -> str:
        """Capability layer. In Phase 4 this pulls in .github/skills + instructions.
        For now, a phase-specific instruction string + any rejection feedback."""
        base = f"[{phase.id}] {phase.title}\nUser story: {run.story}\n"
        if run.last_feedback and run.approvals.get(phase.id) == "rejected":
            base += f"\nPrevious attempt was REJECTED. Address this feedback:\n{run.last_feedback}\n"
        return base

    def run_phase(self, phase: Phase, run: RunState) -> ExitCode:
        # --- iteration cap (credit guard) ---
        used = run.iterations.get(phase.id, 0)
        if used >= phase.max_iterations:
            self.log(f"  ! iteration cap {phase.max_iterations} reached for {phase.id}")
            return ExitCode.ITERATION_CAP

        prompt = self._build_prompt(phase, run)

        # Deterministic setup: ensure declared dirs exist so the agent never needs
        # shell to mkdir. These are within the phase's allowed_writes by construction.
        for d in getattr(phase, "pre_create_dirs", ()):
            target = self.repo_root / d
            try:
                target.mkdir(parents=True, exist_ok=True)
                self.log(f"  [harness] ensured dir exists: {d}/")
            except Exception as e:
                self.log(f"  [harness] could not create dir {d}: {e}")

        # STALE-ARTIFACT GUARD: remove the phase's own verdict/output file before
        # the agent runs, so a fresh write is the ONLY way it can exist afterwards.
        #
        # Without this, a retry loop can silently reuse a previous attempt's file.
        # Observed in run 31235571294: attempt 1 wrote review.md with VERDICT: PASS;
        # a later coding loop re-entered code_review, the reviewer READ the existing
        # review.md, judged it still accurate, and wrote nothing ("The review file
        # already exists and contains a comprehensive VERDICT: PASS analysis").
        # The gate then correctly reported STALE VERDICT and halted — a live, correct
        # review was indistinguishable from a stale one because the artifact predated
        # the attempt. Deleting it first makes "file exists" == "reviewer wrote it
        # this attempt", which is exactly what the gate is asserting.
        #
        # Only files the phase is allowed to write are cleared, so this can never
        # remove source or a different phase's artifact.
        for rel in getattr(phase, "clear_before_run", ()):
            stale = self.repo_root / rel
            try:
                if stale.is_file():
                    stale.unlink()
                    self.log(f"  [harness] cleared stale artifact: {rel}")
            except Exception as e:
                self.log(f"  [harness] could not clear {rel}: {e}")

        # SCOPE GATE (part 1/2): snapshot which production source files exist
        # BEFORE the agent runs. After the phase we compare, so "the agent CREATED
        # this file" is a fact rather than an inference. Only needed for phases that
        # record execution (i.e. coding).
        _pre_existing_sources = set()
        if getattr(phase, "record_execution", False):
            try:
                from execution_record import snapshot_source_files
                _pre_existing_sources = snapshot_source_files(self.repo_root)
            except Exception as e:
                self.log(f"  [harness] scope snapshot failed (gate will not fire): {e}")

        result = self.runner.run(phase, prompt, run, self.repo_root)

        run.iterations[phase.id] = used + result.iterations_used

        # accumulate token usage for per-run credit reporting
        if result.tokens:
            for k, v in result.tokens.items():
                run.total_tokens[k] = run.total_tokens.get(k, 0) + (v or 0)

            # per-phase entry: this phase's in+out, plus running cumulative total
            phase_io = (result.tokens.get("input", 0) or 0) + (result.tokens.get("output", 0) or 0)
            cumulative = (run.total_tokens.get("input", 0) or 0) + (run.total_tokens.get("output", 0) or 0)
            from config import HarnessConfig as _HC
            _cfg = _HC.load(self.harness_dir)
            _model = _cfg.model_for_phase(phase.id) if _cfg else ""
            _in = result.tokens.get("input", 0) or 0
            _out = result.tokens.get("output", 0) or 0
            _cached = result.tokens.get("cache_read", 0) or 0
            _cwrite = result.tokens.get("cache_write", 0) or 0
            est = (_cfg.estimate_cost(_model, _in, _out, cached_tokens=_cached,
                                      cache_write_tokens=_cwrite)
                   if _cfg else {"credits": None, "usd": None, "partial": False})
            run.phase_token_log.append({
                "phase": phase.id,
                "model": _model,
                "phase_tokens": phase_io,
                "cumulative_tokens": cumulative,
                "input_tokens": _in,
                "output_tokens": _out,
                "cached_tokens": _cached,
                "cache_write_tokens": _cwrite,
                "est_credits": est.get("credits"),
                "est_usd": est.get("usd"),
                "included": False,   # usage-based billing: no model is free
                "partial": est.get("partial", False),
                "skills_loaded": list(getattr(result, "skills_loaded", []) or []),
                "tools_invoked": list(getattr(result, "tools_invoked", []) or []),
            })
            # Tokens only — no per-phase cost. The authoritative figure is the
            # GitHub billing-API credit delta printed once at the end of the run.
            # A token-priced per-phase guess printed alongside it just invites the
            # wrong number to be quoted, and the two disagree by up to 16%.
            self.log(f"  [tokens] {phase.id}: {phase_io} tokens "
                     f"(running total: {cumulative})")
        else:
            # No token usage reported, but the phase may still have loaded/invoked
            # skills. Record attribution so the audit trail isn't blank.
            _sk = list(getattr(result, "skills_loaded", []) or [])
            _tl = list(getattr(result, "tools_invoked", []) or [])
            if _sk or _tl:
                from config import HarnessConfig as _HC0
                _cfg0 = _HC0.load(self.harness_dir)
                run.phase_token_log.append({
                    "phase": phase.id,
                    "model": _cfg0.model_for_phase(phase.id) if _cfg0 else "",
                    "phase_tokens": 0,
                    "cumulative_tokens": (run.total_tokens.get("input", 0) or 0)
                                         + (run.total_tokens.get("output", 0) or 0),
                    "est_credits": 0.0,
                    "est_usd": 0.0,
                    "included": False,  # zero because no tokens, not a free model
                    "partial": False,
                    "skills_loaded": _sk,
                    "tools_invoked": _tl,
                })

        if result.errored:
            self.log(f"  ! runner error: {result.error_msg}")
            return ExitCode.SDK_ERROR

        # --- THE INTERLOCK: every attempted write must be in-bounds ---
        # Multi-module (backlog #10): prefix module-relative globs with the detected
        # application module, and load the always-deny exclude set (generated code).
        # Single-module repos are unchanged (module_writes is a no-op, exclude may
        # still carry the default generated-code patterns).
        try:
            from config import HarnessConfig as _HCw
            _cfgw = _HCw.load(self.harness_dir, repo_root=self.repo_root)
            _allowed = _cfgw.module_writes(phase.allowed_writes)
            _exclude = _cfgw.write_exclude
            self.log(f"  [module] target_module={_cfgw.target_module!r} "
                     f"allowed_writes={list(_allowed)} exclude={list(_exclude or [])}")
        except Exception as _e:
            import traceback
            self.log(f"  [module] detection FAILED ({_e!r}) — falling back to un-prefixed scope")
            self.log("  [module] " + traceback.format_exc().replace(chr(10), " | "))
            _allowed = phase.allowed_writes
            _exclude = None
        for w in result.attempted_writes:
            if not is_write_allowed(w, _allowed, repo_root=str(self.repo_root),
                                    exclude_globs=_exclude):
                self.log("  ! " + deny_reason(w, phase.id, _allowed,
                                              repo_root=str(self.repo_root),
                                              exclude_globs=_exclude))
                return ExitCode.BOUNDARY_VIOLATION

        # --- pinned artifact must exist ---
        if phase.required_artifact:
            artifact = self.repo_root / phase.required_artifact
            if not artifact.exists():
                self.log(f"  ! required artifact missing: {phase.required_artifact}")
                return ExitCode.ARTIFACT_MISSING

        # --- record changed MAIN source files (for the per-change coverage gate) ---
        # The coding phase writes production code under src/main/**. Capture those
        # repo-relative paths into run state so the validation gate can scope
        # coverage to ONLY the classes this run changed. Accumulate across loopbacks
        # (a coding retry may touch the same or additional files) without dupes.
        for w in result.attempted_writes:
            from execution_record import _rel_to_repo as _rel
            rel = _rel(w, self.repo_root)
            if "/src/main/" in ("/" + rel.replace("\\", "/")) and rel.endswith(".java"):
                if rel not in run.changed_main_files:
                    run.changed_main_files.append(rel)
        if run.changed_main_files:
            self.log(f"  [harness] changed main source (coverage scope): "
                     + ", ".join(run.changed_main_files))

        # --- EXECUTION RECORD: append actual-vs-approved audit to the plan ---
        if getattr(phase, "record_execution", False):
            try:
                from execution_record import append_execution_record
                plan_file = self.harness_dir / "prompt-steps.md"
                summary = append_execution_record(
                    plan_file, self.repo_root, result.attempted_writes, phase.id)

                # SCOPE GATE (part 2/2): the detection above used to be advisory —
                # it warned and let the run continue. That is how a coding phase was
                # able to invent a whole second Owner/Pet class hierarchy nobody
                # asked for (run 29182275947), poisoning every downstream phase.
                #
                # Split the additions: an unplanned EDIT to an existing file is a
                # warning (a real fix may legitimately need it); CREATING a new
                # production class the plan never approved is a hard violation.
                if summary["scope_additions"]:
                    from execution_record import classify_scope_additions
                    split = classify_scope_additions(
                        summary["scope_additions"], _pre_existing_sources)

                    if split["modified"]:
                        self.log("  [harness] ⚠ SCOPE ADDITION (unplanned edit to an "
                                 "EXISTING file — allowed, review at gate): "
                                 + ", ".join(split["modified"]))

                    if split["created"]:
                        self.log("  ! SCOPE VIOLATION — the phase CREATED production "
                                 "file(s) that the approved plan never listed:")
                        for c in split["created"]:
                            self.log(f"      + {c}   (NEW FILE, not in plan)")
                        self.log("  [harness] approved plan listed: "
                                 + (", ".join(summary["approved"]) or "(none parsed)"))
                        run.last_feedback = (
                            "SCOPE VIOLATION. You created production source files that "
                            "the approved plan never authorised:\n"
                            + "\n".join(f"  - {c}" for c in split["created"])
                            + "\n\nThe approved plan authorises ONLY:\n"
                            + "\n".join(f"  - {a}" for a in summary["approved"])
                            + "\n\nDo NOT invent new classes to work around a failing "
                              "build. If the build fails, fix the AUTHORISED files. If "
                              "the change genuinely cannot be made within the approved "
                              "scope, say so — do not expand the scope silently."
                        )
                        return ExitCode.SCOPE_VIOLATION
                else:
                    self.log("  [harness] execution record appended (scope matches plan)")

                # SECOND CHECK — duplicate classes. The plan-diff above is blind to a
                # BAD PLAN (if prompt_steps authorised model/Owner.java, creating it is
                # "in scope" by definition). But the poisonous SYMPTOM is the same
                # either way: two classes with the same name at different paths, after
                # which the reviewer and test author reason over contradictory versions
                # of the same type. Check the symptom, not just one of its causes.
                from execution_record import find_duplicate_classes, _rel_to_repo as _r
                touched = [_r(w, self.repo_root) for w in result.attempted_writes]
                dupes = find_duplicate_classes(self.repo_root, touched)
                if dupes:
                    self.log("  ! SCOPE VIOLATION — DUPLICATE CLASS(ES): the same class "
                             "name now exists at more than one path:")
                    for name, paths in dupes.items():
                        self.log(f"      {name}:")
                        for p in paths:
                            self.log(f"        - {p}")
                    run.last_feedback = (
                        "SCOPE VIOLATION — you have created DUPLICATE CLASSES. The same "
                        "class name now exists at more than one path:\n"
                        + "\n".join(f"  {n}:\n" + "\n".join(f"    - {p}" for p in ps)
                                    for n, ps in dupes.items())
                        + "\n\nThis is never correct. Every downstream phase (code review, "
                          "unit testing) will reason over contradictory versions of the same "
                          "type. Delete the duplicate you introduced and make the change in "
                          "the ONE class that already existed. Do not create a parallel class "
                          "hierarchy to work around a build failure."
                    )
                    return ExitCode.SCOPE_VIOLATION
            except Exception as e:
                self.log(f"  [harness] execution record error: {e}")

        return ExitCode.OK
