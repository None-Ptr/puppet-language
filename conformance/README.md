# Conformance 用例集

## 1. 目的

让**任何**实现自证符合 `spec/` 下的语言规范。本目录随 `puppet` 发行包分发；`puppethub` 的 CI 必须运行它。

**用例来自规范，不从实现反推。** 每条用例在 `spec` 字段登记它断言的规范位置。

## 2. 目录

```
conformance/
├── README.md
├── runner.py            # 零依赖运行器
└── cases/
    ├── grammar.json     # 词法与语法
    ├── binding.json     # 校验、绑定、传播
    ├── platform.json    # 能力、槽、持久化、收敛
    ├── probes.json      # 探针（tree / get / where）
    ├── templates.json   # 模板与行上下文
    ├── listen.json      # 订阅事件
    ├── interaction.json # 交互覆盖
    └── render.json      # 渲染：几何关系 / 能力声明 oracle / 可见降级
```

## 3. 运行

```bash
python conformance/runner.py --check
```

只校验用例文件本身是否符合本文件的模式，**不需要任何实现**。

```bash
python conformance/runner.py --impl "<启动实现的命令>"
python conformance/runner.py --impl "..." --filter b-cycle
```

驱动一个真实实现跑全部用例。

运行器在每步之后会轮询 `observe`，直到该步的期望满足或达到等待上限（默认 3 秒）。因此像"槽超时"这类异步用例**不需要**额外的同步手段。

每步的期望只针对**该步**（`send` / `fire` / `restart` / `snapshot`）产生的诊断；装载期诊断用 `loadExpect` 断言。

## 4. 实现协议

实现是一个**子进程**，通过标准输入/输出交换**行分隔 JSON**（每行一个 JSON 对象，UTF-8，无嵌套换行）。

### 4.1 请求

| 请求 | 字段 | 期望响应 |
|---|---|---|
| `hello` | — | `{"protocol": "...", "rendering": {…}}`（见 4.1.1） |
| `load` | `program`（字符串数组）、`capabilities`（数组，可选）、`capabilityModules`（字符串数组，可选）、`limits`（对象，可选）、`seedState`（对象，可选）、`renderGeometry`（对象，可选） | `{"diagnostics": [...]}` |
| `send` | `batch`（字符串数组） | `{"diagnostics": [...]}` |
| `fire` | `target`（地址）、`event`（事件名）、`row`（行序号，可选）、`value`（新值，可选） | `{"diagnostics": [...]}` |
| `observe` | — | 见 4.3 |
| `interact` | `target`（地址）、`action`（`click`/`change`/`submit`/`focus`/`blur`）、`value`（新值，可选） | `{"delivered": 布尔, "diagnostics": [...]}` |
| `snapshot` | — | `{"image": <base64 或 null>, "format": "png"}`（见 4.1.3） |
| `restart` | — | `{"diagnostics": [...]}` |
| `quit` | — | 进程退出 |

- `capabilities` 描述**测试替身**（实现按描述注册假能力，不得调用真实业务）：`{"name", "params":[{"name","type","required"}], "returns", "behavior"}`。`behavior` 取值：`return`（附 `value`）、`error`、`hang`（永不返回）、`nonjson`（返回不可序列化的值）、`delay`（附 `delayMs`）。
- `limits`：`{"callTimeoutMs": <整数>, "eventQueue": <整数>}`。后者限定观察流缓冲上限，用于验证"溢出不得静默丢弃"。
- `seedState`：预置的状态文件内容，形状与实现写出的状态一致：`{"revision": <整数>, "items": {"#数据源地址": [行, …]}}`。实现应**先**把它当作既有状态，再执行 `load`（用于持久化漂移类用例）。
- `capabilityModules` 描述**真实能力模块**（`.py` 文件路径，相对路径按 `conformance/` 解析）。
  实现必须从签名与类型注解提取契约，并履行三条义务：说明文本为空 → 错误 `CAP_NO_DOC`
  且**不予注册**；依赖声明与实际 import 不一致 → 警告 `CAP_DEPS_MISMATCH`；模块加载失败 →
  错误 `CAP_IMPORT`。真实能力与替身走**同一份契约形状**，因此行为用例对二者同样适用
  （`conformance/fixtures/` 内置四个测试用模块）。
- `restart`：模拟进程重启——内存状态丢弃，持久分区从状态文件恢复。
- `fire.row`：**派发行内事件时必须给出**。行内事件的绑定名只能由"第几行"确定（规范 01 第 8.1 节）；省略 `row` 而目标又在模板内，属于驱动者的错误用法。
- `fire.value`：**`change` 事件必须给出**——它是"用户改成了什么"。事件载荷 `{value: …}` 由此而来（规范 03 第 2.8 节）；不给，处理器就无从知道新值。

