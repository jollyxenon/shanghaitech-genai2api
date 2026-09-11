import json

from provider import anthropic, genai
from provider.anthropic import (
    COMPAT_THINKING_SIGNATURE,
    anthropic_allowed_tool_names,
    anthropic_messages_to_genai_format,
    normalize_tool_input,
    parse_tool_arguments,
    stream_genai_as_anthropic,
)


def _events(chunks):
    events = []
    for chunk in chunks:
        event_name = None
        data = None
        for line in chunk.strip().splitlines():
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event_name and data:
            events.append((event_name, data))
    return events


def test_anthropic_messages_convert_tool_history_to_genai_promptable_messages():
    body = {
        "model": "GPT-5.5",
        "system": "You are Claude Code.",
        "tool_choice": {"type": "auto"},
        "tools": [
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
        "messages": [
            {"role": "user", "content": "Read README.md"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01",
                        "content": [{"type": "text", "text": "file contents"}],
                    },
                    {"type": "text", "text": "Now summarize it."},
                ],
            },
        ],
    }

    _, messages, model = anthropic_messages_to_genai_format(body, "token")

    assert model == "GPT-5.5"
    assert anthropic_allowed_tool_names(body) == {"read_file"}
    assert messages[0]["role"] == "system"
    assert "<tools>" in messages[0]["content"]
    assert "read_file" in messages[0]["content"]
    assert any(
        message["role"] == "assistant"
        and "<tool_call>" in message["content"]
        and '"name": "read_file"' in message["content"]
        for message in messages
    )
    assert any(
        message["role"] == "user"
        and "<tool_result" in message["content"]
        and "file contents" in message["content"]
        and "Now summarize it." in message["content"]
        for message in messages
    )


