"""诊断：级别、形状、码。

码表与 `spec/06-diagnostics.md` 一致；此处只定义形状，码以字符串常量使用。
"""

from __future__ import annotations

from dataclasses import dataclass

ERROR = "error"
WARNING = "warning"
INFO = "info"


@dataclass
class Diagnostic:
    code: str
    level: str
    message: str
    line: int = 0
    col: int = 0
    suggest: str = ""
    # 仅 DEGRADED_FEATURE 使用：被降级的那个特性（`geometry` / `snapshot` /
    # `control:<类型>` / `attr:<名>` / `animation:<名>` / `icon:<名>`）。
    # conformance 的"声明即 oracle"按它比对，故必须机读。
    feature: str = ""

    def to_dict(self):
        out = {"code": self.code, "level": self.level, "message": self.message}
        if self.line:
            out["line"] = self.line
        if self.col:
            out["col"] = self.col
        if self.suggest:
            out["suggest"] = self.suggest
        if self.feature:
            out["feature"] = self.feature
        return out

    def key(self):
        """用于"本步是否新增"的判据（不含行号：行号随批次变化）。"""
        return (self.code, self.message, self.level)
