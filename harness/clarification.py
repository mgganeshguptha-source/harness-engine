"""
clarification.py — the CONTEXT GATE (clarifications + feasibility).

After the context phase, the harness scans the written context file for the two
markers the build-context skill emits, and halts the run if either is open:

  [NEEDS CLARIFICATION]: <text>     the STORY is ambiguous
  [BLOCKER]: (CLASS) <text>         THIS REPO cannot build it

Both live in ONE scanner because they are the same mechanism — read the newest
context file, halt with status=needs_input, print the items, wait for a human.
Splitting them into two gates duplicated the file discovery, the sentinel
handling and the halt path for no gain.

They stay distinct MARKERS because the remedies differ, and telling a developer
the wrong one wastes their time:

  clarification -> a fact is missing. The BA answers it, the story is edited.
  blocker       -> nothing is unclear; the work cannot be done here as written.
                   Re-scope, sequence behind the ticket that adds the missing
                   piece, or move it to the service that owns the concern.

Emitting "[NEEDS CLARIFICATION]: CatalogClient does not exist" would be a false
statement — nothing is unclear — and the BA would have nothing to answer.

WHY THE VERDICT LINE EXISTS
`**VERDICT: GO**` / `**VERDICT: NO_GO**` is evidence that the feasibility pass
actually RAN. Without it, a file with no [BLOCKER] lines is ambiguous: it could
mean "assessed, nothing found" or "never assessed". Those are identical on disk
and very different in meaning. Clarifications need no such marker because
gap-hunting is inherent to drafting; feasibility is a separate pass that can
simply be skipped.

RESOLUTION RULES
1. Blockers win over the verdict. GO + a classified blocker => halt. Deny wins,
   as with write_exclude: a contradiction resolves toward caution.
2. NO_GO with no classified blocker => downgraded to GO and reported. Same rule
   as the code-review severity gate: an unclassified objection is an opinion,
   and an opinion may not halt a pipeline.
3. No verdict line at all => proceed, reported loudly. Context files written
   before this gate existed carry no verdict; failing them would halt every
   in-flight story the moment the gate ships.

SENTINEL HANDLING
A clean context file often MENTIONS a marker in prose ("No [NEEDS CLARIFICATION]
items", "[BLOCKER]: none"). A naive substring scan counts those as open items
and halts a run that is actually fine, so both scanners require the canonical
"MARKER: <content>" form and skip negation lines.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "[NEEDS CLARIFICATION]"
BLOCKER_MARKER = "[BLOCKER]"

# ---------------------------------------------------------------- clarifications
# A genuine open item is the canonical contract form: the marker, then a colon,
# then at least one non-whitespace character of actual content.
#   "[NEEDS CLARIFICATION]: Should empty string return false?"   -> real
#   "[NEEDS CLARIFICATION]:"                                     -> empty, not real
#   "No [NEEDS CLARIFICATION] items were found."                 -> sentinel, not real
_REAL_MARKER = re.compile(r"\[NEEDS\s+CLARIFICATION\]\s*:\s*\S")

# ---------------------------------------------------------------- feasibility
# Canonical verdict line: **VERDICT: NO_GO** / VERDICT: GO
# Tolerates markdown emphasis, list punctuation, and NO-GO / NOGO spellings.
_VERDICT = re.compile(
    r"^[\s\-\*>\u2022_#]*(?:\*\*|__)?\s*VERDICT\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*"
    r"(NO[_\- ]?GO|GO)\b",
    re.IGNORECASE | re.MULTILINE,
)
_BLOCKER_LINE = re.compile(r"^[\s\-\*>\u2022_#]*\[BLOCKER\]\s*:\s*(\S.*)$",
                           re.IGNORECASE | re.MULTILINE)
_CLASS_PREFIX = re.compile(r"^\(\s*([A-Za-z_/ \-]+?)\s*\)\s*(.*)$", re.DOTALL)

# Only these four may block. Anything else is a note.
_BLOCKING_CLASSES = {
    "MISSING_DEPENDENCY",   # needs an entity/service/method this repo lacks
    "CONTRACT_CONFLICT",    # contradicts a contract this service publishes
    "SCOPE_MISMATCH",       # the concern belongs to a different service
    "STACK_INCOMPATIBLE",   # requires a pattern the stack forbids
}
_CLASS_ALIASES = {
    "MISSING DEPENDENCY": "MISSING_DEPENDENCY",
    "DEPENDENCY": "MISSING_DEPENDENCY",
    "DEPENDENCY MISSING": "MISSING_DEPENDENCY",
    "CONTRACT": "CONTRACT_CONFLICT",
    "CONTRACT CONFLICT": "CONTRACT_CONFLICT",
    "API CONTRACT": "CONTRACT_CONFLICT",
    "SCOPE": "SCOPE_MISMATCH",
    "SCOPE MISMATCH": "SCOPE_MISMATCH",
    "WRONG SERVICE": "SCOPE_MISMATCH",
    "STACK": "STACK_INCOMPATIBLE",
    "STACK INCOMPATIBLE": "STACK_INCOMPATIBLE",
    "INCOMPATIBLE": "STACK_INCOMPATIBLE",
}

# Leading list/quote/emphasis punctuation we strip before classifying a line.
_LEADING = " \t-*>\u2022_#"

# If, after stripping leading punctuation, the line STARTS with one of these,
# it's a negative/sentinel line describing the ABSENCE of items.
_NEGATION_PREFIXES = (
    "no ", "none", "n/a", "na ", "nil",
    "there are no", "no open", "no outstanding",
    "no remaining", "no unresolved", "no blockers", "no blocking",
)


def _is_sentinel(line: str) -> bool:
    """True for a line describing the ABSENCE of items rather than an item."""
    stripped = line.strip().lstrip(_LEADING).strip()
    low = stripped.strip("*_` ").strip().lower()
    if low.startswith(_NEGATION_PREFIXES):
        return True
    # "... : none." / "... : n/a" summary lines that still contain the marker
    return bool(re.search(r":\s*(none|n/?a|nil)\b", low))


def _is_real_marker_line(line: str) -> bool:
    """True iff `line` is a genuine, unresolved [NEEDS CLARIFICATION] item.

    Filters out:
      - lines that merely mention the marker in prose ("No [NEEDS CLARIFICATION]...")
      - the bare marker with no following content
      - section headers like "## Clarifications: none"
    """
    if MARKER not in line:
        return False
    if _is_sentinel(line):
        return False
    return bool(_REAL_MARKER.search(line))


def _normalize_class(raw: str) -> str | None:
    key = raw.strip().upper().replace("-", "_")
    if key in _BLOCKING_CLASSES:
        return key
    spaced = key.replace("_", " ")
    for cand in (_CLASS_ALIASES.get(key), _CLASS_ALIASES.get(spaced)):
        if cand in _BLOCKING_CLASSES:
            return cand
    return None


def _classify_blocker(text: str):
    """Split '(CLASS) description' -> (class|None, description)."""
    m = _CLASS_PREFIX.match(text.strip())
    if not m:
        return None, text.strip()
    cls = _normalize_class(m.group(1))
    return (cls, m.group(2).strip()) if cls else (None, text.strip())


@dataclass
class ClarificationResult:
    """Outcome of the context gate.

    `clear` is the single question the state machine asks: may the run proceed?
    Everything else is for the operator reading the log.
    """
    clear: bool
    scanned_file: str
    items: list = field(default_factory=list)      # open [NEEDS CLARIFICATION]
    # --- feasibility ---
    verdict: str = "MISSING"                       # GO | NO_GO | MISSING | SKIPPED
    blockers: list = field(default_factory=list)   # classified => blocking
    advisory: list = field(default_factory=list)   # unclassified blockers / notes
    downgraded: bool = False                       # NO_GO with no valid class

    @property
    def has_verdict(self) -> bool:
        return self.verdict in ("GO", "NO_GO")


def _newest_context_file(repo_root: Path, search_dir: str) -> Path | None:
    d = repo_root / search_dir
    if not d.is_dir():
        return None
    candidates = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def scan_context(repo_root: Path,
                 search_dir: str = ".github/story-context-files",
                 check_feasibility: bool = True) -> ClarificationResult:
    """Scan the newest context file for BOTH gate markers.

    Returns clear=True only when there are no open clarifications AND no
    classified blockers. `check_feasibility=False` skips the feasibility half
    entirely (config: blocker_gate: off) while leaving clarifications enforced.
    """
    f = _newest_context_file(repo_root, search_dir)
    if f is None:
        # No context file at all => cannot verify => not clear.
        return ClarificationResult(
            clear=False,
            scanned_file=f"(none in {search_dir})",
            items=["No context file was produced to scan."],
        )

    text = f.read_text(encoding="utf-8", errors="replace")

    # ---- clarifications ----
    items = []
    for line in text.splitlines():
        if _is_real_marker_line(line):
            items.append(line.strip().lstrip(_LEADING).strip())

    res = ClarificationResult(clear=False, scanned_file=str(f), items=items)

    if not check_feasibility:
        res.verdict = "SKIPPED"
        res.clear = not items
        return res

    # ---- feasibility ----
    blocking, advisory = [], []
    for raw in _BLOCKER_LINE.findall(text):
        if _is_sentinel(raw):
            continue
        cls, desc = _classify_blocker(raw)
        if cls:
            blocking.append(f"({cls}) {desc}")
        else:
            advisory.append(desc)

    m = _VERDICT.search(text)
    if not m:
        # Rule 3: no verdict line. Proceed unless a classified blocker was
        # written anyway — a named, classified blocker is a checkable claim that
        # stands on its own, verdict line or not.
        res.verdict = "MISSING"
        res.blockers = blocking
        res.advisory = advisory
        res.clear = not items and not blocking
        return res

    raw_v = m.group(1).upper().replace("-", "_").replace(" ", "_")
    verdict = "NO_GO" if raw_v.startswith("NO") else "GO"

    if verdict == "NO_GO" and not blocking:
        # Rule 2: asserted but nothing checkable behind it.
        res.verdict = "GO"
        res.downgraded = True
        res.advisory = advisory or ["NO_GO was asserted with no [BLOCKER] line."]
        res.clear = not items
        return res

    # Rule 1: blockers win over a GO verdict.
    res.verdict = "NO_GO" if blocking else verdict
    res.blockers = blocking
    res.advisory = advisory
    res.clear = not items and not blocking
    return res


# Backwards-compatible alias: the engine imported this name before the gate
# also covered feasibility. Keeping it means an engine/toolkit version skew
# cannot break the clarification half.
scan_clarifications = scan_context
