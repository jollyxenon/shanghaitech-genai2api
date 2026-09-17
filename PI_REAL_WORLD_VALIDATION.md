# Pi Agent 真实场景验证

日期：2026-09-17；环境：WSL Ubuntu，Pi 0.85.1，现有代理端口 31100。

**结论：四项功能都有可行调用路径，但不能认定为全部稳定、开箱即用。** 思考开关和联网搜索通过；图片识别正确但延迟与超时严重；文档需要区分普通文本、直接 PDF 附件和显式上传命令。

本次运行的是独立 Pi CLI 进程（JSON 事件模式及 RPC 模式），不是直接 HTTP 调用冒充 Pi。未检查交互式 TUI 的视觉排版。

## 隔离方式

- 使用独立 `PI_CODING_AGENT_DIR=/tmp/pi-genai-validation/agent` 与空白工作目录，不读取用户原有模型认证、插件配置或项目设置。
- 启用 `--no-extensions --no-skills --no-prompt-templates --no-themes --no-context-files --no-approve --offline --no-session`，禁用自动重试。
- 图片、思考和搜索用 `--no-tools`；文本阅读只开放内置 `read`；尝试自动上传文档时只开放内置 `bash`。没有 MCP、浏览器、搜索插件或上传插件参与。
- Pi 0.85.1 会无条件注册自身附带的 `llama.cpp` 管理模块，即使设置 `--no-extensions`，RPC `get_commands` 仍列出 `/llama`。核对实现后确认它只注册 provider 和管理命令，没有注册模型工具或消息事件处理；本次没有选择该 provider 或执行该命令。所有用户安装的插件均未加载，不能把此情形描述成“扩展对象数量严格为零”。
- 本地观察器仅记录请求字段和内容类型，然后原样转发给 31100，不添加搜索/思考参数、不改写回答。所有样本为本次自建，提示词中没有给出图片数字或发票答案。

隔离清单见 [isolation.json](validation/pi-2026-09-17/isolation.json)，请求字段证据见 [requests.jsonl](validation/pi-2026-09-17/requests.jsonl)。

## 实际结果

| 功能 / 场景 | Pi 与代理链路 | 结果 | 耗时 |
|---|---|---|---:|
| 图片：提取数字、左右图形和颜色 | `@inspection.png` → Chat / GLM | 正确识别 `817493`、左侧青绿色三角形、右侧暗红色矩形；正文混入未标记的英文分析 | 258.94 秒 |
| 图片协议对照 | 相同图片 → Anthropic / GLM，开启思考 | 上传成功，但上游 300 秒读取超时；Pi 收到 error | 300.91 秒 |
| 思考开启 | `--thinking high` → Chat / DeepSeek | 请求实际包含 `reasoning_effort: high`，Pi 收到 28 个 thinking 增量、211 字符独立思考；结果正确 | 2.17 秒 |
| 思考关闭 | `--thinking off` → Chat / DeepSeek | 请求实际包含 `reasoning_effort: none`，无 thinking 增量或块；结果正确 | 2.22 秒 |
| 文本文件提醒 | 原生 `read` → Responses / DeepSeek | Pi 实际执行一次 read，正确返回设备编号、项目、时间和负责人 | 1.62 秒 |
| 联网搜索 | 模型配置 `samplingParams.web_search: true` → Responses / DeepSeek | 正文给出图书馆官网、简介页、检索说明链接；服务日志确认补充来源 10 条；没有调用外部工具 | 36.99 秒 |
| 直接 PDF 附件 | `@invoice.pdf` → Responses / DeepSeek | Pi 将 PDF 原始字节按 UTF-8 放进文本，未发送 input_file；模型明确表示无法读取压缩内容，未猜测金额 | 1.57 秒 |
| PDF 自动调用工具 | 仅开放 bash，让模型执行上传脚本 | Responses 两次分别只输出计划、输出空正文；Anthropic 对照只有思考、没有执行 Bash。三次均未完成任务 | 1.12 / 50.98 / 8.14 秒 |
| PDF 显式命令上传 | Pi 原生 RPC `bash`（对应交互式 `!命令`）→ 上传脚本 → Responses，再由 Pi 总结 | 命令退出码 0；实际发送 input_file，平台解析 PDF；Pi 正确给出 `PI-704193`、数量 7、单价 38、运费 19，合计 `7×38+19=285 CNY` | 1.55 秒 |

思考题两次均答对：红箱 18、蓝箱 13、绿箱 10，共 250 件。普通正文中的算式解释不计入 thinking 字符数。

