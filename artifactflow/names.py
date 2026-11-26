"""Artifact names as rendered parts, and glob matching between them."""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple, Union

from .expr import Part, Unknown

STAR = ("*",)

# A glob token is ("lit", c) | ("any1", chars_or_None, negated) | ("star",)
Token = tuple

INVALID_NAME_CHARS = ['"', ":", "<", ">", "|", "*", "?", "\r", "\n", "\\", "/"]


def is_known(parts: Sequence[Part]) -> bool:
    return all(isinstance(p, str) for p in parts)


def literal(parts: Sequence[Part]) -> str:
    return "".join(p for p in parts if isinstance(p, str))


def display(parts: Sequence[Part]) -> str:
    out = []
    for p in parts:
        if isinstance(p, Unknown):
            out.append("${{ %s }}" % p.desc)
        else:
            out.append(p)
    return "".join(out)


def has_varying(parts: Sequence[Part]) -> bool:
    return any(isinstance(p, Unknown) and p.varying for p in parts)


def collision_key(parts: Sequence[Part]) -> Optional[str]:
    """Same key => certainly the same name in one run. None => cannot tell."""
    if has_varying(parts):
        return None
    out = []
    for p in parts:
        out.append("\x00%s\x00" % p.desc if isinstance(p, Unknown) else p)
    return "".join(out)


def name_tokens(parts: Sequence[Part]) -> List[Token]:
    toks: List[Token] = []
    for p in parts:
        if isinstance(p, Unknown):
            if not toks or toks[-1] != ("star",):
                toks.append(("star",))
        else:
            toks.extend(("lit", c) for c in p)
    return toks


def _expand_braces(pat: str) -> List[str]:
    depth = 0
    start = -1
    for i, c in enumerate(pat):
        if c == "\\":
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth:
            depth -= 1
            if depth == 0:
                body = pat[start + 1:i]
                alts, d, cur = [], 0, ""
                for ch in body:
                    if ch == "," and d == 0:
                        alts.append(cur)
                        cur = ""
                        continue
                    if ch == "{":
                        d += 1
                    elif ch == "}":
                        d -= 1
                    cur += ch
                alts.append(cur)
                if len(alts) == 1:
                    continue
                out = []
                for a in alts:
                    out.extend(_expand_braces(pat[:start] + a + pat[i + 1:]))
                return out
    return [pat]


def pattern_tokens(pattern: str) -> List[List[Token]]:
    """minimatch-style pattern (as used by download-artifact) -> token lists."""
    out = []
    for alt in _expand_braces(pattern):
        toks: List[Token] = []
        i = 0
        while i < len(alt):
            c = alt[i]
            if c == "\\" and i + 1 < len(alt):
                toks.append(("lit", alt[i + 1]))
                i += 2
                continue
            if c == "*":
                if not toks or toks[-1] != ("star",):
                    toks.append(("star",))
                i += 1
                continue
            if c == "?":
                toks.append(("any1", None, False))
                i += 1
                continue
            if c == "[":
                j = alt.find("]", i + 2)
                if j != -1:
                    body = alt[i + 1:j]
                    neg = body.startswith(("!", "^"))
                    if neg:
                        body = body[1:]
                    chars = set()
                    k = 0
                    while k < len(body):
                        if k + 2 < len(body) and body[k + 1] == "-":
                            for o in range(ord(body[k]), ord(body[k + 2]) + 1):
                                chars.add(chr(o))
                            k += 3
                        else:
                            chars.add(body[k])
                            k += 1
                    toks.append(("any1", frozenset(chars), neg))
                    i = j + 1
                    continue
            toks.append(("lit", c))
            i += 1
        out.append(toks)
    return out


def _tok_char_compat(a: Token, b: Token) -> bool:
    if a[0] == "lit" and b[0] == "lit":
        return a[1] == b[1]
    if a[0] == "lit":
        a, b = b, a
    if b[0] == "lit":
        chars, neg = a[1], a[2]
        if chars is None:
            return True
        return (b[1] in chars) != neg
    return True  # two classes: assume they can overlap


def intersects(p: Sequence[Token], q: Sequence[Token]) -> bool:
    """Is there a string matched by both token sequences?"""
    n, m = len(p), len(q)
    memo = {}

    def f(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == n and j == m:
            r = True
        elif i < n and p[i] == ("star",):
            r = f(i + 1, j) or (j < m and f(i, j + 1))
        elif j < m and q[j] == ("star",):
            r = f(i, j + 1) or (i < n and f(i + 1, j))
        elif i < n and j < m:
            r = _tok_char_compat(p[i], q[j]) and f(i + 1, j + 1)
        else:
            r = False
        memo[key] = r
        return r

    return f(0, 0)


def name_matches_pattern(parts: Sequence[Part], pattern: str) -> bool:
    nt = name_tokens(parts)
    return any(intersects(nt, pt) for pt in pattern_tokens(pattern))


def names_may_equal(a: Sequence[Part], b: Sequence[Part]) -> bool:
    if is_known(a) and is_known(b):
        return literal(a) == literal(b)
    ka, kb = collision_key(a), collision_key(b)
    if ka is not None and kb is not None and ka == kb:
        return True
    return intersects(name_tokens(a), name_tokens(b))


def invalid_chars(name: str) -> List[str]:
    return [c for c in INVALID_NAME_CHARS if c in name]
