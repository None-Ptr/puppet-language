"""conformance 适配器：行分隔 JSON over stdio（见 `conformance/README.md` 第 4 节）。

这个文件是"标准实现"与"外部测试者"之间的边界；任何语言都可以实现同一协议。
"""

from __future__ import annotations

import json
import sys

from . import vocab
from .engine import Engine


def describe_rendering() -> dict:
    """参考渲染器的能力声明（启动握手 `hello` 的响应体）。

    本实现是**无渲染器的语义引擎**：词汇全支持，但几何与截图**不提供**
    （二者都必须走可见降级）；可无 GUI 驱动。
    - 数组 = 声明支持的子集；`null` = 支持全部标准（本实现选择显式列全）。
    - `geometry` / `snapshot` / `headless` 是布尔观测/驱动能力。
    """
    return {
        "controls": list(vocab.NODE_TYPES),
        "attributes": sorted(vocab.ALL_ATTRS),
        "animations": sorted(vocab.ANIMATABLE),
        "icons": sorted(vocab.CORE_ICONS),
        "geometry": False,
        "snapshot": False,
        "interaction": False,
        "headless": True,
        "notes": "参考语义引擎：无渲染器；几何、截图与用户动作投递均按规范可见降级。"
                 "conformance 可用 load.renderGeometry / load.rendering 注入测试替身。",
    }


def handle(engine: Engine, request: dict) -> dict:
    op = request.get("op")
    if op == "hello":
        return {"protocol": "1", "rendering": describe_rendering()}
    if op == "load":
        diags = engine.load(
            request.get("program", []),
            capabilities=request.get("capabilities"),
            limits=request.get("limits"),
            seed_state=request.get("seedState"),
            render_geometry=request.get("renderGeometry"),
            rendering=request.get("rendering"),
            capability_modules=request.get("capabilityModules"),
            assets_dir=request.get("assetsDir"),
        )
        return {"diagnostics": [d.to_dict() for d in diags]}
    if op == "send":
        diags = engine.apply_batch(request.get("batch", []))
        return {"diagnostics": [d.to_dict() for d in diags]}
    if op == "fire":
        diags = engine.fire(request.get("target", ""), request.get("event", ""),
                            request.get("row"), request.get("value"))
        return {"diagnostics": [d.to_dict() for d in diags]}
    if op == "restart":
        diags = engine.restart()
        return {"diagnostics": [d.to_dict() for d in diags]}
    if op == "observe":
        return engine.observe()
    if op == "dump":
        # 真源写回 / 往返验证：把当前程序打印成源文本行。
        return {"program": engine.program_lines()}
    if op == "interact":
        return engine.deliver_interaction(request.get("target", ""),
                                          request.get("action", ""),
                                          request.get("value"))
    if op == "snapshot":
        return engine.snapshot()
    if op == "quit":
        return {"ok": True}
    return {"error": "未知操作 %r" % op}


def _emit(payload: dict) -> None:
    """输出纯 ASCII 的行分隔 JSON。

    刻意用 `ensure_ascii=True`：协议不依赖子进程的标准输出编码，
    在 Windows 上尤为关键（默认可能是 GBK）。
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main() -> int:
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="ascii", errors="backslashreplace")
    except Exception:  # noqa: BLE001 - 老环境没有 reconfigure 也不致命
        pass
    engine = Engine()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as ex:
            _emit({"error": "请求不是合法 JSON：%s" % ex})
            continue
        try:
            response = handle(engine, request)
        except Exception as ex:  # noqa: BLE001 - 适配器绝不静默：错误原样回传
            response = {"error": "%s: %s" % (type(ex).__name__, ex)}
        _emit(response)
        if request.get("op") == "quit":
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
