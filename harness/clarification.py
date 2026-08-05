"""
clarification.py — the NEEDS CLARIFICATION gate.

After the context phase, the harness scans the written context file for
`[NEEDS CLARIFICATION]` markers (the contract emitted by the build-context skill
in non-interactive mode). If any remain, the run must NOT proceed to prompt_steps:
the story is ambiguous and a human must resolve it (edit the story) and re-run.

This turns the skill's "I'm unsure" into a hard harness halt — ambiguity can never
be silently guessed into code.

IMPORTANT — sentinel handling:
A clean context file often still *mentions* the marker text in a negative/summary
line, e.g.:

    _No [NEEDS CLARIFICATION] items — all clarifications were provided in the story._
    ## Section 8 — Clarifications: none.

A naive substring scan ("[NEEDS CLARIFICATION]" in line) wrongly counts those as
open items and halts a run that is actually clear. The scanner below therefore:
  1. requires the canonical form  [NEEDS CLARIFICATION]: <non-empty text>  to count
     a line as a REAL open item, and
  2. explicitly skips negation/sentinel lines ("no ...", "none", "n/a", etc.)
     even if they contain the marker substring.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

MARKER = "[NEEDS CLARIFICATION]"

# A genuine open item is the canonical contract form: the marker, then a colon,
# then at least one non-whitespace character of actual content.
#   "[NEEDS CLARIFICATION]: Should empty string return false?"   -> real
#   "[NEEDS CLARIFICATION]:"                                     -> empty, not real
#   "No [NEEDS CLARIFICATION] items were found."                 -> sentinel, not real
_REAL_MARKER = re.compile(r"\[NEEDS\s+CLARIFICATION\]\s*:\s*\S")

# Leading list/quote/emphasis punctuation we strip before classifying a line.
_LEADING = " \t-*>•_#"

# If, after stripping leading punctuation, the line STARTS with one of these,
# it's a negative/sentinel line describing the ABSENCE of clarifications.
_NEGATION_PREFIXES = (
    "no ",
    "none",
    "n/a",
    "na ",
    "nil",
    "there are no",
    "no open",
    "no outstanding",
    "no remaining",
    "no unresolved",
)


def _is_real_marker_line(line: str) -> bool:
    """True iff `line` is a genuine, unresolved [NEEDS CLARIFICATION] item.

    Filters out:
      - lines that merely mention the marker in prose ("No [NEEDS CLARIFICATION]...")
      - the bare marker with no following content
      - section headers like "## Clarifications: none"
    """
    if MARKER not in line:
        return False

    # Normalize: strip leading list/emphasis punctuation and surrounding emphasis.
    stripped = line.strip().lstrip(_LEADING).strip()
    # remove wrapping markdown emphasis so "_No ... items_" classifies as "no ..."
    unwrapped = stripped.strip("*_` ").strip()
    low = unwrapped.lower()

    # 1) explicit negation / sentinel lines -> NOT a real item
    if low.startswith(_NEGATION_PREFIXES):
        return False
    # catch "... : none." / "... : n/a" style summary lines that still contain the marker
    if re.search(r":\s*(none|n/?a|nil)\b", low):
        return False

    # 2) must match the canonical "[NEEDS CLARIFICATION]: <content>" form
    return bool(_REAL_MARKER.search(line))


@dataclass
class ClarificationResult:
    clear: bool
    scanned_file: str
    items: list = field(default_factory=list)  # the clarification lines found


def _newest_context_file(repo_root: Path, search_dir: str) -> Path | None:
    d = repo_root / search_dir
    if not d.is_dir():
        return None
    candidates = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def scan_clarifications(repo_root: Path,
                        search_dir: str = ".github/story-context-files") -> ClarificationResult:
    """Find the newest context file and extract any GENUINE [NEEDS CLARIFICATION] lines.

    A file with only a "No [NEEDS CLARIFICATION] items" sentinel line is treated as
    CLEAR (clear=True, items=[]). Only canonical "[NEEDS CLARIFICATION]: <text>" lines
    count as open items.
    """
    f = _newest_context_file(repo_root, search_dir)
    if f is None:
        # No context file at all => treat as not-clear (can't verify), surface clearly.
        return ClarificationResult(clear=False, scanned_file=f"(none in {search_dir})",
                                   items=["No context file was produced to scan."])
    text = f.read_text(encoding="utf-8", errors="replace")
    items = []
    for line in text.splitlines():
        if _is_real_marker_line(line):
            # keep the human-readable part after the marker
            cleaned = line.strip().lstrip(_LEADING).strip()
            items.append(cleaned)
    return ClarificationResult(clear=(len(items) == 0), scanned_file=str(f), items=items)
