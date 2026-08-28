"""
blocked.py — a phase declares a change it is not permitted to make.

THE PROBLEM
Some legitimate stories need a change no phase is allowed to make. The clearest
case is a new dependency: the work needs a library, the library goes in pom.xml,
and pom.xml sits outside src/main so the coding phase's write boundary refuses
it. The phase that could add it is the phase that is blocked from adding it.

Before this existed the run simply thrashed. The coding agent would write code
against a library that was not on the classpath, the build would fail on a
missing package, the failure would loop straight back to the same phase, and the
run ended at the retry cap having burned three coding attempts on a problem no
amount of coding could solve. Observed on run 33167xxxxx: ~74 credits, and a
final message about coverage thresholds that pointed nowhere near the cause.

THE ANSWER IS NOT TO WEAKEN THE BOUNDARY
Letting an agent add third-party dependencies to a healthcare codebase
unreviewed is precisely what the boundary exists to prevent, and at BCBSM a
dependency change goes through its own approval regardless. Nor is the answer to
rewrite stories until they avoid the limitation — that fits the work to the tool.

THE ANSWER IS TO ASK
A phase that cannot proceed writes .harness/blocked.md — inside its own allowed
paths, so no boundary is breached — saying what it needs and why. The harness
reads it, halts, and tells the developer exactly what to change by hand and how
to resume. The human does the one thing only a human is allowed to do, and the
run continues from where it stopped rather than starting over.

Declaration beats breach: an agent that asks can be answered, an agent that
works around the rule cannot be trusted with the rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

BLOCKED_FILE = ".harness/blocked.md"

# Canonical declaration line: **BLOCKED: MANUAL_CHANGE_REQUIRED**
_DECL = re.compile(
    r"^[\s\-\*>\u2022_#]*(?:\*\*|__)?\s*BLOCKED\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*"
    r"(MANUAL[_\- ]?CHANGE[_\- ]?REQUIRED)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FIELD = re.compile(r"^[\s\-\*>\u2022]*\**\s*(NEEDS|FILE|WHY|SUGGESTED)\s*\**\s*:\s*(\S.*)$",
                    re.IGNORECASE | re.MULTILINE)

# Paths that commonly need a human because no phase may write them. Used only to
# recognise the situation when a phase breached instead of declaring — the
# declaration itself carries no path whitelist.
_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
                "package.json", "requirements.txt", "go.mod", "Cargo.toml")


@dataclass
class BlockedDeclaration:
    declared: bool = False
    needs: str = ""            # what is required, in one line
    file: str = ""             # which file a human must change
    why: str = ""              # why the phase could not do it
    suggested: str = ""        # the concrete change, if the phase proposed one
    raw: str = ""
    fields: dict = field(default_factory=dict)


def read_declaration(repo_root: Path) -> BlockedDeclaration:
    """Read .harness/blocked.md if a phase wrote one this attempt."""
    f = repo_root / BLOCKED_FILE
    if not f.is_file():
        return BlockedDeclaration()
    text = f.read_text(encoding="utf-8", errors="replace")
    if not _DECL.search(text):
        # A file with no canonical marker is notes, not a declaration. Ignoring it
        # is safer than halting a run on a stray file.
        return BlockedDeclaration()
    fields = {k.upper(): v.strip() for k, v in _FIELD.findall(text)}
    return BlockedDeclaration(
        declared=True,
        needs=fields.get("NEEDS", ""),
        file=fields.get("FILE", ""),
        why=fields.get("WHY", ""),
        suggested=fields.get("SUGGESTED", ""),
        raw=text.strip(),
        fields=fields,
    )


def looks_like_build_file(paths) -> str | None:
    """Return the first refused path that is a build file, if any.

    Covers the case where a phase breached instead of declaring: the denial is
    still recognisably 'this needs a human', and the operator gets the same
    actionable message rather than a bare boundary violation.
    """
    for p in paths or []:
        name = str(p).replace("\\", "/").rsplit("/", 1)[-1]
        if name in _BUILD_FILES:
            return str(p)
    return None


def halt_message(feature_id: str, phase_id: str, decl: BlockedDeclaration,
                 detected_path: str | None = None) -> str:
    """The message a developer reads. It must answer three questions: what do I
    change, why could the harness not do it, and how do I continue."""
    needs = decl.needs or (
        f"a change to {detected_path}" if detected_path else "a change it is not permitted to make")
    target = decl.file or detected_path or "(not stated)"

    m = "\n  ========== MANUAL CHANGE REQUIRED ==========\n"
    m += f"  The '{phase_id}' phase stopped because it needs a change it is not\n"
    m += "  allowed to make. It did NOT work around the restriction.\n\n"
    m += f"  What is needed : {needs}\n"
    m += f"  File to change : {target}\n"
    if decl.why:
        m += f"  Why the harness cannot do it : {decl.why}\n"
    else:
        m += ("  Why the harness cannot do it : this file is outside the paths any\n"
              "                     phase may write. Build and dependency changes are\n"
              "                     deliberately a human decision.\n")
    if decl.suggested:
        m += f"\n  Suggested change:\n    {decl.suggested}\n"
    m += "\n  To continue:\n"
    m += f"    1. git fetch && git checkout harness-wip/{feature_id}\n"
    m += "    2. make the change above, commit and push\n"
    m += "    3. re-run the Harness workflow with:\n"
    m += f"         feature_id  = {feature_id}\n"
    m += "         resume      = true\n"
    m += f"         start_phase = {phase_id}\n"
    m += "\n  The phases that already passed will not re-run.\n"
    m += "  ============================================\n"
    return m
