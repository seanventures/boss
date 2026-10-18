"""PromQL, evaluated by BOSS over its own scrapes, with Prometheus' semantics.

WHY BOSS EVALUATES PROMQL ITSELF
--------------------------------
An app alert rule is written in PromQL, the open standard Prometheus, Grafana, Thanos, Mimir,
VictoriaMetrics and Google Cloud all read (operator decision 2026-10-18). The rule is the same
text whether Prometheus evaluates it (when the stack BOSS deploys is reachable) or BOSS's own
monitor does (always: a laptop install often cannot reach the VM's Prometheus). Two evaluators
with two meanings would be two alerting systems; so this module implements the meaning, not an
approximation of it:

  * range selection is left-open, (t - range, t], as in Prometheus 3;
  * an instant selector looks back 5 minutes for the newest sample;
  * `rate` / `increase` extrapolate exactly as Prometheus' `extrapolatedRate` does (the 1.1x
    average-interval threshold, the counter zero-point limit, counter resets added back);
  * `histogram_quantile` is Prometheus' `bucketQuantile` (monotonic counts, +Inf required, a rank
    in the +Inf bucket answers the second-highest bound, NaN with no observations);
  * comparison operators FILTER (and keep the left value) unless `bool`;
  * vector/vector operators match one-to-one on the label set without `__name__`.

What it does not implement (`on`, `ignoring`, `group_left`, `offset`, subqueries, `@`) it refuses
with `PromQLUnsupported`, naming the construct, so a rule that only Prometheus can evaluate says
so instead of evaluating to something else. The agreement with real Prometheus is a test:
`test_alert_rules_mean_the_same_to_boss_and_prometheus` runs `promtool test rules` on the same
series and expects BOSS's values.

Pure: the series come in as a `Store`, the clock as an argument.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Union

#: Prometheus' default lookback for an instant vector selector.
LOOKBACK_MS = 5 * 60 * 1000


class PromQLError(ValueError):
    """The expression is not valid PromQL. The message says where."""


class PromQLUnsupported(PromQLError):
    """Valid PromQL that BOSS's evaluator does not implement; Prometheus can evaluate it."""


# --------------------------------------------------------------------------- #
# Series and values
# --------------------------------------------------------------------------- #
@dataclass
class Series:
    labels: dict[str, str]
    samples: list[tuple[int, float]] = field(default_factory=list)   # (t ms, value), ascending


class Store:
    """The series an expression may select, by metric name."""

    def __init__(self, series: Iterable[Series] = ()):
        self._by_name: dict[str, list[Series]] = {}
        self._index: dict[tuple, Series] = {}
        for s in series:
            self.add(s.labels, s.samples)

    def add(self, labels: dict[str, str], samples: Iterable[tuple[int, float]] = ()) -> Series:
        key = tuple(sorted(labels.items()))
        s = self._index.get(key)
        if s is None:
            s = Series(dict(labels), [])
            self._index[key] = s
            self._by_name.setdefault(labels.get("__name__", ""), []).append(s)
        s.samples.extend((int(t), float(v)) for t, v in samples)
        s.samples.sort(key=lambda x: x[0])
        return s

    def series(self, name: Optional[str]) -> list[Series]:
        if name is not None:
            return list(self._by_name.get(name, []))
        return [s for group in self._by_name.values() for s in group]

    def names(self) -> set[str]:
        return {n for n in self._by_name if n}


Labels = dict[str, str]


@dataclass
class Sample:
    labels: Labels
    value: float


@dataclass
class Scalar:
    value: float


@dataclass
class Vector:
    samples: list[Sample]


@dataclass
class Matrix:
    series: list[tuple[Labels, list[tuple[int, float]]]]


Value = Union[Scalar, Vector, Matrix]


def _sig(labels: Labels, *, drop_name: bool = True) -> tuple:
    return tuple(sorted((k, v) for k, v in labels.items() if not (drop_name and k == "__name__")))


def _no_name(labels: Labels) -> Labels:
    return {k: v for k, v in labels.items() if k != "__name__"}


# --------------------------------------------------------------------------- #
# Lexer
# --------------------------------------------------------------------------- #
_DURATION = re.compile(r"(?:\d+(?:ms|s|m|h|d|w|y))+")
_DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")
_UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000,
            "w": 604_800_000, "y": 31_536_000_000}
