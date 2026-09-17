import base64
import json


TOOL_SYSTEM_PROMPT = """\
You have access to the following tools:

<tools>
{tool_definitions}
</tools>

When you need to call a tool, you MUST use the following XML format. Do NOT use markdown code blocks.

<tool_call>
{{"name": "<function-name>", "arguments": {{<arguments-as-json>}}}}
</tool_call>

{tool_examples}

Rules:
1. You can call multiple tools by using multiple <tool_call> blocks.
2. If you don't need any tool, just respond normally in plain text without any <tool_call> tags.
3. After receiving tool results, analyze them and either call more tools or give a final answer in plain text.
4. The "arguments" field MUST be a valid JSON object matching the tool's parameter schema.
5. NEVER use <arg_key>, <arg_value>, dotted names like Grep.datasource, or a bare tool name.
6. NEVER wrap <tool_call> in markdown code blocks like ```xml or ```json."""

TOOL_CHOICE_REQUIRED_PROMPT = "\nYou MUST call at least one tool in your response. Do NOT respond with plain text only."
TOOL_CHOICE_SPECIFIC_PROMPT = (
    '\nYou MUST call the tool named "{name}" in your response, '
    "and you MUST NOT call any other tool."
)

COMMON_TOOL_EXAMPLES = {
    "Bash": {"command": "pwd"},
    "Read": {"file_path": "/absolute/path/to/file"},
    "Glob": {"pattern": "**/*.py", "path": "/absolute/project/path"},
    "Grep": {
        "pattern": "search text",
        "path": "/absolute/project/path",
        "output_mode": "content",
    },
    "LS": {"path": "/absolute/project/path"},
}


def normalize_content(content):
    """将三种 API 的文本、图片和文档转换成统一内容块，不丢失附件。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("message content 必须是字符串或内容块数组")
    parts = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            raise ValueError("content 中的每一项必须是内容块")
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text", "summary_text"):
            if not isinstance(part.get("text", ""), str):
                raise ValueError("text 内容必须是字符串")
            parts.append({"type": "text", "text": part.get("text", "")})
        elif kind in ("thinking", "redacted_thinking"):
            continue
        elif kind in ("image_url", "input_image", "image"):
            source = part.get("source") or {}
            if not isinstance(source, dict):
                raise ValueError("图片 source 必须是对象")
            image = part.get("image_url") or source.get("url")
            if isinstance(image, str):
                image = {"url": image}
            if source.get("type") == "base64":
                image = {"url": f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"}
            if not isinstance(image, dict) or not isinstance(image.get("url"), str) or not image["url"]:
                raise ValueError("图片需要 image_url、URL source 或 base64 source；不支持 file_id")
            parts.append({"type": "image_url", "image_url": dict(image)})
        elif kind in ("file", "input_file", "document"):
            source = part.get("source") or {}
            if not isinstance(source, dict) or not isinstance(part.get("file", {}), dict):
                raise ValueError("文档 source/file 必须是对象")
            file = dict(part.get("file") or part)
            if kind == "document":
                file = {"filename": part.get("title") or "document"}
                if source.get("type") == "base64":
                    file["file_data"] = f"data:{source.get('media_type', 'application/pdf')};base64,{source.get('data', '')}"
                elif source.get("type") == "url":
                    file["file_url"] = source.get("url")
                elif source.get("type") == "text":
                    encoded = base64.b64encode(source.get("data", "").encode()).decode()
                    file["file_data"] = "data:text/plain;base64," + encoded
                else:
                    raise ValueError("文档 source 需要 base64、url 或 text")
            if not file.get("file_data") and not file.get("file_url"):
                raise ValueError("文件需要 file_data 或 file_url；代理不提供 Files API/file_id 存储")
            parts.append({"type": "file", "file": file})
        else:
            raise ValueError(f"不支持的消息内容类型: {kind}")
    if all(part["type"] == "text" for part in parts):
        return "\n".join(part["text"] for part in parts if part["text"])
    return parts


def merge_content(first, second):
    """合并同一轮的内容并保留图片和文档。"""
    if isinstance(first, str) and isinstance(second, str):
        return "\n".join(text for text in (first, second) if text)
    left = [{"type": "text", "text": first}] if isinstance(first, str) else first
    right = [{"type": "text", "text": second}] if isinstance(second, str) else second
    return (left or []) + (right or [])


def flatten_message_content(content):
    """仅提取可读文本，用于当前问题和用量估算，不把附件编码当提示词。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (flatten_message_content(item) for item in content)))
    if isinstance(content, dict):
        if content.get("type") in ("image_url", "input_image", "image", "file", "input_file", "document"):
            return ""
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return flatten_message_content(content["content"])
        if "input" in content:
            return json.dumps(content["input"], ensure_ascii=False)
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def normalize_message_content(message):
    """规范化消息而不压平多模态内容。"""
    return {**message, "content": normalize_content(message.get("content", ""))}


