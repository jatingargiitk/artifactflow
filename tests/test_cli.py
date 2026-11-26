import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(ROOT, "examples", "demo")


def run(*args):
    env = dict(os.environ, PYTHONPATH=ROOT)
    p = subprocess.run([sys.executable, "-m", "artifactflow", *args], capture_output=True, text=True, env=env,
                       cwd=ROOT)
    return p.returncode, p.stdout, p.stderr


def test_demo_findings_and_exit_code():
    code, out, _ = run("check", DEMO)
    assert code == 1
    for c in ("AF101", "AF105", "AF205", "AF202", "AF206", "AF203", "AF106"):
        assert c in out
    assert out.strip().endswith("4 errors, 2 warnings, 1 info")


def test_default_subcommand_and_json():
    code, out, _ = run(DEMO, "--json")
    assert code == 1
    data = json.loads(out)
    assert data["counts"] == {"error": 4, "warn": 2, "info": 1}
    assert {f["code"] for f in data["findings"]} >= {"AF101", "AF202"}


def test_fail_on_and_ignore():
    code, out, _ = run(DEMO, "--ignore", "AF101,AF202,AF206,AF106")
    assert code == 0
    code, _, _ = run(DEMO, "--ignore", "AF101,AF202,AF206,AF106", "--fail-on", "warn")
    assert code == 1


def test_min_severity_hides_info():
    _, out, _ = run(DEMO, "--min-severity", "warn")
    assert "AF205" not in out


def test_single_workflow():
    code, out, _ = run(DEMO, "-w", os.path.join(DEMO, ".github", "workflows", "comment.yml"))
    assert code == 1 and "AF206" in out and "1 workflow," in out


def test_graph():
    code, out, _ = run("graph", DEMO)
    assert code == 0
    assert "down  publish  NOT ORDERED" in out
    assert "down  report  ok" in out
    code, out, _ = run("graph", DEMO, "--json")
    data = json.loads(out)
    names = [a["name"] for w in data for a in w["artifacts"]]
    assert "SHA256SUMS" in names


def test_no_workflows_dir(tmp_path):
    code, _, err = run(str(tmp_path))
    assert code == 2 and "no .github/workflows" in err


def test_rules_list():
    code, out, _ = run("rules")
    assert code == 0 and out.count("\n") == 16


def test_self_check_is_clean():
    code, out, _ = run(ROOT, "--fail-on", "warn")
    assert code == 0, out
