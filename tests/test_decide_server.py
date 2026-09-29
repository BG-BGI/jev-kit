"""decide/server.py: the `decide` MCP tool. Fully mocked: no key, no network."""
import tests  # noqa: F401, I001 -- first import: isolates HOME and AIRLOCK_* (see tests/__init__.py)

import importlib.util
import io
import json
import unittest
from pathlib import Path

SERVER_PATH = Path(__file__).resolve().parent.parent / "decide" / "server.py"


def _load():
    spec = importlib.util.spec_from_file_location("decide_server", str(SERVER_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = _load()


class FakeJev:
    def __init__(self):
        self.bodies = []

    def __call__(self, body, timeout_s=None):
        self.bodies.append(body)
        answers = {}
        for name, q in body["questions"].items():
            if q["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.9}
            elif q["type"] == "score":
                answers[name] = {"type": "score", "score": 1.1, "confidence": 0.9,
                                 "probabilities": {"0": 0.0, "1": 0.9, "2": 0.1},
                                 "legend": {str(i): t for i, t in enumerate(q["criteria"])}}
            else:
                first = next(iter(q["criteria"]))
                probs = {k: (0.95 if k == first else 0.05 / (len(q["criteria"]) - 1))
                         for k in q["criteria"]}
                answers[name] = {"type": "choice", "choice": first, "confidence": 0.95,
                                 "probabilities": probs}
        return {"answers": answers, "usage": {"input_tokens": 1000}}, 55


class TestBuild(unittest.TestCase):
    def test_batch_of_three_kinds_in_one_request(self):
        body, order = server.build({"context": "ctx", "decisions": [
            {"name": "a", "question": "which?", "options": {"x": "ex", "y": ""}},
            {"name": "b", "kind": "score", "question": "how bad?", "options": ["low", "high"]},
            {"name": "c", "question": "true?"}]})
        self.assertEqual(order, [("a", "choice"), ("b", "score"), ("c", "yesno")])
        qs = body["questions"]
        self.assertEqual(qs["a"]["criteria"], {"x": "ex", "y": None})
        self.assertEqual(qs["b"]["criteria"], ["low", "high"])
        self.assertEqual(qs["c"]["type"], "noul")
        self.assertNotIn("criteria", qs["c"])

    def test_top_level_single_decision(self):
        _, order = server.build({"question": "which?", "options": ["x", "y"]})
        self.assertEqual(order, [("decision", "choice")])

    def test_bad_input_is_refused_with_a_reason(self):
        for args in ({"question": ""}, {"question": "q", "kind": "choice", "options": ["one"]},
                     {"question": "q", "kind": "bogus"},
                     {"decisions": [{"name": "a", "question": "q"}, {"name": "a", "question": "r"}]},
                     {"question": "q", "kind": "score", "options": [str(i) for i in range(11)]}):
            with self.assertRaises(server.DecideError):
                server.build(args)

    def test_context_and_questions_are_redacted(self):
        secret = "sk-ant-api03-" + "c" * 40
        body, _ = server.build({"context": "token %s" % secret, "question": "is %s bad?" % secret})
        self.assertNotIn("c" * 40, json.dumps(body))


class TestCall(unittest.TestCase):
    def test_shapes_answers_and_cost(self):
        result = server.call({"context": "x", "decisions": [
            {"name": "a", "question": "which?", "options": ["x", "y"]},
            {"name": "b", "kind": "score", "question": "how bad?", "options": ["low", "mid", "high"]},
            {"name": "c", "question": "true?"}]}, ask=FakeJev())
        d = result["decisions"]
        self.assertEqual(d["a"]["answer"], "x")
        self.assertTrue(d["a"]["confident"])
        self.assertEqual(d["b"]["answer"], 1.1)
        self.assertEqual(d["b"]["legend"]["2"], "high")
        self.assertEqual((d["c"]["answer"], d["c"]["probability_true"]), (True, 0.9))
        self.assertAlmostEqual(result["jev_cost_usd"], 1000 * server.PRICE_PER_MTOK_USD / 1e6)

    def test_a_transport_error_is_a_decide_error(self):
        def boom(body, timeout_s=None):
            raise OSError("down")
        with self.assertRaises(server.DecideError):
            server.call({"question": "q"}, ask=boom)


class TestProtocol(unittest.TestCase):
    def _serve(self, *msgs):
        out = io.StringIO()
        server.serve(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n"), out, FakeJev())
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_handshake_list_and_call(self):
        responses = self._serve(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "decide", "arguments": {"question": "q"}}})
        self.assertEqual([r["id"] for r in responses], [1, 2, 3])
        self.assertEqual(responses[1]["result"]["tools"][0]["name"], "decide")
        payload = json.loads(responses[2]["result"]["content"][0]["text"])
        self.assertTrue(payload["decisions"]["decision"]["answer"])

    def test_errors_are_tool_errors_not_crashes(self):
        responses = self._serve(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "decide", "arguments": {"question": ""}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "nope"}},
            {"jsonrpc": "2.0", "id": 3, "method": "no/such"})
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertEqual(responses[1]["error"]["code"], -32602)
        self.assertEqual(responses[2]["error"]["code"], -32601)

    def test_garbage_line_is_a_parse_error(self):
        out = io.StringIO()
        server.serve(io.StringIO("not json\n"), out, FakeJev())
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
