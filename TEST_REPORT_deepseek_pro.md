# deepseek-pro 反代能力测试报告

- 测试对象：`http://127.0.0.1:31100`（本项目反代）→ 上游 GenAI 平台 `deepseek-pro`（root=xinference）
- 测试时间：2026（本次会话）
- 测试方式：三条接口（OpenAI Chat Completions / Anthropic Messages / OpenAI Responses）直接发 HTTP 请求；上下文用仓库自带 `context-probe --find` 二分 + 经反代复测
- 说明：首轮测试与 51 万 token 的上下文二分**并发**执行，个别空响应是并发干扰造成的假阴性；所有可疑项都在隔离环境复测过，下表以隔离复测为准。

## 一、结论摘要

| 维度 | 结论 | 评级 |
|---|---|---|
| 上下文窗口 | 模型真实上限 **524,288 token（512k）**，可用约 520k。但反代硬编码 `timeout=60`，超大 prompt 首 token 超过 60s 时**间歇性失败** | ⚠️ 有阻塞性缺陷 |
| 思维链（CoT） | 三条接口都正确分离透传：Chat `reasoning_content`、Anthropic thinking 块、Responses reasoning 事件；流式顺序正确；`<think>` 不泄漏正文 | ✅ 正常 |
| Tool call | 单工具 / 流式 / 并行 / 强制调用 / 多轮回喂 / Anthropic 往返 / Responses 往返**全部正常**；`tool_choice` 指定单个函数**不生效** | ✅ 主体正常，1 项缺陷 |
| Skill（API 模拟） | 工具优先、强制输出格式、元信息记忆、长文档标记召回、两步流程**全部正常** | ✅ 正常 |
| reasoning effort 粒度 | **不存在**：`reasoning_effort`、`thinking.budget_tokens`、`reasoning.effort` 全部被代理忽略；`max_tokens` 也随之无效（上游不支持） | ❌ 无此能力 |
| 稳定性 | 隔离后连续 8 次简单请求 0 空响应 | ✅ 正常 |

**一句话**：deepseek-pro 经反代能力可用，CoT / Tool / Skill 都过关；主要问题集中在**大上下文的 60 秒超时**、**输出长度与截断信号完全不可控**、**`tool_choice` 与 reasoning effort 参数形同虚设**。

## 二、测试环境与方法

- 代理进程已在 `31100` 运行，`API_FORMAT=both`，未开 `--debug`。
- 模型列表确认 `deepseek-pro` 存在（`owned_by=xinference`）。
- 测试脚本（临时件，未入仓）：
  - `/tmp/dspro/harness.py`：26 项功能测试（CoT / Tool / Skill / effort）
  - `/tmp/dspro/repro.py`：可疑失败项隔离复现 + 直连上游验证
  - `/tmp/dspro/final.py`、`/tmp/dspro/ctx_proxy2.py`：经反代的大上下文验证
  - `/tmp/dspro/skill2.py`：Skill 两步流程完整复测
  - 结果原始数据：`/tmp/dspro/results.json`

## 三、分维度结果

### 1. 上下文窗口

**直连上游二分（`pixi run context-probe --model deepseek-pro --find`）**

```
 ~  131,072 tok | actual=131149 | PASS
 ~  262,144 tok | actual=262223 | PASS
 ~  393,216 tok | actual=393281 | PASS
 ~  491,520 tok | actual=491594 | PASS
 ~  507,904 tok | actual=507972 | PASS
 ~  516,096 tok | actual=516169 | PASS
 ~  520,192 tok | actual=520266 | PASS
 ~  524,288 tok | OVER_LIMIT  exact limit=524288
 -> usable up to ~520,256 tokens, fails at ~524,288 tokens
```

**经反代的复测（`/v1/chat/completions`，非流式/流式）**

