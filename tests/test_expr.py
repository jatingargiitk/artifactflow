from artifactflow.expr import Unknown, evaluate, evaluate_if, evaluate_value, parse, render


def ctx(**roots):
    def resolve(name):
        return roots.get(name, Unknown(name, True))
    return resolve


def test_render_matrix_values():
    r = ctx(matrix={"os": "ubuntu-latest", "python": 3.1})
    assert render("dist-${{ matrix.os }}-${{ matrix.python }}", r) == ("dist-ubuntu-latest-3.1",)


def test_render_keeps_unknown_parts():
    r = ctx(matrix={"os": "linux"}, github=Unknown("github", False))
    parts = render("x-${{ matrix.os }}-${{ github.sha }}", r)
    assert parts[0] == "x-linux-"
    assert isinstance(parts[1], Unknown) and parts[1].varying is False and parts[1].desc == "github.sha"


def test_format_and_or():
    r = ctx(matrix={"shard": 2.0, "total": "3"})
    assert render("t${{ matrix.total > 1 && format('-shard-{0}', matrix.shard) || '' }}", r) == ("t-shard-2",)
    assert render("t${{ matrix.missing || 'none' }}", r) == ("tnone",)


def test_loose_comparison_rules():
    r = ctx()
    assert evaluate(parse("'ABC' == 'abc'"), r) is True
    assert evaluate(parse("'3' > 1"), r) is True
    assert evaluate(parse("null == 0"), r) is True
    assert evaluate(parse("true == 1"), r) is True


def test_functions():
    r = ctx(matrix={"list": ["a", "b"]})
    assert evaluate(parse("join(matrix.list, '+')"), r) == "a+b"
    assert evaluate(parse("contains(matrix.list, 'B')"), r) is True
    assert evaluate(parse("startsWith('Hello', 'he')"), r) is True
    assert evaluate(parse("fromJSON('[1,2]')"), r) == [1, 2]
    assert isinstance(evaluate(parse("hashFiles('x')"), r), Unknown)


def test_evaluate_if():
    r = ctx(matrix={"build": "release"}, runner={"os": "Linux"})
    assert evaluate_if("matrix.build == 'release'", r) is True
    assert evaluate_if("${{ matrix.build == 'debug' }}", r) is False
    assert evaluate_if("always()", r) is True
    assert evaluate_if("failure() && runner.os == 'macOS'", r) is False
    assert evaluate_if("failure()", r) is None
    assert evaluate_if("github.event_name == 'push'", r) is None


def test_evaluate_value_keeps_type():
    r = ctx(matrix={"n": 3.0})
    assert evaluate_value("${{ matrix.n }}", r) == 3.0
    assert evaluate_value(True, r) is True
    assert evaluate_value("a ${{ matrix.n }}", r) == "a 3"
