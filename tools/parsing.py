import ast
import json
import logging
import re
import uuid

logger = logging.getLogger(__name__)

# 部分上游模型会自行输出 GenAI 原生的 DSML 工具标记，而不是代理注入的
# <tool_call> 格式；同一模型还可能把标签写成 call / _call 等变体。
# 这里把所有以 call 结尾的 DSML 标签归一化成 <tool_call>，让解析器与流式检测统一处理。
# 同时接受全角 ｜ 与半角 | 两种竖线。
DSML_OPEN_PREFIX_FULLWIDTH = "<｜DSML｜"
DSML_OPEN_PREFIX_ASCII = "<|DSML|"
TOOL_CALL_OPEN_MARKERS = (
    "<tool_call",
    DSML_OPEN_PREFIX_FULLWIDTH,
    DSML_OPEN_PREFIX_ASCII,
)
DSML_OPEN_TAG_RE = re.compile(r"<\s*[|｜]\s*DSML\s*[|｜]\s*([A-Za-z_][A-Za-z0-9_]*)?\s*>", re.IGNORECASE)
DSML_CLOSE_TAG_RE = re.compile(r"</\s*[|｜]\s*DSML\s*[|｜]\s*([A-Za-z_][A-Za-z0-9_]*)?\s*>", re.IGNORECASE)
# 变体：<｜DSML｜ name="read">{...}，工具名写在属性里。
DSML_ATTR_OPEN_TAG_RE = re.compile(
    r"<\s*[|｜]\s*DSML\s*[|｜]\s*name\s*=\s*[\"']([^\"']+)[\"']\s*>",
    re.IGNORECASE,
)
# 带属性的 DSML 标签（如 <｜DSML｜invoke name="x">），仅用于删除非 call 标签。
DSML_ANY_TAG_RE = re.compile(r"</?\s*[|｜]\s*DSML\s*[|｜]\s*[^>]*>", re.IGNORECASE)


def _is_tool_call_tag_name(name):
    # tool_call / call / _call / l_call 都算；空标签名 <｜DSML｜> 当普通工具标签处理；
    # 复数 tool_calls 是外层包裹标签，不算。
    if not name:
        return True
    return name.lower().endswith("call")


def normalize_tool_call_tags(text):
    """把 DSML 工具标记归一化为 <tool_call>，其余文本原样保留。"""
    if not text or "DSML" not in text:
        return text
    changed = False

    def replace_open(match):
        nonlocal changed
        if _is_tool_call_tag_name(match.group(1)):
            changed = True
            return "<tool_call>"
        return match.group(0)

    def replace_close(match):
        nonlocal changed
        if _is_tool_call_tag_name(match.group(1)):
            changed = True
            return "</tool_call>"
        return match.group(0)

    normalized = DSML_OPEN_TAG_RE.sub(replace_open, text)
    normalized = DSML_CLOSE_TAG_RE.sub(replace_close, normalized)
    # 工具名在属性里的变体：归一化成 <tool_call name="...">，复用属性解析路径。
    normalized = DSML_ATTR_OPEN_TAG_RE.sub(r'<tool_call name="\1">', normalized)
    if changed or normalized != text:
        logger.info("Normalized DSML tool tag(s) to <tool_call>")
    return normalized


def strip_dsml_tags(text):
    """删除所有 DSML 标签（含带属性的 invoke / parameter），保留其中正文。"""
    if not text or "DSML" not in text:
        return text
    return DSML_ANY_TAG_RE.sub("", text)


def find_tool_call_open(text):
    """返回最早的完整工具标记起始下标（<tool_call 或 DSML 形式）；没有则返回 -1。"""
    best = -1
    for marker in TOOL_CALL_OPEN_MARKERS:
        position = text.find(marker)
        if position >= 0 and (best < 0 or position < best):
            best = position
    return best


def tool_call_prefix_len(text):
    """返回末尾属于某个工具标记前缀的长度，供流式解析回退（hold back）使用。"""
    longest = 0
    for marker in TOOL_CALL_OPEN_MARKERS:
        length = _tag_prefix_len(text, marker)
        if length > longest:
            longest = length
    return longest


COMMON_TOOL_ARG_KEYS = (
    "notebook_path",
    "new_string",
    "old_string",
    "file_path",
    "head_limit",
    "output_mode",
    "edit_mode",
    "description",
    "command",
    "pattern",
    "offset",
    "limit",
    "path",
)

