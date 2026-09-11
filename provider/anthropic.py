import json
import logging
import ast
import re
import time
import uuid
from typing import Any, Dict, Generator, List, Optional, Tuple

from config import model_registry
from provider.genai import iter_genai_stream
from tools.parsing import extract_tool_calls, _tag_prefix_len
from tools.prompts import inject_tool_prompt

logger = logging.getLogger(__name__)

# 上游 GenAI 没有 Anthropic 的 thinking 签名，这里使用明确的兼容占位值，
# 并且在把历史 thinking 块转回 GenAI 时会直接丢弃，不会伪造真实签名。
COMPAT_THINKING_SIGNATURE = "genai-compat-no-signature"
BARE_TOOL_CALL_RE = re.compile(r'\{\s*"name"\s*:\s*"')

DSML_OPEN_RE = r"<\s*[|｜]DSML[|｜](?:tool_calls|invoke|parameter)\b"
DSML_CLOSE_RE = r"</\s*[|｜]DSML[|｜](?:tool_calls|invoke|parameter)\s*>"
THINK_OPEN_PREFIX = "<think"
THINK_CLOSE = "</think>"


def anthropic_content_to_text(content: Any) -> str:
    """Convert Anthropic content format to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    text_parts: List[str] = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue
        part_type = part.get("type")
        if part_type == "text":
            text_parts.append(part.get("text", ""))
        elif part_type == "tool_use":
            text_parts.append(
                f"[tool_use name={part.get('name', '')} id={part.get('id', '')}] "
                f"{json.dumps(part.get('input', {}), ensure_ascii=False)}"
            )
        elif part_type == "tool_result":
            text_parts.append(f"[tool_result id={part.get('tool_use_id', '')}] {tool_result_content_to_text(part.get('content', ''))}")
        elif part_type in ("thinking", "redacted_thinking"):
            # thinking 块是本地展示用的，不重新喂给上游模型
            continue
        elif part_type == "image":
            text_parts.append("[image]")
        else:
            text_parts.append(json.dumps(part, ensure_ascii=False))
    return "\n".join(part for part in text_parts if part)


def json_dumps_compact(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def anthropic_allowed_tool_names(body: Dict[str, Any]) -> set[str]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return set()
    return {
        tool["name"]
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str) and tool["name"]
    }


def anthropic_tools_to_openai_tools(tools: Any) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted

    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        converted.append({
            "type": "function",
            "function": {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
            },
        })
    return converted


def anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict):
        return None
    choice_type = tool_choice.get("type")
    if choice_type == "any":
        return "required"
    if choice_type == "tool" and tool_choice.get("name"):
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    if choice_type == "none":
        return "none"
    return None


def tool_result_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return anthropic_content_to_text(content)
    if content is None:
        return ""
    return str(content)


def escape_tool_result_attr(value: Any) -> str:
    return str(value or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def anthropic_tool_results_visible_text(tool_results: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for result in tool_results:
        attrs = [f'tool_use_id="{escape_tool_result_attr(result.get("tool_use_id", ""))}"']
        if "is_error" in result:
            attrs.append(f'is_error="{str(bool(result.get("is_error"))).lower()}"')
        content = tool_result_content_to_text(result.get("content", ""))
        blocks.append(f"<tool_result {' '.join(attrs)}>\n{content}\n</tool_result>")
    return "<tool_results>\n" + "\n".join(blocks) + "\n</tool_results>" if blocks else ""


def split_anthropic_content(content: Any) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    text_parts: List[str] = []
    tool_uses: List[Dict[str, Any]] = []
    tool_results: List[Dict[str, Any]] = []
    if isinstance(content, str):
        return content, tool_uses, tool_results
    if not isinstance(content, list):
        return ("" if content is None else str(content)), tool_uses, tool_results

    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            text_parts.append(str(part))
            continue
        part_type = part.get("type")
        if part_type == "text":
            text_parts.append(part.get("text", ""))
        elif part_type == "tool_use":
            tool_uses.append(part)
        elif part_type == "tool_result":
            tool_results.append(part)
        elif part_type in ("thinking", "redacted_thinking"):
            # 历史里的 thinking 块不参与 GenAI 转换
            continue
        elif part_type == "image":
            text_parts.append("[image]")
        else:
            text_parts.append(json.dumps(part, ensure_ascii=False))

    return "\n".join(part for part in text_parts if part), tool_uses, tool_results


def anthropic_message_to_genai_messages(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    role = message.get("role", "user")
    if role not in ("user", "assistant", "system"):
        role = "user"

    text, tool_uses, tool_results = split_anthropic_content(message.get("content", ""))
    if tool_results:
        visible_text = anthropic_tool_results_visible_text(tool_results)
        if text:
            visible_text = f"{visible_text}\n\n{text}" if visible_text else text
        return [{"role": "user", "content": visible_text}]

    if role == "assistant" and tool_uses:
        content = text or ""
        for tool_use in tool_uses:
            call_obj = {
                "name": tool_use.get("name", ""),
                "arguments": tool_use.get("input", {}),
            }
            content += f"\n<tool_call>\n{json.dumps(call_obj, ensure_ascii=False)}\n</tool_call>"
        return [{"role": "assistant", "content": content.strip()}]

    return [{"role": role, "content": text}]


def anthropic_messages_to_genai_format(body: Dict[str, Any], token: str) -> Tuple[str, List[Dict[str, Any]], str]:
    """Convert Anthropic Messages API format to GenAI format.

    Returns:
        Tuple of (system_prompt, messages, model)
    """
    model = body.get("model", "GPT-5.5")
    # Extract system prompt
    system_texts: List[str] = []
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        system_texts.append(system)
    elif isinstance(system, list):
        for item in system:
            if isinstance(item, dict) and item.get("type") == "text":
                system_texts.append(item.get("text", ""))
            elif isinstance(item, str):
                system_texts.append(item)

    system_prompt = "\n\n".join(system_texts)

    # Convert messages
    messages: List[Dict[str, Any]] = []
    for message in body.get("messages", []):
        if not isinstance(message, dict):
            continue
        messages.extend(anthropic_message_to_genai_messages(message))

    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    openai_tools = anthropic_tools_to_openai_tools(body.get("tools"))
    tool_choice = anthropic_tool_choice_to_openai(body.get("tool_choice"))
    if openai_tools and tool_choice != "none":
        messages = inject_tool_prompt(messages, openai_tools, tool_choice)

    return system_prompt, messages, model


def filter_thinking_and_dsml(text: str) -> str:
    """Filter out <think>...</think> and DSML tool tags."""
    # Remove thinking tags
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)

    # Remove DSML tags
    text = re.sub(DSML_OPEN_RE, "", text, flags=re.IGNORECASE)
    text = re.sub(DSML_CLOSE_RE, "", text, flags=re.IGNORECASE)

    return text


def partial_marker_start(text: str, position: int, marker: str) -> int:
    marker = marker.lower()
    for index in range(len(text) - 1, position - 1, -1):
        suffix = text[index:].lower()
        if marker.startswith(suffix) and suffix != marker:
            return index
    return -1


def filter_thinking_text_delta(text: str, state: Dict[str, Any]) -> str:
    """Filter thinking markup even when <think> spans multiple stream chunks."""
    pending = state.pop("pending_think_open", "")
    if pending:
        text = pending + text

    output: List[str] = []
    position = 0
    while position < len(text):
        lower = text.lower()
        if state.get("in_thinking"):
            close_index = lower.find(THINK_CLOSE, position)
            if close_index < 0:
                return "".join(output)
            position = close_index + len(THINK_CLOSE)
            state["in_thinking"] = False
            continue

        open_index = lower.find(THINK_OPEN_PREFIX, position)
        stray_close_index = lower.find(THINK_CLOSE, position)
        if stray_close_index >= 0 and (open_index < 0 or stray_close_index < open_index):
            output.append(text[position:stray_close_index])
            position = stray_close_index + len(THINK_CLOSE)
            continue

        if open_index < 0:
            partial_open = partial_marker_start(text, position, THINK_OPEN_PREFIX)
            if partial_open >= 0:
                output.append(text[position:partial_open])
                state["pending_think_open"] = text[partial_open:]
            else:
                output.append(text[position:])
            break

        output.append(text[position:open_index])
        tag_end = text.find(">", open_index)
        if tag_end < 0:
            state["in_thinking"] = True
            break
        position = tag_end + 1
        state["in_thinking"] = True

    return filter_thinking_and_dsml("".join(output))


def strip_thinking_markup(text: str) -> str:
    cleaned = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"^\s*</think>\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*<think\b[^>]*>.*$", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned.strip()


def strip_markdown_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json|JSON)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return match.group(1).strip() if match else stripped


def extract_balanced_json(text: str) -> Optional[str]:
    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if not starts:
        return None
    start = min(starts)
    stack: List[str] = []
    in_string = False
    escape = False
    quote = ""
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                in_string = False
            continue
        if char in ("'", '"'):
            in_string = True
            quote = char
            continue
        if char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return text[start:index + 1]
    return None


def quote_unquoted_json_keys(text: str) -> str:
    return re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)', r'\1"\2"\3', text)


def parse_json_like_object(arguments: str) -> Optional[Any]:
    candidates = []
    cleaned = strip_markdown_json_fence(strip_thinking_markup(arguments))
    if cleaned:
        candidates.append(cleaned)
    balanced = extract_balanced_json(cleaned or arguments)
    if balanced and balanced not in candidates:
        candidates.append(balanced)

    for candidate in candidates:
        for value in (candidate, quote_unquoted_json_keys(candidate)):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
            try:
                return ast.literal_eval(value)
            except (SyntaxError, ValueError):
                pass
    return None


def parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        if set(arguments) == {"arguments"}:
            nested_value = arguments.get("arguments")
            if isinstance(nested_value, dict):
                return parse_tool_arguments(nested_value)
            if isinstance(nested_value, str):
                return parse_tool_arguments(nested_value)
        return arguments
    if not isinstance(arguments, str) or not arguments.strip():
        return {}
    parsed = parse_json_like_object(arguments)
    if parsed is None:
        return {"arguments": arguments}
    if isinstance(parsed, str):
        nested = parse_tool_arguments(parsed)
        return nested if nested else {"arguments": parsed}
    if isinstance(parsed, dict):
        return parse_tool_arguments(parsed)
    return {"arguments": parsed}


def normalize_tool_input(tool_name: str, arguments: Any) -> Dict[str, Any]:
    parsed = parse_tool_arguments(arguments)
    aliases = {
        "Grep": {
            "cpattern": "pattern",
            "regex": "pattern",
            "file_path": "path",
            "filePath": "path",
        },
        "Glob": {
            "glob": "pattern",
            "file_path": "path",
            "filePath": "path",
        },
        "Read": {
            "path": "file_path",
            "filePath": "file_path",
        },
    }
    for alias, canonical in aliases.get(tool_name, {}).items():
        if canonical not in parsed and alias in parsed:
            parsed[canonical] = parsed.pop(alias)

    if tool_name == "Bash" and "command" not in parsed:
        value = parsed.get("arguments")
        if isinstance(value, str) and value:
            return {"command": value}
        if isinstance(value, dict):
            nested = normalize_tool_input(tool_name, value)
            if nested.get("command"):
                return nested
    return parsed


def tool_input_json(tool_name: str, arguments: Any) -> str:
    return json_dumps_compact(normalize_tool_input(tool_name, arguments))


def stream_genai_as_anthropic(
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
    token: str,
    config: Any,
    allowed_tool_names: Optional[set[str]] = None,
) -> Generator[str, None, None]:
    """Stream GenAI response in Anthropic Messages API format."""
    started_at = time.monotonic()
    first_token_at = None
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    message_id = f"msg_{uuid.uuid4().hex[:24]}"

    # 取出最后一条 user 消息作为当前提问（chatInfo），其余作为历史
    chat_info = ""
    history_messages = list(messages)
    for index in range(len(history_messages) - 1, -1, -1):
        if history_messages[index].get("role") == "user":
            chat_info = anthropic_content_to_text(history_messages[index].get("content", ""))
            del history_messages[index]
            break

    def write_sse(event: str, data: Dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"

    yield write_sse("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })

    next_index = 0
    thinking_index = None
    text_index = None
    text_block_open = False
    text_block_done = False
    tool_detected = False
    buffer = ""
    tool_buffer = ""
    thinking_state: Dict[str, Any] = {"in_thinking": False}

    def open_thinking_block():
        nonlocal thinking_index, next_index
        thinking_index = next_index
        next_index += 1
        return write_sse("content_block_start", {
            "type": "content_block_start",
            "index": thinking_index,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        })

    def close_thinking_block():
        nonlocal thinking_index
        if thinking_index is None:
            return
        index = thinking_index
        thinking_index = None
        yield write_sse("content_block_delta", {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": COMPAT_THINKING_SIGNATURE},
        })
        yield write_sse("content_block_stop", {"type": "content_block_stop", "index": index})

    def emit_text(text: str):
        nonlocal first_token_at, text_index, next_index, text_block_open
        if not text:
            return
        if first_token_at is None:
            first_token_at = time.monotonic()
        if not text_block_open:
            text_index = next_index
            next_index += 1
            text_block_open = True
            yield write_sse("content_block_start", {
                "type": "content_block_start",
                "index": text_index,
                "content_block": {"type": "text", "text": ""},
            })
        content_parts.append(text)
        yield write_sse("content_block_delta", {
            "type": "content_block_delta",
            "index": text_index,
            "delta": {"type": "text_delta", "text": text},
        })

    def close_text_block():
        nonlocal text_block_open, text_block_done
        if text_block_open and not text_block_done:
            text_block_done = True
            yield write_sse("content_block_stop", {"type": "content_block_stop", "index": text_index})

    for event in iter_genai_stream(chat_info, history_messages, model, max_tokens, config, token=token):
        if event["type"] == "error":
            yield write_sse("error", {
                "type": "error",
                "error": {"type": "api_error", "message": event["message"]},
            })
            return

        if event["type"] == "delta":
            reasoning = event.get("reasoning")
            # thinking block 必须排在所有正文之前，正文开始后无法再插入
            if reasoning and not (text_block_open or text_block_done or tool_detected):
                if thinking_index is None:
                    yield open_thinking_block()
                reasoning_parts.append(reasoning)
                yield write_sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": thinking_index,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                })

            content = event.get("content")
            if content:
                content = filter_thinking_text_delta(content, thinking_state)
            if not content:
                continue

            if thinking_index is not None:
                yield from close_thinking_block()

            if tool_detected:
                tool_buffer += content
                continue

            buffer += content
            tag_pos = buffer.find("<tool_call")
            if tag_pos >= 0:
                yield from emit_text(buffer[:tag_pos])
                tool_detected = True
                tool_buffer = buffer[tag_pos:]
                buffer = ""
                continue

            bare_tool_match = BARE_TOOL_CALL_RE.search(buffer) if allowed_tool_names else None
            if bare_tool_match:
                yield from emit_text(buffer[:bare_tool_match.start()])
                tool_detected = True
                tool_buffer = buffer[bare_tool_match.start():]
                buffer = ""
                continue

            if allowed_tool_names and text_index is None and buffer.lstrip().startswith("{"):
                continue

            prefix_len = _tag_prefix_len(buffer, "<tool_call")
            if prefix_len > 0:
                yield from emit_text(buffer[:-prefix_len])
                buffer = buffer[-prefix_len:]
            else:
                yield from emit_text(buffer)
                buffer = ""
            continue

        # done：先冲刷残留文本，再输出工具调用
        stop_reason = "end_turn"
        if tool_detected:
            tool_buffer += buffer
            buffer = ""
            tool_calls, remaining = extract_tool_calls(
                tool_buffer,
                allowed_tool_names=allowed_tool_names,
            )
            if tool_calls:
                if remaining:
                    yield from emit_text(remaining)
                yield from close_thinking_block()
                yield from close_text_block()

                for offset, tool_call in enumerate(tool_calls):
                    function = tool_call.get("function", {})
                    block_index = next_index + offset
                    tool_id = tool_call.get("id") or f"toolu_genai_{uuid.uuid4().hex[:24]}"
                    tool_name = function.get("name") or "tool"
                    yield write_sse("content_block_start", {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": tool_name,
                            "input": {},
                        },
                    })
                    yield write_sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": tool_input_json(tool_name, function.get("arguments", "{}")),
                        },
                    })
                    yield write_sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": block_index,
                    })
                next_index += len(tool_calls)
                stop_reason = "tool_use"
            else:
                logger.warning("Tool tag detected but parsing failed; emitting as text")
                yield from emit_text(tool_buffer)
        else:
            yield from emit_text(buffer)

        yield from close_thinking_block()
        yield from close_text_block()

        # Calculate output tokens
        output_text = "".join(content_parts)
        output_tokens = max(1, len(output_text) // 4) if output_text else 0

        yield write_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        })

        yield write_sse("message_stop", {"type": "message_stop"})

        total_elapsed = time.monotonic() - started_at
        ttft_ms = (first_token_at - started_at) * 1000 if first_token_at else 0
        logger.info(
            "stream metrics model=%s output_chars=%d output_tokens=%d ttft_ms=%.0f total=%.2fs",
            model,
            len(output_text),
            output_tokens,
            ttft_ms,
            total_elapsed,
        )
        return
