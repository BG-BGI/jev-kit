"""Tests for knowledge.signature: normalization, exception type, pair eval."""
import json
import os
import unittest

from knowledge.signature import exception_type, normalize, signature

PAIRS = os.path.join(os.path.dirname(__file__), "..", "eval", "knowledge", "pairs.jsonl")


class NormalizeTests(unittest.TestCase):
    def test_paths_numbers_and_quotes(self):
        n = normalize("FileNotFoundError: [Errno 2] No such file: '/Users/a/x.csv'")
        self.assertIn("<S>", n)
        self.assertNotIn("/Users", n)
        self.assertIn("[Errno <N>]", n)

    def test_bare_absolute_path(self):
        self.assertEqual(normalize("cat: /tmp/a/b: No such file or directory"),
                         "cat: <PATH>: No such file or directory")

    def test_hex_and_uuid(self):
        n = normalize("Error: object 3f2a9c1deadbeef at 0x7fff12ab not found")
        self.assertEqual(n.count("<HEX>"), 2)
        n = normalize("Error: job 123e4567-e89b-12d3-a456-426614174000 failed")
        self.assertIn("<HEX>", n)

    def test_plain_word_not_hex(self):
        self.assertIn("defaced", normalize("Error: defaced"))

    def test_no_stack_frames(self):
        tb = 'Traceback (most recent call last):\n  File "/a.py", line 3, in f\n    x()\nKeyError: \'k\''
        self.assertEqual(normalize(tb), "KeyError: <S>")

    def test_head_capped(self):
        self.assertLessEqual(len(normalize("Error: " + "word " * 500)), 300)

    def test_whitespace_collapsed(self):
        self.assertEqual(normalize("Error:   a \t b"), "Error: a b")

    def test_never_raises(self):
        for bad in (None, "", 5, b"x"):
            normalize(bad)
            exception_type(bad)
            signature("Bash", bad)


class ExceptionTypeTests(unittest.TestCase):
    def test_python(self):
        self.assertEqual(exception_type("Traceback (most recent call last):\n  File \"a\", line 1\nKeyError: 'x'"), "KeyError")

    def test_dotted(self):
        self.assertEqual(exception_type("subprocess.CalledProcessError: boom"), "CalledProcessError")

    def test_node(self):
        self.assertEqual(exception_type("TypeError: x is not a function"), "TypeError")

    def test_exit_code(self):
        self.assertEqual(exception_type("Exit code 1\nsomething broke"), "ExitCode")

    def test_none(self):
        self.assertEqual(exception_type("all fine"), "")


class SignatureTests(unittest.TestCase):
    def test_shape(self):
        s = signature("Bash", "KeyError: 'a'")
        self.assertEqual(len(s), 16)
        int(s, 16)

    def test_tool_matters(self):
        self.assertNotEqual(signature("Bash", "KeyError: 'a'"), signature("Edit", "KeyError: 'a'"))

    def test_stable_across_paths_and_lines(self):
        a = 'Traceback (most recent call last):\n  File "/u/a.py", line 4, in f\nValueError: bad value 12'
        b = 'Traceback (most recent call last):\n  File "/v/b.py", line 900, in g\nValueError: bad value 7'
        self.assertEqual(signature("Bash", a), signature("Bash", b))

    def test_different_cause_differs(self):
        self.assertNotEqual(signature("Bash", "ValueError: too many values to unpack"),
                            signature("Bash", "ValueError: invalid literal for int()"))


class PairEvalTests(unittest.TestCase):
    def test_precision_and_recall(self):
        rows = []
        with open(PAIRS, encoding="utf-8") as f:
            rows = [json.loads(ln) for ln in f if ln.strip()]
        self.assertGreaterEqual(len(rows), 80)
        tp = fp = fn = tn = 0
        for r in rows:
            pred = signature("Bash", r["a"]) == signature("Bash", r["b"])
            if pred and r["same"]:
                tp += 1
            elif pred:
                fp += 1
            elif r["same"]:
                fn += 1
            else:
                tn += 1
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        print(f"\nsignature pairs={len(rows)} tp={tp} fp={fp} fn={fn} tn={tn} "
              f"precision={precision:.3f} recall={recall:.3f}")
        self.assertGreaterEqual(precision, 0.95)


if __name__ == "__main__":
    unittest.main()
