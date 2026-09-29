"""Score the failure verdict on labelled runs of failures.

    python3 -m airlock.failure_eval [--cases eval/failure-cases.jsonl]

Each case is {"id", "expected", "failures": [{tool, summary, error}]}, with
an optional "also_ok" list where a second verdict gives equally right advice. Real
Jev calls, one per case; needs TYPESAFE_API_KEY. What matters most is
`false_speak`: a spoken verdict (change_approach, gather_info, ask_user)
that was wrong, since that is advice steering a session the wrong way.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from . import client, failure_verdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CASES = os.path.join(HERE, "eval", "failure-cases.jsonl")


def run_case(case):
    try:
        result = failure_verdict.decide(case["failures"], client.ask, timeout_s=10.0)
    except Exception as exc:
        return dict(case, error=str(exc)[:200], verdict=None, speak=False)
    return dict(case, **result)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python3 -m airlock.failure_eval")
    parser.add_argument("--cases", default=DEFAULT_CASES)
    args = parser.parse_args(argv)
    with open(args.cases) as f:
        cases = [json.loads(line) for line in f if line.strip()]
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(run_case, cases))

    def ok(r):
        return r.get("verdict") == r["expected"] or r.get("verdict") in (r.get("also_ok") or [])

    correct = sum(1 for r in rows if ok(r))
    should_speak = [r for r in rows if r["expected"] in failure_verdict.SPOKEN]
    spoke_right = sum(1 for r in should_speak if r.get("speak") and ok(r))
    false_speak = [r for r in rows if r.get("speak") and not ok(r)]
    print("== failure_verdict ==  cases=%d errors=%d" % (len(rows), sum(1 for r in rows if r.get("error"))))
    print("  verdict accuracy %d/%d" % (correct, len(rows)))
    print("  spoken when it should be: %d/%d" % (spoke_right, len(should_speak)))
    print("  false_speak (wrong advice said): %d" % len(false_speak))
    for r in rows:
        mark = "ok " if ok(r) else "MISS"
        print("  %s %-14s expected %-16s got %-16s conf %.2f margin %.2f speak %s"
              % (mark, r["id"], r["expected"], r.get("verdict"), r.get("confidence") or 0,
                 r.get("margin") or 0, r.get("speak")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