| 目标 token | 结果 | 耗时 |
|---|---|---|
| ~400,000 | 首次 60s 失败（错误文本当正文返回），另一次 4s 成功召回 ZEBRA | 60s / 4s |
| ~450,000 | 成功召回 ZEBRA | 17s |
| ~500,000 | 首次 60s 失败，重试 9s 成功召回 ZEBRA | 60s / 9s |

**根因**：`provider/genai.py:113` 与 `:122` 把上游请求超时写死为 `timeout=60`：

```python
response = requests.post(GENAI_URL, headers=headers, json=genai_data, stream=True, timeout=60)
```

超大 prompt 上游首 token 可能超过 60s（冷请求尤其明显），此时抛出 `ReadTimeout`，被 `iter_genai_stream` 捕获后转成错误事件：

- **流式 Chat**：错误被当作普通正文增量返回——客户端看到的是
  ```
  "[Error] HTTPSConnectionPool(host='genai.shanghaitech.edu.cn'...Read timed out"
  ```
  （`errors.py:23` 的 `make_error_chunk` 把错误塞进 `delta.content`，`finish_reason="error"`）
- **非流式 Chat**：`api/chat.py:88-99` 用 `json.loads(line[6:])` 解析，错误 chunks 是 `data: {...}\n\ndata: [DONE]` 两行拼接，`json.loads` 抛 `JSONDecodeError` 被 `pass` 掉，最终返回 **HTTP 200 + 空 content + finish_reason="stop"**，错误完全消失。
- Anthropic / Responses 路径把错误作为独立 `error` 事件返回，表现正常。

**建议**：把 `timeout=60` 改为可配置的长超时（例如 300s，与 `context_probe` 一致），或拆成 `(connect, read)` 元组；同时非流式 Chat 收集时应把错误 chunk 显式识别出来并以错误响应返回，而不是解析失败就丢弃。

### 2. 思维链（CoT）

全部通过，原始证据：

- 非流式 Chat：`reasoning_content` 与 `content` 分离，`reasoning_len=218 / content_len=347`，答案正确（鸡 23、兔 12）。
- 流式 Chat：`first_reason_idx=0 < first_content_idx=36`，思维链严格先于正文，`finish=stop`。
- `<think>` 过滤：要求把推理写进 `<think>` 时，正文只返回 `'391'`，标签未泄漏（`reasoning_len=163`）。
- Anthropic：`content` 块为 `['thinking','text']`，`signature=genai-compat-no-signature`（占位签名，符合 README 声明）。
- Responses：同时收到 `response.reasoning_summary_text.delta`（4 段，381 字符）与 `response.output_text.delta`，并有 `response.completed`。

### 3. Tool call

| 用例 | 结果 |
|---|---|
| Chat 非流式单工具 | ✅ `get_weather(location=Shanghai, unit=celsius)`，`finish_reason=tool_calls` |
| Chat 流式单工具 | ✅ `calculate(expression="42*58")`，参数 JSON 合法，且带 reasoning |
| 一轮并行多工具 | ✅ `['get_weather','calculate']` 两个调用 |
| `tool_choice="required"` | ✅ 强制调用 |
| **`tool_choice` 指定 `calculate`** | ❌ 3/3 都返回 `['get_weather','calculate']`，并未限制为指定函数 |
| 未给工具时 | ✅ 不产生 `tool_calls` |
| 多轮 `tool_result` 回喂 | ✅ 回喂 `83810205` 后给出最终答案 |
| Anthropic `tool_use` + `tool_result` 往返 | ✅ `stop_reason=tool_use` → 回喂后 `end_turn`，输入为 JSON 对象 |
| Responses `function_call` + `function_call_output` 往返 | ✅ `function_call` 事件正确，回喂 `42` 后输出 `7 × 6 = 42` |

**`tool_choice` 指定函数的缺陷**：`tools/prompts.py:124-128` 只是在 system prompt 里追加一句
`You MUST call the tool named "calculate" in your response.`，并没有在解析/输出层过滤其它工具。deepseek-pro 会调用它认为相关的**所有**工具（天气问题甚至凭空调了 `calculate(1+1)`）。属于"软提示"，客户端不能依赖它做严格的工具约束。

