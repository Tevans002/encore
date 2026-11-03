"""Evaluation runner for Encore (Part 7).

Reads eval/requests.json, runs each request through encore.py in a subprocess
(isolated session state), parses the resulting JSONL trace, and writes
eval/results.csv with the columns the spec requires.

Usage:
    python eval/run_eval.py                 # all requests, 1 run each
    python eval/run_eval.py --repeat 3      # 3 runs each (variance)
    python eval/run_eval.py --ids H1,B1     # subset
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Paths are relative to the encore/ repo root (this file lives in eval/).
ROOT = Path(__file__).resolve().parents[1]
REQUESTS_PATH = ROOT / "eval" / "requests.json"
RESULTS_PATH = ROOT / "eval" / "results.csv"
TRACE_DIR = ROOT / "traces"

# Model pricing for est_cost_usd (USD per 1M tokens). Adjust to the model you
# actually use. For cohere/north-mini-code:free these are 0 (free tier); the
# column stays present but zero. Replace with real rates if you switch models.
COST_IN_PER_MTOK = 0.0
COST_OUT_PER_MTOK = 0.0

COLUMNS = [
    "request_id", "run", "category", "status", "correct", "tool_calls",
    "llm_calls", "retries", "compressions", "memory_reads", "peak_tokens_in",
    "total_tokens_in", "total_tokens_out", "est_cost_usd", "latency_s",
]

# candidate for deletion VVVV
def newest_trace_since(before: set[Path], timeout_s: float = 300.0) -> Path | None:
    """Return the newest .jsonl trace that appeared after `before`, or None."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        after = set(TRACE_DIR.glob("*.jsonl"))
        new = after - before
        if new:
            return max(new, key=lambda p: p.stat().st_mtime)
        time.sleep(0.2)
    return None

def run_one(req: dict) -> Path | None:
    """Run one request through encore.py in a subprocess; return its trace path."""
    before = set(TRACE_DIR.glob("*.jsonl"))
    cmd = [sys.executable, str(ROOT / "encore.py"), "online"]
    if req.get("inject"):
        inject_path = ROOT / req["inject"]
        if not inject_path.exists():
            print(f"  [warn] inject file missing: {inject_path}", file=sys.stderr)
        else:
            cmd += ["--inject", str(inject_path)]

    try:
        subprocess.run(
            cmd,
            input=req["prompt"] + "\n",
            text=True,
            capture_output=False,     # avoids pipe-holding by Chroma threads
            timeout=300,              # slow free-tier model
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return None

    # Prefer a genuinely new file; fall back to the newest trace by mtime
    # (handles the timestamp-collision case where the child overwrote a trace).
    after = set(TRACE_DIR.glob("*.jsonl"))
    new = after - before
    if new:
        return max(new, key=lambda p: p.stat().st_mtime)

    # Fallback: newest by mtime, but only if it wasn't already present before
    # the run (otherwise the child likely crashed without writing a trace).
    candidates = sorted(
        TRACE_DIR.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates and candidates[0] not in before:
        return candidates[0]
    return None


def metrics_from_trace(path: Path) -> dict:
    """Aggregate a trace into the results.csv metric columns."""
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not events:
        return {"status": "no_events"}

    tool_calls = [e for e in events if e.get("event") == "tool_call"]
    llm_calls = [e for e in events if e.get("event") == "llm_call"]
    final = next((e for e in events if e.get("event") == "final"), {})

    # Status is embedded in the final event's message, e.g.
    # "status=answered_low_confidence (prose; ...)" or "budget_exceeded".
    status = "none"
    msg = str(final.get("message", ""))
    if "status=" in msg:
        status = msg.split("status=", 1)[1].split()[0]
    elif msg in ("budget_exceeded", "policy_halt", "stuck"):
        status = msg

    peak_in = max((e.get("tokens_in", 0) for e in llm_calls), default=0)
    total_in = sum(e.get("tokens_in", 0) for e in llm_calls)
    total_out = sum(e.get("tokens_out", 0) for e in llm_calls)

    t0 = events[0].get("ts")
    t1 = events[-1].get("ts")
    latency_s = ""
    try:
        from datetime import datetime
        d0 = datetime.fromisoformat(str(t0).replace("Z", "+00:00"))
        d1 = datetime.fromisoformat(str(t1).replace("Z", "+00:00"))
        latency_s = round((d1 - d0).total_seconds(), 2)
    except Exception:
        pass

    est_cost = round(
        (total_in / 1_000_000) * COST_IN_PER_MTOK
        + (total_out / 1_000_000) * COST_OUT_PER_MTOK,
        6,
    )

    return {
        "status": status,
        "tool_calls": len(tool_calls),
        "llm_calls": len(llm_calls),
        "retries": sum(1 for e in tool_calls if e.get("error_category") == "transient"),
        "compressions": sum(1 for e in events if e.get("event") == "compress"),
        "memory_reads": sum(1 for e in events if e.get("event") == "memory_read"),
        "peak_tokens_in": peak_in,
        "total_tokens_in": total_in,
        "total_tokens_out": total_out,
        "est_cost_usd": est_cost,
        "latency_s": latency_s,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1, help="runs per request")
    ap.add_argument("--ids", default=None, help="comma-separated request ids to run")
    ap.add_argument("--out", default=str(RESULTS_PATH))
    args = ap.parse_args()

    requests = json.loads(REQUESTS_PATH.read_text(encoding="utf-8"))
    if args.ids:
        wanted = set(args.ids.split(","))
        requests = [r for r in requests if r["id"] in wanted]
    if not requests:
        print("No requests to run.", file=sys.stderr)
        return

    TRACE_DIR.mkdir(exist_ok=True)
    rows = []

    for req in requests:
        for run_idx in range(1, args.repeat + 1):
            print(f"[{req['id']}] run {run_idx}/{args.repeat} ({req['category']}) ...",
                  flush=True)
            trace = run_one(req)
            if trace is None:
                rows.append({
                    "request_id": req["id"], "run": run_idx,
                    "category": req["category"], "status": "no_trace",
                    "correct": "", "tool_calls": "", "llm_calls": "", "retries": "",
                    "compressions": "", "memory_reads": "", "peak_tokens_in": "",
                    "total_tokens_in": "", "total_tokens_out": "",
                    "est_cost_usd": "", "latency_s": "",
                })
                continue
            m = metrics_from_trace(trace)
            rows.append({
                "request_id": req["id"],
                "run": run_idx,
                "category": req["category"],
                "status": m.get("status", ""),
                # 'correct' records your predefined judgment; update after
                # reviewing the answer if reality differs (honest analysis).
                "correct": req.get("expected_correct", ""),
                **{k: m.get(k, "") for k in COLUMNS if k not in
                   ("request_id", "run", "category", "status", "correct")},
            })

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote {len(rows)} rows to {out_path}")


if __name__ == "__main__":
    main()