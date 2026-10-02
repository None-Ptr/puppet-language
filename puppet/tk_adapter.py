"""conformance 适配器（Tk 渲染器）：行分隔 JSON over stdio + **真实几何**。

与 `adapter.py` 同一协议（`conformance/README.md` 第 4 节）。区别：

- `hello` 声明 `geometry=true`（`winfo` 实测矩形）与一个**子集**能力声明——
  这正是"声明即 oracle"的靶子：未声明的词汇必须可见降级（引擎按声明自动发）。
- 布局按规范 05 第 2 节的可观察语义实现：col/row 主轴、gap（相邻间隔）、
  flex（剩余空间）、w/h（固定尺寸）、x/y（脱离流动）、visible=false（不占位）。
- Tk 是标准库（零第三方依赖不破）；需要显示器，故 `headless=false`。

用法：
    python conformance/runner.py --impl-module puppet.tk_adapter
"""

from __future__ import annotations

import json
import sys

from . import adapter as base
from .engine import Engine
from .lang import Lit

try:
    import tkinter as tk
except ImportError:  # pragma: no cover - 无 Tcl/Tk 的环境
    tk = None

# 能力声明：**只声明真实现了的**。规范允许子集实现——代价是未声明词汇
# 必须可见降级（引擎按这份声明自动产生 DEGRADED_FEATURE）。
RENDERING = {
    "controls": ["window", "col", "row", "template",
                 "text", "button", "spacer", "divider", "input", "checkbox"],
    "attributes": ["gap", "w", "h", "x", "y", "bgcolor", "fg",
                   "text", "title", "flex"],
    "animations": [],
    "icons": [],
    "geometry": True,
    "snapshot": False,
    "headless": False,
    "notes": "Tk 参考渲染器：容器与窗口的 w/h 按像素实现；文本类控件按内容自尺寸；"
             "图标不做位图（一律降级）；未声明的词汇一律可见降级。",
}

_CONTAINERS = ("window", "col", "row")
_GAP_DEFAULT = 8          # 规范 04 第 3.2 节：col/row/list 默认 gap=8


