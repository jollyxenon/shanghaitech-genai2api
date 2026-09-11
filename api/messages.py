import json
import logging
import time
import uuid

from flask import Blueprint, current_app, request, jsonify, stream_with_context, Response

from provider.genai import estimate_messages_tokens
from provider.anthropic import (
    COMPAT_THINKING_SIGNATURE,
    anthropic_allowed_tool_names,
    anthropic_messages_to_genai_format,
    parse_tool_arguments,
    stream_genai_as_anthropic,
)

logger = logging.getLogger(__name__)

messages_bp = Blueprint('messages', __name__)


def anthropic_error(message: str, error_type: str = "api_error", code: str = "internal_error", status: int = 500) -> tuple:
    """Return an Anthropic-style error response."""
    return jsonify({
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        }
    }), status


@messages_bp.route('/messages', methods=['POST'])
@messages_bp.route('/v1/messages', methods=['POST'])
def messages():
    """Handle Anthropic Messages API requests."""
    config = current_app.config["APP_CONFIG"]
    request_id = f"msg_{uuid.uuid4().hex[:16]}"
    start_time = time.monotonic()

    try:
        req_data = request.get_json()

        if not req_data or 'messages' not in req_data:
            return anthropic_error("Missing 'messages' field in request body", error_type="invalid_request_error", status=400)

        messages = req_data.get('messages', [])
        model = req_data.get('model', 'GPT-5.5')
        stream = req_data.get('stream', True)
        max_tokens = req_data.get('max_tokens', 30000)

        logger.info("[%s] model=%s stream=%s messages=%d",
                     request_id, model, stream, len(messages))

        # Get current token
        token = config.token_manager.get_token()

        # Convert Anthropic format to GenAI format
        _, genai_messages, model = anthropic_messages_to_genai_format(req_data, token)
        allowed_tool_names = anthropic_allowed_tool_names(req_data)

        if not genai_messages:
            return anthropic_error("No valid messages provided", error_type="invalid_request_error", status=400)

        if stream:
            gen = stream_genai_as_anthropic(
                genai_messages,
                model,
                max_tokens,
                token,
                config,
                allowed_tool_names=allowed_tool_names,
            )
            return Response(
                stream_with_context(gen),
                mimetype='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'close',
                    'Content-Type': 'text/event-stream; charset=utf-8',
                    'X-Accel-Buffering': 'no',
                }
            )

        else:
            # 非流式响应：收集流式事件后合并成单条 message
            output_text_parts = []
            thinking_parts = []
            tool_blocks = {}
            stop_reason = "end_turn"
            for chunk in stream_genai_as_anthropic(
                genai_messages,
                model,
                max_tokens,
                token,
                config,
                allowed_tool_names=allowed_tool_names,
            ):
                event = None
                data = None
                for line in chunk.strip().splitlines():
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                        except json.JSONDecodeError:
                            data = None

                if not data:
                    continue
                if event == "error":
                    return anthropic_error(
                        data.get("error", {}).get("message", "Upstream error"),
                        error_type="api_error",
                        status=502,
                    )
                if event == "content_block_start" and data.get("content_block", {}).get("type") == "tool_use":
                    block = data["content_block"]
                    tool_blocks[data["index"]] = {
                        "type": "tool_use",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "_partial_json": "",
                    }
                elif event == "content_block_delta":
                    delta = data.get("delta", {})
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        output_text_parts.append(delta["text"])
                    elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                        thinking_parts.append(delta["thinking"])
                    elif delta.get("type") == "input_json_delta":
                        tool = tool_blocks.setdefault(data.get("index", 0), {
                            "type": "tool_use",
                            "id": f"toolu_{uuid.uuid4().hex[:24]}",
                            "name": "tool",
                            "_partial_json": "",
                        })
                        tool["_partial_json"] += delta.get("partial_json", "")
                elif event == "message_delta":
                    stop_reason = data.get("delta", {}).get("stop_reason") or stop_reason

            message_id = f"msg_{uuid.uuid4().hex[:24]}"
            output_text = "".join(output_text_parts)
            thinking_text = "".join(thinking_parts)
            output_tokens = (
                max(1, (len(output_text) + len(thinking_text)) // 4)
                if (output_text or thinking_text)
                else 0
            )
            content = []
            if thinking_text:
                content.append({
                    "type": "thinking",
                    "thinking": thinking_text,
                    "signature": COMPAT_THINKING_SIGNATURE,
                })
            if output_text:
                content.append({"type": "text", "text": output_text})
            for index in sorted(tool_blocks):
                tool = tool_blocks[index]
                content.append({
                    "type": "tool_use",
                    "id": tool["id"],
                    "name": tool["name"],
                    "input": parse_tool_arguments(tool.pop("_partial_json", "")),
                })
            if not content:
                content.append({"type": "text", "text": ""})

            response = {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": estimate_messages_tokens(genai_messages),
                    "output_tokens": output_tokens,
                },
            }
            return jsonify(response)

    except Exception as e:
        logger.exception("[%s] Unhandled error", request_id)
        return anthropic_error(
            str(e),
            error_type="api_error",
            status=500
        )
    finally:
        elapsed = time.monotonic() - start_time
        logger.info("[%s] completed in %.2fs", request_id, elapsed)


@messages_bp.route('/messages/count_tokens', methods=['POST'])
@messages_bp.route('/v1/messages/count_tokens', methods=['POST'])
def count_tokens():
    """Estimate token count for Anthropic Messages API."""
    try:
        req_data = request.get_json() or {}
        messages = req_data.get('messages', [])
        system = req_data.get('system', '')

        # Simple estimation: ~4 chars per token
        text_length = len(str(system))
        for msg in messages:
            content = msg.get('content', '')
            text_length += len(str(content))

        estimated_tokens = max(1, text_length // 4)

        return jsonify({
            "input_tokens": estimated_tokens,
        })
    except Exception as e:
        logger.exception("Error counting tokens")
        return jsonify({"input_tokens": 1})
