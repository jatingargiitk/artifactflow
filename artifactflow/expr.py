"""A small evaluator for GitHub Actions ``${{ }}`` expressions.

Only what is needed to work out artifact names statically: context lookups,
literals, ``||``/``&&``/``!``/comparisons and the string functions people put
in names. Anything that is only known at run time comes back as ``Unknown``,
which remembers whether the value can differ between matrix legs of one job
(``varying``) or is the same for the whole run (``varying=False``, e.g.
``github.sha`` or ``inputs.tag``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple, Union


@dataclass(frozen=True)
class Unknown:
    desc: str
    varying: bool = True

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return "<%s>" % self.desc


class ExprError(ValueError):
    pass


_TOKEN_RE = re.compile(
    r"""\s*(?:
      (?P<num>-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][-+]?\d+)?))
    | (?P<str>'(?:[^']|'')*')
    | (?P<op>\|\||&&|==|!=|<=|>=|[!<>()\[\],.*])
    | (?P<ident>[A-Za-z_][A-Za-z0-9_-]*)
    )""",
    re.VERBOSE,
)


def tokenize(src: str) -> List[Tuple[str, str]]:
    pos = 0
    out: List[Tuple[str, str]] = []
    src = src.strip()
    while pos < len(src):
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            raise ExprError("cannot parse expression near %r" % src[pos:pos + 20])
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        while pos < len(src) and src[pos].isspace():
            pos += 1
    return out


# AST nodes are plain tuples: ("lit", v) ("ctx", name) ("prop", obj, key)
# ("index", obj, expr) ("star", obj) ("call", name, args) ("not", x)
# ("bin", op, a, b)


class _Parser:
    def __init__(self, tokens: List[Tuple[str, str]]):
        self.t = tokens
        self.i = 0

    def peek(self) -> Optional[Tuple[str, str]]:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, value: Optional[str] = None) -> Tuple[str, str]:
        tok = self.peek()
        if tok is None or (value is not None and tok[1] != value):
            raise ExprError("expected %r" % value)
        self.i += 1
        return tok

    def parse(self):
        node = self.or_()
        if self.peek() is not None:
            raise ExprError("unexpected %r" % (self.peek()[1],))
        return node

    def or_(self):
        node = self.and_()
        while self.peek() == ("op", "||"):
            self.take()
            node = ("bin", "||", node, self.and_())
        return node

    def and_(self):
        node = self.cmp()
        while self.peek() == ("op", "&&"):
            self.take()
            node = ("bin", "&&", node, self.cmp())
        return node

    def cmp(self):
        node = self.unary()
        while self.peek() and self.peek()[0] == "op" and self.peek()[1] in ("==", "!=", "<", ">", "<=", ">="):
            op = self.take()[1]
            node = ("bin", op, node, self.unary())
        return node

    def unary(self):
        if self.peek() == ("op", "!"):
            self.take()
            return ("not", self.unary())
        return self.postfix(self.primary())

    def primary(self):
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")
        kind, val = tok
        if kind == "num":
            self.take()
            return ("lit", float(int(val, 16)) if val.lower().startswith(("0x", "-0x")) else float(val))
        if kind == "str":
            self.take()
            return ("lit", val[1:-1].replace("''", "'"))
        if kind == "op" and val == "(":
            self.take()
            node = self.or_()
            self.take(")")
            return node
        if kind == "ident":
            self.take()
            low = val.lower()
            if low in ("true", "false"):
                return ("lit", low == "true")
            if low == "null":
                return ("lit", None)
            if self.peek() == ("op", "("):
                self.take()
                args = []
                if self.peek() != ("op", ")"):
                    args.append(self.or_())
                    while self.peek() == ("op", ","):
                        self.take()
                        args.append(self.or_())
                self.take(")")
                return ("call", low, args)
            return ("ctx", low)
        raise ExprError("unexpected %r" % val)

    def postfix(self, node):
        while True:
            tok = self.peek()
            if tok == ("op", "."):
                self.take()
                nxt = self.take()
                if nxt == ("op", "*"):
                    node = ("star", node)
                else:
                    node = ("prop", node, nxt[1])
            elif tok == ("op", "["):
                self.take()
                if self.peek() == ("op", "*"):
                    self.take()
                    self.take("]")
                    node = ("star", node)
                else:
                    idx = self.or_()
                    self.take("]")
                    node = ("index", node, idx)
            else:
                return node


def parse(src: str):
    return _Parser(tokenize(src)).parse()


Value = Union[None, bool, float, str, list, dict, Unknown]


def _merge_unknown(*vals: Any) -> Unknown:
    descs = [v.desc for v in vals if isinstance(v, Unknown)]
    varying = any(v.varying for v in vals if isinstance(v, Unknown))
    return Unknown(" ".join(descs) or "?", varying)


def to_str(v: Value) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v, indent=2)
    return str(v)


def _truthy(v: Value) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, float):
        return v != 0
    if isinstance(v, str):
        return v != ""
    return True


def to_number(v: Value) -> float:
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, float):
        return v
    if isinstance(v, str):
        t = v.strip()
        if t == "":
            return 0.0
        try:
            return float(int(t, 16)) if t.lower().startswith("0x") else float(t)
        except ValueError:
            return float("nan")
    return float("nan")


def _coerce_pair(a: Value, b: Value):
    """GitHub's loose comparison: strings case-insensitively, mixed types as numbers."""
    if isinstance(a, str) and isinstance(b, str):
        return a.lower(), b.lower()
    if type(a) is type(b) and not isinstance(a, (list, dict)):
        return a, b
    if isinstance(a, (list, dict)) or isinstance(b, (list, dict)):
        return id(a), id(b)
    return to_number(a), to_number(b)


