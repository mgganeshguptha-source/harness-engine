"""Print a human-readable audit summary for the most recent run.

Reads audit/<feature>/run-summary.json (written by `collect-audit`) and reports:
  - status (done / needs_input / error)
  - which phases completed and which were never reached
  - which audit files were retained
  - which expected artifacts are missing, and which phase would have produced them

Usage:
    python audit_summary.py <repo_root>

Writes to stdout; the workflow appends this to the GitHub step summary. Designed
to NEVER fail the build — any error is reported as a line, not an exception.
"""
import json
import os
import sys
import glob

ALL_PHASES = [
    "context", "prompt_steps", "coding",
    "unit_testing", "documentation", "raise_pr",
]

# Which audit artifact each phase is expected to produce.
EXPECTED_ARTIFACT = {
    "context.md": "context",
    "prompt-steps.md": "prompt_steps",
    "review.md": "code_review",
    "validation-report.txt": "unit_testing",
    "pr-body.md": "raise_pr",
}


def main(repo_root: str) -> None:
    # Audit trails now live at audit/<feature>/<run_id>/run-summary.json (a
    # per-run subfolder so re-runs don't overwrite). Match that depth first;
    # fall back to the older flat audit/<feature>/ layout for legacy trails.
    summaries = sorted(glob.glob(os.path.join(repo_root, "audit", "*", "*", "run-summary.json")))
    if not summaries:
        summaries = sorted(glob.glob(os.path.join(repo_root, "audit", "*", "run-summary.json")))
    if not summaries:
        print("- No run-summary.json was produced — the run failed before any "
              "phase completed, so no audit trail is available.")
        return

    # newest run wins (mtime), so the summary reflects the run just committed
    path = max(summaries, key=os.path.getmtime)
    adir = os.path.dirname(path)
    try:
        s = json.load(open(path, encoding="utf-8"))
    except Exception as e:  # never fail the build over a summary
        print(f"- Could not read run-summary.json: {e}")
        return

    done = s.get("completed_phases", []) or []
    missing_phases = [p for p in ALL_PHASES if p not in done]
    files = sorted(os.listdir(adir)) if os.path.isdir(adir) else []

    print(f"- **Feature:** {s.get('feature')}")
    if s.get("run_id"):
        print(f"- **Run:** `{s.get('run_id')}`")
    print(f"- **Status:** `{s.get('status')}`")
    print(f"- **Phases completed:** {', '.join(done) if done else '(none)'}")
    print(f"- **Phases NOT reached:** "
          f"{', '.join(missing_phases) if missing_phases else '(none — all complete)'}")
    print(f"- **Audit files retained:** {', '.join(files) if files else '(none)'}")

    absent = [(f, ph) for f, ph in EXPECTED_ARTIFACT.items() if f not in files]
    if absent:
        print("- **Missing artifacts (the producing phase did not run):**")
        for f, ph in absent:
            print(f"    - `{f}` — produced by the `{ph}` phase")
    else:
        print("- **All expected audit artifacts are present.**")


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    try:
        main(root)
    except Exception as e:
        print(f"- audit_summary error: {e}")
