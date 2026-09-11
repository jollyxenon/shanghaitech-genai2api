# GenAI2OpenAI

将上海科技大学 GenAI 平台接入 Claude Code 的代理服务。

A proxy that connects ShanghaiTech's GenAI platform to Claude Code.

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

`GENAI_TOKEN` 也可以填写 JWT。`.env` 已被 Git 忽略，不会提交凭据。

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

### 4. 配置项目设置（减少权限弹窗）

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

### 5. 启动

```bash
claude
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--token` | JWT 令牌或 `学号@密码`（也可写入 `.env` 的 `GENAI_TOKEN`） | — |
| `--port` | 服务监听端口（也可写入 `.env` 的 `PORT`） | `31100` |
| `--api-key` | 客户端认证密钥（也可通过 `API_KEY` 环境变量设置） | 无 |
| `--api-format` | `openai`、`anthropic` 或 `both`（也可写入 `.env` 的 `API_FORMAT`） | `both` |
| `--debug` | 启用详细日志输出 | 关闭 |

## 特性

- **Claude Code 兼容** — 提供 Anthropic Messages API，支持 `tool_use/tool_result` 转换，可直接接入 Claude Code
- **OpenAI 兼容** — 同时提供 OpenAI Chat Completion API，支持 Cursor、Continue 等客户端
- **Tool Calling** — 通过 prompt 注入实现 function calling，兼容不原生支持的模型
- **自动登录与刷新** — 学号密码模式通过 CAS 自动登录，JWT 过期静默刷新
- **动态模型列表** — 自动从 GenAI 平台拉取可用模型

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

## 致谢

本项目基于 [HeZeBang/GenAI2OpenAI](https://github.com/HeZeBang/GenAI2OpenAI) 开发，感谢原作者的工作。

## 许可

MIT License — 详见 LICENSE 文件。