def format_tool_definitions(tools):
    definitions = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool["function"]
        params = func.get("parameters", {})
        params_json = json.dumps(params, ensure_ascii=False, indent=2)
        definitions.append(
            f"<tool_definition>\n"
            f"  <name>{func['name']}</name>\n"
            f"  <description>{func.get('description', '')}</description>\n"
            f"  <parameters>\n{params_json}\n  </parameters>\n"
            f"</tool_definition>"
        )
    return "\n".join(definitions)


def format_tool_examples(tools):
    examples = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        name = tool.get("function", {}).get("name")
        if name not in COMMON_TOOL_EXAMPLES:
            continue
        call_obj = {
            "name": name,
            "arguments": COMMON_TOOL_EXAMPLES[name],
        }
        examples.append(
            "<tool_call>\n"
            f"{json.dumps(call_obj, ensure_ascii=False)}\n"
            "</tool_call>"
        )

    if not examples:
        return ""
    return "Examples of valid tool calls:\n" + "\n".join(examples)


def inject_tool_prompt(messages, tools, tool_choice=None):
    tool_defs = format_tool_definitions(tools)
    tool_examples = format_tool_examples(tools)
    tool_prompt = TOOL_SYSTEM_PROMPT.format(
        tool_definitions=tool_defs,
        tool_examples=tool_examples,
    )

    if tool_choice == "required":
        tool_prompt += TOOL_CHOICE_REQUIRED_PROMPT
    elif isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = tool_choice["function"]["name"]
        tool_prompt += TOOL_CHOICE_SPECIFIC_PROMPT.format(name=name)

    new_messages = []
    has_system = False

    for msg in messages:
        role = msg.get("role")

        if role == "system":
            system_content = flatten_message_content(msg.get("content", ""))
            new_messages.append(
                {
                    "role": "system",
                    "content": system_content + "\n\n" + tool_prompt,
                }
            )
            has_system = True

        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "unknown")
            tool_parts = normalize_content(msg.get("content", ""))
            tool_content = flatten_message_content(tool_parts)
            attachments = [part for part in tool_parts if part["type"] != "text"] if isinstance(tool_parts, list) else []
            result_text = (
                f"<tool_result>\n"
                f"  <tool_call_id>{tool_call_id}</tool_call_id>\n"
                f"  <result>\n{tool_content}\n  </result>\n"
                f"</tool_result>"
            )
            new_messages.append({
                "role": "user",
                "content": merge_content(result_text, attachments) if attachments else result_text,
            })

        elif role == "assistant" and msg.get("tool_calls"):
            tc_text = flatten_message_content(msg.get("content")) or ""
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                call_obj = {
                    "name": func.get("name", ""),
                    "arguments": json.loads(func.get("arguments", "{}")),
                }
                tc_text += f"\n<tool_call>\n{json.dumps(call_obj, ensure_ascii=False)}\n</tool_call>"
            new_messages.append({"role": "assistant", "content": tc_text.strip()})

        else:
            new_messages.append(normalize_message_content(msg))

    if not has_system:
        new_messages.insert(0, {"role": "system", "content": tool_prompt})

    return new_messages