_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>\#[^\n]*)
  | (?P<duration>(?:\d+(?:ms|s|m|h|d|w|y))+(?![a-zA-Z0-9_.]))
  | (?P<number>0x[0-9a-fA-F]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)
  | (?P<ident>[a-zA-Z_:][a-zA-Z0-9_:]*)
  | (?P<op>=~|!~|!=|==|<=|>=|[-+*/%^(){}\[\],=<>@])
""", re.VERBOSE)


def parse_duration(text: str) -> int:
    """'5m' -> 300000 (ms). '1h30m' is one duration."""
    if not _DURATION.fullmatch(text or ""):
        raise PromQLError(f"{text!r} is not a duration (like 30s, 5m, 2h)")
    return sum(int(n) * _UNIT_MS[u] for n, u in _DURATION_PART.findall(text))


def format_duration(ms: int) -> str:
    """300000 -> '5m' (the largest unit that divides it exactly)."""
    ms = int(ms)
    if ms <= 0:
        return "0s"
    for unit in ("d", "h", "m", "s"):
        if ms % _UNIT_MS[unit] == 0:
            return f"{ms // _UNIT_MS[unit]}{unit}"
    return f"{ms}ms"


@dataclass
class _Tok:
    kind: str
    text: str
    pos: int


def _lex(src: str) -> list[_Tok]:
    out: list[_Tok] = []
    i = 0
    while i < len(src):
        m = _TOKEN.match(src, i)
        if not m:
            raise PromQLError(f"unexpected character {src[i]!r} at {i}")
        kind = m.lastgroup or ""
        if kind not in ("ws", "comment"):
            out.append(_Tok(kind, m.group(0), i))
        i = m.end()
    out.append(_Tok("eof", "", len(src)))
    return out


def _unquote(raw: str) -> str:
    if raw.startswith("`"):
        return raw[1:-1]
    body = raw[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "\\": "\\", '"': '"',
                                       "'": "'"}.get(m.group(1), m.group(1)), body)


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #
@dataclass
class Num:
    value: float


@dataclass
class Str:
    value: str


@dataclass
class Selector:
    name: Optional[str]
    matchers: list[tuple[str, str, str]]       # (label, op, value)
    range_ms: Optional[int] = None


@dataclass
class Call:
    func: str
    args: list[Any]


@dataclass
class Agg:
    op: str
    expr: Any
    by: Optional[list[str]] = None
    without: Optional[list[str]] = None
    param: Any = None


@dataclass
class Bin:
    op: str
    lhs: Any
    rhs: Any
    bool_: bool = False


@dataclass
class Neg:
    expr: Any


_AGGS = {"sum", "avg", "min", "max", "count", "topk", "bottomk", "quantile", "stddev",
         "stdvar", "count_values", "group"}
_SUPPORTED_AGGS = {"sum", "avg", "min", "max", "count", "group"}
_RANGE_FUNCS = {"rate", "increase", "avg_over_time", "min_over_time", "max_over_time",
                "sum_over_time", "count_over_time", "last_over_time"}
_FUNCS = _RANGE_FUNCS | {"histogram_quantile", "abs", "clamp_min", "clamp_max", "absent",
                         "scalar", "vector", "ceil", "floor", "round"}
_CMP = ("==", "!=", ">", "<", ">=", "<=")
# Prometheus precedence, lowest first.
_LEVELS = [("or",), ("and", "unless"), _CMP, ("+", "-"), ("*", "/", "%")]


class _Parser:
    def __init__(self, src: str):
        self.toks = _lex(src)
        self.i = 0

    def peek(self, k: int = 0) -> _Tok:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def take(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, text: str) -> _Tok:
        t = self.take()
        if t.text != text:
            raise PromQLError(f"expected {text!r} at {t.pos}, found {t.text or 'the end'!r}")
        return t

    def parse(self):
        node = self.level(0)
        if self.peek().kind != "eof":
            t = self.peek()
            raise PromQLError(f"unexpected {t.text!r} at {t.pos}")
        return node

    def level(self, n: int):
        if n == len(_LEVELS):
            return self.power()
        node = self.level(n + 1)
        while self.peek().text in _LEVELS[n]:
            op = self.take().text
            bool_ = False
            if op in _CMP and self.peek().text == "bool":
                self.take()
                bool_ = True
            if self.peek().text in ("on", "ignoring"):
                raise PromQLUnsupported(f"'{self.peek().text}(...)' vector matching")
            if self.peek().text in ("group_left", "group_right"):
                raise PromQLUnsupported(f"'{self.peek().text}' many-to-one matching")
            rhs = self.level(n + 1)
            node = Bin(op, node, rhs, bool_)
        return node

    def power(self):
        node = self.unary()
        if self.peek().text == "^":
            self.take()
            return Bin("^", node, self.power())     # right-associative
        return node

    def unary(self):
        if self.peek().text == "-":
            self.take()
            return Neg(self.unary())
        if self.peek().text == "+":
            self.take()
            return self.unary()
        return self.postfix(self.primary())

    def postfix(self, node):
        if self.peek().text == "[":
            self.take()
            t = self.take()
            if self.peek().text.startswith(":"):
                raise PromQLUnsupported("subqueries ([range:step])")
            ms = parse_duration(t.text)
            self.expect("]")
            if not isinstance(node, Selector) or node.range_ms is not None:
                raise PromQLUnsupported("a range on anything but a selector (a subquery)")
            node = Selector(node.name, node.matchers, ms)
        if self.peek().text == "offset":
            raise PromQLUnsupported("'offset'")
        if self.peek().text == "@":
            raise PromQLUnsupported("the '@' modifier")
        return node

    def primary(self):
        t = self.peek()
        if t.kind == "number":
            self.take()
            return Num(float(int(t.text, 16)) if t.text.lower().startswith("0x")
                       else float(t.text))
        if t.kind == "string":
            self.take()
            return Str(_unquote(t.text))
        if t.text == "(":
            self.take()
            node = self.level(0)
            self.expect(")")
            return node
        if t.text == "{":
            return self.selector(None)
        if t.kind == "ident":
            word = t.text
            low = word.lower()
            if low in ("inf", "nan") and self.peek(1).text != "(":
                self.take()
                return Num(math.inf if low == "inf" else math.nan)
            if low in _AGGS:
                return self.aggregation()
            if self.peek(1).text == "(":
                return self.call()
            self.take()
            if self.peek().text == "{":
                return self.selector(word)
            return Selector(word, [])
        raise PromQLError(f"unexpected {t.text or 'end of expression'!r} at {t.pos}")

    def selector(self, name: Optional[str]) -> Selector:
        self.expect("{")
        matchers: list[tuple[str, str, str]] = []
        while self.peek().text != "}":
            lab = self.take()
            if lab.kind != "ident":
                raise PromQLError(f"expected a label name at {lab.pos}")
            op = self.take()
            if op.text not in ("=", "!=", "=~", "!~"):
                raise PromQLError(f"expected =, !=, =~ or !~ after {lab.text} at {op.pos}")
            val = self.take()
            if val.kind != "string":
                raise PromQLError(f"label {lab.text} needs a quoted value at {val.pos}")
            matchers.append((lab.text, op.text, _unquote(val.text)))
            if self.peek().text == ",":
                self.take()
            elif self.peek().text != "}":
                raise PromQLError(f"expected ',' or '}}' at {self.peek().pos}")
        self.expect("}")
        if name is None and not any(op in ("=", "=~") and v not in ("", ".*")
                                    for _l, op, v in matchers):
            raise PromQLError("a selector needs a metric name or a non-empty matcher")
        return Selector(name, matchers)

    def labels_list(self) -> list[str]:
        self.expect("(")
        out = []
        while self.peek().text != ")":
            t = self.take()
            if t.kind != "ident":
                raise PromQLError(f"expected a label name at {t.pos}")
            out.append(t.text)
            if self.peek().text == ",":
                self.take()
        self.expect(")")
        return out

    def aggregation(self) -> Agg:
        op = self.take().text.lower()
        if op not in _SUPPORTED_AGGS:
            raise PromQLUnsupported(f"the '{op}' aggregation")
        by = without = None
        if self.peek().text in ("by", "without"):
            kw = self.take().text
            (by, without) = (self.labels_list(), None) if kw == "by" else (None,
                                                                          self.labels_list())
        self.expect("(")
        expr = self.level(0)
        self.expect(")")
        if self.peek().text in ("by", "without"):
            kw = self.take().text
            (by, without) = (self.labels_list(), None) if kw == "by" else (None,
                                                                          self.labels_list())
        return Agg(op, expr, by, without)

    def call(self) -> Call:
        name = self.take().text
        if name not in _FUNCS:
            raise PromQLUnsupported(f"the function {name}()")
        self.expect("(")
        args = []
        while self.peek().text != ")":
            args.append(self.level(0))
            if self.peek().text == ",":
                self.take()
            elif self.peek().text != ")":
                raise PromQLError(f"expected ',' or ')' at {self.peek().pos}")
        self.expect(")")
        return Call(name, args)


def parse(expr: str):
    """The expression's syntax tree. Raises PromQLError / PromQLUnsupported in words."""
    if not (expr or "").strip():
        raise PromQLError("the expression is empty")
    return _Parser(expr).parse()


