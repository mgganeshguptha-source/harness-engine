"""
plan_check.py — the PLAN PATH CHECK gate.

Runs after prompt_steps returns OK, before the expensive coding phase. It reads
the plan's "Impacted Files" table and confirms that every file path the plan
intends to touch lands under a package ROOT that actually exists in the repo.

WHY THIS EXISTS
On run OP0-014 the planner invented a package path (com.example.bookservice)
by inferring it from the project name without reading the tree. The coding phase
cannot create arbitrary directories (write boundary), so a plan pointing at a
package that does not exist is unexecutable — and discovering that inside coding
costs a full, expensive phase. A directory listing costs nothing, so we catch it
here.

WHAT IT DOES AND DOES NOT FLAG
- A NEW leaf sub-package under a real root (e.g. adding cache/ under
  src/main/java/com/example/book/) is ALLOWED — new development legitimately
  creates new sub-packages, and coding writes them within its src/main/** glob.
- A WRONG base package (e.g. com/example/bookservice/... when the tree is
  com/example/book/...) is FLAGGED — the root itself is missing, which means the
  plan is addressing a package that does not exist.

The check therefore verifies, for each planned path, that the longest existing
ancestor directory reaches a meaningful package depth — not that the exact leaf
directory already exists.

INTERFACE (consumed by state_machine.py)
    check_plan(repo_root, plan_file, target_module, log=print) -> PlanCheckResult
        .missing_dirs : list[MissingDir]  (truthy => halt)
        .checked      : int               (paths whose root resolved)
    halt_message(result, plan_file) -> str
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# How many path segments beneath src/main/java (or src/test/java) must exist for
# a planned file's package to count as "rooted". com/example/book => 3 segments.
# We require the plan's path to share an existing ancestor at least this deep;
# a brand-new leaf below that depth is allowed.
_MIN_ROOTED_SEGMENTS = 2

# Source roots we understand. A planned path is checked relative to whichever of
# these it contains.
_SOURCE_ROOTS = ("src/main/java", "src/test/java", "src/main/kotlin",
                 "src/test/kotlin", "src/main/resources", "src/test/resources")


@dataclass
class MissingDir:
    """One planned path whose package root does not exist in the repo."""
    planned_path: str          # path exactly as the plan wrote it (repo-relative)
    expected_root: str         # the package root we looked for and did not find
    deepest_existing: str      # the deepest ancestor that DOES exist (for the message)


@dataclass
class PlanCheckResult:
    missing_dirs: List[MissingDir] = field(default_factory=list)
    checked: int = 0
    # Paths we could not interpret (no recognised source root) — reported, not fatal.
    skipped: List[str] = field(default_factory=list)


def _normalise(p: str) -> str:
    return p.strip().strip("`").replace("\\", "/").lstrip("/")


def _module_prefix(target_module: Optional[str]) -> str:
    """If a target module is configured and the planned path is module-relative,
    the on-disk path is <module>/<planned>. Blank => no prefix (single-module or
    the plan already includes the module)."""
    if not target_module:
        return ""
    return target_module.strip().strip("/").replace("\\", "/")


def _extract_impacted_paths(plan_text: str) -> List[str]:
    """Pull file paths out of the '## Impacted Files' markdown table.

    The table looks like:
        | ID | Path | Role |
        |----|------|------|
        | F1 | src/main/java/com/example/book/service/BookService.java | ... |

    We take column 2 of each data row in that section. If no Impacted Files
    section is present we fall back to scanning the whole document for
    src/{main,test}/... paths, so a differently-shaped plan still gets checked.
    """
    paths: List[str] = []

    # Isolate the Impacted Files section (up to the next '## ' heading).
    section = None
    m = re.search(r"^#{1,6}\s*Impacted Files.*?$", plan_text,
                  flags=re.IGNORECASE | re.MULTILINE)
    if m:
        start = m.end()
        nxt = re.search(r"^#{1,6}\s", plan_text[start:], flags=re.MULTILINE)
        section = plan_text[start:start + nxt.start()] if nxt else plan_text[start:]

    scan = section if section is not None else plan_text

    if section is not None:
        for line in section.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # header/separator rows
            if len(cells) < 2:
                continue
            if set(cells[1].replace(" ", "")) <= set("-:"):
                continue
            if cells[1].lower() == "path":
                continue
            cand = _normalise(cells[1])
            if any(root in cand for root in _SOURCE_ROOTS):
                paths.append(cand)

    # Fallback / supplement: catch any source paths mentioned anywhere.
    for mm in re.finditer(r"(src/(?:main|test)/(?:java|kotlin|resources)/[^\s`|)]+)",
                          scan):
        cand = _normalise(mm.group(1))
        if cand not in paths:
            paths.append(cand)

    return paths


def _split_after_source_root(rel_path: str):
    """Return (source_root, package_segments, filename) or None if no root found."""
    for root in _SOURCE_ROOTS:
        idx = rel_path.find(root)
        if idx == -1:
            continue
        after = rel_path[idx + len(root):].strip("/")
        parts = after.split("/")
        if len(parts) < 1:
            return root, [], ""
        *pkg, fname = parts
        return root, pkg, fname
    return None


_SRC_EXTS = (".java", ".kt")


def _discover_base_packages(source_root_dir: Path) -> set:
    """Return the set of real base packages under a source root, as '/'-joined
    segment strings relative to the source root.

    The base package is the deepest directory chain that is unambiguous: we walk
    down while a directory has exactly one sub-directory and no source files. The
    moment a directory contains source files OR branches into multiple
    sub-packages, that directory is the base — everything below it is application
    structure where new sub-packages are legitimate.

    Example: com/ -> example/ -> book/ {config, controller, service, ...} and
    BookServiceApplication.java  =>  base package "com/example/book".
    """
    if not source_root_dir.is_dir():
        return set()

    def has_source(d: Path) -> bool:
        return any(f.suffix in _SRC_EXTS for f in d.iterdir() if f.is_file())

    def subdirs(d: Path):
        return [c for c in d.iterdir() if c.is_dir()]

    cur = source_root_dir
    segs: list = []
    while True:
        subs = subdirs(cur)
        if has_source(cur) or len(subs) != 1:
            break
        cur = subs[0]
        segs.append(cur.name)
    return {"/".join(segs)} if segs else set()


def _under_a_real_base(pkg_segments: list, real_bases: set) -> bool:
    """True if the planned package sits under (or exactly at) a discovered base."""
    if not real_bases:
        # No base discovered (empty/new source root) — cannot contradict; allow.
        return True
    planned = "/".join(pkg_segments)
    for base in real_bases:
        if planned == base or planned.startswith(base + "/"):
            return True
    return False


def check_plan(repo_root, plan_file, target_module, log=print) -> PlanCheckResult:
    repo_root = Path(repo_root)
    plan_file = Path(plan_file)
    result = PlanCheckResult()

    try:
        plan_text = plan_file.read_text(encoding="utf-8")
    except Exception as e:
        log(f"  [harness] plan path check: cannot read {plan_file} ({e})")
        return result

    prefix = _module_prefix(target_module)
    planned = _extract_impacted_paths(plan_text)

    for raw in planned:
        parsed = _split_after_source_root(raw)
        if parsed is None:
            result.skipped.append(raw)
            continue
        source_root, pkg, _fname = parsed

        # On-disk base: repo_root [+ module prefix] + source_root
        base = repo_root
        if prefix and not raw.startswith(prefix + "/"):
            base = base / prefix
        base = base / source_root

        # Discover the REAL base package(s) under this source root by walking down
        # until a directory contains a source file OR branches — that is the point
        # below which sub-packages are legitimately new. For com/example/book that
        # yields "com/example/book"; a planned com/example/bookservice/... then
        # fails to match any real base and is flagged.
        real_bases = _discover_base_packages(base)

        if _under_a_real_base(pkg, real_bases):
            result.checked += 1
        else:
            # Report the deepest ancestor that DOES exist, for the message.
            existing = base
            for seg in pkg:
                nxt = existing / seg
                if nxt.is_dir():
                    existing = nxt
                else:
                    break
            expected = "/".join(
                ([prefix] if prefix and not raw.startswith(prefix + "/") else [])
                + [source_root] + (sorted(real_bases)[0].split("/") if real_bases else [])
            )
            deepest = str(existing.relative_to(repo_root)).replace("\\", "/") \
                if existing.exists() else "(source root missing)"
            result.missing_dirs.append(MissingDir(
                planned_path=raw,
                expected_root=expected,
                deepest_existing=deepest,
            ))

    if result.checked:
        log(f"  [harness] plan paths: {result.checked} checked, all resolve")
    if result.skipped:
        log(f"  [harness] plan path check: {len(result.skipped)} path(s) "
            f"not interpretable, skipped")

    return result


def halt_message(result: PlanCheckResult, plan_file) -> str:
    lines = []
    lines.append("  ================ PLAN PATH CHECK: HALTED ================")
    lines.append("  The implementation plan references package paths that do NOT")
    lines.append("  exist in this repository. The coding phase cannot create a")
    lines.append("  package that isn't there, so the plan is unexecutable as written.")
    lines.append("")
    lines.append("  This usually means the plan GUESSED a package name instead of")
    lines.append("  reading the source tree. Fix the paths in the plan to match the")
    lines.append("  real package, then resume.")
    lines.append("")
    lines.append("  Offending path(s):")
    for md in result.missing_dirs:
        lines.append(f"    - {md.planned_path}")
        lines.append(f"        expected package root : {md.expected_root}")
        lines.append(f"        deepest existing dir  : {md.deepest_existing or '(source root only)'}")
    lines.append("")
    lines.append(f"  Plan file : {plan_file}")
    lines.append("  =========================================================")
    return "\n".join(lines)
