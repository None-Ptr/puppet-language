"""conformance 适配器（软件光栅渲染器）：真实几何 + 真实 PNG 截图 + 命中测试交互。

与 `tk_adapter` 同一协议，但**不需要显示器、不依赖任何第三方库**：

- **布局**：自带堆叠布局（col/row 主轴、`gap` 相邻间隔、`flex` 分配剩余空间、`w/h` 固定
  尺寸、`x/y` 脱离流动、`visible=false` 不占位）——完全确定性，故可与 Tk **交叉验证**几何。
- **截图**：纯标准库（`zlib` + `struct`）编码真 PNG，不是"假装成功"的空图。
- **交互**：**命中测试**（hit test）——与 Tk 的事件注入是两条不同路径，互为交叉验证。

因此 CI 的语言层作业可以直接跑它，不需要 `xvfb`。
文本不做字形栅格化（按"内容带"绘制），故像素级外观不在其能力声明内——这是能力声明的
诚实表达，不是缺陷。
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import zlib
from typing import Dict

from . import adapter as base
from .diag import ERROR, INFO, Diagnostic
from .engine import Engine
from .lang import Lit

RENDERING = {
    "controls": ["window", "dialog", "col", "row", "template", "text", "button",
                 "input", "checkbox", "divider", "spacer", "icon", "progress"],
    "attributes": ["gap", "w", "h", "x", "y", "bgcolor", "fg", "text",
                   "title", "flex"],
    "animations": [],
    "icons": [],
    "geometry": True,
    "snapshot": True,
    "interaction": True,
    "headless": True,
    "notes": "软件光栅参考渲染器：确定性堆叠布局 + 纯标准库 PNG 截图 + 命中测试交互；"
             "无需显示器。文本按内容带绘制（无字形栅格化），故像素级外观不在其能力内。",
}

_CONTAINERS = ("window", "dialog", "col", "row")


def _under(program, nid: str, root: str) -> bool:
    """`nid` 是否就是 `root`、或它的后代（模态阻断用，见规范 05 第 4 节）。"""
    cur = nid
    while cur:
        if cur == root:
            return True
        node = program.nodes.get(cur)
        cur = node.parent if node is not None else None
    return False
_GAP_DEFAULT = 8
# 未声明 w/h 时的自然尺寸（确定性，不依赖字体度量——故跨实现可比）
_NATURAL = {
    "text": (80, 24), "button": (80, 24), "input": (120, 24), "checkbox": (100, 24),
    "divider": (100, 1), "spacer": (8, 8), "icon": (16, 16), "progress": (100, 8),
    "image": (80, 60), "avatar": (32, 32), "navbar": (100, 40), "slider": (100, 24),
    "dropdown": (120, 24), "switch": (60, 24), "tabs": (100, 60), "list": (100, 60),
}
_NATURAL_DEFAULT = (80, 24)
_WINDOW_DEFAULT = (400, 300)
_BG = {
    "window": (255, 255, 255), "col": (248, 250, 252), "row": (248, 250, 252),
    "text": (30, 41, 59), "button": (37, 99, 235), "input": (255, 255, 255),
    "checkbox": (255, 255, 255), "divider": (226, 232, 240), "spacer": (241, 245, 249),
    "icon": (100, 116, 139), "progress": (191, 219, 254),
    "dialog": (203, 213, 225),          # 覆盖层底色（近似遮罩）
}
_BG_DEFAULT = (226, 232, 240)
_TEXTUAL = ("text", "button", "input", "checkbox")


def _rgb(color: str, fallback):
    """`#rrggbb` → (r, g, b)；不是颜色就退回默认（渲染自由，不算违反）。"""
    if not isinstance(color, str) or not color.startswith("#"):
        return fallback
    try:
        return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))
    except (ValueError, IndexError):
        return fallback


def _png(width: int, height: int, pixels: bytearray) -> bytes:
    """把 RGB 像素缓冲编码为 PNG（8 位真彩色，无滤波）。仅用标准库。"""
    stride = width * 3
    raw = bytearray()
    for row in range(height):
        raw.append(0)                                   # filter type 0 (None)
        raw += pixels[row * stride:(row + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


class RasterRenderer:
    """把程序 IR 布局成矩形并绘制成 PNG；交互走命中测试。"""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.rects: Dict[str, dict] = {}
        self.size = _WINDOW_DEFAULT
        self.state: Dict[str, object] = {}
        self.values: Dict[str, object] = {}
        # 观测替身（load.renderGeometry）：一旦提供即为真值，优先于自算布局
        self.double = None
        self.declares_no_geometry = False

    # ------------------------------------------------------------ 布局

    def relayout(self) -> None:
        self.rects = {}
        self.values = self.engine._observe_attrs()
        self.state = {}
        program = self.engine.program
        root = program.nodes.get("root")
        if root is None:
            return
        for wid in root.children:
            node = program.nodes.get(wid)
            if node is not None and node.type == "window":
                width, height = self._natural(node, _WINDOW_DEFAULT)
                self.size = (int(width), int(height))
                self.rects[node.id] = {"x": 0.0, "y": 0.0,
                                       "width": float(self.size[0]),
                                       "height": float(self.size[1])}
                self._layout_container(node, 0.0, 0.0, float(self.size[0]),
                                       float(self.size[1]), vertical=True)
                break

    def _static(self, node, name):
        expr = node.attrs.get(name)
        return expr.value if isinstance(expr, Lit) else None

    def _natural(self, node, fallback):
        width, height = self._static(node, "w"), self._static(node, "h")
        nat = _NATURAL.get(node.type, _NATURAL_DEFAULT)
        return (width if isinstance(width, (int, float)) else nat[0],
                height if isinstance(height, (int, float)) else nat[1])

    def _layout_container(self, node, x, y, width, height, vertical) -> None:
        gap = self._static(node, "gap")
        gap = float(gap) if isinstance(gap, (int, float)) else float(_GAP_DEFAULT)
        program = self.engine.program
        flow, absolute = [], []
        for cid in node.children:
            child = program.nodes.get(cid)
            if child is None or child.type == "template":
                continue
            if not self.engine.flag(child.id, "visible"):
                continue                      # visible=false 不占位（规范 05 第 2 节）
            if child.type == "dialog":
                # 覆盖层：铺满父的内容区，且**不参与兄弟的流动排布**（不占流动位置）。
                # 隐藏时不进入此处（上面的 visible 检查已拦），故不占位、不绘制、不阻断。
                self._place(child, x, y, width, height)
                continue
            cx, cy = self._static(child, "x"), self._static(child, "y")
            if isinstance(cx, (int, float)) and isinstance(cy, (int, float)):
                absolute.append(child)        # 绝对定位：脱离流动
            else:
                flow.append(child)

        sizes = [self._natural(child, _NATURAL_DEFAULT) for child in flow]
        count = len(flow)
        if count:
            main_total = float(height if vertical else width)
            used = sum(s[1] if vertical else s[0] for s in sizes)
            used += gap * (count - 1)
            flex_total = 0.0
            for child in flow:
                flex = self._static(child, "flex")
                if isinstance(flex, (int, float)) and flex > 0:
                    flex_total += float(flex)
            extra = max(main_total - used, 0.0)
            if flex_total > 0:                # flex 按比例分配剩余空间
                for idx, child in enumerate(flow):
                    flex = self._static(child, "flex")
                    if isinstance(flex, (int, float)) and flex > 0:
                        share = extra * (float(flex) / flex_total)
                        if vertical:
                            sizes[idx] = (sizes[idx][0], sizes[idx][1] + share)
                        else:
                            sizes[idx] = (sizes[idx][0] + share, sizes[idx][1])
            cursor = 0.0
            for idx, child in enumerate(flow):
                cw, ch = float(sizes[idx][0]), float(sizes[idx][1])
                if vertical:
                    box = (x, y + cursor, width, ch)
                else:
                    box = (x + cursor, y, cw, height)
                self._place(child, *box)
                cursor += (ch if vertical else cw) + gap
        for child in absolute:
            cw, ch = self._natural(child, _NATURAL_DEFAULT)
            self._place(child, x + float(self._static(child, "x")),
                        y + float(self._static(child, "y")), float(cw), float(ch))

    def _place(self, node, x, y, width, height) -> None:
        self.rects[node.id] = {"x": x, "y": y, "width": width, "height": height}
        if node.type in _CONTAINERS:
            self._layout_container(node, x, y, width, height,
                                   vertical=node.type != "row")
        elif node.type in ("input", "checkbox"):
            key = "#" + node.id
            self.state[node.id] = (self.values.get(key + ".value")
                                   if self.values.get(key + ".value") is not None
                                   else ("" if node.type == "input" else False))

    def geometry(self) -> dict:
        return {"#" + nid: dict(rect) for nid, rect in self.rects.items()}

    # ------------------------------------------------------------ 绘制

    def _fill(self, pixels, width, height, x, y, w, h, color) -> None:
        x0, y0 = max(int(x), 0), max(int(y), 0)
        x1, y1 = min(int(x + w), width), min(int(y + h), height)
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            start = (yy * width + x0) * 3
            pixels[start:start + len(row)] = row

    def snapshot(self) -> dict:
        width, height = self.size
        pixels = bytearray(width * height * 3)
        self._fill(pixels, width, height, 0, 0, width, height, (255, 255, 255))
        program = self.engine.program
        for nid, rect in self.rects.items():
            node = program.nodes.get(nid)
            if node is None:
                continue
            color = _rgb(self.values.get("#" + nid + ".bgcolor")
                         or self._static(node, "bgcolor"), _BG.get(node.type, _BG_DEFAULT))
            self._fill(pixels, width, height, rect["x"], rect["y"],
                       rect["width"], rect["height"], color)
            if node.type in _TEXTUAL:
                # 文本控件画一条"内容带"：足以让图像反映布局，又不需要字形栅格化
                inner = tuple(min(255, int(c * 0.35 + 255 * 0.65)) for c in color)
                self._fill(pixels, width, height, rect["x"] + 4, rect["y"] + 4,
                           max(rect["width"] - 8, 0), max(rect["height"] - 8, 0), inner)
        png = _png(width, height, pixels)
        return {"image": base64.b64encode(png).decode("ascii"),
                "format": "png", "width": width, "height": height}

    # ------------------------------------------------------------ 交互（命中测试）

    def _modal(self):
        """最上层**可见**的 `dialog`（后声明的在上）；没有则 None。

        这是渲染契约的一部分：模态可见时命中测试必须限制在其子树内（规范 05 第 4 节）。
        """
        modal = None
        for nid, node in self.engine.program.nodes.items():
            if node.type == "dialog" and self.engine.flag(nid, "visible"):
                modal = nid
        return modal

    def _hit(self, x, y):
        """命中测试：返回包含该点的最深层节点（后画的小节点优先）。

        **模态阻断**：存在可见的 `dialog` 时，命中范围限制在其子树内——子树之外的
        交互一律被阻断，**不穿透到下层控件**。
        """
        modal = self._modal()
        best = None
        for nid, rect in self.rects.items():
            if modal is not None and not _under(self.engine.program, nid, modal):
                continue                      # 模态阻断：只在其子树内命中
            if (rect["x"] <= x < rect["x"] + rect["width"]
                    and rect["y"] <= y < rect["y"] + rect["height"]):
                if best is None or rect["width"] * rect["height"] <= \
                        self.rects[best]["width"] * self.rects[best]["height"]:
                    best = nid
        return best

    def deliver(self, target, action, value=None) -> dict:
        nid = (target or "").lstrip("#")
        rect = self.rects.get(nid)
        if rect is None or nid not in self.engine.program.nodes:
            return {"delivered": False, "diagnostics": [Diagnostic(
                "TARGET_MISSING", ERROR,
                "节点 #%s 不存在或不可见" % nid).to_dict()]}
        node = self.engine.program.nodes[nid]
        # 落点取目标中心，并确认命中的是它自己或它的后代（真实 UI 的命中规则）
        hit = self._hit(rect["x"] + rect["width"] / 2.0,
                        rect["y"] + rect["height"] / 2.0)
        if hit is None:
            return {"delivered": False, "diagnostics": [Diagnostic(
                "DEGRADED_FEATURE", INFO, "该位置没有可命中的部件", feature="interaction"
            ).to_dict()]}
        target_id = hit
        tnode = self.engine.program.nodes[target_id]
        event, new_value = action, value
        if tnode.type == "button" and action == "click":
            event = "click"
        elif tnode.type == "checkbox":
            if action == "click":
                new_value = not bool(self.state.get(target_id))
            event = "change"
        elif tnode.type == "input":
            if action == "click":
                event = "focus"
            elif action == "submit":
                event = "submit"
            else:
                event = "change"
        elif action == "click":
            event = "click"
        if tnode.type in ("input", "checkbox"):
            self.state[target_id] = new_value
        self.engine.queue(self.engine.fire(target_id, event, None, new_value))
        return {"delivered": True, "hit": "#" + target_id,
                "event": event, "value": new_value}


def handle(engine: Engine, renderer: RasterRenderer, request: dict) -> dict:
    op = request.get("op")
    if op == "hello":
        return {"protocol": "1", "rendering": RENDERING}
    if op == "interact":
        return renderer.deliver(request.get("target", ""),
                                request.get("action", ""), request.get("value"))
    if op == "snapshot":
        return renderer.snapshot()
    return base.handle(engine, request)


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")
    except Exception:  # noqa: BLE001 - 老环境没有 reconfigure 也不致命
        pass
    engine = Engine(rendering=RENDERING)
    renderer = RasterRenderer(engine)
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
            # 每次装载都是新用例：先回到渲染器自身的声明，再让声明替身覆盖
            renderer.declares_no_geometry = False
            renderer.double = request.get("renderGeometry")
            if isinstance(request.get("rendering"), dict):
                declared = request["rendering"].get("geometry")
                if declared is not None:
                    renderer.declares_no_geometry = not bool(declared)
        renderer.relayout()
        # 几何来源优先级：观测替身 > 声明替身（geometry:false 即无） > 自算布局
        if renderer.double is not None:
            engine.render_geometry = renderer.double
        elif renderer.declares_no_geometry:
            engine.render_geometry = {}
        else:
            engine.render_geometry = renderer.geometry()
        try:
            response = handle(engine, renderer, request)
        except Exception as ex:  # noqa: BLE001 - 适配器绝不静默：错误原样回传
            response = {"error": "%s: %s" % (type(ex).__name__, ex)}
        base._emit(response)
        if request.get("op") == "quit":
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
