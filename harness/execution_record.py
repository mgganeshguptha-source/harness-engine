"""
execution_record.py — appends a factual EXECUTION RECORD to the prompt-steps audit
file after the coding phase, and stamps human approval at the gate.

Design contract:
  - The approved plan (Impacted Files block + steps) is NEVER mutated.
  - The harness APPENDS an EXECUTION RECORD section capturing what coding actually
    did: files touched, and any SCOPE ADDITION (touched but not in the approved
    Impacted Files block). The model cannot forge this — it's harness-written from
    the permission-handler's attempted_writes.
  - End of coding => append the factual record (regardless of approve/reject).
  - On approval => stamp "reviewed & approved" onto the latest record.

This gives a true before/after audit: planned vs executed vs reviewed.
"""
from __future__ import annotations
import re
from datetime import datetime
from pathlib import Path

RECORD_HEADER = "## --- EXECUTION RECORD (appended by harness) ---"


def _approved_paths_from_plan(plan_text: str) -> set[str]:
    """Extract file paths from the Impacted Files markdown table.
    Rows look like: | F1 | src/main/java/.../Owner.java | role |
    """
    paths = set()
    in_table = False
    for line in plan_text.splitlines():
        if "Impacted Files" in line:
            in_table = True
            continue
        if in_table:
            # table rows start with |
            if line.strip().startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                # need at least ID | path | role ; skip header/separator rows
                if len(cells) >= 2 and cells[1] and not set(cells[1]) <= {"-", " "} \
                        and cells[0].upper() not in ("ID",):
                    paths.add(_norm(cells[1]))
            elif line.strip() and not line.strip().startswith(">"):
                # left the table block
                if line.startswith("#"):
                    in_table = False
    return {p for p in paths if p and "/" in p}


def _norm(p: str) -> str:
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _rel_to_repo(path: str, repo_root: Path) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    root = str(repo_root).replace("\\", "/").rstrip("/")
    if p.lower().startswith(root.lower() + "/"):
        return p[len(root) + 1:]
    return p.lstrip("/")


def append_execution_record(plan_file: Path, repo_root: Path,
                            attempted_writes: list, phase_id: str) -> dict:
    """Append a factual execution record. Returns a summary dict (incl. scope_additions)."""
    plan_text = plan_file.read_text(encoding="utf-8") if plan_file.exists() else ""
    approved = _approved_paths_from_plan(plan_text)

    # actual files touched during coding, repo-relative, source files only
    actual = sorted({
        _rel_to_repo(w, repo_root) for w in attempted_writes
        if "/" in _norm(w) and not _norm(w).startswith(".harness")
    })
    actual_set = set(actual)

    # scope additions = touched but not in the approved plan
    additions = sorted(a for a in actual_set if a not in approved)

    ts = datetime.now().isoformat(timespec="seconds")
    lines = [
        "",
        RECORD_HEADER,
        f"- timestamp: {ts}",
        f"- phase: {phase_id}",
        f"- approved impacted files: {sorted(approved) if approved else '(none parsed)'}",
        f"- actually touched: {actual if actual else '(none)'}",
    ]
    if additions:
        lines.append(f"- ⚠ SCOPE ADDITION (touched, not in approved plan): {additions}")
        lines.append("  -> review this scope change before approving the coding phase.")
    else:
        lines.append("- scope: matches approved plan (no additions)")
    lines.append("- review status: PENDING (awaiting coding-gate approval)")
    lines.append("")

    with plan_file.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {"approved": sorted(approved), "actual": actual,
            "scope_additions": additions}


