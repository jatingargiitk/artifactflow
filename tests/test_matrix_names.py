from artifactflow import matrix as mx
from artifactflow import names as nm
from artifactflow.expr import Unknown
from artifactflow.rules import _hidden_lines


def test_matrix_product_exclude():
    legs = mx.expand({"os": ["a", "b"], "v": [1, 2], "exclude": [{"os": "a", "v": 2}]})
    assert legs == [{"os": "a", "v": 1}, {"os": "b", "v": 1}, {"os": "b", "v": 2}]


def test_matrix_include_docs_example():
    # https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations
    m = {"fruit": ["apple", "pear"], "animal": ["cat", "dog"],
         "include": [{"color": "green"}, {"color": "pink", "animal": "cat"},
                     {"fruit": "apple", "shape": "circle"}, {"fruit": "banana"},
                     {"fruit": "banana", "animal": "cat"}]}
    legs = mx.expand(m)
    assert legs == [
        {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
        {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
        {"fruit": "pear", "animal": "cat", "color": "pink"},
        {"fruit": "pear", "animal": "dog", "color": "green"},
        {"fruit": "banana"},
        {"fruit": "banana", "animal": "cat"},
    ]


def test_matrix_only_include_and_expression():
    assert mx.expand({"include": [{"a": 1}, {"a": 2}]}) == [{"a": 1}, {"a": 2}]
    assert mx.expand("${{ fromJSON(needs.x.outputs.m) }}") is None
    assert mx.expand({"os": "${{ fromJSON(inputs.os) }}"}) is None
    assert mx.expand(None) == [{}]


def test_pattern_matching():
    assert nm.name_matches_pattern(("coverage-3.12",), "coverage-*")
    assert not nm.name_matches_pattern(("wheel-x86_64",), "wheels-*")
    assert nm.name_matches_pattern(("dist-linux",), "dist-{linux,mac}")
    assert nm.name_matches_pattern(("a1",), "a[0-9]")
    assert not nm.name_matches_pattern(("ab",), "a[0-9]")
    assert nm.name_matches_pattern(("x", Unknown("matrix.os")), "x-*")


def test_collision_keys():
    const = ("build-", Unknown("github.sha", False))
    assert nm.collision_key(const) == nm.collision_key(("build-", Unknown("github.sha", False)))
    assert nm.collision_key(("build-", Unknown("steps.x.outputs.y", True))) is None


def test_invalid_chars():
    assert nm.invalid_chars('a/b:c') == [":", "/"]
    assert nm.invalid_chars("ok-name_1.2") == []


def test_hidden_lines():
    assert _hidden_lines(".coverage") == [".coverage"]
    assert _hidden_lines("coverage/.last-run.json\nreport/") == ["coverage/.last-run.json"]
    assert _hidden_lines("site/.next/") == ["site/.next/"]
    assert _hidden_lines("dist/**/.*.map") == ["dist/**/.*.map"]
    # only the search root and below are visited, not the directories above it
    assert _hidden_lines("~/.npm/_logs/*.log") == []
    assert _hidden_lines("~/.npm/_logs") == []
    assert _hidden_lines("./dist") == []
    assert _hidden_lines("a/.hidden/b.txt") == []  # the file itself is the search root
    assert _hidden_lines("!dist/.env\ndist/") == []
