"""词汇表：`spec/04-vocabulary.md` 的机器可读副本。

实现与规范共用一份词汇，避免"文档说一套、代码做一套"。
"""

from __future__ import annotations

NODE_TYPES = (
    "window", "col", "row", "navbar", "list", "tabs", "template",
    "text", "icon", "divider", "spacer", "progress", "image", "avatar",
    "button", "input", "checkbox", "switch", "slider", "dropdown",
)

STATE_FLAGS = ("hover", "focus", "pressed", "error", "visible", "disabled")

BOX_ATTRS = ("pad", "margin", "bgcolor", "gradient", "radius", "border", "shadow", "opacity")
LAYOUT_ATTRS = ("gap", "justify", "align", "wrap", "flex", "scroll", "w", "h",
                "x", "y", "offset", "scale", "rotate")
TYPO_ATTRS = ("fg", "size", "weight", "italic", "font", "tooltip")
CONTENT_ATTRS = ("text", "icon", "src", "fit", "initials", "value", "selected",
                 "min", "max", "step", "placeholder", "title",
                 "option_label", "option_value", "primary")
REF_ATTRS = ("source", "template", "options")
META_ATTRS = ("states", "animate", "duration", "curve")

ALL_ATTRS = frozenset(
    BOX_ATTRS + LAYOUT_ATTRS + TYPO_ATTRS + CONTENT_ATTRS + REF_ATTRS
    + META_ATTRS + STATE_FLAGS
)

# `states` 里允许出现的外观属性
STATE_APPEARANCE = frozenset(
    ("bgcolor", "fg", "gradient", "border", "radius", "shadow", "opacity",
     "size", "weight", "italic", "align")
)
# `states` 里出现这些属性即 STATE_LAYOUT_ATTR
# （注意：`align` 属于外观白名单，不在此列——规范 04 第 3.6 节）
STATE_APPEARANCE_LAYOUT = frozenset(
    ("pad", "margin", "gap", "flex", "w", "h", "x", "y", "scroll", "justify", "wrap")
)

# 只适用于特定类型的属性（用于 ATTR_ON_TYPE）
TYPE_ONLY_ATTRS = {
    "placeholder": ("input",),
    "selected": ("dropdown", "tabs"),
    "options": ("dropdown",),
    "option_label": ("dropdown",),
    "option_value": ("dropdown",),
    "min": ("slider", "progress"),
    "max": ("slider", "progress"),
    "step": ("slider",),
    "fit": ("image",),
    "initials": ("avatar",),
    "title": ("window", "navbar"),
    "source": ("list",),
    "template": ("list",),
}

INTERACTION_EVENTS = frozenset(("click", "change", "submit", "focus", "blur"))

# 控件的主交互事件：用户对它做的"那个动作"。
# 这些事件没有任何处理器时，用户操作它不会有任何反应——必须被说出来。
# `input` 刻意不在表内：能打字本身不是需要被响应的动作。
PRIMARY_EVENT = {
    "button": "click",
    "checkbox": "change",
    "switch": "change",
    "slider": "change",
    "dropdown": "change",
    # `tabs` 刻意不在表内：切换选项卡本身就有渲染效果（换显示哪一页），
    # 不需要处理器。见规范 04 第 5.1 节。
}

# 输入类控件"总有一个值"：未声明时也应有确定值，否则 `#id.value` 会报错，
# 而"没写 value"是极常见的情形（此问题由写真实示例暴露）。
VALUE_DEFAULTS = {
    "input": "", "checkbox": False, "switch": False, "slider": 0,
    "progress": None, "dropdown": None, "tabs": 0,
}


def default_value(node_type: str, field: str):
    """返回天然默认值；无默认则返回 None 并由调用方区分（用 _MISSING 哨兵）。"""
    if field == "value":
        return VALUE_DEFAULTS.get(node_type, _MISSING)
    if field == "selected" and node_type in ("dropdown", "tabs"):
        return 0
    return _MISSING


class _Missing:
    def __repr__(self):
        return "<无默认值>"


_MISSING = _Missing()

ANIMATABLE = frozenset((
    "opacity", "bgcolor", "fg", "gradient", "border",
    "w", "h", "size", "radius", "pad", "margin", "gap",
    "x", "y", "align", "justify", "offset", "scale", "rotate", "value",
))

EVENTS = frozenset(("click", "change", "submit", "focus", "blur",
                    "pending", "done", "error", "timeout", "cancel"))

# 需要 `as` 绑定名的类型
TEMPLATE_TYPES = frozenset(("template",))

# 内置函数：名字 → (最少参数, 最多参数)；None 表示不限
BUILTINS = {
    "count": (1, 1), "len": (1, 1),
    "upper": (1, 1), "lower": (1, 1), "trim": (1, 1),
    "contains": (2, 2), "num": (1, 1), "str": (1, 1),
    "abs": (1, 1), "min": (2, 2), "max": (2, 2), "sum": (1, 1), "round": (1, 2),
    "join": (2, 2), "split": (2, 2),
    "at": (2, 2), "first": (1, 1), "last": (1, 1), "slice": (3, 3),
    "keys": (1, 1), "values": (1, 1), "has": (2, 2),
    "fmt": (1, None),
}

# 必须支持的核心图标子集
CORE_ICONS = frozenset("""
add remove delete edit save close check cancel search settings
home menu more refresh download upload share copy filter sort
star favorite user users lock unlock mail phone calendar clock
location image camera play pause stop file folder document list
grid chart cart payment bell warning info error success help
arrow_up arrow_down arrow_left arrow_right chevron_up chevron_down
chevron_left chevron_right plus minus eye eye_off link tag flag
""".split())


def attr_kind(name: str) -> str:
    """返回值类 / 引用类。"""
    return "ref" if name in REF_ATTRS else "value"


def suggest_from(name: str, candidates) -> str:
    """给未知名字找一个近似候选（用于诊断建议）。"""
    best, score = "", 0.0
    for cand in candidates:
        common = len(set(name) & set(cand))
        ratio = common / max(len(set(name) | set(cand)), 1)
        if ratio > score:
            best, score = cand, ratio
    return best if score >= 0.5 else ""