### 4. Skill（API 模拟）

按用户要求不启动 Claude Code，用 system prompt 注入 SKILL 文档 + 工具定义来模拟：

| 用例 | 结果 |
|---|---|
| 技能要求"先调用 calculate 再回答" | ✅ 调用 `calculate({"expression":"12345*6789"})` |
| 技能强制输出格式 `RESULT=<值>` | ✅ 回喂结果后最终输出精确为 `RESULT=83810205` |
| 技能元信息记忆（`SKILL-V9`） | ✅ 回答 `SKILL-V9` |
| 4.9k 字符技能文档，开头埋 `SKILL-MARKER-ZEBRA-42` | ✅ 原样召回，长 system prompt 未被截断/丢失 |
| 两步流程：`list_files` → 按结果 `read_file("report.txt")` → 总结 | ✅ 三步全部按技能执行，最终正确总结 |
| 财务合规技能"必须走 `format_money`" | ✅ 3/3 调用 `format_money({"amount":720,"currency":"CNY"})` |

### 5. reasoning effort 粒度

**结论：没有这个能力。** `reasoning_effort` 在项目 Python 代码里**零出现**（`grep -rn reasoning_effort` 无结果）。三个接口相关参数都只是被忽略或回显：

- Chat：`reasoning_effort` 不读、不传。
- Anthropic：请求里的 `thinking`（含 `budget_tokens`）不读；`thinking` 块只用于输出。
- Responses：`api/responses.py:106` 把 `body["reasoning"]` 存进 `response_meta` **仅用于回显**，不参与上游请求。

隔离复测的思维链长度（同一题目）：

```
reasoning_effort=minimal: 149, 140
reasoning_effort=high:    162, 137      # 与 minimal 区间重叠，非单调 → 纯采样噪声
Anthropic budget_tokens:  1024→119, 4096→208, 16384→129   # 非单调
Responses reasoning.effort: low→162, high→111              # high 反而更短
```

**输出长度控制同样失效**：`max_tokens` 会透传成上游 `maxToken`（`provider/genai.py:100`），但上游不执行：

- 直连上游 `maxToken=64`，仍产出 `content 2339 字符 + reasoning 222 字符`，`totalTokens≈2831`；`maxToken=256/2048` 结果相近。
- 经反代 `max_tokens=64/256/1024` 均产出 2000~4000 字符长文。

**截断与计费信号不可用**：

- Chat 的 `finish_reason` 无论上游是否截断都写死为 `"stop"`（`provider/genai.py:242`、`api/chat.py:118-120`），客户端**无法判断回答是否被截断**。（Responses 路径反而会检查 `finish_reason=="length"` 并回 `response.incomplete`，两者行为不一致。）
- `usage.prompt_tokens` 恒为 `0`（`api/chat.py:140`；Anthropic 非流式 `api/messages.py:179` 的 `input_tokens` 也是 0），只有 `completion_tokens` 是本地估算；Responses 的 `input_tokens` 是估算值。客户端无法据此做上下文裁剪或计费。
- Anthropic `count_tokens` 是 `总字符数/4` 的粗略估算（`api/messages.py:215`），与实测 token 数（每英文单词≈1 token）偏差较大。

## 四、问题清单（按优先级）

1. **【高】大上下文 60s 超时**：`provider/genai.py:113,122` 硬编码 `timeout=60`，导致 40 万~52 万 token 的请求间歇失败；流式把错误当正文、非流式把错误吞成空 200。
2. **【高】Chat 非流式错误被静默丢弃**：`api/chat.py:88-99` 解析错误 chunk 失败即 `pass`，用户拿到空回答却无任何错误提示。
3. **【中】输出长度不可控**：`maxToken` 上游不执行；`finish_reason` 恒为 `stop`；`usage.prompt_tokens=0`。客户端无法限长、无法感知截断、无法统计输入。
4. **【中】`tool_choice` 指定函数不生效**：仅为 prompt 软提示，未做输出层约束（`tools/prompts.py:124-128`）。
5. **【低】reasoning effort 无粒度**：`reasoning_effort` / `thinking.budget_tokens` / `reasoning.effort` 全部无效。若平台本身不支持，则应在文档中明确"该参数不生效"，而不是让客户端误以为可调。

