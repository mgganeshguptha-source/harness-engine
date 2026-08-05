"""
test_state_machine.py — proves the engine's transitions with NO SDK.

A ScriptedExecutor returns pre-programmed exit codes so we can drive the machine
through every path: happy approve-path, reject-loop, boundary-violation halt,
validation-failure halt. Zero credits, zero network.

Run:  python test_state_machine.py
"""
import tempfile
from pathlib import Path

from contracts import ExitCode
from phases import PHASES
from state import RunState
from state_machine import StateMachine


class ScriptedExecutor:
    """Returns codes from a script keyed by phase id; default OK.
    Also writes a clean context file when the context phase runs, so the
    clarification gate (which scans .github/story-context-files/) passes."""
    def __init__(self, script=None, context_clarifications=False):
        self.script = script or {}
        self.calls = []
        self.context_clarifications = context_clarifications
        self.repo_root = None  # set by tests that need the gate

    def run_phase(self, phase, run):
        self.calls.append(phase.id)
        if phase.id == "context" and self.repo_root is not None:
            cd = self.repo_root / ".github" / "story-context-files"
            cd.mkdir(parents=True, exist_ok=True)
            body = "## Section 8\n"
            body += ("- [NEEDS CLARIFICATION]: example gap\n"
                     if self.context_clarifications else "None. All criteria testable.\n")
            (cd / "context.md").write_text(body, encoding="utf-8")
        code = self.script.get(phase.id, ExitCode.OK)
        if isinstance(code, list):
            code = code.pop(0) if code else ExitCode.OK
        return code


class _FakeVR:
    def __init__(self, passed): self.passed = passed; self.summary = "FAKE"; self.exit_code = 0 if passed else 1; self.output_tail = ""

def _pass_validator(repo, hd, log): return _FakeVR(True)
def _fail_validator(repo, hd, log): return _FakeVR(False)


def _sm(ex, hd, validator=_pass_validator):
    # default to a PASSING validator so scripted tests never shell out to mvn.
    # point the executor at the temp repo so the context file (for the
    # clarification gate) is written under it. hd is <repo>/.harness, so repo = hd.parent
    ex.repo_root = hd.parent
    return StateMachine(ex, hd, repo_root=hd.parent, log=lambda *a: None, validator=validator)


def _new_run(tmp):
    return RunState(
        feature_id="PC-1",
        story="Add getFullName() to Owner",
        current_phase=PHASES[0].id,
    )


def test_happy_path_completes_with_all_approvals():
    with tempfile.TemporaryDirectory() as d:
        hd = Path(d)
        ex = ScriptedExecutor()
        sm = _sm(ex, hd)
        run = _new_run(hd)

        for _ in range(len(PHASES) * 2):
            run = sm.run_until_pause(run)
            if run.status == "awaiting_approval":
                run = sm.resolve_gate(run, approved=True)
            elif run.status in ("done", "halted"):
                break

        assert run.status == "done", run.status
        assert set(run.completed_phases) == {p.id for p in PHASES}


def test_validation_failure_loops_back_to_coding():
    """Red tests loop back to coding (not immediate halt), carrying feedback."""
    with tempfile.TemporaryDirectory() as d:
        hd = Path(d)
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "config.yaml").write_text(
            "validation_loopback_phase: coding\nmax_validation_retries: 2\n", encoding="utf-8")
        ex = ScriptedExecutor()
        sm = _sm(ex, hd, validator=_fail_validator)
        run = _new_run(hd)

        # approve context, prompt_steps, coding gates to reach unit_testing
        for _ in range(3):
            run = sm.run_until_pause(run)
            assert run.status == "awaiting_approval"
            run = sm.resolve_gate(run, approved=True)

        # next step runs unit_testing then the failing validation gate -> loopback
        run = sm.step(run)   # unit_testing executes, validation fails, loops to coding
        assert run.current_phase == "coding", run.current_phase
        assert run.validation_attempts == 1
        assert "FAILED" in (run.last_feedback or "")


