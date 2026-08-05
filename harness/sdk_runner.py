"""
sdk_runner.py — the REAL AgentRunner, backed by the Copilot SDK.

This is the only file that talks to Copilot. It satisfies the same AgentRunner
protocol as FakeAgentRunner, so the state machine / executor / boundaries are
unchanged. Swapping fake -> real is a one-line change in run.py.

TWO LAYERS OF THE HARNESS MEET HERE:
  * CAPABILITY (probabilistic): the prompt is built from the user story + your
    .github/skills + .github/instructions. This is the "Markdown gives capability".
  * GUARANTEE (deterministic): on_permission_request enforces the phase write
    boundary in REAL TIME. Every file write the model attempts is checked against
    is_write_allowed() for the current phase; out-of-bounds => denied at the SDK,
    before it touches disk. This is the "code gives a guarantee".

Requires: pip install github-copilot-sdk ; Copilot CLI authed (Pro license).
Python 3.11+.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from phases import Phase
from state import RunState
from executor import AgentResult
from boundaries import is_write_allowed, deny_reason

# SDK imports are done lazily inside run() so the rest of the harness (and all the
# zero-credit tests) import cleanly on machines without the SDK installed.


# Default model for the PoC. "cheapest" — override via HarnessConfig later.
# NOTE: verify the exact string with client.list_models() on your tenant; model
# availability differs by org/subscription.
DEFAULT_MODEL = "gpt-4.1"


def _parse_frontmatter_applyto(text: str) -> str | None:
    """Extract the applyTo value from an instruction file's YAML frontmatter.
    Returns the raw glob string (may be comma-separated), or None if absent."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    fm = text[3:end]
    for line in fm.splitlines():
        line = line.strip()
        if line.startswith("applyTo:"):
            val = line.split(":", 1)[1].strip().strip('"').strip("'")
            return val
    return None


def _glob_intersects_scope(apply_to: str, scope_globs: list) -> bool:
    """Does an instruction's applyTo intersect the phase's file scope?
    '**' always matches (always-on guardrail). Otherwise check prefix overlap
    between the applyTo patterns and the phase scope patterns."""
    patterns = [p.strip() for p in apply_to.split(",") if p.strip()]
    for pat in patterns:
        if pat == "**":
            return True
        # reduce a glob to its literal directory prefix for a coarse overlap test
        pat_prefix = pat.split("*", 1)[0].rstrip("/")
        for sc in scope_globs:
            sc_prefix = sc.split("*", 1)[0].rstrip("/")
            if not pat_prefix or not sc_prefix:
                continue
            if pat_prefix.startswith(sc_prefix) or sc_prefix.startswith(pat_prefix):
                return True
    return False


def _extract_names(data, *attr_candidates) -> list[str]:
    """Defensively pull human-readable name(s) out of an SDK event payload.

    The SDK's event schema (github-copilot-sdk 1.0.x) isn't documented for the
    skills_loaded / tool.execution_* payloads, and the shape differs by event.
    Rather than hard-code one attribute, we try several shapes in order:
      1. a list attribute (e.g. data.skills) of objects/dicts each having a name
      2. a scalar name attribute (e.g. data.name / data.tool_name) on data itself
      3. a pydantic .model_dump() fallback, scanning common keys
    Returns a de-duplicated, order-preserving list of strings (possibly empty).
    """
    out: list[str] = []

    def _name_of(obj) -> str | None:
        for k in ("name", "id", "tool_name", "skill_name", "title"):
            v = getattr(obj, k, None)
            if v is None and isinstance(obj, dict):
                v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        if isinstance(obj, str) and obj.strip():
            return obj.strip()
        return None

    # 1) list-shaped attributes (skills, tools, ...)
    for attr in attr_candidates:
        seq = getattr(data, attr, None)
        if seq is None and isinstance(data, dict):
            seq = data.get(attr)
        if isinstance(seq, (list, tuple)):
            for item in seq:
                n = _name_of(item)
                if n:
                    out.append(n)

    # 2) scalar name directly on data (tool.execution_start is usually one tool)
    if not out:
        n = _name_of(data)
        if n:
            out.append(n)

    # 3) pydantic dump fallback — last resort, scan likely keys
    if not out:
        dump = None
        try:
            dump = data.model_dump()  # pydantic v2
        except Exception:
            try:
                dump = dict(vars(data))
            except Exception:
                dump = None
        if isinstance(dump, dict):
            for attr in attr_candidates:
                seq = dump.get(attr)
                if isinstance(seq, (list, tuple)):
                    for item in seq:
                        n = _name_of(item)
                        if n:
                            out.append(n)
            for k in ("name", "tool_name", "skill_name"):
                v = dump.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())

    # de-dupe, preserve order
    seen = set()
    deduped = []
    for n in out:
        if n not in seen:
            seen.add(n)
            deduped.append(n)
    return deduped


# Reference patterns a SKILL.md might use to point at a companion template/asset .md.
# We inline the CONTENT of any referenced .md so the model actually receives the
# template — non-interactive SDK runs do NOT auto-resolve a skill's file refs the
# way the interactive Copilot skill engine does.
_MD_REF = re.compile(r"[`'\"(]?([A-Za-z0-9_./-]+\.md)[`'\")]?")
# Names that are almost certainly companion assets worth inlining even if the ref
# is loose. We also inline ANY .md the SKILL.md names that resolves on disk.
_TEMPLATE_HINT = re.compile(r"template|analysis|schema|format|skeleton|scaffold", re.IGNORECASE)


