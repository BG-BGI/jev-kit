"""Local SQLite graph for the experience graph (docs/experience-graph.md).

Nothing leaves the machine. The file is `knowledge.db` in the state directory,
mode 600 inside a 700 directory. Tables: nodes, entities, node_entities, edges.
One database for every repo; a `scope` column ("repo:<abs path>", "machine",
"global") is filtered at read time.

Like the recall store, every write enforces the TTL and the size cap, so no
timer owns the graph. `constraint` nodes never expire and are never evicted by
the cap. Nothing is deleted silently: `supersede` keeps both rows, and only
`drop`, `prune` and `purge` remove anything.

Edge direction: `fixes` runs fix -> failure, `supersedes` runs new -> old,
`evidence-for` runs evidence -> pattern, `contradicts` runs new -> old.

Everything fails open. Writes return -1 or None on error, reads return an
empty list. No exception reaches a hook.

    python3 -m knowledge.store list [--bucket B] | show ID | drop ID
    python3 -m knowledge.store prune | purge | constraints
"""
import argparse
import os
import re
import sqlite3
import sys
import time

from airlock import paths, platform_compat
from knowledge import config
from knowledge.types import Node

DB_NAME = "knowledge.db"
BUSY_TIMEOUT_MS = 200
DAY = 86400.0
ROW_OVERHEAD = 200  # bytes counted per node beyond its text, for the cap

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bucket TEXT NOT NULL,
  text TEXT NOT NULL,
  scope TEXT NOT NULL,
  signature TEXT,
  exception_type TEXT NOT NULL DEFAULT '',
  evidence INTEGER NOT NULL DEFAULT 1,
  created REAL NOT NULL,
  touched REAL NOT NULL,
  superseded_by INTEGER,
  provenance TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_sig ON nodes(signature);
CREATE INDEX IF NOT EXISTS nodes_touched ON nodes(touched);
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS node_entities (
  node_id INTEGER NOT NULL,
  entity_id INTEGER NOT NULL,
  PRIMARY KEY (node_id, entity_id)
);
CREATE TABLE IF NOT EXISTS edges (
  src INTEGER NOT NULL,
  dst INTEGER NOT NULL,
  kind TEXT NOT NULL,
  created REAL NOT NULL,
  PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst, kind);
