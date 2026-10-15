import tests  # noqa: F401, I001 -- MUST be the first import; see tests/__init__.py.

import io
import os
import shutil
import stat
import tempfile
import time
import unittest
from unittest import mock

from knowledge import config, store
from knowledge.types import Candidate

DAY = 86400.0
REPO = "repo:/work/a"
OTHER = "repo:/work/b"
ALL = [REPO, "machine", "global"]


def cand(text="boom", **kw):
    return Candidate(bucket_hint=None, text=text, **kw)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._wipe()
        self.addCleanup(self._wipe)

    @staticmethod
    def _wipe():
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                os.remove(str(store.db_path()) + suffix)
            except OSError:
                pass


class TestPut(StoreCase):
    def test_roundtrip_and_entities(self):
        nid = store.put_node(cand("KeyError in parse", signature="s1", exception_type="KeyError",
                                  entities=("a.py", "parse", "a.py"), scope=REPO), "failure")
        self.assertGreater(nid, 0)
        (n,) = store.find_by_signature("s1", ALL)
        self.assertEqual((n.id, n.bucket, n.scope, n.evidence), (nid, "failure", REPO, 1))
        self.assertEqual(n.entities, ("a.py", "parse"))
        self.assertEqual(n.exception_type, "KeyError")

    def test_unknown_bucket_and_empty_text_refused(self):
        self.assertEqual(store.put_node(cand("x"), "success"), -1)
        self.assertEqual(store.put_node(cand("  "), "env"), -1)

    def test_decision_without_rejected_refused(self):
        self.assertEqual(store.put_node(cand("use sqlite"), "decision"), -1)
        self.assertEqual(store.put_node(cand("use sqlite", rejected_alternatives=("", " ")),
                                        "decision"), -1)
        self.assertEqual(store.count_by_bucket(), {})

    def test_decision_with_rejected_stored(self):
        nid = store.put_node(cand("use sqlite", rejected_alternatives=("postgres", "json file")),
                             "decision")
        n = store.get_node(nid)
        self.assertIn("use sqlite", n.text)
        self.assertIn("postgres; json file", n.text)

    def test_db_is_mode_600(self):
        store.put_node(cand("x"), "env")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(store.db_path()).st_mode), 0o600)

    def test_fails_open(self):
        with mock.patch.object(store, "_connect", side_effect=RuntimeError("no db")):
            self.assertEqual(store.put_node(cand("x"), "env"), -1)
            self.assertEqual(store.find_by_signature("s", ALL), [])
            self.assertEqual(store.constraints(), [])
            self.assertFalse(store.drop(1))
            self.assertEqual(store.count_by_bucket(), {})
            self.assertEqual(store.prune(), 0)
            store.touch(1)
            store.supersede(1, 2)
            store.add_edge(1, 2, "fixes")


class TestTtl(StoreCase):
    def test_expired_removed_on_next_write(self):
        t0 = time.time() - 20 * DAY
        old = store.put_node(cand("old env fact"), "env", now=t0)
        self.assertIsNotNone(store.get_node(old))
        store.put_node(cand("fresh"), "env")
        self.assertIsNone(store.get_node(old))

    def test_touch_extends_life(self):
        t0 = time.time() - 20 * DAY
        old = store.put_node(cand("old env fact"), "env", now=t0)
        store.touch(old)
        store.put_node(cand("fresh"), "env")
        n = store.get_node(old)
        self.assertIsNotNone(n)
        self.assertEqual(n.evidence, 2)

    def test_constraints_never_expire(self):
        t0 = time.time() - 400 * DAY
        cid = store.put_node(cand("never push to upstream", source="explicit"), "constraint", now=t0)
        store.put_node(cand("fresh"), "env")
        self.assertEqual(store.prune(), 0)
        self.assertEqual([n.id for n in store.constraints()], [cid])

    def test_expiry_removes_edges(self):
        t0 = time.time() - 20 * DAY
        a = store.put_node(cand("f", signature="s"), "failure", now=t0)
        b = store.put_node(cand("fx", fixes_signature="s"), "fix", now=t0)
        self.assertEqual(len(store.fixes_for("s", ALL)), 1)
        self.assertEqual(store.prune(), 2)
        self.assertEqual(store.fixes_for("s", ALL), [])
        self.assertIsNone(store.get_node(a))
        self.assertIsNone(store.get_node(b))


