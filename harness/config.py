"""
config.py — externalized harness configuration (read from .harness/config.yaml).

Keeps the test command, working module, and validation scope out of code, matching
the toolkit convention of config-driven behaviour. If config.yaml is absent, sane
defaults for spring-petclinic on Windows are used.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import re

try:
    import yaml  # pyyaml
except Exception:
    yaml = None


def _module_is_generated(repo_root: Path, module: str) -> bool:
    """Heuristic: does this module look like GENERATED/openapi code rather than the
    hand-written application? Naming-independent signals, in priority order:

      1. Its pom.xml runs an OpenAPI/codegen plugin (openapi-generator, swagger-codegen,
         or a build-helper wiring generated-sources) — the strongest signal.
      2. Its sources live under a generated-sources path, or every *.java sits in an
         `api`/`model`/`generated` package with NO Spring wiring (no @SpringBootApplication,
         @RestController, @Service, @Configuration, @Component).

    A module that has real Spring wiring is the APPLICATION module, not generated.
    """
    mpom = repo_root / module / "pom.xml"
    try:
        if mpom.exists():
            ptext = mpom.read_text(encoding="utf-8", errors="replace").lower()
            for marker in ("openapi-generator", "swagger-codegen", "openapi-generator-maven-plugin",
                           "generated-sources", "build-helper-maven-plugin"):
                if marker in ptext:
                    return True
    except Exception:
        pass
    # Inspect the module's Java sources for Spring wiring.
    src = repo_root / module / "src" / "main" / "java"
    try:
        spring_markers = ("@SpringBootApplication", "@RestController", "@Service",
                          "@Configuration", "@Component", "@Repository")
        saw_java = False
        for jf in src.rglob("*.java"):
            saw_java = True
            head = jf.read_text(encoding="utf-8", errors="replace")
            if any(mk in head for mk in spring_markers):
                return False  # has Spring wiring => application module, not generated
        # Java present but ZERO Spring wiring anywhere => looks generated (stubs/DTOs only).
        if saw_java:
            return True
    except Exception:
        pass
    return False


def detect_application_module(repo_root: Path) -> str | None:
    """Auto-detect the application (hand-written) module in a multi-module repo.

    BCBSM services are aggregator repos: a root pom.xml with packaging=pom lists
    sub-modules, typically <svc>-application (hand-written) and <svc>-openapi-code
    (generated). Module folder names do NOT match the repo name, so we read the
    layout from the POM/filesystem rather than guessing from the repo name.

    Strategy (naming-independent):
      1. Read <modules> from the root pom.xml.
      2. Keep modules that contain src/main/java (candidate hand-written code).
      3. If more than one qualifies, DROP any that look generated (openapi/codegen
         plugin, or Java with no Spring wiring). This resolves the common
         two-module case where the generated module also ships .java stubs.
      4. If exactly one remains, return it. Otherwise None (caller uses the explicit
         target_module override).

    Returns the module directory name (repo-relative), or None when genuinely
    ambiguous or single-module.
    """
    try:
        root_pom = repo_root / "pom.xml"
        if not root_pom.exists():
            return None
        text = root_pom.read_text(encoding="utf-8", errors="replace")
        mods = re.findall(r"<module>\s*([^<]+?)\s*</module>", text)
        if not mods:
            return None
        candidates = []
        for m in mods:
            m = m.strip().strip("/")
            if (repo_root / m / "src" / "main" / "java").is_dir():
                candidates.append(m)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # Disambiguate: drop modules that look generated.
            non_generated = [m for m in candidates
                             if not _module_is_generated(repo_root, m)]
            if len(non_generated) == 1:
                return non_generated[0]
        # Still ambiguous (0, or >1 real app modules): use explicit override.
        return None
    except Exception:
        return None


def detect_generated_modules(repo_root: Path, application_module: str | None) -> list:
    """Return the non-application sub-modules (generated stubs, etc.) so they can
    be write-excluded. Any <module> that is NOT the application module is treated
    as protected/generated for write purposes."""
    try:
        root_pom = repo_root / "pom.xml"
        if not root_pom.exists():
            return []
        text = root_pom.read_text(encoding="utf-8", errors="replace")
        mods = [m.strip().strip("/") for m in
                re.findall(r"<module>\s*([^<]+?)\s*</module>", text)]
        return [m for m in mods if m and m != application_module]
    except Exception:
        return []


@dataclass
class HarnessConfig:
    # Command to run tests. {test_filter} is substituted (or removed) at runtime.
    # On Windows petclinic ships mvnw.cmd; on *nix it's ./mvnw.
    test_command: str = "mvnw.cmd test"
    # Optional targeted filter for a fast loop. Empty => full suite.
    test_filter: str = "OwnerTest"
    # Seconds before the test run is killed (guards a hung build).
    test_timeout: int = 600
    # Default model for live runs.
    model: str = "gpt-5-mini"
    # Per-phase model override. A phase uses phase_models[phase_id] if present,
    # else falls back to `model`. Put cheap models on simple phases, stronger
    # models on coding/testing.
    phase_models: dict = field(default_factory=dict)
    # --- cost estimation (token -> credit -> $) ---
    # GitHub Copilot moved to USAGE-BASED billing on 2026-06-01. Under that model
    # there are no "premium request multipliers" (that is the legacy request-based
    # system) and there are NO zero-cost "included" models: every model is priced
    # PER TOKEN, and the total converts to AI credits at 1 credit = $0.01.
    #
    # Rates below are USD per 1M tokens: [input, cached_input, output].
    # Source: docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
    # (verified 2026-07-12). Rates change — re-check when they do.
    #
    # NOTE on gpt-5-mini: it was previously treated as free ("included"). That is
    # WRONG under usage-based billing — it bills at $0.25/$0.025/$2.00 per 1M.
    # Treating it as 0 under-reported a real run by ~2.2x (7.3 cr est vs 16 cr billed).
    model_rates: dict = field(default_factory=lambda: {
        # OpenAI
        "gpt-5-mini":        [0.25, 0.025, 2.00],
        "gpt-5.4-mini":      [0.75, 0.075, 4.50],
        "gpt-5.4-nano":      [0.20, 0.02,  1.25],
        "gpt-5.4":           [2.50, 0.25, 15.00],
        "gpt-5.3-codex":     [1.75, 0.175, 14.00],
        "gpt-5.5":           [5.00, 0.50, 30.00],
        # Anthropic (also carry a cache-WRITE rate, see cache_write_rates)
        "claude-haiku-4.5":  [1.00, 0.10,  5.00],
        "claude-sonnet-4.5": [3.00, 0.30, 15.00],
        "claude-sonnet-4.6": [3.00, 0.30, 15.00],
        "claude-sonnet-5":   [2.00, 0.20, 10.00],
        "claude-opus-4.5":   [5.00, 0.50, 25.00],
        # Google
        "gemini-2.5-pro":    [1.25, 0.125, 10.00],
        "gemini-3-flash":    [0.50, 0.05,  3.00],
        "gemini-3.5-flash":  [1.50, 0.15,  9.00],
        # Fine-tuned / others
        "raptor-mini":       [0.25, 0.025, 2.00],
        "mai-code-1-flash":  [0.75, 0.075, 4.50],
    })
    # Anthropic models bill cache WRITES separately (USD per 1M tokens). The SDK
    # does not report cache-write tokens, so we cannot price them — they are the
    # main known source of UNDER-estimation on Anthropic phases. Kept here for
    # documentation and future use if the SDK starts reporting them.
    cache_write_rates: dict = field(default_factory=lambda: {
        "claude-haiku-4.5":  1.25,
        "claude-sonnet-4.5": 3.75,
        "claude-sonnet-4.6": 3.75,
        "claude-sonnet-5":   2.50,
        "claude-opus-4.5":   6.25,
    })
    # Deterministic normalization run by the HARNESS before validation.
    # spring-javaformat:apply rewrites files to the project's required format.
    # Empty string => skip. This is a fixed, known goal — not arbitrary execution.
    pre_validation_command: str = "mvnw.cmd spring-javaformat:apply"
    # When validation fails, loop back to this phase to fix the code, carrying the
    # failure as feedback. Empty string => don't loop, just halt.
    validation_loopback_phase: str = "coding"
    # Max code<->test cycles before giving up and halting for a human.
    # Guards against infinite loops burning credits.
    max_validation_retries: int = 3
    # --- coverage gate ---
    # If > 0, the harness checks line coverage after tests and fails validation
    # when coverage is below this percent. 0 => coverage gate disabled.
    min_coverage: float = 0.0
    # Coverage scope:
    #   "changed" => gate ONLY on the classes the coding phase wrote this run
    #                (per-change coverage — "did MY change get tested").
    #   "global"  => gate on the whole module (legacy behaviour).
    coverage_scope: str = "changed"
    # On a coverage MISS (tests pass but coverage below threshold), loop back to
    # THIS phase to add more tests. Distinct from validation_loopback_phase, which
    # handles RED tests. Coverage is a test problem => loop to unit_testing.
    coverage_loopback_phase: str = "unit_testing"
    # Max coverage-driven retries before halting for a human (separate budget from
    # max_validation_retries, which governs red-test loopbacks).
    max_coverage_retries: int = 2
    # Goal that produces the JaCoCo CSV. report binds to prepare-package, so we
    # explicitly invoke jacoco:report after tests. Scoped to the filter if set.
    coverage_command: str = "mvnw.cmd jacoco:report"
    # Where JaCoCo writes the CSV (repo-relative).
    coverage_csv: str = "target/site/jacoco/jacoco.csv"
    # Which metric to gate on: LINE, BRANCH, INSTRUCTION, METHOD.
    coverage_metric: str = "LINE"
    # Where the harness reads the user story from (repo-relative). In production an
    # MCP/Jira step writes this file; for the demo it's committed to the repo.
    story_file: str = "stories/current-story.md"
    # Directory the clarification gate scans for the newest context file.
    context_output_dir: str = ".github/story-context-files"
    # ---- selective capability loading ----
    # Master switch: if False, fall back to loading ALL capability files every phase.
    selective_capability: bool = True
    # Which stacks this repo contains. Instruction subfolders NOT in this list are
    # excluded entirely (e.g. a backend-only repo skips angular-frontend/ and
    # mobile-frontend/). Values match the instruction subfolder names. Files
    # directly under instructions/ (not in a stack subfolder) are always considered
    # (these are the cross-cutting guardrails like hipaa, logging-standards).
    # Set to [] or ["*"] to include all stacks.
    repo_stacks: list = field(default_factory=lambda: ["backend"])
    # Glob(s) representing the files each phase is concerned with. An instruction
    # file loads in a phase if its `applyTo` glob intersects the phase's scope.
    # applyTo "**" files are always-on guardrails (load in every phase).
    # Keyed by phase id.
    phase_file_scope: dict = field(default_factory=lambda: {
        "context":      ["src/main/java/**", "src/main/**"],
        "prompt_steps": ["src/main/java/**", "src/main/**"],
        "coding":       ["src/main/java/**"],
        "unit_testing": ["src/test/java/**"],
        "documentation": ["docs/**"],
        "raise_pr":     [],
    })
    # Which named skills (folder names under .github/skills) load in which phase.
    # Skills have no path scope, so this mapping is explicit.
    phase_skills: dict = field(default_factory=lambda: {
        "context":      ["build-context", "analyze-service"],
        "prompt_steps": ["build-prompt-steps"],
        "coding":       ["security-review"],
        "code_review":  ["security-review", "review-angular-code"],
        "unit_testing": [],
        "documentation": [],
        "raise_pr":     [],
    })
    # --- code review gate ---
    # Reviewer model. Should DIFFER from the coding model (independent reviewer).
    # If empty, model_for_phase falls back to phase_models/default like any phase.
    # A same-as-coding value is allowed but the harness WARNS (per user choice).
    review_model: str = ""
    # On a review MISS (reviewer reports issues), loop back to this phase.
    review_loopback_phase: str = "coding"
    # Max review-driven retries before halting for a human (independent budget).
    max_review_retries: int = 2

    # --- SCOPE GATE ---
    # How many times the coding phase may be sent back for CREATING production
    # files the approved plan never listed, before halting for a human. Unplanned
    # EDITS to existing files are only warned about; only new-file creation trips
    # this gate.
    max_scope_retries: int = 2

    # --- MULTI-MODULE MAVEN (backlog #10) ---
    # The hand-written application module (repo-relative dir name). BCBSM services
    # are aggregator repos: root pom + <svc>-application (code) + <svc>-openapi-code
    # (generated). Empty => single-module repo (petclinic) OR auto-detect at load.
    # An explicit value here OVERRIDES auto-detection (escape-hatch for odd repos).
    target_module: str = ""
    # Modules that are generated / must never be hand-edited (repo-relative dirs).
    # Auto-populated at load with every non-application module unless set here.
    generated_module: list = field(default_factory=list)
    # Always-deny write globs — deny WINS over a phase's allowed_writes. Protects
    # generated code even when it sits inside an otherwise-writable module.
    # The generated module(s) are added automatically; these are extra patterns.
    write_exclude: list = field(default_factory=lambda: [
        "**/generated/**",       # generated sources
        "**/*MapperImpl.java",   # MapStruct-generated mapper impls
    ])

    @classmethod
    def load(cls, harness_dir: Path, repo_root: Path | None = None) -> "HarnessConfig":
        p = harness_dir / "config.yaml"
        data = {}
        if p.exists() and yaml is not None:
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        # repo root holds the pom.xml; .harness/ sits directly under it.
        if repo_root is None:
            repo_root = harness_dir.parent

        cfg = cls(
            test_command=data.get("test_command", cls.test_command),
            test_filter=data.get("test_filter", cls.test_filter),
            test_timeout=int(data.get("test_timeout", cls.test_timeout)),
            model=data.get("model", cls.model),
            phase_models=data.get("phase_models") or {},
            model_rates=data.get("model_rates") or cls().model_rates,
            cache_write_rates=data.get("cache_write_rates") or cls().cache_write_rates,
            pre_validation_command=data.get("pre_validation_command", cls.pre_validation_command),
            validation_loopback_phase=data.get("validation_loopback_phase", cls.validation_loopback_phase),
            max_validation_retries=int(data.get("max_validation_retries", cls.max_validation_retries)),
            min_coverage=float(data.get("min_coverage", cls.min_coverage)),
            coverage_scope=data.get("coverage_scope", cls.coverage_scope),
            coverage_loopback_phase=data.get("coverage_loopback_phase", cls.coverage_loopback_phase),
            max_coverage_retries=int(data.get("max_coverage_retries", cls.max_coverage_retries)),
            coverage_command=data.get("coverage_command", cls.coverage_command),
            coverage_csv=data.get("coverage_csv", cls.coverage_csv),
            coverage_metric=data.get("coverage_metric", cls.coverage_metric),
            story_file=data.get("story_file", cls.story_file),
            context_output_dir=data.get("context_output_dir", cls.context_output_dir),
            selective_capability=bool(data.get("selective_capability", cls.selective_capability)),
            repo_stacks=data.get("repo_stacks") if data.get("repo_stacks") is not None else cls().repo_stacks,
            phase_file_scope=data.get("phase_file_scope") or cls().phase_file_scope,
            phase_skills=data.get("phase_skills") or cls().phase_skills,
            review_model=data.get("review_model", cls.review_model),
            review_loopback_phase=data.get("review_loopback_phase", cls.review_loopback_phase),
            max_review_retries=int(data.get("max_review_retries", cls.max_review_retries)),
            max_scope_retries=int(data.get("max_scope_retries", cls.max_scope_retries)),
            target_module=data.get("target_module", "") or "",
            generated_module=data.get("generated_module") or [],
            write_exclude=(data.get("write_exclude")
                           if data.get("write_exclude") is not None
                           else cls().write_exclude),
        )

        # --- coverage threshold units normalization ---
        # `min_coverage` is compared against a JaCoCo percentage (0-100), but it is
        # natural to write it as a fraction (0.90) in YAML — and doing so silently
        # DISABLES the gate: `85.0 < 0.90` is False, so anything above 0.9% passes.
        # Accept both spellings and normalize to a percentage. A value in (0, 1] is
        # read as a fraction; anything above 1 is already a percentage. (A literal
        # 1% threshold is not a meaningful gate, so the ambiguity costs nothing.)
        if 0 < cfg.min_coverage <= 1.0:
            cfg.min_coverage = cfg.min_coverage * 100.0

        # --- multi-module resolution (backlog #10) ---
        # Precedence: explicit config value > auto-detect from pom.xml > none.
        if not cfg.target_module:
            detected = detect_application_module(repo_root)
            if detected:
                cfg.target_module = detected
        # Populate generated modules (everything that is NOT the app module) unless
        # the config named them explicitly. These become write-excluded.
        if cfg.target_module and not cfg.generated_module:
            cfg.generated_module = detect_generated_modules(repo_root, cfg.target_module)
        # Every generated module dir is an always-deny write path.
        for gm in cfg.generated_module:
            glob = f"{gm}/**"
            if glob not in cfg.write_exclude:
                cfg.write_exclude.append(glob)

        return cfg

    def module_writes(self, allowed_globs) -> tuple:
        """Rewrite a phase's allowed_writes for a multi-module repo (Option A).

        Module-relative globs (src/main/**, src/test/**, docs/**) are prefixed with
        the detected application module so writes land in <module>/src/main/** rather
        than the repo root. Globs already anchored (starting with '.harness', '.github',
        or the module name, or containing a '/') are left untouched.

        Single-module repos (target_module empty) are unchanged — petclinic still works.
        """
        if not self.target_module:
            return tuple(allowed_globs)
        MODULE_RELATIVE = ("src/main/", "src/test/", "src/", "docs/")
        out = []
        for g in allowed_globs:
            if any(g.startswith(pfx) for pfx in MODULE_RELATIVE):
                out.append(f"{self.target_module}/{g}")
            else:
                out.append(g)  # .harness/**, .github/**, etc. stay repo-root-relative
        return tuple(out)

    def resolved_coverage_csv(self, repo_root: Path) -> Path | None:
        """Locate the JaCoCo CSV, module-aware, WITHOUT hardcoding a module name.

        `coverage_csv` is configured module-relative (target/site/jacoco/jacoco.csv)
        because that is where JaCoCo writes it *within a module*. In a single-module
        repo that is also the repo root, so the raw value works. In an aggregator
        repo the real file is <app-module>/target/site/jacoco/jacoco.csv, and reading
        the un-prefixed path finds nothing — the coverage gate then reports
        'unmeasurable' and loops until retries exhaust even though tests are green.

        Resolution order (first hit wins):
          1. the configured path as-is (absolute, or single-module repo root)
          2. the configured path prefixed with the detected application module
          3. the configured path under any <module>/ named in the root pom
          4. a bounded rglob for the configured FILENAME anywhere under the repo,
             preferring the application module, then the shallowest match

        Step 4 is the safety net: it survives an unusual JaCoCo outputDirectory, an
        aggregated report module, or a layout this code has not seen. Returns None
        only when the file genuinely does not exist (JaCoCo not configured/not run),
        which is a real, actionable failure rather than a path bug.
        """
        configured = Path(self.coverage_csv)
        if configured.is_absolute() and configured.is_file():
            return configured

        # 1. as configured, relative to the repo root (single-module case)
        cand = repo_root / configured
        if cand.is_file():
            return cand

        # 2. prefixed with the detected application module
        if self.target_module:
            cand = repo_root / self.target_module / configured
            if cand.is_file():
                return cand

        # 3. any module declared in the root pom (covers a dedicated report module)
        for mod in self._pom_modules(repo_root):
            cand = repo_root / mod / configured
            if cand.is_file():
                return cand

        # 4. last resort: find the file by name, preferring the app module, then
        #    the shallowest path (avoids picking a nested/aggregated duplicate).
        name = configured.name
        try:
            matches = [p for p in repo_root.rglob(name) if p.is_file()]
        except Exception:
            matches = []
        if matches:
            def _rank(p: Path):
                try:
                    rel = p.relative_to(repo_root)
                except Exception:
                    return (2, 99, str(p))
                in_app = (self.target_module
                          and rel.parts and rel.parts[0] == self.target_module)
                return (0 if in_app else 1, len(rel.parts), str(rel))
            return sorted(matches, key=_rank)[0]

        return None

    @staticmethod
    def _pom_modules(repo_root: Path) -> list:
        """<module> entries from the root pom, or [] if none/unreadable."""
        try:
            root_pom = repo_root / "pom.xml"
            if not root_pom.exists():
                return []
            text = root_pom.read_text(encoding="utf-8", errors="replace")
            return [m.strip().strip("/") for m in
                    re.findall(r"<module>\s*([^<]+?)\s*</module>", text) if m.strip()]
        except Exception:
            return []

    @staticmethod
    def source_to_class_key(rel_path: str) -> tuple | None:
        """Map a repo-relative .java source path to (package, ClassName) for CSV
        lookup — module-agnostic.

        JaCoCo's CSV keys rows by PACKAGE + CLASS, not by file path, so the gate has
        to derive the fully-qualified name from the changed-file path. Deriving it by
        a FIXED path depth (e.g. parts[3:] to skip src/main/java) breaks the moment a
        module directory sits in front, which is exactly the multi-module case: the
        derived package comes out wrong and every changed class silently misses in
        the CSV — indistinguishable from 'no coverage data'.

        Anchor on the 'src/main/java' segment positionally instead, so the same code
        works for src/main/java/... and <any-module>/src/main/java/... alike.
        Returns None for paths that are not main Java sources.
        """
        norm = str(rel_path).replace("\\", "/")
        if not norm.endswith(".java"):
            return None
        seg = norm.split("/")
        idx = None
        for i in range(len(seg) - 2):
            if seg[i] == "src" and seg[i + 1] == "main" and seg[i + 2] == "java":
                idx = i + 3
                break
        if idx is None or idx >= len(seg):
            return None
        pkg_parts = seg[idx:-1]
        class_name = seg[-1][:-len(".java")]
        return (".".join(pkg_parts), class_name)

    def model_for_phase(self, phase_id: str) -> str:
        """Model for a phase: phase_models[phase_id] if set, else the default.
        For 'code_review', review_model wins when set (independent reviewer)."""
        if phase_id == "code_review" and self.review_model:
            return self.review_model
        return (self.phase_models or {}).get(phase_id, self.model)

    def review_model_conflict(self) -> bool:
        """True when the reviewer model resolves to the SAME model as coding —
        an independence smell. Caller (runner) emits a warning; not a hard fail."""
        return self.model_for_phase("code_review") == self.model_for_phase("coding")

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int,
                      cached_tokens: int = 0, cache_write_tokens: int = 0) -> dict:
        """Estimate credits + USD for a phase's token usage. 1 credit = $0.01.

        Usage-based billing (GitHub, from 2026-06-01): every model bills per token.
        There is no zero-cost 'included' tier and no request multipliers.

        Token split:
          - `input_tokens`  : TOTAL prompt tokens (cache reads INCLUDED in this).
          - `cached_tokens` : the cache-read portion of the above; bills at ~10%
                              of the fresh-input rate, so we subtract and reprice.
          - `cache_write_tokens`: Anthropic-only; billed at its own higher rate.

        Returns {credits, usd, included, partial}. `included` is always False and
        is retained so older log entries/readers keep working. `partial` is True
        when a billable component exists for this model but was NOT reported by
        the SDK (so the figure is a known under-estimate).
        """
        rates = (self.model_rates or {}).get(model)
        if not rates:
            # Unknown model -> refuse to guess. A blank beats a wrong number.
            return {"credits": None, "usd": None, "included": False, "partial": False}
        in_rate, cached_rate, out_rate = rates[0], rates[1], rates[2]
        cached = max(int(cached_tokens or 0), 0)
        cwrite = max(int(cache_write_tokens or 0), 0)
        fresh_in = max(int(input_tokens or 0) - cached, 0)

        usd = (fresh_in * in_rate
               + cached * cached_rate
               + int(output_tokens or 0) * out_rate) / 1_000_000.0

        # CACHE-WRITE: reported by the SDK, but deliberately NOT priced.
        #
        # GitHub publishes a separate cache-write rate for Anthropic models
        # ($1.25/1M for Haiku 4.5), so charging it looked correct on paper. It is
        # not, measured against real billing:
        #     run 29174592092 (16 cr billed): excl. cache-write -> 14.5 cr  (91%)
        #     run 29181773991 (27 cr billed): excl. cache-write -> 25.3 cr  (94%)
        #                                     incl. cache-write -> 36.8 cr (136%)
        # Two runs, both accurate when cache-write is excluded, both badly over
        # when it is included. The most likely explanation is that the SDK's
        # cache_write counter is not the billable class GitHub's rate refers to
        # (or those tokens are already inside `input`). Either way: measurement
        # beats inference. We report the count and do not charge for it.
        cw_rate = (self.cache_write_rates or {}).get(model)
        _ = (cw_rate, cache_write_tokens)  # intentionally unused — see above

        # The estimate now runs slightly UNDER actual (~91-94%), so it is a lower
        # bound rather than an upper one.
        partial = False

        return {"credits": usd / 0.01, "usd": usd,
                "included": False, "partial": partial}

    def resolved_test_command(self) -> str:
        """Build the actual command string, applying the targeted filter if set."""
        if self.test_filter:
            # Maven Surefire targeted run
            return f"{self.test_command} -Dtest={self.test_filter}"
        return self.test_command    