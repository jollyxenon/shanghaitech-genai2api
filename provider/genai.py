import json
import logging
import re
import time
import uuid
from datetime import datetime

import requests

from config import GENAI_URL, build_genai_headers, model_registry
from errors import UpstreamError, make_error_chunk
from provider.features import search_sources
from tools.parsing import extract_tool_calls, find_tool_call_open, tool_call_prefix_len
from tools.prompts import flatten_message_content, normalize_message_content

logger = logging.getLogger(__name__)
TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]")

# 上游几十万 token 的大 prompt 首 token 可能远超 60s，用长超时避免被误判为失败。
UPSTREAM_TIMEOUT = 300


def convert_messages_to_genai_format(messages):
    """取最后一条 user 消息的文本作为当前提问（chatInfo）。"""
    chat_info = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            chat_info = flatten_message_content(msg.get("content", ""))
            break
    return chat_info


def extract_content_from_genai(response_data):
    try:
        if "choices" in response_data and len(response_data["choices"]) > 0:
            delta = response_data["choices"][0].get("delta", {})
            content = delta.get("content") or None
            reasoning = delta.get("reasoning_content") or delta.get("reasoning") or None
            return content, reasoning
    except (KeyError, IndexError, TypeError):
        pass
    return None, None


def estimate_text_tokens(text):
    if not text:
        return 0
    return len(TOKEN_PATTERN.findall(text))


def estimate_messages_tokens(messages):
    """估算整段对话的输入 token，用于填充 usage.prompt_tokens / input_tokens。"""
    return sum(
        estimate_text_tokens(flatten_message_content(msg.get("content", "")))
        for msg in messages
    )


def log_stream_metrics(model, started_at, first_token_at, content_text, reasoning_text):
    total_elapsed = max(time.monotonic() - started_at, 1e-6)
    content_tokens = estimate_text_tokens(content_text)
    reasoning_tokens = estimate_text_tokens(reasoning_text)
    total_tokens = content_tokens + reasoning_tokens

    extra = ""
    if first_token_at is not None:
        ttft_ms = (first_token_at - started_at) * 1000
        extra = f" ttft_ms={ttft_ms:.0f}"

    logger.info(
        "stream metrics model=%s est_tokens=%d content_est=%d reasoning_est=%d toks_per_s=%.2f%s",
        model,
        total_tokens,
        content_tokens,
        reasoning_tokens,
        total_tokens / total_elapsed,
        extra,
    )


def split_history_messages(messages):
    """规范化历史消息，并移除最后一条 user（它已经作为 chatInfo 传入）。"""
    normalized = [normalize_message_content(msg) for msg in messages]
    for index in range(len(normalized) - 1, -1, -1):
        if normalized[index].get("role") == "user":
            del normalized[index]
            break
    return normalized


