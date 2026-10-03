# 04 · 词汇

## 1. 总则

- 词汇是**标准的一部分**：控件、属性、事件、图标名都由本规范**按语义**定义。
- **禁止**以"某个渲染后端不支持"为理由从标准中删除词汇；渲染器缺失支持时**必须**声明并产生可见降级（`DEGRADED_FEATURE`），**禁止**渲染成空白。
- 属性分两类：
  - **值类**：可以写字面量，也可以写表达式（构成活绑定，见 `03-semantics.md` 第 2 节）。
  - **引用类**：必须是指向程序中另一实体的地址引用或字面量，**禁止**写表达式（违反即 `REF_ATTR_EXPR`）。
- 未出现在本表内的属性名 → **警告** `UNKNOWN_ATTR`（带行号与近似建议），**禁止**静默丢弃。

## 2. 节点类型

| 类型 | 语义 | 备注 |
|---|---|---|
| `window` | 顶层窗口 / 页面 | 根节点的直接子节点 |
| `dialog` | **模态对话框**（覆盖层容器） | 铺满父的内容区；**可见时阻断下层交互**；见第 2.1 节 |
| `col` | 纵向容器 | 默认 `gap=8` |
| `row` | 横向容器 | 默认 `gap=8` |
| `navbar` | 顶部栏 | 标题 + 可选动作位 |
| `list` | 数据源驱动的重复容器 | 需要 `source` 与 `template` |
| `tabs` | 分页容器 | **每个直接子节点是一页**；`selected` 为页序号（从 0 起） |
| `template` | 行模板 | 页面内不渲染；见 `01-grammar.md` 第 8 节 |
| `text` | 文本 | — |
| `icon` | 图标 | `icon` 取语义名 |
| `divider` | 分隔线 | — |
| `spacer` | 弹性留白 | — |
| `progress` | 进度 | 不写 `value` 即"不确定态" |
| `image` | 图片 | `src` 指向的资源不存在 → **兜底可见**（不留白） |
| `avatar` | 头像 | `src` / `initials` / `icon` 三选一，兜底可见 |
| `button` | 按钮 | 可选 `icon` + `text`；默认圆角 8 |
| `input` | 单行输入 | 值走 `value` |
| `checkbox` | 勾选 | 值走 `value` |
| `switch` | 开关 | 值走 `value` |
| `slider` | 滑块 | `value` / `min` / `max` / `step` |
| `dropdown` | 下拉选择 | **选中值走 `selected`**；选项由 `options` 提供 |

### 2.1 模态对话框（`dialog`）

`dialog` 是**覆盖层容器**，与普通容器有三处不同：

| 方面 | 语义 |
|---|---|
| **布局** | **铺满父的内容区**——不参与兄弟的流动排布，也不占流动位置；其子节点在覆盖层内**纵向流动**（同 `col`） |
| **显隐** | 由运行期状态标志 `visible` 控制：`set #dlg visible=true` 打开、`false` 关闭。**隐藏的 `dialog` 不占位、不绘制、不阻断** |
| **模态** | **可见时，其子树之外的交互一律被阻断**（点击不穿透到下层）；**后声明的 `dialog` 在上**——多个叠加时，最上层是最后声明的那个可见者 |

- **`dialog` 不产生第二个根**：它与其它节点一样住在节点表里，父节点是普通容器（通常是 `window`）。
- **模态的实质是"阻断下层交互"，不是视觉效果**：只靠 `visible` 手写覆盖层做不到这件事——
  `visible` 只控制"画不画"，不控制"能不能点"，点击会**穿透**到下层控件。因此"删除前确认"
  这类交互**必须**由 `dialog` 承担。
- **渲染契约**：铺满、层叠、阻断这三条是**可被 conformance 断言的行为**（见 `05-render-contract.md`），
  不是"渲染器自行决定"。
- 声明支持 `dialog` 的渲染器**必须**实现阻断；未声明的渲染器走**可见降级**
  （`DEGRADED_FEATURE`，`feature=control:dialog`）。

## 3. 通用属性

### 3.1 盒模型（值类）

| 属性 | 语义 |
|---|---|
| `pad` | 内边距（单值或"上下 左右"两值） |
| `margin` | 外边距 |
| `bgcolor` | 背景颜色 |
| `gradient` | 渐变（两个颜色） |
| `radius` | 圆角 |
| `border` | 边框（"宽度 颜色"） |
| `shadow` | 阴影强度 |
| `opacity` | 不透明度 |

### 3.2 布局（值类）

