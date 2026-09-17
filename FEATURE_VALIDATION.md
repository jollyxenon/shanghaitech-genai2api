# 网页功能接入验证记录

日期：2026-09-17。实现入口见 `provider/features.py`，客户端用法见 README。

## 已完成的实现

Chat Completions、Anthropic Messages、OpenAI Responses 共用图片/文档上传、思考开关和联网搜索。流式与非流式均接入。文档当前轮采用同会话 `fileIds`，历史文档使用解析文本；历史图片按网页的消息内容数组保留，不将其混同为本轮新图片。

图片 URL/base64 均先上传到平台，获取地址和尺寸。文件支持 base64、data URL、HTTP(S) URL；Anthropic 还支持文本型 document。无 Files API 或持久文件标识存储，不支持 `file_id`。

## 实际调用结果

通过 Flask 应用路由调用真实上游，使用自行生成的普通图片和文档，不用 mock 代替平台结果。

| 项目 | 接口 / 模型 | 观察 |
|---|---|---|
| 同轮图片＋TXT | Chat / GLM，非流式 | 正确返回图片数字 `5827` 与文档编号 `DOC-482613` |
| 同轮图片＋TXT | Anthropic / GLM，流式 | 正确返回两项信息，并正常发送 `message_stop` |
| 同轮图片＋TXT | Responses / GLM，非流式 | 正确返回两项信息，含 133 字符独立思考内容；此前同类请求出现过短诊断超时 |
| 思考控制与输出 | Chat / GLM，流式 | `thinking: true` 返回 `reasoning_content`，正文为 `391` |
| 思考控制与输出 | Anthropic / GLM，非流式 | enabled 映射为开启，返回 thinking 块与正文 `391` |
| 思考控制与输出 | Responses / GLM，流式 | high 映射为开启，完整产生 reasoning summary 及 completed 事件 |
| 联网搜索 | Responses / DeepSeek，流式 | 返回正文来源链接，随后补充来源接口成功返回 10 条结果；正常 completed |
| 历史文档追问 | Chat / GLM，非流式 | 不在当前轮附文件，仍能正确取回历史文件的 `DOC-482613` |
| 文档格式 | 平台上传接口 | TXT、最小 DOCX、文本型 PDF 均返回 fileId，并提取出正确的样本标记 |
| 图片 URL 输入 | 代理下载及平台上传 | HTTP(S) 下载与上传成功；该次后续模型请求遇到超时 |
| 错误输入 | 三种接口 | 无效 base64、不支持的 file_id、非法 thinking 类型均返回 400 |
| 模型元数据 | `/v1/models` | 返回动态 `capabilities`，GLM 的图片、文档、搜索、思考开关字段均为 true |

字数和耗时仅描述具体样本，不代表模型质量或速度保证。开启思考并不保证每次响应都会带独立思考字段。

## 多轮图片与失败边界

多轮图片观察出现过连续的上游读取超时，包含 40/45 秒诊断阈值与一次 180 秒阈值。按网页历史内容数组格式做双图对照时，也取得过正确的“上一轮 `5827`、本轮 `9364`”答案，但重复请求尚未稳定成功。

尝试将历史图片全部放到顶层的临时方案曾正确回答单图追问；双图组合中出现新旧图对应错误，因此最终实现没有采用这一方案。最终代码保持网页的轮次结构，并将每条历史消息的文字合并在图片块之前。

不能据上述失败断言模型不支持图片，也不能凭一次正确答案宣称所有多图、多轮组合稳定。平台图片读取和模型输出的稳定性仍需后续观察。当前生产聊天读取超时保持原来的 300 秒，没有以诊断脚本的短阈值替代它。

平台联网搜索有自己的提示词和输出形式，可能比用户要求更长；GLM 的搜索和一次普通文本请求中，均观察到 `thinking: false` 时仍将未标记的分析文字作为正文返回。代理只把独立 `reasoning` / `reasoning_content` 结构化，不猜测正文中的哪些句子属于思考；布尔参数是向上游传达控制，不保证所有模型严格执行。

本轮未重复消耗 GPT 系列额度；前一轮已确认当前账号的 GPT 请求被额度限制阻断。

## 本机服务

