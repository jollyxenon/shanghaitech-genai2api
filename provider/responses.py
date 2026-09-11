import json
import logging
import time
import uuid
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from provider.anthropic import parse_tool_arguments
from provider.genai import estimate_text_tokens, iter_genai_stream, split_history_messages
from tools.parsing import extract_tool_calls, find_tool_call_open, tool_call_prefix_len
from tools.prompts import flatten_message_content, inject_tool_prompt

logger = logging.getLogger(__name__)


def _content_to_text(content: Any) -> str:
    """把 Responses 的 content（字符串或 part 数组）压平成文本。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                part_type = part.get("type")
                if part_type in ("input_text", "output_text", "text", "summary_text"):
                    parts.append(part.get("text", ""))
                elif part_type in ("input_image", "image_url"):
                    parts.append("[image]")
                elif part_type == "input_audio":
                    parts.append("[audio]")
        return "\n".join(part for part in parts if part)
    return str(content)


def responses_tools_to_chat_tools(tools: Any) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """把 Responses 的工具定义转成 Chat Completions 格式，并返回 custom 工具名集合。

    Codex 会把顶层 function/custom 工具包在一个 `type: "namespace"` 里发送，
    这里一并展平；custom 工具（自由文本，例如 apply_patch）用单字段 input 表达。
    """
    chat_tools: List[Dict[str, Any]] = []
    custom_tool_names: Set[str] = set()

    def add_function(tool: Dict[str, Any]) -> None:
        name = tool.get("name")
        if not name:
            return
        chat_tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        })

    def add_custom(tool: Dict[str, Any]) -> None:
        name = tool.get("name")
        if not name:
            return
        description = tool.get("description", "")
        definition = (tool.get("format") or {}).get("definition")
        if definition:
            description = f"{description}\n\n自由文本格式定义：\n{definition}"
        chat_tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "string",
                            "description": "该工具的完整自由文本输入",
                        }
                    },
                    "required": ["input"],
                },
            },
        })
        custom_tool_names.add(name)

    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type == "function":
            add_function(tool)
        elif tool_type == "custom":
            add_custom(tool)
        elif tool_type == "namespace":
            for inner in tool.get("tools") or []:
                if not isinstance(inner, dict):
                    continue
                if inner.get("type") == "custom":
                    add_custom(inner)
                elif inner.get("type") == "function":
                    add_function(inner)

    return chat_tools, custom_tool_names


def responses_tool_choice_to_chat(tool_choice: Any) -> Any:
    """把 Responses 的 tool_choice 映射成 inject_tool_prompt 认识的形态。"""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "none":
            return "none"
        if tool_choice == "required":
            return "required"
        return None
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function" and tool_choice.get("name"):
            return {"type": "function", "function": {"name": tool_choice["name"]}}
        if tool_choice.get("type") == "allowed_tools" and tool_choice.get("mode") == "required":
            return "required"
    return None


def _merge_adjacent_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并连续的同角色消息，避免把同一轮拆成多条。"""
    merged: List[Dict[str, Any]] = []
    for message in messages:
        if (
            merged
            and merged[-1]["role"] == message["role"]
            and message["role"] in ("assistant", "user")
        ):
            previous = merged[-1]["content"]
            current = message["content"]
            merged[-1]["content"] = f"{previous}\n{current}".strip() if previous else current
        else:
            merged.append(dict(message))
    return merged


