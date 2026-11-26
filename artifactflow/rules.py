"""The checks. Each finding has a code, a severity and a location."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from . import names as nm
from .expr import Unknown, to_str
from .model import KNOWN_UPLOADERS, Op, RunGraph, fmt_version

SEVERITIES = ("error", "warn", "info")

RULES = {
    "AF101": "two uploads in one run produce the same artifact name",
    "AF102": "same name with overwrite: true from jobs that run in parallel",
    "AF103": "matrix comes from an expression and the name does not vary per leg",
    "AF104": "artifact name contains a character the service rejects",
    "AF105": "path selects hidden files that upload-artifact leaves out",
    "AF106": "archive: false ignores name: the artifact is named after the file",
    "AF201": "download by name that nothing in this run uploads",
    "AF202": "download runs in a job that does not need the uploading job",
    "AF203": "pattern matches no artifact uploaded in this run",
    "AF204": "pattern matches uploads from jobs this job does not need",
    "AF205": "several artifacts land in per-artifact subdirectories",
    "AF206": "run-id / repository without github-token is ignored",
    "AF207": "conflicting download inputs (name + pattern / artifact-ids)",
    "AF301": "artifact action v1-v3: fails on github.com since 2025-01-30",
    "AF302": "input needs a newer version of the action than the one pinned",
    "AF303": "retention-days outside 1-90",
}


@dataclass
class Finding:
    code: str
    severity: str
    path: str
    line: int
    job: str
    message: str
    detail: List[str] = field(default_factory=list)

    def key(self):
        return (self.path, self.line, self.code, self.message)

    def to_dict(self):
        return {"code": self.code, "severity": self.severity, "path": self.path, "line": self.line,
                "job": self.job, "message": self.message, "detail": self.detail}


PRODUCERS = ("upload", "merge", "pages-upload")

# input -> (kind, minimum version, what happens on older versions)
NEEDS_VERSION = {
    ("upload", "overwrite"): (4, 2, 0),
    ("upload", "include-hidden-files"): (4, 4, 0),
    ("upload", "archive"): (7, 0, 0),
    ("download", "pattern"): (4, 0, 0),
    ("download", "merge-multiple"): (4, 0, 0),
    ("download", "artifact-ids"): (4, 3, 0),
    ("download", "skip-decompress"): (8, 0, 0),
    ("download", "digest-mismatch"): (8, 0, 0),
}


def _short(v) -> str:
    return "v%d" % v[0] if v[1:] == (0, 0) else "v%d.%d.%d" % v


def _q(s: str) -> str:
    return "'%s'" % s


def produced_name(op: Op):
    """Name the artifact really gets (archive: false => file basename)."""
    if op.kind == "upload" and op.flag("archive", True) is False and (op.version is None or op.version >= (7, 0, 0)):
        p = op.inputs.get("path")
        if isinstance(p, str) and "\n" not in p.strip():
            base = p.strip().rstrip("/").split("/")[-1]
            if base and not any(ch in base for ch in "*?[{"):
                return (base,), True
            if base:
                parts = []
                for piece in re.split(r"(\*+|\?|\[[^\]]*\]|\{[^}]*\})", base):
                    if not piece:
                        continue
                    if piece[0] in "*?[{":
                        parts.append(Unknown("file name", True))
                    else:
                        parts.append(piece)
                return tuple(parts), True
        return (Unknown("basename(path)", True),), True
    return op.name, False


def _hidden_lines(path_value: str) -> List[str]:
    """Path lines whose matches upload-artifact drops when include-hidden-files is false.

    @actions/glob skips any visited entry whose basename starts with '.': that is the
    search root (the literal part of the pattern) and everything below it. Directories
    above the search root are not visited, so `~/.npm/_logs/*.log` is fine.
    """
    out = []
    for raw in path_value.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        segs = [s for s in line.replace("\\", "/").split("/") if s not in ("", ".")]
        # resolve '..'
        norm: List[str] = []
        for s in segs:
            if s == ".." and norm and norm[-1] != "..":
                norm.pop()
            else:
                norm.append(s)
        first_glob = next((i for i, s in enumerate(norm) if any(c in s for c in "*?[")), len(norm))
        visited = norm[max(first_glob - 1, 0):]
        if any(s.startswith(".") and s != ".." and not s.startswith("${{") for s in visited):
            out.append(line)
    return out


def _ordered(producer: Op, consumer: Op, graph: RunGraph) -> bool:
    if producer.leg.job_id == consumer.leg.job_id:
        return producer.leg.leg_index == consumer.leg.leg_index and producer.step_index < consumer.step_index
    return producer.leg.job_id in graph.ancestors.get(consumer.leg.job_id, set())


def _parallel(a: Op, b: Op, graph: RunGraph) -> bool:
    if a.leg.job_id == b.leg.job_id:
        return a.leg.leg_index != b.leg.leg_index
    anc_a = graph.ancestors.get(a.leg.job_id, set())
    anc_b = graph.ancestors.get(b.leg.job_id, set())
    return a.leg.job_id not in anc_b and b.leg.job_id not in anc_a


def _before(job_id: str, idx: float, op: Op, graph: RunGraph) -> bool:
    if job_id == op.leg.job_id:
        return idx < op.step_index
    return job_id in graph.ancestors.get(op.leg.job_id, set())


def _possible_uploaders(op: Op, graph: RunGraph) -> List[str]:
    return sorted({u for j, i, u in graph.possible_uploaders if _before(j, i, op, graph)})


def _later_same_leg(p: Op, op: Op) -> bool:
    return (p.leg.job_id == op.leg.job_id and p.leg.leg_index == op.leg.leg_index
            and p.step_index > op.step_index)


def _definite_match(name, pattern) -> bool:
    """Matches whatever the unknown parts of the name turn out to be (approximation)."""
    def fill(x):
        return tuple(x if isinstance(x, str) else "" for x in name), \
            tuple(x if isinstance(x, str) else "\x01q" for x in name)
    if not nm.is_known(pattern):
        return False
    a, b = fill(None)
    pat = nm.literal(pattern)
    return nm.name_matches_pattern(a, pat) and nm.name_matches_pattern(b, pat)


def _conditional(op: Op) -> bool:
    return bool(op.step_if or op.leg.job_if or op.leg.caller_ifs)


def check_graph(g: RunGraph, reusable: bool = False) -> List[Finding]:
    f: List[Finding] = []
    add = f.append
    ops = g.ops
    producers = [o for o in ops if o.kind in PRODUCERS]
    has_remote = bool(g.remote_calls)

    seen_static: Set[Tuple[str, int, str]] = set()

    def once(op: Op, code: str) -> bool:
        k = (op.path, op.line, code)
        if k in seen_static:
            return False
        seen_static.add(k)
        return True

    # --- per-op static checks ---------------------------------------------
    for op in ops:
        v = op.version
        if op.kind in ("upload", "download", "merge") and v is not None and v[0] <= 3 and once(op, "AF301"):
            add(Finding("AF301", "error", op.path, op.line, op.leg.job_id,
                        "%s@%s: v1-v3 of the artifact actions fail on github.com since 2025-01-30" % (op.action, op.ref),
                        ["use v4 or later (GHES: v3 is still the supported line)"]))
        base_kind = "download" if op.kind == "download" else "upload"
        if v is not None and op.kind in ("upload", "download"):
            for (kind, inp), need in NEEDS_VERSION.items():
                if kind == base_kind and inp in op.raw_inputs and v < need and v[0] > 3 and once(op, "AF302" + inp):
                    add(Finding("AF302", "warn", op.path, op.line, op.leg.job_id,
                                "'%s' needs %s %s+, this step pins %s: the input is ignored (\"Unexpected input\")"
                                % (inp, op.action, _short(need), op.ref)))
        if op.kind in PRODUCERS:
            rd = op.inputs.get("retention-days")
            if isinstance(rd, (str, float)) and to_str(rd).strip():
                try:
                    days = int(float(to_str(rd)))
                except ValueError:
                    days = None
                if days is not None and days != 0 and not (1 <= days <= 90) and once(op, "AF303"):
                    add(Finding("AF303", "info", op.path, op.line, op.leg.job_id,
                                "retention-days: %d is outside 1-90; the repository maximum applies instead" % days))
            pname, archive_false = produced_name(op)
            if nm.is_known(pname):
                bad = nm.invalid_chars(nm.literal(pname))
                if bad:
                    add(Finding("AF104", "error", op.path, op.line, op.leg.job_id,
                                "artifact name %s contains %s: upload fails with \"The artifact name is not valid\""
                                % (_q(nm.literal(pname)), " ".join(repr(c)[1:-1] for c in bad)),
                                ["leg: %s" % op.leg.label] if op.leg.leg else []))
            if op.kind == "upload":
                p = op.inputs.get("path")
                hidden = op.flag("include-hidden-files", False)
                if isinstance(p, str) and hidden is False:
                    hl = _hidden_lines(p)
                    if hl and once(op, "AF105"):
                        real = [ln for ln in p.splitlines() if ln.strip() and not ln.strip().startswith(("!", "#"))]
                        all_hidden = len(hl) == len(real)
                        nf = to_str(op.inputs.get("if-no-files-found") or "warn")
                        sev = "warn"
                        tail = ("every path is hidden, so nothing is uploaded (if-no-files-found: %s)" % nf
                                if all_hidden else "those files are left out of the artifact")
                        if all_hidden and nf == "error":
                            tail = "every path is hidden: the step fails with \"No files were found\""
                            sev = "error"
                        add(Finding("AF105", sev, op.path, op.line, op.leg.job_id,
                                    "hidden path %s without include-hidden-files: true; %s"
                                    % (", ".join(_q(x) for x in hl), tail),
                                    ["upload-artifact >= v4.4.0 skips files and folders starting with '.'"]))
                if archive_false and op.flag("archive", True) is False:
                    if isinstance(p, str) and ("\n" in p.strip() or any(c in p for c in "*?[") or p.strip().endswith("/")) and once(op, "AF106m"):
                        add(Finding("AF106", "warn", op.path, op.line, op.leg.job_id,
                                    "archive: false uploads exactly one file, but path %s can match a directory or several files"
                                    " (\"only a single file can be uploaded\")" % _q(p.strip().replace("\n", " "))))
        if op.kind == "download":
            ri = op.raw_inputs
            if "name" in ri and "artifact-ids" in ri and once(op, "AF207i"):
                add(Finding("AF207", "error", op.path, op.line, op.leg.job_id,
                            "name and artifact-ids together: the step fails (\"cannot be used together\")"))
            elif ri.get("name") not in (None, "") and ri.get("pattern") not in (None, "") and once(op, "AF207p"):
                add(Finding("AF207", "warn", op.path, op.line, op.leg.job_id,
                            "name and pattern together: pattern %s is ignored, only %s is downloaded"
                            % (_q(str(ri["pattern"])), _q(str(ri["name"])))))
            if not ri.get("github-token") and (ri.get("run-id") or ri.get("repository")) and once(op, "AF206"):
                runid = str(ri.get("run-id") or "")
                if runid.replace(" ", "") not in ("${{github.run_id}}",):
                    keys = [k for k in ("run-id", "repository") if ri.get(k)]
                    add(Finding("AF206", "error", op.path, op.line, op.leg.job_id,
                                "%s set without github-token: download-artifact ignores it and looks in this run"
                                % " and ".join(keys),
                                ["add github-token: ${{ github.token }} (and actions: read)"]))

    # --- producers: collisions -------------------------------------------
    by_key: Dict[str, List[Op]] = {}
    for op in producers:
        pname, _ = produced_name(op)
        k = nm.collision_key(pname)
        if k is None:
            continue
        if nm.is_known(pname) and nm.invalid_chars(nm.literal(pname)):
            continue  # AF104: every one of these fails anyway
        by_key.setdefault(k, []).append(op)
    reported_pairs: Set[Tuple[str, str]] = set()
    for k, group in by_key.items():
        if len(group) < 2:
            continue
        name_disp = nm.display(produced_name(group[0])[0])
        # the first upload wins; every later one without overwrite fails
        conflicts = [o for o in group[1:] if o.flag("overwrite", False) is not True]
        if conflicts:
            locs = {}
            for o in group:
                locs.setdefault((o.path, o.line, o.leg.job_id), []).append(o)
            first = group[0]
            cond = any(_conditional(o) for o in group)
            same_leg_steps = len({(o.leg.job_id, o.leg.leg_index) for o in group}) == 1 and \
                len({o.step_if for o in group}) == len(group)
            sites = ["%s %s" % (o.where, o.leg.label) for o in group[:6]]
            if len(group) > 6:
                sites.append("... %d more" % (len(group) - 6))
            pair = (first.where, k)
            if pair not in reported_pairs:
                reported_pairs.add(pair)
                add(Finding("AF101", "info" if same_leg_steps else "warn" if cond else "error", first.path, first.line, first.leg.job_id,
                            "%d uploads named %s in one run: every one after the first fails with "
                            "(409) Conflict: an artifact with this name already exists" % (len(group), _q(name_disp)),
                            sites + (["some of these have if: conditions; fine only if at most one runs"] if cond else [])
                            + ["fix: put the matrix values in the name, e.g. name: %s-${{ matrix.<key> }}" % name_disp
                               if len({o.leg.job_id for o in group}) == 1 else
                               "fix: give each upload its own name (or overwrite: true if a later job should replace it)"]))
        else:
            par = [(a, b) for i, a in enumerate(group) for b in group[i + 1:] if _parallel(a, b, g)]
            if par:
                a, b = par[0]
                add(Finding("AF102", "warn", a.path, a.line, a.leg.job_id,
                            "%s is uploaded with overwrite: true by jobs that run at the same time; "
                            "whichever finishes last wins" % _q(name_disp),
                            ["%s %s" % (a.where, a.leg.label), "%s %s" % (b.where, b.leg.label)]))
    for op in producers:
        if op.kind != "upload" or op.leg.legs_known:
            continue
        if op.leg.leg_index == 0 and not any(isinstance(p, Unknown) and p.varying for p in op.name) and once(op, "AF103"):
            add(Finding("AF103", "warn", op.path, op.line, op.leg.job_id,
                        "matrix comes from an expression and name %s has no matrix value in it: "
                        "with more than one leg, every leg after the first fails with (409) Conflict"
                        % _q(nm.display(op.name))))

    # --- consumers ----------------------------------------------------------
    for op in ops:
        if op.kind not in ("download", "merge", "pages-deploy"):
            continue
        ri = op.raw_inputs
        if op.kind == "download" and ri.get("github-token") and ri.get("run-id") and \
                str(ri.get("run-id")).replace(" ", "") != "${{github.run_id}}":
            continue  # another run: nothing to check against
        if op.kind == "download" and ri.get("artifact-ids"):
            continue
        if op.kind == "download" and not ri.get("github-token") and ri.get("run-id") and \
                str(ri.get("run-id")).replace(" ", "") != "${{github.run_id}}":
            continue  # AF206 already explains why this finds nothing
        others = [p for p in producers if p is not op and not _later_same_leg(p, op)]
        maybe = _possible_uploaders(op, g)
        if op.kind in ("download", "pages-deploy") and op.name:
            cands = [p for p in producers if p is not op and nm.names_may_equal(produced_name(p)[0], op.name)]
            disp = nm.display(op.name)
            if not cands:
                # archive: false hint
                af = [p for p in others if p.kind == "upload" and produced_name(p)[1]
                      and nm.names_may_equal(p.name, op.name)]
                if af:
                    p = af[0]
                    add(Finding("AF106", "error", op.path, op.line, op.leg.job_id,
                                "downloads %s, but the upload at %s has archive: false, so the artifact is named %s"
                                % (_q(disp), p.where, _q(nm.display(produced_name(p)[0]))),
                                ["with archive: false the name input is ignored; download name: %s"
                                 % nm.display(produced_name(p)[0])]))
                    continue
                known = [u for u in maybe if u.split("@")[0].lower() in KNOWN_UPLOADERS and any(
                    nm.literal(op.name).startswith(n.strip())
                    for n in KNOWN_UPLOADERS[u.split("@")[0].lower()].split(","))]
                if known:
                    continue
                sev = "warn" if (has_remote or nm.has_varying(op.name) or reusable or maybe
                                 or _conditional(op)) else "error"
                hint = []
                if _conditional(op):
                    hint.append("the step or job has an if: condition; this fails only when it runs")
                if maybe:
                    hint.append("may be uploaded by %s" % ", ".join(maybe[:3]))
                close = sorted({nm.display(produced_name(p)[0]) for p in producers})[:5]
                if close:
                    hint.append("uploaded in this run: " + ", ".join(close))
                if has_remote:
                    hint.append("this run also calls %s; it may upload it" % ", ".join(u for _, u in g.remote_calls[:3]))
                if reusable:
                    sev = "info"
                    hint.append("reusable workflow with no caller in this repo: the caller may upload it")
                if once(op, "AF201"):
                    add(Finding("AF201", sev, op.path, op.line, op.leg.job_id,
                                "downloads %s but nothing in this run uploads it: \"Artifact '%s' not found\""
                                % (_q(disp), disp), hint))
                continue
            if not any(_ordered(p, op, g) for p in cands) and once(op, "AF202"):
                p = cands[0]
                same = p.leg.job_id == op.leg.job_id
                add(Finding("AF202", "error", op.path, op.line, op.leg.job_id,
                            ("%s is uploaded %s (%s)" % (_q(disp), "later in the same job" if p.step_index > op.step_index
                                                         else "by a different leg of this matrix job", p.where)) if same else
                            "%s is uploaded by job '%s', which job '%s' does not need: the download can run "
                            "before the upload exists" % (_q(disp), p.leg.job_id, op.leg.job_id),
                            [] if same else ["add needs: [%s]" % p.leg.job_id.split("/")[0]]))
            continue
        # pattern or "download everything"
        pattern = op.pattern
        if op.kind == "download" and pattern is None:
            matched = others
            pat_disp = None
        else:
            pat_disp = nm.display(pattern or ("*",))
            pat_tokens = nm.name_tokens(pattern or ("*",)) if pattern and not nm.is_known(pattern) else None
            matched = []
            for p in others:
                pname = produced_name(p)[0]
                if pat_tokens is not None:
                    if nm.intersects(nm.name_tokens(pname), pat_tokens):
                        matched.append(p)
                elif nm.name_matches_pattern(pname, nm.literal(pattern or ("*",))):
                    matched.append(p)
        if pat_disp is not None and not matched:
            if once(op, "AF203"):
                add(Finding("AF203", "info" if (reusable or maybe) else "warn", op.path, op.line, op.leg.job_id,
                            "pattern %s matches no artifact uploaded in this run: the step succeeds with "
                            "\"Total of 0 artifact(s) downloaded\"" % _q(pat_disp),
                            (["uploaded in this run: " + ", ".join(sorted({nm.display(produced_name(p)[0]) for p in producers})[:6])]
                             if producers else []) + (["may be uploaded by %s" % ", ".join(maybe[:3])] if maybe else [])))
            continue
        unordered = [p for p in matched if not _ordered(p, op, g)
                     and (pattern is None or _definite_match(produced_name(p)[0], pattern))]
        ordered = [p for p in matched if _ordered(p, op, g)]
        if unordered and once(op, "AF204"):
            jobs = sorted({p.leg.job_id for p in unordered})
            what = "pattern %s" % _q(pat_disp) if pat_disp else "download with no name or pattern"
            add(Finding("AF204", "error" if not ordered else "warn", op.path, op.line, op.leg.job_id,
                        "%s matches artifacts from %s, which job '%s' does not need: it gets whatever exists "
                        "when it runs" % (what, ", ".join("'%s'" % j for j in jobs), op.leg.job_id),
                        ["add needs: [%s]" % ", ".join(sorted({j.split("/")[0] for j in jobs}))]))
        if op.kind != "download":
            continue
        names_matched = {nm.display(produced_name(p)[0]) for p in matched}
        many = len(matched) > 1 or any(not p.leg.legs_known for p in matched)
        merge = op.flag("merge-multiple", False)
        if merge is False and once(op, "AF205"):
            dest = to_str(op.inputs.get("path") or "") or "$GITHUB_WORKSPACE"
            v = op.version
            if many:
                ex = sorted(names_matched)[:3]
                add(Finding("AF205", "info", op.path, op.line, op.leg.job_id,
                            "%d artifacts match, so each is extracted to %s/<artifact-name>/ (merge-multiple is false)"
                            % (len(names_matched), dest.rstrip("/")),
                            ["e.g. " + ", ".join("%s/%s/" % (dest.rstrip("/"), n) for n in ex),
                             "set merge-multiple: true to get one flat directory"]))
            elif v is not None and v[0] == 4 and pat_disp is not None:
                add(Finding("AF205", "info", op.path, op.line, op.leg.job_id,
                            "download-artifact@%s puts even a single pattern match in %s/%s/; v5+ extracts it flat"
                            % (op.ref, dest.rstrip("/"), sorted(names_matched)[0])))
            elif pat_disp is not None:
                add(Finding("AF205", "info", op.path, op.line, op.leg.job_id,
                            "pattern %s matches one artifact, so it is extracted flat into %s; once a second artifact "
                            "matches, files move to %s/<artifact-name>/" % (_q(pat_disp), dest, dest.rstrip("/"))))
    return f


def check(graphs: Sequence[RunGraph], reusable_paths: Set[str] = frozenset()) -> List[Finding]:
    out: List[Finding] = []
    seen = set()
    for g in graphs:
        if g.workflow.error:
            out.append(Finding("AF000", "error", g.workflow.path, 1, "", g.workflow.error))
            continue
        for fd in check_graph(g, g.workflow.path in reusable_paths):
            if fd.key() in seen:
                continue
            seen.add(fd.key())
            out.append(fd)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    out.sort(key=lambda x: (x.path, x.line, order[x.severity], x.code))
    return out
