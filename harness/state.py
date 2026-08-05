"""
state.py — the harness run state, persisted to .harness/run-state.json.

State is NOT ephemeral chat. A run is resumable: if the machine halts on a human
gate, the human can come back later, and the orchestrator reloads exactly where it
stopped. This is the "pinned state, not vibes" property of a harness.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class RunState:
    feature_id: str                       # e.g. "PC-1-fullname"
    story: str                            # the user story text
    current_phase: str                    # phase id we are on / paused at
    status: str = "running"               # running | awaiting_approval | halted | done
    completed_phases: list = field(default_factory=list)
    # approvals[phase_id] = "approved" | "rejected"
    approvals: dict = field(default_factory=dict)
    # last human feedback on a rejection, fed back into the phase
    last_feedback: Optional[str] = None
    # per-phase iteration counters (credit guard bookkeeping)
    iterations: dict = field(default_factory=dict)
    # how many times validation has failed and looped back (retry cap bookkeeping)
    validation_attempts: int = 0
    # how many times the per-change coverage gate has failed and looped back to
    # unit_testing (separate cap from test-failure retries).
    coverage_attempts: int = 0
    # repo-relative src/main files the coding phase actually wrote this run. Used
    # by the per-change coverage gate to scope coverage to ONLY the changed classes.
    changed_main_files: list = field(default_factory=list)
    # how many times the code-review gate has failed and looped back to coding
    # (independent budget from validation and coverage retries).
    review_attempts: int = 0
    # how many times the coding phase created production files the plan never
    # approved (scope gate). Bounded, then halt for a human.
    scope_attempts: int = 0
    # cumulative token usage across all phases (for credit estimation)
    total_tokens: dict = field(default_factory=dict)
    # per-phase token usage + the model used, in execution order, for the
    # phase-by-phase report (build_context = N tokens, running total, ...).
    phase_token_log: list = field(default_factory=list)

    # ---- persistence ----
    def save(self, harness_dir: Path) -> None:
        p = harness_dir / "run-state.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, harness_dir: Path) -> Optional["RunState"]:
        p = harness_dir / "run-state.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls(**data)
