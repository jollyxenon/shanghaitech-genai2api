import json

from provider import genai
from provider.genai import stream_genai_response, stream_genai_response_with_tools
from tools.parsing import extract_tool_calls


class DummyTokenManager:
    def get_token(self):
        return "token"

    def force_refresh(self):
        return None


class DummyConfig:
    token_manager = DummyTokenManager()


def _fake_response(chunks):
    class FakeResponse:
        status_code = 200
        text = ""

        def iter_lines(self):
            for chunk in chunks:
                yield ("data: " + json.dumps(chunk)).encode()

    return FakeResponse()


def _patch_upstream(monkeypatch, chunks):
    monkeypatch.setattr(genai.model_registry, "get_root_ai_type", lambda model, token: "xinference")
    monkeypatch.setattr(genai.requests, "post", lambda *args, **kwargs: _fake_response(chunks))


def _data_chunks(raw_chunks):
    result = []
    for line in raw_chunks:
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            continue
        result.append(json.loads(payload))
    return result


def test_stream_genai_response_uses_fixed_id_and_keeps_reasoning(monkeypatch):
    _patch_upstream(monkeypatch, [
        {"choices": [{"delta": {"reasoning_content": "想一想"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "答案"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])

    chunks = _data_chunks(stream_genai_response(
        "提问", [{"role": "user", "content": "提问"}], "chatglm", 100, DummyConfig()
    ))

    assert {chunk["id"] for chunk in chunks} == {chunks[0]["id"]}
    reasoning = "".join(
        chunk["choices"][0]["delta"].get("reasoning_content", "") for chunk in chunks
    )
    content = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    assert reasoning == "想一想"
    assert content == "答案"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_genai_response_with_tools_keeps_reasoning_and_tool_calls(monkeypatch):
    _patch_upstream(monkeypatch, [
        {"choices": [{"delta": {"reasoning_content": "需要执行命令"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": '<tool_call>{"name": "Bash", "arguments": {"command": "pwd"}}</tool_call>'}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])

    chunks = _data_chunks(stream_genai_response_with_tools(
        "跑一下", [{"role": "user", "content": "跑一下"}], "chatglm", 100,
        DummyConfig(), allowed_tool_names={"Bash"},
    ))

    reasoning = "".join(
        chunk["choices"][0]["delta"].get("reasoning_content", "") for chunk in chunks
    )
    assert reasoning == "需要执行命令"

    tool_call_deltas = [
        tc for chunk in chunks
        for tc in chunk["choices"][0]["delta"].get("tool_calls", [])
    ]
    names = [tc.get("function", {}).get("name") for tc in tool_call_deltas if tc.get("function", {}).get("name")]
    arguments = "".join(
        tc.get("function", {}).get("arguments", "") for tc in tool_call_deltas
    )
    assert names == ["Bash"]
    assert json.loads(arguments) == {"command": "pwd"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    # 工具标记不应泄漏到正文
    assert all(
        "<tool_call>" not in chunk["choices"][0]["delta"].get("content", "")
        for chunk in chunks
    )


def test_extract_tool_calls_normalizes_string_arguments_to_json_object():
    tool_calls, _ = extract_tool_calls(
        '<tool_call>{"name": "Read", "arguments": "README.md"}</tool_call>',
        allowed_tool_names={"Read"},
    )
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"arguments": "README.md"}


def test_extract_tool_calls_maps_bare_bash_string_to_command():
    tool_calls, _ = extract_tool_calls(
        '<tool_call>{"name": "Bash", "arguments": "pwd"}</tool_call>',
        allowed_tool_names={"Bash"},
    )
    assert json.loads(tool_calls[0]["function"]["arguments"]) == {"command": "pwd"}
