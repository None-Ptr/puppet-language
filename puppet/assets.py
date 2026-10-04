"""随发行包分发的资产定位（`spec/` 与 `conformance/`）。

下游（puppethub）需要按"触碰的词汇"检索规范章节，因此定位逻辑必须**公开**——
让下游自己再写一遍路径推导，就是第二份真相，迟早漂移。

- 开发态（源码树 / `pip install -e`）：仓库根。
- 安装态：setuptools 的 data-files 落在 `<prefix>/share/puppet`。
- 环境变量 `PUPPET_ASSETS` 可覆盖（下游打包可自行安置资产）。
"""

from __future__ import annotations

import os
import sys


def assets_dir() -> str:
    """`spec/` 与 `conformance/` 所在的根目录。"""
    override = os.environ.get("PUPPET_ASSETS")
    if override:
        return override
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(root, "spec")):
        return root
    installed = os.path.join(sys.prefix, "share", "puppet")
    if os.path.isdir(os.path.join(installed, "spec")):
        return installed
    return root


def spec_dir() -> str:
    return os.path.join(assets_dir(), "spec")


def conformance_dir() -> str:
    return os.path.join(assets_dir(), "conformance")
