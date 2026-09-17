"""GenAI 网页功能：附件上传、思考开关、联网检索。"""
import base64
import binascii
import logging
import mimetypes
import re
import time
import uuid
from urllib.parse import unquote, urljoin, urlparse

import requests

from config import build_genai_headers
from errors import UpstreamError
from tools.prompts import flatten_message_content, normalize_content

logger = logging.getLogger(__name__)
BASE_URL = "https://genai.shanghaitech.edu.cn/htk"
SITE_URL = "https://genai.shanghaitech.edu.cn/"
IMAGE_LIMIT = 20 * 1024 * 1024
DOCUMENT_LIMIT = 10 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
APP_JS_PATTERN = re.compile(r"(/js/app\.[0-9a-f]+\.js)")
UPLOAD_TOKEN_PATTERN = re.compile(r'token\s*:\s*"([0-9a-f]{32})"')
_upload_token = None


def image_upload_token():
    """图片服务令牌：从前端脚本自动获取，进程内缓存。

    图片服务是独立域名，它不认用户的登录令牌（不带令牌会返回 success=true 但
    result 为空）。网页前端把自己使用的固定令牌写在了 app.js 中，这里同样读取
    该公开资源，避免让使用者手工填写。
    """
    global _upload_token
    if _upload_token:
        return _upload_token
    try:
        with requests.get(SITE_URL, timeout=15) as response:
            response.raise_for_status()
            app_js = APP_JS_PATTERN.search(response.text)
        if not app_js:
            raise UpstreamError("未能从网页首页定位前端脚本，无法获取图片服务令牌")
        with requests.get(urljoin(SITE_URL, app_js.group(1)), timeout=30) as response:
            response.raise_for_status()
            found = UPLOAD_TOKEN_PATTERN.search(response.text)
        if not found:
            raise UpstreamError("前端脚本中未找到图片服务令牌，图片上传暂不可用")
    except requests.RequestException as exc:
        raise UpstreamError("获取图片服务令牌失败，请稍后重试") from exc
    _upload_token = found.group(1)
    logger.info("image upload token acquired from web frontend")
    return _upload_token


def is_web_search_tool(tool):
    """识别客户端托管搜索声明，不将其伪装成本地函数调用。"""
    return isinstance(tool, dict) and str(tool.get("type", "")).startswith("web_search")


def request_options(body, api_format):
    """把客户端思考及搜索参数映射为平台已验证的布尔控制。"""
    options = {}
    thinking = body.get("thinking")
    if thinking is not None:
        if isinstance(thinking, bool):
            options["thinking"] = thinking
        elif api_format == "anthropic" and isinstance(thinking, dict):
            mode = thinking.get("type")
            if mode not in ("enabled", "adaptive", "disabled"):
                raise ValueError("thinking.type 必须是 enabled、adaptive 或 disabled")
            options["thinking"] = mode != "disabled"
            if "budget_tokens" in thinking:
                logger.warning("thinking.budget_tokens 仅启用思考，GenAI 不支持精确思考预算")
        else:
            raise ValueError("thinking 必须是布尔值，或 Anthropic thinking 对象")
    else:
        reasoning = body.get("reasoning") or {}
        if not isinstance(reasoning, dict):
            raise ValueError("reasoning 必须是对象")
        effort = body.get("reasoning_effort") if api_format == "chat" else reasoning.get("effort")
        if effort is not None:
            if effort not in ("none", "minimal", "low", "medium", "high", "xhigh"):
                raise ValueError("无效的 reasoning effort")
            options["thinking"] = effort != "none"
            if effort != "none":
                logger.info("reasoning effort=%s 映射为 thinking=true；平台不提供档位控制", effort)

    search = body.get("web_search")
    if search is not None and not isinstance(search, bool):
        raise ValueError("web_search 必须是布尔值")
    declared = any(is_web_search_tool(tool) for tool in body.get("tools") or [])
    tool_choice = body.get("tool_choice")
    tools_disabled = tool_choice == "none" or (isinstance(tool_choice, dict) and tool_choice.get("type") == "none")
    if isinstance(tool_choice, dict) and tool_choice.get("type") in ("function", "tool", "custom"):
        selected_name = tool_choice.get("name") or (tool_choice.get("function") or {}).get("name")
        search_names = {tool.get("name", "web_search") for tool in body.get("tools") or [] if is_web_search_tool(tool)}
        tools_disabled = selected_name not in search_names
    options["netGo"] = search if search is not None else (
        "web_search_options" in body or (declared and not tools_disabled)
    )
    return options


