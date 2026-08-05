"""
fake_runner.py — a stand-in for the Copilot SDK. NO network, NO credits.

It simulates what an agent would do in each phase by writing plausible artifact
files into the workspace, and (crucially) it can be told to MISBEHAVE — e.g. try to
write a test file during the coding phase — so we can watch the interlock catch it.

This is the claude-shepherd FakeAgentRunner idea: the entire harness is exercised
end-to-end with zero model calls, so the deterministic shell is independently testable.
"""
from __future__ import annotations
from pathlib import Path

from phases import Phase
from state import RunState
from executor import AgentResult


class FakeAgentRunner:
    def __init__(self, misbehave_in: str | None = None, log=print):
        # misbehave_in = a phase id where the fake will attempt an out-of-bounds write
        self.misbehave_in = misbehave_in
        self.log = log

    def run(self, phase: Phase, prompt: str, run: RunState, repo_root: Path) -> AgentResult:
        writes = []
        feat_dir = repo_root / ".harness" / "features" / run.feature_id
        feat_dir.mkdir(parents=True, exist_ok=True)

        # Simulate the realistic file each phase would produce.
        if phase.id == "context":
            f = repo_root / ".harness" / "context.md"
            f.write_text(f"# Context for {run.feature_id}\n\nStory: {run.story}\n\n"
                         "Owner extends Person (firstName, lastName). Target: add getFullName().\n",
                         encoding="utf-8")
            writes.append(".harness/context.md")

        elif phase.id == "prompt_steps":
            f = repo_root / ".harness" / "prompt-steps.md"
            f.write_text("# Implementation steps\n\n1. Add getFullName() to Owner.java\n"
                         "2. Return firstName + ' ' + lastName\n\nImpacted files:\n"
                         "- src/main/java/.../Owner.java\n", encoding="utf-8")
            writes.append(".harness/prompt-steps.md")

        elif phase.id == "coding":
            writes.append("src/main/java/org/springframework/samples/petclinic/owner/Owner.java")
            self.log("  (fake) would edit Owner.java to add getFullName()")

        elif phase.id == "unit_testing":
            writes.append("src/test/java/org/springframework/samples/petclinic/owner/OwnerFullNameTests.java")
            self.log("  (fake) would add OwnerFullNameTests.java")

        elif phase.id == "documentation":
            writes.append("docs/fullname.md")
            self.log("  (fake) would write docs/fullname.md")

        elif phase.id == "raise_pr":
            self.log("  (fake) would run: gh pr create ...")

        # --- deliberate misbehaviour to prove the interlock ---
        if self.misbehave_in == phase.id:
            bad = "src/test/java/Sneaky.java" if phase.id == "coding" else "src/main/java/Sneaky.java"
            self.log(f"  (fake) MISBEHAVING: attempting out-of-bounds write {bad}")
            writes.append(bad)

        return AgentResult(attempted_writes=writes, iterations_used=1)