def selected_metrics(expr: str) -> set[str]:
    """Every metric name the expression selects (to know which store can answer it)."""
    out: set[str] = set()

    def walk(n):
        if isinstance(n, Selector):
            if n.name:
                out.add(n.name)
            for lab, op, val in n.matchers:
                if lab == "__name__" and op == "=":
                    out.add(val)
        elif isinstance(n, Call):
            for a in n.args:
                walk(a)
        elif isinstance(n, Agg):
            walk(n.expr)
        elif isinstance(n, Bin):
            walk(n.lhs)
            walk(n.rhs)
        elif isinstance(n, Neg):
            walk(n.expr)
    walk(parse(expr))
    return out


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def _match(labels: Labels, matchers: list[tuple[str, str, str]]) -> bool:
    for lab, op, val in matchers:
        have = labels.get(lab, "")
        if op == "=" and have != val:
            return False
        if op == "!=" and have == val:
            return False
        if op in ("=~", "!~"):
            try:
                hit = re.fullmatch(val, have) is not None
            except re.error as exc:
                raise PromQLError(f"{lab}{op}{val!r}: {exc}") from None
            if hit != (op == "=~"):
                return False
    return True


def _select(store: Store, sel: Selector) -> list[Series]:
    name = sel.name
    matchers = list(sel.matchers)
    for lab, op, val in sel.matchers:
        if lab == "__name__" and op == "=":
            name = val
    return [s for s in store.series(name) if _match(s.labels, matchers)]


