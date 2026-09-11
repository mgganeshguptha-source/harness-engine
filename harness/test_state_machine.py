"""
test_state_machine.py — proves the engine's transitions with NO SDK.

A ScriptedExecutor returns pre-programmed exit codes so the machine can be driven
through every path: the happy approve-path, a reject loop, a boundary-violation
halt, validation failure and loopback, the retry cap, and the context gates.
Zero credits, zero network, no Maven.

Run:  python test_state_machine.py
 or:  python -m pytest test_state_machine.py -v

WRITTEN TO SURVIVE SPINE CHANGES
An earlier version of this file hardcoded the phase order and the number of
approvals needed to reach a given phase. Adding the design and validation phases
broke five tests that were not testing anything about design or validation — they
were testing that the spine still had exactly the shape they were written
against. Every test here drives the machine with a generic loop and asserts on
BEHAVIOUR (did it halt, where, with what state) rather than on positions in the
sequence. Adding a tenth phase should not break any of them.
"""
import tempfile
from pathlib import Path

from contracts import ExitCode
from phases import PHASES, phase_by_id
from state import RunState
from state_machine import StateMachine

PHASE_IDS = [p.id for p in PHASES]


# --------------------------------------------------------------------------
# Artifact stubs. Each is the minimum content that satisfies the gate reading
# it — the point is to exercise the machine's transitions, not the parsers,
# which have their own tests.
# --------------------------------------------------------------------------
def _context_body(clarifications=False, design="NO", feasibility="GO", blocker=None):
    body = "## Acceptance Criteria\n- AC-1: THE service SHALL do the thing.\n\n"
    body += "## Clarifications Needed\n"
    body += ("- [NEEDS CLARIFICATION]: example gap\n" if clarifications
             else "None. All criteria testable.\n")
    body += f"\n## Design trigger\n**DESIGN REQUIRED: {design}**\n"
    body += f"\n## Feasibility\n**VERDICT: {feasibility}**\n"
    if blocker:
        body += f"[BLOCKER]: {blocker}\n"
    return body


# Content per required artifact. prompt-steps deliberately names no source paths:
# the plan-path check only inspects paths it finds, so a plan without them passes
# trivially and keeps this file independent of any repository layout.
_ARTIFACTS = {
    ".harness/design.md": "# Design\n\n## Decisions\n### D1 - approach\n**Serves:** AC-1\n",
    ".harness/prompt-steps.md": "# Plan\n\n## Acceptance criteria coverage\n| AC-1 | Step 1 |\n",
    ".harness/review.md": "# Review\n\n**VERDICT: PASS**\nNo blocking issues.\n",
    ".harness/validation.md": "# Validation\n\n**VERDICT: PASS**\n\n### AC-1 - MET\nEvidence: stub.\n",
}


class ScriptedExecutor:
    """Returns codes from a script keyed by phase id; default OK.

    Also writes whatever artifact each phase is required to produce, so the
    artifact and gate checks see a well-formed run. Driving the machine without
    this would exercise only the failure paths.
    """

    def __init__(self, script=None, context_clarifications=False,
                 design_required="NO", feasibility="GO", blocker=None):
        self.script = script or {}
        self.calls = []
        self.context_clarifications = context_clarifications
        self.design_required = design_required
        self.feasibility = feasibility
        self.blocker = blocker
        self.repo_root = None  # set by _sm()

    def run_phase(self, phase, run):
        self.calls.append(phase.id)
        code = self.script.get(phase.id, ExitCode.OK)
        if isinstance(code, list):
            code = code.pop(0) if code else ExitCode.OK

        if self.repo_root is not None and code == ExitCode.OK:
            if phase.id == "context":
                cd = self.repo_root / ".github" / "story-context-files"
                cd.mkdir(parents=True, exist_ok=True)
                (cd / "context.md").write_text(
                    _context_body(self.context_clarifications,
                                  self.design_required, self.feasibility,
                                  self.blocker),
                    encoding="utf-8")
            art = getattr(phase, "required_artifact", None)
            if art and art in _ARTIFACTS:
                p = self.repo_root / art
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_ARTIFACTS[art], encoding="utf-8")
        return code


