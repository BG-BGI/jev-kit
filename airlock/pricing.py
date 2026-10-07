"""Model prices from the Vercel AI Gateway, cached for the sub-agent cost cap.

`GET https://ai-gateway.vercel.sh/v1/models` is public (no key) and lists a
per-token `pricing.input` / `pricing.output` for every model. This module keeps
the Anthropic rows in one small file under the state dir and answers one
question: what does a Claude Code model alias (haiku, sonnet, opus, fable)
cost, so tiers.json's `cost_cap` can refuse a sub-agent dearer than the cap.

Price of an alias = input + output USD per token of the newest non-"fast"
gateway model in that family. The sum is a deliberate simplification: it is
monotonic in both rates, needs no guess at an input/output mix, and ranks the
four families the same way any realistic mix does.

A hook lives for milliseconds, so the hook NEVER touches the network. It reads
the cache and, when that is older than REFRESH_AFTER_S, spawns a detached
`python -m airlock.pricing refresh` and carries on with the stale numbers
(stale beats none). Attempts are rate-limited by a marker file so a failing
network is not retried on every tool call. No cache at all means no price,
which means the cap fails open.

    python3 -m airlock.pricing refresh    # pull now
    python3 -m airlock.pricing show       # print the cached alias prices
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

from . import paths

URL = "https://ai-gateway.vercel.sh/v1/models"
CACHE_NAME = "gateway-pricing.json"
ATTEMPT_NAME = "gateway-pricing.attempt"
REFRESH_AFTER_S = 24 * 3600
RETRY_AFTER_S = 3600
FETCH_TIMEOUT_S = 15

# anthropic/claude-sonnet-4.5, anthropic/claude-opus-5.5, claude-fable-5.1 ...
# Version-first ids (claude-3-haiku) and "-fast" variants do not match.
_ID = re.compile(r"^anthropic/claude-(haiku|sonnet|opus|fable)-(\d+(?:\.\d+)*)$")


def _cache_path():
    return str(paths.state_file(CACHE_NAME))


def _attempt_path():
    return str(paths.state_file(ATTEMPT_NAME))


def _rate(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract(models):
    """alias -> {"id", "input", "output", "cache_read", "cache_write"} from
    the gateway's `data` list: the newest non-fast model of each family.
    Rows without both base rates are skipped, never guessed; the two cache
    rates are optional (None when the gateway omits them)."""
    best = {}
    for m in models if isinstance(models, list) else []:
        if not isinstance(m, dict):
            continue
        hit = _ID.match(str(m.get("id") or ""))
        pricing = m.get("pricing")
        if not hit or not isinstance(pricing, dict):
            continue
        rin, rout = _rate(pricing.get("input")), _rate(pricing.get("output"))
        if rin is None or rout is None:
            continue
        alias = hit.group(1)
        version = tuple(int(p) for p in hit.group(2).split("."))
        if alias not in best or version > best[alias][0]:
            best[alias] = (version, {"id": m["id"], "input": rin, "output": rout,
                                     "cache_read": _rate(pricing.get("input_cache_read")),
                                     "cache_write": _rate(pricing.get("input_cache_write"))})
    return {alias: row for alias, (_, row) in best.items()}


def load(path=None):
    """The cache as {"fetched_at": float, "aliases": {...}}, or None when it
    is missing or malformed. Never raises."""
    try:
        with open(path or _cache_path(), "r") as f:
            data = json.load(f)
        if isinstance(data.get("aliases"), dict) and isinstance(data.get("fetched_at"), (int, float)):
            return data
    except Exception:
        pass
    return None


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def refresh(path=None, url=URL, now=None):
    """Pull the gateway list and rewrite the cache. Returns the aliases, or
    None when the pull failed or held no Claude prices. A failed pull leaves
    the previous cache untouched."""
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            body = json.load(resp)
        aliases = extract(body.get("data"))
        if not aliases:
            return None
        _write_atomic(path or _cache_path(), json.dumps(
            {"fetched_at": time.time() if now is None else now, "source": url, "aliases": aliases},
            indent=2, sort_keys=True))
        return aliases
    except Exception:
        return None


def is_stale(cache, now=None):
    if cache is None:
        return True
    return (time.time() if now is None else now) - cache["fetched_at"] >= REFRESH_AFTER_S


def _recently_attempted(now):
    try:
        with open(_attempt_path(), "r") as f:
            return now - float(f.read()) < RETRY_AFTER_S
    except (OSError, ValueError):
        return False


def maybe_refresh_async(cache=None, now=None):
    """Start a detached refresh when the cache is stale and none was started
    in the last hour. Returns True when one was spawned. Never raises, never
    waits."""
    now = time.time() if now is None else now
    try:
        if not is_stale(cache, now) or _recently_attempted(now):
            return False
        _write_atomic(_attempt_path(), str(now))
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                  "stderr": subprocess.DEVNULL, "cwd": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "airlock.pricing", "refresh"], **kwargs)
        return True
    except Exception:
        return False


def alias_price(alias, cache=None):
    """USD per token (input + output) for a model alias, or None when the
    cache has no row for it."""
    cache = cache if cache is not None else load()
    row = (cache or {}).get("aliases", {}).get(alias)
    if not isinstance(row, dict):
        return None
    rin, rout = _rate(row.get("input")), _rate(row.get("output"))
    return None if rin is None or rout is None else rin + rout


def rates(alias, cache=None):
    """The full per-token rate card for a model alias: {"id", "input",
    "output", "cache_read", "cache_write"}, USD per token, cache rates None
    when the gateway did not list them. None when the alias is unknown, so a
    caller costing a transcript can fall back rather than invent numbers."""
    cache = cache if cache is not None else load()
    row = (cache or {}).get("aliases", {}).get(alias)
    if not isinstance(row, dict):
        return None
    rin, rout = _rate(row.get("input")), _rate(row.get("output"))
    if rin is None or rout is None:
        return None
    return {"id": row.get("id"), "input": rin, "output": rout,
            "cache_read": _rate(row.get("cache_read")),
            "cache_write": _rate(row.get("cache_write"))}


def _main(argv):
    cmd = argv[0] if argv else "show"
    if cmd == "refresh":
        aliases = refresh()
        print("refreshed %d families" % len(aliases) if aliases else "refresh failed; cache unchanged")
        return 0 if aliases else 1
    if cmd == "show":
        cache = load()
        if not cache:
            print("no cache; run: python3 -m airlock.pricing refresh")
            return 1
        age_h = (time.time() - cache["fetched_at"]) / 3600
        print("cache age %.1fh" % age_h)
        for alias, row in sorted(cache["aliases"].items(), key=lambda kv: alias_price(kv[0], cache) or 0):
            cr, cw = _rate(row.get("cache_read")), _rate(row.get("cache_write"))
            print("%-7s %-34s $%.2f/M in  $%.2f/M out  %s cache-read  %s cache-write" % (
                alias, row["id"], row["input"] * 1e6, row["output"] * 1e6,
                "-" if cr is None else "$%.2f/M" % (cr * 1e6),
                "-" if cw is None else "$%.2f/M" % (cw * 1e6)))
        return 0
    print("usage: python3 -m airlock.pricing [refresh|show]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
