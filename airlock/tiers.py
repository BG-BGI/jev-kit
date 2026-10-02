"""The sub-agent ladder: one place that knows which agent types are which rung.

Agent type names differ per machine -- one box calls the Sonnet worker
`worker`, another `workerS`, a third has a house agent nobody else has. Every
part of the tier guard (policy, the enforce-mode block, the warn line, the
rewrite) reads the ladder from here, and here alone.

Config: ~/.config/airlock/tiers.json, an ordered list of lists of equivalent
names, cheapest rung first:

    [["scout-find"],
     ["scout"],
     ["workerS", "worker"],
     ["workerO"],
     ["claude", "general-purpose", "Plan", "Explore"],
     ["fable"]]

The first name in each list is the canonical rung name AND the name the guard
suggests (or rewrites to) for that rung, so put the type you actually want
dispatched first. Anything malformed -- not a list of non-empty lists of
strings, fewer than two rungs, a duplicated name -- is ignored whole and the
built-in ladder below is used instead; a broken config can never make the
guard misjudge a rung.

The built-in ladder keeps the historical rung name "director" for the
Opus-level rung, because the log, the tuning lock files and the tests all use
it. "director" is not a dispatchable subagent_type, so it is listed in
NON_DISPATCHABLE and the suggested name for that rung is the next entry,
"claude". A machine-supplied tiers.json has no such reserved name: its first
entry is both the rung name and the suggestion.

A second ladder, MODEL_LADDER, judges a dispatch by the model it runs on
whenever that model can be known in code (see effective_model): the Agent
call names one in `model`, the type has a fixed one (Explore runs on Haiku),
or the machine says what its general types inherit. The advice then names a
cheaper `model`, which works on any install, instead of a cheaper agent type,
which only exists on machines that defined one. tiers.json may be an object
to set those options, with the ladder under "ladder":

    {"ladder": [...],
     "inherit_model": "opus",
     "type_models": {"my-reader": "haiku"},
     "cost_cap": true}

"cost_cap" refuses a sub-agent dispatch whose model costs more than a
reference model (same cost passes). `true` caps at "inherit_model"; a model
alias ("sonnet") caps there instead. Prices come from airlock/pricing.py. It
needs no Jev call, and fails open whenever the reference, the dispatch's
model or the prices are unknown.
"""
import json

from . import paths

CONFIG_NAME = "tiers.json"

# Cheapest rung first. First entry of each list = canonical rung name.
DEFAULT_LADDER = [
    ["scout-find"],
    ["scout"],
    ["workerS", "worker"],
    ["workerO"],
    ["director", "claude", "general-purpose", "Plan", "Explore"],
    ["fable"],
]

# Rung names that are not real subagent_type values and must never be
# suggested or rewritten to.
NON_DISPATCHABLE = frozenset(["director"])

# subagent_type prefixes treated as the Opus-level rung (the plugin agents
# this box ships). Unchanged by config: they name a rung, not a type.
DIRECTOR_PREFIXES = ("feature-dev:", "code-simplifier:")

# Rung a type falls into when nothing matches: the second-most expensive rung
# (the Opus-level one), since an unrecognised subagent_type is at least as
# expensive as the director dispatching it directly.
_UNKNOWN_RUNG_FROM_TOP = 2

_CACHE = {}
_OPTIONS_CACHE = {}

# Claude Code's model aliases, cheapest first. A dispatch whose model can be
# known in code is judged on THIS ladder instead of by agent type: the model,
# not the type name, is what the call costs, and a stock install has no
# custom agent types to climb down to -- only a cheaper `model` to set.
MODEL_LADDER = ["haiku", "sonnet", "opus", "fable"]

# Built-in agent types that run on a fixed model rather than inheriting the
# session's. Merged under tiers.json's "type_models", which may add or
# override entries.
DEFAULT_TYPE_MODELS = {"Explore": "haiku"}

# Built-in agent types that inherit the session's model. They take
# tiers.json's "inherit_model" when a machine sets one; "" is a dispatch that
# named no type, which Claude Code runs as general-purpose.
GENERAL_TYPES = frozenset(["", "claude", "general-purpose", "Plan"])


def _valid(ladder):
    if not isinstance(ladder, list) or len(ladder) < 2:
        return False
    seen = set()
    for rung in ladder:
        if not isinstance(rung, list) or not rung:
            return False
        for name in rung:
            if not isinstance(name, str) or not name.strip():
                return False
            if name in seen:
                return False
            seen.add(name)
    return True


