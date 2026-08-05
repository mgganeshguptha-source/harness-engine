"""
validation.py — the deterministic validation gate.

THE HARNESS runs the tests, not the agent. After the unit_testing phase, the state
machine calls run_validation(); it shells out to the configured test command FROM
THE HARNESS (this is allowed — it's our trusted code, not the model), captures the
exit code, and returns pass/fail. A non-zero exit => VALIDATION_FAILED => the run
HALTS before documentation/PR.

This is the interlock that makes "you cannot ship red tests" a guarantee rather
than a hope: the green run is verified by code, as a precondition to proceeding.
"""
from __future__ import annotations
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from config import HarnessConfig


@dataclass
class ValidationResult:
    passed: bool
    exit_code: int
    summary: str
    output_tail: str
    # Why validation failed, so the state machine can route correctly:
    #   None       -> passed
    #   "test"     -> tests are RED  => loop back to coding
    #   "coverage" -> tests pass but per-change coverage below threshold
    #                 => loop back to unit_testing
    failure_kind: str | None = None
    # Coverage detail (populated when the coverage gate ran), for messaging.
    coverage_pct: float | None = None
    coverage_target: float | None = None
    coverage_classes: list = field(default_factory=list)   # classes measured
    coverage_covered: int = 0
    coverage_missed: int = 0


def _java_file_to_class_key(rel_path: str) -> tuple[str, str] | None:
    """Map a repo-relative .java source path to (package, ClassName) as JaCoCo
    reports them in its CSV.

    'src/main/java/org/springframework/samples/petclinic/owner/Owner.java'
      -> ('org.springframework.samples.petclinic.owner', 'Owner')
    Returns None for non-source or unparseable paths.
    """
    p = rel_path.replace("\\", "/").strip()
    if not p.endswith(".java"):
        return None
    # locate the source root marker
    for marker in ("src/main/java/", "src/test/java/", "src/main/kotlin/"):
        i = p.find(marker)
        if i != -1:
            pkgpath = p[i + len(marker):]
            break
    else:
        # no recognizable source root; fall back to basename only
        pkgpath = p.rsplit("/", 1)[-1]
    cls = pkgpath.rsplit("/", 1)[-1][:-len(".java")]
    pkg_dir = pkgpath[: -len(pkgpath.rsplit("/", 1)[-1])].rstrip("/")
    package = pkg_dir.replace("/", ".")
    return (package, cls)


