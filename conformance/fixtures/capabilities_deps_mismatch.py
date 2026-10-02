"""conformance 测试用：依赖声明与实际 import 不一致（必须报警 `CAP_DEPS_MISMATCH`）。"""

from puppet import capability

REQUIRES = ["flask", "puppet", "requests"]


@capability()
def ping() -> str:
    """返回 pong；声明里多写了并未导入的 requests / flask。"""
    return "pong"