def _instant(store: Store, sel: Selector, t: int) -> Vector:
    out = []
    for s in _select(store, sel):
        best = None
        for ts, v in s.samples:
            if t - LOOKBACK_MS < ts <= t:
                best = v
            elif ts > t:
                break
        if best is not None:
            out.append(Sample(dict(s.labels), best))
    return Vector(out)


def _range(store: Store, sel: Selector, t: int) -> Matrix:
    out = []
    for s in _select(store, sel):
        pts = [(ts, v) for ts, v in s.samples if t - sel.range_ms < ts <= t]
        if pts:
            out.append((dict(s.labels), pts))
    return Matrix(out)


def extrapolated_rate(samples: list[tuple[int, float]], range_start: int, range_end: int,
                      *, is_counter: bool, is_rate: bool) -> Optional[float]:
    """Prometheus' extrapolatedRate (promql/functions.go), for rate()/increase()."""
    if len(samples) < 2:
        return None
    first_t, first_v = samples[0]
    last_t, last_v = samples[-1]
    result = last_v - first_v
    if is_counter:
        prev = first_v
        for _t, v in samples[1:]:
            if v < prev:
                result += prev
            prev = v
    duration_to_start = (first_t - range_start) / 1000.0
    duration_to_end = (range_end - last_t) / 1000.0
    sampled = (last_t - first_t) / 1000.0
    if sampled <= 0:
        return None
    average = sampled / (len(samples) - 1)
    threshold = average * 1.1
    extrapolate_to = sampled
    if duration_to_start >= threshold:
        duration_to_start = average / 2
    if is_counter and result > 0 and first_v >= 0:
        duration_to_zero = sampled * (first_v / result)
        if duration_to_zero < duration_to_start:
            duration_to_start = duration_to_zero
    extrapolate_to += duration_to_start
    if duration_to_end >= threshold:
        duration_to_end = average / 2
    extrapolate_to += duration_to_end
    factor = extrapolate_to / sampled
    if is_rate:
        factor /= (range_end - range_start) / 1000.0
    return result * factor


