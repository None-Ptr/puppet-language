"""L3 表达式求值（`spec/01-grammar.md` 第 4 节）。

求值只读取当前值，不产生副作用；失败一律抛出 `EvalError`（不得静默）。
"""

from __future__ import annotations

from .lang import Bin, Call, DictLit, Expr, ListLit, Lit, Local, Ref, Un
from .vocab import BUILTINS


class EvalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class CollectionRef:
    """指向数据源本身的引用，供 `count` / `len` / `first` 等使用。"""

    __slots__ = ("source",)

    def __init__(self, source: str):
        self.source = source

    def __repr__(self):
        return "CollectionRef(%s)" % self.source


def type_name(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, CollectionRef):
        return "collection"
    return type(value).__name__


def as_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (str, int)):
        return str(value)
    raise EvalError("EXPR_TYPE", "无法转换为字符串：%s" % type_name(value))


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvalError("EXPR_TYPE", "需要数字，实际 %s" % type_name(value))
    return value


def _items(value, ctx=None):
    if isinstance(value, CollectionRef):
        return ctx.source_items(value.source)
    if isinstance(value, list):
        return list(value)
    raise EvalError("EXPR_TYPE", "需要列表或数据源，实际 %s" % type_name(value))


def _map(fn, args):
    return [fn(a) for a in args]


def evaluate(expr: Expr, ctx):
    if isinstance(expr, Lit):
        return expr.value
    if isinstance(expr, Ref):
        return ctx.read_ref(expr.addr, expr.field)
    if isinstance(expr, Local):
        return ctx.read_local(expr.name, expr.field)
    if isinstance(expr, Un):
        value = evaluate(expr.operand, ctx)
        if expr.op == "not":
            if not isinstance(value, bool):
                raise EvalError("EXPR_TYPE", "not 需要布尔，实际 %s" % type_name(value))
            return not value
        return -_num(value)
    if isinstance(expr, Bin):
        return _binary(expr, ctx)
    if isinstance(expr, ListLit):
        return [evaluate(i, ctx) for i in expr.items]
    if isinstance(expr, DictLit):
        return {k: evaluate(v, ctx) for k, v in expr.fields.items()}
    if isinstance(expr, Call):
        return _call(expr, ctx)
    raise EvalError("EXPR_TYPE", "无法求值的表达式")


def _binary(expr: Bin, ctx):
    if expr.op == "and":
        left = evaluate(expr.left, ctx)
        if not isinstance(left, bool):
            raise EvalError("EXPR_TYPE", "and 需要布尔")
        return evaluate(expr.right, ctx) if left else False
    if expr.op == "or":
        left = evaluate(expr.left, ctx)
        if not isinstance(left, bool):
            raise EvalError("EXPR_TYPE", "or 需要布尔")
        return True if left else evaluate(expr.right, ctx)

    left = evaluate(expr.left, ctx)
    right = evaluate(expr.right, ctx)
    op = expr.op

    if op == "+":
        if isinstance(left, str) or isinstance(right, str):
            if isinstance(left, (str, int, float, bool)) and isinstance(right, (str, int, float, bool)):
                return as_text(left) + as_text(right)
            raise EvalError("EXPR_TYPE", "加法不支持 %s + %s" % (type_name(left), type_name(right)))
        return _num(left) + _num(right)
    if op == "-":
        return _num(left) - _num(right)
    if op == "*":
        return _num(left) * _num(right)
    if op == "/":
        divisor = _num(right)
        if divisor == 0:
            raise EvalError("EXPR_DIV_ZERO", "除以零")
        return _num(left) / divisor
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op in ("<", "<=", ">", ">="):
        if isinstance(left, bool) or isinstance(right, bool) \
                or not isinstance(left, (int, float, str)) or not isinstance(right, (int, float, str)):
            raise EvalError("EXPR_TYPE", "比较不支持 %s 与 %s" % (type_name(left), type_name(right)))
        if type(left) is not type(right):
            raise EvalError("EXPR_TYPE", "比较的两侧类型不同：%s / %s"
                            % (type_name(left), type_name(right)))
        return {"<": left < right, "<=": left <= right,
                ">": left > right, ">=": left >= right}[op]
    raise EvalError("EXPR_TYPE", "未知运算符 %s" % op)