COMMON_TOOL_NAMES = (
    "NotebookEdit",
    "TodoWrite",
    "MultiEdit",
    "WebFetch",
    "WebSearch",
    "Bash",
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "LS",
)

TOOL_ARG_KEYS_BY_TOOL = {
    "Bash": ("command", "description"),
    "Read": ("file_path", "path", "offset", "limit"),
    "Glob": ("pattern", "path"),
    "Grep": ("pattern", "path", "output_mode", "head_limit"),
    "LS": ("path",),
}


def strip_think_blocks(content):
    return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()


def _extract_json_object(raw):
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(raw)):
            ch = raw[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start:idx + 1]
                    try:
                        return json.loads(candidate)
                    except (json.JSONDecodeError, ValueError):
                        break
        start = raw.find("{", start + 1)
    return None


def _extract_balanced_brace_segment(text, start):
    if start < 0 or start >= len(text) or text[start] != "{":
        return None, None

    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx + 1], idx + 1

    return None, None


def _sanitize_json_escapes(raw):
    return re.sub(r'\\(?=[^"\\/bfnrtu])', r'\\\\', raw)


def _load_relaxed_json(raw):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass

    sanitized = _sanitize_json_escapes(raw)
    if sanitized != raw:
        try:
            return json.loads(sanitized)
        except (json.JSONDecodeError, ValueError):
            pass

    stripped = sanitized.strip()

    # 部分模型会把工具参数写成 Python 字面量（单引号、True/False/None），
    # 这里回退到 ast.literal_eval 兼容这种形态。
    if stripped[:1] in ("{", "["):
        try:
            literal = ast.literal_eval(stripped)
            if isinstance(literal, (dict, list)):
                return literal
        except (ValueError, SyntaxError):
            pass

    if not stripped.startswith("{"):
        return None

    depth = 0
    in_string = False
    escape = False
    for ch in stripped:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)

    if depth <= 0:
        return None

    try:
        return json.loads(stripped + ("}" * depth))
    except (json.JSONDecodeError, ValueError):
        return None


def _clean_arg_value(value):
    cleaned = value.strip()
    cleaned = re.sub(r'</?tool_call>\s*$', '', cleaned, flags=re.DOTALL).strip()
    cleaned = re.sub(r'</?arg_value>\s*$', '', cleaned, flags=re.DOTALL).strip()

    for suffix in ('"}})', '"}}', '"})', '"}', '">', '"'):
        if cleaned.endswith(suffix):
            cleaned = cleaned[:-len(suffix)].rstrip()
            break

    return cleaned


def _clean_tool_name(name):
    cleaned = name.strip()
    match = re.search(r'[A-Za-z_][A-Za-z0-9_.-]*', cleaned)
    return match.group(0) if match else cleaned


def _extract_tool_name_from_attrs(attrs):
    if not attrs:
        return None

    match = re.search(r'\bname\s*=\s*["\']([^"\']+)["\']', attrs)
    if not match:
        return None

    return _clean_tool_name(match.group(1))


def _normalize_tool_payload(payload, tool_name=None):
    if not isinstance(payload, dict):
        return None

    if "name" in payload:
        name = _clean_tool_name(payload["name"])
        if "arguments" in payload:
            arguments = payload["arguments"]
        else:
            # 有些模型会把参数直接平铺在 name 旁边（缺少 arguments 包裹）。
            arguments = {key: value for key, value in payload.items() if key != "name"}
        if name == "Bash" and isinstance(arguments, str):
            arguments = {"command": arguments}
        return {
            "name": name,
            "arguments": arguments,
        }

    if len(payload) == 1:
        candidate_name, candidate_args = next(iter(payload.items()))
        cleaned_name = _clean_tool_name(candidate_name)
        if cleaned_name and isinstance(candidate_args, dict):
            return {"name": cleaned_name, "arguments": candidate_args}

    if tool_name:
        return {"name": tool_name, "arguments": payload}

    return None


def _parse_malformed_named_tool_payload(raw, tool_name=None):
    """Recover common model-emitted JSON with unescaped quotes inside Bash command."""
    normalized = raw.strip()
    name = tool_name
    if not name:
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', normalized)
        if name_match:
            name = _clean_tool_name(name_match.group(1))
    if not name or '"arguments"' not in normalized:
        return None

    command_match = re.search(r'"command"\s*:\s*"', normalized, re.DOTALL)
    if not command_match:
        return None

    tail = normalized[command_match.end():]
    value_match = re.match(r'(?s)(.*)"\s*}\s*}?\s*$', tail)
    if not value_match:
        return None

    command = value_match.group(1).replace('\\"', '"')
    return {"name": name, "arguments": {"command": command}}