class _FakeVR:
    def __init__(self, passed, failure_kind="test"):
        self.passed = passed
        self.summary = "FAKE"
        self.exit_code = 0 if passed else 1
        self.output_tail = "" if passed else "[ERROR] simulated failure"
        self.failure_kind = None if passed else failure_kind


def _pass_validator(repo, hd, log):
    return _FakeVR(True)


def _fail_validator(repo, hd, log):
    return _FakeVR(False)


def _sm(ex, hd, validator=_pass_validator):
    # A PASSING validator by default so scripted tests never shell out to Maven.
    # hd is <repo>/.harness, so the repo root is its parent.
    ex.repo_root = hd.parent
    return StateMachine(ex, hd, repo_root=hd.parent, log=lambda *a: None,
                        validator=validator)


def _new_run():
    return RunState(feature_id="PC-1",
                    story="Add getFullName() to Owner",
                    current_phase=PHASES[0].id)


def _drive(sm, run, approve=True, max_steps=80):
    """Run to a terminal state, approving every human gate on the way.

    Step-bounded rather than phase-count-bounded, so loopbacks and retries do not
    exhaust the budget on a longer spine.
    """
    for _ in range(max_steps):
        run = sm.run_until_pause(run)
        if run.status == "awaiting_approval":
            run = sm.resolve_gate(run, approved=approve)
        elif run.status in ("done", "halted", "needs_input"):
            break
    return run


def _hd(d):
    """A .harness dir inside a temp repo."""
    hd = Path(d) / ".harness"
    hd.mkdir(parents=True, exist_ok=True)
    return hd


# ---- the showpiece guarantees --------------------------------------------

def test_happy_path_completes_with_all_approvals():
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        sm = _sm(ScriptedExecutor(), hd)
        run = _drive(sm, _new_run())

        assert run.status == "done", run.status
        # Every phase is accounted for. A by-exception phase that was skipped
        # still records itself complete, so the set is exhaustive either way.
        assert set(run.completed_phases) == set(PHASE_IDS), \
            sorted(set(PHASE_IDS) - set(run.completed_phases))


def test_boundary_violation_halts():
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        sm = _sm(ScriptedExecutor({"coding": ExitCode.BOUNDARY_VIOLATION}), hd)
        run = _drive(sm, _new_run())

        assert run.status == "halted", run.status
        assert run.current_phase == "coding"
        # Nothing after coding may have run: a boundary violation is not
        # something the pipeline is allowed to carry on past.
        assert "unit_testing" not in run.completed_phases


def test_reject_loops_same_phase_then_proceeds():
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        sm = _sm(ScriptedExecutor(), hd)
        run = _new_run()

        run = sm.run_until_pause(run)
        assert run.status == "awaiting_approval"
        assert run.current_phase == "context"

        run = sm.resolve_gate(run, approved=False, feedback="missing edge cases")
        assert run.current_phase == "context"
        assert run.last_feedback == "missing edge cases"
        assert run.approvals["context"] == "rejected"

        # Re-run and approve: it moves to whatever follows context in the spine,
        # rather than to a hardcoded phase name.
        run = sm.run_until_pause(run)
        assert run.status == "awaiting_approval"
        run = sm.resolve_gate(run, approved=True)
        assert run.current_phase == PHASE_IDS[PHASE_IDS.index("context") + 1]


def test_validation_failure_loops_back_rather_than_halting():
    """A red build returns work to a phase that can fix it, carrying the failure."""
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        (hd / "config.yaml").write_text(
            "validation_loopback_phase: coding\nmax_validation_retries: 2\n",
            encoding="utf-8")
        sm = _sm(ScriptedExecutor(), hd, validator=_fail_validator)
        run = _new_run()

        for _ in range(40):
            run = sm.run_until_pause(run)
            if run.validation_attempts >= 1:
                break
            if run.status == "awaiting_approval":
                run = sm.resolve_gate(run, approved=True)
            elif run.status in ("done", "halted", "needs_input"):
                break

        assert run.validation_attempts >= 1, run.validation_attempts
        # It went back to a phase that can act on the failure, rather than
        # stopping the run on the first red build.
        assert run.current_phase in ("coding", "unit_testing"), run.current_phase
        assert "FAILED" in (run.last_feedback or "").upper()