def iter_genai_stream(chat_info, history_messages, model, max_tokens, config, token=None, upstream_options=None):
    """统一的上游 GenAI 流迭代器，供 Chat / Anthropic / Responses 三个适配层共用。

    依次产出：
        {"type": "delta", "content": str|None, "reasoning": str|None}
        {"type": "done", "finish_reason": str|None}
        {"type": "error", "message": str}
    """
    token = token or config.token_manager.get_token()
    root_ai_type = model_registry.get_root_ai_type(model, token)
    headers = build_genai_headers(token)

    genai_data = {
        "chatInfo": chat_info,
        "messages": history_messages,
        "type": "3",
        "stream": True,
        "aiType": model,
        "aiSecType": "1",
        "promptTokens": 0,
        "rootAiType": root_ai_type,
        "maxToken": max_tokens or 30000,
        **(upstream_options or {}),
    }

    logger.debug("=== GenAI Request ===")
    logger.debug("Model: %s, rootAiType: %s", model, root_ai_type)
    logger.debug("Messages count: %d", len(history_messages))

    response = None
    try:
        response = requests.post(
            GENAI_URL,
            headers=headers,
            json=genai_data,
            stream=True,
            timeout=UPSTREAM_TIMEOUT,
        )

        if response.status_code == 401:
            new_token = config.token_manager.force_refresh()
            if new_token:
                logger.info("Token refreshed after 401, retrying request")
                headers = build_genai_headers(new_token)
                token = new_token
                response.close()
                response = requests.post(
                    GENAI_URL, headers=headers, json=genai_data, stream=True,
                    timeout=UPSTREAM_TIMEOUT,
                )

        if response.status_code != 200:
            logger.warning("GenAI API error %d: %s", response.status_code, response.text[:500])
            if response.status_code == 401:
                yield {"type": "error", "message": "Upstream authentication failed"}
            elif response.status_code == 429:
                yield {"type": "error", "message": "Upstream rate limit exceeded"}
            else:
                yield {"type": "error", "message": f"Upstream API error: {response.status_code}"}
            return

        finish_reason = None
        emitted_tokens = 0
        truncated = False
        for line in response.iter_lines():
            if not line:
                continue

            line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            line_str = line_str.strip()
            if not line_str:
                continue

            # SSE 结束标记，正常收尾；其后的用量信息不再需要。
            if line_str in ("[DONE]", "data:[DONE]", "data: [DONE]"):
                break

            # SSE 注释行（": keep-alive"）与其它字段行不是数据，跳过。
            if line_str.startswith(":") or line_str.split(":", 1)[0] in ("event", "id", "retry"):
                continue

            if line_str.startswith("data:"):
                line_str = line_str[5:].strip()
                if not line_str:
                    continue

            try:
                genai_json = json.loads(line_str)
            except json.JSONDecodeError:
                # 上游有时直接在 SSE 通道里写纯文本错误（worker 不可用时就是一行
                # "No available workers (all circuits open or unhealthy)"）。以前这里
                # 只记 debug 日志然后跳过，结果把“上游不可用”伪装成 HTTP 200 空回复。
                logger.warning("GenAI 上游返回非 JSON 文本: %s", line_str[:200])
                yield {"type": "error", "message": f"Upstream error: {line_str[:200]}"}
                return

            if isinstance(genai_json, dict) and genai_json.get("success") is False:
                err_msg = genai_json.get("message", "Unknown upstream error")
                logger.warning("GenAI business error: %s", err_msg)
                yield {"type": "error", "message": f"Upstream error: {err_msg}"}
                return

            if isinstance(genai_json, dict) and "choices" not in genai_json:
                error = genai_json.get("error")
                err_msg = genai_json.get("errMsg")
                if isinstance(error, dict):
                    err_msg = error.get("message") or err_msg
                if err_msg:
                    logger.warning("GenAI upstream error: %s", err_msg)
                    yield {"type": "error", "message": f"Upstream error: {err_msg}"}
                    return
                continue

            choices = genai_json.get("choices") or []
            if not choices:
                continue

            content, reasoning = extract_content_from_genai(genai_json)
            if content or reasoning:
                yield {"type": "delta", "content": content, "reasoning": reasoning}
                emitted_tokens += estimate_text_tokens(content or "") + estimate_text_tokens(reasoning or "")
                # 上游不执行 maxToken，长度上限只能在本地按估算 token 强制生效。
                if max_tokens and emitted_tokens >= max_tokens:
                    truncated = True
                    break

            if choices[0].get("finish_reason") is not None:
                finish_reason = choices[0].get("finish_reason")
                break

        if genai_data.get("netGo") and not truncated:
            try:
                sources = search_sources(genai_data, token)
                # 附加来源也受本地输出预算约束，只保留完整的链接行。
                if max_tokens and sources:
                    kept = []
                    for source_line in sources.splitlines(keepends=True):
                        count = estimate_text_tokens(source_line)
                        if emitted_tokens + count > max_tokens:
                            truncated = True
                            break
                        emitted_tokens += count
                        kept.append(source_line)
                    sources = "".join(kept)
                if sources:
                    yield {"type": "delta", "content": sources, "reasoning": None}
            except (requests.RequestException, UpstreamError, ValueError):
                logger.warning("补充检索列表获取失败；保留已返回的正文引用", exc_info=True)
        yield {"type": "done", "finish_reason": "length" if truncated else finish_reason}

    except Exception as e:
        logger.exception("Error in iter_genai_stream")
        yield {"type": "error", "message": str(e)}
    finally:
        if response is not None:
            response.close()


def collect_genai_response(chat_info, messages, model, max_tokens, config, upstream_options=None):
    """非流式收集上游输出，返回 (content, reasoning, finish_reason)。

    上游错误直接抛 UpstreamError，不能像以前那样解析 SSE 失败就丢掉。
    """
    content_parts = []
    reasoning_parts = []
    finish_reason = None
    history = split_history_messages(messages)
    for event in iter_genai_stream(chat_info, history, model, max_tokens, config, upstream_options=upstream_options):
        if event["type"] == "error":
            raise UpstreamError(event["message"])
        if event["type"] == "delta":
            if event.get("content"):
                content_parts.append(event["content"])
            if event.get("reasoning"):
                reasoning_parts.append(event["reasoning"])
        elif event["type"] == "done":
            finish_reason = event.get("finish_reason")
    return "".join(content_parts), "".join(reasoning_parts), finish_reason


def _chat_finish_reason(upstream_reason):
    """把上游 finish_reason 映射成 OpenAI Chat 的取值。"""
    return "length" if upstream_reason == "length" else "stop"