def responses_input_to_genai_messages(input_value: Any, instructions: Any = None) -> List[Dict[str, Any]]:
    """把 Responses 的 input items 转换成 GenAI 的 messages。"""
    if isinstance(input_value, str):
        items: List[Any] = [{"type": "message", "role": "user", "content": input_value}]
    elif isinstance(input_value, list):
        items = input_value
    else:
        items = []

    messages: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue

        item_type = item.get("type") or "message"
        if item_type == "message":
            role = item.get("role", "user")
            if role == "developer":
                role = "system"
            if role not in ("user", "assistant", "system"):
                role = "user"
            messages.append({"role": role, "content": _content_to_text(item.get("content"))})
        elif item_type == "function_call":
            call_obj = {
                "name": item.get("name", ""),
                "arguments": parse_tool_arguments(item.get("arguments", {})),
            }
            messages.append({
                "role": "assistant",
                "content": f"<tool_call>\n{json.dumps(call_obj, ensure_ascii=False)}\n</tool_call>",
            })
        elif item_type in ("custom_tool_call",):
            call_obj = {
                "name": item.get("name", ""),
                "arguments": {"input": item.get("input", "")},
            }
            messages.append({
                "role": "assistant",
                "content": f"<tool_call>\n{json.dumps(call_obj, ensure_ascii=False)}\n</tool_call>",
            })
        elif item_type in ("function_call_output", "custom_tool_call_output"):
            output = _content_to_text(item.get("output"))
            call_id = item.get("call_id", "")
            messages.append({
                "role": "user",
                "content": (
                    "<tool_result>\n"
                    f"  <tool_call_id>{call_id}</tool_call_id>\n"
                    f"  <result>\n{output}\n  </result>\n"
                    "</tool_result>"
                ),
            })
        elif item_type in ("reasoning", "item_reference"):
            # 历史思维链与引用不重新喂给上游模型
            continue
        else:
            logger.debug("Ignoring unsupported Responses input item type: %s", item_type)

    text = _content_to_text(instructions)
    if text.strip():
        messages.insert(0, {"role": "system", "content": text})

    return _merge_adjacent_messages(messages)


def _custom_tool_input(arguments: Any) -> str:
    """custom 工具的自由文本输入。"""
    parsed = parse_tool_arguments(arguments)
    if isinstance(parsed, dict):
        value = parsed.get("input")
        if isinstance(value, str):
            return value
        if value is not None:
            return json.dumps(value, ensure_ascii=False)
    if isinstance(arguments, str):
        return arguments
    return json.dumps(parsed, ensure_ascii=False)