def _call(expr: Call, ctx):
    name = expr.name
    if name not in BUILTINS:
        raise EvalError("EXPR_UNKNOWN_FUNC", "未知函数 %s" % name)
    low, high = BUILTINS[name]
    if len(expr.args) < low or (high is not None and len(expr.args) > high):
        raise EvalError("EXPR_TYPE", "函数 %s 参数个数不符" % name)
    args = [evaluate(a, ctx) for a in expr.args]

    if name == "count":
        return len(_items(args[0], ctx))
    if name == "len":
        v = args[0]
        if isinstance(v, str) or isinstance(v, list):
            return len(v)
        if isinstance(v, dict):
            return len(v)
        return len(_items(v, ctx))
    if name == "upper":
        return as_text(args[0]).upper()
    if name == "lower":
        return as_text(args[0]).lower()
    if name == "trim":
        return as_text(args[0]).strip()
    if name == "contains":
        if isinstance(args[0], (list, str, dict)) and not isinstance(args[0], CollectionRef):
            return args[1] in args[0]
        return args[1] in _items(args[0], ctx)
    if name == "num":
        v = args[0]
        if isinstance(v, bool):
            raise EvalError("EXPR_TYPE", "布尔不能转数字")
        if isinstance(v, (int, float)):
            return v
        if isinstance(v, str):
            try:
                return float(v) if "." in v else int(v)
            except ValueError as ex:
                raise EvalError("EXPR_TYPE", "无法转换为数字：%r" % v) from ex
        raise EvalError("EXPR_TYPE", "无法转换为数字：%s" % type_name(v))
    if name == "str":
        return as_text(args[0])
    if name == "abs":
        return abs(_num(args[0]))
    if name == "min":
        return min(_num(args[0]), _num(args[1]))
    if name == "max":
        return max(_num(args[0]), _num(args[1]))
    if name == "sum":
        return sum(_num(x) for x in _items(args[0], ctx))
    if name == "round":
        digits = int(_num(args[1])) if len(args) > 1 else 0
        return round(_num(args[0]), digits)
    if name == "join":
        return as_text(args[1]).join(as_text(x) for x in _items(args[0], ctx))
    if name == "split":
        return as_text(args[0]).split(as_text(args[1]))
    if name == "at":
        items = _items(args[0], ctx)
        idx = int(_num(args[1]))
        if idx < 0 or idx >= len(items):
            raise EvalError("EXPR_TYPE", "下标越界：%d" % idx)
        return items[idx]
    if name == "first":
        items = _items(args[0], ctx)
        if not items:
            raise EvalError("EXPR_TYPE", "空列表没有首项")
        return items[0]
    if name == "last":
        items = _items(args[0], ctx)
        if not items:
            raise EvalError("EXPR_TYPE", "空列表没有末项")
        return items[-1]
    if name == "slice":
        items = _items(args[0], ctx)
        return items[int(_num(args[1])):int(_num(args[2]))]
    if name == "keys":
        if not isinstance(args[0], dict):
            raise EvalError("EXPR_TYPE", "keys 需要字典")
        return list(args[0].keys())
    if name == "values":
        if not isinstance(args[0], dict):
            raise EvalError("EXPR_TYPE", "values 需要字典")
        return list(args[0].values())
    if name == "has":
        if not isinstance(args[0], dict):
            raise EvalError("EXPR_TYPE", "has 需要字典")
        return as_text(args[1]) in args[0]
    if name == "fmt":
        template = as_text(args[0])
        out = template
        for value in args[1:]:
            out = out.replace("{}", as_text(value), 1)
        return out
    raise EvalError("EXPR_UNKNOWN_FUNC", "未实现的函数 %s" % name)
