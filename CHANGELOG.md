# Changelog

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
