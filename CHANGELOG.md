# Changelog

## Unreleased

### New Features

- **DSML 工具标记兼容**：部分模型会绕过注入的 `<tool_call>`，直接输出 GenAI 原生的 DSML 标记。代理现在统一处理这些变体：
  - 归一化 `<｜DSML｜tool_call>` / `<｜DSML｜call>` / `<｜DSML｜_call>` / `<｜DSML｜l_call>`（含半角 `|`）为 `<tool_call>`
  - 支持工具名写在属性里的变体 `<｜DSML｜ name="Read">`
  - 兼容空标签名 `<｜DSML｜>` 与混用开闭标签（如 `<tool_call>` 配 `</｜DSML｜>`）
  - 三种接口（Chat / Anthropic / Responses）的流式检测都能识别 DSML 起始标记

### Changes

- 工具参数解析兼容 Python 字面量（单引号字符串、`True`/`False`/`None`），以及缺少 `arguments` 包裹、直接平铺参数键的形态
- 标签内“裸工具名 + 参数 JSON”（如 `read\n{"path": ...}`）也能解析

### Bug Fixes

- 移除 Anthropic 流式文本里对 DSML 标签的预先删除：它会在解析器看到工具调用之前把标签删掉，导致工具调用丢失、属性碎片（`name="...">`）泄漏为正文
- 解析失败时输出的是清理过 DSML 标签的文本，而不是原始标签内容

## v2.2.0

### New Features

- **OpenAI Responses API**：新增 `POST /v1/responses`，可直接接入 Codex CLI（`wire_api = "responses"`）与 OpenAI Agents SDK
  - 流式事件按官方格式输出：`response.created` / `response.in_progress` / `response.output_item.added|done` / `response.output_text.delta|done` / `response.reasoning_summary_text.delta|done` / `response.function_call_arguments.delta|done` / `response.custom_tool_call_input.delta|done` / `response.completed`
  - 支持 Responses 的 `input` items：`message`、`function_call`、`function_call_output`、`custom_tool_call`、`reasoning`
  - 支持 `type: "namespace"` 包裹的工具定义（Codex 的默认形态），展平其中的 function 与 custom 工具；`custom` 工具（如 apply_patch）输出为 `custom_tool_call`
  - 每个事件带递增 `sequence_number`，`response.completed` 携带完整 `output` 与 `usage`，并回显请求的 `tools` / `tool_choice` / `parallel_tool_calls` / `store` 等字段
  - 上游因长度截断（`finish_reason = length`）时发出 `response.incomplete`（`incomplete_details.reason = max_output_tokens`），不会误报为正常完成
- **思维链透传**：上游 `reasoning_content` 在三个接口分别以 Chat 的 `reasoning_content`、Anthropic 的 thinking 块、Responses 的 reasoning 事件输出

### Changes

- `provider/genai.py` 抽出统一的 `iter_genai_stream()` 上游流迭代器，Chat / Anthropic / Responses 三个适配层共用，统一处理 401 重试与错误上报
- Chat Completions 流式响应改为整个请求使用同一个 `chatcmpl-*` ID（此前每个 chunk 都会重新生成）
- 工具调用参数统一序列化为合法 JSON 对象（字符串参数会被解析，Bash 的裸字符串映射为 `command`）
- 模型名保持完全透传，不做映射

### Bug Fixes

- Chat Completions 工具流式路径不再丢弃 `reasoning_content`
- Anthropic 非流式响应现在包含 thinking 块，且流式 block index 在有 thinking 时按顺序顺延（不再硬编码 text 为 0、tool 为 1）
- Anthropic 历史里的 `thinking` / `redacted_thinking` 块不再作为普通 JSON 文本转发给上游

### Notes

- Anthropic thinking 的 `signature` 是明确的兼容占位值 `genai-compat-no-signature`，不是真实签名
- Responses API 不提供服务端会话存储，也不支持 `previous_response_id`

## v2.1.0

### Changes

- 依赖管理与运行由 `uv` 迁移到 `pixi`
- 移除 `uv.lock`、`.python-version` 与 `pyproject.toml`，由 `pixi.toml` 作为唯一清单
- 新增 `pixi` 任务：`pixi run serve` 启动代理（等价于 `python main.py`），`pixi run test` 运行单元测试
- 支持从项目根目录 `.env` 读取登录凭据、端口和 API 格式，默认端口为 `31100`
- README 与工具说明同步改用 `pixi` 命令
- 新增 `tools/context_probe.py`：实测 GenAI 模型真实上下文长度，提供 `pixi run context-probe` 任务

### Bug Fixes

- 修正请求构造：把最后一条 user 消息作为 `chatInfo`（当前提问），其余作为历史 `messages` 传入上游，避免提问重复
- 兼容上游以 `reasoning` 字段返回思考内容（原先只识别 `reasoning_content`）
- 处理上游返回 `choices` 缺失但带 `error`/`errMsg` 的响应，避免静默失败

## v2.0.0

### Breaking Changes

- `--token` 参数现在为必需项，移除了硬编码的默认 token
- `Config.token` 字段替换为 `Config.token_manager`，使用 `TokenManager` 对象管理 token 生命周期
- 新增 `pycryptodome` 依赖

### New Features

- **学号密码登录**: `--token` 支持 `学号@密码` 格式，自动通过 CAS 统一身份认证获取 JWT
- **Token 自动刷新**: 学号密码模式下，JWT 过期时自动重新登录，对客户端完全透明
- **401 自动重试**: 上游返回 401 时，自动刷新 token 并重试当前请求
- **JWT 离线校验**: 解码 JWT payload 中的 `exp` 字段，预留 60 秒安全余量提前刷新
- **启动时快速失败**: 学号密码模式启动时立即尝试登录，密码错误直接报错退出

### Internal

- 新增 `auth/cas_login.py`: CAS 登录流程（AES-128-CBC 密码加密、IDS 表单解析、重定向跟随）
- 新增 `auth/token_manager.py`: `TokenManager` 类（JWT/学号密码模式识别、线程安全刷新）
- `app.py`: `before_request` 改用 `token_manager.get_token()`，新增 `LoginError` 错误处理
- `provider/genai.py`: 401 响应时调用 `force_refresh()` 并重试

## v1.0.0

### Features

- OpenAI 兼容的 Chat Completion 代理（流式/非流式）
- 动态模型列表，自动从 GenAI 平台拉取
- Tool Calling 支持（通过 prompt 注入，兼容非原生模型）
- 流式 Tool Calling 解析
- Token 过期流式错误推送
- API Key 客户端认证
- 健康检查端点
