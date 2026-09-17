import logging
import time
import uuid
from datetime import datetime

from flask import Blueprint, current_app, request, jsonify, stream_with_context, Response

from errors import UpstreamError, openai_error
from provider.features import prepare_request
from tools.prompts import inject_tool_prompt
from tools.parsing import extract_tool_calls
from provider.genai import (
    collect_genai_response,
    convert_messages_to_genai_format,
    estimate_messages_tokens,
    estimate_text_tokens,
    stream_genai_response,
    stream_genai_response_with_tools,
)

logger = logging.getLogger(__name__)

chat_bp = Blueprint('chat', __name__)


@chat_bp.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    config = current_app.config["APP_CONFIG"]
    request_id = f"req_{uuid.uuid4().hex[:16]}"
    start_time = time.monotonic()

    try:
        req_data = request.get_json()

        if not req_data or 'messages' not in req_data:
            return openai_error("Missing 'messages' field in request body")

        messages = req_data.get('messages', [])
        model = req_data.get('model', 'gpt-3.5-turbo')
        stream = req_data.get('stream', False)
        max_tokens = req_data.get('max_tokens', 30000)
        tools = [tool for tool in req_data.get('tools') or [] if tool.get('type') == 'function']
        tool_choice = req_data.get('tool_choice', None)

        has_tools = tools and len(tools) > 0
        allowed_tool_names = {
            tool["function"]["name"]
            for tool in (tools or [])
            if tool.get("type") == "function" and tool.get("function", {}).get("name")
        }
        # tool_choice 指定单个函数时收窄允许集合，让输出层也只透传该工具。
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            restricted = (tool_choice.get("function") or {}).get("name")
            if restricted in allowed_tool_names:
                allowed_tool_names = {restricted}

        logger.info("[%s] model=%s stream=%s tools=%s messages=%d",
                     request_id, model, stream, bool(has_tools), len(messages))

        if has_tools:
            messages = inject_tool_prompt(messages, tools, tool_choice)

        messages, upstream_options = prepare_request(messages, model, config, req_data, "chat")
        chat_info = convert_messages_to_genai_format(messages)

        if not chat_info:
            return openai_error("No user message found in 'messages'")

        if stream:
            if has_tools:
                gen = stream_genai_response_with_tools(
                    chat_info, messages, model, max_tokens, config, allowed_tool_names,
                    upstream_options=upstream_options,
                )
            else:
                gen = stream_genai_response(
                    chat_info, messages, model, max_tokens, config,
                    upstream_options=upstream_options,
                )
            return Response(
                stream_with_context(gen),
                mimetype='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'Content-Type': 'text/event-stream',
                }
            )

        else:
            try:
                complete_content, complete_reasoning, finish_reason = collect_genai_response(
                    chat_info, messages, model, max_tokens, config,
                    upstream_options=upstream_options,
                )
            except UpstreamError as e:
                logger.warning("[%s] upstream error: %s", request_id, e)
                return openai_error(str(e), error_type="upstream_error", status=502)

            completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

            if has_tools:
                tool_calls, remaining_text = extract_tool_calls(
                    complete_content,
                    allowed_tool_names=allowed_tool_names,
                )
            else:
                tool_calls, remaining_text = None, complete_content

            if tool_calls:
                message_obj = {
                    "role": "assistant",
                    "content": remaining_text,
                    "tool_calls": tool_calls
                }
                finish_reason = "tool_calls"
            else:
                message_obj = {
                    "role": "assistant",
                    "content": complete_content
                }
                finish_reason = "length" if finish_reason == "length" else "stop"

            if complete_reasoning:
                message_obj["reasoning_content"] = complete_reasoning

            prompt_tokens = estimate_messages_tokens(messages)
            completion_tokens = estimate_text_tokens(complete_content + complete_reasoning)
            response = {
                "id": completion_id,
                "object": "chat.completion",
                "created": int(datetime.now().timestamp()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message_obj,
                    "finish_reason": finish_reason
                }],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens
                }
            }
            return jsonify(response)

    except ValueError as e:
        return openai_error(str(e), error_type="invalid_request_error", status=400)
    except UpstreamError as e:
        return openai_error(str(e), error_type="upstream_error", status=502)
    except Exception as e:
        logger.exception("[%s] Unhandled error", request_id)
        return openai_error(
            str(e),
            error_type="server_error",
            code="internal_error",
            status=500
        )
    finally:
        elapsed = time.monotonic() - start_time
        logger.info("[%s] completed in %.2fs", request_id, elapsed)
