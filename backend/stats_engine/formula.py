"""지표 수식 DSL.

stat_definition.formula, league_constant_definition.formula, qualification_rule.rule_expression,
analyst.custom_metric.formula 가 모두 이 DSL 로 작성된다. 파이썬 eval 은 쓰지 않고,
ast 로 파싱한 뒤 화이트리스트 노드만 허용해 직접 평가한다.

문법
  - 숫자, 사칙연산(+ - * /), 거듭제곱(**), 단항 부호, 괄호
  - 비교(< <= > >= == !=)와 and / or / not  → 자격 조건, if_() 조건에 사용
  - 변수: 지표 코드. 점(.)으로 구분된 이름을 하나의 코드로 취급한다.
      bat.HR            → 자신의 지표
      PTS               → 접두어 없는 지표
      lg.FIP_C          → 리그 상수 (시즌별)
      team.AST          → (선수 수식에서) 소속 팀의 같은 레벨 지표
      ctx.TEAM_GAMES    → 평가 문맥 값 (팀 경기 수 등)
      cm.X / cf.X       → 커스텀 지표 / 분석가 정의 필드
  - 함수: min, max, abs, sqrt, safe_div(a, b[, default]), if_(cond, a, b), coalesce(a, b, ...)

NULL 규칙
  - 참조 값이 없으면(None) 결과도 None (coalesce 로 기본값 지정 가능)
  - 0 으로 나누면 None  → 타수 0 인 선수의 타율은 계산되지 않는다
"""
from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache

Number = float | int
Value = Number | bool | None

# 예약 네임스페이스 (지표 카테고리 코드로 사용할 수 없음)
RESERVED_NAMESPACES = frozenset({"lg", "ctx", "team", "opp", "cm", "cf"})

_CODE_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class FormulaError(ValueError):
    """수식 파싱/검증 오류."""


_BIN_OPS: dict[type[ast.operator], Callable[[Number, Number], Number]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Pow: operator.pow,
}

_CMP_OPS: dict[type[ast.cmpop], Callable[[Number, Number], bool]] = {
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
}

# 함수 이름 → 허용 인자 수 (min, max)
_FUNCTIONS: dict[str, tuple[int, int]] = {
    "min": (2, 16),
    "max": (2, 16),
    "abs": (1, 1),
    "sqrt": (1, 1),
    "safe_div": (2, 3),
    "if_": (3, 3),
    "coalesce": (1, 16),
}