def test_stream_genai_as_anthropic_emits_tool_use_blocks(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""

        def iter_lines(self):
            chunks = [
                {
                    "choices": [
                        {
                            "delta": {
                                "content": (
                                    '<tool_call>{"name": "Bash", '
                                    '"arguments": {"command": "pwd"}}</tool_call>'
                                )
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            for chunk in chunks:
                yield ("data: " + json.dumps(chunk)).encode()

    class DummyTokenManager:
        def force_refresh(self):
            return None

    class DummyConfig:
        token_manager = DummyTokenManager()

    monkeypatch.setattr(genai.model_registry, "get_root_ai_type", lambda model, token: "xinference")
    monkeypatch.setattr(genai.requests, "post", lambda *args, **kwargs: FakeResponse())

    events = _events(
        stream_genai_as_anthropic(
            [{"role": "user", "content": "run pwd"}],
            "GPT-5.5",
            1000,
            "token",
            DummyConfig(),
            allowed_tool_names={"Bash"},
        )
    )

    assert not any("<tool_call>" in json.dumps(data) for _, data in events)
    tool_starts = [
        data
        for event, data in events
        if event == "content_block_start"
        and data["content_block"]["type"] == "tool_use"
    ]
    assert tool_starts[0]["content_block"]["name"] == "Bash"
    input_deltas = [
        data["delta"]["partial_json"]
        for event, data in events
        if event == "content_block_delta"
        and data["delta"]["type"] == "input_json_delta"
    ]
    assert json.loads(input_deltas[0]) == {"command": "pwd"}
    message_delta = [data for event, data in events if event == "message_delta"][0]
    assert message_delta["delta"]["stop_reason"] == "tool_use"


def test_stream_genai_as_anthropic_emits_bare_malformed_json_tool_use(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""

        def iter_lines(self):
            chunks = [
                {
                    "choices": [
                        {
                            "delta": {
                                "content": '{"name": "Bash", "arguments": {"command": "printf "$(pwd)""}}'
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            for chunk in chunks:
                yield ("data: " + json.dumps(chunk)).encode()

    class DummyTokenManager:
        def force_refresh(self):
            return None

    class DummyConfig:
        token_manager = DummyTokenManager()

    monkeypatch.setattr(genai.model_registry, "get_root_ai_type", lambda model, token: "xinference")
    monkeypatch.setattr(genai.requests, "post", lambda *args, **kwargs: FakeResponse())

    events = _events(
        stream_genai_as_anthropic(
            [{"role": "user", "content": "run pwd"}],
            "chatglm",
            1000,
            "token",
            DummyConfig(),
            allowed_tool_names={"Bash"},
        )
    )

    tool_starts = [
        data
        for event, data in events
        if event == "content_block_start"
        and data["content_block"]["type"] == "tool_use"
    ]
    assert tool_starts[0]["content_block"]["name"] == "Bash"
    input_delta = next(
        data["delta"]["partial_json"]
        for event, data in events
        if event == "content_block_delta"
        and data["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(input_delta) == {"command": 'printf "$(pwd)"'}


def test_stream_genai_as_anthropic_filters_split_thinking_blocks(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""

        def iter_lines(self):
            chunks = [
                {"choices": [{"delta": {"content": "<think>hidden"}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": " reasoning</think>\n\nvisible"}, "finish_reason": None}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ]
            for chunk in chunks:
                yield ("data: " + json.dumps(chunk)).encode()

    class DummyTokenManager:
        def force_refresh(self):
            return None

    class DummyConfig:
        token_manager = DummyTokenManager()

    monkeypatch.setattr(genai.model_registry, "get_root_ai_type", lambda model, token: "xinference")
    monkeypatch.setattr(genai.requests, "post", lambda *args, **kwargs: FakeResponse())

    events = _events(
        stream_genai_as_anthropic(
            [{"role": "user", "content": "reply"}],
            "MiniMax-M1",
            1000,
            "token",
            DummyConfig(),
        )
    )
    text = "".join(
        data["delta"]["text"]
        for event, data in events
        if event == "content_block_delta"
        and data.get("delta", {}).get("type") == "text_delta"
    )

    assert text == "\n\nvisible"


def test_parse_tool_arguments_unwraps_nested_json_strings():
    assert parse_tool_arguments('{"arguments":"{\\"path\\": \\"README.md\\"}"}') == {
        "path": "README.md"
    }


def test_normalize_tool_input_maps_bash_arguments_string_to_command():
    assert normalize_tool_input("Bash", '"pwd"') == {"command": "pwd"}
    assert normalize_tool_input("Bash", '{"arguments": "pwd"}') == {"command": "pwd"}
    assert normalize_tool_input("Bash", '{"arguments": {"command": "pwd"}}') == {"command": "pwd"}


def test_normalize_tool_input_maps_claude_code_tool_aliases():
    assert normalize_tool_input("Grep", {"cpattern": "Claude Code", "file_path": "README.md"}) == {
        "pattern": "Claude Code",
        "path": "README.md",
    }
    assert normalize_tool_input("Read", {"path": "README.md"}) == {"file_path": "README.md"}
    assert normalize_tool_input("Glob", {"glob": "tests/*.py", "filePath": "/tmp/project"}) == {
        "pattern": "tests/*.py",
        "path": "/tmp/project",
    }


def _dummy_config_with_upstream(monkeypatch, chunks):
    class FakeResponse:
        status_code = 200
        text = ""

        def iter_lines(self):
            for chunk in chunks:
                yield ("data: " + json.dumps(chunk)).encode()

    class DummyTokenManager:
        def force_refresh(self):
            return None

    class DummyConfig:
        token_manager = DummyTokenManager()

    monkeypatch.setattr(genai.model_registry, "get_root_ai_type", lambda model, token: "xinference")
    monkeypatch.setattr(genai.requests, "post", lambda *args, **kwargs: FakeResponse())
    return DummyConfig()


def test_stream_genai_as_anthropic_emits_thinking_block_before_text(monkeypatch):
    config = _dummy_config_with_upstream(monkeypatch, [
        {"choices": [{"delta": {"reasoning_content": "先想一想"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "答案"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])

    events = _events(stream_genai_as_anthropic(
        [{"role": "user", "content": "提问"}], "chatglm", 1000, "token", config,
    ))

    starts = [
        (data["index"], data["content_block"]["type"])
        for event, data in events
        if event == "content_block_start"
    ]
    assert starts == [(0, "thinking"), (1, "text")]

    thinking = "".join(
        data["delta"]["thinking"]
        for event, data in events
        if event == "content_block_delta" and data["delta"]["type"] == "thinking_delta"
    )
    assert thinking == "先想一想"

    signatures = [
        data["delta"]["signature"]
        for event, data in events
        if event == "content_block_delta" and data["delta"]["type"] == "signature_delta"
    ]
    assert signatures == [COMPAT_THINKING_SIGNATURE]

    text = "".join(
        data["delta"]["text"]
        for event, data in events
        if event == "content_block_delta" and data["delta"]["type"] == "text_delta"
    )
    assert text == "答案"


def test_stream_genai_as_anthropic_drops_reasoning_after_text(monkeypatch):
    config = _dummy_config_with_upstream(monkeypatch, [
        {"choices": [{"delta": {"content": "正文"}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_content": "迟到的思考"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])

    events = _events(stream_genai_as_anthropic(
        [{"role": "user", "content": "提问"}], "chatglm", 1000, "token", config,
    ))

    assert not any(event == "content_block_start" and data.get("content_block", {}).get("type") == "thinking"
                   for event, data in events)
    assert not any(
        data.get("delta", {}).get("type") == "thinking_delta" for _, data in events
    )


def test_anthropic_history_thinking_blocks_are_not_forwarded():
    body = {
        "model": "chatglm",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "内部推理", "signature": COMPAT_THINKING_SIGNATURE},
                    {"type": "text", "text": "对外回答"},
                ],
            },
            {"role": "user", "content": "继续"},
        ],
    }

    _, messages, _ = anthropic_messages_to_genai_format(body, "token")
    joined = json.dumps(messages, ensure_ascii=False)
    assert "内部推理" not in joined
    assert COMPAT_THINKING_SIGNATURE not in joined
    assert "对外回答" in joined
