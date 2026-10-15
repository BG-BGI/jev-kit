"""Error signature: the exact identity of a failure.

A signature is a hash of the tool name, the exception type and the
placeholder-normalized head of the error message. It carries no stack frames,
so the same failure hits the same signature from a different file, line,
checkout path or run.

Normalization, in order: pick the error line out of a traceback or log, cut
it to the first 300 characters, then replace quoted strings with `<S>`,
absolute paths with `<PATH>`, hex ids and hashes with `<HEX>`, numbers with
`<N>`, and collapse whitespace.

Pure functions, no I/O, never raise. On any internal error `signature`
returns a hash of the raw head so a caller still gets a stable string.

    from knowledge.signature import normalize, exception_type, signature
"""
import hashlib
import re

HEAD_CHARS = 300

_EXC_NAME = r"(?:[A-Za-z_][\w]*\.)*[A-Z][\w]*(?:Error|Exception|Exit|Warning|Failure|Interrupt|Fault)"
# A line that names an exception: "KeyError: 'a'", "pkg.mod.FooError: msg", "FooError".
_PY_EXC = re.compile(r"^(" + _EXC_NAME + r")(?::|\s|$)")
# "Error: msg" as Node prints it, plus "Uncaught"/"Unhandled" prefixes.
_NODE_EXC = re.compile(r"^(?:Uncaught |Unhandled )?(Error|[A-Z]\w*Error)(?::|$)")
_OTHER_LINE = re.compile(
    r"^(?:fatal:|error:|error\[|ERROR:|ERROR |FAILED |fail:|panic:|"
    r"npm ERR! code |npm error code |[\w./+-]+: .*(?:command not found|"
    r"No such file or directory|Permission denied|not a directory|"
    r"Is a directory|Operation not permitted))"
)
_EXIT_PREFIX = re.compile(r"^\s*Exit code:?\s*(\d+)\s*$", re.I)
_PYTEST_E = re.compile(r"^E\s{2,}")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_QUOTED = re.compile(r"""'[^'\n]*'|"[^"\n]*"|`[^`\n]*`""")
_PATH = re.compile(
    r"(?<![\w<])(?:[A-Za-z]:\\[^\s'\"`,;)]+|~?/[^\s'\"`,;)]*[^\s'\"`,;):.]|\.{1,2}/[^\s'\"`,;)]+)"
)
_LIST = re.compile(r"'\[[^\]\n]*\]'")
_RELPATH = re.compile(r"(?<![\w<])[\w.-]+(?:/[\w.-]+)+(?![\w>])")
_FILE = re.compile(
    r"(?<![\w<])[\w-]+\.(?:py|js|ts|tsx|jsx|json|md|rs|go|sh|yml|yaml|toml|txt|cfg|ini)\b"
)
_PIN = re.compile(r"[\w.-]+==[\w.*]+")
_NF_A = re.compile(r"^([\w.-]+): [^\s:]+: command not found$")
_NF_B = re.compile(r"^([\w.-]+): command not found: \S+$")
_HEX = re.compile(r"\b(?:0x[0-9a-fA-F]+|[0-9a-fA-F]{7,64})\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_NUM = re.compile(r"(?<![\w<])\d+(?:\.\d+)*(?![\w>])")
_VER_NUM = re.compile(r"(?<=\w)\d+(?=\b)")
_WS = re.compile(r"\s+")
_NPM_PREFIX = re.compile(r"^npm (?:ERR!|error)\s*")


def _lines(text):
    text = _ANSI.sub("", text or "")
    return [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]


def _is_error_line(ln):
    ln = _PYTEST_E.sub("", ln) if _PYTEST_E.match(ln) else ln
    return bool(_PY_EXC.match(ln) or _NODE_EXC.match(ln) or _OTHER_LINE.match(ln))


def _error_lines(text):
    """The part of `text` that states the failure, as a list of lines."""
    lines = [ln for ln in _lines(text) if not _EXIT_PREFIX.match(ln)]
    if not lines:
        return []
    npm = [ln for ln in lines if ln.startswith(("npm ERR!", "npm error"))]
    if npm:
        body = [_NPM_PREFIX.sub("", ln) for ln in npm]
        body = [b for b in body if b and not b.startswith("A complete log")]
        return body[:2]
    # Last line that names an exception wins: it is the final one of a
    # chained traceback, and a pytest summary line comes after the E lines.
    for i in range(len(lines) - 1, -1, -1):
        ln = lines[i]
        if _PYTEST_E.match(ln):
            ln = _PYTEST_E.sub("", ln)
        if ln.startswith("FAILED ") and " - " in ln:
            ln = ln.split(" - ", 1)[1]
        if _PY_EXC.match(ln) or _NODE_EXC.match(ln):
            return [ln]
        if _OTHER_LINE.match(ln):
            return [ln]
    # No recognisable error line: the first line stands for the failure.
    return lines[:1]


def _head(text):
    return " ".join(_error_lines(text))[:HEAD_CHARS]


def normalize(text):
    """Placeholder-normalized head of the error message in `text`."""
    try:
        s = _head(text)
        s = _NF_A.sub(r"\1: <S>: command not found", s)
        s = _NF_B.sub(r"\1: command not found: <S>", s)
        s = _LIST.sub("<S>", s)
        s = _QUOTED.sub("<S>", s)
        s = _PIN.sub("<S>", s)
        s = _PATH.sub("<PATH>", s)
        s = _UUID.sub("<HEX>", s)
        s = _HEX.sub(lambda m: "<HEX>" if re.search(r"\d", m.group(0)) else m.group(0), s)
        s = _RELPATH.sub("<PATH>", s)
        s = _FILE.sub("<PATH>", s)
        s = _NUM.sub("<N>", s)
        return _WS.sub(" ", s).strip()
    except Exception:
        return ""


def exception_type(text):
    """The exception class name, `ExitCode` for a bare exit code, or ''."""
    try:
        lines = _lines(text)
        errs = _error_lines(text)
        if errs:
            first = errs[0]
            m = _PY_EXC.match(first) or _NODE_EXC.match(first)
            if m:
                return m.group(1).rsplit(".", 1)[-1]
            if any(ln.startswith(("npm ERR!", "npm error")) for ln in lines):
                m = re.match(r"code\s+(\w+)", first)
                if m:
                    return m.group(1)
        if any(_EXIT_PREFIX.match(ln) for ln in lines):
            return "ExitCode"
        return ""
    except Exception:
        return ""


def signature(tool, text):
    """sha1 hex[:16] of `tool|exception_type|normalize(text)`."""
    try:
        key = "|".join((tool or "", exception_type(text), normalize(text)))
    except Exception:
        key = "|".join((tool or "", "", ""))
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