| 属性 | 语义 |
|---|---|
| `gap` | 子节点间距（`col`/`row`/`list` 默认 8） |
| `justify` | 主轴分布 |
| `align` | 交叉轴对齐 |
| `wrap` | 是否换行 |
| `flex` | 弹性占比 |
| `scroll` | 溢出滚动方式 |
| `w` / `h` | 尺寸（不写则由内容决定） |
| `x` / `y` | 绝对位置（不参与正常流动布局） |
| `offset` | 相对偏移（在正常布局位置基础上平移） |
| `scale` | 缩放比例 |
| `rotate` | 旋转角度 |

### 3.3 排版（值类）

`fg`、`size`、`weight`、`italic`、`font`、`tooltip`。

- `window` 上的 `font` 与 `primary` 是**整页主题**（全局字体与主色）。

### 3.4 内容

| 属性 | 类别 | 语义 |
|---|---|---|
| `text` | 值类 | 文本内容 |
| `icon` | 值类 | 图标语义名（未知即 `UNKNOWN_ICON`） |
| `src` | 值类 | 资源地址：**资源目录内的相对路径**（**不支持远程**，见下"资源引用"） |
| `fit` | 值类 | 图片填充方式 |
| `initials` | 值类 | 头像文字兜底 |
| `size` | 值类 | 头像直径 |
| `value` | 值类 | 输入 / 选择 / 进度的当前值 |
| `selected` | 值类 | `dropdown` 的选中项、`tabs` 的页序号 |
| `min` / `max` / `step` | 值类 | 滑块边界与步长 |
| `placeholder` | 值类 | 占位提示 |
| `title` | 值类 | 窗口标题 |
| `source` | **引用类** | 数据源地址 |
| `template` | **引用类** | 模板地址 |
| `options` | **引用类** | 下拉选项的数据源地址 |
| `option_label` / `option_value` | 值类 | 选项在数据行中对应的字段名 |
| `as` | 声明 | 模板的绑定名（不是值，不参与求值） |
| `primary` | 值类 | 主题主色 |

**资源引用（`src`）的规则**：

- **只允许资源目录内的相对路径**；远程地址与绝对路径一律**错误**（`ASSET_REMOTE`）。
  理由：远程地址会引入网络依赖、隐私问题与一整套失败模式（超时 / 重试 / 缓存 /
  部分加载），而"可离线搭建"是真实需求。**禁用比"允许但不定义失败语义"诚实。**
- **资源文件不存在 → 错误**（`ASSET_MISSING`）：这是**引用断裂**——程序能装载，但那处
  资源永远显示不出来，属"静默的无效"。存在性校验需要**资源根目录**，由调用方（宿主）
  在装载时提供；不提供则跳过该项校验，语言实现**不做无依据的判断**。
- **渲染期可见性**：资源在装载后消失（例如文件被删）→ **兜底可见**，**不留白**。
  `image` 与 `avatar` 走**同一套**规则——不做"头像宽容、图片严格"的分裂。

### 3.5 状态标志（运行期属性，可读写）

`hover`、`focus`、`pressed`、`error`、`visible`、`disabled`。

- 读取：`#id.<标志>`；写入：`set #id <标志>=<布尔>`。
- `disabled=true` 的节点**不触发**交互事件（引擎层拦截，见 `03-semantics.md` 第 3.4 节）。
- `visible=false` 的节点**不占位**、不接收交互。

### 3.6 状态外观 `states`（值类，字典）

```
states={hover: {bgcolor: "#eef2ff"}, error: {border: "1 #dc2626"}, focus: {border: "2 #93c5fd"}}
```

- 键：状态名（`hover` / `focus` / `pressed` / `error`）。
- 值：**外观属性表**，允许的属性白名单见下。
- 白名单：`bgcolor`、`fg`、`gradient`、`border`、`radius`、`shadow`、`opacity`、`size`、`weight`、`italic`、`align`。
- 出现布局类属性（`pad` / `margin` / `gap` / `flex` / `w` / `h` / `x` / `y` / `scroll` / `justify` / `wrap`）→ **警告** `STATE_LAYOUT_ATTR`。
- 出现白名单外属性 → **警告** `STATE_UNKNOWN_ATTR`。
- 状态外观**不触发事件、不改写程序**。

### 3.7 动效

| 属性 | 类别 | 语义 |
|---|---|---|
| `animate` | 值类 | 参与过渡的属性名列表 |
| `duration` | 值类 | 过渡时长（毫秒），默认 200 |
| `curve` | 值类 | 缓动名，默认 `ease_out` |

- 未声明 `animate` 即**不做任何过渡**。
- 交互控件的状态过渡**可以**由实现默认开启（且必须在能力声明中可见）。

## 4. 控件专属属性汇编

