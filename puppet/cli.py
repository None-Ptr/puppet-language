"""`puppet` 命令：标准工具（不需要 app 即可运行）。

子命令：`spec`（规范位置）、`validate`（静态校验）、`conformance`（跑合规用例）。

注意：`validate` 只做静态校验——解析与 IR 校验，不起运行时。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from . import SPEC_VERSION
from .engine import Engine
from .ir import apply_stmt, new_program, validate
from .lang import parse_program

def _assets_dir() -> str:
    """定位随发行包分发的 `spec/` 与 `conformance/`。

    开发态（源码树 / `pip install -e`）：仓库根。
    安装态：setuptools 的 data-files 落在 `<prefix>/share/puppet`。
    可用环境变量 `PUPPET_ASSETS` 覆盖（下游打包可自行安置资产）。
    """
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


ROOT = _assets_dir()


def cmd_spec(_args) -> int:
    path = os.path.join(ROOT, "spec")
    if not os.path.isdir(path):
        print("找不到规范目录：%s（发行包安装不完整？）" % path, file=sys.stderr)
        return 1
    print("规范版本：%s" % SPEC_VERSION)
    print(path)
    return 0


def _validate_target(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    program = new_program()
    diags = []
    stmts, pdiags = parse_program(lines)
    diags += pdiags
    for stmt in stmts:
        apply_stmt(program, stmt, diags)
    diags += validate(program)
    return diags


def cmd_validate(args) -> int:
    if not os.path.exists(args.path):
        print("路径不存在：%s" % args.path)
        return 1
    if os.path.isdir(args.path):
        targets = [os.path.join(args.path, name) for name in sorted(os.listdir(args.path))
                   if name.endswith(".puppet")]
        if not targets:
            print("目录里没有 .puppet 文件：%s" % args.path)
            return 1
    else:
        targets = [args.path]

    total_diags = total_errors = 0
    for target in targets:
        diags = _validate_target(target)
        total_diags += len(diags)
        total_errors += sum(1 for d in diags if d.level == "error")
        if not diags:
            print("%s：零诊断" % os.path.basename(target))
            continue
        print("%s：" % os.path.basename(target))
        for diag in diags:
            mark = {"error": "错误", "warning": "警告", "info": "信息"}[diag.level]
            location = ("第 %d 行" % diag.line) if diag.line else "全局"
            print("  %s %s %s: %s" % (location, mark, diag.code, diag.message))
    print("合计 %d 条诊断，其中错误 %d 条" % (total_diags, total_errors))
    if args.strict:
        return 1 if total_diags else 0
    return 1 if total_errors else 0


def cmd_conformance(args) -> int:
    runner = os.path.join(ROOT, "conformance", "runner.py")
    if not os.path.isfile(runner):
        print("找不到 conformance 运行器：%s（发行包安装不完整？）" % runner,
              file=sys.stderr)
        return 1
    module = "puppet.tk_adapter" if args.tk else "puppet.adapter"
    cmd = [sys.executable, runner, "--impl", '"%s" -m %s' % (sys.executable, module)]
    if args.filter:
        cmd += ["--filter", args.filter]
    return subprocess.call(cmd, cwd=ROOT)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="puppet", description="Puppet 语言标准工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("spec", help="显示规范版本与位置").set_defaults(func=cmd_spec)

    p_val = sub.add_parser("validate", help="静态校验 .puppet 文件或目录")
    p_val.add_argument("path", help="文件路径，或包含 .puppet 的目录")
    p_val.add_argument("--strict", action="store_true", help="有任何诊断即视为失败")
    p_val.set_defaults(func=cmd_validate)

    p_conf = sub.add_parser("conformance", help="运行合规用例集")
    p_conf.add_argument("--filter", default="")
    p_conf.add_argument("--tk", action="store_true",
                        help="用 Tk 渲染器适配器跑（真实几何；需要显示器）")
    p_conf.set_defaults(func=cmd_conformance)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
