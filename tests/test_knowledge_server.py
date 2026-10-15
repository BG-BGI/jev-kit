"""knowledge/server.py: the graph_log, graph_search and graph_constraint_drop MCP tools.
Fully mocked: ingest and store are fakes, no key, no network, no database."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import importlib.util
import io
import json
import unittest
from pathlib import Path

from knowledge.types import Node

SERVER_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "server.py"


def _load():
    spec = importlib.util.spec_from_file_location("knowledge_server", str(SERVER_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load()


class FakeIngest:
    def __init__(self, result=None):
        self.seen = []
        self.result = result or {"status": "stored", "id": 7, "reason": ""}

    def ingest(self, cand):
        self.seen.append(cand)
        return self.result


def _node(i, bucket, text="t"):
    return Node(id=i, bucket=bucket, text=text, scope="machine", signature=None,
                exception_type="", entities=(), evidence=1, created=0.0, touched=0.0,
                superseded_by=None, provenance="")


class FakeStore:
    def __init__(self):
        self.dropped = []
        self.nodes = [_node(1, "fix", "use uv sync"), _node(2, "decision", "chose sqlite")]
        self.nearest_args = None

    def scopes_for(self, cwd):
        return ["repo:" + cwd, "machine", "global"]

    def nearest(self, text, exc, entities, scopes, k=3):
        self.nearest_args = (text, exc, entities, scopes, k)
        return self.nodes

    def top_for_scopes(self, scopes, k):
        return self.nodes

    def constraints(self):
        return [_node(3, "constraint", "never push upstream")]

    def drop(self, node_id):
        self.dropped.append(node_id)
        return node_id == 3


def rpc(method, params=None, deps=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg, deps)


def tool(name, args, deps):
    r = rpc("tools/call", {"name": name, "arguments": args}, deps)["result"]
    return r["isError"], r["content"][0]["text"]


def deps(ingest=None, store=None):
    return {"ingest": ingest or FakeIngest(), "store": store or FakeStore(), "cwd": "/w/repo"}


class TestProtocol(unittest.TestCase):
    def test_initialize_carries_instructions(self):
        r = rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(r["serverInfo"]["name"], "jev-knowledge")
        self.assertEqual(r["instructions"], server.INSTRUCTIONS)

    def test_instructions_pin_the_trigger_rule(self):
        text = server.INSTRUCTIONS
        for clause in ("NOT the decide server", "abandon an approach", "choose between named",
                       "rejected_alternatives", "`doc`", "NEVER log secrets",
                       "ONLY when the human explicitly", "ToolSearch select:"):
            self.assertIn(clause, text)

    def test_tools_list(self):
        names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["graph_log", "graph_search", "graph_constraint_drop"])

    def test_unknown_tool_and_method(self):
        self.assertEqual(rpc("tools/call", {"name": "nope"})["error"]["code"], -32602)
        self.assertEqual(rpc("nope")["error"]["code"], -32601)

    def test_notification_gets_no_response(self):
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "initialized"}))

    def test_serve_roundtrip_and_parse_error(self):
        out = io.StringIO()
        server.serve(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n'), out)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(lines[0]["result"], {})
        self.assertEqual(lines[1]["error"]["code"], -32700)


class TestGraphLog(unittest.TestCase):
    def test_decision_becomes_self_report_candidate(self):
        d = deps()
        err, text = tool("graph_log", {
            "bucket": "decision", "text": "Chose sqlite over postgres: no daemon.",
            "entities": ["knowledge/store.py"], "rejected_alternatives": ["postgres"]}, d)
        self.assertFalse(err)
        self.assertEqual(json.loads(text)["status"], "stored")
        c = d["ingest"].seen[0]
        self.assertEqual((c.bucket_hint, c.source), ("decision", "self_report"))
        self.assertEqual(c.rejected_alternatives, ("postgres",))
        self.assertEqual(c.entities, ("knowledge/store.py",))
        self.assertEqual(c.scope, "repo:/w/repo")

    def test_constraint_is_explicit(self):
        d = deps()
        tool("graph_log", {"bucket": "constraint", "text": "never push upstream",
                           "scope": "machine"}, d)
        c = d["ingest"].seen[0]
        self.assertEqual((c.source, c.scope), ("explicit", "machine"))

    def test_decision_without_rejected_alternatives_refused_before_ingest(self):
        d = deps()
        err, text = tool("graph_log", {"bucket": "decision", "text": "chose x"}, d)
        self.assertTrue(err)
        self.assertIn("rejected_alternatives", text)
        self.assertEqual(d["ingest"].seen, [])

    def test_bad_input_is_a_tool_error(self):
        for args in ({"bucket": "nope", "text": "x"}, {"bucket": "doc", "text": "  "},
                     {"bucket": "doc", "text": "x", "scope": "galaxy"},
                     {"bucket": "doc", "text": "x", "entities": "notalist"}):
            err, _ = tool("graph_log", args, deps())
            self.assertTrue(err, args)

    def test_secret_is_redacted_before_ingest(self):
        d = deps()
        tool("graph_log", {"bucket": "doc", "text": "token ghp_" + "a" * 36 + " worked"}, d)
        self.assertNotIn("ghp_", d["ingest"].seen[0].text)

    def test_ingest_statuses_pass_through(self):
        for status in ("dropped", "duplicate", "refused", "off"):
            d = deps(ingest=FakeIngest({"status": status, "id": None, "reason": "r"}))
            err, text = tool("graph_log", {"bucket": "doc", "text": "x"}, d)
            self.assertFalse(err)
            self.assertEqual(json.loads(text)["status"], status)

    def test_ingest_exception_fails_as_tool_error(self):
        class Boom:
            def ingest(self, c):
                raise RuntimeError("secret detail")
        err, text = tool("graph_log", {"bucket": "doc", "text": "x"}, deps(ingest=Boom()))
        self.assertTrue(err)
        self.assertNotIn("secret detail", text)


class TestSearchAndDrop(unittest.TestCase):
    def test_search_with_query_uses_nearest(self):
        d = deps()
        err, text = tool("graph_search", {"query": "sqlite"}, d)
        self.assertFalse(err)
        self.assertEqual(len(json.loads(text)["nodes"]), 2)
        self.assertEqual(d["store"].nearest_args[0], "sqlite")
        self.assertEqual(d["store"].nearest_args[3][0], "repo:/w/repo")

    def test_search_bucket_filter(self):
        _, text = tool("graph_search", {"bucket": "fix"}, deps())
        self.assertEqual([n["id"] for n in json.loads(text)["nodes"]], [1])

    def test_search_constraints(self):
        _, text = tool("graph_search", {"bucket": "constraint"}, deps())
        self.assertEqual(json.loads(text)["nodes"][0]["text"], "never push upstream")

    def test_search_bad_bucket(self):
        err, _ = tool("graph_search", {"bucket": "x"}, deps())
        self.assertTrue(err)

    def test_drop(self):
        d = deps()
        _, text = tool("graph_constraint_drop", {"id": 3}, d)
        self.assertEqual(json.loads(text), {"dropped": True, "id": 3})
        _, text = tool("graph_constraint_drop", {"id": 99}, d)
        self.assertFalse(json.loads(text)["dropped"])

    def test_drop_rejects_non_integer(self):
        for bad in ("3", True, None, 1.5):
            err, _ = tool("graph_constraint_drop", {"id": bad}, deps())
            self.assertTrue(err, bad)


if __name__ == "__main__":
    unittest.main()
