"""程序 IR 与校验（`spec/02-ir.md`）。

程序 IR 只由命令批次改变；运行期状态另存（见 `engine.py`）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import vocab
from .diag import Diagnostic, ERROR, INFO, WARNING
from .lang import (ActionStmt, Add, CallStmt, Data, Del, DictLit, Expr, ListLit,
                   Listen, Lit, Move, On, Probe, Ref, SetStmt, Stmt, collect_refs,
                   is_static)

ROOT = "root"


@dataclass
class Node:
    id: str
    type: str
    parent: Optional[str] = None
    children: List[str] = field(default_factory=list)
    attrs: Dict[str, Expr] = field(default_factory=dict)
    as_name: str = ""
    line: int = 0


@dataclass
class DataSource:
    id: str
    persist: bool = False
    schema: Dict[str, tuple] = field(default_factory=dict)
    initial: Optional[Expr] = None
    line: int = 0


@dataclass
class Handler:
    target: str
    event: str
    bind: str = ""
    when: Optional[Expr] = None
    actions: List = field(default_factory=list)
    line: int = 0


@dataclass
class ListenDecl:
    target: str
    event: str
    line: int = 0


@dataclass
class Program:
    nodes: Dict[str, Node] = field(default_factory=dict)
    data: Dict[str, DataSource] = field(default_factory=dict)
    handlers: List[Handler] = field(default_factory=list)
    listens: List[ListenDecl] = field(default_factory=list)
    calls: List[CallStmt] = field(default_factory=list)
    probes: List[Probe] = field(default_factory=list)


def new_program() -> Program:
    program = Program()
    program.nodes[ROOT] = Node(id=ROOT, type="root")
    return program


# ------------------------------------------------------------------ 结构应用

def apply_stmt(program: Program, stmt: Stmt, diags: List[Diagnostic]) -> List[str]:
    """把一条语句应用到程序 IR。返回被改动的数据源地址（供级联使用）。"""
    changed: List[str] = []
    line = getattr(stmt, "line", 0)

    if isinstance(stmt, Add):
        return _apply_add(program, stmt, diags)

    if isinstance(stmt, SetStmt):
        node = program.nodes.get(stmt.target)
        if node is None:
            src = program.data.get(stmt.target)
            if src is None:
                diags.append(Diagnostic("TARGET_MISSING", ERROR,
                                        "目标 %s 不存在" % _hash(stmt.target), line=line))
                return changed
            return changed
        for key, expr in stmt.attrs.items():
            old = node.attrs.get(key)
            if old is not None and not is_static(old):
                diags.append(Diagnostic("BIND_OVERRIDDEN", INFO,
                                        "属性 %s 的绑定已被覆盖" % key, line=line))
            node.attrs[key] = expr
        return changed

    if isinstance(stmt, Del):
        node = program.nodes.get(stmt.target)
        if node is None:
            diags.append(Diagnostic("DEL_MISSING", WARNING,
                                    "要删除的 %s 不存在" % _hash(stmt.target), line=line))
            return changed
        _detach(program, node)
        return changed

    if isinstance(stmt, Move):
        node = program.nodes.get(stmt.target)
        new_parent = program.nodes.get(stmt.new_parent)
        if node is None or new_parent is None:
            diags.append(Diagnostic("TARGET_MISSING", ERROR, "移动的目标或新父不存在", line=line))
            return changed
        if stmt.anchor is not None:
            anchor = program.nodes.get(stmt.anchor[1])
            if anchor is None or anchor.parent != new_parent.id:
                diags.append(Diagnostic("ANCHOR_MISSING", ERROR,
                                        "锚点 %s 不存在于新父之下" % _hash(stmt.anchor[1]), line=line))
                return changed
        if new_parent.id == node.id or node.id in _ancestors(program, new_parent.id):
            diags.append(Diagnostic("MOVE_CYCLE", ERROR, "移动会造成成环", line=line))
            return changed
        _detach(program, node)
        node.parent = new_parent.id
        _insert(program, new_parent, node.id, stmt.anchor)
        return changed

    if isinstance(stmt, Data):
        if stmt.source in program.data or stmt.source in program.nodes:
            diags.append(Diagnostic("ID_DUP", ERROR,
                                    "地址 %s 已被占用" % _hash(stmt.source), line=line))
            return changed
        program.data[stmt.source] = DataSource(
            id=stmt.source, persist=stmt.persist, schema=stmt.schema,
            initial=stmt.initial, line=line)
        # 声明不是"变化"：装载期不得触发 `on … change` 级联。
        return changed

    if isinstance(stmt, On):
        program.handlers.append(Handler(
            target=stmt.target, event=stmt.event, bind=stmt.bind,
            when=stmt.when, actions=stmt.actions, line=line))
        return changed

    if isinstance(stmt, Listen):
        if stmt.target not in program.nodes and stmt.target not in program.data:
            diags.append(Diagnostic("TARGET_MISSING", ERROR,
                                    "订阅的目标 %s 不存在" % _hash(stmt.target), line=line))
            return changed
        program.listens.append(ListenDecl(target=stmt.target, event=stmt.event, line=line))
        return changed

    if isinstance(stmt, CallStmt):
        program.calls.append(stmt)
        return changed

    if isinstance(stmt, Probe):
        program.probes.append(stmt)
        return changed

    # 顶层动作（集合原语）由引擎执行：它们改的是状态，不是程序。
    if isinstance(stmt, ActionStmt):
        return changed

    return changed


def _hash(name: str) -> str:
    return "#" + name


def _apply_add(program: Program, stmt: Add, diags: List[Diagnostic]) -> List[str]:
    line = stmt.line
    parent = program.nodes.get(stmt.parent)
    if parent is None:
        diags.append(Diagnostic("PARENT_MISSING", ERROR,
                                "父 %s 不存在" % _hash(stmt.parent), line=line))
        return []
    if stmt.self_id in program.nodes or stmt.self_id in program.data:
        diags.append(Diagnostic("ID_DUP", ERROR,
                                "地址 %s 已被占用" % _hash(stmt.self_id), line=line))
        return []
    if stmt.anchor is not None:
        anchor = program.nodes.get(stmt.anchor[1])
        if anchor is None or anchor.parent != parent.id:
            diags.append(Diagnostic("ANCHOR_MISSING", ERROR,
                                    "锚点 %s 不存在" % _hash(stmt.anchor[1]), line=line))
            return []
    node = program.nodes.get(stmt.self_id)
    if node is None:
        node = Node(id=stmt.self_id, type=stmt.type, line=line)
    node.type = stmt.type
    node.parent = parent.id
    node.as_name = stmt.as_name
    node.attrs.update(stmt.attrs)
    program.nodes[stmt.self_id] = node
    if stmt.self_id not in parent.children:
        _insert(program, parent, stmt.self_id, stmt.anchor)
    return []


def _insert(program: Program, parent: Node, node_id: str, anchor):
    if anchor is None:
        parent.children.append(node_id)
        return
    kind, anchor_id = anchor
    if anchor_id in parent.children:
        idx = parent.children.index(anchor_id)
        parent.children.insert(idx if kind == "before" else idx + 1, node_id)
    else:
        parent.children.append(node_id)


def _detach(program: Program, node: Node) -> None:
    parent = program.nodes.get(node.parent) if node.parent else None
    if parent and node.id in parent.children:
        parent.children.remove(node.id)
    for child in list(node.children):
        _detach(program, program.nodes[child]) if child in program.nodes else None
    program.nodes.pop(node.id, None)


def _ancestors(program: Program, node_id: str) -> List[str]:
    out, cur = [], program.nodes.get(node_id)
    seen = set()
    while cur is not None and cur.parent is not None:
        if cur.parent in seen:
            break
        seen.add(cur.parent)
        out.append(cur.parent)
        cur = program.nodes.get(cur.parent)
    return out


def ancestors_of(program: Program, node_id: str) -> List[str]:
    return _ancestors(program, node_id)


def in_template(program: Program, node_id: str) -> bool:
    for aid in _ancestors(program, node_id):
        node = program.nodes.get(aid)
        if node is not None and node.type == "template":
            return True
    return False


def _unknown_attr(name: str, line: int) -> Diagnostic:
    guess = vocab.suggest_from(name, vocab.ALL_ATTRS)
    return Diagnostic("UNKNOWN_ATTR", WARNING,
                      "未知属性 %s" % name, line=line,
                      suggest=("是不是想写 %s？" % guess) if guess else "")


# ------------------------------------------------------------------ 校验

def validate(program: Program) -> List[Diagnostic]:
    diags: List[Diagnostic] = []
    used_templates = set()

    for node in program.nodes.values():
        if node.id == ROOT:
            continue
        if node.type not in vocab.NODE_TYPES:
            guess = vocab.suggest_from(node.type, vocab.NODE_TYPES)
            diags.append(Diagnostic("UNKNOWN_TYPE", WARNING,
                                    "未知节点类型 %s" % node.type, line=node.line,
                                    suggest=("是不是想写 %s？" % guess) if guess else ""))
        for name, expr in node.attrs.items():
            if name not in vocab.ALL_ATTRS:
                diags.append(_unknown_attr(name, node.line))
                continue
            only = vocab.TYPE_ONLY_ATTRS.get(name)
            if only and node.type not in only:
                diags.append(Diagnostic("ATTR_ON_TYPE", WARNING,
                                        "属性 %s 不适用于 %s" % (name, node.type),
                                        line=node.line))
            if name in vocab.REF_ATTRS:
                if not isinstance(expr, (Ref, Lit)):
                    diags.append(Diagnostic("REF_ATTR_EXPR", ERROR,
                                            "引用类属性 %s 不能写表达式" % name,
                                            line=node.line))
                elif isinstance(expr, Ref) and expr.field is None:
                    used_templates.add(expr.addr)
            if name == "icon" and isinstance(expr, Lit) and isinstance(expr.value, str):
                if expr.value not in vocab.CORE_ICONS:
                    guess = vocab.suggest_from(expr.value, vocab.CORE_ICONS)
                    diags.append(Diagnostic("UNKNOWN_ICON", WARNING,
                                            "未知图标 %s" % expr.value, line=node.line,
                                            suggest=("是不是想写 %s？" % guess) if guess else ""))
            if name == "states":
                diags += _check_states(expr, node)
            if name == "animate":
                diags += _check_animate(expr, node)

    for node in program.nodes.values():
        if node.type == "template" and node.id not in used_templates:
            diags.append(Diagnostic("TEMPLATE_UNUSED", WARNING,
                                    "模板 %s 未被任何列表使用" % _hash(node.id), line=node.line))

    diags += _check_handlers(program)
    diags += _check_interaction_coverage(program)
    diags += _check_controlled_writeback(program)
    diags += _check_bindings(program)
    return diags


MUTATING_ACTIONS = ("append", "remove", "remove_where", "update_where", "clear", "sort")


def _referenced_pairs(program: Program) -> set:
    """全程序里被引用过的 (地址, 属性) 对。"""
    pairs: set = set()

    def take(expr) -> None:
        if expr is None:
            return
        for ref in collect_refs(expr):
            pairs.add((ref.addr, ref.field))

    for node in program.nodes.values():
        for expr in node.attrs.values():
            take(expr)
    for handler in program.handlers:
        take(handler.when)
        for action in handler.actions:
            take(action.item)
            take(action.where)
            for expr in action.attrs.values():
                take(expr)
            for expr in action.params.values():
                take(expr)
    for call in program.calls:
        for expr in call.params.values():
            take(expr)
    return pairs


def _template_data_sources(program: Program, template: Node) -> set:
    """模板的行数据来自哪个数据源（由使用它的列表决定）。"""
    out = set()
    for node in program.nodes.values():
        ref = node.attrs.get("template")
        src = node.attrs.get("source")
        if isinstance(ref, Ref) and ref.addr == template.id \
                and isinstance(src, Ref) and src.addr in program.data:
            out.add(src.addr)
    return out


def _check_controlled_writeback(program: Program) -> List[Diagnostic]:
    """受控控件的值绑定了数据源，但它的 change 处理器没有回写那个数据源 → 警告。

    绑定值是**派生值**：不回写，用户改动就会被重算覆盖（界面弹回）。
    三条件同时成立才报，避免噪音：
      ① 值类属性是绑定、且能追溯到某个数据源；
      ② 该控件有 change 处理器；
      ③ 这些处理器一个都没写该数据源，且没有 call（能力可能写，无法静态判定）。
    """
    diags: List[Diagnostic] = []
    for node in program.nodes.values():
        for field in ("value", "selected"):
            expr = node.attrs.get(field)
            if expr is None or is_static(expr):
                continue
            sources = {r.addr for r in collect_refs(expr) if r.addr in program.data}
            template = _owning_template(program, node.id)
            if template is not None:
                sources |= _template_data_sources(program, template)
            if not sources:
                continue
            handlers = [h for h in program.handlers
                        if h.target == node.id and h.event == "change"]
            if not handlers:
                continue
            written, uncertain = set(), False
            for handler in handlers:
                for action in handler.actions:
                    if action.kind == "call":
                        uncertain = True
                    elif action.kind in MUTATING_ACTIONS:
                        written.add(action.target)
            if uncertain or (sources & written):
                continue
            diags.append(Diagnostic(
                "BOUND_VALUE_NOT_WRITTEN", WARNING,
                "#%s 的 %s 绑定了 %s，但它的 change 处理器没有写回该数据源："
                "用户的改动会被绑定重算覆盖（界面弹回）"
                % (node.id, field, "、".join("#" + s for s in sorted(sources))),
                line=node.line))
    return diags


def _owning_template(program: Program, node_id: str):
    for aid in [node_id] + _ancestors(program, node_id):
        node = program.nodes.get(aid)
        if node is not None and node.type == "template":
            return node
    return None


def _check_interaction_coverage(program: Program) -> List[Diagnostic]:
    """主交互事件没有任何处理器 → 警告。

    用户的动作不会有任何反应，而"没反应"必须被说出来，不能靠猜。
    """
    diags: List[Diagnostic] = []
    covered = {(h.target, h.event) for h in program.handlers}
    watched = {(l.target, l.event) for l in program.listens}
    read_pairs = _referenced_pairs(program)
    for node in program.nodes.values():
        event = vocab.PRIMARY_EVENT.get(node.type)
        if event is None:
            continue
        # 被订阅也算"有人管"：外部驱动者（LLM / 调度者）会响应，不属静默失败
        if (node.id, event) in covered or (node.id, event) in watched:
            continue
        # 值类控件的值若在别处被读到，交互是有去处的
        # （"提交时才读控件的值"是极常见且完全正当的写法）
        if event == "change" and ((node.id, "value") in read_pairs
                                  or (node.id, "selected") in read_pairs):
            continue
        diags.append(Diagnostic(
            "UNCOVERED_INTERACTION", WARNING,
            "%s #%s 的 %s 既没有处理器也没有被订阅：用户操作它不会有任何反应"
            % (node.type, node.id, event), line=node.line))
    return diags


def _check_states(expr: Expr, node: Node) -> List[Diagnostic]:
    diags: List[Diagnostic] = []
    if not isinstance(expr, DictLit):
        return diags
    for state, value in expr.fields.items():
        if not isinstance(value, DictLit):
            continue
        for key in value.fields:
            if key in vocab.STATE_APPEARANCE_LAYOUT:
                diags.append(Diagnostic("STATE_LAYOUT_ATTR", WARNING,
                                        "状态 %s 中不允许布局属性 %s" % (state, key),
                                        line=node.line))
            elif key not in vocab.STATE_APPEARANCE:
                diags.append(Diagnostic("STATE_UNKNOWN_ATTR", WARNING,
                                        "状态 %s 中的 %s 不是外观属性" % (state, key),
                                        line=node.line))
    return diags


def _check_animate(expr: Expr, node: Node) -> List[Diagnostic]:
    names: List[str] = []
    if isinstance(expr, Lit) and isinstance(expr.value, str):
        names = [expr.value]
    elif isinstance(expr, ListLit):
        names = [i.value for i in expr.items if isinstance(i, Lit) and isinstance(i.value, str)]
    return [Diagnostic("ANIMATE_UNKNOWN", WARNING,
                       "属性 %s 不可动画" % name, line=node.line)
            for name in names if name not in vocab.ANIMATABLE]


def _check_handlers(program: Program) -> List[Diagnostic]:
    diags: List[Diagnostic] = []
    for h in program.handlers:
        if h.event not in vocab.EVENTS:
            diags.append(Diagnostic("UNKNOWN_EVENT", WARNING,
                                    "未知事件 %s" % h.event, line=h.line))
        node = program.nodes.get(h.target)
        if node is not None and h.event in vocab.INTERACTION_EVENTS:
            disabled = node.attrs.get("disabled")
            if isinstance(disabled, Lit) and disabled.value is True:
                diags.append(Diagnostic("DISABLED_HANDLER", WARNING,
                                        "静态禁用的节点 #%s 绑定了 %s" % (h.target, h.event),
                                        line=h.line))
        if h.when is not None and isinstance(h.when, Lit) and not isinstance(h.when.value, bool):
            diags.append(Diagnostic("WHEN_NOT_BOOL", ERROR,
                                    "when 守卫必须是布尔", line=h.line))
    for listen in program.listens:
        if listen.event not in vocab.EVENTS:
            diags.append(Diagnostic("UNKNOWN_EVENT", WARNING,
                                    "未知事件 %s" % listen.event, line=listen.line))
    return diags


def _check_bindings(program: Program) -> List[Diagnostic]:
    diags: List[Diagnostic] = []
    graph: Dict[tuple, set] = {}

    def known(addr: str) -> bool:
        return addr in program.nodes or addr in program.data

    for node in program.nodes.values():
        for name, expr in node.attrs.items():
            if is_static(expr):
                continue
            deps = set()
            for ref in collect_refs(expr):
                if not known(ref.addr):
                    diags.append(Diagnostic("BIND_UNKNOWN_REF", ERROR,
                                            "引用 %s 不存在" % _hash(ref.addr),
                                            line=node.line))
                    continue
                if ref.field is not None and ref.addr in program.nodes:
                    target = program.nodes[ref.addr]
                    if ref.field not in target.attrs and ref.field not in vocab.STATE_FLAGS:
                        diags.append(Diagnostic("BIND_UNKNOWN_REF", ERROR,
                                                "节点 %s 没有属性 %s" % (_hash(ref.addr), ref.field),
                                                line=node.line))
                        continue
                    deps.add((ref.addr, ref.field))
            graph[(node.id, name)] = deps

    cycles = _find_cycle(graph)
    for key in sorted(cycles):
        diags.append(Diagnostic("BIND_CYCLE", ERROR,
                                "绑定成环：%s.%s" % (_hash(key[0]), key[1])))
    return diags


def _find_cycle(graph: Dict[tuple, set]) -> set:
    """返回参与环的键集合。"""
    color: Dict[tuple, int] = {}
    bad: set = set()

    def visit(node, stack):
        color[node] = 1
        for dep in graph.get(node, ()):
            if dep not in graph:
                continue
            state = color.get(dep, 0)
            if state == 1:
                bad.update(stack[stack.index(dep):] if dep in stack else [dep, node])
            elif state == 0:
                visit(dep, stack + [dep])
        color[node] = 2

    for node in list(graph):
        if color.get(node, 0) == 0:
            visit(node, [node])
    return bad