# --- SCOPE GATE ---------------------------------------------------------------
# Detection of scope additions already existed, but it was purely ADVISORY: the
# harness logged "⚠ SCOPE ADDITION recorded (review at gate)" and then carried on.
# In run 29182275947 the coding phase, told only "BUILD FAILURE", responded by
# INVENTING a parallel class hierarchy — creating
#     src/main/java/.../model/Owner.java   (a second Owner!)
#     src/main/java/.../model/Pet.java
# neither of which the story or plan ever asked for. Everything downstream then
# reasoned over two contradictory Owner classes: the reviewer flip-flopped, the
# coding agent reported "found BOTH Owner implementations", and the run halted in
# confusion. The warning fired and nothing stopped it.
#
# The gate below is deliberately NARROW. A scope addition is not automatically
# wrong — a real fix sometimes needs a helper the planner did not foresee, and
# halting on every unplanned edit would be brittle and infuriating. What is almost
# never legitimate is a coding phase CREATING A NEW PRODUCTION CLASS that the plan
# never approved. So we split them:
#
#   - MODIFIED an unplanned existing file  -> warn, allow (as before)
#   - CREATED  a new unplanned prod. file  -> SCOPE_VIOLATION, halt
#
# "Created" is decided against the filesystem state captured BEFORE the phase ran,
# so it is a fact, not a guess.

def classify_scope_additions(additions: list, pre_existing: set,
                             source_roots: tuple = ("src/main/",)) -> dict:
    """Split scope additions into created-new vs modified-existing.

    `additions`     : repo-relative paths touched but absent from the approved plan.
    `pre_existing`  : repo-relative paths that existed on disk BEFORE the phase ran.
    `source_roots`  : only paths under these prefixes count as production code.

    Returns {"created": [...], "modified": [...]}. Only `created` is a violation.
    """
    created, modified = [], []
    for a in additions:
        n = _norm(a)
        if not any(n.startswith(r) for r in source_roots):
            continue                      # not production source -> not our concern
        if n in pre_existing:
            modified.append(n)            # unplanned edit to an existing file: warn
        else:
            created.append(n)             # brand-new unplanned production file: HALT
    return {"created": sorted(created), "modified": sorted(modified)}


def snapshot_source_files(repo_root: Path,
                          source_roots: tuple = ("src/main/",)) -> set:
    """Repo-relative paths of production source files that exist RIGHT NOW.
    Captured before a phase runs so we can tell 'created' from 'modified'."""
    seen = set()
    for root in source_roots:
        base = repo_root / root
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file():
                seen.add(_norm(str(p.relative_to(repo_root))))
    return seen


def find_duplicate_classes(repo_root: Path, of_interest: list,
                           source_roots: tuple = ("src/main/",)) -> dict:
    """Detect the SAME class name existing at more than one path.

    The scope gate above only catches coding writing files the PLAN did not
    authorise. It is blind to the other half of the failure: a bad PLAN that
    authorises the wrong path in the first place. Both produce the same poisonous
    symptom — two classes with the same name (e.g. `owner/Owner.java` AND
    `model/Owner.java`) — after which the reviewer and the test author reason over
    contradictory versions of the same type and the run dissolves into confusion.

    So we check the symptom directly, not just one of its causes.

    `of_interest`: repo-relative paths the phase touched. We only flag duplicates
    involving a class this phase actually wrote — pre-existing duplicates in a repo
    are the repo's business, not ours.

    Returns {class_name: [path, path, ...]} for duplicated names only.
    """
    by_name: dict = {}
    for root in source_roots:
        base = repo_root / root
        if not base.is_dir():
            continue
        for p in base.rglob("*.java"):
            if p.is_file():
                by_name.setdefault(p.stem, []).append(
                    _norm(str(p.relative_to(repo_root))))

    touched_names = {Path(_norm(t)).stem for t in of_interest}
    return {name: sorted(paths) for name, paths in by_name.items()
            if len(paths) > 1 and name in touched_names}


def stamp_approval(plan_file: Path, approved: bool, feedback: str = "") -> None:
    """Update the latest EXECUTION RECORD's review status with the human decision."""
    if not plan_file.exists():
        return
    text = plan_file.read_text(encoding="utf-8")
    ts = datetime.now().isoformat(timespec="seconds")
    verdict = "APPROVED" if approved else "REJECTED"
    stamp = f"- review status: {verdict} by human at {ts}"
    if feedback:
        stamp += f" — feedback: {feedback}"
    # replace the LAST 'review status: PENDING' line
    pending = "- review status: PENDING (awaiting coding-gate approval)"
    idx = text.rfind(pending)
    if idx != -1:
        text = text[:idx] + stamp + text[idx + len(pending):]
        plan_file.write_text(text, encoding="utf-8")
