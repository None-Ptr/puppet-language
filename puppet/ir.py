"""程序 IR 与校验（`spec/02-ir.md`）。

程序 IR 只由命令批次改变；运行期状态另存（见 `engine.py`）。
"""

from __future__ import annotations

import os
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
    if stmt.self_id in program.data:
        # 数据源地址不归 `add` / `upsert` 管：那是 `data` 的地盘。
        diags.append(Diagnostic("ID_DUP", ERROR,
                                "地址 %s 已被占用" % _hash(stmt.self_id), line=line))
        return []
    if stmt.self_id in program.nodes:
        if not stmt.is_upsert:
            diags.append(Diagnostic("ID_DUP", ERROR,
                                    "地址 %s 已被占用" % _hash(stmt.self_id), line=line))
            return []
        return _apply_upsert(program, stmt, diags)
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


def _apply_upsert(program: Program, stmt: Add, diags: List[Diagnostic]) -> List[str]:
    """`upsert` 的"存在"分支：**只更新给定属性**（规范 01 第 7.3 节）。

    未提及的属性与其绑定**原样保留**；命中的绑定被覆盖时给 `BIND_OVERRIDDEN`
    （与 `set` 同款——规范 06 第 4.4 节把 `upsert` 与 `set` 并列写进这条码）。
    类型 / 父 / 锚**不是**"给定属性"：改属性是 `upsert` 的事，改类型用 `del`+`add`、
    搬家用 `move`——声明与现状不一致时**可见**（`UPSERT_KEPT`），不静默吞掉。
    """
    node = program.nodes[stmt.self_id]
    line = stmt.line
    if stmt.type != node.type:
        diags.append(Diagnostic("UPSERT_KEPT", WARNING,
                                "upsert 只更新属性：#%s 保持 %s（声明的是 %s）"
                                % (node.id, node.type, stmt.type), line=line))
    if stmt.parent != node.parent:
        diags.append(Diagnostic("UPSERT_KEPT", WARNING,
                                "upsert 只更新属性：#%s 仍在 %s 之下（声明的是 %s）"
                                % (node.id, _hash(node.parent), _hash(stmt.parent)),
                                line=line))
    if stmt.anchor is not None:
        diags.append(Diagnostic("UPSERT_KEPT", WARNING,
                                "upsert 只更新属性：锚点 %s 被忽略（要调整位置请用 move）"
                                % _hash(stmt.anchor[1]), line=line))
    for key, expr in stmt.attrs.items():
        old = node.attrs.get(key)
        if old is not None and not is_static(old):
            diags.append(Diagnostic("BIND_OVERRIDDEN", INFO,
                                    "属性 %s 的绑定已被覆盖" % key, line=line))
        node.attrs[key] = expr
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

def validate(program: Program, assets_root: Optional[str] = None) -> List[Diagnostic]:
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
                elif isinstance(expr, Ref):
                    if expr.field is None:
                        used_templates.add(expr.addr)
                        diags += _check_ref_target(program, name, expr.addr, node.line)
                    else:
                        # 引用类属性要的是**地址**（数据源 / 模板，见 04 第 3 节）；
                        # 带字段的引用是**值引用**——指向"某个属性的值"，永远成不了
                        # 数据源。不报就是"引用断裂的静默无效"（程序能装载，但该属性
                        # 永不生效，且一声不响）。
                        diags.append(Diagnostic("REF_KIND", ERROR,
                                                "%s 必须指向地址，不能带字段（%s.%s）"
                                                % (name, _hash(expr.addr), expr.field),
                                                line=node.line))
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
    diags += _check_failure_coverage(program)
    diags += _check_controlled_writeback(program)
    diags += _check_bindings(program)
    diags += _check_assets(program, assets_root)
    return diags


REMOTE_PREFIXES = ("http://", "https://", "//")