def _resolve_skill_assets(skill_md: Path, gh: Path) -> list:
    """Find companion files to inline for a skill, returning (label, content) pairs.

    Two sources, de-duplicated against each other:
      1) TEMPLATES REFERENCED by name in the SKILL.md text (resolved across the
         skill folder, .github/skills/, and .github/).
      2) ASSET SWEEP: every .md directly under THIS skill's own assets/ folder,
         whether or not the SKILL.md names it. This matches the repo convention
         (templates live in <skill>/assets/) and removes the fragile dependency on
         the SKILL.md spelling out the exact filename.

    Never inlines the SKILL.md itself; caps total to avoid runaway prompt growth.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        text = ""
    skill_dir = skill_md.parent
    search_roots = [skill_dir, gh / "skills", gh]
    found = []
    seen_paths = set()

    def _add(cand: Path):
        """Inline a resolved .md candidate once. Returns True if added."""
        cand = cand.resolve()
        if cand in seen_paths:
            return False
        if not cand.is_file() or cand.suffix.lower() != ".md" or cand.name.lower() == "skill.md":
            return False
        try:
            content = cand.read_text(encoding="utf-8")
        except Exception:
            return False
        seen_paths.add(cand)
        found.append((cand.name, content, cand))
        return True

    # 1) templates the SKILL.md references by name
    for m in _MD_REF.finditer(text):
        ref = m.group(1).strip()
        base = ref.split("/")[-1]
        if base.lower() == "skill.md":
            continue
        candidates = [(skill_dir / ref)]
        for root in search_roots:
            if root.is_dir():
                candidates.extend(root.rglob(base))
        for cand in candidates:
            if _add(cand):
                break  # first resolving candidate wins for this ref
        if len(found) >= 8:
            break

    # 2) sweep THIS skill's own assets/ folder — inline any .md not already added.
    # Scoped to the skill's assets/ (not a global sweep), so we only pick up the
    # skill's own companion files.
    assets_dir = skill_dir / "assets"
    if assets_dir.is_dir():
        for asset in sorted(assets_dir.rglob("*.md")):
            _add(asset)
            if len(found) >= 8:
                break

    return found


def _load_capability_layer(repo_root: Path, phase: Phase) -> tuple:
    """Load the capability layer for THIS phase.

    Config-driven (HarnessConfig):
      - selective_capability=False -> load everything (legacy behaviour).
      - always-on guardrails: instruction files with applyTo "**".
      - phase-scoped instructions: applyTo glob intersects phase_file_scope[phase].
      - phase skills: the named skills in phase_skills[phase].

    This keeps cross-cutting guardrails (HIPAA, prompt-injection, logging,
    output-naming) in EVERY phase, while stack/phase-specific rules and skills
    load only where relevant — cutting tokens without dropping safety rules.
    """
    from config import HarnessConfig
    cfg = HarnessConfig.load(repo_root / ".harness")
    gh = repo_root / ".github"
    chunks = []
    # CAPABILITY MANIFEST: an auditable record of exactly which approved capability
    # content (instructions, skills, templates) was delivered to the model for this
    # phase — name, repo-relative path, size, sha256. This is the harness-side PROOF
    # of deterministic skill delivery (the SDK's own 'loaded' telemetry only reports
    # its internal agent skill and can never show repo skills).
    manifest = {"instructions": [], "skills": [], "assets": []}

    def _entry(p: Path, text: str, **extra):
        try:
            rel = str(p.resolve().relative_to(repo_root.resolve()))
        except Exception:
            rel = str(p)
        raw = text.encode("utf-8")
        return {"name": p.name, "path": rel, "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), **extra}

    # ---- instructions ----
    instr_dir = gh / "instructions"
    if instr_dir.is_dir():
        scope = cfg.phase_file_scope.get(phase.id, []) if cfg.selective_capability else None
        stacks = cfg.repo_stacks or []
        include_all_stacks = (not stacks) or ("*" in stacks)
        for md in sorted(instr_dir.rglob("*.md")):
            # REPO-LEVEL STACK FILTER: if this file sits in a stack subfolder
            # (backend/, angular-frontend/, mobile-frontend/) that this repo does
            # not contain, skip it entirely — before any phase logic. Files
            # directly under instructions/ have no stack subfolder => always kept.
            rel_parent = md.parent.relative_to(instr_dir)
            stack_folder = rel_parent.parts[0] if rel_parent.parts else None
            if (cfg.selective_capability and not include_all_stacks
                    and stack_folder is not None and stack_folder not in stacks):
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                continue
            if cfg.selective_capability:
                apply_to = _parse_frontmatter_applyto(text)
                # no applyTo -> treat as always-on (safe default)
                if apply_to is None or apply_to == "**":
                    pass  # always-on guardrail
                elif _glob_intersects_scope(apply_to, scope):
                    pass  # relevant to this phase
                else:
                    continue  # skip — not relevant to this phase
            chunks.append(f"\n--- instructions/{md.name} ---\n{text}")
            manifest["instructions"].append(_entry(md, text))

    # ---- skills ----
    skills_dir = gh / "skills"
    if skills_dir.is_dir():
        if cfg.selective_capability:
            wanted = set(cfg.phase_skills.get(phase.id, []))
        else:
            wanted = None  # all
        for skill_md in sorted(skills_dir.rglob("SKILL.md")):
            skill_name = skill_md.parent.name
            if wanted is not None and skill_name not in wanted:
                continue
            try:
                skill_text = skill_md.read_text(encoding="utf-8")
                chunks.append(f"\n--- skill: {skill_name} ---\n" + skill_text)
                manifest["skills"].append(_entry(skill_md, skill_text, skill=skill_name))
            except Exception:
                pass
            # Inline any template/companion .md files this skill references, so the
            # model actually RECEIVES the template content. In non-interactive SDK
            # mode the skill engine does not auto-resolve these file references, so
            # without this the model sees a dangling "follow template.md" pointer and
            # improvises — breaking output standardization.
            for asset_name, asset_text, asset_path in _resolve_skill_assets(skill_md, gh):
                chunks.append(
                    f"\n--- skill '{skill_name}' REQUIRED template: {asset_name} ---\n"
                    f"You MUST produce output that follows this template's EXACT structure, "
                    f"section order, and headings. Do not add, rename, drop, or reorder "
                    f"sections. Fill each section per its instructions; omit a section only "
                    f"when the template explicitly says it is optional/skippable.\n\n"
                    f"{asset_text}"
                )
                manifest["assets"].append(_entry(asset_path, asset_text, for_skill=skill_name))

    return "\n".join(chunks), manifest


# ---------------------------------------------------------------------------
# PUSH-MODE CODE REVIEW (diff-scoping)
#
# Instead of telling the reviewer "go read the repo" (pull mode — 15+ tool
# calls, unbounded exploration, ~557K tokens observed on a loopback pass),
# the harness gathers the review material DETERMINISTICALLY (zero LLM tokens)
# and inlines it into the prompt:
#   1. git diff HEAD -- src/main          (the exact production change)
#   2. full content of every changed/new production source file
#   3. the implementation plan (.harness/prompt-steps.md)
# The reviewer then needs ~1-2 turns: reason once, write review.md. Read
# permission is DENIED for this phase (see on_permission_request), so the
# review is reproducible — the reviewer cannot wander into unrelated code.
# If the dossier cannot be built (git unavailable, no changed files), the
# harness FALLS BACK to the legacy pull-mode prompt and read access.
# ---------------------------------------------------------------------------

_DOSSIER_FILE_CAP = 40_000    # chars per inlined file
_DOSSIER_TOTAL_CAP = 200_000  # chars for the whole dossier (~50K tokens)


def _run_git(repo_root: Path, *args: str) -> str:
    """Run a git command in repo_root; return stdout, or '' on any failure."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(repo_root),
            capture_output=True, text=True, timeout=60,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _build_review_dossier(repo_root: Path, log=print) -> str | None:
    """Assemble the inline review material. Returns None if nothing changed
    (or git failed) — caller falls back to legacy pull-mode review."""
    diff = _run_git(repo_root, "diff", "HEAD", "--", "src/main")
    tracked = [ln.strip() for ln in _run_git(
        repo_root, "diff", "HEAD", "--name-only", "--", "src/main"
    ).splitlines() if ln.strip()]
    untracked = [ln.strip() for ln in _run_git(
        repo_root, "ls-files", "--others", "--exclude-standard", "--", "src/main"
    ).splitlines() if ln.strip()]
    changed = list(dict.fromkeys(tracked + untracked))  # de-dupe, keep order
    if not changed:
        return None

    parts: list[str] = []
    if diff.strip():
        parts.append(
            "--- GIT DIFF (production source: HEAD vs working tree) ---\n" + diff
        )
    for rel in changed:
        p = repo_root / rel
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if len(text) > _DOSSIER_FILE_CAP:
            text = text[:_DOSSIER_FILE_CAP] + "\n... [TRUNCATED by harness — file cap reached]"
        parts.append(f"--- FULL FILE (current content): {rel} ---\n{text}")

    plan = repo_root / ".harness" / "prompt-steps.md"
    if plan.is_file():
        try:
            parts.append(
                "--- IMPLEMENTATION PLAN: .harness/prompt-steps.md ---\n"
                + plan.read_text(encoding="utf-8")
            )
        except Exception:
            pass

    if not parts:
        return None
    dossier = "\n\n".join(parts)
    if len(dossier) > _DOSSIER_TOTAL_CAP:
        dossier = dossier[:_DOSSIER_TOTAL_CAP] + "\n... [TRUNCATED by harness — dossier cap reached]"
    log(f"  [review] push-mode dossier: {len(changed)} changed file(s), "
        f"{len(dossier)} chars inlined")
    return dossier