def bucket_quantile(q: float, buckets: list[tuple[float, float]]) -> float:
    """Prometheus' bucketQuantile over (upper bound, cumulative count) pairs."""
    if math.isnan(q):
        return math.nan
    if q < 0:
        return -math.inf
    if q > 1:
        return math.inf
    merged: dict[float, float] = {}
    for le, c in buckets:
        merged[le] = merged.get(le, 0.0) + c
    items = sorted(merged.items())
    if not items or items[-1][0] != math.inf:
        return math.nan
    fixed, running = [], -math.inf
    for le, c in items:                       # ensureMonotonic
        running = max(running, c)
        fixed.append((le, running))
    if len(fixed) < 2:
        return math.nan
    observations = fixed[-1][1]
    if observations == 0:
        return math.nan
    rank = q * observations
    b = next(i for i, (_le, c) in enumerate(fixed) if c >= rank)
    if b == len(fixed) - 1:
        return fixed[-2][0]
    if b == 0 and fixed[0][0] <= 0:
        return fixed[0][0]
    start, end, count = 0.0, fixed[b][0], fixed[b][1]
    if b > 0:
        start = fixed[b - 1][0]
        count -= fixed[b - 1][1]
        rank -= fixed[b - 1][1]
    if count == 0:
        return end
    return start + (end - start) * (rank / count)


def _arith(op: str, a: float, b: float) -> float:
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            if b == 0:
                return math.nan if a == 0 or math.isnan(a) else math.copysign(math.inf, a)
            return a / b
        if op == "%":
            return math.nan if b == 0 else math.fmod(a, b)
        if op == "^":
            return a ** b
    except (OverflowError, ValueError, ZeroDivisionError):
        return math.nan
    raise PromQLError(f"unknown operator {op}")


def _compare(op: str, a: float, b: float) -> bool:
    return {"==": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b,
            "<=": a <= b}[op]