def _dotted_name(node: ast.AST) -> str | None:
    """Name / Attribute 체인을 'a.b.C' 문자열로 변환. 다른 노드면 None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


@dataclass(frozen=True)
class Formula:
    """파싱·검증이 끝난 수식."""

    source: str
    tree: ast.Expression = field(repr=False, compare=False)
    variables: frozenset[str]

    def evaluate(self, values: Mapping[str, Value]) -> Value:
        """변수 값 매핑으로 수식을 평가한다. 누락 변수는 None 으로 취급."""
        return _Evaluator(values).visit(self.tree.body)

    def namespaced(self, namespace: str) -> frozenset[str]:
        """특정 네임스페이스(lg, team, ctx ...) 참조만 접두어를 뗀 코드로 반환."""
        prefix = namespace + "."
        return frozenset(v[len(prefix):] for v in self.variables if v.startswith(prefix))

    def local_variables(self) -> frozenset[str]:
        """예약 네임스페이스가 아닌 참조 (= 같은 엔티티의 지표 코드)."""
        return frozenset(v for v in self.variables if v.split(".", 1)[0] not in RESERVED_NAMESPACES)


@lru_cache(maxsize=4096)
def parse(source: str) -> Formula:
    """수식을 파싱하고 허용되지 않는 구문이 있으면 FormulaError 를 던진다."""
    if not source or not source.strip():
        raise FormulaError("빈 수식입니다")
    if len(source) > 2000:
        raise FormulaError("수식이 너무 깁니다 (최대 2000자)")
    # 수식은 단일 식이므로 줄바꿈·들여쓰기를 공백 하나로 정규화 (YAML 여러 줄 수식 허용)
    normalized = " ".join(source.split())
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"수식 구문 오류: {exc.msg} (위치 {exc.offset})") from exc
    variables: set[str] = set()
    _Validator(variables).visit(tree.body)
    return Formula(source=source, tree=tree, variables=frozenset(variables))


class _Validator(ast.NodeVisitor):
    """허용 노드 화이트리스트 검사 + 변수 수집."""

    def __init__(self, variables: set[str]) -> None:
        self.variables = variables

    def generic_visit(self, node: ast.AST) -> None:
        raise FormulaError(f"허용되지 않는 구문: {type(node).__name__}")

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError(f"숫자 상수만 허용됩니다: {node.value!r}")

    def _visit_variable(self, node: ast.AST) -> None:
        name = _dotted_name(node)
        if name is None:
            raise FormulaError("잘못된 변수 참조입니다")
        segments = name.split(".")
        if any(s.startswith("_") or not _CODE_SEGMENT.match(s) for s in segments):
            raise FormulaError(f"잘못된 변수 이름: {name}")
        if name in _FUNCTIONS:
            raise FormulaError(f"함수 이름을 변수로 쓸 수 없습니다: {name}")
        self.variables.add(name)

    visit_Name = _visit_variable
    visit_Attribute = _visit_variable

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if type(node.op) not in _BIN_OPS and not isinstance(node.op, ast.Div):
            raise FormulaError(f"허용되지 않는 연산자: {type(node.op).__name__}")
        self.visit(node.left)
        self.visit(node.right)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if not isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
            raise FormulaError(f"허용되지 않는 연산자: {type(node.op).__name__}")
        self.visit(node.operand)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        for v in node.values:
            self.visit(v)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op in node.ops:
            if type(op) not in _CMP_OPS:
                raise FormulaError(f"허용되지 않는 비교 연산자: {type(op).__name__}")
        self.visit(node.left)
        for c in node.comparators:
            self.visit(c)

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            raise FormulaError(f"허용되지 않는 함수: {ast.unparse(node.func)}")
        if node.keywords:
            raise FormulaError("함수에 키워드 인자를 쓸 수 없습니다")
        lo, hi = _FUNCTIONS[node.func.id]
        if not lo <= len(node.args) <= hi:
            raise FormulaError(f"{node.func.id}() 인자 수 오류: {len(node.args)}")
        for a in node.args:
            self.visit(a)


class _Evaluator:
    """검증된 AST 평가기 (None 전파)."""

    def __init__(self, values: Mapping[str, Value]) -> None:
        self.values = values

    def visit(self, node: ast.AST) -> Value:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.Name, ast.Attribute)):
            v = self.values.get(_dotted_name(node))  # type: ignore[arg-type]
            if isinstance(v, bool):
                return v
            return v if isinstance(v, (int, float)) else None
        if isinstance(node, ast.BinOp):
            left, right = self.visit(node.left), self.visit(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Div):
                return None if right == 0 else left / right
            try:
                return _BIN_OPS[type(node.op)](left, right)
            except (OverflowError, ZeroDivisionError):
                return None
        if isinstance(node, ast.UnaryOp):
            v = self.visit(node.operand)
            if isinstance(node.op, ast.Not):
                return None if v is None else not v
            if v is None:
                return None
            return -v if isinstance(node.op, ast.USub) else +v
        if isinstance(node, ast.BoolOp):
            vals = [self.visit(v) for v in node.values]
            if any(v is None for v in vals):
                return None
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.Compare):
            left = self.visit(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = self.visit(comp)
                if left is None or right is None:
                    return None
                if not _CMP_OPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            return self._call(node.func.id, node.args)  # type: ignore[attr-defined]
        raise FormulaError(f"평가할 수 없는 노드: {type(node).__name__}")  # 검증기에서 걸러짐

    def _call(self, name: str, args: list[ast.expr]) -> Value:
        if name == "if_":
            cond = self.visit(args[0])
            if cond is None:
                return None
            return self.visit(args[1] if cond else args[2])
        if name == "coalesce":
            for a in args:
                v = self.visit(a)
                if v is not None:
                    return v
            return None
        vals = [self.visit(a) for a in args]
        if name == "safe_div":
            num, den = vals[0], vals[1]
            default = vals[2] if len(vals) == 3 else None
            if num is None or den is None or den == 0:
                return default
            return num / den
        if any(v is None for v in vals):
            return None
        if name == "min":
            return min(vals)  # type: ignore[type-var]
        if name == "max":
            return max(vals)  # type: ignore[type-var]
        if name == "abs":
            return abs(vals[0])  # type: ignore[arg-type]
        if name == "sqrt":
            return math.sqrt(vals[0]) if vals[0] >= 0 else None  # type: ignore[operator]
        raise FormulaError(f"알 수 없는 함수: {name}")


def topological_order(dependencies: Mapping[str, frozenset[str] | set[str]]) -> list[str]:
    """{코드: 의존 코드 집합} 을 계산 순서로 정렬. 순환이 있으면 FormulaError.

    dependencies 에 키로 없는 의존 코드(원시 지표 등)는 이미 값이 있다고 보고 무시한다.
    """
    order: list[str] = []
    state: dict[str, int] = {}  # 0=방문 중, 1=완료

    def visit(code: str, path: list[str]) -> None:
        if state.get(code) == 1:
            return
        if state.get(code) == 0:
            cycle = path[path.index(code):] + [code]
            raise FormulaError("순환 참조: " + " → ".join(cycle))
        state[code] = 0
        for dep in sorted(dependencies[code]):
            if dep in dependencies:
                visit(dep, path + [code])
        state[code] = 1
        order.append(code)

    for code in sorted(dependencies):
        visit(code, [])
    return order