def _build_unittest_dossier(repo_root: Path, log=print) -> str | None:
    """Inline EVERYTHING the test author needs, so it never explores the repo.

    Backlog #6: unit_testing was ~223K tokens because, given only the story, the
    agent hunted for the class under test AND for existing test conventions.
    First attempt at this inlined only the changed production source and left
    reads enabled with a 'you MAY still read test files' note — the agent took
    the invitation (6 reads + grep) and tokens barely moved (~2%).

    So we now push the three things it was actually looking for:
      1. the changed production source (full current content),
      2. the implementation plan,
      3. EXISTING TEST FILES for the same class/package (conventions, fixtures,
         base classes) + a listing of the test tree so it knows what exists.
    With all of that inline there is no legitimate reason to read, and the
    caller denies reads for this phase (see on_permission_request).
    """
    tracked = [ln.strip() for ln in _run_git(
        repo_root, "diff", "HEAD", "--name-only", "--", "src/main"
    ).splitlines() if ln.strip()]
    untracked = [ln.strip() for ln in _run_git(
        repo_root, "ls-files", "--others", "--exclude-standard", "--", "src/main"
    ).splitlines() if ln.strip()]
    changed = list(dict.fromkeys(tracked + untracked))
    if not changed:
        return None

    parts: list[str] = []
    for rel in changed:
        p = repo_root / rel
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if len(text) > _DOSSIER_FILE_CAP:
            text = text[:_DOSSIER_FILE_CAP] + "\n... [TRUNCATED by harness — file cap reached]"
        parts.append(f"--- PRODUCTION CODE UNDER TEST (full current content): {rel} ---\n{text}")

    plan = repo_root / ".harness" / "prompt-steps.md"
    if plan.is_file():
        try:
            parts.append(
                "--- IMPLEMENTATION PLAN: .harness/prompt-steps.md ---\n"
                + plan.read_text(encoding="utf-8")
            )
        except Exception:
            pass

    # --- existing tests for the SAME package: conventions + fixtures + base classes.
    # This is what the agent was grepping/reading for. Push it instead.
    test_root = repo_root / "src" / "test"
    if test_root.is_dir():
        # packages touched by the change, e.g. .../petclinic/owner
        pkg_dirs: list[Path] = []
        for rel in changed:
            pkg = Path(rel).parent                       # src/main/java/<pkg...>
            rest = pkg.parts[3:] if len(pkg.parts) > 3 else ()  # strip src/main/java
            cand = test_root / "java" / Path(*rest) if rest else None
            if cand and cand.is_dir() and cand not in pkg_dirs:
                pkg_dirs.append(cand)

        sibling_tests: list[Path] = []
        for d in pkg_dirs:
            for f in sorted(d.glob("*.java")):
                if f not in sibling_tests:
                    sibling_tests.append(f)

        budget = 6  # cap: enough to convey conventions, not the whole suite
        for f in sibling_tests[:budget]:
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if len(text) > _DOSSIER_FILE_CAP:
                text = text[:_DOSSIER_FILE_CAP] + "\n... [TRUNCATED by harness — file cap reached]"
            rel_f = f.relative_to(repo_root).as_posix()
            parts.append(
                f"--- EXISTING TEST IN THE SAME PACKAGE (follow these conventions): {rel_f} ---\n{text}"
            )

        # full listing of the test tree so the agent knows what already exists
        all_tests = sorted(
            p.relative_to(repo_root).as_posix()
            for p in test_root.rglob("*.java")
        )
        if all_tests:
            shown = all_tests[:200]
            listing = "\n".join(shown)
            if len(all_tests) > len(shown):
                listing += f"\n... (+{len(all_tests) - len(shown)} more)"
            parts.append(
                "--- EXISTING TEST FILES (full listing of src/test) ---\n" + listing
            )

    if not parts:
        return None
    dossier = "\n\n".join(parts)
    if len(dossier) > _DOSSIER_TOTAL_CAP:
        dossier = dossier[:_DOSSIER_TOTAL_CAP] + "\n... [TRUNCATED by harness — dossier cap reached]"
    log(f"  [unit_testing] context dossier: {len(changed)} file(s) under test, "
        f"{len(dossier)} chars inlined (incl. sibling tests + test-tree listing)")
    return dossier