def _parse_tagged_arguments(raw, tool_name=None):
    normalized = raw.replace('\\"', '"').strip()
    if '<arg_key>' not in normalized:
        return None

    name, remainder = normalized.split('<arg_key>', 1)
    name = _clean_tool_name(name)
    if not name:
        name = tool_name
    if not name:
        return None

    remainder = re.sub(
        r'</arg_value>\s*([A-Za-z0-9_]+)\s*(?=(?:":\s*"|":\s*|="\s*|=\s*|:\s*"|:\s*))',
        r'</arg_value><arg_key>\1',
        remainder,
    )
    remainder = re.sub(
        r'"\s*,\s*"([A-Za-z0-9_]+)"\s*:',
        r'"</arg_value><arg_key>\1":',
        remainder,
    )

    arg_pattern = re.compile(
        r'<arg_key>\s*"?([^<":=]+)"?\s*'
        r'(?:</arg_key>\s*<arg_value>\s*|</arg_key>\s*"|":\s*"|":\s*|="\s*|=\s*"|=\s*|:\s*"|:\s*)'
        r'(.*?)'
        r'(?=(?:</arg_value>\s*<arg_key>)|(?:</arg_value>)|(?:<arg_key>)|(?:</tool_call>)|$)',
        re.DOTALL,
    )

    arguments = {}
    for key, value in arg_pattern.findall('<arg_key>' + remainder):
        key = key.strip()
        if not key:
            continue
        arguments[key] = _clean_arg_value(value)

    if not arguments:
        return None

    return {"name": name, "arguments": arguments}


def _parse_broken_arg_key_lines(raw, tool_name=None):
    normalized = raw.replace('\\"', '"').strip()
    if '</arg_key>' not in normalized:
        return None

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if not lines:
        return None

    name = tool_name or _clean_tool_name(lines[0])
    if not name:
        return None

    arguments = {}
    for line in lines[1:]:
        line = re.sub(r'</?tool_call\b[^>]*>', '', line).strip()
        line = re.sub(r'</?arg_value>', '', line).strip()
        if not line:
            continue

        if '</arg_key>' in line:
            key, value = line.split('</arg_key>', 1)
            key = re.sub(r'</?arg_key>', '', key).strip().strip('"')
            if key and value:
                arguments[key] = _clean_arg_value(value)
            continue

        for key in COMMON_TOOL_ARG_KEYS:
            if line.startswith(key) and len(line) > len(key):
                value = line[len(key):]
                if value:
                    arguments[key] = _clean_arg_value(value)
                    break

    if not arguments:
        return None

    return {"name": name, "arguments": arguments}


def _extract_compact_tool_name(text, tool_name=None):
    if tool_name:
        return tool_name, text

    for candidate in COMMON_TOOL_NAMES:
        if text.startswith(candidate):
            return candidate, text[len(candidate):]

    return None, text


def _parse_compact_tool_arguments(raw, tool_name=None):
    normalized = raw.replace('\\"', '"').strip()
    normalized = re.sub(r'</?tool_call\b[^>]*>', '', normalized).strip()
    normalized = re.sub(r'</?arg_value>', '', normalized).strip()
    if not normalized:
        return None

    name, remainder = _extract_compact_tool_name(normalized, tool_name=tool_name)
    if not name or not remainder:
        return None
    remainder = remainder.strip()

    keys = TOOL_ARG_KEYS_BY_TOOL.get(name, COMMON_TOOL_ARG_KEYS)
    sorted_keys = sorted(keys, key=len, reverse=True)
    matches = []
    position = 0
    while position < len(remainder):
        matched_key = None
        for key in sorted_keys:
            if remainder.startswith(key, position):
                matched_key = key
                break
        if matched_key:
            matches.append((position, matched_key))
            position += len(matched_key)
            continue
        position += 1

    if not matches:
        return None
    if matches[0][0] != 0:
        return None

    arguments = {}
    for index, (start, key) in enumerate(matches):
        value_start = start + len(key)
        value_end = matches[index + 1][0] if index + 1 < len(matches) else len(remainder)
        value = remainder[value_start:value_end].strip()
        if value:
            arguments[key] = _clean_arg_value(value)

    if not arguments:
        return None

    return {"name": name, "arguments": arguments}