def _cmp_norm(v: Value):
    if isinstance(v, str):
        return v.lower()
    return v


def _desc(node) -> str:
    kind = node[0]
    if kind == "ctx":
        return node[1]
    if kind == "prop":
        return "%s.%s" % (_desc(node[1]), node[2])
    if kind == "index":
        inner = node[2]
        if inner[0] == "lit":
            return "%s.%s" % (_desc(node[1]), to_str(inner[1]))
        return "%s[...]" % _desc(node[1])
    if kind == "call":
        return "%s(...)" % node[1]
    return "expr"


Resolver = Callable[[str], Value]


def evaluate(node, resolve: Resolver) -> Value:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "ctx":
        return resolve(node[1])
    if kind in ("prop", "index"):
        obj = evaluate(node[1], resolve)
        if kind == "prop":
            key: Value = node[2]
        else:
            key = evaluate(node[2], resolve)
        if isinstance(obj, Unknown):
            return Unknown(_desc(node), obj.varying)
        if isinstance(key, Unknown):
            return Unknown(_desc(node), key.varying)
        if hasattr(obj, "get_prop"):
            return obj.get_prop(to_str(key).lower())
        if isinstance(obj, dict):
            ks = to_str(key).lower()
            for k, v in obj.items():
                if str(k).lower() == ks:
                    return v
            return None
        if isinstance(obj, list) and isinstance(key, float):
            i = int(key)
            return obj[i] if 0 <= i < len(obj) else None
        return None
    if kind == "star":
        obj = evaluate(node[1], resolve)
        if isinstance(obj, dict):
            return list(obj.values())
        if isinstance(obj, Unknown):
            return obj
        return []
    if kind == "not":
        v = evaluate(node[1], resolve)
        if isinstance(v, Unknown):
            return v
        return not _truthy(v)
    if kind == "bin":
        op = node[1]
        a = evaluate(node[2], resolve)
        if op == "||":
            if isinstance(a, Unknown):
                b = evaluate(node[3], resolve)
                return _merge_unknown(a, b) if isinstance(b, Unknown) else Unknown(a.desc + " || ...", a.varying)
            return a if _truthy(a) else evaluate(node[3], resolve)
        if op == "&&":
            if isinstance(a, Unknown):
                b = evaluate(node[3], resolve)
                if not isinstance(b, Unknown) and not _truthy(b):
                    return b  # falsy whichever side is returned
                return a
            return evaluate(node[3], resolve) if _truthy(a) else a
        b = evaluate(node[3], resolve)
        if isinstance(a, Unknown) or isinstance(b, Unknown):
            return _merge_unknown(a, b)
        x, y = _coerce_pair(a, b)
        try:
            if op == "==":
                return x == y
            if op == "!=":
                return x != y
            if op == "<":
                return x < y
            if op == ">":
                return x > y
            if op == "<=":
                return x <= y
            if op == ">=":
                return x >= y
        except TypeError:
            return False
    if kind == "call":
        return _call(node[1], [evaluate(a, resolve) for a in node[2]])
    raise ExprError("unknown node %r" % (kind,))


