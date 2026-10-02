"""conformance 测试用：返回值不可序列化（必须 `CALL_RESULT`，不得静默成功）。"""

from puppet import capability

REQUIRES = ["puppet"]


@capability()
def opaque(x: int = 1) -> object:
    """返回一个不可序列化的对象（违反返回值契约）。"""
    return object()
