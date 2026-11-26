import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from artifactflow.cli import build_graphs  # noqa: E402
from artifactflow.rules import check  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """repo({"ci.yml": "...yaml..."}, actions={"x": "...action.yml..."}) -> findings."""

    def make(workflows, actions=None):
        wf_dir = tmp_path / ".github" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)
        for name, text in workflows.items():
            (wf_dir / name).write_text(textwrap.dedent(text))
        for path, text in (actions or {}).items():
            d = tmp_path / path
            d.mkdir(parents=True, exist_ok=True)
            (d / "action.yml").write_text(textwrap.dedent(text))
        graphs, reusable = build_graphs(str(tmp_path))
        return check(graphs, reusable)

    make.root = tmp_path
    return make


def codes(findings, severity=None):
    return sorted(f.code for f in findings if severity is None or f.severity == severity)
