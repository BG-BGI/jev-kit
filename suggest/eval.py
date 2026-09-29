"""Score the skill suggester on labelled prompts against a fixed roster.

    python3 -m suggest.eval                      # eval/skill-roster.json + eval/skill-cases.jsonl
    python3 -m suggest.eval --roster R --cases C --json out.json

Each case is {"id", "prompt", "expected"}: a skill name, or null when no
skill fits. The two numbers the cookbook moved, and the ones to watch:

  wrong_skill      a skill was expected and a different one was suggested
  load_when_none   nothing fits, and a skill was suggested anyway

Real Jev calls, two per case at most; needs TYPESAFE_API_KEY. The roster
and cases shipped here are synthetic: no real paths, names or tokens.
"""
import argparse
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

from . import suggest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROSTER = os.path.join(HERE, "eval", "skill-roster.json")
DEFAULT_CASES = os.path.join(HERE, "eval", "skill-cases.jsonl")
WORKERS = 4


def load(roster_path, cases_path):
    with open(roster_path) as f:
        skills = json.load(f)
    with open(cases_path) as f:
        cases = [json.loads(line) for line in f if line.strip()]
    return skills, cases


def outcome(expected, got):
    if expected is None:
        return "correct_none" if got is None else "load_when_none"
    if got == expected:
        return "correct"
    return "missed" if got is None else "wrong_skill"


def run_case(case, skills, ask):
    try:
        result = suggest.suggest(case["prompt"], skills, ask, timeout_s=10.0)
    except Exception as exc:
        return dict(case, got=None, outcome="error", error=str(exc)[:200])
    got = result.get("skill")
    return dict(case, got=got, outcome=outcome(case.get("expected"), got),
                reason=result.get("reason"), gate=result.get("gate"),
                shortlist=result.get("shortlist"), fits=result.get("fits"),
                latency_ms=result.get("latency_ms"), tokens=result.get("tokens"))


def summarise(rows):
    expect_skill = [r for r in rows if r.get("expected")]
    expect_none = [r for r in rows if not r.get("expected")]

    def share(subset, name):
        return sum(1 for r in subset if r["outcome"] == name) / len(subset) if subset else 0.0

    latencies = [r["latency_ms"] for r in rows if r.get("latency_ms") is not None]
    return {
        "cases": len(rows),
        "errors": sum(1 for r in rows if r["outcome"] == "error"),
        "correct_skill": share(expect_skill, "correct"),
        "wrong_skill": share(expect_skill, "wrong_skill"),
        "missed": share(expect_skill, "missed"),
        "correct_none": share(expect_none, "correct_none"),
        "load_when_none": share(expect_none, "load_when_none"),
        "median_latency_ms": statistics.median(latencies) if latencies else None,
        "mean_tokens": (statistics.mean(r["tokens"] for r in rows if r.get("tokens"))
                        if any(r.get("tokens") for r in rows) else None),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m suggest.eval")
    parser.add_argument("--roster", default=DEFAULT_ROSTER)
    parser.add_argument("--cases", default=DEFAULT_CASES)
    parser.add_argument("--json", help="also write every row and the summary here")
    args = parser.parse_args(argv)

    from airlock import client
    skills, cases = load(args.roster, args.cases)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = list(pool.map(lambda c: run_case(c, skills, client.ask), cases))
    summary = summarise(rows)

    print("== skill_suggest ==  roster=%d cases=%d errors=%d"
          % (len(skills), summary["cases"], summary["errors"]))
    print("  expected a skill: correct %.1f%%  wrong_skill %.1f%%  missed %.1f%%"
          % (100 * summary["correct_skill"], 100 * summary["wrong_skill"], 100 * summary["missed"]))
    print("  expected none:    correct %.1f%%  load_when_none %.1f%%"
          % (100 * summary["correct_none"], 100 * summary["load_when_none"]))
    print("  median latency %s ms, mean %s Jev tokens per turn"
          % (summary["median_latency_ms"], summary["mean_tokens"] and int(summary["mean_tokens"])))
    for r in rows:
        if r["outcome"] not in ("correct", "correct_none"):
            print("  %-10s %-15s expected %-18s got %-18s (%s, gate %s, fits %s)"
                  % (r["id"], r["outcome"], r.get("expected"), r.get("got"), r.get("reason"),
                     r.get("gate"), {k: round(v, 2) for k, v in (r.get("fits") or {}).items()}))
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"summary": summary, "rows": rows}, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
