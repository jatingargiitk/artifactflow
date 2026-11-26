"""artifactflow command line."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Set

from . import __version__
from . import names as nm
from .model import Builder, RunGraph, Workflow, find_workflows, load_workflow
from .rules import PRODUCERS, RULES, SEVERITIES, Finding, _ordered, check, produced_name

SEV_RANK = {"error": 0, "warn": 1, "info": 2}


def _rel(path: str, root: str) -> str:
    try:
        r = os.path.relpath(path, root)
    except ValueError:
        return path
    return r if not r.startswith("..") else path


def build_graphs(root: str, files: Optional[Sequence[str]] = None):
    """Return (graphs, reusable_paths)."""
    b = Builder(root)
    paths = list(files) if files else find_workflows(root)
    wfs: List[Workflow] = []
    for p in paths:
        rel = _rel(os.path.abspath(p), os.path.abspath(root))
        wf = b.cache.get(rel) or load_workflow(p, rel)
        b.cache[rel] = wf
        wfs.append(wf)
    # local reusable workflows that some local workflow calls
    called: Set[str] = set()
    for wf in b.cache.values():
        for job in wf.jobs.values():
            if isinstance(job, dict) and isinstance(job.get("uses"), str) and job["uses"].startswith("./"):
                called.add(job["uses"].split("@")[0][2:])
    graphs: List[RunGraph] = []
    reusable: Set[str] = set()
    for wf in wfs:
        trig = wf.triggers
        if "workflow_call" in trig:
            if trig == {"workflow_call"} and wf.path in called:
                continue  # checked through its callers
            reusable.add(wf.path)
        graphs.append(b.build(wf))
    return graphs, reusable


def _print_findings(findings: List[Finding], out) -> None:
    for fd in findings:
        job = " [%s]" % fd.job if fd.job else ""
        out.write("%s:%d  %-5s  %s%s  %s\n" % (fd.path, fd.line, fd.severity, fd.code, job, fd.message))
        for d in fd.detail:
            out.write("    %s\n" % d)


def cmd_check(args) -> int:
    root = args.dir
    if args.workflow:
        files = args.workflow
    else:
        files = None
        if not os.path.isdir(os.path.join(root, ".github", "workflows")):
            sys.stderr.write("artifactflow: no .github/workflows in %s\n" % root)
            return 2
    graphs, reusable = build_graphs(root, files)
    findings = check(graphs, reusable)
    if args.ignore:
        ign = {c.strip().upper() for c in args.ignore.split(",")}
        findings = [f for f in findings if f.code not in ign]
    shown = [f for f in findings if SEV_RANK[f.severity] <= SEV_RANK[args.min_severity]]
    n_up = sum(1 for g in graphs for o in g.ops if o.kind in PRODUCERS)
    n_down = sum(1 for g in graphs for o in g.ops if o.kind in ("download", "merge", "pages-deploy"))
    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    if args.json:
        json.dump({"version": __version__, "workflows": len(graphs), "uploads": n_up, "downloads": n_down,
                   "counts": counts, "findings": [f.to_dict() for f in shown]}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_findings(shown, sys.stdout)
        if shown:
            sys.stdout.write("\n")
        sys.stdout.write("%d workflow%s, %d upload step%s, %d download step%s (per matrix leg): %d error%s, %d warning%s, %d info\n"
                         % (len(graphs), "" if len(graphs) == 1 else "s",
                            n_up, "" if n_up == 1 else "s", n_down, "" if n_down == 1 else "s",
                            counts["error"], "" if counts["error"] == 1 else "s",
                            counts["warn"], "" if counts["warn"] == 1 else "s", counts["info"]))
    fail_rank = SEV_RANK[args.fail_on]
    return 1 if any(SEV_RANK[f.severity] <= fail_rank for f in findings) else 0


def _pattern_hits(name, pattern) -> bool:
    if nm.is_known(pattern):
        return nm.name_matches_pattern(name, nm.literal(pattern))
    return nm.intersects(nm.name_tokens(name), nm.name_tokens(pattern))


def cmd_graph(args) -> int:
    graphs, _ = build_graphs(args.dir, args.workflow or None)
    out = sys.stdout
    any_ops = False
    data = []
    for g in graphs:
        prods = [o for o in g.ops if o.kind in PRODUCERS]
        cons = [o for o in g.ops if o.kind in ("download", "merge", "pages-deploy")]
        if not prods and not cons:
            continue
        any_ops = True
        names: Dict[str, Dict] = {}
        for p in prods:
            n = nm.display(produced_name(p)[0])
            names.setdefault(n, {"up": [], "down": []})["up"].append(p)
        for c in cons:
            if c.kind == "download" and c.raw_inputs.get("github-token") and c.raw_inputs.get("run-id"):
                key = "(other run) " + (nm.display(c.name) if c.name else nm.display(c.pattern or ("*",)))
                names.setdefault(key, {"up": [], "down": []})["down"].append((c, None))
                continue
            if c.name and c.kind != "merge":
                hit = False
                for n, ent in names.items():
                    if any(nm.names_may_equal(produced_name(p)[0], c.name) for p in ent["up"]):
                        ent["down"].append((c, all(_ordered(p, c, g) for p in ent["up"]
                                                   if nm.names_may_equal(produced_name(p)[0], c.name))))
                        hit = True
                if not hit:
                    names.setdefault(nm.display(c.name), {"up": [], "down": []})["down"].append((c, False))
            else:
                pat = c.pattern or ("*",)
                hit = False
                for n, ent in list(names.items()):
                    ups = [p for p in ent["up"] if p is not c and _pattern_hits(produced_name(p)[0], pat)]
                    if ups:
                        hit = True
                        ent["down"].append((c, all(_ordered(p, c, g) for p in ups)))
                if not hit:
                    key = "(pattern %s)" % nm.display(pat)
                    names.setdefault(key, {"up": [], "down": []})["down"].append((c, False))
        wf_data = {"workflow": g.workflow.path, "artifacts": []}
        if not args.json:
            out.write("%s  (%s)\n" % (g.workflow.path, g.workflow.name))
        for n in names:
            ent = names[n]
            ups = sorted({p.leg.label for p in ent["up"]})
            downs = []
            for c, ok in ent["down"]:
                lab = c.leg.label + ("" if c.kind == "download" else " [%s]" % c.kind)
                mark = "?" if ok is None else ("ok" if ok else "NOT ORDERED" if ent["up"] else "MISSING")
                downs.append((lab, mark))
            downs = sorted(set(downs))
            wf_data["artifacts"].append({"name": n, "uploaded_by": ups,
                                         "downloaded_by": [{"job": d, "status": m} for d, m in downs]})
            if args.json:
                continue
            out.write("  %s\n" % n)
            for u in ups:
                out.write("    up    %s\n" % u)
            if not ent["up"]:
                out.write("    up    (nothing in this run)\n")
            for d, m in downs:
                out.write("    down  %s  %s\n" % (d, m))
        data.append(wf_data)
        if not args.json:
            out.write("\n")
    if args.json:
        json.dump(data, out, indent=2)
        out.write("\n")
    elif not any_ops:
        out.write("no artifact steps found\n")
    return 0


def cmd_rules(args) -> int:
    for code, text in RULES.items():
        sys.stdout.write("%s  %s\n" % (code, text))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="artifactflow",
                                 description="Check how artifacts move between jobs in GitHub Actions workflows.")
    ap.add_argument("--version", action="version", version="artifactflow " + __version__)
    sub = ap.add_subparsers(dest="cmd")

    c = sub.add_parser("check", help="find artifact hand-offs that break (default)")
    c.add_argument("dir", nargs="?", default=".")
    c.add_argument("-w", "--workflow", action="append", help="check only this workflow file (repeatable)")
    c.add_argument("--json", action="store_true")
    c.add_argument("--fail-on", choices=["error", "warn", "info"], default="error")
    c.add_argument("--min-severity", choices=["error", "warn", "info"], default="info",
                   help="hide findings below this severity")
    c.add_argument("--ignore", help="comma-separated rule codes to skip")
    c.set_defaults(func=cmd_check)

    gp = sub.add_parser("graph", help="show which job uploads and downloads each artifact")
    gp.add_argument("dir", nargs="?", default=".")
    gp.add_argument("-w", "--workflow", action="append")
    gp.add_argument("--json", action="store_true")
    gp.set_defaults(func=cmd_graph)

    r = sub.add_parser("rules", help="list rule codes")
    r.set_defaults(func=cmd_rules)

    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in ("check", "graph", "rules", "-h", "--help", "--version")):
        argv = ["check"] + argv
    args = ap.parse_args(argv)
    return args.func(args)