def _split_chat_info(messages: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
    """取出最后一条 user 作为当前提问，其余作为历史。"""
    chat_info = ""
    history = list(messages)
    for index in range(len(history) - 1, -1, -1):
        if history[index].get("role") == "user":
            chat_info = flatten_message_content(history[index].get("content", ""))
            del history[index]
            break
    return chat_info, history


def _base_response(
    response_id: str,
    created_at: int,
    model: str,
    status: str,
    output: List[Dict[str, Any]],
    instructions: Any,
    max_output_tokens: Optional[int],
    response_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    meta = response_meta or {}
    return {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output,
        "instructions": instructions if isinstance(instructions, str) else None,
        "parallel_tool_calls": meta.get("parallel_tool_calls", True),
        "tool_choice": meta.get("tool_choice") or "auto",
        "tools": meta.get("tools") or [],
        "error": None,
        "incomplete_details": None,
        "metadata": {},
        "temperature": None,
        "top_p": None,
        "max_output_tokens": max_output_tokens,
        "previous_response_id": None,
        "reasoning": meta.get("reasoning"),
        "store": meta.get("store", False),
        "text": {"format": {"type": "text"}},
        "truncation": "disabled",
        "usage": None,
        "user": None,
    }


def stream_genai_as_responses(
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int,
    config: Any,
    instructions: Any = None,
    custom_tool_names: Optional[Set[str]] = None,
    allowed_tool_names: Optional[Set[str]] = None,
    max_output_tokens: Optional[int] = None,
    response_meta: Optional[Dict[str, Any]] = None,
) -> Generator[str, None, None]:
    """把 GenAI 流转换成 OpenAI Responses API 的 SSE 事件。"""
    custom_tool_names = custom_tool_names or set()
    response_id = f"resp_{uuid.uuid4().hex[:24]}"
    created_at = int(time.time())
    sequence_number = 0

    chat_info, history_messages = _split_chat_info(messages)

    def ev(name: str, payload: Dict[str, Any]) -> str:
        nonlocal sequence_number
        body = {"type": name, "sequence_number": sequence_number}
        body.update(payload)
        sequence_number += 1
        return f"event: {name}\ndata: {json.dumps(body, ensure_ascii=False, separators=(',', ':'))}\n\n"

    def base(status: str, output: List[Dict[str, Any]]) -> Dict[str, Any]:
        return _base_response(
            response_id, created_at, model, status, output, instructions,
            max_output_tokens, response_meta,
        )

    yield ev("response.created", {"response": base("in_progress", [])})
    yield ev("response.in_progress", {"response": base("in_progress", [])})

    output_items: List[Dict[str, Any]] = []
    next_output_index = 0

    reasoning_item_id: Optional[str] = None
    reasoning_index: Optional[int] = None
    reasoning_parts: List[str] = []

    message_item_id: Optional[str] = None
    message_index: Optional[int] = None
    message_parts: List[str] = []
    message_closed = False

    buffer = ""
    tool_buffer = ""
    tool_detected = False

    def start_reasoning():
        nonlocal reasoning_item_id, reasoning_index, next_output_index
        reasoning_item_id = f"rs_{uuid.uuid4().hex[:24]}"
        reasoning_index = next_output_index
        next_output_index += 1
        reasoning_parts.clear()
        item = {
            "id": reasoning_item_id,
            "type": "reasoning",
            "summary": [],
            "content": [],
            "encrypted_content": None,
            "status": "in_progress",
        }
        yield ev("response.output_item.added", {"output_index": reasoning_index, "item": item})
        yield ev("response.reasoning_summary_part.added", {
            "item_id": reasoning_item_id,
            "output_index": reasoning_index,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": ""},
        })

    def finish_reasoning():
        nonlocal reasoning_item_id
        if reasoning_item_id is None:
            return
        text = "".join(reasoning_parts)
        item_id = reasoning_item_id
        index = reasoning_index
        reasoning_item_id = None
        yield ev("response.reasoning_summary_text.done", {
            "item_id": item_id,
            "output_index": index,
            "summary_index": 0,
            "text": text,
        })
        yield ev("response.reasoning_summary_part.done", {
            "item_id": item_id,
            "output_index": index,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": text},
        })
        item = {
            "id": item_id,
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": text}],
            "content": [],
            "encrypted_content": None,
            "status": "completed",
        }
        yield ev("response.output_item.done", {"output_index": index, "item": item})
        output_items.append(item)

    def start_message():
        nonlocal message_item_id, message_index, next_output_index
        message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        message_index = next_output_index
        next_output_index += 1
        message_parts.clear()
        item = {
            "id": message_item_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        }
        yield ev("response.output_item.added", {"output_index": message_index, "item": item})
        yield ev("response.content_part.added", {
            "item_id": message_item_id,
            "output_index": message_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        })

    def message_delta(delta: str):
        yield ev("response.output_text.delta", {
            "item_id": message_item_id,
            "output_index": message_index,
            "content_index": 0,
            "delta": delta,
            "logprobs": [],
        })

    def finish_message():
        nonlocal message_item_id, message_closed
        if message_item_id is None:
            return
        text = "".join(message_parts)
        item_id = message_item_id
        index = message_index
        message_item_id = None
        message_closed = True
        yield ev("response.output_text.done", {
            "item_id": item_id,
            "output_index": index,
            "content_index": 0,
            "text": text,
            "logprobs": [],
        })
        yield ev("response.content_part.done", {
            "item_id": item_id,
            "output_index": index,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        })
        item = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        yield ev("response.output_item.done", {"output_index": index, "item": item})
        output_items.append(item)

    def emit_tool_calls(tool_calls: List[Dict[str, Any]]):
        nonlocal next_output_index
        for tool_call in tool_calls:
            function = tool_call.get("function", {})
            name = function.get("name") or "tool"
            arguments = function.get("arguments", "{}")
            call_id = tool_call.get("id") or f"call_{uuid.uuid4().hex[:24]}"

            if name in custom_tool_names:
                item_id = f"ctc_{uuid.uuid4().hex[:24]}"
                index = next_output_index
                next_output_index += 1
                input_text = _custom_tool_input(arguments)
                yield ev("response.output_item.added", {
                    "output_index": index,
                    "item": {
                        "id": item_id,
                        "type": "custom_tool_call",
                        "call_id": call_id,
                        "name": name,
                        "input": "",
                    },
                })
                yield ev("response.custom_tool_call_input.delta", {
                    "item_id": item_id,
                    "output_index": index,
                    "delta": input_text,
                })
                yield ev("response.custom_tool_call_input.done", {
                    "item_id": item_id,
                    "output_index": index,
                    "input": input_text,
                })
                item = {
                    "id": item_id,
                    "type": "custom_tool_call",
                    "call_id": call_id,
                    "name": name,
                    "input": input_text,
                    "status": "completed",
                }
                yield ev("response.output_item.done", {"output_index": index, "item": item})
                output_items.append(item)
            else:
                item_id = f"fc_{uuid.uuid4().hex[:24]}"
                index = next_output_index
                next_output_index += 1
                yield ev("response.output_item.added", {
                    "output_index": index,
                    "item": {
                        "id": item_id,
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": call_id,
                        "name": name,
                        "arguments": "",
                    },
                })
                yield ev("response.function_call_arguments.delta", {
                    "item_id": item_id,
                    "output_index": index,
                    "delta": arguments,
                })
                yield ev("response.function_call_arguments.done", {
                    "item_id": item_id,
                    "output_index": index,
                    "arguments": arguments,
                })
                item = {
                    "id": item_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
                yield ev("response.output_item.done", {"output_index": index, "item": item})
                output_items.append(item)

    for event in iter_genai_stream(chat_info, history_messages, model, max_tokens, config):
        if event["type"] == "error":
            yield ev("error", {"code": None, "message": event["message"], "param": None})
            return

        if event["type"] == "delta":
            reasoning = event.get("reasoning")
            # reasoning 必须排在最前面，正文开始后无法再插入
            if reasoning and not (message_item_id or message_closed or tool_detected):
                if reasoning_item_id is None:
                    yield from start_reasoning()
                reasoning_parts.append(reasoning)
                yield ev("response.reasoning_summary_text.delta", {
                    "item_id": reasoning_item_id,
                    "output_index": reasoning_index,
                    "summary_index": 0,
                    "delta": reasoning,
                })

            content = event.get("content")
            if not content:
                continue

            if reasoning_item_id is not None:
                yield from finish_reasoning()

            if tool_detected:
                tool_buffer += content
                continue

            buffer += content
            tag_pos = find_tool_call_open(buffer)
            if tag_pos >= 0:
                pre = buffer[:tag_pos]
                if pre:
                    if message_item_id is None:
                        yield from start_message()
                    message_parts.append(pre)
                    yield from message_delta(pre)
                tool_detected = True
                tool_buffer = buffer[tag_pos:]
                buffer = ""
                continue

            prefix_len = tool_call_prefix_len(buffer)
            if prefix_len > 0:
                safe = buffer[:-prefix_len]
                if safe:
                    if message_item_id is None:
                        yield from start_message()
                    message_parts.append(safe)
                    yield from message_delta(safe)
                buffer = buffer[-prefix_len:]
            else:
                if buffer:
                    if message_item_id is None:
                        yield from start_message()
                    message_parts.append(buffer)
                    yield from message_delta(buffer)
                buffer = ""
            continue

        # done
        yield from finish_reasoning()

        if tool_detected:
            tool_buffer += buffer
            buffer = ""
            tool_calls, remaining = extract_tool_calls(
                tool_buffer, allowed_tool_names=allowed_tool_names
            )
            if tool_calls:
                if remaining:
                    if message_item_id is None:
                        yield from start_message()
                    message_parts.append(remaining)
                    yield from message_delta(remaining)
            else:
                logger.warning("Tool tag detected but parsing failed; emitting as text")
                emit_target = remaining if remaining is not None else tool_buffer
                if emit_target:
                    if message_item_id is None:
                        yield from start_message()
                    message_parts.append(emit_target)
                    yield from message_delta(emit_target)
                tool_calls = []

            yield from finish_message()
            if tool_calls:
                yield from emit_tool_calls(tool_calls)
        else:
            if buffer:
                if message_item_id is None:
                    yield from start_message()
                message_parts.append(buffer)
                yield from message_delta(buffer)
            yield from finish_message()

        output_text = "".join(message_parts)
        reasoning_text = "".join(reasoning_parts)
        output_tokens = estimate_text_tokens(output_text) + estimate_text_tokens(reasoning_text)
        input_tokens = estimate_text_tokens(chat_info) + sum(
            estimate_text_tokens(str(message.get("content", ""))) for message in history_messages
        )

        truncated = event.get("finish_reason") == "length"
        response_object = base("incomplete" if truncated else "completed", output_items)
        response_object["usage"] = {
            "input_tokens": input_tokens,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
            "output_tokens": output_tokens,
            "output_tokens_details": {"reasoning_tokens": estimate_text_tokens(reasoning_text)},
            "total_tokens": input_tokens + output_tokens,
        }
        if truncated:
            response_object["incomplete_details"] = {"reason": "max_output_tokens"}
            yield ev("response.incomplete", {"response": response_object})
        else:
            yield ev("response.completed", {"response": response_object})

        logger.info(
            "responses stream model=%s items=%d output_tokens=%d truncated=%s",
            model,
            len(output_items),
            output_tokens,
            truncated,
        )
        return
