"""执行语义（`spec/03-semantics.md`）。

引擎只写状态；程序 IR 只由命令批次改变。任何"降级 / 丢弃 / 失败 / 上限"都产生诊断。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

from .capabilities import load_capability_module, type_matches
from .diag import Diagnostic, ERROR, INFO, WARNING
from .eval import CollectionRef, EvalError, evaluate
from .ir import (ROOT, Program, ancestors_of, apply_stmt, in_template, new_program,
                 validate)
from .lang import (ActionStmt, Add, CallStmt, Del, Expr, Lit, Move, On, Probe,
                   Ref, SetStmt, Stmt, is_static, parse_program)
from .serialize import program_lines as _program_lines
from . import vocab

STATE_FLAGS = vocab.STATE_FLAGS
FLAG_DEFAULTS = {"visible": True, "disabled": False, "hover": False,
                 "focus": False, "pressed": False, "error": False}
SERIALIZABLE = (str, int, float, bool, list, dict)


class Store:
    """状态文件：原子写。"""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def write(self, payload: dict) -> None:
        with self.lock:
            # 状态目录可能被外部清掉（宿主"重置状态"就是删掉它，人或脚本也可能删）。
            # 状态是可丢、可从声明重建的，所以"目录没了"不该让引擎崩在持久化上。
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.path)

    def read(self) -> dict:
        with self.lock:
            if not os.path.exists(self.path):
                return {}
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:  # noqa: BLE001 - 损坏状态按缺失处理（并会被记录）
                return {}


class _Ctx:
    """求值上下文：读引用、读局部绑定。

    绑定是**可叠加的**，不是替换关系：处理器可以有 `as t`，其动作里又能
    `update_where … as r`；两个绑定在同一个表达式里必须同时可见。
    """

    def __init__(self, engine: "Engine", bindings: Optional[Dict[str, dict]] = None):
        self.engine = engine
        self.bindings: Dict[str, dict] = dict(bindings or {})
        self.cache: Dict[tuple, Any] = {}
        self.visiting: set = set()

    def child(self, name: str, row) -> "_Ctx":
        """派生一个作用域：在既有绑定之上再加一层。"""
        inner = _Ctx(self.engine, self.bindings)
        if name:
            inner.bindings[name] = row
        return inner

    def read_ref(self, addr: str, field: Optional[str]):
        program = self.engine.program
        if addr in program.data:
            if field is None:
                return CollectionRef(addr)
            if field == "status":
                rec = self.engine.slots.get(addr)
                return rec["status"] if rec else "idle"
            if field == "value":
                rec = self.engine.slots.get(addr)
                return rec.get("value") if rec else None
            items = self.engine.items.get(addr) or []
            if len(items) == 1 and field in items[0]:
                return items[0][field]
            raise EvalError("BIND_EVAL", "数据源 #%s 取不到字段 %s" % (addr, field))
        node = program.nodes.get(addr)
        if node is None:
            raise EvalError("BIND_EVAL", "未知地址 #%s" % addr)
        if field is None:
            raise EvalError("BIND_EVAL", "节点 #%s 不能作为值使用" % addr)
        if field in STATE_FLAGS:
            if field in node.attrs and not self.engine.is_flag_static(addr, field):
                return self.value_of(addr, field)
            return self.engine.flag(addr, field)
        if field not in node.attrs:
            default = vocab.default_value(node.type, field)
            if default is not vocab._MISSING:
                return default
        return self.value_of(addr, field)

    def source_items(self, source: str) -> List[dict]:
        """数据源的行集合（供 count / len / first 等使用）。"""
        if source not in self.engine.program.data:
            raise EvalError("BIND_EVAL", "未知数据源 #%s" % source)
        return list(self.engine.items.get(source, []))

    def read_local(self, name: str, field: Optional[str]):
        row = self.bindings.get(name)
        if row is None:
            raise EvalError("BIND_EVAL", "当前上下文没有绑定 %s" % name)
        if field is None:
            return dict(row)
        if field not in row:
            raise EvalError("BIND_EVAL", "绑定的记录没有字段 %s" % field)
        return row[field]

    def value_of(self, nid: str, attr: str):
        key = (nid, attr)
        if key in self.cache:
            return self.cache[key]
        if key in self.visiting:
            raise EvalError("BIND_CYCLE", "绑定成环：#%s.%s" % (nid, attr))
        node = self.engine.program.nodes.get(nid)
        expr = node.attrs.get(attr) if node else None
        if expr is None:
            raise EvalError("BIND_EVAL", "节点 #%s 没有属性 %s" % (nid, attr))
        self.visiting.add(key)
        try:
            value = evaluate(expr, self)
        finally:
            self.visiting.discard(key)
        self.cache[key] = value
        return value


class Engine:
    CASCADE_LIMIT = 32

    def __init__(self, workdir: Optional[str] = None, rendering: Optional[dict] = None,
                 store: Optional[object] = None):
        self.workdir = workdir or tempfile.mkdtemp(prefix="puppet-state-")
        # `store` 是宿主注入点：状态文件不必是本机文件（PuppetHub 把它接到 storage
        # 插件上，让落盘走同一条通道）。缺省仍用自带 Store——引擎单独用时
        # 不该知道宿主的存在。duck interface：read() -> dict · write(payload)。
        self.store = store if store is not None else Store(
            os.path.join(self.workdir, "state.json"))
        self.lock = threading.RLock()
        self.program = new_program()
        self.items: Dict[str, List[dict]] = {}
        self.slots: Dict[str, dict] = {}
        self.flags: Dict[str, Dict[str, bool]] = {}
        self.capabilities: Dict[str, dict] = {}
        self.limits: Dict[str, Any] = {"callTimeoutMs": 5000}
        # 资源根目录（可选）：提供时校验 `src` 指向的文件确实存在。
        self.assets_dir: Optional[str] = None
        self.pending: List[Diagnostic] = []
        self.events: List[dict] = []
        self.probes: List[dict] = []
        self.subscriptions: set = set()
        self.pending_limit = 256
        self.dropped = 0
        self.revision = 0
        self._baseline: set = set()
        self._seq = 0
        # 已被"生命周期边界"作废的槽序号：它们的迟到结果**不得**再产生诊断（见 `settle`）。
        self._cancelled: set = set()
        # 几何测试替身（conformance 用）：地址 → 矩形。为空表示"本渲染器无几何"。
        self.render_geometry: Dict[str, dict] = {}
        # 能力声明（渲染契约第 1 节）：渲染器支持哪些词汇。None = 全部标准。
        self.rendering = self._merge_rendering(rendering)
        # 构造时的声明即**基线**：每个批次都从它出发，替身只在本次生效。
        self._rendering_base = dict(self.rendering)

    # ------------------------------------------------------------ 能力声明

    @staticmethod
    def _merge_rendering(rendering) -> Dict[str, Any]:
        """能力声明：词汇能力为列表（`None` = 支持全部标准词汇）。

        测试替身可经 `load.rendering` 覆盖——实现必须**按声明行事**：
        未声明支持的词汇被使用时必须产生可见降级（声明即 oracle 的实现侧）。
        """
        base: Dict[str, Any] = {"controls": None, "attributes": None,
                                "animations": None, "icons": None}
        if isinstance(rendering, dict):
            for key in base:
                if key in rendering:
                    base[key] = rendering[key]
        return base

    def _rendering_degradations(self) -> List[Diagnostic]:
        """按能力声明扫描程序词汇：未声明支持的必须可见降级，禁止静默。"""
        declared = self.rendering
        controls = declared.get("controls")
        attributes = declared.get("attributes")
        animations = declared.get("animations")
        icons = declared.get("icons")
        out: List[Diagnostic] = []
        for node in self.program.nodes.values():
            # `root` 是**结构锚点**，不是词汇（`validate` 同样跳过它）。把非词汇节点算成
            # "未声明支持"，会在**每一个**程序上产生一条假的降级诊断——诊断必须可信，
            # 一条永远存在的假警告会把整条观察流训练成无人看。
            if node.type not in vocab.NODE_TYPES:
                continue
            if controls is not None and node.type not in controls:
                out.append(Diagnostic(
                    "DEGRADED_FEATURE", INFO,
                    "渲染器未声明支持控件 %s，已降级呈现" % node.type,
                    feature="control:" + node.type))
            for name, expr in node.attrs.items():
                if (attributes is not None and name in vocab.ALL_ATTRS
                        and name not in attributes):
                    out.append(Diagnostic(
                        "DEGRADED_FEATURE", INFO,
                        "渲染器未声明支持属性 %s，已降级" % name,
                        feature="attr:" + name))
                if (icons is not None and name == "icon"
                        and isinstance(expr, Lit) and isinstance(expr.value, str)
                        and expr.value not in icons):
                    out.append(Diagnostic(
                        "DEGRADED_FEATURE", INFO,
                        "图标 %s 不在能力声明内，已降级" % expr.value,
                        feature="icon:" + expr.value))
                if animations is not None and name == "animate":
                    for item in self._static_names(expr):
                        if item not in animations:
                            out.append(Diagnostic(
                                "DEGRADED_FEATURE", INFO,
                                "属性 %s 不在可动画声明内，改为立即生效" % item,
                                feature="animation:" + item))
        return out

    @staticmethod
    def _static_names(expr) -> List[str]:
        """`animate` 的静态值：字符串或字符串列表；动态表达式不猜。"""
        if not isinstance(expr, Lit):
            return []
        value = expr.value
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [v for v in value if isinstance(v, str)]
        return []

    # ------------------------------------------------------------ 装载与批次

    def load(self, lines, capabilities=None, limits=None, seed_state=None,
             render_geometry=None, rendering=None,
             capability_modules=None, assets_dir=None) -> List[Diagnostic]:
        with self.lock:
            inflight = self._inflight_slots()
            self.program = new_program()
            self.items = {}
            self.slots = {}
            self.flags = {}
            self.pending = []
            # 装载 = 新的生命周期：观察缓冲必须清空，否则上一个程序的诊断/事件/
            # 探针结果会漏进下一个程序（跨用例污染，且表现为"莫名"的失败）。
            self.events = []
            self.probes = []
            self.subscriptions = set()
            self.render_geometry = dict(render_geometry or {})
            # `load.rendering` 是**替身**，不是"实现声明的唯一来源"：每个批次从
            # **构造时的基线**出发，替身只在本批生效。两个坑都要避开：
            # ① 不传时若重置成"支持全部"，渲染器的子集声明被抹掉 → 降级再也不产生；
            # ② 不传时若沿用上一批的值，上一条用例的替身会**泄漏**到下一条。
            merged: Dict[str, Any] = dict(self._rendering_base)
            if rendering is not None:
                merged.update(rendering)
            self.rendering = merged
            self.capabilities = {c["name"]: c for c in (capabilities or [])}
            self.limits = {"callTimeoutMs": 5000}
            self.limits.update(limits or {})
            self.revision = 0
            self._baseline = set()
            self.dropped = 0
            self.pending_limit = int(self.limits.get("eventQueue", 256))
            if seed_state:
                self.store.write(seed_state)
            saved = self.store.read()
            stored_items = (saved.get("items") or {}) if isinstance(saved, dict) else {}

            diags: List[Diagnostic] = []
            self._cancel_inflight(inflight, diags, "装载")
            # 真实能力模块：与替身同一份契约形状；同名以真实模块为准
            for path in capability_modules or []:
                contracts, cap_diags = load_capability_module(path)
                diags += cap_diags
                self.capabilities.update(contracts)
            stmts, pdiags = parse_program(list(lines))
            diags += pdiags
            # 动作与调用需要数据源已存在，故延后执行
            deferred: List[Stmt] = []
            changed: List[str] = []
            for stmt in stmts:
                if isinstance(stmt, (ActionStmt, CallStmt)):
                    deferred.append(stmt)
                    continue
                if isinstance(stmt, SetStmt):
                    self._split_flag_set(stmt, diags)
                changed += apply_stmt(self.program, stmt, diags)
            self._refresh_subscriptions()
            self.assets_dir = assets_dir
            diags += validate(self.program, self.assets_dir)
            diags += self._rendering_degradations()
            if not seed_state and not stored_items and any(
                    src.persist for src in self.program.data.values()):
                diags.append(Diagnostic("STATE_REBUILT", INFO,
                                        "没有可用的状态文件，持久数据从声明初值重建"))
            self._seed_items(stored_items, diags)
            ctx = _Ctx(self)
            for stmt in deferred:
                if isinstance(stmt, CallStmt):
                    self._exec_call(stmt.func, stmt.params, stmt.into, diags, ctx)
                else:
                    changed += self._exec_action(stmt.action, diags, ctx, stmt.line)
            # 写在源文件里的探针在装载完成后执行（此时程序已完整）
            for probe in self.program.probes:
                self._run_probe(probe.verb, probe.target)
            diags += self._cascade(changed)
            diags += self._propagate()
            self._persist()
            self._baseline = {d.key() for d in diags}
            return diags

    def apply_batch(self, lines) -> List[Diagnostic]:
        with self.lock:
            diags: List[Diagnostic] = []
            stmts, pdiags = parse_program(list(lines))
            diags += pdiags
            changed: List[str] = []
            ctx = _Ctx(self)
            for stmt in stmts:
                if isinstance(stmt, CallStmt):
                    self._exec_call(stmt.func, stmt.params, stmt.into, diags, ctx)
                    continue
                if isinstance(stmt, ActionStmt):
                    changed += self._exec_action(stmt.action, diags, ctx, stmt.line)
                    continue
                if isinstance(stmt, Probe):
                    self._run_probe(stmt.verb, stmt.target)
                    continue
                if isinstance(stmt, SetStmt):
                    self._split_flag_set(stmt, diags, ctx)
                changed += apply_stmt(self.program, stmt, diags)
            self._seed_new_sources(diags)
            self._refresh_subscriptions()
            diags += validate(self.program, self.assets_dir)
            diags += self._rendering_degradations()
            diags += self._cascade(changed)
            diags += self._propagate()
            self.revision += 1
            self._persist()
            return self._new_only(diags)

    def fire(self, target: str, event: str, row: Optional[int] = None,
             value=None) -> List[Diagnostic]:
        target = target.lstrip("#")          # 接受 "#id" 与 "id" 两种写法
        with self.lock:
            diags: List[Diagnostic] = []
            if event in vocab.INTERACTION_EVENTS and self.flag(target, "disabled"):
                return diags                      # 引擎层拦截（禁用节点不响应）
            # 载荷：`change` 与 `submit` 都携带新值（04 词汇表：input 显式绑定 change
            # **或 submit**——提交时读控件的值是正当写法，载荷不带给绑定就取不到）。
            payload: dict = {"value": value} if event in ("change", "submit") else {}
            self._publish(target, event, row, value)   # 观察独立于反应
            row_name, row_value = self._row_context(target, row, diags)
            changed = self._dispatch(target, event, payload, diags,
                                     row_name, row_value)
            diags += self._cascade(changed)
            diags += self._propagate()
            self.revision += 1
            self._persist()
            return self._new_only(diags)

    def restart(self) -> List[Diagnostic]:
        with self.lock:
            diags: List[Diagnostic] = []
            # 重启是一个新的生命周期：诊断重新报一次，不沿用上一轮的"已报过"基线
            self._baseline = set()
            saved = self.store.read()
            stored = (saved.get("items") or {}) if isinstance(saved, dict) else {}
            if not stored and any(src.persist for src in self.program.data.values()):
                diags.append(Diagnostic("STATE_REBUILT", INFO,
                                        "没有可用的状态文件，持久数据从声明初值重建"))
            self._cancel_inflight(self._inflight_slots(), diags, "重启")
            self.slots = {}
            self.flags = {}
            self._seed_items(stored, diags)
            diags += self._propagate()
            self.revision = int((saved or {}).get("revision", self.revision)) + 1
            self._persist()
            self._baseline = {d.key() for d in diags}
            return diags

    # ------------------------------------------------------------ 状态辅助

    def flag(self, nid: str, name: str) -> bool:
        return bool(self.flags.get(nid, {}).get(name, FLAG_DEFAULTS.get(name, False)))

    def set_flag(self, nid: str, name: str, value: bool) -> None:
        self.flags.setdefault(nid, {})[name] = bool(value)

    def is_flag_static(self, nid: str, name: str) -> bool:
        node = self.program.nodes.get(nid)
        if node is None:
            return True
        return is_static(node.attrs.get(name)) if name in node.attrs else True

    def _split_flag_set(self, stmt: SetStmt, diags: List[Diagnostic],
                        ctx: Optional[_Ctx] = None) -> None:
        """顶层 `set` 作用于状态标志时写运行期状态，不写程序。"""
        node = self.program.nodes.get(stmt.target)
        if node is None or not stmt.attrs:
            return
        ctx = ctx if ctx is not None else _Ctx(self)
        for key in [k for k in stmt.attrs if k in STATE_FLAGS]:
            expr = stmt.attrs.pop(key)
            try:
                self.set_flag(stmt.target, key, bool(evaluate(expr, ctx)))
            except EvalError as ex:
                diags.append(Diagnostic(ex.code, ERROR, ex.message, line=stmt.line))

    def _seed_flags(self) -> None:
        for node in self.program.nodes.values():
            for name in STATE_FLAGS:
                expr = node.attrs.get(name)
                if isinstance(expr, Lit):
                    self.set_flag(node.id, name, bool(expr.value))

    def _seed_items(self, stored_items: Optional[dict] = None,
                    diags: Optional[List[Diagnostic]] = None) -> None:
        stored_items = stored_items or {}
        for src in self.program.data.values():
            key = "#" + src.id
            rows = stored_items.get(key)
            if src.persist and isinstance(rows, list):
                self.items[src.id] = rows
                if diags is not None:
                    diags.extend(self._drift_diags(src, rows))
            else:
                self.items[src.id] = self._initial_items(src)
        self._seed_flags()

    def _seed_new_sources(self, diags: List[Diagnostic]) -> None:
        """命令批里新声明的数据源也要按声明初值播种。

        不做这件事，运行期新声明的数据源**永远是空的**——而"先声明数据、再挂界面"
        正是改写程序时最常见的第一步（`apply_stmt` 对 `data` 只登记声明，
        播种原本只发生在 `load` 里）。与装载期一致：播种不是"变化"，不触发级联。
        """
        stored: dict = {}
        if any(src.persist for src in self.program.data.values()):
            saved = self.store.read()
            stored = (saved.get("items") or {}) if isinstance(saved, dict) else {}
        for src in self.program.data.values():
            if src.id in self.items:
                continue
            rows = stored.get("#" + src.id)
            if src.persist and isinstance(rows, list):
                self.items[src.id] = rows
                diags += self._drift_diags(src, rows)
            else:
                self.items[src.id] = self._initial_items(src)
                if src.persist:
                    diags.append(Diagnostic("STATE_REBUILT", INFO,
                                            "没有可用的状态文件，持久数据从声明初值重建"))

    def _drift_diags(self, src, rows: list) -> List[Diagnostic]:
        """结构漂移：不迁移、不删除，但必须说出来。"""
        extra = set()
        for row in rows:
            if isinstance(row, dict):
                extra |= {k for k in row if k not in src.schema}
        if not extra:
            return []
        return [Diagnostic("PERSIST_SCHEMA_DRIFT", WARNING,
                           "持久状态里出现程序未声明的字段：%s（不迁移、不删除）"
                           % ", ".join(sorted(extra)))]

    def _initial_items(self, src) -> List[dict]:
        if src.initial is None:
            return []
        try:
            value = evaluate(src.initial, _Ctx(self))
        except EvalError:
            return []
        if not isinstance(value, list):
            return []
        return [self._coerce_item(src.id, item) for item in value if isinstance(item, dict)]

    def _coerce_item(self, source: str, item: dict) -> dict:
        src = self.program.data.get(source)
        if src is None:
            return dict(item)
        out = {}
        for name, (_typ, default) in src.schema.items():
            if name in item:
                out[name] = item[name]
            elif default is not None:
                try:
                    out[name] = evaluate(default, _Ctx(self))
                except EvalError:
                    out[name] = None
            else:
                out[name] = None
        for key, value in item.items():
            out.setdefault(key, value)
        return out

    def _persist(self) -> None:
        payload = {
            "revision": self.revision,
            "items": {"#" + src.id: self.items.get(src.id, [])
                      for src in self.program.data.values() if src.persist},
        }
        self.store.write(payload)

    def _new_only(self, diags: List[Diagnostic]) -> List[Diagnostic]:
        """返回**本步新增**的诊断。

        基线的含义是"**装载期**已经报过的静态诊断"，不随批次增长。两个反面都要避开：

        * 若把批次诊断也塞进基线 → 同一个错误在后续批次里再次出现时被**静默抑制**，
          LLM 会以为改动成功了。那是比崩溃更糟的假成功，直接违背"失败必须可见"。
        * 若基线里什么都不放 → 装载期的静态诊断（如未知属性）会在**每一个**批次里重报，
          诊断流被同一条消息淹没。

        步内去重照旧（`seen`）：一条批可能对同一节点报同一条诊断多次。
        """
        out = []
        seen = set()
        for diag in diags:
            key = diag.key()
            if key in self._baseline or key in seen:
                continue
            seen.add(key)
            out.append(diag)
        return out

    def _refresh_subscriptions(self) -> None:
        self.subscriptions = {(l.target, l.event) for l in self.program.listens}

    def _publish(self, target: str, event: str, row: Optional[int] = None,
                 value=None) -> None:
        """把订阅的事件推进观察流。只有被 `listen` 订阅的才推送。"""
        if (target, event) not in self.subscriptions:
            return
        item = {"target": "#" + target, "event": event}
        if row is not None:
            item["row"] = row
        if value is not None:
            item["value"] = value
        self.events.append(item)
        if len(self.events) > self.pending_limit:
            self.dropped += len(self.events) - self.pending_limit
            self.events = self.events[-self.pending_limit:]

    def queue(self, diags: List[Diagnostic]) -> None:
        """观察流**不得静默丢弃**：溢出时丢最早的，并留下可见标记。"""
        self.pending.extend(diags)
        if len(self.pending) > self.pending_limit:
            self.dropped += len(self.pending) - self.pending_limit
            self.pending = self.pending[-self.pending_limit:]

    # ------------------------------------------------------------ 传播

    def _propagate(self) -> List[Diagnostic]:
        diags: List[Diagnostic] = []
        ctx = _Ctx(self)
        for node in self.program.nodes.values():
            if in_template(self.program, node.id):
                continue
            for name in list(node.attrs):
                if name in vocab.REF_ATTRS:
                    continue          # 引用类属性是"指向"，不是值
                try:
                    ctx.value_of(node.id, name)
                except EvalError as ex:
                    diags.append(Diagnostic(ex.code, ERROR, ex.message, line=node.line))
                    self.set_flag(node.id, "error", True)
        return diags

    def _refresh(self) -> None:
        self.queue(self._propagate())
        with self.lock:
            self._persist()

    # ------------------------------------------------------------ 处理器

    def _cascade(self, changed: List[str]) -> List[Diagnostic]:
        diags: List[Diagnostic] = []
        queue = [c for c in changed if c in self.program.data]
        rounds = 0
        while queue:
            rounds += 1
            if rounds > self.CASCADE_LIMIT:
                diags.append(Diagnostic(
                    "CONVERGENCE_LIMIT", ERROR,
                    "处理器级联达到上限（%d 轮），已停止传播" % self.CASCADE_LIMIT))
                break
            source = queue.pop(0)
            queue += self._dispatch(source, "change", {"source": "#" + source}, diags)
        return diags

    def _owning_template(self, target: str):
        for nid in [target] + ancestors_of(self.program, target):
            node = self.program.nodes.get(nid)
            if node is not None and node.type == "template":
                return node
        return None

    def _row_context(self, target: str, row: Optional[int], diags: List[Diagnostic]):
        """行内事件的上下文：该事件发生在模板的第几行。

        行内事件的绑定名只能由"第几行"确定，因此派发者**必须**给出行序号；
        无法确定时**必须**报错，不得静默按"无绑定"派发。
        """
        template = self._owning_template(target)
        if template is None:
            if row is not None:
                diags.append(Diagnostic("ROW_CONTEXT", ERROR,
                                        "#%s 不在任何模板内，不应携带行上下文" % target))
            return "", None
        if row is None:
            diags.append(Diagnostic("ROW_CONTEXT", ERROR,
                                    "#%s 位于模板 #%s 内，派发时必须给出行序号"
                                    % (target, template.id)))
            return template.as_name, None
        for node in self.program.nodes.values():
            ref = node.attrs.get("template")
            src = node.attrs.get("source")
            if isinstance(ref, Ref) and ref.addr == template.id and isinstance(src, Ref):
                items = self.items.get(src.addr, [])
                if 0 <= row < len(items):
                    return template.as_name, items[row]
                diags.append(Diagnostic("ROW_CONTEXT", ERROR,
                                        "行序号 %s 越界（共 %d 行）" % (row, len(items))))
                return template.as_name, None
        diags.append(Diagnostic("ROW_CONTEXT", ERROR,
                                "模板 #%s 未被任何列表使用" % template.id))
        return template.as_name, None

    def _dispatch(self, target: str, event: str, payload: dict,
                  diags: List[Diagnostic], row_name: str = "",
                  row=None) -> List[str]:
        """派发处理器。两个绑定互不相干：

        * 处理器的 `as <名>` → **事件载荷**（`change` 时是 `{value: <新值>}`）；
        * 模板声明的 `as <名>` → **行记录**（行上下文）。
        """
        changed: List[str] = []
        for handler in list(self.program.handlers):
            if handler.target == target and handler.event == event:
                changed += self._run_handler(handler, diags, payload, row_name, row)
        return changed

    def _run_handler(self, handler, diags: List[Diagnostic], payload: dict,
                     row_name: str = "", row=None) -> List[str]:
        bindings: Dict[str, dict] = {}
        if handler.bind:
            bindings[handler.bind] = payload if payload is not None else {}
        if row_name and row is not None:
            bindings[row_name] = row
        ctx = _Ctx(self, bindings or None)
        if handler.when is not None:
            try:
                if not evaluate(handler.when, ctx):
                    return []
            except EvalError as ex:
                diags.append(Diagnostic(ex.code, ERROR, ex.message, line=handler.line))
                return []
        changed: List[str] = []
        for action in handler.actions:
            changed += self._exec_action(action, diags, ctx, handler.line)
        return changed

    def _exec_action(self, action, diags: List[Diagnostic], ctx: _Ctx, line: int) -> List[str]:
        changed: List[str] = []
        if action.kind == "set":
            node = self.program.nodes.get(action.target)
            if node is None:
                diags.append(Diagnostic("TARGET_MISSING", ERROR,
                                        "目标 #%s 不存在" % action.target, line=line))
                return changed
            for key, expr in action.attrs.items():
                try:
                    value = evaluate(expr, ctx)
                except EvalError as ex:
                    diags.append(Diagnostic(ex.code, ERROR, ex.message, line=line))
                    continue
                if key in STATE_FLAGS:
                    self.set_flag(action.target, key, bool(value))
                    continue
                old = node.attrs.get(key)
                if old is not None and not is_static(old):
                    diags.append(Diagnostic("BIND_OVERRIDDEN", INFO,
                                            "属性 %s 的绑定已被覆盖" % key, line=line))
                node.attrs[key] = Lit(value, repr(value))
            return changed

        source = action.target
        if source not in self.program.data:
            diags.append(Diagnostic("TARGET_MISSING", ERROR,
                                    "数据源 #%s 不存在" % source, line=line))
            return changed

        if action.kind == "append":
            item = self._eval_dict(action.item, ctx, diags, line)
            if item is None:
                return changed
            self.items.setdefault(source, []).append(self._coerce_item(source, item))
            changed.append(source)
        elif action.kind == "remove":
            item = self._eval_dict(action.item, ctx, diags, line)
            if item is None:
                return changed
            rows = self.items.setdefault(source, [])
            for idx, row in enumerate(rows):
                if all(row.get(k) == v for k, v in item.items()):
                    rows.pop(idx)
                    changed.append(source)
                    break
        elif action.kind == "remove_where":
            rows = self.items.setdefault(source, [])
            keep = []
            for row in rows:
                try:
                    hit = evaluate(action.where, ctx.child(action.bind, row))
                except EvalError as ex:
                    diags.append(Diagnostic(ex.code, ERROR, ex.message, line=line))
                    keep.append(row)
                    continue
                if hit:
                    changed.append(source)
                else:
                    keep.append(row)
            self.items[source] = keep
        elif action.kind == "update_where":
            rows = self.items.setdefault(source, [])
            for row in rows:
                sub = ctx.child(action.bind, row)
                try:
                    hit = evaluate(action.where, sub)
                except EvalError as ex:
                    diags.append(Diagnostic(ex.code, ERROR, ex.message, line=line))
                    continue
                if not hit:
                    continue
                for key, expr in action.attrs.items():
                    try:
                        row[key] = evaluate(expr, sub)
                    except EvalError as ex:
                        diags.append(Diagnostic(ex.code, ERROR, ex.message, line=line))
                changed.append(source)
        elif action.kind == "clear":
            if self.items.get(source):
                self.items[source] = []
                changed.append(source)
        elif action.kind == "sort":
            rows = self.items.setdefault(source, [])
            rows.sort(key=lambda r: (r.get(action.by) is None, r.get(action.by)))
            changed.append(source)
        elif action.kind == "call":
            self._exec_call(action.func, action.params, action.into, diags, ctx)
        if changed:
            self._publish(source, "change")
        return changed

    def _eval_dict(self, expr, ctx: _Ctx, diags, line) -> Optional[dict]:
        if expr is None:
            return None
        try:
            value = evaluate(expr, ctx)
        except EvalError as ex:
            diags.append(Diagnostic(ex.code, ERROR, ex.message, line=line))
            return None
        return value if isinstance(value, dict) else None

    # ------------------------------------------------------------ 能力与槽

    def _exec_call(self, func: str, params: Dict[str, Expr], into: str,
                   diags: List[Diagnostic], ctx: _Ctx) -> None:
        cap = self.capabilities.get(func)
        if cap is None:
            diags.append(Diagnostic("CALL_UNKNOWN", ERROR, "能力 %s 不存在" % func))
            self._put_slot(into, "error", None, 0)
            return
        args = {}
        for name, expr in params.items():
            try:
                args[name] = evaluate(expr, ctx)
            except EvalError as ex:
                diags.append(Diagnostic(ex.code, ERROR, ex.message))
                return
        missing = [p.get("name") for p in cap.get("params", [])
                   if p.get("required") and p.get("name") not in args]
        if missing:
            diags.append(Diagnostic("CALL_CONTRACT", ERROR,
                                    "缺少必需参数：%s" % ", ".join(str(m) for m in missing)))
            self._put_slot(into, "error", None, 0)
            return
        known = {p.get("name") for p in cap.get("params", [])}
        extra = [k for k in args if k not in known]
        if extra:
            diags.append(Diagnostic("CALL_CONTRACT", ERROR,
                                    "多余参数：%s" % ", ".join(sorted(extra))))
            self._put_slot(into, "error", None, 0)
            return
        bad = []
        for p in cap.get("params", []):
            if p.get("name") in args and not type_matches(p.get("type", "any"),
                                                          args[p["name"]]):
                bad.append("%s 期望 %s，实际 %s" % (p["name"], p.get("type"),
                                                  type(args[p["name"]]).__name__))
        if bad:
            diags.append(Diagnostic("CALL_CONTRACT", ERROR,
                                    "参数类型不符：%s" % "；".join(bad)))
            self._put_slot(into, "error", None, 0)
            return
        self._seq += 1
        seq = self._seq
        previous = self.slots.get(into)
        if previous and previous.get("status") == "pending":
            # 作废已经在**这一刻**报出来了，所以旧调用的迟到结果**不再重复报**：
            # 否则它会在几秒后（旧调用的超时定时器终于炸掉时）漂进当时正在跑的那一步，
            # 让诊断失去归属。丢弃没有被静默——它就在下面这条里。
            if previous.get("seq"):
                self._cancelled.add(int(previous["seq"]))
            diags.append(Diagnostic("SLOT_CANCELLED", INFO,
                                    "槽 #%s 上正在进行的调用被新调用取消" % into))
            self._publish(into, "cancel")
            self._dispatch(into, "cancel", {"status": "cancel"}, diags)
        self._put_slot(into, "pending", None, seq)
        self._publish(into, "pending")
        self._dispatch(into, "pending", {"status": "pending", "value": None}, diags)
        threading.Thread(target=self._run_capability,
                         args=(cap, args, into, seq), daemon=True).start()

    def _inflight_slots(self) -> List[tuple]:
        return [(into, int(rec.get("seq", 0)))
                for into, rec in self.slots.items() if rec.get("status") == "pending"]

    def _cancel_inflight(self, inflight: List[tuple], diags: List[Diagnostic],
                         reason: str) -> None:
        """生命周期边界上的作废：在途调用是**上一个生命周期**的事。

        报告必须落在**那一步**（装载 / 重启）。否则它那条"陈旧结果被丢弃"会在一段时间后漂进
        **无关的后续步骤**——在 conformance 里表现为"某个用例莫名期望无诊断，却拿到一条 info"。
        跨用例污染比丢一条日志糟得多：它让诊断**失去归属**，而归属正是诊断可被行动的前提。
        """
        for into, seq in inflight:
            self._cancelled.add(seq)
            diags.append(Diagnostic("SLOT_CANCELLED", INFO,
                                    "槽 #%s 的在途调用随%s作废（其迟到结果不再产生诊断）"
                                    % (into, reason)))
        if len(self._cancelled) > 4096:               # 序号单调，只留最近的一段
            newest = max(self._cancelled)
            self._cancelled = {seq for seq in self._cancelled if seq > newest - 2048}

    def _put_slot(self, into: str, status: str, value, seq: int) -> None:
        self.slots[into] = {"status": status, "value": value, "seq": seq}

    def _run_capability(self, cap: dict, args: dict, into: str, seq: int) -> None:
        behavior = cap.get("behavior", "return")
        timeout = float(self.limits.get("callTimeoutMs", 5000)) / 1000.0
        state = {"done": False}

        def settle(status: str, value, code: str = "", message: str = "") -> None:
            if state["done"]:
                return
            state["done"] = True
            with self.lock:
                current = self.slots.get(into) or {}
                if seq in self._cancelled:
                    # 已在生命周期边界（装载 / 重启）那一步报过：不重复，更不许漂到别的步骤。
                    return
                if current.get("seq") != seq:
                    self.queue([Diagnostic("SLOT_STALE_DROPPED", INFO,
                                           "槽 #%s 丢弃陈旧结果" % into)])
                    return
                if code:
                    self.queue([Diagnostic(code, ERROR, message)])
                self._put_slot(into, status, value, seq)
                self._publish(into, status, None, value)
                slot_diags: List[Diagnostic] = []
                changed = self._dispatch(into, status,
                                         {"status": status, "value": value}, slot_diags)
                slot_diags += self._cascade(changed)
                self.queue(slot_diags)
            self._refresh()

        def on_timeout() -> None:
            settle("timeout", None, "SLOT_TIMEOUT",
                   "能力 %s 调用超时（%sms）" % (cap["name"], self.limits.get("callTimeoutMs")))

        timer = threading.Timer(timeout, on_timeout)
        timer.start()
        impl = cap.get("callable")
        try:
            if impl is not None:
                # **真实能力**：`async` 直接 await，同步函数在本线程里跑（已由线程池承载）
                value = (asyncio.run(impl(**args))
                         if inspect.iscoroutinefunction(impl) else impl(**args))
                json.dumps(value)                 # 契约：必须可序列化
                settle("done", value)
                return
            if behavior == "hang":
                timer.join()
                return
            if behavior == "delay":
                time.sleep(float(cap.get("delayMs", 100)) / 1000.0)
            if behavior == "error":
                raise RuntimeError("能力故意失败")
            if behavior == "nonjson":
                settle("error", None, "CALL_RESULT", "返回值不可序列化")
                return
            value = cap.get("value")
            json.dumps(value)                     # 契约：必须可序列化
            settle("done", value)
        except Exception as ex:  # noqa: BLE001 - 一律转为可见失败
            settle("error", None, "CALL_RESULT", "调用失败：%s" % ex)
        finally:
            timer.cancel()

    # ------------------------------------------------------------ 观察

    def render_state(self) -> dict:
        """**只读**的渲染状态快照：与 `observe()` 同一份数据，但**不取走**观察流。

        为什么必须存在：渲染器每一帧都要读当前状态（值 / 状态标志 / 模板行 / 数据），
        而观察流的**所有权在驱动者手里**（`observe()` 是"取走"语义）。渲染器若靠在渲染时
        调 `observe()` 取状态，就会把驱动者的事件与诊断吃掉——那是标准的静默失败
        （驱动者看不到事件，而界面上一切正常）。
        """
        with self.lock:
            out = {
                "nodes": ["#" + nid for nid in self.program.nodes],
                "attrs": self._observe_attrs(),
                "flags": self._observe_flags(),
                "rows": self._observe_rows(),
                "data": {"#" + key: value for key, value in self.items.items()},
            }
            if self.render_geometry:
                out["geometry"] = self._observe_geometry()
            return out

    def observe(self) -> dict:
        with self.lock:
            diags = self.pending
            self.pending = []
            if self.dropped:
                diags = [Diagnostic("OBSERVATION_DROPPED", WARNING,
                                    "观察流队列溢出，已丢弃 %d 条较早的事件" % self.dropped)] + diags
                self.dropped = 0
            events = self.events
            self.events = []
            probes = self.probes
            self.probes = []
            out = {
                "diagnostics": [d.to_dict() for d in diags],
                "events": events,
                "probes": probes,
                "nodes": ["#" + nid for nid in self.program.nodes],
                "attrs": self._observe_attrs(),
                "data": {"#" + k: v for k, v in self.items.items()},
                "flags": self._observe_flags(),
                "slots": {"#" + k: {"status": v.get("status"),
                                    "value": v.get("value"),
                                    "seq": v.get("seq", 0)}
                          for k, v in self.slots.items()},
                "rows": self._observe_rows(),
            }
            if self.render_geometry:
                out["geometry"] = self._observe_geometry()
            return out

    def _observe_geometry(self) -> Dict[str, Any]:
        """几何快照（观察面可选字段）。

        仅当渲染器提供几何（或注入测试替身）时该字段存在；否则字段**不出现**——
        驱动者据此判断"这个实现没有几何面"，再走截图兜底或看 `where` 的可见降级。
        """
        return {key: rect for key, rect in self.render_geometry.items()
                if key.lstrip("#") in self.program.nodes}

    def _observe_attrs(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        ctx = _Ctx(self)
        for node in self.program.nodes.values():
            if in_template(self.program, node.id):
                continue
            for name in node.attrs:
                if name in STATE_FLAGS or name in vocab.REF_ATTRS:
                    continue
                try:
                    value = ctx.value_of(node.id, name)
                except EvalError:
                    continue
                if isinstance(value, SERIALIZABLE) and not isinstance(value, CollectionRef):
                    out["#%s.%s" % (node.id, name)] = value
        return out

    def _observe_flags(self) -> Dict[str, bool]:
        out: Dict[str, bool] = {}
        for node in self.program.nodes.values():
            for name in STATE_FLAGS:
                out["#%s.%s" % (node.id, name)] = self.flag(node.id, name)
        return out

    def _observe_rows(self) -> Dict[str, list]:
        out: Dict[str, list] = {}
        for node in self.program.nodes.values():
            if node.type != "list":
                continue
            tmpl_ref = node.attrs.get("template")
            src_ref = node.attrs.get("source")
            if not isinstance(tmpl_ref, Ref) or not isinstance(src_ref, Ref):
                continue
            template = self.program.nodes.get(tmpl_ref.addr)
            if template is None:
                continue
            rows = []
            for item in self.items.get(src_ref.addr, []):
                entry: Dict[str, dict] = {}
                for kid in self._descendants(template.id):
                    attrs = {}
                    ctx = _Ctx(self, {template.as_name: item})
                    for name in self.program.nodes[kid].attrs:
                        if name in STATE_FLAGS:
                            continue
                        try:
                            value = ctx.value_of(kid, name)
                        except EvalError:
                            continue
                        if isinstance(value, SERIALIZABLE) and not isinstance(value, CollectionRef):
                            attrs[name] = value
                    if attrs:
                        entry["#" + kid] = attrs
                rows.append(entry)
            out["#" + node.id] = rows
        return out

    def _descendants(self, root_id: str) -> List[str]:
        out: List[str] = []
        stack = list(self.program.nodes[root_id].children) if root_id in self.program.nodes else []
        while stack:
            cur = stack.pop(0)
            node = self.program.nodes.get(cur)
            if node is None:
                continue
            out.append(cur)
            stack.extend(node.children)
        return out

    # ------------------------------------------------------------ 探针

    def _run_probe(self, verb: str, target: str = "") -> None:
        """探针：只读查询。输出进入观察流的 `probes` 通道；任何降级都必须可见。"""
        target = (target or "").lstrip("#")
        result = None
        diags: List[Diagnostic] = []
        if verb == "tree":
            start = target or ROOT
            if start not in self.program.nodes:
                diags.append(Diagnostic("TARGET_MISSING", ERROR,
                                        "探针目标 #%s 不存在" % start))
            else:
                result = self._tree(start)
        elif verb == "get":
            node = self.program.nodes.get(target)
            if node is None:
                diags.append(Diagnostic("TARGET_MISSING", ERROR,
                                        "探针目标 #%s 不存在" % target))
            else:
                result = {"id": "#" + node.id, "type": node.type,
                          "attrs": sorted(node.attrs), "children": list(node.children)}
        elif verb == "where":
            if target not in self.program.nodes:
                diags.append(Diagnostic("TARGET_MISSING", ERROR,
                                        "探针目标 #%s 不存在" % target))
            else:
                rect = self.render_geometry.get("#" + target)
                if rect is not None:
                    result = {"id": "#" + target, "geometry": rect}
                else:
                    diags.append(Diagnostic(
                        "DEGRADED_FEATURE", INFO,
                        "几何反查不可用：当前环境未提供几何信息，请改用视觉自检",
                        feature="geometry"))
                    result = {"id": "#" + target, "geometry": None}
        self.probes.append({"verb": verb,
                            "target": ("#" + target) if target else "",
                            "result": result})
        self.queue(diags)

    def program_lines(self) -> List[str]:
        """当前程序 → 源文本行（真源写回 / 往返验证）。

        只读程序 IR——绝不掺入运行期状态（`spec/02-ir.md` 的不变式）。
        """
        with self.lock:
            return _program_lines(self.program)

    def snapshot(self) -> dict:
        """视觉快照（可选观察面，仅供驱动者自检）。

        参考语义引擎不含渲染器 → 必须**可见降级**，返回空图而非假装成功。
        """
        with self.lock:
            self.queue([Diagnostic("DEGRADED_FEATURE", INFO,
                                   "截图不可用：参考语义引擎不含渲染器",
                                   feature="snapshot")])
            return {"image": None, "format": "png"}

    def deliver_interaction(self, target: str, action: str, value=None) -> dict:
        """把"用户动作"交给渲染层投递为事件（规范 05 第 4 节）。

        默认实现**没有渲染层**：做不到就**必须可见降级**，禁止假装送达。
        真正把用户动作转成事件的职责在渲染器（见 `puppet/tk_adapter.py`）。
        """
        with self.lock:
            nid = (target or "").lstrip("#")
            if nid not in self.program.nodes:
                return {"delivered": False,
                        "diagnostics": [Diagnostic(
                            "TARGET_MISSING", ERROR,
                            "节点 #%s 不存在" % nid).to_dict()]}
            self.queue([Diagnostic(
                "DEGRADED_FEATURE", INFO,
                "本实现没有渲染层，用户动作无法投递为事件（action=%s）" % action,
                feature="interaction")])
            return {"delivered": False}

    def probe(self, verb: str, target: str = "") -> dict:
        with self.lock:
            if verb == "tree":
                start = target or ROOT
                return {"tree": self._tree(start)}
            if verb == "get":
                node = self.program.nodes.get(target)
                if node is None:
                    return {"get": None}
                return {"get": {"id": "#" + target, "type": node.type,
                                "attrs": [k for k in node.attrs]}}
            if verb == "where":
                return {"where": {"id": "#" + target, "geometry": None}}
            return {}

    def _tree(self, root_id: str) -> dict:
        node = self.program.nodes.get(root_id)
        if node is None:
            return {}
        return {"id": "#" + node.id, "type": node.type,
                "children": [self._tree(c) for c in node.children if c in self.program.nodes]}