## 五、复现命令

```bash
# 上下文精确上限（直连上游，耗时较长、消耗额度）
pixi run context-probe --model deepseek-pro --find

# 功能测试（临时脚本）
pixi run python -u /tmp/dspro/harness.py      # CoT / Tool / Skill / effort
pixi run python -u /tmp/dspro/repro.py       # 可疑项隔离复现 + 直连上游 maxToken 验证
pixi run python -u /tmp/dspro/skill2.py      # Skill 两步流程
pixi run python -u /tmp/dspro/ctx_proxy2.py  # 经反代的大上下文（抓 60s 超时证据）
```

---

## 六、修复记录与复测（补充）

针对上面三个高/中优先级问题做了修复，并在独立端口 31200 起新实例端到端复测。

### 修复内容

| 问题 | 改动 |
|---|---|
| 60s 硬超时 | `provider/genai.py` 新增 `UPSTREAM_TIMEOUT = 300`，替换两处 `timeout=60` |
| 非流式错误被静默丢弃 | 新增 `collect_genai_response()` 直接收集事件，出错抛 `UpstreamError`；`api/chat.py` 捕获后返回 HTTP 502 |
| 流式 Chat 错误当正文 | `errors.make_error_chunk` 改为标准 `{"error": {...}}` 事件，不再塞进 `delta.content` |
| `max_tokens` 无效 | `iter_genai_stream` 按估算 token 本地截断，命中上限返回 `finish_reason="length"`；Anthropic 映射为 `stop_reason="max_tokens"` |
| `prompt_tokens` 恒 0 | 新增 `estimate_messages_tokens()`，Chat `usage.prompt_tokens` 与 Anthropic `input_tokens` 改为估算值 |
| `tool_choice` 指定函数无效 | 三个接口在 `tool_choice` 指定函数时收窄 `allowed_tool_names`，输出层过滤其它工具；prompt 追加 "MUST NOT call any other tool" |
| reasoning effort 无粒度 | 上游确实不支持。三个接口收到相关参数时记录 warning，README 明确声明并说明被忽略 |

### 复测结果（31200 实例）

```
非流式无效模型      -> HTTP 502 {"error": {"type": "upstream_error", "message": "Upstream error: 未找到对应节点信息..."}}
流式无效模型        -> data: {"error": {...}} 然后 [DONE]
max_tokens=256      -> finish=length, content_len=0, reasoning_len=269（思维链计入预算）
max_tokens=4096     -> finish=stop, content_len=2969
usage.prompt_tokens -> 21 / 14 / 250025 / 500025（不再恒 0）
Anthropic           -> stop_reason=max_tokens, input_tokens=21
Responses           -> max_output_tokens=256 时 response.incomplete
Chat tool_choice    -> 3/3 只返回 calculate
Anthropic tool_choice -> 2/2 只返回 calculate
Responses tool_choice -> 只返回 calculate
500k token 上下文   -> 84s 成功召回 ZEBRA（旧代码 60s 超时会失败）
reasoning 参数      -> 三个接口各打出一条 warning
```

### 注意的行为变化

- **小 `max_tokens` 可能只返回思维链**：本地预算包含思维链 token，对推理模型给 `max_tokens=64` 之类的极小值时，思维链会用完预算导致正文为空（`finish_reason=length`）。这与 OpenAI / Anthropic 推理模型把思考计入输出预算的语义一致。
- **流式错误事件格式变更**：Chat 的流式错误从 `delta.content` 文本变成 `{"error": {...}}` 对象，依赖旧格式的客户端需要适配。
