# GenAI 网页能力与请求协议调查

调查时间：2026-09-17。入口：https://genai.shanghaitech.edu.cn/dashboard/analysis 。

本报告记录调查时的平台与代理状态。后续功能已接入代理，当前用法见 [README](README.md#图片文档联网搜索与深度思考)，实现验证和未解决的平台限制见 [FEATURE_VALIDATION.md](FEATURE_VALIDATION.md)。

## 结论与上一轮纠正

**图片上传、图片理解、可见的深度思考、文档问答和联网搜索均已取得实际运行证据。现有代理对网页新增输入能力的适配落后于平台。**

上一轮的以下判断需要撤回：

- 不能把图片上传函数中的 `status == 3` 当作某些对话模型不支持图片的证据：当前下拉框中的 **8 个对话模型全部为 `status: "3"`**，都满足该条件。
- `uploadAction3` 在 `data()` 中初始为空，不代表运行时没有配置。本次运行时已读到实际图片服务地址，并成功上传。
- 本地 README 和代理代码只说明代理目前实现了什么，不能作为线上平台能力的上限。
- 不能仅凭模型描述判断思考内容是否实际返回。需区分“是否有开关”“请求是否开启”和“这次响应有没有思考字段”。
- 模型列表共 **10 条：8 个对话模型、2 个绘图模型**。对话模型中 **7 个有深度思考开关，Qwen-3.8 没有该开关**。上一轮的数量表述不正确。

本次没有完成所有模型、所有功能的全面成功验证：GPT 四个模型受到当前账号额度限制，Kimi 的图片请求遭遇后端读取图片超时。此类失败不能记为“不支持”。

## 证据方法

使用 Playwright 操作已登录的网页，观察组件运行时状态、普通图片/文本文件上传、实际聊天请求及 SSE；按网页请求格式发出少量短请求，对模型做交叉观察。前端源代码用于解释请求和界面行为。

用户提供的两张截图对应的历史对话均已核实为 `deepseek-pro`：图片对话保存了图片 URL，另一段“你好”对话恢复了 251 字符的思考内容。未复制用户图片到调查报告，也未保存登录凭据。

本次新建图片为白底的紫色正方形、橙色圆形与数字 `5827`。新建文本文件仅含“海盐计划 / DOC-739126 / 周四下午三点”。这些普通样本用于验证模型确实读到了附件。

## 按模型观察结果

| 网页模型 | 实际 aiType | 图片结果 | 深度思考开关 | 本次思考响应观察 |
|---|---|---|---|---|
| GPT-6-Astra | `gpt-6-astra` | 请求被“额度上限了”拦截 | 有 | 未能实际验证 |
| GPT-5.6-Sol | `GPT-5.6-SOL` | 请求被“额度上限了”拦截 | 有 | 未能实际验证 |
| GPT-5.6-Terra | `GPT-5.6-Terra` | 请求被“额度上限了”拦截 | 有 | 未能实际验证 |
| GPT-5.6-Luna | `GPT-5.6-Luna` | 请求被“额度上限了”拦截 | 有 | 未能实际验证 |
| GLM-5.3-Flash | `chatglm` | 正确识别两种图形、颜色与数字 | 有 | 图片请求返回 `reasoning_content`，53 字符 |
| Qwen-3.8 | `qwen-instruct` | 一次正确识图；另一次新提示词请求超时 | 无 | 按网页格式不传 `thinking` 的纯文本请求未返回思考字段 |
| DeepSeek-V4.1 | `deepseek-pro` | 网页正确识图，同时显示思考；后续一次图片请求超时 | 有 | 网页图片对话显示 158 字符思考；独立文本对照见下文 |
| Kimi-K3 | `Kimi-k3` | 后端加载 `genaipic` 图片连接超时 | 有 | 纯文本请求返回 `reasoning_content`，46 字符 |

表中“正确识图”均要求回答包含实际图像内容，而不是仅仅上传成功。三种图像内容都正确的模型为 DeepSeek、GLM、Qwen。Kimi 的错误明确出现在图片 URL 加载环节，尚不能判断成功识图的效果。

没有强行向 Qwen 添加网页不发送的思考参数。因此，只能确认它当前没有网页开关、样本响应没有独立思考字段，不能断言其底层永远不具备推理能力。

GPT 系列模型描述仍写着“推理过程不可见”。由于额度阻断，本次没有将描述升级成实测结论。

## 图片协议

运行时 `file_url` 字典返回图片服务根地址，客户端拼接出：

```text
POST https://genaipic.shanghaitech.edu.cn//sys/common/upload
GET  https://genaipic.shanghaitech.edu.cn//sys/common/static/{result.url}
```

这里的双斜杠是网页实际拼接结果。**图片上传服务不是上一轮推测的主站 `/htk/sys/common/upload`。**

上传使用 `multipart/form-data`：

```text
file: 图片二进制
biz: temp
uploadType: local
```

请求带图片服务自己的 `token` 头，和聊天接口的 `X-Access-Token` 不同。报告不复制该值。

上传成功响应的关键结构：

```json
{
  "success": true,
  "result": {
    "url": "temp/<图片路径>.png",
    "width": 480,
    "height": 220,
    "suffix": null
  }
}
```

聊天请求携带 `imageUrl`（第一张）、`imageUrls`（全部图片 URL）和 `width` / `height`。本次从网页上传、发送到模型回答均已跑通。

**但平台自己也接受不经上传的图片地址**，这对代理更重要：

| 传入 `imageUrls` 的值 | 实测结果 |
|---|---|
| `data:image/png;base64,…` | 模型正确识别图形与数字；不传 `width` / `height` 也正常 |
| 公开 HTTPS 图片地址 | 平台自行抓取并准确描述图片内容 |
| 3 MB PNG 的 data URL | 正常识别内容 |

因此代理不需要调用图片服务：base64 图片直接以 data URL、网络图片以原 URL 传入即可，既省去上传往返，也避开了图片服务不认用户登录令牌的问题（该服务只认前端脚本里的固定令牌，且不传令牌会返回 `success:true` + `result:null`）。

前端校验允许 JPG/JPEG/PNG/WEBP/GIF，单张文件小于 20 MiB；文件输入允许多选。此处是前端限制，未做服务端极限探测或逐格式验证。

多轮图片历史与当前轮参数不同。前端会把历史图片转换为：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "上一轮的问题"},
    {"type": "image_url", "image_url": {"url": "https://...", "detail": "high"}}
  ]
}
```

这一转换已在前端代码确认，尚未另行验证多张图片、多轮图片的全部组合。代理只补顶层 `imageUrls` 仍不足以支持完整的图片历史。

## 深度思考协议

`POST /htk/chat/start/chat` 使用顶层布尔字段：

```json
{"thinking": true}
```

网页仅在 `enableDeepThink == 1` 时加入该字段。关闭时发送 `false`，不是省略字段。

模型注册信息中：

- GPT 四个模型：`thinkingField=reasoning_effort`，配置值 `medium`。
- GLM / DeepSeek / Kimi：`thinkingField=chat_template_kwargs`，配置值 `{"thinking": true, "reasoning_effort": "high"}`。
- Qwen：`enableDeepThink=0`，没有上述映射配置。

这些是平台模型配置，**网页发给聊天接口的是 `thinking` 布尔值，并未直接发送这些底层字段。** 目前证据证明可开关，未证明客户端能自由选择 low/medium/high 或精确思考预算。

独立纯文本对照使用短算术问题，附不同观察编号以减少完全相同问题复用的影响：

| 模型 | 请求 | 正文 | reasoning_content 字符数 |
|---|---|---|---:|
| DeepSeek | `thinking: true` | `391` | 93 |
| DeepSeek | `thinking: false` | `391` | 0 |
| Kimi | `thinking: true` | `391` | 46 |
| Qwen | 不传 `thinking`，与网页一致 | `391` | 0 |

这说明 DeepSeek 的思考开关对实际响应有影响。字符数用于描述本次样本，不是推理质量或能力上限。

实时响应与历史存储需要区分：

- SSE：实测 GLM、DeepSeek、Kimi 返回 `choices[0].delta.reasoning_content`；前端同时兼容 `reasoning`。
- 历史接口：`answer` 内含 `<think>...</think>`，网页提取后展示。本次图片历史恢复出 158 字符思考。
- 前端也有正文 `<think>` 的解析路径，但不能仅凭历史格式推断所有实时响应都会使用标签。

一次完全相同图片问题的重复请求在约 0.03 秒内直接返回最终正文，没有思考增量；可能存在结果复用，但本次没有审计缓存实现，不能确定原因。之后的文本对照与 GLM 观察采用不同提示词。

## 文档协议

网页实际上传入口：

```text
POST /htk/chat/upload/chat/file?chatGroupId=<会话>&aiType=<模型>
X-Access-Token: <当前登录令牌>
Content-Type: multipart/form-data