- `window`：`title`、`w`、`h`、`bgcolor`（整窗底色）、`font` / `primary`（整页主题）。
- `list`：`source`、`template`。
- `tabs`：`selected`。
- `input`：`value`、`placeholder`。
- `checkbox` / `switch`：`value`。
- `slider`：`value`、`min`、`max`、`step`。
- `dropdown`：`selected`、`options`、`option_label`、`option_value`。
- `progress`：`value`（省略即不确定态）。
- `image`：`src`、`fit`、`radius`。
- `avatar`：`src`、`initials`、`icon`、`size`。

属性用于不适用的类型 → **警告** `ATTR_ON_TYPE`。

## 5. 事件名

| 事件 | 来源 |
|---|---|
| `click` | 可交互节点的点按 |
| `change` | 值变化（输入、勾选、开关、滑块、下拉） |
| `submit` | 输入框确认 |
| `focus` / `blur` | 获得 / 失去焦点 |
| `change`（数据源） | 数据源内容变化 |
| `pending` / `done` / `error` / `timeout` / `cancel` | 槽状态变化 |

未识别事件名 → **警告** `UNKNOWN_EVENT`。

### 5.1 主交互事件

| 控件 | 主交互事件 |
|---|---|
| `button` | `click` |
| `checkbox` / `switch` / `slider` / `dropdown` | `change` |

- 这些事件**既没有处理器、也没有被订阅、且其值无人读取**时 → **警告** `UNCOVERED_INTERACTION`：用户对它做的那个动作**不会有任何反应**——因为**没有任何人在看它**。
- 这不是"必须绑定"，而是"你漏了这件事必须被说出来"——因为"点了没反应"是最典型的静默失败。
- **两种"有人管"的情形，不得报警告**：
  - **被 `listen` 订阅**：外部驱动者（LLM 或调度者）会在观察流里看到并响应；
  - **值在别处被读取**：`#控件.value` / `#控件.selected` 出现在程序的任何表达式里——"提交时才读控件的值"是常见且完全正当的写法。
- **不在表内的两类控件**：
  - `input`：能打字本身不是需要被响应的动作。若应用确实需要，显式绑定 `change` 或 `submit`；
  - `tabs`：切换选项卡本身就有渲染效果（换显示哪一页），不需要处理器。

## 6. 可动画属性（语义定义）

按**语义类别**定义，不按任何后端的实现能力定义：

| 类别 | 属性 |
|---|---|
| 透明度 | `opacity` |
| 颜色 | `bgcolor`、`fg`、`gradient`、`border` |
| 尺寸 | `w`、`h`、`size`、`radius`、`pad`、`margin`、`gap` |
| 位置 | `x`、`y`、`align`、`justify`、`offset` |
| 变换 | `scale`、`rotate` |
| 值 | `value` |

`animate` 中出现表外属性 → **警告** `ANIMATE_UNKNOWN`。
渲染器**可以**不支持其中部分属性，但**必须**在能力声明中列出，并在实际降级时产生 `DEGRADED_FEATURE`。

## 7. 图标

### 7.1 规则

- 图标以**语义名**引用（如 `add`、`delete`、`search`）。
- 实现**必须**支持**核心子集**；**可以**支持更多。
- 未识别的图标名 → **警告** `UNKNOWN_ICON`（带近似建议）；**禁止**静默渲染为空白。

### 7.2 核心子集（必须支持）

```
add  remove  delete  edit  save  close  check  cancel  search  settings
home  menu  more  refresh  download  upload  share  copy  filter  sort
star  favorite  user  users  lock  unlock  mail  phone  calendar  clock
location  image  camera  play  pause  stop  file  folder  document  list
grid  chart  cart  payment  bell  warning  info  error  success  help
arrow_up  arrow_down  arrow_left  arrow_right  chevron_up  chevron_down
chevron_left  chevron_right  plus  minus  eye  eye_off  link  tag  flag
```

## 8. 内置函数

算术与比较不属函数。以下内置函数由标准定义：

| 函数 | 说明 |
|---|---|
| `count(x)` | 集合 / 列表长度（**普通函数**，不限于数据源） |
| `len(x)` | 字符串或集合长度 |
| `upper(s)` / `lower(s)` / `trim(s)` | 字符串处理 |
| `contains(a, b)` | 包含判定 |
| `num(x)` / `str(x)` | 显式类型转换 |
| `abs(x)` / `min(a, b)` / `max(a, b)` / `sum(xs)` / `round(x)` | 数值处理 |
| `join(xs, sep)` / `split(s, sep)` | 字符串与列表互转 |
| `at(xs, i)` / `first(xs)` / `last(xs)` / `slice(xs, a, b)` | 列表访问 |
| `keys(d)` / `values(d)` / `has(d, k)` | 字典访问 |
| `fmt(tpl, …)` | 格式化 |

- 函数集**可以扩充**（属兼容改动）。
- 未知函数 → **错误** `EXPR_UNKNOWN_FUNC`。
- 参数个数或类型不符 → **错误** `EXPR_TYPE`。
