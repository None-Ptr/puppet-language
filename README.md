# Puppet 语言标准（发行名 `openpuppet-language`）

**An app is an agent.** 本仓是 **Puppet 语言标准**的规范与参考实现：不含任何具体渲染实现，
不含 app 宿主。

| 目录 | 内容 |
|---|---|
| `spec/` | **人读规范**（`2.0-draft`，未冻结）：01 词法语法 / 02 IR / 03 执行语义 / 04 词汇 / 05 渲染契约 / 06 诊断 |
| `conformance/` | **可执行用例集**：随发行包分发，任意实现用它自证合规 |
| `puppet/` | **参考实现**（零第三方依赖）：词法、IR、引擎、协议适配器、Tk 渲染器 |
| `docs/design-v2-draft.md` | 设计草案：决定"为什么" |
| `examples/` | 真实示例程序（`puppet validate examples --strict` 零诊断） |

## 安装

```bash
pip install openpuppet-language
```

Tk 参考渲染器（`puppet conformance --tk`，真实几何）需要 Tcl/Tk：
Debian/Ubuntu 需 `apt install python3-tk`。

## 命令

| 命令 | 作用 |
|---|---|
| `puppet spec` | 规范版本与位置 |
| `puppet validate <路径>` | 静态校验 `.puppet` 文件或目录（`--strict`：有任何诊断即失败） |
| `puppet conformance` | 跑合规用例集（`--tk` 改用 Tk 渲染器；`--filter <子串>` 过滤） |

只想校验用例文件本身、不需要任何实现：

```bash
python conformance/runner.py --check
```

## 参与

- **用例与实现都以 `spec/` 为准**：conformance 用例**从规范写**，不移植旧测试。
- **杜绝静默失败**：任何降级、丢弃、未实现都必须产生诊断或观察事件。
- **渲染器按能力声明行事**：启动握手（`hello`）声明支持范围，未声明支持的词汇被使用时
  必须产生 `DEGRADED_FEATURE`——标准不靠信任，靠用例抓。