class TkRenderer:
    """把程序 IR 镜像成 Tk 部件树，并量出真实矩形。每次请求整体重建。"""

    def __init__(self, engine: Engine):
        if tk is None:
            raise RuntimeError("本环境没有 Tcl/Tk，无法运行 Tk 渲染器")
        self.engine = engine
        self.root = tk.Tk()
        self.root.withdraw()
        self.widgets: dict = {}
        self.origin = None
        # 能力声明替身可关掉几何（双向"按声明行事"）。
        self.geometry_on = True
        # 观测替身（load.renderGeometry）：一旦提供，它就是真值——
        # 与业务替身 capabilities 同一待遇，优先于真实测量。
        self.double = None

    def close(self):
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def pump(self):
        """让 Tk 把布局算完（不进 mainloop，事件驱动全靠它）。"""
        self.root.update()

    # ------------------------------------------------------------ 镜像

    def sync(self):
        for w in self.widgets.values():
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass
        self.widgets = {}
        self.origin = None
        program = self.engine.program
        root = program.nodes.get("root")
        if root is None:
            return
        for wid in root.children:
            node = program.nodes.get(wid)
            if node is not None and node.type == "window":
                widget = self._build(node, None)
                if self.origin is None and widget is not None:
                    self.origin = widget

    def _build(self, node, parent):
        typ = node.type
        if typ == "template":
            return None          # 页面内不渲染（规范 04 第 2 节）；行实例由 list 承担
        if typ == "window":
            widget = tk.Toplevel(self.root)
            title = self._static(node, "title")
            if isinstance(title, str):
                widget.title(title)
            width, height = self._static(node, "w"), self._static(node, "h")
            if isinstance(width, (int, float)) and isinstance(height, (int, float)):
                widget.geometry("%dx%d" % (int(width), int(height)))
            self._style(node, widget)
        else:
            widget = self._make(typ, node, parent)
            self._style(node, widget)
        self.widgets[node.id] = widget
        for cid in node.children:
            child = self.engine.program.nodes.get(cid)
            if child is None or child.type == "template":
                continue
            self._build(child, widget)
        if typ in _CONTAINERS:
            self._layout_children(node, widget)
        return widget

    def _make(self, typ, node, parent):
        text = self._static(node, "text")
        label = str(text) if text is not None else ""
        if typ == "text":
            return tk.Label(parent, text=label)
        if typ == "button":
            return tk.Button(parent, text=label)
        if typ == "input":
            return tk.Entry(parent)
        if typ == "checkbox":
            return tk.Checkbutton(parent, text=label)
        if typ == "divider":
            return tk.Frame(parent, height=1, bd=0, relief="flat", bg="#e2e8f0")
        if typ == "spacer":
            return tk.Frame(parent, width=1, height=1)
        # 未声明支持的控件（list/tabs/slider/…）与未知类型：降级为占位容器——
        # 可见降级由引擎按声明发 DEGRADED_FEATURE；子树继续参与布局，
        # 禁止"渲染成空白后丢弃子树"。
        return tk.Frame(parent)

    def _style(self, node, widget):
        color = self._static(node, "bgcolor")
        if isinstance(color, str):
            try:
                widget.configure(bg=color)
            except tk.TclError:
                pass
        fg = self._static(node, "fg")
        if isinstance(fg, str):
            try:
                widget.configure(fg=fg)
            except tk.TclError:
                pass
        width, height = self._static(node, "w"), self._static(node, "h")
        if isinstance(widget, tk.Frame) and (
                isinstance(width, (int, float)) or isinstance(height, (int, float))):
            if isinstance(width, (int, float)):
                widget.configure(width=int(width))
            if isinstance(height, (int, float)):
                widget.configure(height=int(height))
            widget.pack_propagate(False)

    def _layout_children(self, node, widget):
        horizontal = node.type == "row"
        gap = self._static(node, "gap")
        gap = gap if isinstance(gap, (int, float)) else _GAP_DEFAULT
        kids = [self.engine.program.nodes.get(cid) for cid in node.children]
        kids = [k for k in kids if k is not None and k.type != "template"]
        last = len(kids) - 1
        for idx, child in enumerate(kids):
            if not self.engine.flag(child.id, "visible"):
                continue                      # visible=false 不占位（规范 05 第 2 节）
            target = self.widgets.get(child.id)
            if target is None:
                continue
            x, y = self._static(child, "x"), self._static(child, "y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                target.place(x=int(x), y=int(y))   # 绝对定位脱离流动
                continue
            edge = int(gap / 2.0)             # 相邻子节点共享一个 gap，首尾不留半格
            pad = (0 if idx == 0 else edge, 0 if idx == last else edge)
            flex = self._static(child, "flex")
            expand = isinstance(flex, (int, float)) and flex > 0
            if horizontal:
                target.pack(side="left", padx=pad,
                            fill="both" if expand else "y", expand=expand)
            else:
                target.pack(side="top", pady=pad,
                            fill="both" if expand else "x", expand=expand)

    # ------------------------------------------------------------ 测量

    def measure(self) -> dict:
        """量出真实矩形：相对第一个窗口客户区左上角，浮点像素（规范 05 第 8 节）。"""
        out: dict = {}
        if self.origin is None:
            return out
        try:
            ox = self.origin.winfo_rootx()
            oy = self.origin.winfo_rooty()
        except tk.TclError:
            return out
        for nid, widget in self.widgets.items():
            try:
                if not widget.winfo_ismapped():
                    continue          # 未映射 = 不可见 / 未布局，不提供几何
                out["#" + nid] = {
                    "x": widget.winfo_rootx() - ox,
                    "y": widget.winfo_rooty() - oy,
                    "width": widget.winfo_width(),
                    "height": widget.winfo_height(),
                }
            except tk.TclError:
                continue
        return out

    def _static(self, node, name):
        expr = node.attrs.get(name)
        return expr.value if isinstance(expr, Lit) else None


def handle(engine: Engine, request: dict) -> dict:
    if request.get("op") == "hello":
        return {"protocol": "1", "rendering": RENDERING}
    return base.handle(engine, request)


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")
    except Exception:  # noqa: BLE001 - 老环境没有 reconfigure 也不致命
        pass
    engine = Engine(rendering=RENDERING)
    renderer = TkRenderer(engine)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as ex:
            base._emit({"error": "请求不是合法 JSON：%s" % ex})
            continue
        if request.get("op") == "load":
            # 每次装载都是新用例：先回到渲染器自身的声明，再让声明替身覆盖。
            renderer.geometry_on = True
            renderer.double = request.get("renderGeometry")
            if isinstance(request.get("rendering"), dict):
                declared = request["rendering"].get("geometry")
                if declared is not None:
                    renderer.geometry_on = bool(declared)
        # 几何来源优先级：观测替身 > 真实测量。先镜像并让 Tk 算完，
        # 再应答——观察与探针（含批内 where）拿到的都是当前矩形。
        renderer.sync()
        renderer.pump()
        if renderer.double is not None:
            engine.render_geometry = renderer.double
        else:
            engine.render_geometry = renderer.measure() if renderer.geometry_on else {}
        try:
            response = handle(engine, request)
        except Exception as ex:  # noqa: BLE001 - 适配器绝不静默：错误原样回传
            response = {"error": "%s: %s" % (type(ex).__name__, ex)}
        base._emit(response)
        if request.get("op") == "quit":
            break
    renderer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