def attachment_url(url):
    """仅接受 HTTP(S) 附件地址，不读取本地文件或 URL 内的凭据。"""
    if not isinstance(url, str):
        raise ValueError("附件 URL 必须是字符串")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("附件 URL 必须是无用户凭据的 HTTP(S) 地址")
    return parsed


def decode_file(data, filename, limit):
    """解码内联 base64，并按网页的附件限制检查大小。"""
    if not isinstance(data, str):
        raise ValueError("附件 base64 必须是字符串")
    mime = mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
    if data.startswith("data:"):
        header, separator, data = data.partition(",")
        if not separator or not header.endswith(";base64"):
            raise ValueError("附件 data URL 必须使用 base64 编码")
        mime = header[5:-7] or mime
    if len(data) > (limit * 4 // 3 + 4):
        raise ValueError("附件超过大小限制")
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("附件不是有效 base64") from exc
    if not raw or len(raw) >= limit:
        raise ValueError("附件为空或超过大小限制")
    return raw, mime


def download_attachment(url, limit):
    """有界下载 HTTP(S) 附件，不向源站转发平台令牌。"""
    for _ in range(6):
        parsed = attachment_url(url)
        with requests.get(url, stream=True, allow_redirects=False, timeout=(10, 30)) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size >= limit:
                    raise ValueError(f"附件必须小于 {limit // (1024 * 1024)} MiB")
                chunks.append(chunk)
            if not size:
                raise ValueError("附件为空")
            filename = unquote(parsed.path.rsplit("/", 1)[-1])
            mime = response.headers.get("Content-Type", "application/octet-stream").split(";")[0]
            if mime == "application/octet-stream":
                mime = mimetypes.guess_type(filename)[0] or mime
            return b"".join(chunks), mime, filename
    raise ValueError("附件 URL 重定向次数过多")


def upload_json(url, token, **kwargs):
    """上传到平台业务接口并校验业务状态，不在日志暴露凭据或正文。"""
    headers = {"X-Access-Token": token}
    with requests.post(url, headers=headers, timeout=(10, 120), **kwargs) as response:
        response.raise_for_status()
        payload = response.json()
    if not payload.get("success"):
        raise UpstreamError("附件上传失败: " + str(payload.get("message", "未知错误")))
    return payload.get("result") or {}


def upload_image(source, token):
    """上传 URL/base64 图片，取得平台 URL 与视觉请求所需的尺寸。"""
    if source.startswith("data:"):
        raw, mime = decode_file(source, "image.png", IMAGE_LIMIT)
    else:
        raw, mime, _ = download_attachment(source, IMAGE_LIMIT)
    if mime not in IMAGE_TYPES:
        raise ValueError("图片仅支持 PNG/JPEG/WEBP/GIF")
    headers = build_genai_headers(token)
    with requests.get(BASE_URL + "/sys/dict/getDictItems/file_url", headers=headers, timeout=15) as response:
        response.raise_for_status()
        settings = response.json()
    if not settings.get("success") or not settings.get("result"):
        raise UpstreamError("平台未提供图片服务地址")
    base = settings["result"][0]["value"].rstrip("/")
    filename = "image" + (mimetypes.guess_extension(mime) or ".png")
    with requests.post(
        base + "/sys/common/upload",
        headers={"token": image_upload_token()},
        data={"biz": "temp", "uploadType": "local"},
        files={"file": (filename, raw, mime)},
        timeout=(10, 120),
    ) as response:
        response.raise_for_status()
        payload = response.json()
    result = payload.get("result") or {}
    if not payload.get("success") or not result.get("url"):
        raise UpstreamError("图片上传失败: " + str(payload.get("message", "缺少图片地址")))
    if not result.get("width") or not result.get("height"):
        raise UpstreamError("图片上传未返回有效尺寸")
    logger.info("attachment uploaded kind=image bytes=%d", len(raw))
    return base + "/sys/common/static/" + result["url"], result["width"], result["height"]


def upload_document(file, token, model, group):
    """上传文档以获得文件标识和解析文本，当前请求使用同一会话。"""
    filename = str(file.get("filename") or "").replace("\\", "/").rsplit("/", 1)[-1]
    if file.get("file_data"):
        raw, mime = decode_file(file["file_data"], filename, DOCUMENT_LIMIT)
    else:
        raw, mime, remote_name = download_attachment(file["file_url"], DOCUMENT_LIMIT)
        filename = filename or remote_name
    if not filename:
        filename = "document"
    if "." not in filename:
        filename += mimetypes.guess_extension(mime) or ".bin"
    result = upload_json(
        BASE_URL + "/chat/upload/chat/file", token,
        params={"chatGroupId": group, "aiType": model},
        data={"biz": "temp"}, files={"file": (filename, raw, mime)},
    )
    if not result.get("fileId"):
        raise UpstreamError("文档上传响应缺少 fileId")
    logger.info("attachment uploaded kind=document bytes=%d parsed_chars=%d", len(raw), len(result.get("content") or ""))
    return result, filename


def prepare_request(messages, model, config, body, api_format):
    """在开始响应前完成附件处理，保留历史多模态输入并生成当前轮参数。"""
    options = request_options(body, api_format)
    # 网页每轮都发送空图片字段；历史有图片时也保持同样的请求形状。
    options.update(imageUrl="", imageUrls=[], width="", height="")
    last_user = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
    if last_user is None:
        raise ValueError("请求需要至少一条 user 消息")
    prepared, images, file_ids = [], [], []
    group = "proxy-" + uuid.uuid4().hex[:20]
    image_count = document_count = 0
    started = time.monotonic()
    try:
        for index, message in enumerate(messages):
            content = normalize_content(message.get("content", ""))
            parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
            output = []
            for part in parts:
                if part["type"] == "text":
                    output.append(part)
                elif part["type"] == "image_url":
                    image_count += 1
                    url, width, height = upload_image(part["image_url"]["url"], config.token_manager.get_token())
                    if index == last_user:
                        if not images:
                            options.update(width=width, height=height)
                        images.append(url)
                    else:
                        output.append({"type": "image_url", "image_url": {"url": url, "detail": part["image_url"].get("detail", "high")}})
                elif part["type"] == "file":
                    document_count += 1
                    result, filename = upload_document(part["file"], config.token_manager.get_token(), model, group)
                    if index == last_user:
                        file_ids.append(result["fileId"])
                    else:
                        text = result.get("content")
                        if not isinstance(text, str) or not text.strip():
                            raise UpstreamError("历史文档未返回解析文本，无法保留上下文")
                        output.append({"type": "text", "text": f"[file name]: {filename}\n[file content begin]\n{text}\n[file content end]"})
            # 与网页一致：历史内容先放一个合并文本块，再放图片块。
            text = flatten_message_content(output)
            history_images = [part for part in output if part["type"] == "image_url"]
            content = [{"type": "text", "text": text}, *history_images] if history_images else text
            if index == last_user and not flatten_message_content(parts).strip():
                if images or file_ids:
                    content = "请分析所附内容。\n" + content
                else:
                    raise ValueError("当前 user 消息没有文本或附件")
            prepared.append({**message, "content": content})
    except (requests.RequestException, KeyError) as exc:
        logger.warning("attachment preparation failed error=%s", type(exc).__name__)
        raise UpstreamError("附件服务请求失败，请稍后重试") from exc
    if image_count or document_count or options.get("netGo"):
        options["chatGroupId"] = group
    if images:
        options.update(imageUrl=images[0], imageUrls=images)
    if file_ids:
        options["fileIds"] = file_ids
    logger.info("request features model=%s thinking=%s search=%s images=%d documents=%d prepare_ms=%.0f", model, options.get("thinking", "default"), options["netGo"], image_count, document_count, (time.monotonic() - started) * 1000)
    return prepared, options


def search_sources(body, token):
    """获取独立检索列表，不把其编号冒充正文引用编号。"""
    with requests.post(BASE_URL + "/chat/net/search", headers=build_genai_headers(token), json=body, timeout=(10, 45)) as response:
        response.raise_for_status()
        payload = response.json()
    if not payload.get("success"):
        raise UpstreamError("补充检索失败")
    links = (payload.get("result") or {}).get("links") or []
    lines = []
    for link in links:
        url = link.get("link", "")
        if urlparse(url).scheme in ("http", "https"):
            title = str(link.get("title") or url).replace("[", "\\[").replace("]", "\\]").replace("\n", " ")
            lines.append(f"- [{title}](<{url}>)")
    logger.info("search sources count=%d", len(lines))
    return "\n\n补充检索结果（独立于正文引用编号）：\n" + "\n".join(lines) if lines else ""
