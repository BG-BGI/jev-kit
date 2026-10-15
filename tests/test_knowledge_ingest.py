"""knowledge/ingest.py against the real store in a temp state dir, fake judge."""
import os
import tempfile
import unittest
from unittest import mock

from knowledge import config, gate, ingest, store
from knowledge.gate import Verdict
from knowledge.types import Candidate


def verdict(**kw):
    base = dict(store=True, bucket="failure", novelty="new", durability=1.0,
                scope="machine", promote=False, confident=True, target=None)
    base.update(kw)
    return Verdict(**base)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"AIRLOCK_STATE_DIR": self.tmp.name + "/state",
                                           "AIRLOCK_CONFIG_DIR": self.tmp.name + "/cfg"})
        env.start()
        self.addCleanup(env.stop)
        p = mock.patch.object(gate, "log_event")
        self.log = p.start()
        self.addCleanup(p.stop)

    def run_ingest(self, c, v):
        calls = []

        def judge(cand, neighbours):
            calls.append((cand, neighbours))
            return v
        r = ingest.ingest(c, judge=judge)
        return r, calls

    def cand(self, **kw):
        base = dict(bucket_hint="failure", text="KeyError: 'x' in parse", tool="Bash",
                    signature="sig1", exception_type="KeyError", entities=("a.py",))
        base.update(kw)
        return Candidate(**base)

    def actions(self):
        return [c.args[0] for c in self.log.call_args_list]

    def test_off(self):
        config.set_enabled(False)
        r, calls = self.run_ingest(self.cand(), verdict())
        self.assertEqual(r["status"], "off")
        self.assertEqual(calls, [])

    def test_explicit_constraint_no_gate(self):
        c = Candidate("constraint", "never push to upstream", source="explicit")
        r, calls = self.run_ingest(c, None)
        self.assertEqual(r["status"], "stored")
        self.assertEqual(calls, [])
        self.assertEqual(store.constraints()[0].text, "never push to upstream")

    def test_non_explicit_constraint_dropped(self):
        c = Candidate("constraint", "never do x", source="self_report")
        r, calls = self.run_ingest(c, verdict(bucket="constraint"))
        self.assertEqual((r["status"], r["reason"]), ("dropped", "constraint_needs_explicit"))
        self.assertEqual(store.constraints(), [])

    def test_gate_constraint_answer_dropped(self):
        r, _ = self.run_ingest(self.cand(), verdict(bucket="constraint"))
        self.assertEqual(r["status"], "dropped")

    def test_new_stored_with_template(self):
        r, _ = self.run_ingest(self.cand(), verdict())
        self.assertEqual(r["status"], "stored")
        n = store.get_node(r["id"])
        self.assertIn("failure (Bash KeyError): KeyError: 'x' in parse", n.text)
        self.assertIn("sig sig1", n.text)

    def test_prose_verbatim_when_not_hook(self):
        c = Candidate("doc", "The FDK docs say run fdk validate first", source="self_report")
        r, _ = self.run_ingest(c, verdict(bucket="doc"))
        self.assertEqual(store.get_node(r["id"]).text, "The FDK docs say run fdk validate first")

    def test_secrets_redacted_before_store_and_judge(self):
        secret = "ghp_" + "a" * 30
        r, calls = self.run_ingest(self.cand(text="boom %s" % secret), verdict())
        self.assertNotIn(secret, store.get_node(r["id"]).text)
        self.assertNotIn(secret, calls[0][0].text)

    def test_gate_none_dropped(self):
        r, _ = self.run_ingest(self.cand(), None)
        self.assertEqual((r["status"], r["reason"]), ("dropped", "gate_dropped"))
        self.assertEqual(store.count_by_bucket().get("failure", 0), 0)
        self.assertIn("ingest_dropped", self.actions())

    def test_store_false_dropped(self):
        r, _ = self.run_ingest(self.cand(), verdict(store=False))
        self.assertEqual(r["status"], "dropped")

    def test_duplicate_touches(self):
        first, _ = self.run_ingest(self.cand(), verdict())
        r, calls = self.run_ingest(self.cand(), verdict(novelty="duplicate", target=first["id"]))
        self.assertEqual((r["status"], r["id"]), ("duplicate", first["id"]))
        self.assertEqual(store.get_node(first["id"]).evidence, 2)
        self.assertEqual(sum(store.count_by_bucket().values()), 1)
        self.assertEqual([n.id for n in calls[0][1]], [first["id"]])

    def test_refines_adds_evidence_edge(self):
        first, _ = self.run_ingest(self.cand(), verdict())
        with mock.patch.object(store, "add_edge", wraps=store.add_edge) as ae:
            r, _ = self.run_ingest(self.cand(text="KeyError more detail", signature="sig2"),
                                   verdict(novelty="refines", target=first["id"]))
        self.assertEqual(r["status"], "stored")
        ae.assert_called_with(r["id"], first["id"], "evidence-for")

    def test_contradicts_supersedes_in_same_write(self):
        first, _ = self.run_ingest(self.cand(), verdict())
        r, _ = self.run_ingest(self.cand(text="KeyError opposite", signature="sig3"),
                               verdict(novelty="contradicts", target=first["id"]))
        self.assertEqual(store.get_node(first["id"]).superseded_by, r["id"])

    def test_fix_gets_fixes_edge(self):
        failure, _ = self.run_ingest(self.cand(), verdict())
        c = self.cand(bucket_hint="fix", text="pip install foo", signature="sig9",
                      fixes_signature="sig1")
        r, _ = self.run_ingest(c, verdict(bucket="fix"))
        self.assertEqual([n.id for n in store.fixes_for("sig1", ["machine"])], [r["id"]])

    def test_promote_creates_pattern_linked(self):
        with mock.patch.object(store, "add_edge", wraps=store.add_edge) as ae:
            r, _ = self.run_ingest(self.cand(), verdict(promote=True))
        self.assertEqual(store.count_by_bucket().get("pattern"), 1)
        src, dst, kind = ae.call_args.args
        self.assertEqual((src, kind), (r["id"], "evidence-for"))

    def test_decision_without_alternatives_refused_before_gate(self):
        c = Candidate("decision", "use sqlite", source="self_report")
        r, calls = self.run_ingest(c, verdict(bucket="decision"))
        self.assertEqual(r["status"], "refused")
        self.assertEqual(calls, [])

    def test_decision_with_alternatives_stored(self):
        c = Candidate("decision", "use sqlite", source="self_report",
                      rejected_alternatives=("postgres",))
        r, _ = self.run_ingest(c, verdict(bucket="decision"))
        self.assertEqual(r["status"], "stored")

    def test_gate_pattern_answer_dropped(self):
        r, _ = self.run_ingest(self.cand(), verdict(bucket="pattern"))
        self.assertEqual(r["status"], "dropped")

    def test_session_only_dropped(self):
        r, _ = self.run_ingest(self.cand(), verdict(durability=0.0))
        self.assertEqual((r["status"], r["reason"]), ("dropped", "session_only"))

    def test_fails_open_on_judge_exception(self):
        def boom(c, n):
            raise RuntimeError("x")
        r = ingest.ingest(self.cand(), judge=boom)
        self.assertEqual(r["status"], "dropped")

    def test_every_outcome_logged(self):
        self.run_ingest(self.cand(), verdict())
        self.run_ingest(self.cand(), None)
        self.assertEqual(self.actions(), ["ingest_stored", "ingest_dropped"])


if __name__ == "__main__":
    unittest.main()