def _parse_changed_coverage(csv_path: Path, metric: str,
                            changed_files: list) -> tuple[float | None, int, int, list]:
    """Per-change coverage: compute `metric` coverage over ONLY the JaCoCo rows
    whose (PACKAGE, CLASS) match the changed source files.

    Returns (percent | None, covered, missed, measured_class_labels).
    percent is None if the report is missing/unreadable OR none of the changed
    classes appear in the report (e.g. brand-new class with no test touching it
    still appears with 0 covered; truly absent => None so caller can decide).
    """
    if not csv_path.exists():
        return (None, 0, 0, [])

    # Build the set of (package, class) we care about. JaCoCo emits nested/inner
    # classes as 'Owner.Inner' or 'Owner$1'; match the top-level class as a prefix.
    wanted = set()
    for f in changed_files or []:
        key = _java_file_to_class_key(f)
        if key:
            wanted.add(key)
    if not wanted:
        return (None, 0, 0, [])

    try:
        import csv as _csv
        covered = missed = 0
        measured = []
        matched_any = False
        with csv_path.open(encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                pkg = row.get("PACKAGE", "")
                cls = row.get("CLASS", "")
                # top-level class name (strip inner-class suffixes)
                top = cls.split("$", 1)[0].split(".", 1)[0]
                if (pkg, top) in wanted:
                    matched_any = True
                    m = int(row[f"{metric}_MISSED"])
                    c = int(row[f"{metric}_COVERED"])
                    missed += m
                    covered += c
                    measured.append(f"{pkg}.{cls}")
        if not matched_any:
            return (None, 0, 0, [])
        total = covered + missed
        if total == 0:
            # class(es) matched but have zero of this metric (e.g. an interface).
            # Treat as 100% — nothing to cover — rather than a failure.
            return (100.0, covered, missed, measured)
        return (100.0 * covered / total, covered, missed, measured)
    except Exception:
        return (None, 0, 0, [])


def _parse_coverage(csv_path: Path, metric: str) -> float | None:
    """Global coverage across ALL rows (legacy 'global' scope)."""
    if not csv_path.exists():
        return None
    try:
        import csv as _csv
        covered = missed = 0
        with csv_path.open(encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                missed += int(row[f"{metric}_MISSED"])
                covered += int(row[f"{metric}_COVERED"])
        total = covered + missed
        if total == 0:
            return None
        return 100.0 * covered / total
    except Exception:
        return None


def run_validation(repo_root: Path, harness_dir: Path, log=print,
                   changed_files: list | None = None) -> ValidationResult:
    cfg = HarnessConfig.load(harness_dir)

    # ---- DETERMINISTIC PRE-STEP: normalize formatting ----
    # The harness applies the project's required format (spring-javaformat) before
    # validating. This is a fixed, known goal run by trusted harness code — not the
    # agent, and not arbitrary execution. It turns a mechanical "format violation"
    # into a non-issue so validation tests true correctness, not whitespace.
    if cfg.pre_validation_command:
        log(f"  [harness] formatting: {cfg.pre_validation_command}")
        try:
            fmt = subprocess.run(
                cfg.pre_validation_command,
                cwd=str(repo_root), shell=True, capture_output=True,
                text=True, timeout=cfg.test_timeout,
            )
            if fmt.returncode == 0:
                log("  [harness] formatting applied (or already clean)")
            else:
                # Non-fatal: log and continue; validation will catch real problems.
                log(f"  [harness] formatting step returned exit {fmt.returncode} (continuing)")
        except Exception as e:
            log(f"  [harness] formatting step error: {type(e).__name__}: {e} (continuing)")

    cmd = cfg.resolved_test_command()
    log(f"  [harness] running validation: {cmd}")
    log(f"  [harness] cwd: {repo_root}")

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            shell=True,                # needed for mvnw.cmd on Windows
            capture_output=True,
            text=True,
            timeout=cfg.test_timeout,
        )
    except subprocess.TimeoutExpired:
        return ValidationResult(False, -1, "TIMEOUT", f"test run exceeded {cfg.test_timeout}s")
    except Exception as e:
        return ValidationResult(False, -2, f"ERROR: {type(e).__name__}: {e}", "")

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    tail = "\n".join(out.splitlines()[-25:])  # last lines hold the BUILD result

    passed = proc.returncode == 0
    failure_kind = None if passed else "test"
    # Maven prints BUILD SUCCESS / BUILD FAILURE; use exit code as source of truth,
    # the text scan is only for a friendlier summary line.
    if "BUILD SUCCESS" in out:
        summary = "BUILD SUCCESS"
    elif "BUILD FAILURE" in out:
        summary = "BUILD FAILURE"
    else:
        summary = f"exit {proc.returncode}"

    # ---- COVERAGE GATE (only if tests passed and a threshold is set) ----
    coverage_note = ""
    cov_pct = None
    cov_covered = cov_missed = 0
    cov_measured: list = []
    if passed and cfg.min_coverage > 0:
        log(f"  [harness] coverage: {cfg.coverage_command}")
        try:
            subprocess.run(cfg.coverage_command, cwd=str(repo_root), shell=True,
                           capture_output=True, text=True, timeout=cfg.test_timeout)
        except Exception as e:
            log(f"  [harness] coverage command error: {e} (continuing to parse if report exists)")

        csv_path = repo_root / cfg.coverage_csv
        if cfg.coverage_scope == "changed":
            # PER-CHANGE coverage: only the classes the coding phase wrote this run.
            log(f"  [harness] coverage scope: changed classes = "
                f"{changed_files if changed_files else '(none recorded)'}")
            cov_pct, cov_covered, cov_missed, cov_measured = _parse_changed_coverage(
                csv_path, cfg.coverage_metric, changed_files or [])
        else:
            cov_pct = _parse_coverage(csv_path, cfg.coverage_metric)

        if cov_pct is None:
            # Can't measure -> treat as a gate failure (don't silently pass). For
            # "changed" scope this also fires when none of the changed classes
            # appear in the report (misconfigured JaCoCo, or no changed source).
            passed = False
            failure_kind = "coverage"
            summary = "COVERAGE UNREADABLE"
            if cfg.coverage_scope == "changed":
                coverage_note = (
                    f"Could not measure {cfg.coverage_metric} coverage for the changed "
                    f"class(es) {changed_files or '[]'} from {cfg.coverage_csv}. "
                    f"Check that JaCoCo (jacoco-maven-plugin) is configured and that the "
                    f"changed files are main source.")
            else:
                coverage_note = f"Could not read {cfg.coverage_metric} coverage from {cfg.coverage_csv}"
            log("  ! " + coverage_note)
        elif cov_pct < cfg.min_coverage:
            passed = False
            failure_kind = "coverage"
            scope_lbl = "changed-class" if cfg.coverage_scope == "changed" else "global"
            summary = f"COVERAGE {cov_pct:.1f}% < {cfg.min_coverage:.1f}% ({scope_lbl})"
            coverage_note = (
                f"{scope_lbl} {cfg.coverage_metric} coverage {cov_pct:.1f}% is below the "
                f"required {cfg.min_coverage:.1f}% "
                f"(covered={cov_covered}, missed={cov_missed}; "
                f"measured: {', '.join(cov_measured) if cov_measured else 'n/a'})")
            log("  ! " + coverage_note)
        else:
            scope_lbl = "changed-class" if cfg.coverage_scope == "changed" else "global"
            coverage_note = (f"{scope_lbl} {cfg.coverage_metric} coverage {cov_pct:.1f}% "
                             f"(>= {cfg.min_coverage:.1f}%)")
            log(f"  [harness] {coverage_note}")

    # Write a validation report into the workspace (auditable artifact).
    # NOTE: petclinic's nohttp checkstyle scans the whole tree including .harness/,
    # and would flag any literal http:// URL we capture from Maven output. Neutralize
    # such URLs in the persisted report so our own audit file can't fail the build.
    report = harness_dir / "validation-report.txt"
    safe_tail = tail.replace("http://", "hxxp://")
    cov_line = f"coverage: {coverage_note}\n" if coverage_note else ""
    try:
        report.write_text(
            f"command: {cmd}\nexit_code: {proc.returncode}\nsummary: {summary}\n{cov_line}\n--- tail ---\n{safe_tail}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    final_tail = tail if not coverage_note else (tail + "\n\nCOVERAGE: " + coverage_note)
    return ValidationResult(
        passed, proc.returncode, summary, final_tail,
        failure_kind=failure_kind,
        coverage_pct=cov_pct,
        coverage_target=(cfg.min_coverage if cfg.min_coverage > 0 else None),
        coverage_classes=cov_measured,
        coverage_covered=cov_covered,
        coverage_missed=cov_missed,
    )
