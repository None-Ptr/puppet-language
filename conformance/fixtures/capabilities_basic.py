"""conformance 测试用：契约齐备的真实能力模块。

`REQUIRES` 与源码实际 import 一致（否则应报 `CAP_DEPS_MISMATCH`）。
"""

import json
import time

from puppet import capability

REQUIRES = ["json", "puppet", "time"]


@capability()
def echo_upper(text: str) -> dict:
    """返回大写后的文本与长度，用于验证真实能力被加载并真正执行。"""
    return {"text": text.upper(), "length": len(text)}


@capability()
def stall(seconds: float = 2.0) -> str:
    """故意慢，用于验证**真实调用**的超时与取消（不是测试替身）。"""
    time.sleep(float(seconds))
    return json.dumps({"slept": seconds})
