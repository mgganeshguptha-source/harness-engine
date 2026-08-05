"""
contracts.py — PINNED exit-code contract for the harness.

This is the protocol the orchestrator speaks. Phases never advance on the model's
good behaviour; they advance only when the state machine returns one of these
typed codes. Treat this file as a contract: changing a number is a breaking change.

Mirrors the claude-shepherd pattern (exit codes as gate protocol).
"""
from enum import IntEnum


class ExitCode(IntEnum):
    # --- success / flow ---
    OK = 0                 # phase completed, machine may advance
    AWAITING_APPROVAL = 10  # phase produced an artifact, blocked on a human gate
    REJECTED = 11          # human rejected; loop back to the same phase with feedback

    # --- guard trips (the interlocks) ---
    BOUNDARY_VIOLATION = 12  # model tried to write outside its allowed paths
    ITERATION_CAP = 13       # phase exceeded its turn budget (credit guard)
    VALIDATION_FAILED = 14   # mvn test (or other gate) did not pass
    NEEDS_CLARIFICATION = 15  # context has unresolved [NEEDS CLARIFICATION] markers
    SCOPE_VIOLATION = 16     # created NEW production files that the plan never approved

    # --- errors ---
    ARTIFACT_MISSING = 20  # required output file/section not produced
    SDK_ERROR = 21         # the Copilot SDK / runtime errored
    CONFIG_ERROR = 22      # bad or missing config


# Human-readable labels for logging / CLI output
LABELS = {
    ExitCode.OK: "OK",
    ExitCode.AWAITING_APPROVAL: "AWAITING_APPROVAL",
    ExitCode.REJECTED: "REJECTED",
    ExitCode.BOUNDARY_VIOLATION: "BOUNDARY_VIOLATION",
    ExitCode.ITERATION_CAP: "ITERATION_CAP",
    ExitCode.VALIDATION_FAILED: "VALIDATION_FAILED",
    ExitCode.NEEDS_CLARIFICATION: "NEEDS_CLARIFICATION",
    ExitCode.SCOPE_VIOLATION: "SCOPE_VIOLATION",
    ExitCode.ARTIFACT_MISSING: "ARTIFACT_MISSING",
    ExitCode.SDK_ERROR: "SDK_ERROR",
    ExitCode.CONFIG_ERROR: "CONFIG_ERROR",
}


def label(code: ExitCode) -> str:
    return LABELS.get(code, f"UNKNOWN({int(code)})")
