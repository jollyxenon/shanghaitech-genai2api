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

上传方案下的多轮图片曾出现连续超时，包含 40/45 秒诊断阈值与一次 180 秒阈值；当时按网页历史内容数组格式做双图对照也偶发正确，但重复请求不稳定。

改为 data URL 直传后，这批超时不再复现：多轮历史图片追问在 0.3 秒内正确回答 `5827`。旧方案同时试过“把历史图片都放到顶层”的临时办法，双图组合中曾出现新旧图对应错误，因此未采用；最终实现保持网页的轮次结构，即每条历史消息的文字合并在图片块之前。

仍不能凭少数成功宣称所有模型、所有多图多轮组合都稳定，只是原先那个稳定复现的失败点已消失。当前生产聊天读取超时保持原来的 300 秒，没有以诊断脚本的短阈值替代它。

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

## 图片链路：从上传改为直传

图片服务的令牌确实拿不到，但最终发现根本不需要用它。

**先确认令牌无法由登录态推导**。图片服务是独立域名 `genaipic.shanghaitech.edu.cn`，实测同一张图片上传：

| 请求头 | 结果 |
|---|---|
| 前端使用的固定令牌（控制组） | 成功，返回 `url`、`width`、`height` |
| 用户 JWT 放在 `token` / `X-Access-Token` / `Authorization` | `success:true` 但 `result:null`，文件未保存 |
| 用户 JWT 放在 query、Cookie、multipart 表单字段 | 同上，`result:null` |
| 随机 32 位十六进制放在 `token` 头 | 同上，`result:null` |
| 不带任何鉴权 | 同上，`result:null` |

主站也没有替代入口：`POST /htk/sys/common/upload` 用用户令牌返回 500，`/htk/chat/upload/image` 不存在，字典接口 `file_url` 只返回服务地址（`result` 仅一项 `路径`），不返回令牌；前端 `initDictData1` 只从该字典拼接上传地址，令牌是写在前端脚本里的常量。

**再试绕过上传**，直接给上游图片地址，结果两种都能用：

| 输入形式 | 结果 |
|---|---|
| `imageUrls: ["data:image/png;base64,…"]` | 正确回答“紫色正方形、橙色圆形、5827”，且不传 `width` / `height` 也行 |
| `imageUrls: ["https://www.python.org/static/img/python-logo.png"]` | 平台自行抓取并准确描述了官方标志 |
| 3 MB PNG 的 data URL | 正常回答“黑色正方形 + 白色圆形 + 噪点背景” |

于是代理改为不调用图片服务：base64 图片以 data URL、网络图片以原 URL 直接放到顶层 `imageUrls`；历史图片放在各自消息的内容数组中（网页格式）。

实测对比（同一张 480×220 图片，GLM）：

| 链路 | 延迟 |
|---|---|
| 旧：上传到图片服务再引用 | 16–31 秒，并多次出现 40/45/180/290 秒超时 |
| 新：data URL 直传 | 0.6 秒（Chat）、0.8 秒（Responses）、27.5 秒（Anthropic 一次）、3 MB 图 1.8 秒 |
| 多轮历史图片追问 | 0.3 秒正确回答 `5827`（旧方案在此场景反复超时） |
| deepseek-pro 直传 | 正确回答“紫色正方形、橙色圆形、数字 5827” |

服务日志确认图片不再上传：`images=1` 且 `prepare_ms=0`，不再出现 `attachment uploaded kind=image`。文档仍需要上传（平台只提供文档上传接口），继续使用用户自己的登录令牌。

## 本地检查

- 现有检查：`pixi run pytest -q --disable-warnings`，62 项通过。
- 新增测试文件：无。
- 现有测试替身缺少 HTTP 响应的 `close()`，产生 16 条资源清理警告。真实请求已使用 requests.Response，并在完成、出错、客户端断开时关闭连接；未为这些旧替身添加生产兼容分支。
- `python -m compileall` 与 `git diff --check` 通过。

日志新增附件数量、上传字节数、解析长度、准备耗时及功能开关；不输出文件原文、base64 或用户令牌。
