"""Expand a ``strategy.matrix`` the way GitHub Actions does."""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

from .expr import Unknown


def _is_expr(v: Any) -> bool:
    return isinstance(v, str) and "${{" in v


def expand(matrix: Any) -> Optional[List[Dict[str, Any]]]:
    """Return the list of legs, or None when the matrix is only known at run time."""
    if matrix is None:
        return [{}]
    if _is_expr(matrix) or not isinstance(matrix, dict):
        return None
    base_keys = [k for k in matrix if k not in ("include", "exclude")]
    values = []
    for k in base_keys:
        v = matrix[k]
        if _is_expr(v) or not isinstance(v, list):
            return None
        values.append(v)
    include = matrix.get("include") or []
    exclude = matrix.get("exclude") or []
    if _is_expr(include) or _is_expr(exclude):
        return None
    if base_keys:
        legs = [dict(zip(base_keys, combo)) for combo in itertools.product(*values)]
    else:
        legs = []

    def matches(leg: Dict[str, Any], ex: Dict[str, Any]) -> bool:
        for k, v in ex.items():
            if k not in leg:
                return False
            if isinstance(v, dict) and isinstance(leg[k], dict):
                if not all(leg[k].get(kk) == vv for kk, vv in v.items()):
                    return False
            elif leg[k] != v:
                return False
        return True

    if isinstance(exclude, list):
        legs = [leg for leg in legs if not any(isinstance(ex, dict) and matches(leg, ex) for ex in exclude)]

    originals = [dict(leg) for leg in legs]
    if isinstance(include, list):
        for inc in include:
            if not isinstance(inc, dict):
                continue
            added = False
            for idx, orig in enumerate(originals):
                # can be added if it does not overwrite any original matrix value
                if all(k not in orig or orig[k] == v for k, v in inc.items() if k in base_keys):
                    legs[idx].update({k: v for k, v in inc.items() if k not in orig or k not in base_keys})
                    added = True
            if not added:
                legs.append(dict(inc))
    if not legs:
        return [{}] if not base_keys else []
    return legs


def leg_label(leg: Dict[str, Any]) -> str:
    if not leg:
        return ""
    def s(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (dict, list)):
            return "{...}" if isinstance(v, dict) else "[...]"
        return str(v)
    return ", ".join(s(v) for v in leg.values())


def unknown_matrix() -> Unknown:
    return Unknown("matrix", True)
