# 模型首字时间与输出速度测试

- 测试时间：2026-09-12 04:27:30
- 接口：`POST http://127.0.0.1:31100/v1/chat/completions`（流式）
- 每个模型：预热 1 次 + 正式 3 次，取中位数
- max_tokens：2048
- Prompt：请从 1 开始连续数数，一直数到 200，数字之间用空格分隔。只输出数字本身，不要输出任何解释或其他文字。

| 模型 | 首字中位 (s) | 正文首字 (s) | 首字类型 | 输出 tokens | 解码耗时 (s) | 速度 (tok/s) | 状态 |
|---|---:|---:|---|---:|---:|---:|---|
| gpt-6-astra | - | - | - | - | - | - | 失败：{'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-SOL | - | - | - | - | - | - | 失败：{'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-Terra | - | - | - | - | - | - | 失败：{'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-Luna | - | - | - | - | - | - | 失败：{'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| chatglm | - | - | - | - | - | - | 失败：{'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| qwen-instruct | - | - | - | - | - | - | 失败：{'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| deepseek-chat | 0.09 | 0.73 | reasoning | 347 | 1.77 | 171.69 | 正常 |
| deepseek-pro | 0.10 | 1.51 | reasoning | 659 | 2.15 | 289.39 | 正常 |
| Kimi-k3 | - | - | - | - | - | - | 失败：{'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |

## 每次请求明细

| 模型 | 轮次 | 首字 (s) | 正文首字 (s) | tokens | 总耗时 (s) | 速度 (tok/s) | finish_reason |
|---|---:|---:|---:|---:|---:|---:|---|
| gpt-6-astra | 1 | - | - | 0 | 0.02 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| gpt-6-astra | 2 | - | - | 0 | 0.02 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| gpt-6-astra | 3 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-SOL | 1 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-SOL | 2 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-SOL | 3 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-Terra | 1 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-Terra | 2 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-Terra | 3 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-Luna | 1 | - | - | 0 | 0.02 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-Luna | 2 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| GPT-5.6-Luna | 3 | - | - | 0 | 0.01 | - | ERR {'message': 'Upstream error: 额度上限了', 'type': 'upstream_error', 'code': None} |
| chatglm | 1 | - | - | 0 | 1.64 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| chatglm | 2 | - | - | 0 | 1.45 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| chatglm | 3 | - | - | 0 | 1.36 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| qwen-instruct | 1 | - | - | 0 | 1.39 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| qwen-instruct | 2 | - | - | 0 | 1.45 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| qwen-instruct | 3 | - | - | 0 | 1.35 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| deepseek-chat | 1 | 0.09 | 2.78 | 653 | 3.89 | 171.69 | stop |
| deepseek-chat | 2 | 0.09 | 0.73 | 347 | 1.86 | 195.94 | stop |
| deepseek-chat | 3 | 0.10 | 0.42 | 248 | 1.57 | 169.02 | stop |
| deepseek-pro | 1 | 0.12 | 1.51 | 660 | 2.27 | 307.07 | stop |
| deepseek-pro | 2 | 0.10 | 1.59 | 659 | 2.39 | 287.64 | stop |
| deepseek-pro | 3 | 0.06 | 1.44 | 619 | 2.20 | 289.39 | stop |
| Kimi-k3 | 1 | - | - | 0 | 1.44 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| Kimi-k3 | 2 | - | - | 0 | 1.48 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |
| Kimi-k3 | 3 | - | - | 0 | 1.50 | - | ERR {'message': 'Upstream error: No available workers (all circuits open or unhealthy)', 'type': 'upstream_error', 'code': None} |

## 口径说明

- 首字时间（TTFT）：从发出请求到收到第一个携带内容或思维链的增量。
- 正文首字：从发出请求到收到第一个正文（非思维链）增量的时间；纯文本模型与 TTFT 相同。
- 速度：输出 token 数 ÷（总耗时 − 首字时间），即首字后的解码速度，不含首字等待。
- 输出 token 数优先取流末尾 `usage.completion_tokens`（代理估算值），缺失时本地按同一规则估算。
- 思维链 token 计入输出，因此推理模型的“速度”是含思维链的吞吐。
