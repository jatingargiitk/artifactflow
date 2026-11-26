"""Load workflows and turn them into per-leg artifact producers and consumers."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from . import matrix as mx
from .expr import Part, Unknown, Value, evaluate_if, evaluate_value, render, to_str


class LineDict(dict):
    line = 0


class _Loader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    d = LineDict()
    d.line = node.start_mark.line + 1
    for k_node, v_node in node.value:
        k = loader.construct_object(k_node, deep=True)
        v = loader.construct_object(v_node, deep=True)
        d[k] = v
    return d


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
# YAML 1.1 turns a bare `on` into True; keep workflow keys as strings.
_Loader.yaml_implicit_resolvers = {
    k: [(tag, rx) for tag, rx in v if tag != "tag:yaml.org,2002:bool"]
    for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load_yaml(text: str) -> Any:
    data = yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    return _fix_bools(data)


_TRUE = {"true", "True", "TRUE"}
_FALSE = {"false", "False", "FALSE"}


def _fix_bools(v: Any) -> Any:
    # We disabled the bool resolver (so `on:` survives); map YAML 1.2 booleans back.
    if isinstance(v, LineDict):
        for k in list(v.keys()):
            v[k] = _fix_bools(v[k])
        return v
    if isinstance(v, list):
        return [_fix_bools(x) for x in v]
    if isinstance(v, str):
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    return v


ARTIFACT_ACTIONS = {
    "actions/upload-artifact": "upload",
    "actions/upload-artifact/merge": "merge",
    "actions/download-artifact": "download",
    "actions/upload-pages-artifact": "pages-upload",
    "actions/deploy-pages": "pages-deploy",
}

# Third-party actions known to call upload-artifact themselves (names are computed inside them).
KNOWN_UPLOADERS = {
    "hynek/build-and-inspect-python-package": "Packages, Package Metadata, PyPI README",
}

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_SHA_COMMENT_RE = re.compile(r"#\s*(v?\d+(?:\.\d+){0,2})\b")


def parse_version(ref: str, raw_line: str = "") -> Optional[Tuple[int, int, int]]:
    m = _VERSION_RE.match(ref or "")
    if not m and raw_line:
        c = _SHA_COMMENT_RE.search(raw_line)
        if c:
            m = _VERSION_RE.match(c.group(1))
    if not m:
        return None
    # `@v4` / `@v4.1` are moving tags: they resolve to the newest release in that line
    return (int(m.group(1)), int(m.group(2) if m.group(2) is not None else 999),
            int(m.group(3) if m.group(3) is not None else 999))


def fmt_version(v: Optional[Tuple[int, int, int]]) -> str:
    if v is None:
        return "?"
    return ".".join(["v%d" % v[0]] + [str(x) for x in v[1:] if x != 999])


@dataclass
class JobLeg:
    """One concrete job instance in a run (a matrix leg, maybe inside a reusable workflow)."""

    job_id: str  # display id, e.g. "build" or "call-tests/test"
    leg: Dict[str, Any]
    leg_index: int
    legs_known: bool
    n_legs: Optional[int]
    workflow_path: str
    line: int
    job_if: Optional[str]
    caller_ifs: Tuple[str, ...] = ()

    @property
    def label(self) -> str:
        lbl = mx.leg_label(self.leg)
        if not self.legs_known:
            return self.job_id + " (matrix from expression)"
        return "%s (%s)" % (self.job_id, lbl) if lbl else self.job_id


@dataclass
class Op:
    kind: str  # upload | merge | download | pages-upload | pages-deploy
    leg: JobLeg
    step_index: float
    step_name: str
    path: str
    line: int
    action: str
    version: Optional[Tuple[int, int, int]]
    ref: str
    inputs: Dict[str, Value]
    raw_inputs: Dict[str, Any]
    name: Tuple[Part, ...] = ()
    pattern: Optional[Tuple[Part, ...]] = None
    step_if: Optional[str] = None  # set only when the condition is not known to be true

    @property
    def where(self) -> str:
        return "%s:%d" % (self.path, self.line)

    def flag(self, key: str, default: bool = False) -> Optional[bool]:
        v = self.inputs.get(key)
        if key not in self.inputs or v is None or v == "":
            return default
        if isinstance(v, Unknown):
            return None
        if isinstance(v, bool):
            return v
        return to_str(v).strip().lower() == "true"


@dataclass
class Workflow:
    path: str
    data: Dict[str, Any]
    raw_lines: List[str]
    error: Optional[str] = None

    @property
    def name(self) -> str:
        n = self.data.get("name") if isinstance(self.data, dict) else None
        return str(n) if n else self.path

    @property
    def triggers(self) -> Set[str]:
        on = self.data.get("on") if isinstance(self.data, dict) else None
        if isinstance(on, str):
            return {on}
        if isinstance(on, list):
            return {str(x) for x in on}
        if isinstance(on, dict):
            return {str(k) for k in on}
        return set()

    @property
    def jobs(self) -> Dict[str, Any]:
        j = self.data.get("jobs") if isinstance(self.data, dict) else None
        return j if isinstance(j, dict) else {}


def load_workflow(path: str, display_path: str) -> Workflow:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.splitlines()
    try:
        data = load_yaml(text)
    except yaml.YAMLError as e:
        return Workflow(display_path, {}, lines, error="YAML error: %s" % str(e).splitlines()[0])
    if not isinstance(data, dict):
        return Workflow(display_path, {}, lines, error="not a workflow mapping")
    return Workflow(display_path, data, lines)


def find_workflows(root: str) -> List[str]:
    wf_dir = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return []
    out = []
    for fn in sorted(os.listdir(wf_dir)):
        if fn.endswith((".yml", ".yaml")):
            out.append(os.path.join(wf_dir, fn))
    return out


_OS_MAP = (("ubuntu", "Linux"), ("windows", "Windows"), ("macos", "macOS"))


def _runner_os(runs_on: Value) -> Value:
    vals: List[str] = []
    if isinstance(runs_on, str):
        vals = [runs_on]
    elif isinstance(runs_on, list):
        vals = [to_str(x) for x in runs_on if isinstance(x, str)]
    elif isinstance(runs_on, Unknown):
        return Unknown("runner.os", runs_on.varying)
    for v in vals:
        low = v.lower()
        for prefix, os_name in _OS_MAP:
            if low.startswith(prefix):
                return os_name
    return None


@dataclass
class RunGraph:
    """Everything that happens in one workflow run: job legs, needs, artifact ops."""

    workflow: Workflow
    legs: List[JobLeg] = field(default_factory=list)
    ancestors: Dict[str, Set[str]] = field(default_factory=dict)  # job_id -> needed job_ids (transitive)
    ops: List[Op] = field(default_factory=list)
    remote_calls: List[Tuple[str, str]] = field(default_factory=list)  # (job_id, uses)
    possible_uploaders: List[Tuple[str, float, str]] = field(default_factory=list)  # (job_id, step, uses)
    notes: List[str] = field(default_factory=list)


class Builder:
    def __init__(self, root: str):
        self.root = root
        self.cache: Dict[str, Workflow] = {}

    def workflow(self, rel: str) -> Optional[Workflow]:
        if rel.startswith("./"):
            rel = rel[2:]
        if rel not in self.cache:
            full = os.path.join(self.root, rel)
            if not os.path.isfile(full):
                return None
            self.cache[rel] = load_workflow(full, rel)
        return self.cache[rel]

    def build(self, wf: Workflow) -> RunGraph:
        g = RunGraph(wf)
        self._add_jobs(g, wf, prefix="", outer_needs=set(), inputs=None, outer_env={}, depth=0, caller_ifs=())
        return g

    def _add_jobs(self, g: RunGraph, wf: Workflow, prefix: str, outer_needs: Set[str],
                  inputs: Optional[Dict[str, Value]], outer_env: Dict[str, Any], depth: int,
                  caller_ifs: Tuple[str, ...], outer_leg: Optional[Dict[str, Any]] = None) -> Set[str]:
        """Add wf's jobs to g. Returns the set of job ids added (for callers' needs)."""
        jobs = wf.jobs
        added: Set[str] = set()
        wf_env = wf.data.get("env") if isinstance(wf.data.get("env"), dict) else {}
        # needs closure inside this workflow
        direct: Dict[str, List[str]] = {}
        for jid, job in jobs.items():
            needs = job.get("needs") if isinstance(job, dict) else None
            if isinstance(needs, str):
                needs = [needs]
            direct[jid] = [str(n) for n in needs] if isinstance(needs, list) else []
        closure: Dict[str, Set[str]] = {}

        def close(j: str, seen: Tuple[str, ...] = ()) -> Set[str]:
            if j in closure:
                return closure[j]
            out: Set[str] = set()
            for n in direct.get(j, []):
                if n in seen:
                    continue
                out.add(n)
                out |= close(n, seen + (j,))
            closure[j] = out
            return out

        expanded_ids: Dict[str, Set[str]] = {}  # local job -> display ids (reusable calls expand)
        order = list(jobs.keys())
        for jid in order:
            close(jid)

        # first pass: decide display ids for callers (needed for needs of later jobs)
        for jid in order:
            expanded_ids[jid] = {prefix + jid}

        for jid in order:
            job = jobs[jid]
            if not isinstance(job, dict):
                continue
            disp = prefix + jid
            anc: Set[str] = set(outer_needs)
            for n in closure[jid]:
                anc |= expanded_ids.get(n, {prefix + n})
            legs = mx.expand((job.get("strategy") or {}).get("matrix") if isinstance(job.get("strategy"), dict) else None)
            legs_known = legs is not None
            if legs is None:
                legs = [{}]
            job_if = job.get("if")
            if job_if is not None:
                jctx = self._ctx(wf, jid, {}, 0, 1, False, job, {}, wf_env, {}, inputs, outer_env)
                jcond = evaluate_if(job_if, jctx)
                if jcond is False:
                    g.notes.append("%s: skipped, if: is always false" % disp)
                    continue
                if jcond is True:
                    job_if = None
            job_env = job.get("env") if isinstance(job.get("env"), dict) else {}
            uses = job.get("uses")
            if isinstance(uses, str):
                if uses.startswith("./") and depth < 9:
                    callee = self.workflow(uses.split("@")[0])
                    if callee is None or callee.error:
                        g.notes.append("%s: reusable workflow %s not found or unreadable" % (disp, uses))
                        g.remote_calls.append((disp, uses))
                        g.ancestors[disp] = anc
                        continue
                    sub_ids: Set[str] = set()
                    for li, leg in enumerate(legs):
                        ctx = self._ctx(wf, jid, leg, li, len(legs), legs_known, job, {}, wf_env, job_env, inputs, outer_env)
                        with_ = job.get("with") if isinstance(job.get("with"), dict) else {}
                        callee_inputs = self._callee_inputs(callee, with_, ctx)
                        sub_prefix = disp + ("[%d]" % li if len(legs) > 1 else "") + "/"
                        ids = self._add_jobs(g, callee, sub_prefix, anc, callee_inputs, {}, depth + 1,
                                             caller_ifs + ((str(job_if),) if job_if else ()))
                        sub_ids |= ids
                    expanded_ids[jid] = sub_ids or {disp}
                    added |= sub_ids
                    continue
                g.remote_calls.append((disp, uses))
                g.ancestors[disp] = anc
                added.add(disp)
                continue
            g.ancestors[disp] = anc
            added.add(disp)
            for li, leg in enumerate(legs):
                jl = JobLeg(disp, leg, li, legs_known, len(legs) if legs_known else None, wf.path,
                            getattr(job, "line", 0), str(job_if) if job_if is not None else None, caller_ifs)
                g.legs.append(jl)
                steps = job.get("steps") if isinstance(job.get("steps"), list) else []

                def factory(step_env, _leg=leg, _li=li, _n=len(legs), _known=legs_known, _job=job, _jid=jid,
                            _jenv=job_env):
                    return self._ctx(wf, _jid, _leg, _li, _n, _known, _job, step_env, wf_env, _jenv, inputs, outer_env)

                self._add_steps(g, jl, steps, factory, wf.path, wf.raw_lines, 0.0, 1.0, 0)
        return added

    def _add_steps(self, g: RunGraph, jl: JobLeg, steps: List[Any], factory, path: str,
                   raw_lines: List[str], base: float, scale: float, depth: int) -> None:
        for si, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            suses = step.get("uses")
            if not isinstance(suses, str):
                continue
            step_env = step.get("env") if isinstance(step.get("env"), dict) else {}
            ctx = factory(step_env)
            with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
            idx = base + si * scale
            cond = evaluate_if(step.get("if"), ctx) if "if" in step else True
            if cond is False:
                continue  # never runs on this leg
            step_if = None if cond is True else str(step.get("if"))
            if suses.startswith("./") and depth < 4:
                self._add_composite(g, jl, suses, with_, ctx, idx, scale / 100.0, depth, step_if)
                continue
            if "@" not in suses:
                continue
            action, ref = suses.rsplit("@", 1)
            kind = ARTIFACT_ACTIONS.get(action.lower())
            if not kind:
                if any(("artifact" in str(k).lower()) for k in with_) or action.lower() in KNOWN_UPLOADERS:
                    g.possible_uploaders.append((jl.job_id, idx, suses))
                continue
            vals = {str(k): evaluate_value(v, ctx) for k, v in with_.items()}
            line = getattr(step, "line", 0)
            raw_line = ""
            for ln in range(max(line - 1, 0), min(line + 6, len(raw_lines))):
                if suses in raw_lines[ln]:
                    raw_line = raw_lines[ln]
                    break
            op = Op(kind, jl, idx, str(step.get("name") or step.get("id") or "step %d" % (si + 1)),
                    path, line, action, parse_version(ref, raw_line), ref, vals, dict(with_), step_if=step_if)
            if kind in ("upload", "merge", "pages-upload"):
                default = {"upload": "artifact", "merge": "merged-artifacts", "pages-upload": "github-pages"}[kind]
                op.name = render(with_.get("name") if with_.get("name") not in (None, "") else default, ctx)
                if kind == "merge":
                    op.pattern = render(with_.get("pattern") or "*", ctx)
            elif kind == "download":
                if with_.get("name") not in (None, ""):
                    op.name = render(with_["name"], ctx)
                if with_.get("pattern") not in (None, ""):
                    op.pattern = render(with_["pattern"], ctx)
            elif kind == "pages-deploy":
                op.name = render(with_.get("artifact_name") or "github-pages", ctx)
            g.ops.append(op)

    def _add_composite(self, g: RunGraph, jl: JobLeg, uses: str, with_: Dict[str, Any], ctx, idx: float,
                       scale: float, depth: int, step_if: Optional[str]) -> None:
        rel = uses[2:].rstrip("/")
        meta = None
        for fn in ("action.yml", "action.yaml"):
            full = os.path.join(self.root, rel, fn)
            if os.path.isfile(full):
                meta = load_workflow(full, rel + "/" + fn)
                break
        if meta is None or meta.error:
            return
        runs = meta.data.get("runs")
        if not isinstance(runs, dict) or str(runs.get("using", "")).lower() != "composite":
            return
        steps = runs.get("steps") if isinstance(runs.get("steps"), list) else []
        decl = meta.data.get("inputs") if isinstance(meta.data.get("inputs"), dict) else {}
        cin: Dict[str, Value] = {}
        for k, spec in decl.items():
            if isinstance(spec, dict) and "default" in spec:
                cin[str(k).lower()] = evaluate_value(spec["default"], ctx)
        for k, v in with_.items():
            cin[str(k).lower()] = evaluate_value(v, ctx)
        inner_inputs = _Inputs(cin)

        def factory(step_env, _outer=ctx):
            def resolve(root: str) -> Value:
                if root == "inputs":
                    return inner_inputs
                if root == "matrix":
                    return Unknown("matrix", True)  # not available inside composite actions
                if root == "env":
                    outer_env = _outer("env")

                    def look(name: str) -> Value:
                        for k, v in step_env.items():
                            if str(k).lower() == name.lower():
                                return evaluate_value(v, resolve)
                        return outer_env.get_prop(name) if hasattr(outer_env, "get_prop") else Unknown("env." + name)
                    return _LazyEnv(look)
                return _outer(root)
            return resolve

        before = len(g.ops)
        self._add_steps(g, jl, steps, factory, meta.path, meta.raw_lines, idx, scale, depth + 1)
        if step_if:
            for op in g.ops[before:]:
                op.step_if = op.step_if or step_if

    def _callee_inputs(self, callee: Workflow, with_: Dict[str, Any], ctx) -> Dict[str, Value]:
        out: Dict[str, Value] = {}
        on = callee.data.get("on")
        decl = {}
        if isinstance(on, dict) and isinstance(on.get("workflow_call"), dict):
            decl = on["workflow_call"].get("inputs") or {}
        if isinstance(decl, dict):
            for k, spec in decl.items():
                if isinstance(spec, dict) and "default" in spec:
                    out[str(k).lower()] = evaluate_value(spec["default"], lambda n: Unknown(n, False))
        for k, v in with_.items():
            out[str(k).lower()] = evaluate_value(v, ctx)
        return out

    def _ctx(self, wf: Workflow, jid: str, leg: Dict[str, Any], li: int, n: int, legs_known: bool,
             job: Dict[str, Any], step_env: Dict[str, Any], wf_env: Dict[str, Any], job_env: Dict[str, Any],
             inputs: Optional[Dict[str, Value]], outer_env: Dict[str, Any]):
        cache: Dict[str, Value] = {}
        resolving: Set[str] = set()

        def env_lookup(name: str) -> Value:
            low = name.lower()
            for scope in (step_env, job_env, wf_env):
                for k, v in scope.items():
                    if str(k).lower() == low:
                        if low in resolving:
                            return Unknown("env." + name, True)
                        resolving.add(low)
                        try:
                            return evaluate_value(v, resolve)
                        finally:
                            resolving.discard(low)
            return Unknown("env." + name, True)

        def resolve(root: str) -> Value:
            if root in cache:
                return cache[root]
            v: Value
            if root == "matrix":
                v = _norm(leg) if legs_known else Unknown("matrix", True)
            elif root == "strategy":
                v = {"job-index": float(li), "job-total": float(n)} if legs_known else Unknown("strategy", True)
            elif root == "runner":
                runs_on = evaluate_value(job.get("runs-on"), resolve_no_runner)
                os_v = _runner_os(runs_on)
                v = {"os": os_v if os_v is not None else Unknown("runner.os", True),
                     "arch": Unknown("runner.arch", True), "name": Unknown("runner.name", True),
                     "temp": Unknown("runner.temp", False), "tool_cache": Unknown("runner.tool_cache", False)}
            elif root == "github":
                v = _GithubCtx(jid, wf)
            elif root == "env":
                v = _LazyEnv(env_lookup)
            elif root == "inputs":
                v = _Inputs(inputs)
            elif root in ("vars", "secrets", "needs"):
                v = Unknown(root, False)
            else:
                v = Unknown(root, True)
            cache[root] = v
            return v

        def resolve_no_runner(root: str) -> Value:
            if root == "runner":
                return Unknown("runner", True)
            return resolve(root)

        return resolve


def _norm(v: Any) -> Any:
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


class _GithubCtx:
    """github.* : job and workflow are known, everything else is the same for the whole run."""

    def __init__(self, jid: str, wf: Workflow):
        self.known = {"job": jid, "workflow": wf.name}

    def get_prop(self, key: str) -> Value:
        if key in self.known:
            return self.known[key]
        return Unknown("github." + key, False)


class _LazyEnv:
    def __init__(self, lookup):
        self._lookup = lookup

    def get_prop(self, key: str) -> Value:
        return self._lookup(key)


class _Inputs:
    def __init__(self, inputs: Optional[Dict[str, Value]]):
        self._inputs = inputs

    def get_prop(self, key: str) -> Value:
        if self._inputs is None:
            return Unknown("inputs." + key, False)
        return self._inputs.get(key)
