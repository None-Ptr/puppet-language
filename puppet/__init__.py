"""Puppet 语言标准实现。

本包是 `spec/` 下语言规范的**参考实现**：解析、校验、IR、执行语义。
它不包含任何渲染实现；渲染由下游软件（puppethub）按渲染契约提供。
"""

from .assets import assets_dir, conformance_dir, spec_dir
from .capabilities import capability, contract_of, load_capability_module
from .diag import Diagnostic, ERROR, INFO, WARNING
from .engine import Engine
from .ir import Program, new_program, validate
from .serialize import program_lines, program_source

SPEC_VERSION = "2.1"

__all__ = [
    "Diagnostic", "ERROR", "INFO", "WARNING",
    "Engine", "Program", "new_program", "validate",
    "capability", "contract_of", "load_capability_module",
    "program_lines", "program_source",
    "assets_dir", "spec_dir", "conformance_dir",
    "SPEC_VERSION",
]