def _parse_argument_tag(raw, tool_name=None):
    if not tool_name:
        return None

    match = re.search(r'<argument>\s*(.*?)\s*</argument>', raw, re.DOTALL)
    if not match:
        return None

    argument = match.group(1).strip()
    if not argument:
        return None

    key = "command" if tool_name == "Bash" else "argument"
    return {"name": tool_name, "arguments": {key: argument}}


def _parse_tool_call_body(raw, tool_name=None):
    raw = raw.strip()
    normalized_raw = raw.replace('\\"', '"')

    call = _load_relaxed_json(raw)
    normalized_call = _normalize_tool_payload(call, tool_name=tool_name)
    if normalized_call:
        return normalized_call

    embedded_json = _extract_json_object(raw)
    normalized_call = _normalize_tool_payload(embedded_json, tool_name=tool_name)
    if normalized_call:
        return normalized_call

    embedded_json = _extract_json_object(normalized_raw)
    normalized_call = _normalize_tool_payload(embedded_json, tool_name=tool_name)
    if normalized_call:
        return normalized_call

    malformed_call = _parse_malformed_named_tool_payload(raw, tool_name=tool_name)
    if malformed_call:
        return malformed_call

    # 变体：标签内先写裸工具名，再直接跟参数 JSON（缺少 {"name": ...} 包裹）。
    named_json = re.match(
        r'^([A-Za-z_][A-Za-z0-9_.-]*)\s*\n\s*(\{.*\})\s*$',
        normalized_raw,
        re.DOTALL,
    )
    if named_json:
        arguments = _load_relaxed_json(named_json.group(2))
        if isinstance(arguments, dict):
            return {
                "name": _clean_tool_name(named_json.group(1)),
                "arguments": arguments,
            }

    tagged_arguments = _parse_tagged_arguments(raw, tool_name=tool_name)
    if tagged_arguments:
        return tagged_arguments

    broken_arg_key_lines = _parse_broken_arg_key_lines(raw, tool_name=tool_name)
    if broken_arg_key_lines:
        return broken_arg_key_lines

    compact_tool_arguments = _parse_compact_tool_arguments(raw, tool_name=tool_name)
    if compact_tool_arguments:
        return compact_tool_arguments

    argument_tag = _parse_argument_tag(raw, tool_name=tool_name)
    if argument_tag:
        return argument_tag

    inline_pairs = re.findall(
        r'([A-Za-z0-9_]+)"\s*:\s*"([^"]*)"',
        normalized_raw,
        re.DOTALL,
    )
    if inline_pairs:
        name = _clean_tool_name(
            normalized_raw.split('<arg_key>', 1)[0].split('{', 1)[0].strip()
        )
        if not name:
            name = tool_name
        if name and not name.startswith('"'):
            arguments = {
                key.strip(): value.strip()
                for key, value in inline_pairs
                if key.strip()
            }
            if arguments:
                return {"name": name, "arguments": arguments}

    name_m = re.search(r'<name>\s*(.*?)\s*</name>', raw, re.DOTALL)
    args_m = re.search(r'<arguments>\s*(.*?)\s*</arguments>', raw, re.DOTALL)
    if name_m:
        name = _clean_tool_name(name_m.group(1))
        arguments = {}
        if args_m:
            args_str = args_m.group(1).strip()
            try:
                arguments = json.loads(args_str)
            except (json.JSONDecodeError, ValueError):
                arguments = {"raw": args_str}
        return {"name": name, "arguments": arguments}

    return None


