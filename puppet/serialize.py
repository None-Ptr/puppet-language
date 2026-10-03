"""程序 IR → `.puppet` 源文本（`spec/02-ir.md` 第 1 节：批次结束时一次性写回）。

本模块是 `apply_stmt`（文本 → IR）的**反向操作**，两者共同保证**往返一致**：

    parse → apply_stmt → program_source → parse 得到等价 IR

三条约束，缺一即违规：

- **只读程序 IR**：反向序列化**绝不**触碰运行期状态（不变式：引擎不用运行期内容改写程序）。
- **父先于子**：`add` 要求父已存在，故节点按深度优先打印，顺序由 `children` 决定。
- **信息会丢，且这是允许的**：IR 不保留语句历史（原语句是 `add` 还是 `upsert`、锚点位置、
  注释、行号），所以写回是"整份重排"而非"文本补丁"。因此**注释与手工排版必然不保留**，
  diff 应比对**语义**而非文本。这是既定取舍，不是缺陷。
"""

from __future__ import annotations

from typing import List

from .ir import ROOT, DataSource, Handler, Program
from .lang import Action, CallStmt, Expr, Probe, expr_source


def _attrs(attrs) -> str:
    """属性表 → `k=v k=v`。规范规定属性顺序无意义，故保持插入序即可。"""
    return " ".join("%s=%s" % (k, expr_source(v)) for k, v in attrs.items())


def _addr(name: str) -> str:
    """裸地址 → 源文本地址。`root` 是特殊地址，同样带 `#`。"""
    return "#" + name


# ------------------------------------------------------------------ 动作

def action_source(act: Action) -> str:
    """`spec/01-grammar.md` 第 7.7 节的八种动作写法。"""
    kind = act.kind
    if kind == "set":
        tail = _attrs(act.attrs)
        return "set %s%s" % (_addr(act.target), (" " + tail) if tail else "")
    if kind in ("append", "remove"):
        return "%s %s item=%s" % (kind, _addr(act.target), expr_source(act.item))
    if kind == "remove_where":
        return "remove_where %s as %s where %s" % (
            _addr(act.target), act.bind, expr_source(act.where))
    if kind == "update_where":
        return "update_where %s as %s set %s where %s" % (
            _addr(act.target), act.bind, _attrs(act.attrs), expr_source(act.where))
    if kind == "clear":
        return "clear %s" % _addr(act.target)
    if kind == "sort":
        return "sort %s by %s" % (_addr(act.target), act.by)
    if kind == "call":
        params = ", ".join("%s: %s" % (k, expr_source(v))
                           for k, v in act.params.items())
        return "call %s with {%s} into %s" % (act.func, params, _addr(act.into))
    return ""


# ------------------------------------------------------------------ 各类语句

def _node_line(program: Program, parent_id: str, node_id: str) -> str:
    node = program.nodes[node_id]
    head = _addr(parent_id)
    if node.type == "template":
        # `add <父> template <地址> as <绑定名>`（第 8 节）
        parts = ["add", head, "template", _addr(node_id)]
        if node.as_name:
            parts += ["as", node.as_name]
        return " ".join(parts)
    parts = ["add", head, node.type, _addr(node_id)]
    if node.as_name:
        parts += ["as", node.as_name]
    body = _attrs(node.attrs)
    if body:
        parts.append(body)
    return " ".join(parts)


def _walk(program: Program, node_id: str, out: List[str]) -> None:
    """深度优先：父先于子（`add` 的前置条件）。顺序即 `children` 顺序。"""
    for child_id in program.nodes[node_id].children:
        out.append(_node_line(program, node_id, child_id))
        _walk(program, child_id, out)


def _data_source(ds: DataSource) -> str:
    parts = ["data", _addr(ds.id)]
    if ds.persist:
        parts.append("persist=true")
    parts.append("=")
    parts.append(expr_source(ds.initial) if ds.initial is not None else "[]")
    if ds.schema:
        fields = []
        for name, spec in ds.schema.items():
            typ, default = spec if isinstance(spec, tuple) else (spec, None)
            item = "%s: %s" % (name, typ)
            if default is not None:
                item += " = " + expr_source(default)
            fields.append(item)
        parts.append("of {" + ", ".join(fields) + "}")
    return " ".join(parts)


def _handler_source(h: Handler) -> str:
    parts = ["on", _addr(h.target), h.event]
    if h.bind:
        parts += ["as", h.bind]
    if h.when is not None:
        parts += ["when", expr_source(h.when)]
    head = " ".join(parts) + ":"
    actions = [action_source(a) for a in h.actions]
    actions = [a for a in actions if a]
    return head + (" " + "; ".join(actions) if actions else "")


def _call_source(c: CallStmt) -> str:
    params = ", ".join("%s: %s" % (k, expr_source(v)) for k, v in c.params.items())
    return "call %s with {%s} into %s" % (c.func, params, _addr(c.into))


def _probe_source(p: Probe) -> str:
    return ("%s %s" % (p.verb, _addr(p.target))) if p.target else p.verb


# ------------------------------------------------------------------ 入口

def program_lines(program: Program) -> List[str]:
    """程序 IR → 源文本行（可直接写文件，逐行）。

    打印顺序：节点树 → 数据源 → 处理器 → 订阅 → 调用 → 探针。
    前两者有前置依赖（`add` 的父、`call` 的 `into` 数据源），故节点在前、
    数据源紧随；处理器与订阅最后，因为它们引用节点与数据源。
    """
    out: List[str] = []
    _walk(program, ROOT, out)
    for ds in program.data.values():
        out.append(_data_source(ds))
    for h in program.handlers:
        out.append(_handler_source(h))
    for decl in program.listens:
        out.append("listen %s %s" % (_addr(decl.target), decl.event))
    for call in program.calls:
        out.append(_call_source(call))
    for probe in program.probes:
        out.append(_probe_source(probe))
    return out


def program_source(program: Program) -> str:
    """程序 IR → 源文本（单字符串，行尾 `\\n`）。"""
    lines = program_lines(program)
    return "".join(line + "\n" for line in lines)