"""

COLS = ("id, bucket, text, scope, signature, exception_type, evidence, "
        "created, touched, superseded_by, provenance")

_TOKEN = re.compile(r"[A-Za-z0-9_]{3,}")


def db_path():
    return paths.state_dir() / DB_NAME


def _connect(timeout_ms=BUSY_TIMEOUT_MS):
    d = db_path().parent
    d.mkdir(parents=True, exist_ok=True)
    platform_compat.restrict_path(d, 0o700)
    conn = sqlite3.connect(str(db_path()), timeout=timeout_ms / 1000.0)
    conn.executescript(SCHEMA)
    platform_compat.restrict_path(str(db_path()), 0o600)
    return conn


def _now():
    return time.time()


def _node(conn, row):
    ents = conn.execute(
        "SELECT e.name FROM entities e JOIN node_entities ne ON ne.entity_id = e.id "
        "WHERE ne.node_id = ? ORDER BY e.name", (row[0],)).fetchall()
    return Node(id=row[0], bucket=row[1], text=row[2], scope=row[3], signature=row[4],
                exception_type=row[5], entities=tuple(e[0] for e in ents), evidence=row[6],
                created=row[7], touched=row[8], superseded_by=row[9], provenance=row[10])


def _marks(items):
    return ",".join("?" * len(items))


def _prune(conn, ttl_days=config.TTL_DAYS, max_mb=config.MAX_MB, now=None):
    """Drop expired non-constraint nodes, then least recently touched until under the cap."""
    now = _now() if now is None else now
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM nodes WHERE bucket != 'constraint' AND touched < ?",
        (now - ttl_days * DAY,))]
    cap = int(max_mb * 1024 * 1024)
    total = conn.execute(
        "SELECT COALESCE(SUM(size), 0) FROM nodes").fetchone()[0]
    gone = set(ids)
    total -= sum(r[0] for r in conn.execute(
        "SELECT size FROM nodes WHERE id IN (%s)" % _marks(ids), ids)) if ids else 0
    if total > cap:
        for nid, size in conn.execute(
                "SELECT id, size FROM nodes WHERE bucket != 'constraint' "
                "ORDER BY touched ASC, id ASC").fetchall():
            if total <= cap:
                break
            if nid in gone:
                continue
            gone.add(nid)
            total -= size
    _delete_nodes(conn, sorted(gone))
    return len(gone)


def _delete_nodes(conn, ids):
    for i in ids:
        conn.execute("DELETE FROM nodes WHERE id = ?", (i,))
        conn.execute("DELETE FROM node_entities WHERE node_id = ?", (i,))
        conn.execute("DELETE FROM edges WHERE src = ? OR dst = ?", (i, i))
        conn.execute("UPDATE nodes SET superseded_by = NULL WHERE superseded_by = ?", (i,))


def _add_edge(conn, src, dst, kind, now):
    if kind not in config.EDGE_KINDS:
        return False
    conn.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?)", (src, dst, kind, now))
    return True


def put_node(c, bucket, now=None, ttl_days=config.TTL_DAYS, max_mb=config.MAX_MB):
    """Write a node, return its id, or -1 if refused or on error. Never raises.

    Refuses an unknown bucket, empty text, and a `decision` with no rejected
    alternatives. Rejected alternatives are appended to the text. TTL and cap
    are enforced on this write. A `fix` with `fixes_signature` is linked to
    the matching `failure` nodes.
    """
    try:
        text = (c.text or "").strip()
        if bucket not in config.BUCKETS or not text:
            return -1
        rejected = tuple(r.strip() for r in (c.rejected_alternatives or ()) if r and r.strip())
        if bucket == "decision":
            if not rejected:
                return -1
            text += "\nRejected: " + "; ".join(rejected)
        stamp = _now() if now is None else now
        entities = sorted({e for e in (c.entities or ()) if e})
        conn = _connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO nodes (bucket, text, scope, signature, exception_type, evidence, "
                    "created, touched, provenance, session_id, source, size) "
                    "VALUES (?,?,?,?,?,1,?,?,?,?,?,?)",
                    (bucket, text, c.scope or "machine", c.signature, c.exception_type or "",
                     stamp, stamp, c.provenance or "", c.session_id or "", c.source or "",
                     len(text.encode("utf-8", "replace")) + ROW_OVERHEAD))
                nid = cur.lastrowid
                for name in entities:
                    conn.execute("INSERT OR IGNORE INTO entities (name) VALUES (?)", (name,))
                    (eid,) = conn.execute(
                        "SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
                    conn.execute("INSERT OR IGNORE INTO node_entities VALUES (?,?)", (nid, eid))
                if bucket == "fix" and c.fixes_signature:
                    for (fid,) in conn.execute(
                            "SELECT id FROM nodes WHERE bucket = 'failure' AND signature = ? "
                            "AND id != ?", (c.fixes_signature, nid)).fetchall():
                        _add_edge(conn, nid, fid, "fixes", stamp)
                _prune(conn, ttl_days, max_mb, stamp)
                alive = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (nid,)).fetchone()
        finally:
            conn.close()
        return nid if alive else -1
    except Exception:
        return -1


def touch(node_id, now=None):
    """evidence += 1, touched = now. Never raises."""
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute("UPDATE nodes SET evidence = evidence + 1, touched = ? WHERE id = ?",
                             (_now() if now is None else now, int(node_id)))
        finally:
            conn.close()
    except Exception:
        pass


def add_edge(src, dst, kind, now=None):
    """Add an edge between two existing nodes. Unknown kinds and ids are ignored."""
    try:
        conn = _connect()
        try:
            with conn:
                n = conn.execute("SELECT COUNT(*) FROM nodes WHERE id IN (?,?)",
                                 (int(src), int(dst))).fetchone()[0]
                if n == len({int(src), int(dst)}):
                    _add_edge(conn, int(src), int(dst), kind, _now() if now is None else now)
        finally:
            conn.close()
    except Exception:
        pass


def supersede(old, new, now=None):
    """Mark `old` superseded by `new` and add the edge. Both rows stay."""
    try:
        old, new = int(old), int(new)
        if old == new:
            return
        conn = _connect()
        try:
            with conn:
                n = conn.execute("SELECT COUNT(*) FROM nodes WHERE id IN (?,?)",
                                 (old, new)).fetchone()[0]
                if n == 2:
                    conn.execute("UPDATE nodes SET superseded_by = ? WHERE id = ?", (new, old))
                    _add_edge(conn, new, old, "supersedes", _now() if now is None else now)
        finally:
            conn.close()
    except Exception:
        pass


def _query(sql, args):
    try:
        conn = _connect()
        try:
            return [_node(conn, r) for r in conn.execute(sql, args).fetchall()]
        finally:
            conn.close()
    except Exception:
        return []


def find_by_signature(sig, scopes):
    """Exact signature match, in scope, not superseded. Newest touch first."""
    if not sig or not scopes:
        return []
    return _query("SELECT %s FROM nodes WHERE signature = ? AND superseded_by IS NULL "
                  "AND scope IN (%s) ORDER BY touched DESC, id DESC" % (COLS, _marks(scopes)),
                  [sig] + list(scopes))


def fixes_for(sig, scopes):
    """Fix nodes linked by a `fixes` edge to a failure node with this signature."""
    if not sig or not scopes:
        return []
    cols = ", ".join("f." + c.strip() for c in COLS.split(","))
    return _query(
        "SELECT DISTINCT %s FROM nodes f JOIN edges e ON e.src = f.id AND e.kind = 'fixes' "
        "JOIN nodes x ON x.id = e.dst WHERE x.signature = ? AND f.bucket = 'fix' "
        "AND f.superseded_by IS NULL AND f.scope IN (%s) AND x.scope IN (%s) "
        "ORDER BY f.evidence DESC, f.touched DESC" % (cols, _marks(scopes), _marks(scopes)),
        [sig] + list(scopes) + list(scopes))


def _tokens(s):
    return {t.lower() for t in _TOKEN.findall(s or "")}


def nearest(text, exception_type, entities, scopes, k=3):
    """The k closest in-scope, non-superseded nodes by cheap lexical overlap.

    Score is token Jaccard on text, plus 0.25 for the same exception type and
    0.15 per shared entity (max 0.45). Zero-score nodes are not returned.
    """
    if not scopes or k <= 0:
        return []
    want, ents = _tokens(text), {e for e in (entities or ()) if e}
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT %s FROM nodes WHERE superseded_by IS NULL AND scope IN (%s)"
                % (COLS, _marks(scopes)), list(scopes)).fetchall()
            scored = []
            for r in rows:
                have = _tokens(r[2])
                union = want | have
                s = len(want & have) / len(union) if union else 0.0
                if exception_type and r[5] == exception_type:
                    s += 0.25
                if ents:
                    shared = conn.execute(
                        "SELECT COUNT(*) FROM entities e JOIN node_entities ne ON ne.entity_id = e.id "
                        "WHERE ne.node_id = ? AND e.name IN (%s)" % _marks(ents),
                        [r[0]] + sorted(ents)).fetchone()[0]
                    s += min(0.45, 0.15 * shared)
                if s > 0:
                    scored.append((s, r[8], r[0], r))
            scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
            return [_node(conn, t[3]) for t in scored[:k]]
        finally:
            conn.close()
    except Exception:
        return []


def top_for_scopes(scopes, k):
    """Best in-scope non-superseded nodes, constraints excluded (see `constraints`).

    Ranked by evidence, then recency.
    """
    if not scopes or k <= 0:
        return []
    return _query("SELECT %s FROM nodes WHERE superseded_by IS NULL AND bucket != 'constraint' "
                  "AND scope IN (%s) ORDER BY evidence DESC, touched DESC, id DESC LIMIT ?"
                  % (COLS, _marks(scopes)), list(scopes) + [int(k)])


def constraints():
    """Every live constraint, any scope, oldest first. Superseded ones are left out."""
    return _query("SELECT %s FROM nodes WHERE bucket = 'constraint' AND superseded_by IS NULL "
                  "ORDER BY created ASC, id ASC" % COLS, [])


def get_node(node_id):
    """One node by id, or None."""
    rows = _query("SELECT %s FROM nodes WHERE id = ?" % COLS, [int(node_id)])
    return rows[0] if rows else None


def list_nodes(bucket=None, limit=200):
    """Newest first. Includes superseded rows, for the CLI and for audits."""
    if bucket:
        return _query("SELECT %s FROM nodes WHERE bucket = ? ORDER BY touched DESC, id DESC LIMIT ?"
                      % COLS, [bucket, int(limit)])
    return _query("SELECT %s FROM nodes ORDER BY touched DESC, id DESC LIMIT ?" % COLS,
                  [int(limit)])


def drop(node_id):
    """Delete one node and its edges. True if it existed."""
    try:
        conn = _connect()
        try:
            with conn:
                if not conn.execute("SELECT 1 FROM nodes WHERE id = ?",
                                    (int(node_id),)).fetchone():
                    return False
                _delete_nodes(conn, [int(node_id)])
            return True
        finally:
            conn.close()
    except Exception:
        return False


def count_by_bucket():
    try:
        conn = _connect()
        try:
            return dict(conn.execute("SELECT bucket, COUNT(*) FROM nodes GROUP BY bucket"))
        finally:
            conn.close()
    except Exception:
        return {}


def prune(ttl_days=config.TTL_DAYS, max_mb=config.MAX_MB, now=None):
    """Run TTL and cap now. Returns the number of nodes removed."""
    try:
        conn = _connect(timeout_ms=2000)
        try:
            with conn:
                return _prune(conn, ttl_days, max_mb, now)
        finally:
            conn.close()
    except Exception:
        return 0


def purge():
    """Delete everything, constraints included. Returns the number of nodes removed."""
    try:
        conn = _connect(timeout_ms=2000)
        try:
            with conn:
                n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
                for t in ("nodes", "entities", "node_entities", "edges"):
                    conn.execute("DELETE FROM %s" % t)
            return n
        finally:
            conn.close()
    except Exception:
        return 0


def _git_root(cwd):
    d = os.path.abspath(cwd or os.getcwd())
    cur = d
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return d
        cur = parent


def scopes_for(cwd):
    """["repo:<git root or cwd>", "machine", "global"]. Never raises."""
    try:
        root = _git_root(cwd)
    except Exception:
        root = cwd or ""
    return ["repo:" + root, config.SCOPE_MACHINE, config.SCOPE_GLOBAL]


def _line(n):
    first = n.text.splitlines()[0] if n.text else ""
    return "%d  %-10s x%d  %-14s %s%s" % (
        n.id, n.bucket, n.evidence, n.scope[:14], first[:config.PREVIEW_CHARS],
        "  [superseded by %d]" % n.superseded_by if n.superseded_by else "")


def main(argv=None):
    p = argparse.ArgumentParser(prog="knowledge.store")
    sub = p.add_subparsers(dest="cmd", required=True)
    sl = sub.add_parser("list")
    sl.add_argument("--bucket")
    sub.add_parser("show").add_argument("id", type=int)
    sub.add_parser("drop").add_argument("id", type=int)
    sub.add_parser("prune")
    sub.add_parser("purge")
    sub.add_parser("constraints")
    a = p.parse_args(argv)
    if a.cmd == "list":
        for n in list_nodes(a.bucket):
            print(_line(n))
    elif a.cmd == "constraints":
        for n in constraints():
            print(_line(n))
    elif a.cmd == "show":
        n = get_node(a.id)
        if not n:
            print("not found")
        else:
            print(_line(n))
            print(n.text)
            print("signature: %s  exception: %s  entities: %s  provenance: %s" % (
                n.signature or "-", n.exception_type or "-", ", ".join(n.entities) or "-",
                n.provenance or "-"))
    elif a.cmd == "drop":
        print("dropped %d" % a.id if drop(a.id) else "not found")
    elif a.cmd == "prune":
        print("pruned %d nodes" % prune())
    elif a.cmd == "purge":
        print("purged %d nodes" % purge())
    return 0


if __name__ == "__main__":
    sys.exit(main())
