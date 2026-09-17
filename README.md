# GenAI2OpenAI

将上海科技大学 GenAI 平台接入 Claude Code、Codex CLI 等客户端的代理服务，同时提供 Anthropic Messages、OpenAI Chat Completions 与 OpenAI Responses 三种接口。

A proxy that connects ShanghaiTech's GenAI platform to Claude Code, Codex CLI and other clients, exposing the Anthropic Messages, OpenAI Chat Completions and OpenAI Responses APIs.

## 快速开始

### 1. 安装

```bash
pixi install
```

### 2. 配置环境变量

在项目根目录创建 `.env`（可复制 `.env.example`），例如：

```dotenv
GENAI_TOKEN=学号@密码
PORT=31100
API_FORMAT=both
```

`GENAI_TOKEN` 也可以填写 JWT。`.env` 已被 Git 忽略，不会提交凭据；图片上传所需的令牌由代理从前端脚本自动获取，无需手工配置。

### 3. 启动代理

配置 `.env` 后可直接启动：

```bash
pixi run serve
```

命令行参数可以覆盖 `.env` 中的设置：

```bash
pixi run serve --port 31200 --api-format anthropic
```

也可以直接运行任意 `pixi run python main.py ...` 命令。

手动获取 JWT：前往 [GenAI 对话平台](https://genai.shanghaitech.edu.cn/)，F12 打开 Network，发送消息后从 `chat` 请求头中复制 `x-access-token`。

### 4. 配置 Claude Code

设置环境变量（写入 `~/.bashrc` 或 `~/.zshrc`）：

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:31100"
export ANTHROPIC_AUTH_TOKEN="local-proxy"   # 如果设了 --api-key，改成同一个值
export ANTHROPIC_MODEL="chatglm"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="MiniMax-M1"
export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-pro"
export ANTHROPIC_DEFAULT_OPUS_MODEL="chatglm"
export ANTHROPIC_REASONING_MODEL="MiniMax-M1"
```

### 5. 配置项目设置（减少权限弹窗）

在项目根目录创建 `.claude/settings.json`：

```json
{
  "permissions": {
    "allow": [
      "Bash(pixi run *)",
      "Bash(python *)",
      "Bash(curl *localhost*)"
    ]
  }
}
```

### 6. 启动

```bash
claude
```

### 7. 配置 Codex CLI

Codex CLI 默认走 OpenAI Responses API（`wire_api = "responses"`），编辑 `~/.codex/config.toml`：

```toml
model = "chatglm"                 # 必须填 GenAI 平台真实的模型 id（aiType），代理不做映射
model_provider = "genai"
disable_response_storage = true   # 代理不提供服务端会话存储

[model_providers.genai]
name = "ShanghaiTech GenAI"
base_url = "http://127.0.0.1:31100/v1"   # 注意要带 /v1，Codex 会请求 {base_url}/responses
wire_api = "responses"
```

可选：`model_reasoning_effort` 会映射成 GenAI 的思考开关（`none` 关闭，其余受支持值开启），不代表平台支持不同推理档位。上游返回的思考内容会以 `response.reasoning_summary_text.delta` 事件透传；`model_reasoning_summary` 不改变上游输出。

如果你想改用 Chat Completions 接口，把 `wire_api` 改成 `"chat"` 即可。

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--token` | JWT 令牌或 `学号@密码`（也可写入 `.env` 的 `GENAI_TOKEN`） | — |
| `--port` | 服务监听端口（也可写入 `.env` 的 `PORT`） | `31100` |
| `--api-key` | 客户端认证密钥（也可通过 `API_KEY` 环境变量设置） | 无 |
| `--api-format` | `openai`、`anthropic` 或 `both`（也可写入 `.env` 的 `API_FORMAT`） | `both` |
| `--debug` | 启用详细日志输出 | 关闭 |

## 特性

- **Claude Code 兼容** — 提供 Anthropic Messages API，支持 `tool_use/tool_result` 转换与扩展思考（thinking）块，可直接接入 Claude Code
- **Codex CLI 兼容** — 提供 OpenAI Responses API，支持 `function_call`、`custom_tool_call`（如 apply_patch）与 reasoning summary 事件
- **OpenAI 兼容** — 同时提供 OpenAI Chat Completion API，支持 Cursor、Continue 等客户端
- **Tool Calling** — 通过 prompt 注入实现 function calling，兼容不原生支持 function calling 的模型
- **思维链透传** — 上游 `reasoning_content` 在三个接口里分别以 `reasoning_content`、thinking 块、reasoning 事件输出
- **自动登录与刷新** — 学号密码模式通过 CAS 自动登录，JWT 过期静默刷新
- **动态模型列表** — 自动从 GenAI 平台拉取可用模型，`/v1/models` 额外返回图片、文档、搜索、思考开关的 `capabilities`
- **图片与文档输入** — 三种 API 都支持 URL/base64 图片、内联或 URL 文档；保留历史图片，解析历史文档以延续上下文
- **联网搜索** — 映射网页的 `netGo`，保留正文来源链接，并追加独立标注的补充检索结果
- **思考开关** — 支持 `thinking` 布尔值及各客户端的推理参数映射，按上游实际返回透传思考内容

## 图片、文档、联网搜索与深度思考

三种 API 共用附件上传及功能参数处理，流式和非流式均可使用。模型是否返回思考、是否能读取图片仍取决于上游；代理不生成或伪造思考内容。

### 控制参数

| 功能 | Chat Completions | Anthropic Messages | OpenAI Responses |
|---|---|---|---|
| 开启思考 | `"thinking": true` 或 `"reasoning_effort": "high"` | `"thinking": {"type": "enabled", "budget_tokens": 1024}`，也支持 `adaptive` | `"reasoning": {"effort": "high"}` |
| 关闭思考 | `"thinking": false` 或 `"reasoning_effort": "none"` | `"thinking": {"type": "disabled"}` | `"reasoning": {"effort": "none"}` |
| 开启搜索 | `"web_search": true` 或 `"web_search_options": {}` | `"web_search": true` 或声明 `web_search_20250305` 等搜索工具 | `"web_search": true` 或 `"tools": [{"type": "web_search"}]` |

三个接口均支持扩展字段 `thinking: true/false` 和 `web_search: true/false`。显式布尔值优先于其他映射；未传思考参数时保留上游默认行为。`minimal/low/medium/high/xhigh` 均只映射为开启，不能控制真实思考档位；`budget_tokens` 也不限制平台思考预算，会记日志说明。

搜索工具声明在这里用于启用平台搜索，**不实现 OpenAI/Anthropic 原生托管搜索工具的事件生命周期、工具结果块、地域筛选或搜索次数预算**。`tool_choice: "none"`（Anthropic 的 `{"type":"none"}` 同理）会关闭由工具声明启用的搜索；显式 `web_search: true` 仍然生效。

例如，通过 Chat 同时开启搜索和思考：

```bash
curl http://127.0.0.1:31100/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "deepseek-pro",
    "messages": [{"role": "user", "content": "查找上海科技大学图书馆官网并简要介绍"}],
    "thinking": true,
    "web_search": true,
    "stream": true
  }'
```

### 图片与文档格式

| 输入 | Chat Completions 内容块 | Anthropic 内容块 | Responses 内容块 |
|---|---|---|---|
| 图片 URL / data URL | `{"type":"image_url","image_url":{"url":"https://… 或 data:image/png;base64,…"}}` | `{"type":"image","source":{"type":"url","url":"https://…"}}`，或 `source.type=base64`、`media_type`、`data` | `{"type":"input_image","image_url":"https://… 或 data:image/png;base64,…"}` |
| 内联文档 | `{"type":"file","file":{"filename":"note.pdf","file_data":"base64 或 data URL"}}` | `{"type":"document","title":"note.pdf","source":{"type":"base64","media_type":"application/pdf","data":"base64"}}` | `{"type":"input_file","filename":"note.pdf","file_data":"data:application/pdf;base64,…"}` |
| 文档 URL | `{"type":"file","file":{"file_url":"https://…/note.pdf"}}` | `{"type":"document","source":{"type":"url","url":"https://…/note.pdf"}}` | `{"type":"input_file","file_url":"https://…/note.pdf"}` |

Anthropic 还支持文本型文档 `source: {"type":"text","data":"文档正文"}`。只发送附件、没有文字时，代理会补上“请分析所附内容。”作为当前问题。

下面的 Python 示例通过 Chat 同时发送本地图片和文档；将文件路径换成自己的文件即可：

```python
import base64
from pathlib import Path
import requests

image = base64.b64encode(Path("picture.png").read_bytes()).decode()
document = base64.b64encode(Path("note.pdf").read_bytes()).decode()
response = requests.post(
    "http://127.0.0.1:31100/v1/chat/completions",
    json={
        "model": "chatglm",
        "thinking": True,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "结合图片与文档回答我的问题"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                {"type": "file", "file": {"filename": "note.pdf", "file_data": document}},
            ],
        }],
    },
    timeout=360,
)
response.raise_for_status()
print(response.json()["choices"][0]["message"])
```

如配置了代理 `--api-key`，请求还需添加 `Authorization: Bearer <你的代理密钥>`。

图片直接交给上游读取：base64 图片以 data URL、网络图片以原始 URL 放入 `imageUrls`，不需要经过图片服务上传，也不需要 `width` / `height`。历史图片按网页协议保留在各自消息的多模态内容块中，合并文本块放在图片块之前；实测多轮图片追问可在 0.3 秒内正确回答。当前轮文档先上传并通过同一会话的 `fileIds` 引用；历史文档使用平台解析结果恢复为该轮的文本内容。客户端应在每次请求中发送完整历史及附件，不依赖平台网页会话自动续接。

单张内联图片须小于 20 MiB（base64 会使请求体约为图片的 4/3）；文档须小于 10 MiB，TXT、文本型 PDF 和 DOCX 已完成实际上传解析验证。扫描 PDF、表格及其他格式由平台解析器决定效果。每次请求会重新处理所带附件，暂不提供附件缓存、`/v1/files` 存储或 `file_id` 引用接口。图片与文档由平台侧解析，个别请求可能较慢（实测图片 0.3–1.8 秒，Anthropic 路径有过一次 27.5 秒），代理不会把慢当成失败，仍按 300 秒上游超时处理。

平台搜索正文与额外来源接口的编号可能不同，因此补充列表会单独标为“补充检索结果（独立于正文引用编号）”，不会伪造 `url_citation` 对应关系。补充检索失败时保留已经返回的正文与链接，并记录 warning。

日志会记录思考开关、搜索开关、附件数量、上传字节数、解析文本长度和准备耗时；不记录附件 base64、文档正文或上传凭据。文本 token 用量仍为估算，不包含准确的视觉计费量。

### Pi Agent 实际使用

Pi 0.85.1 屏蔽用户插件后的真实验证见 [PI_REAL_WORLD_VALIDATION.md](PI_REAL_WORLD_VALIDATION.md)，附有独立模型配置和请求记录。思考开关、联网搜索、原生文本读取已通过；单图一次正确但耗时约 259 秒，另一次 300 秒超时。Pi 的 `@PDF` 会按文本读取，不会自动生成文档上传请求；通过 Pi 原生 `!命令` 显式调用上传脚本后可以完成 PDF 问答。模型自动执行上传命令的尝试未通过，不能把代理支持文档等同于客户端直接支持 PDF 附件。

## 已知限制

- **令牌过期的恢复仍有缺口**：模型元数据与文档上传阶段遇到失效令牌时，实际观察到 HTTP 500 或流中断，尚未统一恢复与正确传播错误。本轮重启服务恢复了登录状态，详见 Pi 验证报告。
- **模型名完全透传**：请求里的 `model` 会直接作为 GenAI 平台的 `aiType` 发送，不做任何映射。请填写平台真实存在的模型 id。
- **无服务端会话状态**：Responses API 不支持 `previous_response_id`，也不做 response storage。客户端需要每轮发送完整历史（Codex 的 `disable_response_storage = true` 正好符合）。
- **Responses API 是兼容子集**：实现了 Codex / OpenAI Agents SDK 实际使用的 message、function_call、custom_tool_call、reasoning 与文本 delta 事件；web_search 声明会启用 GenAI 网页搜索，但不伪造原生托管工具事件，mcp、image_generation 等托管工具事件未实现。`response.completed` 会回显请求的 `tools` / `tool_choice` / `store` 等字段，但超出上下文上限时发出的是 `response.incomplete`。
- **Anthropic thinking 签名是占位值**：上游 GenAI 没有 Anthropic 意义上的签名，代理返回 `genai-compat-no-signature`。历史里的 thinking 块会被忽略，不会转发给上游。
- **图片以内联方式发送**：base64 图片会以 data URL 随请求体一并发送，体积约为原图的 4/3，因此大图会拉长请求体；代理不再调用网页的图片服务，也就不依赖任何前端令牌。只验证过 PNG，其他格式由平台侧解析能力决定。
- **只有独立 reasoning 字段会被结构化透传**：`reasoning` / `reasoning_content` 会映射到客户端思考字段。上游若把分析写成未标记正文，代理不会猜测其类别。Anthropic 输出路径会过滤正文 `<think>...</think>` 标签；不使用网页历史中的标签内容来伪造实时思考。
- **重复相同问题会命中平台缓存**：同一 `model` 与相同问题重复请求时，平台会在几十毫秒内直接返回上次答案，且不带任何思考增量。想观察思考应以新提问验证；不能据此认为模型不再推理。
- **部分模型关闭思考后仍把分析写进正文**：GLM 在 `thinking: false` 时，正文会以未标记的英文分析开头（例如 `The user wants me to…`）；同模型开启思考后思考字段与正文均正常。DeepSeek 关闭思考后正文保持干净。这是模型侧行为，代理不猜测哪些句子属于分析，也不事后改写成思考。
- **工具调用仍由文本模拟**：function calling 依赖 prompt 注入 + 文本解析（`<tool_call>` 标记），模型不按格式输出时可能解析失败，此时工具标记会作为普通文本返回。支持图片输入不等于支持原生 function calling。
- **`tool_choice` 指定单个函数会收窄允许集合**：Chat 的 `{"type":"function",...}`、Anthropic 的 `{"type":"tool"}` 会让代理只透传该工具的调用，其它工具调用被丢弃；但这只是代理层过滤 + prompt 约束，不是模型原生能力。
- **输出长度由代理本地强制**：上游不执行 `maxToken`，代理按估算 token 在本地截断，命中上限时 Chat 的 `finish_reason` 与 Anthropic 的 `stop_reason` 会分别返回 `length` / `max_tokens`；未命中时正常返回 `stop` / `end_turn`。预算包含思维链 token，因此对推理模型给很小的 `max_tokens` 时可能只产出思维链、正文为空。
- **usage 是本地估算**：`prompt_tokens` / `input_tokens` / `completion_tokens` 均由代理按文本估算，不是上游真实计费值。
- **思考支持开关，不支持档位或精确预算**：客户端推理参数映射为上游 `thinking` 布尔值。模型标为不提供开关、额度不足或上游未返回思考字段时，代理不会补造思考内容。`/v1/models` 的 `capabilities` 来自平台配置，不代表已逐模型保证所有组合都可用。
- **上游超时与错误传播**：上游单次请求超时为 300 秒；大 prompt（数十万 token）首 token 仍可能超时，此时流式接口返回标准 `error` 事件（HTTP 仍为 200），非流式接口返回 HTTP 502 与 `{"error": {...}}`。上游即使用 HTTP 200 + SSE 返回纯文本错误（如 worker 不可用时的 `No available workers (all circuits open or unhealthy)`），也会按同样方式上报，不再表现为空回复。
- **兼容 GenAI 原生 DSML 标记**：部分模型会自行输出 `<｜DSML｜tool_call>`、`<｜DSML｜call>`、`<｜DSML｜_call>`、`<｜DSML｜ name="Tool">` 等 DSML 变体，代理会统一归一化为 `<tool_call>` 再解析；标签内的参数同时兼容 JSON 与 Python 字面量（单引号、`True`/`None`）。模型输出严重残缺时仍可能解析失败。

## 上下文探测工具

GenAI 平台上不同模型的上下文窗口不一样（128k ~ 1M 都有）。`context-probe` 工具用于**实测**某个模型的真实上下文长度，供 Claude Code 等前端安全地控制 prompt 大小。

原理是在输入**最前端**放一个唯一标记（`ZEBRA`），再让模型复述它；能复述说明整个上下文都被保留，后端拒绝时其报错信息通常会给出精确上限（如 `maximum context length is 524288 tokens`）。

```bash
# 定量探测指定模型（在列出的尺寸上打点）
pixi run context-probe --model chatglm --tokens 8192,131072,524288

# 自动二分找出模型的上限（推荐）
pixi run context-probe --model GPT-5.6-SOL --find

# 快速扫描所有对话模型（较慢，谨慎使用）
pixi run context-probe --all
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--model` | 要探测的模型 id（与 `--all` 二选一） | — |
| `--all` | 扫描所有对话模型 | — |
| `--tokens` | 逗号分隔的目标 token 尺寸列表 | 一组基准值 |
| `--find` | 二分找出精确上下文上限 | 关闭 |
| `--low` / `--high` | `--find` 的搜索区间 | `8192` / `2097152` |
| `--max-output` | 每次探测的最大输出 token | `300` |
| `--timeout` | 单次请求超时（秒） | `300` |
| `--token` | JWT 或 `学号@密码`（覆盖 `.env` 的 `GENAI_TOKEN`） | — |

> 注意：`--find` 和较大的 `--tokens` 会向平台发送很大的请求体并消耗真实 token 额度；az 系列模型可能触发平台配额限制（报 `额度上限了`）。

## 速度基准工具

`bench-speed` 工具通过本地代理（默认 `http://127.0.0.1:31100`）逐一向每个对话模型发流式请求，测量两个指标：

- **首字时间（TTFT）**：从发出请求到收到第一个内容或思维链增量。
- **正文首字时间**：从发出请求到收到第一个正文（非思维链）增量。
- **输出速度**：输出 token 数 ÷（总耗时 − 首字时间），即首字后的解码速度。

模型顺序执行以避免并发干扰，每个模型先预热再取多次中位数；输出 token 优先取流末尾 `usage.completion_tokens`。

```bash
# 扫描 /v1/models 里的全部对话模型
pixi run bench-speed

# 只测指定模型，次数与输出上限可调
pixi run bench-speed --models deepseek-chat,deepseek-pro --runs 5 --max-tokens 2048

# 把 Markdown 报告写入文件
pixi run bench-speed --output BENCH_speed.md
```

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--base-url` | 代理地址 | `http://127.0.0.1:31100` |
| `--api-key` | 代理 API key（或设置 `API_KEY` 环境变量） | — |
| `--models` | 逗号分隔的模型 id，默认取 `/v1/models` 全部对话模型 | — |
| `--runs` | 每个模型正式测量次数 | `3` |
| `--warmup` | 每个模型预热次数（不计入结果） | `1` |
| `--max-tokens` | 单次请求输出上限 | `512` |
| `--timeout` | 单次请求超时（秒） | `300` |
| `--prompt` | 测试用的用户消息 | 计数到 200 |
| `--output` | 把 Markdown 报告写入该文件 | — |

> 注意：被平台标记为不可用的模型（额度上限、`No available workers`）会在报告里记为失败，这不代表速度，而是上游状态。

## 致谢

本项目基于 [HeZeBang/GenAI2OpenAI](https://github.com/HeZeBang/GenAI2OpenAI) 开发，感谢原作者的工作。

## 许可

MIT License — 详见 LICENSE 文件。
