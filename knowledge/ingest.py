"""Ingest: the one door a candidate goes through to become a node.

    from knowledge import ingest
    ingest.ingest(Candidate(...))  # {"status": ..., "id": ..., "reason": ...}

status is "stored", "dropped", "duplicate", "refused" or "off".

Rules, in order:

  1. The kill switch is off: status "off", nothing runs.
  2. An explicit constraint is stored verbatim with no gate call. A
     constraint from any other path is dropped: only the human's own words
     write one.
  3. A decision with no rejected alternatives is refused before the gate.
  4. Otherwise: nearest neighbours, one Jev call (gate.judge), then act on the
     novelty answer. duplicate touches the old node. refines writes a node
     and an evidence-for edge. contradicts writes a node, a contradicts edge
     and supersedes the old one. new writes a node. A confident promote also
     writes a pattern node linked evidence-for.

Hook-born text is templated from the captured fields; prose from a
self-report or the human is used as given. Every stored string is redacted.
A `fix` with `fixes_signature` gets its `fixes` edge inside store.put_node.

Every outcome, drops included, is a row in metrics.db (kind "knowledge",
action "ingest_<status>"). Fails open: nothing here raises.
"""
import dataclasses

from airlock import redact

from . import config, gate

HEAD_CHARS = 240
BUCKET_LABEL = {"failure": "failure", "fix": "fix", "danger": "danger", "env": "env"}


def template(c):
    """Greppable text for a hook-born candidate, built from its fields."""
    head = " ".join(str(c.text or "").split())[:HEAD_CHARS]
    label = BUCKET_LABEL.get(c.bucket_hint or "", c.bucket_hint or "episode")
    who = " ".join(x for x in (c.tool, c.exception_type) if x)
    out = "%s%s: %s" % (label, " (%s)" % who if who else "", head)
    ents = [str(e) for e in (c.entities or ()) if e][:3]
    if ents:
        out += " | on: " + ", ".join(ents)
    if c.signature:
        out += " | sig %s" % c.signature
    return out


def _scopes(c):
    seen = []
    for s in (c.scope, "machine", "global"):
        if s and s not in seen:
            seen.append(s)
    return seen


def _neighbours(store, c):
    found, seen = [], set()
    scopes = _scopes(c)
    try:
        if c.signature:
            for n in store.find_by_signature(c.signature, scopes):
                if n.id not in seen:
                    seen.add(n.id)
                    found.append(n)
    except Exception:
        pass
    try:
        for n in store.nearest(c.text, c.exception_type, tuple(c.entities or ()), scopes, k=3):
            if n.id not in seen:
                seen.add(n.id)
                found.append(n)
    except Exception:
        pass
    return found[:3]


def _result(c, status, node_id=None, reason="", **fields):
    gate.log_event("ingest_" + status, source=c.source, session_id=c.session_id,
                   bucket=fields.pop("bucket", None) or c.bucket_hint, reason=reason or None,
                   tool=c.tool or None, node_id=node_id, **fields)
    return {"status": status, "id": node_id, "reason": reason}


def _promote(store, c, node_id, text):
    pat = dataclasses.replace(c, bucket_hint="pattern", text="pattern: " + text,
                              signature=None, fixes_signature=None,
                              rejected_alternatives=())
    pid = store.put_node(pat, "pattern")
    if pid is not None and pid > 0:
        store.add_edge(node_id, pid, "evidence-for")
        return pid
    return None


def ingest(c, store=None, judge=None):
    """Route one candidate. `store` and `judge` are injectable for tests."""
    try:
        return _ingest(c, store, judge)
    except Exception as exc:
        try:
            return _result(c, "dropped", None, "error:%s" % type(exc).__name__)
        except Exception:
            return {"status": "dropped", "id": None, "reason": "error"}


def _ingest(c, store, judge):
    if not config.enabled():
        return {"status": "off", "id": None, "reason": "kill switch is off"}
    if store is None:
        from . import store
    judge = judge or gate.judge

    if c.source == "hook":
        text = template(c)
    else:
        text = " ".join(str(c.text or "").split()) if c.bucket_hint != "constraint" \
            else str(c.text or "").strip()
    text = redact.redact(text)
    if not text:
        return _result(c, "dropped", None, "empty")
    c = dataclasses.replace(c, text=text)

    if c.bucket_hint == "constraint":
        if c.source != "explicit":
            return _result(c, "dropped", None, "constraint_needs_explicit")
        nid = store.put_node(c, "constraint")
        if nid is None or nid < 0:
            return _result(c, "refused", None, "store_refused")
        return _result(c, "stored", nid, "explicit_constraint", bucket="constraint")

    if c.bucket_hint == "decision" and not any(r and r.strip() for r in c.rejected_alternatives):
        return _result(c, "refused", None, "decision_without_rejected_alternatives")

    neighbours = _neighbours(store, c)
    verdict = judge(c, neighbours)
    if verdict is None:
        return _result(c, "dropped", None, "gate_dropped")
    if not verdict.store:
        return _result(c, "dropped", None, "gate_said_no", bucket=verdict.bucket or None)
    if verdict.bucket == "constraint":
        return _result(c, "dropped", None, "constraint_needs_explicit")
    if verdict.bucket == "pattern":
        return _result(c, "dropped", None, "pattern_only_by_promotion")
    if verdict.bucket == "decision" and not any(
            r and r.strip() for r in c.rejected_alternatives):
        return _result(c, "refused", None, "decision_without_rejected_alternatives")
    if verdict.durability <= 0.0:
        return _result(c, "dropped", None, "session_only", bucket=verdict.bucket)

    target = verdict.target
    if verdict.novelty == "duplicate" and target is not None:
        store.touch(target)
        return _result(c, "duplicate", target, "touched", bucket=verdict.bucket)

    scope = verdict.scope if verdict.scope else c.scope
    c = dataclasses.replace(c, scope=scope)
    nid = store.put_node(c, verdict.bucket)
    if nid is None or nid < 0:
        return _result(c, "refused", None, "store_refused", bucket=verdict.bucket)
    if target is not None and verdict.novelty == "refines":
        store.add_edge(nid, target, "evidence-for")
    elif target is not None and verdict.novelty == "contradicts":
        store.add_edge(nid, target, "contradicts")
        store.supersede(target, nid)
    promoted = _promote(store, c, nid, text) if verdict.promote else None
    return _result(c, "stored", nid, verdict.novelty, bucket=verdict.bucket,
                   promoted=promoted)