def load_ladder(path=None):
    """Read the ladder from config, falling back to DEFAULT_LADDER. Never
    raises, never returns something malformed."""
    p = path or str(paths.config_file(CONFIG_NAME))
    try:
        with open(p, "r") as f:
            data = json.load(f)
    except Exception:
        return [list(r) for r in DEFAULT_LADDER]
    if isinstance(data, dict):
        data = data.get("ladder")
    if not isinstance(data, list) or not _valid(data):
        return [list(r) for r in DEFAULT_LADDER]
    return [list(r) for r in data]


def ladder(path=None):
    """Cached load_ladder. A hook process lives for milliseconds, so the cache
    only ever saves repeated reads inside one process (the eval loop);
    reset_cache() clears it for tests."""
    key = path or ""
    if key not in _CACHE:
        _CACHE[key] = load_ladder(path)
    return _CACHE[key]


def reset_cache():
    _CACHE.clear()
    _OPTIONS_CACHE.clear()


def rung_names(path=None):
    return [rung[0] for rung in ladder(path)]


def rung_index(path=None):
    return {name: i for i, name in enumerate(rung_names(path))}


def rung_for_agent_type(subagent_type, path=None):
    """Canonical rung name for one subagent_type. Anything unrecognised lands
    on the Opus-level rung."""
    t = subagent_type or ""
    rungs = ladder(path)
    for rung in rungs:
        if t in rung:
            return rung[0]
    for prefix in DIRECTOR_PREFIXES:
        if t.startswith(prefix):
            return rungs[max(0, len(rungs) - _UNKNOWN_RUNG_FROM_TOP)][0]
    return rungs[max(0, len(rungs) - _UNKNOWN_RUNG_FROM_TOP)][0]


def dispatch_name_for_rung(rung_name, path=None):
    """The subagent_type to actually dispatch for a rung, or None if that rung
    has no dispatchable name (never suggest or rewrite to one of those)."""
    for rung in ladder(path):
        if rung[0] != rung_name:
            continue
        for name in rung:
            if name not in NON_DISPATCHABLE:
                return name
        return None
    return None


def is_known_agent_type(subagent_type, path=None):
    """True only if the name appears verbatim in the ladder. Used to refuse a
    rewrite to a type this machine does not actually have."""
    t = subagent_type or ""
    return any(t in rung for rung in ladder(path))


def model_name(value):
    """Canonical alias for a model string, or None when it names no rung of
    MODEL_LADDER. 'claude-haiku-4-5-20251001', 'haiku' and 'Haiku' all give
    'haiku'; 'inherit', '' and anything unrecognised give None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    for name in MODEL_LADDER:
        if name in v:
            return name
    return None


def model_index():
    return {name: i for i, name in enumerate(MODEL_LADDER)}


def _options(path=None):
    """The whole tiers.json object when it is one, else {}. The ladder-only
    list form carries no options. Never raises."""
    key = path or ""
    if key not in _OPTIONS_CACHE:
        p = path or str(paths.config_file(CONFIG_NAME))
        try:
            with open(p, "r") as f:
                data = json.load(f)
        except Exception:
            data = None
        _OPTIONS_CACHE[key] = data if isinstance(data, dict) else {}
    return _OPTIONS_CACHE[key]


def type_models(path=None):
    """DEFAULT_TYPE_MODELS with tiers.json's "type_models" laid over it. An
    entry naming no known model is ignored, never trusted."""
    merged = dict(DEFAULT_TYPE_MODELS)
    raw = _options(path).get("type_models")
    if isinstance(raw, dict):
        for agent_type, value in raw.items():
            name = model_name(value)
            if isinstance(agent_type, str) and name:
                merged[agent_type] = name
    return merged


def inherit_model(path=None):
    """tiers.json's "inherit_model": the model this machine's sessions run
    on, which the general built-in types inherit. None when unset, so a
    machine that never opted in keeps judging those types by agent type."""
    return model_name(_options(path).get("inherit_model"))


def effective_model(subagent_type, model_override, path=None):
    """The model a dispatch will run on, when code can know it, else None.
    An explicit `model` on the call wins; then a type with a fixed model;
    then a general type on a machine that configured inherit_model."""
    named = model_name(model_override)
    if named:
        return named
    t = subagent_type or ""
    fixed = type_models(path).get(t)
    if fixed:
        return fixed
    if t in GENERAL_TYPES:
        return inherit_model(path)
    return None


def cost_cap_reference(path=None):
    """The model alias sub-agents may not out-cost, from tiers.json's
    "cost_cap": a model alias, or true for "inherit_model". None when the cap
    is off, malformed, or true with no inherit_model to point at."""
    raw = _options(path).get("cost_cap")
    if raw is True:
        return inherit_model(path)
    if isinstance(raw, str):
        return model_name(raw)
    return None