def _phase_instruction(phase: Phase, run: RunState, repo_root: Path,
                       review_dossier: str | None = None,
                       unittest_dossier: str | None = None) -> str:
    """The per-phase task prompt. Phase-specific, story-aware, feedback-aware.

    All output paths are given as ABSOLUTE paths derived from repo_root. In a
    monorepo the agent may otherwise resolve a relative '.github/...' against the
    git root (one level up) instead of the service folder; absolute paths remove
    that ambiguity. We also tell the agent the directory already exists and that
    shell is disallowed, so it writes the file directly without attempting mkdir.
    """
    story = run.story
    rr = str(repo_root).replace("\\", "/").rstrip("/")
    ctx_dir = f"{rr}/.github/story-context-files"
    plan_file = f"{rr}/.harness/prompt-steps.md"
    src_main = f"{rr}/src/main"
    src_test = f"{rr}/src/test"
    docs_dir = f"{rr}/docs"
    pr_body = f"{rr}/.harness/pr-body.md"

    # code_review prompt: PUSH MODE (dossier inlined, reads disabled) when the
    # harness could build the dossier; legacy PULL MODE (agent reads the repo)
    # only as a fallback.
    _review_rules = (
        f"Judge the PRODUCTION CODE on: correctness against the story and "
        f"plan, edge-case handling in the code itself (null/empty/boundary logic), "
        f"security, error handling, readability, and adherence to project standards. "
        f"Use the security-review skill.\n"
        f"SCOPE — WHAT YOU MUST NOT DO:\n"
        f"  - Do NOT review, run, inspect, or comment on unit tests or TEST COVERAGE. "
        f"Test existence and coverage are enforced by a SEPARATE automated gate later "
        f"in the pipeline — they are OUT OF SCOPE for this review.\n"
        f"  - Do NOT request changes on the grounds that tests are missing, insufficient, "
        f"or that coverage is low. A verdict of CHANGES_REQUESTED must be justified by a "
        f"problem in the PRODUCTION CODE itself, never by anything test-related.\n"
        f"  - Do NOT run the build or tests. Do NOT edit, create, or fix any source or "
        f"test file — reviewing is not fixing.\n"
        f"The ONLY file you may write is the verdict at this EXACT path (parent .harness "
        f"ALREADY EXISTS, shell disallowed): {rr}/.harness/review.md\n"
        f"Write {rr}/.harness/review.md in EXACTLY this structure:\n"
        f"  - First line MUST be:  VERDICT: PASS   (if the production code is correct and ready)\n"
        f"    or:                  VERDICT: CHANGES_REQUESTED   (only if the production code has a problem)\n"
        f"  - If CHANGES_REQUESTED, list each problem on its own line as:\n"
        f"      [ISSUE]: <specific, actionable description of the CODE problem and where>\n"
        f"  - You may add free-form notes after the issues.\n"
        f"Be strict about production-code correctness, safety, and standards — but if the "
        f"only thing you could fault is test coverage, the verdict is PASS. "
        f"Write ONLY {rr}/.harness/review.md."
    )
    if review_dossier is not None:
        code_review_task = (
            f"You are an INDEPENDENT code reviewer. You did NOT write this code. "
            f"Review ONLY the production source implementation of the change for:\n"
            f"  STORY: {story}\n"
            f"ALL review material is provided INLINE below — the git diff, the full "
            f"current content of every changed production file, and the implementation "
            f"plan. Review ONLY this material. Do NOT read, open, list, glob, or search "
            f"any file in the repository — file-read access is DISABLED for this phase "
            f"and any read attempt will be denied. Do NOT ask for more files; everything "
            f"in scope is below. After reasoning, write your verdict file immediately.\n"
            f"{_review_rules}\n\n"
            f"================ REVIEW MATERIAL (inlined by harness) ================\n\n"
            f"{review_dossier}\n\n"
            f"================ END REVIEW MATERIAL ================\n"
        )
    else:
        code_review_task = (
            f"You are an INDEPENDENT code reviewer. You did NOT write this code. "
            f"Review ONLY the production source implementation of the change for:\n  STORY: {story}\n"
            f"Read (read-only) the plan at {plan_file} and the application source under "
            f"{src_main}/. Do NOT open or read files under {src_test}/.\n"
            f"{_review_rules}"
        )

    if unittest_dossier is not None:
        unit_testing_task = (
            f"Write unit tests under {src_test}/ that verify:\n  STORY: {story}\n"
            f"Do NOT modify application code under {src_main}/. Tests only.\n"
            f"EVERYTHING you need is provided INLINE below: the production code under "
            f"test, the implementation plan, existing tests in the same package (follow "
            f"their conventions, imports, and fixtures), and a listing of the whole test "
            f"tree. Do NOT read, open, list, glob, or search any file — file-read access "
            f"is DISABLED for this phase and any read attempt will be denied. Do not ask "
            f"for more files; nothing else is in scope. Write the test file immediately.\n\n"
            f"================ TEST CONTEXT (inlined by harness) ================\n\n"
            f"{unittest_dossier}\n\n"
            f"================ END TEST CONTEXT ================\n"
        )
    else:
        unit_testing_task = (
            f"Write unit tests under {src_test}/ that verify:\n  STORY: {story}\n"
            f"Do NOT modify application code under {src_main}/. Tests only."
        )

    # coding prompt: state the APPROVED file list explicitly. The scope gate will
    # halt the phase if it creates production files the plan never listed, so it is
    # only fair (and far cheaper) to tell it the boundary up front rather than catch
    # it afterwards. In run 29182275947 the coding phase, given only "BUILD FAILURE",
    # invented a whole second Owner/Pet class hierarchy to route around the error.
    _approved_block = ""
    try:
        from execution_record import _approved_paths_from_plan
        _pf = repo_root / ".harness" / "prompt-steps.md"
        if _pf.is_file():
            _ap = sorted(_approved_paths_from_plan(_pf.read_text(encoding="utf-8")))
            if _ap:
                _approved_block = (
                    "\nThe approved plan authorises changes to EXACTLY these files:\n"
                    + "\n".join(f"  - {p}" for p in _ap)
                    + "\nDo NOT create new production classes that are not on this list. "
                      "If the build fails, fix the files on this list — do NOT invent new "
                      "classes to route around the failure. If the change genuinely cannot "
                      "be made within this scope, say so explicitly instead of expanding it.\n"
                )
    except Exception:
        pass

    base = {
        "context": (
            f"You are running in NON-INTERACTIVE / CI mode. Do NOT ask any questions "
            f"and do NOT wait for input. Use the build-context skill in CI mode.\n"
            f"Explore the repository (read-only) and write a context document as a new "
            f"timestamped .md file in this EXACT directory (it ALREADY EXISTS, do not "
            f"mkdir, shell is disallowed): {ctx_dir}\n"
            f"  STORY: {story}\n"
            f"Fill every section you can from the story. For any genuine ambiguity, write a "
            f"precise '[NEEDS CLARIFICATION]: <missing dimension>' line in Section 8 — do NOT "
            f"guess and do NOT use vague language. Do NOT modify any source files. "
            f"Write ONLY the single context .md file in {ctx_dir}."
        ),
        "prompt_steps": (
            f"You are running in NON-INTERACTIVE / CI mode. Use the build-prompt-steps "
            f"skill in CI mode: read the newest context .md file from this directory on "
            f"disk (there is no chat attachment): {ctx_dir}\n"
            f"Do not stop for human checkpoints. Write a numbered implementation plan, "
            f"including an 'Impacted Files' section, to this EXACT file (its parent "
            f".harness ALREADY EXISTS, do not mkdir, shell is disallowed): {plan_file}\n"
            f"  STORY: {story}\n"
            f"THIS IS A PLANNING PHASE ONLY. You MUST NOT create, edit, or write any "
            f".java file or any source or test file. Do NOT implement the change in this "
            f"phase — implementation happens in a later phase. The ONLY file you are "
            f"permitted to write is {plan_file}. Any attempt to write under src/main or "
            f"src/test will be denied by the harness and will FAIL the run. Describe the "
            f"intended code changes as TEXT inside the plan file instead: list the target "
            f"file paths, method signatures, and (optionally) before/after snippets as "
            f"fenced code blocks within {plan_file}. Do not call any file-write tool for "
            f"anything other than {plan_file}.\n"
            f"Write ONLY {plan_file}."
        ),
        "coding": (
            f"Implement the change described in {plan_file} for:\n  STORY: {story}\n"
            f"Edit ONLY application source under {src_main}/. Do NOT create or edit any test files."
            f"{_approved_block}"
        ),
        "code_review": code_review_task,
        "unit_testing": unit_testing_task,
        "documentation": (
            f"Document the change as a markdown file under {docs_dir}/ (the directory "
            f"ALREADY EXISTS, do not mkdir, shell is disallowed) for:\n  STORY: {story}\n"
            f"Write ONLY under {docs_dir}/."
        ),
        "raise_pr": (
            f"Summarize the change for a pull request body and write it to "
            f"this EXACT file (parent .harness ALREADY EXISTS, shell disallowed): "
            f"{pr_body}\n  STORY: {story}\nWrite ONLY {pr_body}."
        ),
    }[phase.id]

    out = base
    if run.last_feedback and run.approvals.get(phase.id) == "rejected":
        out += f"\n\nThe previous attempt was REJECTED. Address this feedback:\n{run.last_feedback}\n"
    return out


