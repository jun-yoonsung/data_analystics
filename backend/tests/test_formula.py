"""수식 DSL 테스트."""
import pytest

from stats_engine.formula import FormulaError, parse, topological_order


def test_variables_and_namespaces():
    f = parse("(bat.H + bat.BB) / bat.PA + lg.FIP_C * team.AST - ctx.TEAM_GAMES")
    assert f.variables == {"bat.H", "bat.BB", "bat.PA", "lg.FIP_C", "team.AST", "ctx.TEAM_GAMES"}
    assert f.local_variables() == {"bat.H", "bat.BB", "bat.PA"}
    assert f.namespaced("lg") == {"FIP_C"}
    assert f.namespaced("team") == {"AST"}


def test_batting_average():
    f = parse("bat.H / bat.AB")
    assert f.evaluate({"bat.H": 150, "bat.AB": 500}) == pytest.approx(0.3)


def test_division_by_zero_and_missing_values_return_none():
    f = parse("bat.H / bat.AB")
    assert f.evaluate({"bat.H": 0, "bat.AB": 0}) is None
    assert f.evaluate({"bat.H": 3}) is None


def test_multiline_formula_is_normalized():
    f = parse("(a.X\n + a.Y)\n/ a.Z")
    assert f.evaluate({"a.X": 1, "a.Y": 3, "a.Z": 2}) == 2


def test_functions():
    assert parse("safe_div(PTS, G, 0)").evaluate({"PTS": 10, "G": 0}) == 0
    assert parse("if_(SEC > 0, PTS, 0)").evaluate({"SEC": 0, "PTS": 5}) == 0
    assert parse("coalesce(XG, 0) + 1").evaluate({}) == 1
    assert parse("max(A, B, C)").evaluate({"A": 1, "B": 7, "C": 3}) == 7
    assert parse("sqrt(A)").evaluate({"A": -1}) is None


def test_comparison_for_qualification():
    f = parse("bat.PA >= 3.1 * ctx.TEAM_GAMES")
    assert f.evaluate({"bat.PA": 447, "ctx.TEAM_GAMES": 144}) is True
    assert f.evaluate({"bat.PA": 446, "ctx.TEAM_GAMES": 144}) is False
    assert f.evaluate({"ctx.TEAM_GAMES": 144}) is None


def test_fip_formula_matches_manual_calculation():
    # 180이닝(540아웃), HR 15, BB 40, HBP 5, SO 170, FIP 상수 3.10
    f = parse("3 * (13 * pit.HR + 3 * (pit.BB + pit.HBP) - 2 * pit.SO) / pit.OUTS + lg.FIP_C")
    v = f.evaluate({"pit.HR": 15, "pit.BB": 40, "pit.HBP": 5, "pit.SO": 170, "pit.OUTS": 540, "lg.FIP_C": 3.10})
    expected = (13 * 15 + 3 * 45 - 2 * 170) / 180 + 3.10
    assert v == pytest.approx(expected)


@pytest.mark.parametrize("source", [
    "__import__('os')",
    "bat.H.__class__",
    "open('x')",
    "[1, 2]",
    "a if b else c",
    "lambda: 1",
    "x // 2",
    "x % 2",
    "'text'",
    "True",
    "f(x=1)",
    "min(1)",
    "",
    "a +",
])
def test_rejects_unsafe_or_invalid(source):
    with pytest.raises(FormulaError):
        parse(source)


def test_topological_order_and_cycle():
    order = topological_order({"OPS": {"OBP", "SLG"}, "SLG": {"TB", "AB"}, "TB": {"H"}, "OBP": {"H"}})
    assert order.index("TB") < order.index("SLG") < order.index("OPS")
    assert order.index("OBP") < order.index("OPS")
    with pytest.raises(FormulaError, match="순환"):
        topological_order({"A": {"B"}, "B": {"C"}, "C": {"A"}})