class TestCap(StoreCase):
    def test_cap_evicts_least_recently_touched(self):
        t0 = time.time() - 3600
        ids = [store.put_node(cand("x" * 400_000 + str(i)), "env", now=t0 + i, max_mb=1)
               for i in range(3)]
        self.assertIsNone(store.get_node(ids[0]))
        self.assertIsNotNone(store.get_node(ids[2]))

    def test_cap_never_evicts_constraints(self):
        t0 = time.time() - 3600
        cid = store.put_node(cand("c" * 600_000), "constraint", now=t0, max_mb=1)
        for i in range(3):
            store.put_node(cand("x" * 400_000 + str(i)), "env", now=t0 + 1 + i, max_mb=1)
        self.assertIsNotNone(store.get_node(cid))


class TestEdges(StoreCase):
    def test_supersede_keeps_both_rows(self):
        old = store.put_node(cand("use A", signature="d", rejected_alternatives=("B",)), "decision")
        new = store.put_node(cand("use B", signature="d", rejected_alternatives=("A",)), "decision")
        store.supersede(old, new)
        self.assertEqual(store.get_node(old).superseded_by, new)
        self.assertIsNotNone(store.get_node(new))
        self.assertEqual([n.id for n in store.find_by_signature("d", ALL)], [new])
        self.assertEqual([n.id for n in store.nearest("use", "", (), ALL)], [new])
        self.assertEqual({n.id for n in store.list_nodes()}, {old, new})

    def test_superseded_constraint_not_injected(self):
        old = store.put_node(cand("always X"), "constraint")
        new = store.put_node(cand("never X"), "constraint")
        store.supersede(old, new)
        self.assertEqual([n.id for n in store.constraints()], [new])

    def test_bad_edge_kind_and_missing_ids_ignored(self):
        a = store.put_node(cand("a"), "env")
        store.add_edge(a, a + 99, "fixes")
        store.add_edge(a, a, "bogus")
        store.supersede(a, a + 99)
        self.assertIsNone(store.get_node(a).superseded_by)

    def test_fixes_for_via_put_and_via_add_edge(self):
        f = store.put_node(cand("KeyError x", signature="s1", scope=REPO), "failure")
        auto = store.put_node(cand("add default", fixes_signature="s1", scope=REPO), "fix")
        manual = store.put_node(cand("use .get", scope=REPO), "fix")
        store.add_edge(manual, f, "fixes")
        store.add_edge(manual, f, "fixes")  # idempotent
        got = store.fixes_for("s1", ALL)
        self.assertEqual({n.id for n in got}, {auto, manual})
        self.assertEqual(store.fixes_for("other", ALL), [])
        self.assertEqual(store.fixes_for("s1", [OTHER]), [])

    def test_fixes_for_skips_superseded_fix(self):
        store.put_node(cand("f", signature="s"), "failure")
        a = store.put_node(cand("old fix", fixes_signature="s"), "fix")
        b = store.put_node(cand("new fix", fixes_signature="s"), "fix")
        store.supersede(a, b)
        self.assertEqual([n.id for n in store.fixes_for("s", ALL)], [b])


class TestScopes(StoreCase):
    def test_find_and_top_filter_scope(self):
        a = store.put_node(cand("in repo", signature="s", scope=REPO), "failure")
        store.put_node(cand("elsewhere", signature="s", scope=OTHER), "failure")
        g = store.put_node(cand("global fact", signature="s", scope="global"), "env")
        self.assertEqual({n.id for n in store.find_by_signature("s", ALL)}, {a, g})
        self.assertEqual({n.id for n in store.find_by_signature("s", [OTHER])} & {a, g}, set())
        self.assertEqual({n.id for n in store.top_for_scopes(ALL, 10)}, {a, g})
        self.assertEqual(store.find_by_signature("s", []), [])

    def test_nearest_filters_scope(self):
        store.put_node(cand("parse error in config loader", scope=OTHER), "failure")
        mine = store.put_node(cand("parse error in config loader", scope=REPO), "failure")
        self.assertEqual([n.id for n in store.nearest("config loader parse", "", (), ALL)], [mine])

    def test_scopes_for(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, ".git"))
        sub = os.path.join(d, "a", "b")
        os.makedirs(sub)
        self.assertEqual(store.scopes_for(sub), ["repo:" + os.path.abspath(d), "machine", "global"])
        plain = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, plain, True)
        self.assertEqual(store.scopes_for(plain)[1:], ["machine", "global"])