def stream_genai_response(chat_info, messages, model, max_tokens, config, upstream_options=None):
    """OpenAI Chat Completions 兼容的流式响应。"""
    started_at = time.monotonic()
    first_token_at = None
    content_parts = []
    reasoning_parts = []
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(datetime.now().timestamp())
    sent_role = False
    prompt_tokens = estimate_messages_tokens(messages)

    def make_chunk(delta, finish_reason=None, usage=None):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            chunk["usage"] = usage
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    def emit(delta):
        nonlocal sent_role
        if not sent_role:
            delta = {"role": "assistant", **delta}
            sent_role = True
        return make_chunk(delta)

    history = split_history_messages(messages)

    for event in iter_genai_stream(chat_info, history, model, max_tokens, config, upstream_options=upstream_options):
        if event["type"] == "error":
            yield make_error_chunk(event["message"])
            return

        if event["type"] == "delta":
            if first_token_at is None:
                first_token_at = time.monotonic()
            delta = {}
            if event.get("reasoning"):
                reasoning_parts.append(event["reasoning"])
                delta["reasoning_content"] = event["reasoning"]
            if event.get("content"):
                content_parts.append(event["content"])
                delta["content"] = event["content"]
            if delta:
                yield emit(delta)
            continue

        # done
        log_stream_metrics(
            model, started_at, first_token_at, "".join(content_parts), "".join(reasoning_parts)
        )
        output_tokens = estimate_text_tokens("".join(content_parts) + "".join(reasoning_parts))
        yield make_chunk({}, finish_reason=_chat_finish_reason(event.get("finish_reason")), usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens,
        })
        yield "data: [DONE]\n\n"
        return


def stream_genai_response_with_tools(
    chat_info, messages, model, max_tokens, config, allowed_tool_names=None, upstream_options=None
):
    """带工具调用的 Chat Completions 流式响应。

    上游模型不支持原生 function calling，因此从正文里解析 <tool_call> 标记，
    再转成 OpenAI 的 tool_calls 增量。
    """
    started_at = time.monotonic()
    first_token_at = None
    content_parts = []
    reasoning_parts = []
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(datetime.now().timestamp())

    buffer = ""
    tool_buffer = ""
    sent_role = False
    tool_detected = False
    prompt_tokens = estimate_messages_tokens(messages)

    def make_chunk(delta, finish_reason=None, usage=None):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            chunk["usage"] = usage
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    def emit(delta):
        nonlocal sent_role
        if not sent_role:
            delta = {"role": "assistant", **delta}
            sent_role = True
        return make_chunk(delta)

    history = split_history_messages(messages)

    for event in iter_genai_stream(chat_info, history, model, max_tokens, config, upstream_options=upstream_options):
        if event["type"] == "error":
            yield make_error_chunk(event["message"])
            return

        if event["type"] == "delta":
            if first_token_at is None:
                first_token_at = time.monotonic()

            # 思维链单独成增量，不参与工具标记检测
            if event.get("reasoning"):
                reasoning_parts.append(event["reasoning"])
                yield emit({"reasoning_content": event["reasoning"]})

            content = event.get("content")
            if not content:
                continue
            content_parts.append(content)

            if tool_detected:
                tool_buffer += content
                continue

            buffer += content

            tag_pos = find_tool_call_open(buffer)
            if tag_pos >= 0:
                pre = buffer[:tag_pos]
                if pre.strip():
                    yield emit({"content": pre})
                tool_detected = True
                tool_buffer = buffer[tag_pos:]
                buffer = ""
                continue

            plen = tool_call_prefix_len(buffer)
            if plen > 0:
                safe = buffer[:-plen]
                if safe:
                    yield emit({"content": safe})
                buffer = buffer[-plen:]
            else:
                if buffer:
                    yield emit({"content": buffer})
                buffer = ""
            continue

        # done：先冲刷残留文本，再输出工具调用
        if tool_detected:
            tool_buffer += buffer
            buffer = ""
            tool_calls, remaining = extract_tool_calls(
                tool_buffer, allowed_tool_names=allowed_tool_names
            )

            if tool_calls:
                logger.debug("Streaming tool calling: detected %d tool_call(s)", len(tool_calls))
                if remaining and remaining.strip():
                    yield emit({"content": remaining.strip()})

                for index, tool_call in enumerate(tool_calls):
                    function = tool_call.get("function", {})
                    # 先发名称与 id，再发参数，贴近原生增量顺序
                    yield make_chunk({
                        "tool_calls": [{
                            "index": index,
                            "id": tool_call.get("id"),
                            "type": "function",
                            "function": {"name": function.get("name"), "arguments": ""},
                        }]
                    })
                    yield make_chunk({
                        "tool_calls": [{
                            "index": index,
                            "function": {"arguments": function.get("arguments", "{}")},
                        }]
                    })

                log_stream_metrics(
                    model, started_at, first_token_at,
                    "".join(content_parts), "".join(reasoning_parts),
                )
                yield make_chunk({}, finish_reason="tool_calls")
                yield "data: [DONE]\n\n"
                return

            logger.warning("Tool tag detected but parsing failed — emitting as text")
            yield emit({"content": remaining if remaining is not None else tool_buffer})
            yield make_chunk({}, finish_reason=_chat_finish_reason(event.get("finish_reason")))
            yield "data: [DONE]\n\n"
            return

        if buffer:
            yield emit({"content": buffer})
        if not sent_role:
            yield emit({"content": ""})

        log_stream_metrics(
            model, started_at, first_token_at,
            "".join(content_parts), "".join(reasoning_parts),
        )
        output_tokens = estimate_text_tokens("".join(content_parts) + "".join(reasoning_parts))
        yield make_chunk({}, finish_reason=_chat_finish_reason(event.get("finish_reason")), usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": prompt_tokens + output_tokens,
        })
        yield "data: [DONE]\n\n"
        return