已重启现有用户服务 `genai2agent-upstream.service`，服务状态为 active，监听端口仍为 31100。实际端口验证：`/health` 返回 200，`/v1/models` 返回 10 个模型及新增能力字段，`/v1/responses` 使用 DeepSeek、`reasoning.effort: none` 返回 completed 与正文 `READY`。

## deepseek-pro 思维链归属专项观察

针对“思维链是否与正文混在一起”的直接观察，均为开启思考的新提问：

| 观察 | 请求 | 结果 |
|---|---|---|
| 原始字段抓取 | 上游流，`thinking: true` | 同时出现 `reasoning_content`（234 字）与 `content`（286 字）；正文以“结论”开头 |
| 本机 Chat | 新问题，`thinking: true` | 思考 304 字，正文 303 字，正文无 `<think>` 标签 |
| 本机 Anthropic | 新问题，`thinking: enabled` | 思考 287 字单独成块，正文 266 字 |
| 本机 Responses | 新问题，`effort: high` | reasoning summary 290 字，正文 279 字 |
| 关闭思考 | 4 个新任务，`thinking: false` | 均无思考字段，正文无未标记分析句（改错、代码、建议、概念解释） |

两个容易造成误判的现象：

1. **重复相同问题命中平台缓存**。同一模型同一问题的第 2、3、4 次请求分别在 0.04 / 0.03 / 0.03 秒返回同样答案，`reasoning_content` 均为 0；第 1 次 (0.75 秒) 返回 215 字思考。换新问题后思考正常返回。
2. **GLM 开启与关闭思考时行为不同**。GLM 在 `thinking: false` 时正文以 `The user wants me to…` 等未标记分析开头（4/4 任务复现）；`thinking: true` 时思考字段（125/104/196 字）与正文都正常。DeepSeek 关闭思考时正文保持干净。

因此“思维链与正文混合”是 GLM 在关闭思考时的表现，不是 deepseek-pro 的当前行为；“没有思维链”更常见的原因是重复提问命中缓存。两者都是平台/模型侧行为，代理不做猜测性拆分。数据来自 2026-09-17 本机代理与真实上游。

## 图片上传令牌的自动获取

图片服务是独立域名 `genaipic.shanghaitech.edu.cn`，它不认用户的 CAS/JWT 登录态。实测同一张图片上传：

| 请求头 | 结果 |
|---|---|
| 前端使用的固定令牌（控制组） | `success:true`，返回 `url`、`width`、`height`，静态地址可访问 |
| 用户 JWT 放在 `token` / `X-Access-Token` / `Authorization` | 同样返回 `success:true`，但 `result:null`，实际未保存文件 |
| 不带任何鉴权 | 同上，`result:null` |

主站侧也没有可替代的入口：`POST /htk/sys/common/upload` 用用户令牌返回 500，`/htk/chat/upload/image` 不存在；字典接口只提供图片服务地址，不提供令牌。前端代码里的上传请求直接写死了 `token:"…"`（`app.js` 与业务 chunk 中都有），并不是从接口获取的。

因此代理改为**首次需要上传图片时自动获取**：取网站首页 → 匹配 `app.<hash>.js` → 从脚本中提取 32 位十六进制令牌 → 缓存在进程内。实测耗时 0.03–0.06 秒完成发现与上传，第二次上传不再重复下载。重启本机服务后，真实请求日志中出现 `attachment uploaded kind=image bytes=8279`、`prepare_ms=68`，全程不需手工配置令牌。

为此新增的测试与检查：`pixi run pytest -q --disable-warnings` 62 项通过；仓库源码中不再出现该令牌值（仅本地被忽略的 `.playwright-mcp/` 前端副本与 `.env` 中存在，后者已被 Git 忽略）。

## 本地检查

- 现有检查：`pixi run pytest -q --disable-warnings`，62 项通过。
- 新增测试文件：无。
- 现有测试替身缺少 HTTP 响应的 `close()`，产生 16 条资源清理警告。真实请求已使用 requests.Response，并在完成、出错、客户端断开时关闭连接；未为这些旧替身添加生产兼容分支。
- `python -m compileall` 与 `git diff --check` 通过。

日志新增附件数量、上传字节数、解析长度、准备耗时及功能开关；不输出文件原文、base64 或用户令牌。