def test_validation_retry_cap_halts():
    """After max retries, the run halts for a human instead of looping forever."""
    with tempfile.TemporaryDirectory() as d:
        hd = Path(d)
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "config.yaml").write_text(
            "validation_loopback_phase: coding\nmax_validation_retries: 2\n", encoding="utf-8")
        ex = ScriptedExecutor()
        sm = _sm(ex, hd, validator=_fail_validator)
        run = _new_run(hd)

        # drive many steps; always approve gates; tests always fail -> should cap out
        for _ in range(40):
            run = sm.run_until_pause(run)
            if run.status == "awaiting_approval":
                run = sm.resolve_gate(run, approved=True)
            elif run.status == "halted":
                break

        assert run.status == "halted"
        assert run.validation_attempts == 3   # 2 retries + the final over-cap attempt


def test_reject_loops_same_phase_then_proceeds():
    with tempfile.TemporaryDirectory() as d:
        hd = Path(d)
        ex = ScriptedExecutor()
        sm = _sm(ex, hd)
        run = _new_run(hd)

        # advance to first gate (context)
        run = sm.run_until_pause(run)
        assert run.status == "awaiting_approval"
        assert run.current_phase == "context"

        # reject -> should stay on context with feedback
        run = sm.resolve_gate(run, approved=False, feedback="missing edge cases")
        assert run.current_phase == "context"
        assert run.last_feedback == "missing edge cases"
        assert run.approvals["context"] == "rejected"

        # re-run context, now approve -> moves to prompt_steps
        run = sm.run_until_pause(run)
        assert run.status == "awaiting_approval"
        run = sm.resolve_gate(run, approved=True)
        assert run.current_phase == "prompt_steps"


def test_boundary_violation_halts():
    with tempfile.TemporaryDirectory() as d:
        hd = Path(d)
        ex = ScriptedExecutor({"coding": ExitCode.BOUNDARY_VIOLATION})
        sm = _sm(ex, hd)
        run = _new_run(hd)

        # drive, approving gates, until we hit the coding violation
        for _ in range(len(PHASES) * 2):
            run = sm.run_until_pause(run)
            if run.status == "awaiting_approval":
                run = sm.resolve_gate(run, approved=True)
            else:
                break

        assert run.status == "halted"
        assert run.current_phase == "coding"


def test_validation_failure_halts():
    with tempfile.TemporaryDirectory() as d:
        hd = Path(d)
        ex = ScriptedExecutor({"unit_testing": ExitCode.VALIDATION_FAILED})
        sm = _sm(ex, hd)
        run = _new_run(hd)

        for _ in range(len(PHASES) * 2):
            run = sm.run_until_pause(run)
            if run.status == "awaiting_approval":
                run = sm.resolve_gate(run, approved=True)
            else:
                break

        assert run.status == "halted"
        assert run.current_phase == "unit_testing"


def test_state_persists_and_reloads():
    with tempfile.TemporaryDirectory() as d:
        hd = Path(d)
        ex = ScriptedExecutor()
        sm = _sm(ex, hd)
        run = _new_run(hd)

        run = sm.run_until_pause(run)          # pauses at context gate
        assert run.status == "awaiting_approval"

        # reload from disk -> same place
        reloaded = RunState.load(hd)
        assert reloaded is not None
        assert reloaded.current_phase == "context"
        assert reloaded.status == "awaiting_approval"


def test_clarification_gate_halts_needs_input():
    """Unresolved [NEEDS CLARIFICATION] in context => needs_input, no prompt_steps."""
    with tempfile.TemporaryDirectory() as d:
        hd = Path(d)
        ex = ScriptedExecutor(context_clarifications=True)  # context writes a marker
        sm = _sm(ex, hd)
        run = _new_run(hd)
        run = sm.run_until_pause(run)
        # context ran, gate found a marker -> needs_input, stays on context
        assert run.status == "needs_input", run.status
        assert run.current_phase == "context"
        assert "prompt_steps" not in run.completed_phases


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