def _call(name: str, args: List[Value]) -> Value:
    unknowns = [a for a in args if isinstance(a, Unknown)]
    if name == "hashfiles":
        return Unknown("hashFiles(...)", True)
    if name == "format":
        if not args or isinstance(args[0], Unknown):
            return _merge_unknown(*args) if unknowns else ""
        fmt = to_str(args[0])
        rest = args[1:]
        if any(isinstance(a, Unknown) for a in rest):
            return Unknown("format(...)", any(a.varying for a in rest if isinstance(a, Unknown)))

        def rep(m):
            if m.group(0) == "{{":
                return "{"
            if m.group(0) == "}}":
                return "}"
            i = int(m.group(1))
            return to_str(rest[i]) if i < len(rest) else ""

        return re.sub(r"\{\{|\}\}|\{(\d+)\}", rep, fmt)
    if unknowns:
        return Unknown("%s(...)" % name, any(u.varying for u in unknowns))
    if name == "join":
        arr = args[0] if args else []
        sep = to_str(args[1]) if len(args) > 1 else ","
        if isinstance(arr, list):
            return sep.join(to_str(x) for x in arr)
        return to_str(arr)
    if name == "contains":
        hay, needle = (args + [None, None])[:2]
        if isinstance(hay, list):
            return any(_cmp_norm(x) == _cmp_norm(needle) for x in hay)
        return to_str(needle).lower() in to_str(hay).lower()
    if name == "startswith":
        return to_str(args[0]).lower().startswith(to_str(args[1]).lower())
    if name == "endswith":
        return to_str(args[0]).lower().endswith(to_str(args[1]).lower())
    if name == "tojson":
        return json.dumps(args[0] if args else None, indent=2)
    if name == "fromjson":
        try:
            v = json.loads(to_str(args[0]))
        except ValueError:
            return Unknown("fromJSON(...)", True)
        return float(v) if isinstance(v, int) and not isinstance(v, bool) else v
    if name in ("success", "always", "failure", "cancelled"):
        return Unknown(name + "()", True)
    return Unknown("%s(...)" % name, True)


_TEMPLATE_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)

Part = Union[str, Unknown]


def render(text: Any, resolve: Resolver) -> Tuple[Part, ...]:
    """Render a string that may contain ``${{ }}`` into literal / Unknown parts."""
    if text is None:
        return ()
    if not isinstance(text, str):
        return (to_str(_yaml_scalar(text)),)
    parts: List[Part] = []
    pos = 0
    for m in _TEMPLATE_RE.finditer(text):
        if m.start() > pos:
            parts.append(text[pos:m.start()])
        try:
            v = evaluate(parse(m.group(1)), resolve)
        except (ExprError, RecursionError):
            v = Unknown(m.group(1).strip(), True)
        if isinstance(v, Unknown):
            parts.append(v)
        else:
            parts.append(to_str(v))
        pos = m.end()
    if pos < len(text):
        parts.append(text[pos:])
    # merge adjacent literals
    merged: List[Part] = []
    for p in parts:
        if isinstance(p, str) and merged and isinstance(merged[-1], str):
            merged[-1] = merged[-1] + p
        elif p != "":
            merged.append(p)
    return tuple(merged)


def _yaml_scalar(v: Any) -> Value:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


def evaluate_value(text: Any, resolve: Resolver) -> Value:
    """Evaluate a YAML value: a whole ``${{ x }}`` keeps its type, otherwise a string."""
    if isinstance(text, str):
        m = _TEMPLATE_RE.fullmatch(text.strip())
        if m:
            try:
                return evaluate(parse(m.group(1)), resolve)
            except (ExprError, RecursionError):
                return Unknown(m.group(1).strip(), True)
        parts = render(text, resolve)
        if all(isinstance(p, str) for p in parts):
            return "".join(parts)  # type: ignore[arg-type]
        return _merge_unknown(*[p for p in parts if isinstance(p, Unknown)])
    return _yaml_scalar(text)


_STATUS_TRUE = re.compile(r"\b(always|success)\(\s*\)", re.I)


def evaluate_if(text: Any, resolve: Resolver) -> Optional[bool]:
    """Evaluate a job/step ``if:``. True/False when known on the normal (green) path, else None."""
    if text is None:
        return True
    if isinstance(text, bool):
        return text
    src = str(text).strip()
    m = _TEMPLATE_RE.fullmatch(src)
    if m:
        src = m.group(1)
    elif "${{" in src:
        return None
    src = _STATUS_TRUE.sub("true", src)
    try:
        v = evaluate(parse(src), resolve)
    except (ExprError, RecursionError):
        return None
    if isinstance(v, Unknown):
        return None
    return _truthy(v)
