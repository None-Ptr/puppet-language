"""能力（工具）的契约提取与真实加载（规范 `03-semantics.md` 第 4 节）。

三条硬性义务，缺一即违规：

- **说明文本非空**：docstring 是 LLM 发现工具的唯一说明书（`03` 第 4.5 节），
  缺失即 **错误** `CAP_NO_DOC`，且该能力**不注册**。
- **依赖显式声明 + 扫描兜底 + 不一致必须报警**：模块级 `REQUIRES` 与源码实际 import
  不一致时 **警告** `CAP_DEPS_MISMATCH`——否则部署机上才缺包（v1 的教训）。
- **返回值可序列化**：由引擎校验（`CALL_RESULT`）；`async` 函数直接 await，
  同步函数走线程池，超时 / 取消由槽负责。

测试替身（conformance 的 `load.capabilities`）与真实模块（`load.capabilityModules`）
走**同一份契约形状**，因此行为用例对二者同样适用。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import os
from typing import Any, Dict, List, Optional, Tuple

from .diag import ERROR, INFO, WARNING, Diagnostic

MARKER = "__puppet_capability__"

_PY_TYPES = {"str": str, "int": int, "float": float, "bool": bool,
             "list": list, "dict": dict, "any": object}


def capability(fn=None, *, name: Optional[str] = None, returns: Any = None,
               requires: Optional[List[str]] = None):
    """把一个函数标记为能力。

    ```python
    @capability()
    def compress(path: str, level: int = 6) -> dict:
        \"\"\"压缩文件，返回 {"bytes": <大小>}。\"\"\"
    ```

    `requires` 是**本能力**额外需要的依赖；模块级 `REQUIRES` 覆盖全模块。
    """
    def wrap(func):
        setattr(func, MARKER, {"name": name or func.__name__,
                               "returns": returns, "requires": requires})
        return func
    return wrap if fn is None else wrap(fn)


def type_name(annotation: Any) -> str:
    """把类型注解归一化成规范里的类型名（`str` / `int` / …）。"""
    if annotation is inspect.Parameter.empty or annotation is None:
        return "any"
    if isinstance(annotation, str):
        return annotation.strip() or "any"
    return getattr(annotation, "__name__", "any")


def contract_of(func) -> Optional[Dict[str, Any]]:
    """从签名与类型注解提取契约；非能力函数返回 `None`。"""
    marker = getattr(func, MARKER, None)
    if marker is None:
        return None
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):       # 不可 introspect 的可调用对象
        params = []
    else:
        params = []
        for pname, param in sig.parameters.items():
            if pname in ("self", "cls"):
                continue
            params.append({
                "name": pname,
                "type": type_name(param.annotation),
                "required": param.default is inspect.Parameter.empty,
                **({} if param.default is inspect.Parameter.empty
                   else {"default": param.default}),
            })
    doc = (func.__doc__ or "").strip()
    return {
        "name": marker.get("name") or func.__name__,
        "params": params,
        "returns": type_name(marker.get("returns"))
        if marker.get("returns") is not None
        else type_name(getattr(func, "__annotations__", {}).get("return")),
        "doc": doc,
        "requires": list(marker.get("requires") or []),
        "callable": func,
        "behavior": "callable",
    }


def type_matches(declared: str, value: Any) -> bool:
    """按契约声明的类型判定实参；`any` 或未声明一律接受。"""
    expected = _PY_TYPES.get(str(declared).lower())
    if expected is None or expected is object:
        return True
    if expected is float and isinstance(value, int) and not isinstance(value, bool):
        return True                      # 整数提升为浮点（规范 04 的隐式转换）
    if expected is int and isinstance(value, bool):
        return False                     # 布尔不参与数值比较
    return isinstance(value, expected)


def _imported_modules(path: str) -> List[str]:
    """扫描源码的顶层 import（兜底：动态 import 扫不到，故仍要求显式声明）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError):
        return []
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return sorted(found)


def load_capability_module(path: str) -> Tuple[Dict[str, dict], List[Diagnostic]]:
    """加载一个能力模块，返回 `(契约表, 诊断)`。

    失败（找不到文件 / 导入报错）**必须可见**：`CAP_IMPORT` 错误，不静默跳过。
    """
    diags: List[Diagnostic] = []
    name = os.path.splitext(os.path.basename(path))[0]
    if not os.path.isfile(path):
        diags.append(Diagnostic("CAP_IMPORT", ERROR, "能力模块不存在：%s" % path))
        return {}, diags
    spec = importlib.util.spec_from_file_location("puppet_capability_" + name, path)
    if spec is None or spec.loader is None:
        diags.append(Diagnostic("CAP_IMPORT", ERROR, "能力模块无法加载：%s" % path))
        return {}, diags
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as ex:  # noqa: BLE001 - 一律转为可见失败
        diags.append(Diagnostic("CAP_IMPORT", ERROR,
                                "能力模块导入失败（%s）：%s: %s"
                                % (name, type(ex).__name__, ex)))
        return {}, diags

    # 依赖核对：显式声明 vs 实际 import
    declared = list(getattr(module, "REQUIRES", []) or [])
    scanned = _imported_modules(path)
    if declared and sorted(set(declared)) != scanned:
        diags.append(Diagnostic(
            "CAP_DEPS_MISMATCH", WARNING,
            "能力模块 %s 的依赖声明与实际 import 不一致：声明 %s，实际 %s"
            % (name, sorted(set(declared)), scanned)))

    contracts: Dict[str, dict] = {}
    for attr in vars(module).values():
        contract = contract_of(attr)
        if contract is None:
            continue
        if not contract["doc"]:
            diags.append(Diagnostic(
                "CAP_NO_DOC", ERROR,
                "能力 %s 没有说明文本（docstring 是工具的唯一说明书），不予注册"
                % contract["name"]))
            continue
        if contract["name"] in contracts:
            diags.append(Diagnostic(
                "CALL_UNKNOWN", ERROR, "能力 %s 重复定义" % contract["name"]))
            continue
        contracts[contract["name"]] = contract
    return contracts, diags