### 4.1.1 能力声明握手（`hello`）

运行器在 spawn 出实现之后、跑任何用例之前，先发一次 `hello`，读回实现声明的**渲染能力**。
这是"**声明即 oracle**"的输入：conformance 用声明决定每条渲染期望是**硬断言**，还是退化为
**要求可见降级**（见第 6 节）。

```json
→ {"op": "hello"}
← {"protocol": "1", "rendering": {
     "controls":   ["window", "col", "row", "button"],
     "attributes": ["gap", "flex", "bgcolor"],
     "animations": ["opacity", "bgcolor"],
     "icons":      ["add", "delete"],
     "geometry": false, "snapshot": false, "headless": true,
     "notes": "……可选的人读说明……"
   }}
```

| 字段 | 类型 | 含义 |
|---|---|---|
| `controls` | 数组或 `null` | 支持的节点 / 控件类型；`null` = 支持全部标准类型 |
| `attributes` | 数组或 `null` | 支持的属性名；`null` = 全部标准属性 |
| `animations` | 数组或 `null` | 支持过渡的属性；`null` = 全部标准可动画属性 |
| `icons` | 数组或 `null` | 支持的图标语义名；`null` = 核心子集 |
| `geometry` | 布尔 | 是否提供几何观察（`observe.geometry`） |
| `snapshot` | 布尔 | 是否支持 `snapshot` 请求 |
| `headless` | 布尔 | 是否可**无 GUI** 驱动（`puppethub` 对 `puppetOS` 的唯一义务） |

- 未实现 `hello` 的实现会回一条 `error`；运行器将其视为**未声明**（`rendering = {}`），不影响
  非渲染用例；渲染用例随后会要求它对一切被请求的特性**可见降级**。
- **谎报**（声明支持却做不到）由行为用例抓出——标准不靠信任。

### 4.1.2 测试替身：几何与能力声明

- `observe` 的响应**可以**含可选字段 `geometry`（见 4.3）：地址 → `{x, y, width, height}`，以
  窗口内容区左上角为原点、**浮点像素**。**仅当 `rendering.geometry` 为真时该字段出现**；否则
  字段缺席——实现必须在被 `where` 询问时产生 `DEGRADED_FEATURE(feature=geometry)`，**禁止**静默。
- **观测替身**：为了在没有真实渲染器时**确定性地**验证关系断言，`load` **可以**带
  `renderGeometry`（对象：地址 → 矩形）。存在时它**就是真值**：实现照它返回 `observe.geometry`
  与 `where` 结果，且**不**报几何降级——即使实现自己有真实几何（替身优先，与 `capabilities`
  同一待遇）。这是"观测面测试替身"：一个替身业务能力，一个替身观测能力。
- **声明替身**：`load` 还**可以**带 `rendering`（形状同 4.1.1 的 `rendering`），**覆盖**实现的
  真实声明。实现必须**双向按声明行事**：
  - 声明里没有的词汇被程序使用 → 必须 `DEGRADED_FEATURE`（哪怕实现其实支持）；
  - 声明 `geometry: false` → 必须表现为无几何（哪怕实现其实量得到）。
  这让"未声明 → 必须降级"的 oracle 路径可以**确定性**地用例化，而不依赖某个实现恰好缺什么。
- 几何来源优先级：**观测替身 > 声明替身（geometry:false 即无） > 真实测量**。
- 参考实现两枚：`python -m puppet.adapter`（无渲染器，词汇全声明、geometry/snapshot=false、
  headless=true）与 `python -m puppet.tk_adapter`（Tk 渲染器，geometry=true、词汇子集声明——
  未声明词汇真实降级）。同一套用例必须两者全绿，这正是"渲染器无关"的自证。

### 4.1.3 视觉快照（`snapshot`）

- `snapshot` 是**可选**请求，**仅供驱动者自检**；其结果**不进断言**（自检归驱动者，见设计草案第 5 节）。
- 支持 → `{"image": "<base64 PNG>", "format": "png"}`；不支持 → `{"image": null, …}` **并**产生
  `DEGRADED_FEATURE(feature=snapshot)`。
- 用例以 `expect.degraded: ["snapshot"]` 表达"若不支持就必须可见降级"（见第 5 节）。

### 4.1.4 用户动作投递（`interact`）

- `fire`（4.1）**直接派发进引擎**——它验证语义，**不验证渲染器**。`interact` 才是"用户动作"：
  实现**必须**把它投递给**真实部件**，由渲染器自己的事件绑定翻译成引擎事件
  （用户动作 → 部件事件 → 渲染器 → `fire` → 处理器）。规范 05 第 4 节要求这条路径可观察。
