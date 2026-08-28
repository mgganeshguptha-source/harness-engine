"""
halt_gates.py — the closed vocabulary for WHY a run stopped.

WHY THIS IS A FIXED SET AND NOT FREE TEXT
Free-text halt reasons do not aggregate. "review failed", "code review retries
exhausted" and "review gate" are one event stored three ways, and every query
then needs a normalisation step written by hand, retroactively, against records
whose original meaning has to be guessed at. The information needed to
disambiguate them is gone by the time anyone wants the number.

A closed set costs one line per gate at the point it halts — the gate already
knows which one it is — and makes halt_gate groupable, which is the entire
reason to record it.

THE SPLIT THAT MATTERS MOST
INFRA gates (sdk_error, config_error) are OUR problems: expired tokens, a missing
Maven wrapper, network. Every other value is the harness working correctly and
refusing to ship something. Without that distinction, "40% of runs halted" reads
as a broken pipeline when it may be 35% good gates and 5% expired tokens — two
findings that call for opposite responses.

ADDING A VALUE
Add it here, bump schema_version in the metrics record. The constraint is only
that the set is closed AT WRITE TIME; it is not frozen forever. `OTHER` exists so
an unmapped halt is visible in the data as a missing value rather than silently
polluting an existing one.
"""
from __future__ import annotations

# --- gates that stopped the run because the WORK is not acceptable ---
CLARIFICATION = "clarification"      # open [NEEDS CLARIFICATION] in context.md
FEASIBILITY = "feasibility"          # classified [BLOCKER]; this repo cannot build it
WRITE_BOUNDARY = "write_boundary"    # a phase wrote outside its allowed paths
SCOPE = "scope"                      # unplanned or duplicate class
CODE_REVIEW = "code_review"          # reviewer verdict, retries exhausted
COVERAGE = "coverage"                # below the coverage bar after retries
TEST_BUILD = "test_build"            # compilation or test failure after retries
AC_CONFORMANCE = "ac_conformance"    # acceptance criteria NOT_MET after retries
ARTIFACT_MISSING = "artifact_missing"  # a phase produced no required artifact
ITERATION_CAP = "iteration_cap"      # a phase burned its model-turn budget
PHASE_RUN_CAP = "phase_run_cap"      # one phase re-entered past the global cap

# --- gates that stopped the run because OUR PLUMBING failed ---
SDK_ERROR = "sdk_error"              # auth, network, Copilot CLI
CONFIG_ERROR = "config_error"        # bad config, missing wrapper, bad paths

# --- escape hatch ---
OTHER = "other"                      # unmapped; halt_detail carries the text

HALT_GATES = frozenset({
    CLARIFICATION, FEASIBILITY, WRITE_BOUNDARY, SCOPE, CODE_REVIEW,
    COVERAGE, TEST_BUILD, AC_CONFORMANCE, ARTIFACT_MISSING, ITERATION_CAP,
    PHASE_RUN_CAP, SDK_ERROR, CONFIG_ERROR, OTHER,
})

# Halts caused by our own plumbing rather than by the work being unacceptable.
# Report these separately: they are a reliability problem, not a quality signal.
INFRA_GATES = frozenset({SDK_ERROR, CONFIG_ERROR})

# Human-readable labels for logs and reports.
LABELS = {
    CLARIFICATION: "Story ambiguous — clarification needed",
    FEASIBILITY: "Not feasible in this repository",
    WRITE_BOUNDARY: "Phase wrote outside its allowed paths",
    SCOPE: "Unplanned or duplicate code",
    CODE_REVIEW: "Code review requested changes",
    COVERAGE: "Coverage below the required bar",
    TEST_BUILD: "Tests failed to build or pass",
    AC_CONFORMANCE: "Acceptance criteria not met",
    ARTIFACT_MISSING: "Phase produced no artifact",
    ITERATION_CAP: "Phase exhausted its turn budget",
    PHASE_RUN_CAP: "Phase re-entered too many times",
    SDK_ERROR: "Infrastructure or authentication failure",
    CONFIG_ERROR: "Configuration error",
    OTHER: "Unclassified halt",
}


def is_valid(gate: str | None) -> bool:
    return gate is None or gate in HALT_GATES


def is_infra(gate: str | None) -> bool:
    """True when the halt was our plumbing, not the work."""
    return gate in INFRA_GATES


def normalize(gate: str | None) -> str | None:
    """Coerce an unknown value to OTHER so it shows up as a gap in the data
    rather than as a new, uncountable category."""
    if gate is None:
        return None
    g = str(gate).strip().lower().replace("-", "_").replace(" ", "_")
    return g if g in HALT_GATES else OTHER


# Exit codes the state machine treats as halting, mapped to gates. Kept here
# rather than in contracts.py so the vocabulary lives in one file.
_EXIT_NAME_TO_GATE = {
    "BOUNDARY_VIOLATION": WRITE_BOUNDARY,
    "SCOPE_VIOLATION": SCOPE,
    "ITERATION_CAP": ITERATION_CAP,
    "VALIDATION_FAILED": TEST_BUILD,
    "ARTIFACT_MISSING": ARTIFACT_MISSING,
    "SDK_ERROR": SDK_ERROR,
    "CONFIG_ERROR": CONFIG_ERROR,
    "NEEDS_CLARIFICATION": CLARIFICATION,
}


def from_exit_code(code) -> str:
    """Map an ExitCode to its gate, by NAME rather than by value.

    Matching on the enum's name keeps this stable if the numeric values are ever
    renumbered — the label is what carries the meaning. An unmapped code returns
    OTHER, so a new exit code shows up in the data as an unclassified halt rather
    than being silently attributed to an existing gate.
    """
    name = getattr(code, "name", None) or str(code)
    return _EXIT_NAME_TO_GATE.get(str(name).upper(), OTHER)