file: 文件二进制
biz: temp
```

本次 `.txt` 成功返回：

```json
{
  "success": true,
  "result": {
    "fileId": "<文件标识>",
    "fileName": "genai-capability-note.txt",
    "content": "调查样本文档项目名称：海盐计划资料编号：DOC-739126会议时间：周四下午三点",
    "chatGroupId": "<同一会话>"
  }
}
```

随后聊天请求带 `fileIds: ["<文件标识>"]`。本次实际请求中没有把文档内容拼进 `chatInfo` 或 `messages`，DeepSeek 仍准确回答了三项内容，证明服务器通过文件标识取到了已解析内容。

前端允许多文件；单文件小于 10 MiB；普通文件选择器没有 `accept` 类型限制。后端具体支持哪些格式仍需按格式验证，本次只实测 TXT，不能将其等同于 PDF、扫描件、DOCX、XLSX 全部成功。

文档上传与聊天使用同一 `chatGroupId` 和 `aiType`。`fileId` 能否跨会话复用、文件保留期、连续多轮如何延续文档上下文尚未验证，代理接入时应先遵循已观察到的同会话流程。

## 联网搜索协议与网页错误

开启时，聊天请求带：

```json
{"netGo": true}
```

本次对 DeepSeek 询问上海科技大学图书馆官网，SSE 返回 1448 字符正文，带来源编号及网页链接。流结束后，网页再发：

```text
POST /htk/chat/net/search
```

请求体基本沿用原聊天参数。响应形状为：

```json
{
  "success": true,
  "result": {
    "links": [
      {"num": 1, "title": "标题", "link": "https://...", "snippet": "摘要"}
    ],
    "count": 10
  }
}
```

本次来源接口确实返回 10 条结果。聊天正文的来源编号与该列表并不完全对应，例如正文将图书馆简介标为来源 2，而后取列表中同一 URL 是第 8 条。**代理不应仅按编号把两次结果直接拼成同一组引用。** 是否由第二次搜索或缓存差异造成，本次未能确定。

网页控制台同时出现：

```text
ReferenceError: newMessagea is not defined
chunk-a18ada4c.3a2e7379.js:1:77887
```

源码中本地模型完成分支在来源接口成功后使用了 `text: newMessagea`。本次响应成功，但当前消息的 `result` 没有被挂上；这说明后端搜索已工作，前端参考来源更新仍存在错误。没有修改线上网页。

聊天 SSE 正常产生 `finish_reason: stop` 后，网页会主动 `AbortController.abort()`。因此观察副本读流出现的 `AbortError`，在已经收到 `stop` 且显示完整回答时，不应当作模型失败。

## 接入现有代理的具体缺口

这次只做调研与记录，未修改代理运行逻辑。

| 位置 | 当前行为 | 已确认需要处理的内容 |
|---|---|---|
| `config.py` / `ModelInfo` | 保存模型名称、根类型等 | 保留动态的 `enableDeepThink` 等能力信息，避免按名称写死 |
| `provider/genai.py` / `iter_genai_stream` | 只发文本相关参数 | 接入 `thinking`、`netGo`、当前轮图片、`fileIds` 及上传会话 |
| `tools/prompts.py` / `flatten_message_content` | 把内容列表压平，图片对象可能转 JSON 文本 | 保留真正的图片内容块，区分正文与附件 |
| `provider/anthropic.py` / `anthropic_content_to_text` | 图片转成 `[image]` | 解析图片来源并走图片上传/引用流程 |
| `provider/responses.py` / `_content_to_text` | `input_image` / `image_url` 转成 `[image]` | 保留图片输入，处理文件输入 |
| 三种 API 请求入口 | 思考参数被忽略并记录 warning | 将受支持的开关语义映射到 `thinking`；不要冒充已支持精确 effort/预算 |
| 思考输出 | 已透传独立 reasoning 字段；部分输出路径过滤正文 think 标签 | 保留现有独立字段通路；若增加历史接口，需解析平台历史 think 标签；实时标签支持需依据实际协议明确处理 |
| 搜索输出 | 没有来源获取与结构化映射 | 区分聊天正文引用和额外搜索结果，处理网页已暴露的编号不一致 |

因此，“只给 `genai_data` 加四个字段”并不完整。图片在进入请求构造之前已被压成文本，文件还需要上传与会话关联，搜索来源又是独立接口；三种客户端格式都需要各自保留结构化输入。

README 中“上游是文本模型”“上游没有思考开关”已不适合作为平台现状描述。后续实现时应同时更新文档，区分平台支持的能力与代理已实现的能力。

## 调查产物和未完成验证

本地观察数据：

- `.playwright-mcp/capability-observations.jsonl`：图片请求及各模型响应摘要，包括失败结果。
- `.playwright-mcp/text-observations.jsonl`：深度思考文本对照。
- `.playwright-mcp/platform-history-observations.json`：脱敏模型配置与本次自建对话的历史摘要，不含登录凭据和思考全文。
- `.playwright-mcp/console-2026-09-17T10-16-00-012Z.log`：网页搜索完成分支的错误。
- `.playwright-mcp/observe_capabilities.py`：按已观察到的协议记录短图片请求的临时调查脚本；执行会消耗真实平台额度。

主要未验证项：GPT 额度恢复后的实际图片和思考结果；Kimi 图片加载恢复后的识图结果；Qwen 是否存在网页未开放的思考方式；多图、多轮图片及文档上下文；PDF/DOCX 等文件格式；搜索引用一致性。均不以当前失败推导为不支持。

浏览器临时抓流包装已通过重新加载页面移除；调查未登出用户，也未删除历史对话。
