import json

from flask import jsonify


class UpstreamError(Exception):
    """上游 GenAI 连接失败或返回错误。"""


def openai_error(message, error_type="invalid_request_error", code=None, status=400):
    """Return an OpenAI-formatted JSON error response."""
    return jsonify({
        "error": {
            "message": message,
            "type": error_type,
            "code": code
        }
    }), status


def make_error_chunk(message):
    """流式响应里的错误事件，采用 OpenAI 的 error 对象形态。

    不能把错误文本塞进 delta.content：那样客户端会把它当成模型正常输出。
    """
    error = {"error": {"message": message, "type": "upstream_error", "code": None}}
    return f"data: {json.dumps(error, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