class SdkAgentRunner:
    def __init__(self, model: str = DEFAULT_MODEL, log=print):
        self.model = model
        self.log = log

    def run(self, phase: Phase, prompt: str, run: RunState, repo_root: Path) -> AgentResult:
        """Synchronous wrapper the executor calls; drives the async SDK underneath."""
        try:
            return asyncio.run(self._run_async(phase, run, repo_root))
        except Exception as e:  # surface as SDK_ERROR via the executor
            import traceback
            tb = traceback.format_exc()
            self.log("  ! SDK exception:\n" + tb)
            msg = f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__} (no message)"
            return AgentResult(errored=True, error_msg=msg)

    async def _run_async(self, phase: Phase, run: RunState, repo_root: Path) -> AgentResult:
        import os
        from copilot import CopilotClient
        from copilot.rpc import (
            PermissionDecisionApproveOnce,
            PermissionDecisionReject,
        )

        attempted_writes: list[str] = []

        # PUSH-MODE REVIEW: build the dossier deterministically BEFORE the SDK
        # call (zero LLM tokens). If it builds, the reviewer gets everything
        # inline and read access is disabled below; if not, legacy pull mode.
        review_dossier: str | None = None
        if phase.id == "code_review":
            review_dossier = _build_review_dossier(repo_root, log=self.log)
            if review_dossier is None:
                self.log("  [review] dossier unavailable — falling back to "
                         "legacy pull-mode review (reads allowed)")
        review_push_mode = review_dossier is not None

        # #6: unit_testing context dossier — inline the changed production code +
        # plan so the test author doesn't burn tokens rediscovering the class
        # under test (~223K observed). Reads stay enabled (tests need fixtures).
        unittest_dossier: str | None = None
        if phase.id == "unit_testing":
            unittest_dossier = _build_unittest_dossier(repo_root, log=self.log)
            if unittest_dossier is None:
                self.log("  [unit_testing] dossier unavailable — falling back to "
                         "story-only prompt (reads allowed)")
        unittest_push_mode = unittest_dossier is not None
        # Reads are denied whenever a phase runs in push mode: everything in scope
        # is already inline, so a read is exploration we are paying for twice.
        push_mode = review_push_mode or unittest_push_mode

        # ---- THE INTERLOCK (real-time, at the SDK) ----
        def on_permission_request(request, invocation):
            # NOTE: request.kind is a plain string (ClassVar), NOT an enum.
            # Using request.kind.value here would raise AttributeError, which the
            # SDK silently turns into a denial.
            kind = request.kind
            if kind == "write":
                path = getattr(request, "file_name", "") or ""
                attempted_writes.append(path)
                self.log(f"  . write request: {path}")
                if not is_write_allowed(path, phase.allowed_writes, repo_root=str(repo_root)):
                    msg = deny_reason(path, phase.id, phase.allowed_writes, repo_root=str(repo_root))
                    self.log("  ! " + msg)
                    return PermissionDecisionReject(feedback=msg)
                return PermissionDecisionApproveOnce()
            if kind == "shell":
                cmd = getattr(request, "full_command_text", "") or str(getattr(request, "commands", ""))
                self.log(f"  ! shell denied: {cmd}")
                return PermissionDecisionReject(
                    feedback="Shell commands are not permitted in this phase."
                )
            if kind == "read" and push_mode:
                # PUSH MODE (code_review / unit_testing): everything in scope is
                # already inline in the prompt, so exploratory reads are denied.
                #
                # CRITICAL EXEMPTION: the phase's OWN OUTPUT FILE must stay
                # readable. The SDK's create/edit tools READ the target before
                # writing it — so a blanket read-deny silently makes the phase
                # unable to write its own artifact. Observed in run 29181773991:
                # the reviewer PASSED the code on attempts 2 and 3, could not
                # write review.md, fell back to shell (denied), and emitted the
                # verdict to chat instead. The harness then re-parsed the STALE
                # review.md from attempt 1 and looped until the retry cap blew.
                #
                # Rule: if the phase is allowed to WRITE a path, it may READ it.
                target = (getattr(request, "file_name", "")
                          or getattr(request, "path", "")
                          or getattr(request, "file_path", "")
                          or "")
                if target and is_write_allowed(target, phase.allowed_writes,
                                               repo_root=str(repo_root)):
                    self.log(f"  . read approved (own output file): {target}")
                    return PermissionDecisionApproveOnce()
                # Unknown/blank path: approve rather than risk strangling the write.
                # Fail OPEN here — a stray read costs tokens; a blocked write costs
                # the whole phase.
                if not target:
                    self.log("  . read approved (path not reported by SDK — failing open)")
                    return PermissionDecisionApproveOnce()
                self.log(f"  ! read denied (push-mode {phase.id}): {target}")
                return PermissionDecisionReject(
                    feedback="All material for this phase (code, plan, existing tests, "
                             "file listings) is already inlined in your prompt. Do not "
                             "read source files. You MAY read and write your own output "
                             "file. Produce your output file now."
                )
            # read / url / memory / etc -> approve for the PoC (tighten later)
            self.log(f"  . {kind} request -> approved")
            return PermissionDecisionApproveOnce()

        # Build the prompt: capability layer (skills/instructions) + phase task.
        capability, cap_manifest = _load_capability_layer(repo_root, phase)
        task = _phase_instruction(phase, run, repo_root,
                                  review_dossier=review_dossier,
                                  unittest_dossier=unittest_dossier)
        full_prompt = (capability + "\n\n" if capability else "") + task

        # ---- CAPABILITY MANIFEST: log + persist the proof of injection ----
        # One human-readable log line per phase, plus a merge-written JSON in the
        # workspace (collected into the audit trail). This is the auditable evidence
        # that the organization's approved skills/templates were DELIVERED to the
        # model this run — hash-verified, independent of SDK skill telemetry.
        def _fmt(items):
            return "[" + ", ".join(f"{i['name']}({i['sha256'][:8]})" for i in items) + "]"
        self.log(f"  [capability] phase '{phase.id}': "
                 f"skills={_fmt(cap_manifest['skills'])} "
                 f"templates={_fmt(cap_manifest['assets'])} "
                 f"instructions={len(cap_manifest['instructions'])} file(s)")
        try:
            mf_path = repo_root / ".harness" / "capability-manifest.json"
            existing = {}
            if mf_path.exists():
                try:
                    existing = json.loads(mf_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
            phases = existing.get("phases", {})
            phases[phase.id] = {
                "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **cap_manifest,
            }
            existing["phases"] = phases
            mf_path.parent.mkdir(parents=True, exist_ok=True)
            mf_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception as e:
            self.log(f"  [capability] manifest write failed (non-fatal): {e}")

        # per-phase model: config phase_models[phase_id] overrides the default model.
        from config import HarnessConfig as _HC
        _cfg = _HC.load(repo_root / ".harness")
        phase_model = (_cfg.model_for_phase(phase.id) if _cfg else None) or self.model
        self.log(f"  [model] phase '{phase.id}' -> {phase_model}")
        if phase.id == "code_review" and _cfg and _cfg.review_model_conflict():
            self.log("  [warn] reviewer model == coding model — review independence is "
                     "reduced; set review_model to a different model for a true "
                     "independent review.")

        # working_directory scopes the agent to the petclinic repo so writes match
        # our globs. use_logged_in_user=True rides on your authed Copilot CLI (Pro).
        # Auth: pass the token EXPLICITLY to the client (the reliable path).
        # Env-var auto-detection doesn't always propagate to the spawned CLI
        # subprocess in CI, so we read it here and hand it to CopilotClient via
        # github_token=. When a token is present, the SDK sets use_logged_in_user
        # to False automatically. Locally (no token), fall back to logged-in user.
        ci_token = (os.environ.get("COPILOT_GITHUB_TOKEN")
                    or os.environ.get("GH_TOKEN")
                    or os.environ.get("GITHUB_TOKEN"))

        client_kwargs = {"working_directory": str(repo_root)}
        if ci_token:
            client_kwargs["github_token"] = ci_token
        else:
            client_kwargs["use_logged_in_user"] = True

        async with CopilotClient(**client_kwargs) as client:
            async with await client.create_session(
                on_permission_request=on_permission_request,
                model=phase_model,
            ) as session:
                done = asyncio.Event()
                last_message = {"text": ""}
                usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}
                seen_events = {}
                errors = []
                # capability attribution: which skills the SDK loaded (definitive)
                # and which tools it invoked (inferred from tool.execution_start).
                skills_loaded: list[str] = []
                tools_invoked: list[str] = []

                def on_event(event):
                    t = event.type.value
                    # DIAGNOSTIC: count every event type so we can see what the SDK emits in CI
                    seen_events[t] = seen_events.get(t, 0) + 1
                    if t == "assistant.message":
                        try:
                            last_message["text"] = event.data.content or ""
                        except Exception:
                            pass
                    elif t == "assistant.usage":
                        try:
                            d = event.data
                            usage["input"] += getattr(d, "input_tokens", 0) or 0
                            usage["output"] += getattr(d, "output_tokens", 0) or 0
                            usage["cache_read"] += getattr(d, "cache_read_tokens", 0) or 0
                            usage["cache_write"] += getattr(d, "cache_write_tokens", 0) or 0
                            usage["reasoning"] += getattr(d, "reasoning_tokens", 0) or 0
                        except Exception:
                            pass
                    elif "error" in t.lower():
                        # capture any error-type event so failures aren't silent
                        try:
                            errors.append(f"{t}: {getattr(event.data, 'message', str(event.data))[:300]}")
                        except Exception:
                            errors.append(t)
                    elif t == "session.skills_loaded":
                        # DEFINITIVE: the skills the SDK actually loaded this session.
                        try:
                            for n in _extract_names(event.data, "skills", "loaded", "items"):
                                if n not in skills_loaded:
                                    skills_loaded.append(n)
                        except Exception:
                            pass
                    elif t in ("tool.execution_start", "tool.execution_complete"):
                        # INFERRED USE: a skill/tool was invoked. Skills surface as
                        # tool calls, so this is the closest signal to "was used".
                        # Record on _start (the canonical trigger); _complete is a
                        # harmless fallback if _start was missed.
                        try:
                            for n in _extract_names(event.data, "tools", "tool", "items"):
                                if n not in tools_invoked:
                                    tools_invoked.append(n)
                        except Exception:
                            pass
                    # terminal events: idle OR any completion-style event ends the turn
                    if t in ("session.idle", "session.completed", "turn.completed", "session.error"):
                        done.set()

                session.on(on_event)
                await session.send(full_prompt)
                # guard against hanging forever if no terminal event arrives
                try:
                    await asyncio.wait_for(done.wait(), timeout=300)
                except asyncio.TimeoutError:
                    self.log("  [diag] timed out waiting for a terminal event")

                # DIAGNOSTICS — what did the SDK actually emit?
                self.log(f"  [diag] events seen: {seen_events}")
                if errors:
                    for e in errors:
                        self.log(f"  [diag] ERROR EVENT: {e}")

                # CAPABILITY ATTRIBUTION — what loaded vs what was invoked.
                # 'configured' is what config.phase_skills SAID should load this
                # phase; 'loaded' is what the SDK reported loading; 'invoked' is
                # what was actually called. loaded != invoked (loaded only means
                # available to the model).
                try:
                    from config import HarnessConfig as _HC2
                    _cfg2 = _HC2.load(repo_root / ".harness")
                    _configured = list((_cfg2.phase_skills or {}).get(phase.id, [])) if _cfg2 else []
                except Exception:
                    _configured = []
                self.log(f"  [skills] phase '{phase.id}': "
                         f"configured={_configured or '(none)'} | "
                         f"loaded={skills_loaded or '(none reported)'} | "
                         f"invoked={tools_invoked or '(none reported)'}")

                if last_message["text"]:
                    self.log("  [agent] " + last_message["text"][:500])

                # report token usage for this phase
                total = usage["input"] + usage["output"]
                if total > 0:
                    self.log(f"  [tokens] phase '{phase.id}': "
                             f"in={usage['input']} out={usage['output']} "
                             f"cache_r={usage['cache_read']} reasoning={usage['reasoning']}")
                # stash on the result so the orchestrator can aggregate
                self._last_usage = usage
                self._last_skills = skills_loaded
                self._last_tools = tools_invoked

        return AgentResult(attempted_writes=attempted_writes, iterations_used=1,
                           tokens=getattr(self, "_last_usage", {}),
                           skills_loaded=getattr(self, "_last_skills", []),
                           tools_invoked=getattr(self, "_last_tools", []))