def _clean_remaining_text(text):
    if not text:
        return None

    cleaned = re.sub(r'<tool_result>.*?</tool_result>', '', text, flags=re.DOTALL)
    cleaned = re.sub(r'<tool_condition>.*?</tool_condition>', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'<agent-other-thinking>.*?</agent-other-thinking>', '', cleaned, flags=re.DOTALL)
    cleaned = re.sub(r'</?tool_condition\b[^>]*>', '', cleaned)
    cleaned = re.sub(r'</?tool_call\b[^>]*>', '', cleaned)
    cleaned = re.sub(r'</?tool_calls\b[^>]*>', '', cleaned)
    cleaned = re.sub(r'</?tool_\d+>', '', cleaned)
    cleaned = re.sub(r'</?tool_utils\b[^>]*>', '', cleaned)
    cleaned = strip_dsml_tags(cleaned)
    cleaned = cleaned.replace('</think>', '')
    cleaned = re.sub(r'^\s*\[\]\s*$', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    cleaned = cleaned.strip()
    return cleaned or None


def _extract_function_style_calls(cleaned, allowed_tool_names):
    call_pattern = re.compile(r'([A-Za-z_][A-Za-z0-9_.-]*)\(\s*{')
    matches = list(call_pattern.finditer(cleaned))
    if not matches:
        return [], None

    spans = []
    tool_calls = []
    for i, match in enumerate(matches):
        tool_name = _clean_tool_name(match.group(1))
        json_text, end_idx = _extract_balanced_brace_segment(cleaned, match.end() - 1)
        if not json_text:
            continue

        payload = _load_relaxed_json(json_text)
        if not isinstance(payload, dict):
            continue
        if allowed_tool_names and tool_name not in allowed_tool_names:
            logger.warning("Skipping disallowed function-style tool_call[%d] name=%s", i, tool_name)
            continue

        span_end = end_idx
        while span_end < len(cleaned) and cleaned[span_end].isspace():
            span_end += 1
        if span_end < len(cleaned) and cleaned[span_end] == ")":
            span_end += 1

        spans.append((match.start(), span_end))
        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(payload, ensure_ascii=False),
            }
        })

    if not tool_calls:
        return [], None

    remaining_parts = []
    cursor = 0
    for start, end in sorted(spans):
        if start > cursor:
            remaining_parts.append(cleaned[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(cleaned):
        remaining_parts.append(cleaned[cursor:])

    return tool_calls, _clean_remaining_text("".join(remaining_parts))


def _extract_bare_json_tool_calls(cleaned, allowed_tool_names):
    tool_calls = []
    spans = []
    idx = 0
    while idx < len(cleaned):
        start = cleaned.find("{", idx)
        if start == -1:
            break

        json_text, end_idx = _extract_balanced_brace_segment(cleaned, start)
        if not json_text:
            idx = start + 1
            continue

        payload = _load_relaxed_json(json_text)
        call = _normalize_tool_payload(payload)
        if not call:
            idx = start + 1
            continue
        if allowed_tool_names and call["name"] not in allowed_tool_names:
            logger.warning("Skipping disallowed bare-json tool_call name=%s", call["name"])
            idx = end_idx
            continue

        spans.append((start, end_idx))
        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
            }
        })
        idx = end_idx

    if not tool_calls:
        return [], None

    remaining_parts = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            remaining_parts.append(cleaned[cursor:start])
        cursor = max(cursor, end)
    if cursor < len(cleaned):
        remaining_parts.append(cleaned[cursor:])

    return tool_calls, _clean_remaining_text("".join(remaining_parts))


def _run_fallback_extractors(cleaned, allowed_tool_names):
    for extractor in (
        _extract_numbered_tool_calls,
        _extract_function_style_calls,
        _extract_bare_json_tool_calls,
        _extract_malformed_named_tool_calls,
        _extract_fenced_argument_calls,
    ):
        tool_calls, remaining = extractor(cleaned, allowed_tool_names)
        if tool_calls:
            return tool_calls, remaining
    return None, None


def _extract_fenced_argument_calls(cleaned, allowed_tool_names):
    if not allowed_tool_names or len(allowed_tool_names) != 1:
        return [], None

    tool_name = next(iter(allowed_tool_names))
    fence_pattern = re.compile(r'```(?:json)?\s*({.*?})\s*```', re.DOTALL)
    matches = list(fence_pattern.finditer(cleaned))
    if not matches:
        return [], None

    tool_calls = []
    for match in matches:
        payload = _load_relaxed_json(match.group(1))
        if not isinstance(payload, dict) or "arguments" not in payload:
            continue

        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(payload["arguments"], ensure_ascii=False),
            }
        })

    if not tool_calls:
        return [], None

    remaining = fence_pattern.sub('', cleaned).strip()
    return tool_calls, _clean_remaining_text(remaining)


def _extract_malformed_named_tool_calls(cleaned, allowed_tool_names):
    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', cleaned)
    if not name_match:
        return [], None

    start = cleaned.rfind("{", 0, name_match.start())
    if start < 0:
        start = name_match.start()

    call = _parse_malformed_named_tool_payload(cleaned[start:])
    if not call:
        return [], None
    if allowed_tool_names and call["name"] not in allowed_tool_names:
        logger.warning("Skipping disallowed malformed tool_call name=%s", call["name"])
        return [], None

    return [{
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": call["name"],
            "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
        }
    }], _clean_remaining_text(cleaned[:start])


