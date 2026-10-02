"""conformance 测试用：缺失说明文本的能力（必须报错 `CAP_NO_DOC` 且不注册）。"""

from puppet import capability

REQUIRES = ["puppet"]


@capability()
def silent(x: str) -> str:
    return x