搜索的开关及返回链路通过，但结果质量仍有限：正文偏长，补充列表含其它学校资源和不相关采购公告；本轮没有逐条核验其时间戳与每个链接的内容，不能据此宣称全部引用准确。正文编号与独立补充列表仍不可直接配对。

图片上传仅约 30 毫秒，长等待发生在后续聊天请求；现有证据不足以继续确定是平台取图、排队还是模型执行导致。不能将这次成功归纳为稳定可用，也不能用另一次超时断言图片不受支持。

PDF 显式命令的成功证明了“Pi 原生 shell → 代理上传解析 → Pi 总结”可用；不证明 Pi 原生 PDF 附件已接通，也不证明模型自动工具调用可靠。

## 本轮暴露的运行问题

1. 首轮观察端口 31101 已被另一个现有服务占用，启动失败。该批结果无效，改用系统分配端口后重新执行，没有停止或修改占用该端口的服务。
2. 正式运行开始时，现有代理保存的上游登录令牌已失效。模型列表抛出 `TokenExpiredError`，Chat 表现为 HTTP 500，Responses 表现为流缺少终止事件，图片表现为“平台未提供图片服务地址”。重启现有 `genai2agent-upstream.service` 后模型列表恢复为 200 / 10 个模型，才执行上表场景。
3. 令牌过期在模型元数据、图片准备阶段的恢复和错误传播仍不完善。本轮只恢复了运行状态，没有修改业务代码来修复它。
4. 模型是否执行工具与客户端收到正常 stop 是两回事。空回复和只说计划均按未完成任务记录，不因 Pi 进程退出码为 0 判为成功。

## 无用户插件的复现配置

[models.example.json](validation/pi-2026-09-17/models.example.json) 已将地址设为真实代理的 31100 端口，不依赖本轮观察器。它是独立示例，没有覆盖用户全局配置。将其作为独立 Pi 配置目录中的 `models.json`，通过环境变量 `PI_GENAI_API_KEY` 提供代理密钥；未启用代理鉴权时，可设任意占位值。

模型配置中：

- `reasoning: true`、`input: ["text", "image"]` 显式声明客户端能力。
- Chat 的 `thinkingLevelMap.off: "none"` 使 Pi 的 off 真正发出关闭参数，high 映射为开启；不承诺上游有多档思考强度。
- `search` provider 使用 `openai-responses` 和 `samplingParams: {"web_search": true}`。这是 Pi 的模型请求参数配置，不是搜索插件。

启动时始终保留隔离参数，例如：

```bash
PI_CODING_AGENT_DIR=/tmp/pi-genai-validation/agent \
pi --no-extensions --no-skills --no-prompt-templates --no-themes \
   --no-context-files --no-approve --offline --no-session \
   --provider reasoning --model deepseek-pro --thinking high --no-tools \
   --mode json '仓库红、蓝、绿箱共41箱；红比蓝多5，绿比蓝少3，各有多少箱？'
```

PDF 的可行路径是在 Pi 中显式执行普通脚本，再提问。归档的 [document_query.py](validation/pi-2026-09-17/document_query.py) 只依赖 Python 标准库、直接调用 31100；它不是 Pi 插件，也不在本地提取 PDF 文本：

```text
!python3 /绝对路径/document_query.py /绝对路径/invoice.pdf "读取发票编号和金额并计算总额"
请依据刚才命令的输出，用中文整理发票核对结果。
```

本次通过 RPC 的 `bash`、`prompt` 命令执行了相同流程，实际 shell 输出与最终回答已归档。未验证 DOCX 在 Pi 中的独立入口，也未重新验证多轮图片、全部模型或交互式拖放。

## 可复查材料

- [全部 11 个场景的结果](validation/pi-2026-09-17/results.json)：包括失败和补充对照，不只保存成功结果。
- [请求字段记录](validation/pi-2026-09-17/requests.jsonl)：记录路径、模型、思考、搜索、工具及附件类型；不含认证头或 base64。
- [服务日志摘要](validation/pi-2026-09-17/service-observations.log)：图片/文档上传、功能开关、搜索来源和超时。
- [图片样本](validation/pi-2026-09-17/inspection.png)、[PDF 样本](validation/pi-2026-09-17/invoice.pdf)、[文本样本](validation/pi-2026-09-17/handover.txt)、[预期内容](validation/pi-2026-09-17/expected.json)。
- 原始 Pi 事件及临时执行脚本保留在 `/tmp/pi-genai-validation/`，未复制用户凭据文件。

本轮没有改动代理业务代码，没有安装或启用插件，也没有新增自动化测试。临时 Pi 进程和观察器在收尾时停止，用户原有插件配置保持原样。