def _check_assets(program: Program, assets_root: Optional[str]) -> List[Diagnostic]:
    """资源引用（`src`）：只允许本地资源；本地缺失必须可见。

    - **禁用远程地址**：它会引入网络依赖、隐私问题与一整套失败模式（超时 / 重试 /
      缓存 / 部分加载），而"可离线搭建"是真实需求。**禁用比"允许但不定义失败语义"诚实。**
    - **本地缺失**：这是"引用断裂"，与 `REF_MISSING` 同理——程序能装载，但那处资源
      永远显示不出来。存在性校验需要**资源根目录**（语言实现无从知道 app 在哪），
      故由调用方传入；不传则**跳过**该项校验（仅做禁用远程与形式检查）。
    """
    diags: List[Diagnostic] = []
    for node in program.nodes.values():
        expr = node.attrs.get("src")
        if not isinstance(expr, Lit) or not isinstance(expr.value, str):
            continue                    # 值类属性可写表达式：运行期才知道，渲染器负责可见降级
        value = expr.value.strip()
        if not value:
            continue
        if value.startswith(REMOTE_PREFIXES):
            diags.append(Diagnostic(
                "ASSET_REMOTE", ERROR,
                "资源地址不支持远程：%s（只允许资源目录内的相对路径）" % value,
                line=node.line))
            continue
        if os.path.isabs(value) or "://" in value:
            diags.append(Diagnostic(
                "ASSET_REMOTE", ERROR,
                "资源地址必须是资源目录内的相对路径：%s" % value, line=node.line))
            continue
        if assets_root:
            if not os.path.isfile(os.path.join(assets_root, value)):
                diags.append(Diagnostic(
                    "ASSET_MISSING", ERROR,
                    "资源 %s 不存在于资源目录" % value, line=node.line))
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


def _check_ref_target(program: Program, attr: str, addr: str, line: int) -> List[Diagnostic]:
    """引用类属性的目标必须存在、且类型正确。

    这是"目标不存在"家族里的**引用断裂**一档：程序能装载，但该属性永远不会生效
    ——一种**静默的无效**，必须被说出来。`source` / `options` 必须指向数据源，
    `template` 必须指向模板节点。
    """
    node = program.nodes.get(addr)
    is_data = addr in program.data
    if attr == "template":
        if node is None:
            return [Diagnostic("REF_MISSING", ERROR,
                               "模板 %s 不存在" % _hash(addr), line=line)]
        if node.type != "template":
            return [Diagnostic("REF_KIND", ERROR,
                               "template 应指向模板节点，而 %s 是 %s"
                               % (_hash(addr), node.type), line=line)]
        return []
    # `source` / `options`：必须指向数据源
    if is_data:
        return []
    if node is not None:
        return [Diagnostic("REF_KIND", ERROR,
                           "%s 应指向数据源，而 %s 是节点（%s）"
                           % (attr, _hash(addr), node.type), line=line)]
    return [Diagnostic("REF_MISSING", ERROR,
                       "%s 指向的数据源 %s 不存在" % (attr, _hash(addr)), line=line)]


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


def _failure_targets(program: Program) -> List[tuple]:
    """**处理器动作里**所有 `call` 的槽地址，带行号。

    只统计**用户触发**的调用（`on … : call …`）。顶层 `call` 是**驱动者触发**的
    动作（写在顶层即"由驱动者触发"），它的失败诊断进观察流、驱动者看得到，
    不构成"界面静默"，故不在本检查范围内。
    """
    found: List[tuple] = []
    for handler in program.handlers:
        for action in handler.actions:
            if action.kind == "call" and action.into:
                found.append((action.into, handler.line))
    return found


def _check_failure_coverage(program: Program) -> List[Diagnostic]:
    """能力的失败路径（`error` / `timeout`）没有任何处理器或订阅 → 警告。

    与 `UNCOVERED_INTERACTION` **同源**：那条管"用户点了没人接"，这条管"用户点了、
    但调用失败而没人接"——两者的判据相同：**用户操作了，而界面上不会发生任何可感知的变化**。

    为什么不能只靠 `SLOT_TIMEOUT` 一类诊断：那些进的是**观察流**，是驾驶舱的通道；
    造出来的 app 的**使用者看不到**。界面反馈必须在界面里。
    """
    diags: List[Diagnostic] = []
    covered = {(h.target, h.event) for h in program.handlers}
    watched = {(l.target, l.event) for l in program.listens}
    failure_events = ("error", "timeout")
    seen = set()
    for slot, line in _failure_targets(program):
        if not slot or slot in seen:
            continue
        seen.add(slot)
        # 被订阅也算"有人管"：外部驱动者会响应，不属静默失败
        if any((slot, ev) in covered or (slot, ev) in watched
               for ev in failure_events):
            continue
        diags.append(Diagnostic(
            "UNCOVERED_FAILURE", WARNING,
            "槽 %s 的调用失败（error / timeout）既没有处理器也没有被订阅："
            "失败在界面上不会有任何可感知的变化" % _hash(slot), line=line))
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