def _extract_numbered_tool_calls(cleaned, allowed_tool_names):
    block_pattern = re.compile(r'<tool_\d+>\s*(.*?)\s*</tool_\d+>', re.DOTALL)
    matches = list(block_pattern.finditer(cleaned))
    if not matches:
        return [], None

    tool_calls = []
    for i, match in enumerate(matches):
        call = _parse_tool_call_body(match.group(1))
        if not call:
            continue
        if allowed_tool_names and call["name"] not in allowed_tool_names:
            logger.warning("Skipping disallowed numbered tool_call[%d] name=%s", i, call["name"])
            continue
        tool_calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
            }
        })

    if not tool_calls:
        return [], None

    remaining = block_pattern.sub('', cleaned)
    remaining = re.sub(r'</?tool_calls>', '', remaining).strip()
    return tool_calls, _clean_remaining_text(remaining)


def _coerce_tool_arguments(tool_name, arguments):
    """把模型解析出的参数统一成 dict，保证序列化后是合法 JSON 对象。"""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        parsed = _load_relaxed_json(arguments)
        if isinstance(parsed, dict):
            return parsed
        if tool_name == "Bash":
            return {"command": arguments}
        return {"arguments": arguments}
    if arguments is None:
        return {}
    return {"arguments": arguments}


def extract_tool_calls(content, allowed_tool_names=None):
    cleaned = normalize_tool_call_tags(strip_think_blocks(content))

    cleaned = re.sub(
        r'```(?:xml|json|plaintext|text)?\s*\n?\s*(<tool_call\b[^>]*>.*?</tool_call>)\s*\n?\s*```',
        r'\1',
        cleaned,
        flags=re.DOTALL
    )

    match_pattern = re.compile(r'<tool_call\b([^>]*)>\s*(.*?)\s*</tool_call>', re.DOTALL)
    open_pattern = re.compile(r'<tool_call\b([^>]*)>', re.DOTALL)
    matches = list(match_pattern.finditer(cleaned))
    remaining = None

    if not matches and '<tool_call' in cleaned:
        openings = list(open_pattern.finditer(cleaned))
        if openings:
            remaining = cleaned[:openings[0].start()].strip()
            matches = []
            for index, opening in enumerate(openings):
                end = openings[index + 1].start() if index + 1 < len(openings) else len(cleaned)
                body = cleaned[opening.end():end].strip()
                if not body:
                    continue
                matches.append((opening.group(1), body))

    if not matches:
        fallback_tool_calls, fallback_remaining = _run_fallback_extractors(
            cleaned,
            allowed_tool_names,
        )
        if fallback_tool_calls:
            return fallback_tool_calls, fallback_remaining

        logger.debug("No <tool_call> tags found in content (%d chars): %s",
                     len(content), content[:500])
        if "DSML" in content:
            return None, _clean_remaining_text(cleaned) or ""
        return None, content

    logger.debug("Found %d <tool_call> match(es)", len(matches))

    tool_calls = []
    for i, match in enumerate(matches):
        if isinstance(match, tuple):
            attrs, body = match
        else:
            attrs, body = match.group(1), match.group(2)
        tool_name = _extract_tool_name_from_attrs(attrs)
        call = _parse_tool_call_body(body, tool_name=tool_name)
        if call:
            if allowed_tool_names and call["name"] not in allowed_tool_names:
                logger.warning("Skipping disallowed tool_call[%d] name=%s", i, call["name"])
                continue
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(
                        _coerce_tool_arguments(call["name"], call.get("arguments", {})),
                        ensure_ascii=False,
                    )
                }
            })
        else:
            logger.warning("Failed to parse tool_call[%d] — raw: %s", i, body[:300])
            continue

    if not tool_calls:
        fallback_tool_calls, fallback_remaining = _run_fallback_extractors(
            cleaned,
            allowed_tool_names,
        )
        if fallback_tool_calls:
            return fallback_tool_calls, fallback_remaining
        if "DSML" in content:
            return None, _clean_remaining_text(cleaned) or ""
        return None, content

    if remaining is None:
        remaining = match_pattern.sub('', cleaned).strip()
    return tool_calls, _clean_remaining_text(remaining)


def _tag_prefix_len(text, tag):
    max_len = min(len(tag) - 1, len(text))
    for length in range(max_len, 0, -1):
        if text[-length:] == tag[:length]:
            return length
    return 0
