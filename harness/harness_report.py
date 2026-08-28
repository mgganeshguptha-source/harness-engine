#!/usr/bin/env python3
"""
harness_report.py — aggregate the per-run metrics records.

Usage:
    git fetch origin harness-metrics && git checkout harness-metrics
    python harness_report.py metrics/

    python harness_report.py metrics/ --jsonl > runs.jsonl   # for pandas/a DB

Reads metrics/<yyyy>/<mm>/*.json and answers the questions the raw records
cannot answer one at a time: which gate stops runs most often, what a story
costs, whether story quality is improving.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path


def load(root: Path) -> list:
    recs = []
    for p in sorted(root.rglob("*.json")):
        try:
            recs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"  ! skipped {p.name}: {e}", file=sys.stderr)
    return recs


def _nums(recs, key):
    return [r[key] for r in recs if isinstance(r.get(key), (int, float))]


def _med(vals):
    return round(statistics.median(vals), 1) if vals else None


def report(recs: list) -> None:
    if not recs:
        print("No metrics records found.")
        return

    total = len(recs)
    done = [r for r in recs if r.get("status") == "done"]
    halted = [r for r in recs if r.get("status") != "done"]

    print(f"\nHARNESS METRICS — {total} run(s)\n" + "=" * 52)
    print(f"  completed : {len(done):>4}  ({100*len(done)//total}%)")
    print(f"  halted    : {len(halted):>4}  ({100*len(halted)//total}%)")

    # --- halts by gate. The infra split is the point: those are OUR failures,
    # the rest are the harness correctly refusing to ship something.
    if halted:
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from halt_gates import is_infra
        except Exception:
            def is_infra(_):
                return False
        gates = Counter(r.get("halt_gate") or "unrecorded" for r in halted)
        infra = sum(c for g, c in gates.items() if is_infra(g))
        print(f"\n  WHY RUNS HALTED")
        for g, c in gates.most_common():
            tag = "  (infrastructure)" if is_infra(g) else ""
            print(f"    {g:<20} {c:>3}  {100*c//len(halted):>3}%{tag}")
        if infra:
            print(f"\n    {infra} of {len(halted)} halts were infrastructure, not quality.")

    # --- cost
    credits = _nums(recs, "credits_actual")
    if credits:
        print(f"\n  COST (actual credits)")
        print(f"    median {_med(credits)}   min {min(credits)}   max {max(credits)}"
              f"   total {round(sum(credits),2)}")
        worst = sorted((r for r in recs if isinstance(r.get("credits_actual"), (int, float))),
                       key=lambda r: -r["credits_actual"])[:3]
        for r in worst:
            print(f"      {r.get('feature_id'):<12} {r['credits_actual']:>7}"
                  f"   {r.get('status')}  loops="
                  f"{sum(r.get(k, 0) or 0 for k in r if k.startswith('loopback_'))}")

    # --- duration
    durs = _nums(recs, "duration_sec")
    if durs:
        print(f"\n  DURATION (seconds)")
        print(f"    median {_med(durs)}   max {max(durs)}")
        phase_keys = sorted({k for r in recs for k in r if k.startswith("dur_")})
        for k in phase_keys:
            v = _nums(recs, k)
            if v:
                print(f"    {k[4:]:<16} median {_med(v):>7}")

    # --- story quality: the adoption signal. Rising here means developers are
    # writing better stories, which no other metric captures.
    qs = _nums(recs, "quality_score")
    if qs:
        print(f"\n  STORY QUALITY (0-100)")
        print(f"    median {_med(qs)}   min {min(qs)}   max {max(qs)}")
        if len(qs) >= 6:
            half = len(qs) // 2
            print(f"    first half {_med(qs[:half])} -> second half {_med(qs[half:])}")

    # --- gate effectiveness
    ac_nm = _nums(recs, "ac_not_met")
    if ac_nm:
        caught = sum(1 for v in ac_nm if v > 0)
        print(f"\n  AC CONFORMANCE")
        print(f"    runs with an unmet criterion: {caught}/{len(ac_nm)}")

    loops = Counter()
    for r in recs:
        for k, v in r.items():
            if k.startswith("loopback_") and isinstance(v, int) and v:
                loops[k[9:]] += v
    if loops:
        print(f"\n  LOOPBACKS (total across runs)")
        for g, c in loops.most_common():
            print(f"    {g:<16} {c:>4}")

    resumed = sum(1 for r in recs if r.get("resumed"))
    if resumed:
        print(f"\n  {resumed} run(s) were resumed rather than restarted.")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default="metrics")
    ap.add_argument("--jsonl", action="store_true",
                    help="emit one record per line instead of a report")
    a = ap.parse_args()
    root = Path(a.path)
    if not root.exists():
        print(f"No such path: {root}", file=sys.stderr)
        return 1
    recs = load(root)
    if a.jsonl:
        for r in recs:
            print(json.dumps(r, sort_keys=True))
    else:
        report(recs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