class _Eval:
    def __init__(self, store: Store, t: int):
        self.store, self.t = store, int(t)

    def ev(self, n) -> Value:
        if isinstance(n, Num):
            return Scalar(n.value)
        if isinstance(n, Str):
            raise PromQLError("a string is not a value here")
        if isinstance(n, Selector):
            return _range(self.store, n, self.t) if n.range_ms else _instant(self.store, n,
                                                                             self.t)
        if isinstance(n, Neg):
            v = self.ev(n.expr)
            if isinstance(v, Scalar):
                return Scalar(-v.value)
            if isinstance(v, Vector):
                return Vector([Sample(_no_name(s.labels), -s.value) for s in v.samples])
            raise PromQLError("cannot negate a range")
        if isinstance(n, Call):
            return self.call(n)
        if isinstance(n, Agg):
            return self.agg(n)
        if isinstance(n, Bin):
            return self.binary(n)
        raise PromQLError(f"cannot evaluate {type(n).__name__}")

    # functions
    def _matrix(self, node, func: str) -> tuple[Selector, Matrix]:
        if not isinstance(node, Selector) or not node.range_ms:
            raise PromQLError(f"{func}() needs a range selector, like metric[5m]")
        return node, _range(self.store, node, self.t)

    def call(self, n: Call) -> Value:
        f, args = n.func, n.args
        if f in ("rate", "increase"):
            sel, m = self._matrix(args[0] if args else None, f)
            out = []
            for labels, pts in m.series:
                v = extrapolated_rate(pts, self.t - sel.range_ms, self.t, is_counter=True,
                                      is_rate=(f == "rate"))
                if v is not None:
                    out.append(Sample(_no_name(labels), v))
            return Vector(out)
        if f.endswith("_over_time"):
            _sel, m = self._matrix(args[0] if args else None, f)
            out = []
            for labels, pts in m.series:
                vals = [v for _t, v in pts]
                agg = {"avg_over_time": lambda x: sum(x) / len(x),
                       "min_over_time": min, "max_over_time": max, "sum_over_time": sum,
                       "count_over_time": lambda x: float(len(x)),
                       "last_over_time": lambda x: x[-1]}[f]
                keep = labels if f == "last_over_time" else _no_name(labels)
                out.append(Sample(keep, float(agg(vals))))
            return Vector(out)
        if f == "histogram_quantile":
            if len(args) != 2:
                raise PromQLError("histogram_quantile(q, buckets) takes two arguments")
            q = self.ev(args[0])
            if not isinstance(q, Scalar):
                raise PromQLError("histogram_quantile's first argument must be a number")
            v = self._vector(self.ev(args[1]), f)
            groups: dict[tuple, tuple[Labels, list[tuple[float, float]]]] = {}
            for s in v.samples:
                if "le" not in s.labels:
                    continue
                try:
                    raw = s.labels["le"]
                    le = math.inf if raw.lower() in ("+inf", "inf") else float(raw)
                except ValueError:
                    continue
                base = {k: val for k, val in s.labels.items() if k not in ("le", "__name__")}
                groups.setdefault(_sig(base), (base, []))[1].append((le, s.value))
            return Vector([Sample(base, bucket_quantile(q.value, bs))
                           for base, bs in groups.values()])
        if f in ("abs", "ceil", "floor"):
            v = self._vector(self.ev(args[0]), f)
            fn = {"abs": abs, "ceil": math.ceil, "floor": math.floor}[f]
            return Vector([Sample(_no_name(s.labels), float(fn(s.value))) for s in v.samples])
        if f == "round":
            v = self._vector(self.ev(args[0]), f)
            to = self.ev(args[1]) if len(args) > 1 else Scalar(1.0)
            if not isinstance(to, Scalar):
                raise PromQLError("round's second argument must be a number")
            inverse = 1.0 / to.value      # Prometheus' funcRound, to the bit
            return Vector([Sample(_no_name(s.labels),
                                  math.floor(s.value * inverse + 0.5) / inverse
                                  if math.isfinite(s.value) else s.value)
                           for s in v.samples])
        if f in ("clamp_min", "clamp_max"):
            v = self._vector(self.ev(args[0]), f)
            lim = self.ev(args[1])
            if not isinstance(lim, Scalar):
                raise PromQLError(f"{f}'s second argument must be a number")
            fn = max if f == "clamp_min" else min
            return Vector([Sample(_no_name(s.labels), fn(s.value, lim.value))
                           for s in v.samples])
        if f == "absent":
            v = self.ev(args[0])
            empty = (not v.samples) if isinstance(v, Vector) else (
                not v.series if isinstance(v, Matrix) else False)
            if not empty:
                return Vector([])
            labels: Labels = {}
            node = args[0]
            if isinstance(node, Selector):
                labels = {lab: val for lab, op, val in node.matchers
                          if op == "=" and lab != "__name__"}
            return Vector([Sample(labels, 1.0)])
        if f == "scalar":
            v = self._vector(self.ev(args[0]), f)
            return Scalar(v.samples[0].value if len(v.samples) == 1 else math.nan)
        if f == "vector":
            s = self.ev(args[0])
            if not isinstance(s, Scalar):
                raise PromQLError("vector() takes a number")
            return Vector([Sample({}, s.value)])
        raise PromQLUnsupported(f"the function {f}()")

    def _vector(self, v: Value, where: str) -> Vector:
        if not isinstance(v, Vector):
            raise PromQLError(f"{where} needs an instant vector")
        return v

    def agg(self, n: Agg) -> Value:
        v = self._vector(self.ev(n.expr), n.op)
        groups: dict[tuple, tuple[Labels, list[float]]] = {}
        for s in v.samples:
            if n.by is not None:
                key_labels = {k: s.labels[k] for k in n.by if k in s.labels}
            elif n.without is not None:
                key_labels = {k: val for k, val in s.labels.items()
                              if k not in n.without and k != "__name__"}
            else:
                key_labels = {}
            groups.setdefault(_sig(key_labels), (key_labels, []))[1].append(s.value)
        out = []
        for labels, vals in groups.values():
            if n.op == "sum":
                val = math.fsum(vals) if all(math.isfinite(x) for x in vals) else sum(vals)
            elif n.op == "avg":
                val = sum(vals) / len(vals)
            elif n.op == "min":
                real = [x for x in vals if not math.isnan(x)]
                val = min(real) if real else math.nan
            elif n.op == "max":
                real = [x for x in vals if not math.isnan(x)]
                val = max(real) if real else math.nan
            elif n.op == "count":
                val = float(len(vals))
            else:  # group
                val = 1.0
            out.append(Sample(labels, float(val)))
        return Vector(out)

    def binary(self, n: Bin) -> Value:
        lhs, rhs = self.ev(n.lhs), self.ev(n.rhs)
        op = n.op
        if isinstance(lhs, Matrix) or isinstance(rhs, Matrix):
            raise PromQLError(f"'{op}' cannot take a range; wrap it in a function such as rate()")
        if op in ("and", "or", "unless"):
            if not (isinstance(lhs, Vector) and isinstance(rhs, Vector)):
                raise PromQLError(f"'{op}' needs vectors on both sides")
            right = {_sig(s.labels) for s in rhs.samples}
            if op == "and":
                return Vector([s for s in lhs.samples if _sig(s.labels) in right])
            if op == "unless":
                return Vector([s for s in lhs.samples if _sig(s.labels) not in right])
            left = {_sig(s.labels) for s in lhs.samples}
            return Vector(list(lhs.samples) + [s for s in rhs.samples
                                               if _sig(s.labels) not in left])
        cmp = op in _CMP
        if isinstance(lhs, Scalar) and isinstance(rhs, Scalar):
            if cmp:
                if not n.bool_:
                    raise PromQLError("comparisons between numbers need 'bool'")
                return Scalar(1.0 if _compare(op, lhs.value, rhs.value) else 0.0)
            return Scalar(_arith(op, lhs.value, rhs.value))
        if isinstance(lhs, Vector) and isinstance(rhs, Scalar):
            return self._vs(lhs, rhs.value, op, n.bool_, scalar_left=False)
        if isinstance(lhs, Scalar) and isinstance(rhs, Vector):
            return self._vs(rhs, lhs.value, op, n.bool_, scalar_left=True)
        assert isinstance(lhs, Vector) and isinstance(rhs, Vector)
        index: dict[tuple, Sample] = {}
        for s in rhs.samples:
            k = _sig(s.labels)
            if k in index:
                raise PromQLError(f"'{op}': more than one series on the right match "
                                  f"{dict(k)}; aggregate first (many-to-many)")
            index[k] = s
        out, seen = [], set()
        for s in lhs.samples:
            k = _sig(s.labels)
            other = index.get(k)
            if other is None:
                continue
            if k in seen:
                raise PromQLError(f"'{op}': more than one series on the left match {dict(k)}")
            seen.add(k)
            if cmp:
                hit = _compare(op, s.value, other.value)
                if n.bool_:
                    out.append(Sample(_no_name(s.labels), 1.0 if hit else 0.0))
                elif hit:
                    out.append(Sample(dict(s.labels), s.value))
            else:
                out.append(Sample(_no_name(s.labels), _arith(op, s.value, other.value)))
        return Vector(out)

    def _vs(self, v: Vector, x: float, op: str, bool_: bool, *, scalar_left: bool) -> Vector:
        out = []
        for s in v.samples:
            a, b = (x, s.value) if scalar_left else (s.value, x)
            if op in _CMP:
                hit = _compare(op, a, b)
                if bool_:
                    out.append(Sample(_no_name(s.labels), 1.0 if hit else 0.0))
                elif hit:
                    out.append(Sample(dict(s.labels), s.value))
            else:
                out.append(Sample(_no_name(s.labels), _arith(op, a, b)))
        return Vector(out)


def evaluate(expr: Union[str, Any], store: Store, t_ms: int) -> Value:
    """The value of `expr` at `t_ms` over `store`."""
    node = parse(expr) if isinstance(expr, str) else expr
    return _Eval(store, t_ms).ev(node)


def instant_vector(expr: str, store: Store, t_ms: int) -> list[Sample]:
    """An alert rule's result: the elements that would be alerts. A scalar is refused, as
    Prometheus refuses it for an alerting rule."""
    v = evaluate(expr, store, t_ms)
    if isinstance(v, Vector):
        return list(v.samples)
    raise PromQLError("an alert expression must give a vector, not a single number")


__all__ = ["LOOKBACK_MS", "Matrix", "PromQLError", "PromQLUnsupported", "Sample", "Scalar",
           "Series", "Store", "Vector", "bucket_quantile", "evaluate", "extrapolated_rate",
           "format_duration", "instant_vector", "parse", "parse_duration", "selected_metrics"]
