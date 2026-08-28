"""
ac_validation.py — the AC CONFORMANCE gate.

Reads .harness/validation.md, written by the build-validation skill, and decides
whether the delivered code satisfies the acceptance criteria.

WHY THIS IS NOT THE CODE-REVIEW GATE
review.py asks whether the code that EXISTS is correct and safe; it reads a diff
and blocks on five defect classes. None of those classes can catch a criterion
that was never implemented, because nothing in the diff is wrong — the problem is
code that is absent. A well-written change can pass review with AC-4 silently
missing. This gate starts from the criteria instead and requires a verdict for
each one.

WHY THE COUNT IS CHECKED, NOT JUST THE VERDICT
The failure mode here is not a wrong verdict, it is a MISSING one: a validator
that rules on six of seven criteria and reports PASS looks identical in the log
to one that checked all seven. So the gate cross-checks the criteria found in
validation.md against the AC ids in context.md, and treats a criterion with no
verdict as unvalidated rather than satisfied. That is the same reasoning as the
prompt-steps coverage matrix — a blank cell must never read as a pass.

INCONCLUSIVE IS NOT PASS
A criterion nobody could verify is unknown, not satisfied, and it is the one most
likely to be broken precisely because nothing has ever exercised it. Whether an
INCONCLUSIVE result halts the run is configurable (validation_gate), because on
a first rollout most UNVERIFIABLE findings will be gaps in the test fixture
rather than in the code — but it defaults to halting, and the non-blocking modes
announce themselves on every run.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

# Canonical verdict line: **VERDICT: CHANGES_REQUESTED**
_VERDICT = re.compile(
    r"^[\s\-\*>\u2022_#]*(?:\*\*|__)?\s*VERDICT\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*"
    r"(PASS|CHANGES[_\- ]?REQUESTED|INCONCLUSIVE|FAIL)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Per-criterion heading: "### AC-4 — NOT_MET"  /  "### AC-2.1 - MET"
_AC_VERDICT = re.compile(
    r"^#{1,6}\s*(AC-[0-9]+(?:\.[0-9]+)?)\s*[\u2014\u2013:\-]+\s*"
    r"(MET|NOT[_\- ]?MET|UNVERIFIABLE)\b",
    re.IGNORECASE | re.MULTILINE,
)

# AC ids as they appear in context.md: "- AC-3: ..." or "AC-2.1 —"
_AC_IN_CONTEXT = re.compile(r"^[\s\-\*>\u2022]*\**\s*(AC-[0-9]+(?:\.[0-9]+)?)\s*:",
                            re.MULTILINE)
_WITHDRAWN = re.compile(r"(AC-[0-9]+(?:\.[0-9]+)?)\s*:?\s*\[WITHDRAWN\]", re.IGNORECASE)


@dataclass
class ValidationResult:
    passed: bool
    verdict: str                                    # PASS | CHANGES_REQUESTED | INCONCLUSIVE | MISSING
    scanned_file: str
    not_met: list = field(default_factory=list)     # AC ids failing
    unverifiable: list = field(default_factory=list)
    met: list = field(default_factory=list)
    unvalidated: list = field(default_factory=list)  # in context.md, no verdict here
    parse_ok: bool = True


def _norm(v: str) -> str:
    return v.upper().replace("-", "_").replace(" ", "_")


def _newest_context(repo_root: Path, search_dir: str) -> Path | None:
    d = repo_root / search_dir
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _context_ac_ids(repo_root: Path, search_dir: str) -> list:
    """AC ids declared in context.md, excluding withdrawn ones."""
    f = _newest_context(repo_root, search_dir)
    if f is None:
        return []
    text = f.read_text(encoding="utf-8", errors="replace")
    withdrawn = {m.upper() for m in _WITHDRAWN.findall(text)}
    seen, out = set(), []
    for m in _AC_IN_CONTEXT.findall(text):
        u = m.upper()
        if u in withdrawn or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def scan_validation(repo_root: Path,
                    harness_dir: Path | None = None,
                    context_dir: str = ".github/story-context-files"
                    ) -> ValidationResult:
    """Read .harness/validation.md and rule on AC conformance."""
    hd = harness_dir or (repo_root / ".harness")
    f = hd / "validation.md"
    if not f.is_file():
        return ValidationResult(
            passed=False, verdict="MISSING", parse_ok=False,
            scanned_file=str(f),
            unvalidated=_context_ac_ids(repo_root, context_dir))

    text = f.read_text(encoding="utf-8", errors="replace")

    met, not_met, unver = [], [], []
    for ac, v in _AC_VERDICT.findall(text):
        acu, vn = ac.upper(), _norm(v)
        if vn == "MET":
            met.append(acu)
        elif vn == "NOT_MET":
            not_met.append(acu)
        else:
            unver.append(acu)

    ruled = set(met) | set(not_met) | set(unver)
    declared = _context_ac_ids(repo_root, context_dir)
    # A criterion declared in context.md with no verdict here was not validated.
    # It must never be silently counted as satisfied.
    unvalidated = [a for a in declared if a not in ruled]

    m = _VERDICT.search(text)
    stated = _norm(m.group(1)) if m else None

    # The per-criterion verdicts are authoritative: a stated PASS alongside a
    # NOT_MET criterion is a contradiction, and it resolves toward caution — the
    # same deny-wins rule the feasibility and write-boundary gates use.
    if not_met:
        verdict = "CHANGES_REQUESTED"
    elif unvalidated:
        verdict = "INCONCLUSIVE"
    elif unver:
        verdict = "INCONCLUSIVE"
    elif met:
        verdict = "PASS"
    else:
        verdict = stated or "MISSING"

    return ValidationResult(
        passed=(verdict == "PASS"),
        verdict=verdict,
        scanned_file=str(f),
        not_met=not_met, unverifiable=unver, met=met, unvalidated=unvalidated,
        parse_ok=(m is not None))
