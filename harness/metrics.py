"""
metrics.py — one metrics record per run.

WHY ONE FILE PER RUN, NOT ONE SHARED JSONL
A single appended file is the obvious design and the wrong one here: two runs
finishing at once both fetch, append and push, and the second conflicts. Writing
to a unique path per run means no two runs ever touch the same file, so git
merges them cleanly with no locking and no retry logic.

Aggregation is unaffected — `cat metrics/**/*.json | jq -s` gives the same JSONL
you would have had, and pandas reads the directory just as happily. The
concurrency problem simply never arises.

    metrics/2026/08/BOOK-1-31493208647.json

Date-partitioned so a repo with thousands of runs does not end up with one
directory holding all of them.

WRITTEN ONCE, AT THE END
The record is written after the run finishes, `if: always()`, so halted runs are
captured too — those are the interesting ones. Building it incrementally per
phase would leave a partial, misleading record whenever a run crashed, and
RunState already holds everything needed at the end.

FLAT SCHEMA, DELIBERATELY
No nesting: `tokens_coding`, not `tokens: {coding: ...}`. Flat fields map
straight to columns when this eventually moves to a table, and `jq` queries stay
one-liners. Every record carries schema_version because fields WILL be added, and
a loader needs to tell an old record from a new one rather than guess.

WHAT IS DELIBERATELY ABSENT
No story text, no acceptance-criteria text, no code, no file paths, no email
addresses. Ids and counts only. At BCBSM a metrics file containing story bodies
is a PHI/PII question, and the boundary is far cheaper to draw now than to
retrofit across records already written.

The developer identity captured is the GitHub LOGIN, never the email: logins are
already public in the repository, emails are personal data.

A NOTE ON THE ACTOR FIELD
Recording who triggered a run makes cohort analysis possible — did onboarding
help, is a team converging. It also makes it trivially possible to rank
individuals by halt rate, which would be a misuse: a high halt rate usually means
hard stories or a thin repo, not a weak developer, and once people believe they
are measured on it they will write stories that please the gate rather than
stories that are correct.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(part: str, fallback: str = "unknown") -> str:
    """Make a value safe for a filename without silently colliding."""
    cleaned = _SAFE.sub("-", str(part or "")).strip("-")
    return cleaned or fallback


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration_sec(started_at: str | None) -> float | None:
    if not started_at:
        return None
    try:
        t0 = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - t0).total_seconds(), 1)
    except Exception:
        return None


def _phase_sequence(run) -> str:
    """Phases in the order they ran, repeats included.

    Kept as a flat comma string rather than a list: it stays one column in a
    table later, and the loopbacks are readable straight from it — a sequence
    ending "coding,code_review,coding,code_review" tells the whole story.
    """
    seq = list(getattr(run, "phase_history", None) or run.completed_phases or [])
    cur = getattr(run, "current_phase", None)
    if cur and (not seq or seq[-1] != cur):
        seq.append(cur)
    return ",".join(seq)


def build_record(run, repo_root: Path, cfg=None, log=print) -> dict:
    """Assemble the metrics record from RunState plus the run's artifacts.

    Every lookup is defensive: a metrics record must never be the reason a run
    fails, so a missing field becomes null rather than an exception.
    """
    tokens = dict(getattr(run, "total_tokens", None) or {})
    durations = dict(getattr(run, "phase_durations", None) or {})

    rec = {
        "schema_version": SCHEMA_VERSION,

        # --- identity ---
        "run_id": _env("GITHUB_RUN_ID") or "local",
        "feature_id": getattr(run, "feature_id", None),
        "repo": _env("GITHUB_REPOSITORY"),
        "actor": getattr(run, "actor", None) or _env("GITHUB_ACTOR"),
        "engine_ref": _env("HARNESS_ENGINE_REF"),
        "toolkit_ref": _env("HARNESS_TOOLKIT_REF"),

        # --- timing ---
        "started_at": getattr(run, "started_at", None),
        "finished_at": _iso_now(),
        "duration_sec": _duration_sec(getattr(run, "started_at", None)),

        # --- outcome ---
        "status": getattr(run, "status", None),
        "halt_gate": getattr(run, "halt_gate", None),
        "halt_detail": getattr(run, "halt_detail", None),
        "halt_phase": (getattr(run, "current_phase", None)
                       if getattr(run, "status", None) != "done" else None),
        "resumed": bool(_env("HARNESS_RESUMED") == "true"),

        # --- flow ---
        "phases_run": len(getattr(run, "completed_phases", None) or []),
        "phase_seq": _phase_sequence(run),
        "loopback_review": getattr(run, "review_attempts", 0),
        "loopback_coverage": getattr(run, "coverage_attempts", 0),
        "loopback_validation": getattr(run, "validation_attempts", 0),
        "loopback_scope": getattr(run, "scope_attempts", 0),
        "loopback_ac": getattr(run, "ac_attempts", 0),

        # --- cost ---
        "credits_actual": getattr(run, "credits_actual", None),
        "tokens_total": (tokens.get("input", 0) or 0) + (tokens.get("output", 0) or 0),
        "tokens_input": tokens.get("input"),
        "tokens_output": tokens.get("output"),
        "tokens_cache_read": tokens.get("cache_read"),
    }

    # Per-phase tokens and durations, flattened.
    for entry in (getattr(run, "phase_token_log", None) or []):
        pid = entry.get("phase")
        if pid:
            key = f"tokens_{pid}"
            rec[key] = rec.get(key, 0) + int(entry.get("phase_tokens") or 0)
    for pid, secs in durations.items():
        rec[f"dur_{pid}"] = secs

    _add_context_metrics(rec, repo_root, cfg, log)
    _add_validation_metrics(rec, repo_root, log)
    return rec


def _add_context_metrics(rec: dict, repo_root: Path, cfg, log) -> None:
    """Quality score, feasibility, design trigger, AC and clarification counts."""
    rec.update({"quality_score": None, "feasibility": None, "design_required": None,
                "ac_total": None, "ac_assumed": None, "clarifications": None,
                "blockers": None})
    try:
        from clarification import scan_context
        out_dir = getattr(cfg, "context_output_dir", ".github/story-context-files")
        cr = scan_context(repo_root, out_dir)
        rec["feasibility"] = cr.verdict
        rec["design_required"] = cr.design_required
        rec["clarifications"] = len(cr.items)
        rec["blockers"] = len(cr.blockers)

        d = repo_root / out_dir
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            text = files[0].read_text(encoding="utf-8", errors="replace")
            m = re.search(r"\*\*Total\*\*\s*\|\s*\*\*(\d+)\s*/\s*(\d+)\*\*", text)
            if m:
                # Normalised to a percentage so the number stays comparable if the
                # number of scoring dimensions ever changes.
                rec["quality_score"] = round(
                    100 * int(m.group(1)) / max(int(m.group(2)), 1))
            ids = set(re.findall(r"^[\s\-\*>\u2022]*\**\s*(AC-[0-9]+(?:\.[0-9]+)?)\s*:",
                                 text, re.MULTILINE))
            rec["ac_total"] = len(ids) or None
            rec["ac_assumed"] = len(re.findall(r"\[ASSUMED\]", text)) or 0
    except Exception as e:
        log(f"  [metrics] context metrics unavailable ({type(e).__name__})")


def _add_validation_metrics(rec: dict, repo_root: Path, log) -> None:
    """Per-AC conformance counts and coverage."""
    rec.update({"ac_met": None, "ac_not_met": None, "ac_unverifiable": None,
                "ac_verdict": None, "coverage_pct": None})
    try:
        from ac_validation import scan_validation
        vr = scan_validation(repo_root, repo_root / ".harness")
        if vr.verdict != "MISSING":
            rec["ac_verdict"] = vr.verdict
            rec["ac_met"] = len(vr.met)
            rec["ac_not_met"] = len(vr.not_met)
            rec["ac_unverifiable"] = len(vr.unverifiable) + len(vr.unvalidated)
    except Exception:
        pass
    try:
        report = repo_root / ".harness" / "validation-report.txt"
        if report.is_file():
            m = re.search(r"coverage[^0-9]{0,20}([0-9]+(?:\.[0-9]+)?)\s*%",
                          report.read_text(encoding="utf-8", errors="replace"),
                          re.IGNORECASE)
            if m:
                rec["coverage_pct"] = float(m.group(1))
    except Exception:
        pass


def write_record(rec: dict, repo_root: Path, log=print) -> Path | None:
    """Write the record to metrics/<yyyy>/<mm>/<feature>-<run_id>.json.

    Returns the path, or None on failure — a metrics write must NEVER fail a run.
    """
    try:
        now = datetime.now(timezone.utc)
        d = repo_root / "metrics" / now.strftime("%Y") / now.strftime("%m")
        d.mkdir(parents=True, exist_ok=True)
        name = f"{_safe(rec.get('feature_id'), 'nofeature')}-{_safe(rec.get('run_id'), 'norun')}.json"
        p = d / name
        p.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding="utf-8")
        log(f"  [metrics] wrote {p.relative_to(repo_root)}")
        return p
    except Exception as e:
        log(f"  [metrics] could not write metrics record ({type(e).__name__}: {e})")
        return None


def emit(run, repo_root: Path, cfg=None, log=print) -> Path | None:
    """Build and write the record. Swallows every error by design."""
    try:
        return write_record(build_record(run, repo_root, cfg, log), repo_root, log)
    except Exception as e:
        log(f"  [metrics] record not emitted ({type(e).__name__}: {e})")
        return None