- 投递不了（如没有渲染层）**必须**返回 `{"delivered": false}` **并**产生
  `DEGRADED_FEATURE(feature=interaction)`；**禁止**假装送达。
- 用例用 `requires: ["interaction"]` 表达"这条用例要求实现能投递用户动作"；实现未声明该能力时
  运行器**可见跳过**（打印 `skip`），**不是**静默通过。

### 4.2 诊断

每条诊断是一个对象，**必须**含 `code` 与 `level`（`error` / `warning` / `info`），**应该**含 `line`、`message`。

### 4.3 观察面

```json
{
  "events": [{"target": "#b", "event": "click"}],
  "nodes": ["#root", "#win"],
  "attrs": {"#cnt.text": "共 1 条"},
  "data":  {"#todos": [{"text": "a", "done": false}]},
  "flags": {"#b.error": true},
  "slots": {"#slot": {"status": "done", "value": {"ok": true}, "seq": 1}},
  "rows":  {"#items": [{"#label": {"text": "a"}}]},
  "geometry": {"#a": {"x": 16, "y": 16, "width": 80, "height": 40}}
}
```

| 字段 | 含义 |
|---|---|
| `diagnostics` | **自上一次请求以来**新产生的诊断（异步观察的主要通道） |
| `events` | **自上一次请求以来**的订阅事件（只有被 `listen` 订阅的才推送）；载荷 `{target, event, row?}` |
| `probes` | **自上一次请求以来**的探针结果；形状 `{verb, target, result}`（`result` 为 `null` 表示目标不存在） |
| `nodes` | 当前存在的节点地址 |
| `attrs` | **求值后**的属性值，键为 `#地址.属性名` |
| `data` | 数据源的数据项 |
| `flags` | 运行期状态标志，键为 `#地址.标志名` |
| `slots` | 槽的状态、值、序号 |
| `rows` | 列表行：列表地址 → 各行（行内以模板子地址为键） |
| `geometry` | **可选**几何快照：地址 → `{x, y, width, height}`。仅当 `rendering.geometry` 为真时出现 |

## 5. 用例模式

```json
{
  "id": "b-recompute",
  "title": "值类属性随数据源变化自动重算",
  "spec": "03-semantics.md 第 2 节",
  "program": ["add #root window #win", "..."],
  "capabilities": [],
  "limits": {},
  "seedState": {},
  "loadExpect": { "diagnostics": [], "forbid": [] },
  "steps": [
    { "send": ["append #todos item={text: \"a\"}"],
      "expect": { "diagnostics": [], "attrs": {"#cnt.text": "共 1 条"} } },
    { "restart": true, "expect": { "data": {"#todos": []} } }
  ]
}
```

| 字段 | 必需 | 说明 |
|---|---|---|
| `id` | 是 | 全局唯一 |
| `title` | 是 | 人读标题 |
| `spec` | 是 | 断言的规范位置 |
| `program` | 是 | 程序文本（行数组） |
| `loadExpect` | 否 | 装载后的期望 |
| `steps` | 是 | 至少一步；每步**必须**含 `send` / `fire` / `restart` / `snapshot` / `expect` 之一 |
| `capabilities` / `limits` / `seedState` / `renderGeometry` | 否 | 见第 4 节 |
| `capabilityModules` | 否 | 真实能力模块（`.py` 路径，相对 `conformance/`）；见第 4.1 节 |
| `rendering` | 否 | 能力声明**替身**，覆盖实现自身的声明（见 4.1.2） |
| `requires` | 否 | 本用例要求的能力（如 `["interaction"]`）；实现未声明时**可见跳过** |

**期望对象**（`loadExpect` 与每步的 `expect` 同构）。运行器**只在用例声明了 `loadExpect` 时**才在装载后额外观察一次——没人监听时不消费观察流，否则装载后立刻完成的异步诊断会被吸进装载期，步骤断言就看不到它了：（`loadExpect` 与每步的 `expect` 同构）：

| 键 | 语义 |
|---|---|
| `diagnostics` | **必须包含**：列表中的每一项都是某条实际诊断的子集（按 `code` 等键比对） |
| `forbid` | **必须不含**：这些码不得出现在实际诊断中 |
| `events` | **必须包含**：列表中的每一项都是某个实际订阅事件的子集 |
| `noEvents` | 期望**没有**订阅事件 |
| `probes` | **必须包含**：列表中的每一项都是某个实际探针结果的子集（按 `verb` 比对） |
| `nodes` / `attrs` / `data` / `flags` / `slots` / `rows` | 实际观察结果的**子集**必须匹配（递归子集：字典按键子集，列表要求等长逐项匹配） |
| `geometry` | `observe.geometry` 的**子集**必须匹配（仅在几何可用时比对；见第 6 节） |
| `relations` | **必须成立**的关系断言列表：`{"rel", "a", "b", "tol?", "value?"}`；词汇见表（第 6 节）。几何可用 → 硬判定；不可用 → 退化为要求 `DEGRADED_FEATURE(feature=geometry)` |
| `degraded` | **声明即 oracle**：列出的特性若**未被实现声明支持**，实际诊断中必须出现对应的 `DEGRADED_FEATURE(feature=…)`；已声明支持则不要求 |
| `note` | 说明文字，不参与比对 |

