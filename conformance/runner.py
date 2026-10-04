#!/usr/bin/env python3
"""Puppet 语言标准 · conformance 运行器（零依赖，仅标准库）。

用法：
    python conformance/runner.py --check
    python conformance/runner.py --impl "<启动实现的命令>"
    python conformance/runner.py --impl "..." --filter b-cycle

实现协议见同目录 README.md 第 4 节。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CASES = os.path.join(HERE, "cases")

EXPECT_KEYS = {
    "diagnostics", "forbid", "minErrors", "noDiagnostics",
    "events", "noEvents", "probes",
    "nodes", "nodesAbsent", "attrs", "data", "flags", "slots", "rows",
    "geometry", "relations", "degraded", "snapshotAvailable", "note",
}
OBSERVE_KEYS = {"events", "probes", "nodes", "attrs", "data", "flags",
                "slots", "rows", "geometry"}
CASE_KEYS = {
    "id", "title", "spec", "program", "steps", "requires",
    "loadExpect", "capabilities", "limits", "seedState", "renderGeometry",
    "rendering", "capabilityModules", "assetsDir",
}
STEP_KEYS = {"send", "fire", "interact", "restart", "snapshot", "roundtrip", "expect"}
STEP_ACTIONS = {"send", "fire", "interact", "restart", "snapshot", "roundtrip", "expect"}
BEHAVIORS = {"return", "error", "hang", "nonjson", "delay"}
LEVELS = {"error", "warning", "info"}
# `interact` 的动作集 = 规范 05 第 4 节的交互事件名
INTERACTIONS = {"click", "change", "submit", "focus", "blur"}

# 几何关系词汇（见 spec/05-render-contract.md 第 8 节）。全部为**定性/相对**断言：
# 不比较绝对坐标，因而跨渲染器（字号 / DPI 不同）可比。
RELATIONS = {
    "left_of", "right_of", "above", "below",
    "contains", "inside", "overlaps", "disjoint",
    "aligned_x", "aligned_y", "aligned_center_x", "aligned_center_y",
    "same_width", "same_height", "same_size",
    "gap_h", "gap_v", "width_ratio", "height_ratio",
}
RELATION_NEEDS_VALUE = {"gap_h", "gap_v", "width_ratio", "height_ratio"}
# 观测/驱动能力（布尔）：geometry / snapshot / interaction / headless
DEGRADED_OBS = {"geometry", "snapshot", "interaction", "headless"}
# 词汇能力（列表）：control:<类型> / attr:<名> / animation:<名> / icon:<名>
DEGRADED_PREFIXES = ("control:", "attr:", "animation:", "icon:")
DEFAULT_TOL = 1.0


# ---------------------------------------------------------------- 用例装载

def load_cases(cases_dir):
    """返回 (cases, problems)。cases 为 (来源文件, 用例) 列表。"""
    problems = []
    cases = []
    if not os.path.isdir(cases_dir):
        return cases, ["用例目录不存在：%s" % cases_dir]
    seen = {}
    for name in sorted(os.listdir(cases_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cases_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as ex:  # noqa: BLE001 - 报告而非抛出
            problems.append("%s: 不是合法 JSON：%s" % (name, ex))
            continue
        if not isinstance(data, list):
            problems.append("%s: 顶层必须是用例数组" % name)
            continue
        for i, case in enumerate(data):
            where = "%s[%d]" % (name, i)
            problems += validate_case(case, where)
            if isinstance(case, dict) and isinstance(case.get("id"), str):
                cid = case["id"]
                if cid in seen:
                    problems.append("%s: id 重复（已在 %s 出现）" % (where, seen[cid]))
                seen[cid] = where
            cases.append((name, case))
    return cases, problems


def _req(problems, case, key, types, where):
    if key not in case:
        problems.append("%s: 缺少必需字段 %s" % (where, key))
        return False
    if not isinstance(case[key], types):
        problems.append("%s: 字段 %s 类型不符" % (where, key))
        return False
    return True


def validate_expect(expect, where, problems):
    if not isinstance(expect, dict):
        problems.append("%s: 期望必须是对象" % where)
        return
    for key in expect:
        if key not in EXPECT_KEYS:
            problems.append("%s: 未知期望键 %s" % (where, key))
    for d in expect.get("diagnostics", []):
        if not isinstance(d, dict) or "code" not in d:
            problems.append("%s: diagnostics 的每项必须含 code" % where)
        elif "level" in d and d["level"] not in LEVELS:
            problems.append("%s: 诊断级别非法：%r" % (where, d["level"]))
    for code in expect.get("forbid", []):
        if not isinstance(code, str):
            problems.append("%s: forbid 的每项必须是字符串" % where)
    for ev in expect.get("events", []):
        if not isinstance(ev, dict) or "target" not in ev or "event" not in ev:
            problems.append("%s: events 的每项必须含 target 与 event" % where)
    if "noEvents" in expect and not isinstance(expect["noEvents"], bool):
        problems.append("%s: noEvents 必须是布尔" % where)
    for pr in expect.get("probes", []):
        if not isinstance(pr, dict) or "verb" not in pr:
            problems.append("%s: probes 的每项必须含 verb" % where)
    for key in ("nodes", "nodesAbsent"):
        for nid in expect.get(key, []):
            if not isinstance(nid, str) or not nid.startswith("#"):
                problems.append("%s: %s 的每项必须是 # 开头的地址" % (where, key))
    for key in ("attrs", "data", "flags", "slots", "rows", "geometry"):
        if key in expect and not isinstance(expect[key], dict):
            problems.append("%s: %s 必须是对象" % (where, key))
    for rel in expect.get("relations", []):
        if not isinstance(rel, dict):
            problems.append("%s: relations 的每项必须是对象" % where)
            continue
        name = rel.get("rel")
        if name not in RELATIONS:
            problems.append("%s: 未知关系 %r" % (where, name))
        for side in ("a", "b"):
            val = rel.get(side)
            if not isinstance(val, str) or not val.startswith("#"):
                problems.append("%s: relations.%s 必须是 # 开头的地址" % (where, side))
        if "tol" in rel and not isinstance(rel["tol"], (int, float)):
            problems.append("%s: relations.tol 必须是数字" % where)
        if name in RELATION_NEEDS_VALUE and "value" not in rel:
            problems.append("%s: 关系 %s 需要 value" % (where, name))
        if "value" in rel and not isinstance(rel["value"], (int, float)):
            problems.append("%s: relations.value 必须是数字" % where)
    for feat in expect.get("degraded", []):
        if not isinstance(feat, str):
            problems.append("%s: degraded 的每项必须是字符串" % where)
        elif not (feat in DEGRADED_OBS or feat.startswith(DEGRADED_PREFIXES)):
            problems.append("%s: degraded 特性 %r 形式非法" % (where, feat))
    if "snapshotAvailable" in expect and not isinstance(expect["snapshotAvailable"], bool):
        problems.append("%s: snapshotAvailable 必须是布尔" % where)
    if "minErrors" in expect and not isinstance(expect["minErrors"], int):
        problems.append("%s: minErrors 必须是整数" % where)


def validate_case(case, where):
    problems = []
    if not isinstance(case, dict):
        return ["%s: 用例必须是对象" % where]
    for key in case:
        if key not in CASE_KEYS:
            problems.append("%s: 未知字段 %s" % (where, key))
    for key in ("id", "title", "spec"):
        _req(problems, case, key, str, where)
    if _req(problems, case, "program", list, where):
        for line in case["program"]:
            if not isinstance(line, str):
                problems.append("%s: program 的每行必须是字符串" % where)
    if "loadExpect" in case:
        validate_expect(case["loadExpect"], where + ".loadExpect", problems)
    if _req(problems, case, "steps", list, where):
        if not case["steps"]:
            problems.append("%s: steps 不能为空" % where)
        for i, step in enumerate(case["steps"]):
            sw = "%s.steps[%d]" % (where, i)
            if not isinstance(step, dict):
                problems.append("%s: 步骤必须是对象" % sw)
                continue
            for key in step:
                if key not in STEP_KEYS:
                    problems.append("%s: 未知字段 %s" % (sw, key))
            if not (STEP_ACTIONS & set(step)):
                problems.append("%s: 必须至少含 send / fire / restart / expect 之一" % sw)
            if "send" in step:
                if not isinstance(step["send"], list) or not all(
                        isinstance(x, str) for x in step["send"]):
                    problems.append("%s: send 必须是字符串数组" % sw)
            if "fire" in step:
                fire = step["fire"]
                if not isinstance(fire, dict) or "target" not in fire or "event" not in fire:
                    problems.append("%s: fire 必须含 target 与 event" % sw)
            if "restart" in step and not isinstance(step["restart"], bool):
                problems.append("%s: restart 必须是布尔" % sw)
            if "interact" in step:
                act = step["interact"]
                if not isinstance(act, dict) or "target" not in act or "action" not in act:
                    problems.append("%s: interact 必须含 target 与 action" % sw)
                elif act["action"] not in INTERACTIONS:
                    problems.append("%s: interact.action 非法：%r" % (sw, act["action"]))
            if "snapshot" in step and not isinstance(step["snapshot"], bool):
                problems.append("%s: snapshot 必须是布尔" % sw)
            if "roundtrip" in step and not isinstance(step["roundtrip"], bool):
                problems.append("%s: roundtrip 必须是布尔" % sw)
            if "expect" in step:
                validate_expect(step["expect"], sw + ".expect", problems)
    for cap in case.get("capabilities", []):
        if not isinstance(cap, dict) or "name" not in cap:
            problems.append("%s: capabilities 的每项必须含 name" % where)
        elif cap.get("behavior", "return") not in BEHAVIORS:
            problems.append("%s: capability behavior 非法：%r" % (where, cap.get("behavior")))
    if "limits" in case and not isinstance(case["limits"], dict):
        problems.append("%s: limits 必须是对象" % where)
    if "seedState" in case and not isinstance(case["seedState"], dict):
        problems.append("%s: seedState 必须是对象" % where)
    for req in case.get("requires", []):
        if not isinstance(req, str):
            problems.append("%s: requires 的每项必须是字符串（能力名）" % where)
    for path in case.get("capabilityModules", []):
        if not isinstance(path, str):
            problems.append("%s: capabilityModules 的每项必须是字符串路径" % where)
    if "assetsDir" in case and not isinstance(case["assetsDir"], str):
        problems.append("%s: assetsDir 必须是字符串路径" % where)
    if "rendering" in case:
        r = case["rendering"]
        if not isinstance(r, dict):
            problems.append("%s: rendering 必须是对象（能力声明替身）" % where)
        else:
            for key in ("controls", "attributes", "animations", "icons"):
                if key in r and r[key] is not None and not isinstance(r[key], list):
                    problems.append("%s: rendering.%s 必须是数组或 null" % (where, key))
            for key in ("geometry", "snapshot", "headless"):
                if key in r and not isinstance(r[key], bool):
                    problems.append("%s: rendering.%s 必须是布尔" % (where, key))
    if "renderGeometry" in case:
        rg = case["renderGeometry"]
        if not isinstance(rg, dict):
            problems.append("%s: renderGeometry 必须是对象" % where)
        else:
            for addr, rect in rg.items():
                if not isinstance(addr, str) or not addr.startswith("#"):
                    problems.append("%s: renderGeometry 的键必须是 # 开头的地址" % where)
                elif not isinstance(rect, dict) or \
                        not {"x", "y", "width", "height"} <= set(rect):
                    problems.append("%s: renderGeometry[%s] 必须含 x/y/width/height"
                                    % (where, addr))
    return problems


# ---------------------------------------------------------------- 期望匹配

def deep_subset(expected, actual, path, problems):
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            problems.append("%s: 期望对象，实际 %r" % (path, actual))
            return
        for key, val in expected.items():
            if key not in actual:
                problems.append("%s.%s: 实际结果中缺失" % (path, key))
            else:
                deep_subset(val, actual[key], "%s.%s" % (path, key), problems)
        return
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            problems.append("%s: 期望 %d 项列表，实际 %r" % (path, len(expected), actual))
            return
        for i, (e, a) in enumerate(zip(expected, actual)):
            deep_subset(e, a, "%s[%d]" % (path, i), problems)
        return
    if expected != actual:
        problems.append("%s: 期望 %r，实际 %r" % (path, expected, actual))


def _any_subset(want, candidates):
    """候选中是否存在一个，使得 `want` 是它的（递归）子集。"""
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        probs = []
        deep_subset(want, cand, "", probs)
        if not probs:
            return True
    return False


# ---------------------------------------------------------------- 几何关系与降级

def _rect_edges(rect):
    x = float(rect["x"])
    y = float(rect["y"])
    return x, y, x + float(rect["width"]), y + float(rect["height"])


def eval_relation(spec, rects):
    """评估一条关系断言，返回不成立的原因（空列表 = 成立）。"""
    name = spec.get("rel")
    addr_a, addr_b = spec.get("a"), spec.get("b")
    ra, rb = rects.get(addr_a), rects.get(addr_b)
    if ra is None or rb is None:
        return ["节点 %s 没有几何" % (addr_a if ra is None else addr_b)]
    default_tol = 0.05 if name in ("width_ratio", "height_ratio") else DEFAULT_TOL
    tol = float(spec.get("tol", default_tol))
    ax0, ay0, ax1, ay1 = _rect_edges(ra)
    bx0, by0, bx1, by1 = _rect_edges(rb)
    problems = []

    def fail(desc, want=None, got=None):
        extra = "" if want is None else "（期望 %s，实际 %s）" % (want, got)
        problems.append("%s %s vs %s%s" % (desc, addr_a, addr_b, extra))

    if name == "left_of":
        if not ax1 <= bx0 + tol:
            fail("不在左侧")
    elif name == "right_of":
        if not ax0 >= bx1 - tol:
            fail("不在右侧")
    elif name == "above":
        if not ay1 <= by0 + tol:
            fail("不在上方")
    elif name == "below":
        if not ay0 >= by1 - tol:
            fail("不在下方")
    elif name == "contains":
        if not (ax0 <= bx0 + tol and ay0 <= by0 + tol
                and ax1 + tol >= bx1 and ay1 + tol >= by1):
            fail("不包含")
    elif name == "inside":
        if not (bx0 <= ax0 + tol and by0 <= ay0 + tol
                and bx1 + tol >= ax1 and by1 + tol >= ay1):
            fail("不在其内")
    elif name in ("overlaps", "disjoint"):
        intersects = (ax0 < bx1 - tol and bx0 < ax1 - tol
                      and ay0 < by1 - tol and by0 < ay1 - tol)
        if name == "overlaps" and not intersects:
            fail("不重叠")
        if name == "disjoint" and intersects:
            fail("重叠了")
    elif name == "aligned_x":
        if abs(ax0 - bx0) > tol:
            fail("左缘未对齐", bx0, ax0)
    elif name == "aligned_y":
        if abs(ay0 - by0) > tol:
            fail("上缘未对齐", by0, ay0)
    elif name == "aligned_center_x":
        if abs((ax0 + ax1) - (bx0 + bx1)) > 2 * tol:
            fail("水平未居中")
    elif name == "aligned_center_y":
        if abs((ay0 + ay1) - (by0 + by1)) > 2 * tol:
            fail("垂直未居中")
    elif name == "same_width":
        if abs((ax1 - ax0) - (bx1 - bx0)) > tol:
            fail("宽度不等", bx1 - bx0, ax1 - ax0)
    elif name == "same_height":
        if abs((ay1 - ay0) - (by1 - by0)) > tol:
            fail("高度不等", by1 - by0, ay1 - ay0)
    elif name == "same_size":
        if abs((ax1 - ax0) - (bx1 - bx0)) > tol or abs((ay1 - ay0) - (by1 - by0)) > tol:
            fail("尺寸不等")
    elif name == "gap_h":
        want = float(spec["value"])
        got = bx0 - ax1
        if abs(got - want) > tol:
            fail("水平间隙不符", want, got)
    elif name == "gap_v":
        want = float(spec["value"])
        got = by0 - ay1
        if abs(got - want) > tol:
            fail("垂直间隙不符", want, got)
    elif name in ("width_ratio", "height_ratio"):
        want = float(spec["value"])
        if name == "width_ratio":
            got = (ax1 - ax0) / (bx1 - bx0) if (bx1 - bx0) else 0.0
        else:
            got = (ay1 - ay0) / (by1 - by0) if (by1 - by0) else 0.0
        if abs(got - want) > tol:
            fail("比例不符", want, round(got, 3))
    return problems


def _declared(rendering, feature):
    """该特性是否被实现"声明支持"。

    列表为 `null` 或缺省 = 支持全部标准（这是给"懒得枚举"的实现的便利）；
    显式数组 = 只支持所列子集。布尔观测能力直接读同名键。
    """
    rendering = rendering or {}
    if feature in DEGRADED_OBS:
        return bool(rendering.get(feature))
    for prefix, key in (("control:", "controls"), ("attr:", "attributes"),
                        ("animation:", "animations"), ("icon:", "icons")):
        if feature.startswith(prefix):
            listed = rendering.get(key)
            return listed is None or feature[len(prefix):] in listed
    return False


def _has_degraded(diags, feature):
    for d in diags:
        if (isinstance(d, dict) and d.get("code") == "DEGRADED_FEATURE"
                and d.get("feature") == feature):
            return True
    return False


def _is_expected_degradation(diag, rendering):
    """未声明支持的特性产生的 DEGRADED_FEATURE 是**规范要求的行为**，不是缺陷。

    `noDiagnostics` 因此对它豁免——否则任何一个子集渲染器都过不了写死
    "零诊断"的用例，套件就不再渲染器无关（声明即 oracle 的另一半）。
    """
    if not isinstance(diag, dict) or diag.get("code") != "DEGRADED_FEATURE":
        return False
    feature = diag.get("feature")
    return bool(feature) and not _declared(rendering, feature)


def check_expect(expect, diags, snap, where, rendering=None, geometry_available=False):
    problems = []
    by_code = {}
    for d in diags:
        if isinstance(d, dict) and isinstance(d.get("code"), str):
            by_code.setdefault(d["code"], []).append(d)

    for want in expect.get("diagnostics", []):
        matched = False
        for have in by_code.get(want["code"], []):
            if all(have.get(k) == v for k, v in want.items()):
                matched = True
                break
        if not matched:
            problems.append("%s: 缺少诊断 %s" % (where, json.dumps(want, ensure_ascii=False)))
    for code in expect.get("forbid", []):
        if code in by_code:
            problems.append("%s: 出现了不应出现的诊断 %s" % (where, code))
    if "minErrors" in expect:
        n = sum(1 for d in diags if isinstance(d, dict) and d.get("level") == "error")
        if n < expect["minErrors"]:
            problems.append("%s: 需要至少 %d 条错误级诊断，实际 %d"
                            % (where, expect["minErrors"], n))
    if expect.get("noDiagnostics"):
        silent = [d for d in diags if not _is_expected_degradation(d, rendering)]
        if silent:
            problems.append("%s: 期望无诊断，实际 %s"
                            % (where, json.dumps(silent, ensure_ascii=False)))

    events = (snap.get("events") or []) if isinstance(snap, dict) else []
    for want in expect.get("events", []):
        if not _any_subset(want, events):
            problems.append("%s: 缺少事件 %s" % (where, json.dumps(want, ensure_ascii=False)))
    if expect.get("noEvents") and events:
        problems.append("%s: 期望无订阅事件，实际 %s"
                        % (where, json.dumps(events, ensure_ascii=False)))

    probes = (snap.get("probes") or []) if isinstance(snap, dict) else []
    for want in expect.get("probes", []):
        if not _any_subset(want, probes):
            problems.append("%s: 缺少探针结果 %s"
                            % (where, json.dumps(want, ensure_ascii=False)))

    nodes = snap.get("nodes", []) if isinstance(snap, dict) else []
    for nid in expect.get("nodes", []):
        if nid not in nodes:
            problems.append("%s: 节点 %s 不存在" % (where, nid))
    for nid in expect.get("nodesAbsent", []):
        if nid in nodes:
            problems.append("%s: 节点 %s 不应存在" % (where, nid))
    for key in ("attrs", "data", "flags", "slots", "rows"):
        if key in expect:
            deep_subset(expect[key], (snap or {}).get(key, {}), "%s.%s" % (where, key), problems)
    if "geometry" in expect and geometry_available:
        deep_subset(expect["geometry"], (snap or {}).get("geometry", {}) or {},
                    "%s.geometry" % where, problems)
    # 关系断言：几何可用 → 硬断言；不可用 → 只要求**可见降级**（静默才是失败）
    for spec in expect.get("relations", []):
        if geometry_available:
            rects = (snap or {}).get("geometry") or {}
            for detail in eval_relation(spec, rects):
                problems.append("%s: 关系 %s 不成立：%s"
                                % (where, spec.get("rel"), detail))
        elif not _has_degraded(diags, "geometry"):
            problems.append("%s: 实现未声明几何，却未产生 DEGRADED_FEATURE(feature=geometry)"
                            "——几何被静默省略" % where)
    # 声明即 oracle：未声明支持的特性必须出现可见降级；声明支持的则不要求降级
    # （谎报"支持"会被行为用例抓出来）。
    for feat in expect.get("degraded", []):
        if _declared(rendering, feat):
            continue
        if not _has_degraded(diags, feat):
            problems.append("%s: 未声明支持 %s，但未见 DEGRADED_FEATURE(feature=%s)"
                            % (where, feat, feat))
    if "snapshotAvailable" in expect:
        # 只断言"真的产出了图像"（非空），**不做像素比对**——像素级外观属实现自由
        got = bool((snap or {}).get("snapshotAvailable"))
        if got != bool(expect["snapshotAvailable"]):
            problems.append("%s: 截图可用性期望 %r，实际 %r"
                            % (where, expect["snapshotAvailable"], got))
    return problems


# ---------------------------------------------------------------- 实现进程

class Impl:
    def __init__(self, command, cwd=None):
        self.command = command
        self.cwd = cwd
        self.proc = None
        self.rendering = {}
        self.spawn()
        self.declare()

    def spawn(self):
        self.proc = subprocess.Popen(  # noqa: S602 - 命令由使用者提供
            self.command, shell=True, cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
        )

    def declare(self):
        """启动握手：读取实现声明的渲染能力——"声明即 oracle"的输入。

        未实现 `hello` 的旧实现会回一条 error；此时视为"未声明"，不影响非渲染
        用例；渲染用例会要求它对被请求的一切产生可见降级。
        """
        self.rendering = {}
        self.pointer_bad = ""
        try:
            resp = self.request({"op": "hello"})
        except Exception:  # noqa: BLE001 - 握手失败不致命，视为未声明
            return self.rendering
        if isinstance(resp, dict):
            self.rendering = resp.get("rendering") or {}
        # 宿主指针语义自述的**值域门**（05 第 10 节）：声明了就必须在值域里——
        # 自述不合法与"声明支持却做不到"同罪，都是可见失败。
        pointer = self.rendering.get("pointer", "mouse")
        if pointer not in ("mouse", "touch"):
            self.pointer_bad = ("hello.rendering.pointer = %r 不在值域 "
                                "{mouse, touch}" % (pointer,))
        return self.rendering

    def request(self, payload):
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("实现进程未响应即退出（stderr: %s）" % self._stderr())
        return json.loads(line)

    def _stderr(self):
        try:
            self.proc.terminate()
            _, err = self.proc.communicate(timeout=2)
            return (err or "").strip()[:400]
        except Exception:  # noqa: BLE001
            return ""

    def kill(self):
        try:
            if self.proc:
                self.proc.kill()
                self.proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    def restart(self):
        """一条用例把实现搞崩之后，重启它，避免污染后续用例。"""
        self.kill()
        self.spawn()
        self.declare()

    def close(self):
        try:
            self.request({"op": "quit"})
        except Exception:  # noqa: BLE001 - 退出协议失败不致命
            pass
        finally:
            self.kill()


def run_case(case, impl, wait_seconds):
    """返回不匹配项列表。每步的期望只针对该步产生的诊断。

    **声明即 oracle**：由启动握手（或因用例自带 `renderGeometry` 测试替身）得知
    几何是否可用，据此决定几何关系断言是**硬断言**，还是退化为**要求可见降级**。
    """
    problems = []
    # 能力声明：用例可用 `rendering` 替身覆盖实现的真实声明（声明即 oracle）。
    rendering = case.get("rendering") or impl.rendering or {}
    geometry_available = bool(rendering.get("geometry")) or bool(case.get("renderGeometry"))

    req = {"op": "load", "program": case["program"]}
    for key in ("capabilities", "limits", "seedState", "renderGeometry", "rendering"):
        if key in case:
            req[key] = case[key]
    if case.get("capabilityModules"):
        # 相对路径按 conformance/ 解析（用例不写本机绝对路径）
        req["capabilityModules"] = [
            p if os.path.isabs(p) else os.path.join(HERE, p)
            for p in case["capabilityModules"]]
    # 资源根目录（可选）：提供时实现会校验 `src` 指向的文件确实存在；同样按 conformance/ 解析
    assets_dir = case.get("assetsDir") or ""
    if assets_dir and not os.path.isabs(assets_dir):
        assets_dir = os.path.join(HERE, assets_dir)
    if assets_dir:
        req["assetsDir"] = assets_dir
    resp = impl.request(req)
    load_diags = list(resp.get("diagnostics", []))
    # 只有用例真的要断言装载期诊断时才观察一次：**没人监听时不消费观察流**，
    # 否则装载后立刻完成的异步诊断会被吸进装载期，步骤断言就看不到它了。
    if "loadExpect" in case:
        snap = impl.request({"op": "observe"})
        load_diags += snap.get("diagnostics", [])
        problems += check_expect(case["loadExpect"], load_diags, snap, "load",
                                 rendering, geometry_available)

    for i, step in enumerate(case["steps"], 1):
        where = "step %d" % i
        diags = []
        if "send" in step:
            diags += impl.request({"op": "send", "batch": step["send"]}).get("diagnostics", [])
        if "fire" in step:
            req = {"op": "fire"}
            req.update(step["fire"])
            diags += impl.request(req).get("diagnostics", [])
        if "interact" in step:
            req = {"op": "interact"}
            req.update(step["interact"])
            diags += impl.request(req).get("diagnostics", [])
        if step.get("restart"):
            diags = impl.request({"op": "restart"}).get("diagnostics", [])
        snapshot_image = None
        if step.get("snapshot"):
            snap_resp = impl.request({"op": "snapshot"})
            diags += snap_resp.get("diagnostics", [])
            # 只记"是否真的产出了图像"，不比对像素
            snapshot_image = bool(snap_resp.get("image"))
        if step.get("roundtrip"):
            # 往返：把当前程序**打印成源文本**，再原样重新装载。
            # 验证"命令批 → 打印 → 源文本 → 重载 → 行为等价"这条真源写回链路。
            # 装载参数与初始 load 完全一致，使唯一的变化就是"经了一轮打印与重解析"。
            dumped = impl.request({"op": "dump"}).get("program") or []
            req = {"op": "load", "program": dumped}
            for key in ("capabilities", "limits", "seedState", "renderGeometry",
                        "rendering", "capabilityModules"):
                if key in case:
                    req[key] = case[key]
            if assets_dir:
                req["assetsDir"] = assets_dir
            diags += impl.request(req).get("diagnostics", [])
        want = step.get("expect")
        if want is None:
            continue
        # 几何不可用但用例断言了关系时，驱动者先"问一次"几何（where 探针），
        # 给实现一个报出可见降级的机会——静默才是失败。
        rels = want.get("relations") or []
        if rels and not geometry_available:
            first = (rels[0].get("a") or "").lstrip("#")
            diags += impl.request({"op": "send", "batch": ["where #%s" % first]}) \
                          .get("diagnostics", [])
        deadline = time.time() + wait_seconds
        last = None
        seen_events = []
        seen_probes = []
        while True:
            snap = impl.request({"op": "observe"})
            diags += snap.get("diagnostics", [])
            # 事件与诊断一样是"自上次请求以来"的增量；轮询时累积，否则会被后一次观察清空
            seen_events += snap.get("events") or []
            seen_probes += snap.get("probes") or []
            view = dict(snap)
            view["events"] = seen_events
            view["probes"] = seen_probes
            view["snapshotAvailable"] = snapshot_image
            last = check_expect(want, diags, view, where, rendering, geometry_available)
            if not last or time.time() >= deadline:
                break
            time.sleep(0.05)
        problems += last
    return problems


# ---------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(description="Puppet 语言标准 conformance 运行器")
    ap.add_argument("--check", action="store_true", help="只校验用例文件，不需要实现")
    ap.add_argument("--impl", help="启动实现进程的命令")
    ap.add_argument("--impl-module", help="以 'python -m <模块>' 方式启动实现（无需处理引号）")
    ap.add_argument("--cases", default=DEFAULT_CASES, help="用例目录")
    ap.add_argument("--filter", default="", help="只跑 id 含该子串的用例")
    ap.add_argument("--wait", type=float, default=3.0, help="每步观察轮询上限（秒）")
    args = ap.parse_args(argv)

    cases, problems = load_cases(args.cases)
    if problems:
        print("用例文件有问题：")
        for p in problems:
            print("  - " + p)
        return 1
    if args.check:
        print("用例文件校验通过：%d 条用例，来自 %s"
              % (len(cases), os.path.relpath(args.cases, HERE)))
        return 0

    if args.impl_module:
        args.impl = '"%s" -m %s' % (sys.executable, args.impl_module)
    if not args.impl:
        print("需要 --check / --impl / --impl-module 之一。", file=sys.stderr)
        return 2

    selected = [(n, c) for n, c in cases if args.filter in c["id"]]
    impl = Impl(args.impl, cwd=os.path.dirname(HERE))
    failed = 0
    skipped = 0
    if getattr(impl, "pointer_bad", ""):
        # 握手自述不合法是**实例级**失败：不占用例计数，但同样让整轮变红。
        print("FAIL pointer  (宿主指针语义自述)")
        print("      " + impl.pointer_bad)
    try:
        for name, case in selected:
            # 能力门槛：实现未声明的能力 → **可见跳过**（不是静默通过）
            effective = case.get("rendering") or impl.rendering or {}
            missing = [c for c in case.get("requires", []) if not _declared(effective, c)]
            if missing:
                skipped += 1
                print("skip %s  (实现未声明：%s)" % (case["id"], ", ".join(missing)))
                continue
            try:
                issues = run_case(case, impl, args.wait)
            except Exception as ex:  # noqa: BLE001 - 单条用例失败不终止整轮
                issues = ["运行异常：%s" % ex]
                impl.restart()
            if issues:
                failed += 1
                print("FAIL %s  (%s)" % (case["id"], case["title"]))
                for it in issues:
                    print("      " + it)
            else:
                print("ok   %s" % case["id"])
    finally:
        impl.close()

    print("\n%d/%d 通过（跳过 %d）"
          % (len(selected) - failed - skipped, len(selected), skipped))
    return 1 if failed or getattr(impl, "pointer_bad", "") else 0


if __name__ == "__main__":
    raise SystemExit(main())