class TestRanking(StoreCase):
    def test_nearest_ranks_text_exception_and_entities(self):
        a = store.put_node(cand("ModuleNotFoundError no module named foo", exception_type="ModuleNotFoundError",
                                entities=("foo",)), "failure")
        store.put_node(cand("disk full writing cache"), "env")
        got = store.nearest("no module named foo", "ModuleNotFoundError", ("foo",), ALL, k=3)
        self.assertEqual([n.id for n in got], [a])

    def test_nearest_k_and_empty(self):
        for i in range(5):
            store.put_node(cand("shared words here %d" % i), "env")
        self.assertEqual(len(store.nearest("shared words here", "", (), ALL, k=2)), 2)
        self.assertEqual(store.nearest("zzzzzz", "", (), ALL), [])

    def test_top_for_scopes_orders_by_evidence_excludes_constraints(self):
        a = store.put_node(cand("a"), "env")
        b = store.put_node(cand("b"), "env")
        store.put_node(cand("rule"), "constraint")
        store.touch(a)
        store.touch(a)
        got = store.top_for_scopes(ALL, 5)
        self.assertEqual([n.id for n in got], [a, b])
        self.assertEqual([n.id for n in store.top_for_scopes(ALL, 1)], [a])


class TestDropPurge(StoreCase):
    def test_drop(self):
        a = store.put_node(cand("a", signature="s", entities=("e",)), "failure")
        b = store.put_node(cand("b", fixes_signature="s"), "fix")
        self.assertTrue(store.drop(a))
        self.assertFalse(store.drop(a))
        self.assertEqual(store.fixes_for("s", ALL), [])
        self.assertIsNotNone(store.get_node(b))

    def test_drop_constraint(self):
        c = store.put_node(cand("rule"), "constraint")
        self.assertTrue(store.drop(c))
        self.assertEqual(store.constraints(), [])

    def test_drop_clears_superseded_pointer(self):
        a = store.put_node(cand("a"), "env")
        b = store.put_node(cand("b"), "env")
        store.supersede(a, b)
        store.drop(b)
        self.assertIsNone(store.get_node(a).superseded_by)

    def test_count_and_purge(self):
        store.put_node(cand("a"), "env")
        store.put_node(cand("b"), "env")
        store.put_node(cand("c"), "constraint")
        self.assertEqual(store.count_by_bucket(), {"env": 2, "constraint": 1})
        self.assertEqual(store.purge(), 3)
        self.assertEqual(store.count_by_bucket(), {})


class TestCli(StoreCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            self.assertEqual(store.main(argv), 0)
        return out.getvalue()

    def test_list_show_drop_constraints(self):
        a = store.put_node(cand("flaky port 8080", signature="s1"), "env")
        c = store.put_node(cand("never push to upstream"), "constraint")
        out = self.run_cli(["list"])
        self.assertIn("flaky port 8080", out)
        self.assertIn("never push", out)
        self.assertNotIn("never push", self.run_cli(["list", "--bucket", "env"]))
        self.assertIn("never push", self.run_cli(["constraints"]))
        self.assertIn("signature: s1", self.run_cli(["show", str(a)]))
        self.assertIn("not found", self.run_cli(["show", "999"]))
        self.assertIn("dropped %d" % c, self.run_cli(["drop", str(c)]))
        self.assertIn("not found", self.run_cli(["drop", str(c)]))

    def test_prune_and_purge(self):
        store.put_node(cand("old"), "env", now=time.time() - 30 * DAY)
        self.assertIn("pruned 1 nodes", self.run_cli(["prune"]))
        store.put_node(cand("x"), "env")
        self.assertIn("purged 1 nodes", self.run_cli(["purge"]))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self._rm)
        self._rm()

    @staticmethod
    def _rm():
        try:
            os.remove(config.switch_path())
        except OSError:
            pass

    def test_default_on(self):
        self.assertTrue(config.enabled())

    def test_off_and_on(self):
        config.set_enabled(False)
        self.assertFalse(config.enabled())
        with open(config.switch_path(), "w") as f:
            f.write("  OFF \n")
        self.assertFalse(config.enabled())
        config.set_enabled(True)
        self.assertTrue(config.enabled())

    def test_other_text_is_on(self):
        config.switch_path().parent.mkdir(parents=True, exist_ok=True)
        config.switch_path().write_text("garbage")
        self.assertTrue(config.enabled())


if __name__ == "__main__":
    unittest.main()