def test_validation_retry_cap_halts():
    """When the retries are spent the run halts for a human; it does not loop on."""
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        (hd / "config.yaml").write_text(
            "validation_loopback_phase: coding\nmax_validation_retries: 2\n",
            encoding="utf-8")
        sm = _sm(ScriptedExecutor(), hd, validator=_fail_validator)
        run = _drive(sm, _new_run())

        assert run.status in ("halted", "needs_input"), run.status
        # Either the validation retries ran out or the global per-phase cap
        # stopped it first. Both are correct terminations; what must never
        # happen is looping for ever.
        assert run.validation_attempts >= 1 or run.halt_gate == "phase_run_cap"


def test_clarification_gate_halts_needs_input():
    """An unresolved [NEEDS CLARIFICATION] stops the run before any code."""
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        sm = _sm(ScriptedExecutor(context_clarifications=True), hd)
        run = sm.run_until_pause(_new_run())

        assert run.status == "needs_input", run.status
        assert run.current_phase == "context"
        assert "coding" not in run.completed_phases


def test_feasibility_blocker_halts_when_gate_is_blocking():
    """A classified blocker stops the run: the story cannot be built here."""
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        (hd / "config.yaml").write_text("blocker_gate: blocking\n", encoding="utf-8")
        sm = _sm(ScriptedExecutor(
            feasibility="NO_GO",
            blocker="(MISSING_DEPENDENCY) needs a library this repo lacks."), hd)
        run = sm.run_until_pause(_new_run())

        assert run.status == "needs_input", run.status
        assert run.halt_gate == "feasibility", run.halt_gate


def test_feasibility_blocker_is_advisory_when_configured():
    """Advisory mode reports the blocker and carries on — the rollout setting."""
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        (hd / "config.yaml").write_text("blocker_gate: advisory\n", encoding="utf-8")
        sm = _sm(ScriptedExecutor(
            feasibility="NO_GO",
            blocker="(MISSING_DEPENDENCY) needs a library this repo lacks."), hd)
        run = _drive(sm, _new_run())

        assert run.status == "done", run.status


def test_design_phase_skipped_when_not_required():
    """Design runs by exception. NO means skipped, not failed."""
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        ex = ScriptedExecutor(design_required="NO")
        run = _drive(_sm(ex, hd), _new_run())

        assert run.status == "done", run.status
        assert "design" in run.completed_phases      # accounted for
        assert "design" not in ex.calls              # but never executed


def test_design_phase_runs_when_required():
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        ex = ScriptedExecutor(design_required="YES")
        run = _drive(_sm(ex, hd), _new_run())

        assert run.status == "done", run.status
        assert "design" in ex.calls


def test_state_persists_and_reloads():
    with tempfile.TemporaryDirectory() as d:
        hd = _hd(d)
        sm = _sm(ScriptedExecutor(), hd)
        run = sm.run_until_pause(_new_run())
        assert run.status == "awaiting_approval"

        reloaded = RunState.load(hd)
        assert reloaded is not None
        assert reloaded.current_phase == "context"
        assert reloaded.status == "awaiting_approval"


def test_every_phase_has_a_task_prompt():
    """A phase added to phases.py but not to the runner's prompt table used to
    die deep inside asyncio with a bare KeyError, surfaced as an SDK error —
    which sent the operator looking at Copilot rather than at the engine."""
    import inspect
    import sdk_runner
    src = inspect.getsource(sdk_runner._phase_instruction)
    missing = [pid for pid in PHASE_IDS if f'"{pid}": (' not in src]
    assert not missing, f"phases with no task prompt: {missing}"


def test_phase_ids_are_unique_and_context_runs_first():
    assert len(PHASE_IDS) == len(set(PHASE_IDS)), "duplicate phase id"
    for pid in PHASE_IDS:
        assert phase_by_id(pid) is not None, pid
    assert PHASE_IDS[0] == "context", "context must run first"


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}  {e}")
            failed += 1
        except Exception as e:
            print(f"ERROR {fn.__name__}  {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