## 6. 声明即 oracle（渲染 / 几何分档）

渲染契约（`spec/05-render-contract.md` 第 1 节）把"支持范围"交给实现**声明**，把"缺失必须可见"
交给 `DEGRADED_FEATURE`。conformance 把两者闭环——用例**从规范写**，一套用例同时约束
"能做"与"不能做却不说"两种实现：

| 实现声明 | 用例断言 | 运行器行为 |
|---|---|---|
| `geometry: true`（或用例带 `renderGeometry`） | `relations` / `geometry` | **硬断言**：按 `observe.geometry` 判定，不成立即 FAIL |
| `geometry: false` | `relations` | 退化为**诚实性检查**：运行器先发一次 `where` 探针，要求出现 `DEGRADED_FEATURE(feature=geometry)`；**静默即 FAIL** |
| 未声明支持 `<特性>` | `degraded: ["<特性>"]` | 要求出现对应的 `DEGRADED_FEATURE(feature=…)` |
| 已声明支持 `<特性>` | `degraded: ["<特性>"]` | **不**要求降级（谎报"支持"由行为用例抓出） |
| `interaction: false` | `requires: ["interaction"]` | **可见跳过**（打印 `skip`），不是静默通过 |

两条配套规则，缺一不可：

- **`noDiagnostics` 豁免**：未声明支持的特性产生的 `DEGRADED_FEATURE` 是**规范要求的行为**，
  不是缺陷——期望"零诊断"的用例对它豁免。否则任何一个子集渲染器都过不了写死"零诊断"的
  用例，套件就不再渲染器无关。
- **声明替身**（`load.rendering`）：让"未声明 → 必须降级"的路径**确定性**用例化（见 4.1.2），
  不必依赖某个实现恰好缺什么词汇。

几何关系词汇（与规范 05 第 8 节一一对应）。每项形如
`{"rel": "…", "a": "#x", "b": "#y", "tol": 1, "value": 8}`；`tol` 缺省 1（像素），
比例关系缺省 0.05。全部为**定性 / 相对**断言，**不比较绝对坐标**——字号、DPI、窗口尺寸
不同不应导致断言不可移植：

| 类别 | 关系 |
|---|---|
| 方位 | `left_of` `right_of` `above` `below` |
| 包含 | `contains` `inside` |
| 重叠 | `overlaps` `disjoint` |
| 对齐 | `aligned_x` `aligned_y` `aligned_center_x` `aligned_center_y` |
| 尺寸 | `same_width` `same_height` `same_size` |
| 间距 | `gap_h` `gap_v`（`value` = 期望间隙 ± `tol`） |
| 比例 | `width_ratio` `height_ratio`（`value` = 期望比值） |

## 7. 覆盖范围

覆盖：词法、语法、校验、IR 结构、绑定与传播、事件、能力与槽、持久化、收敛、诊断码，
以及**渲染的可观察行为**——几何关系（无真实渲染器时经 `renderGeometry` 替身确定性验证；
有真实渲染器时直接走上真实布局）、能力声明 oracle、词汇 / 几何 / 截图的可见降级
（`cases/render.json`）、观察流溢出标记（`p-observation-dropped`，靠 `limits.eventQueue`
把时序问题转化为确定性的批量溢出）、**真实能力层**（加载 / docstring 强制 / 依赖声明核对 /
超时 / 取消 / 返回值与参数契约，见 `cases/capabilities.json`）、`tabs` 的页结构与 `selected`
语义（`cases/tabs.json`）、经渲染器的用户动作投递（`cases/interaction.json` 的 `ir-*`）。

**不覆盖**：像素级视觉呈现（字体度量、抗锯齿、阴影质量、动效曲线的精确形状）。这些属实现自由
（规范 05 第 9 节），同一实现在等价输入下一致即可；按规范纪律，本套件**不**对像素做黄金图像比对。

**已知未覆盖项**：`tabs` 的"非选中页不占位"尚未用例化——它需要真实渲染器的几何（声明替身给不出
"随 `selected` 切换而变化的矩形"）；待第二个几何渲染器出现后补入，届时还能交叉验证两个实现。
