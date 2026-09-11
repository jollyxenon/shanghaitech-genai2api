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


def flatten_message_content(content):
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts = [flatten_message_content(item) for item in content]
        return "\n".join(part for part in parts if part)

    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        if "content" in content:
            return flatten_message_content(content["content"])
        if "input" in content:
            return json.dumps(content["input"], ensure_ascii=False)
        return json.dumps(content, ensure_ascii=False)

    return str(content)


def normalize_message_content(message):
    normalized = dict(message)
    normalized["content"] = flatten_message_content(message.get("content", ""))
    return normalized


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
            tool_content = flatten_message_content(msg.get("content", ""))
            new_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"<tool_result>\n"
                        f"  <tool_call_id>{tool_call_id}</tool_call_id>\n"
                        f"  <result>\n{tool_content}\n  </result>\n"
                        f"</tool_result>"
                    ),
                }
            )

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
