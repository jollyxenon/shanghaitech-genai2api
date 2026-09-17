# 项目协作说明

本项目在 WSL Ubuntu 下使用 pixi。运行命令以 `pixi.toml` 为准：`pixi run serve` 启动服务，`pixi run test` 运行已有检查。

## 请求链路

- `api/chat.py`、`api/messages.py`、`api/responses.py` 分别处理三种客户端协议。
- `tools/prompts.py` 统一文本、图片和文档内容块；`flatten_message_content` 只用于提取文本，不能替代附件处理。
- `provider/features.py` 在开始 SSE 前完成附件上传、文件解析及思考/搜索开关映射。当前图片使用顶层 `imageUrls` 和上传结果的宽高；历史图片保留在原消息的内容数组中，一个合并文本块后跟图片块，与网页协议一致。当前轮文档上传和聊天必须使用同一 `chatGroupId`，不假定文件标识可以跨会话复用。
- `provider/genai.py` 是唯一聊天流入口；三种输出适配层共用它的 delta/done/error 事件。搜索的补充结果通过正文增量追加，编号不与主回答直接配对。
- 模型能力来自 `ModelRegistry` 的平台元数据，不按模型名硬编码。思考仅支持布尔开关，不宣称精确 effort/budget 支持。

## 验证与文档

平台协议证据见 `GENAI_WEB_CAPABILITIES.md`；当前使用方法和支持边界以 README 为准。Pi 客户端实际验证见 `PI_REAL_WORLD_VALIDATION.md`：无用户插件时思考和搜索通过，图片仍有长延迟/超时；`@PDF` 不等于文档上传，显式原生 shell 上传成功不代表模型会自动执行工具。改动后同步 README 和 CHANGELOG。附件日志只记录数量、大小、解析长度和耗时，不打印令牌、base64 或完整文档。

代理无服务端会话存储及 Files API。客户端需要发送完整历史。上游图片加载可能超时；单次成功不等于平台已稳定支持所有多轮组合，运行结果应分别记录。
