"""Measure streaming time-to-first-token (TTFT) and output speed of every model.

Through the local proxy, sends streaming /v1/chat/completions requests and times:

    TTFT   - from request start to the first delta carrying content or reasoning
    速度   - completion_tokens / (total_time - TTFT)，即首字之后的解码速度

首个 token 也可能是 reasoning_content（思维链），这类模型会在结果里标注。

Usage:
    pixi run bench-speed
    pixi run bench-speed --models chatglm,deepseek-chat --runs 5
    pixi run bench-speed --runs 1 --max-tokens 256
    pixi run bench-speed --output BENCH_speed.md
"""

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# `python tools/bench_speed.py` 会把 tools/ 而不是项目根目录放进 sys.path，
# 这里显式补上根目录，才能 import 项目模块。
try:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
except NameError:
    _PROJECT_ROOT = Path.cwd()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from provider.genai import estimate_text_tokens

# 图片模型不是对话模型，默认跳过。
SKIP_MODELS = {"gpt-image-1.5", "GPT-Image-2", "GPT-Image-3"}

# 固定的计数任务：输出长度可预期，模型也不会拒答，适合横向比速度。
DEFAULT_PROMPT = (
    "请从 1 开始连续数数，一直数到 200，数字之间用空格分隔。"
    "只输出数字本身，不要输出任何解释或其他文字。"
)


@dataclass
class RunResult:
    """一次流式请求的计时结果。"""

    ok: bool = False
    ttft: float | None = None          # 首字时间（秒，含思维链）
    content_ttft: float | None = None  # 首个正文 token 时间（秒）
    total: float | None = None         # 整次请求耗时（秒）
    tokens: int = 0                    # 输出 token 数（优先用上游 usage）
    first_kind: str = ""               # content / reasoning
    finish_reason: str | None = None
    error: str | None = None

    @property
    def decode_seconds(self) -> float | None:
        """首字之后的解码时长。"""
        if self.ttft is None or self.total is None or self.total <= self.ttft:
            return None
        return self.total - self.ttft

    @property
    def speed(self) -> float | None:
        """解码速度（tokens/s）。"""
        decode = self.decode_seconds
        if not decode or not self.tokens:
            return None
        return self.tokens / decode


def list_models(base_url: str, api_key: str | None, timeout: int) -> list[str]:
    """从代理的 /v1/models 拉取对话模型 id 列表。"""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = requests.get(f"{base_url.rstrip('/')}/v1/models", headers=headers, timeout=timeout)
    resp.raise_for_status()
    return [
        m["id"]
        for m in resp.json().get("data", [])
        if m.get("id") and m["id"] not in SKIP_MODELS
    ]


def stream_once(
    base_url: str,
    api_key: str | None,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: int,
) -> RunResult:
    """发一次流式请求并计时，返回本次结果。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tokens,
    }

    started = time.monotonic()
    ttft = None
    content_ttft = None
    first_kind = ""
    tokens_from_usage = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = None
    error = None

    try:
        with requests.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json=payload,
            stream=True,
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                body = resp.text[:200].replace("\n", " ")
                return RunResult(total=time.monotonic() - started,
                                 error=f"HTTP {resp.status_code}: {body}")

            resp.encoding = "utf-8"
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if "error" in chunk:
                    error = str(chunk["error"])[:200]
                    break

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                reasoning = delta.get("reasoning_content")

                if (content or reasoning) and ttft is None:
                    ttft = time.monotonic() - started
                    first_kind = "reasoning" if reasoning else "content"
                if content:
                    if content_ttft is None:
                        content_ttft = time.monotonic() - started
                    content_parts.append(content)
                if reasoning:
                    reasoning_parts.append(reasoning)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                usage = chunk.get("usage")
                if usage:
                    tokens_from_usage = usage.get("completion_tokens")
    except Exception as e:  # 网络超时、连接被拒等一律记录为该模型的失败
        return RunResult(total=time.monotonic() - started, error=f"{type(e).__name__}: {e}")

    total = time.monotonic() - started
    if error:
        return RunResult(total=total, ttft=ttft, error=error)

    text = "".join(content_parts) + "".join(reasoning_parts)
    tokens = tokens_from_usage if tokens_from_usage else estimate_text_tokens(text)
    return RunResult(
        ok=ttft is not None,
        ttft=ttft,
        content_ttft=content_ttft,
        total=total,
        tokens=tokens,
        first_kind=first_kind,
        finish_reason=finish_reason,
        error=None if ttft is not None else "无任何输出",
    )


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


@dataclass
class ModelSummary:
    """一个模型所有有效 run 的汇总。"""

    model: str
    results: list[RunResult] = field(default_factory=list)

    @property
    def ok_results(self) -> list[RunResult]:
        return [r for r in self.results if r.ok]

    @property
    def median_ttft(self) -> float | None:
        return median([r.ttft for r in self.ok_results if r.ttft is not None])

    @property
    def median_content_ttft(self) -> float | None:
        return median([r.content_ttft for r in self.ok_results if r.content_ttft is not None])

    @property
    def median_tokens(self) -> float | None:
        return median([float(r.tokens) for r in self.ok_results if r.tokens])

    @property
    def median_decode(self) -> float | None:
        return median([r.decode_seconds for r in self.ok_results if r.decode_seconds])

    @property
    def median_speed(self) -> float | None:
        return median([r.speed for r in self.ok_results if r.speed])

    @property
    def first_kind(self) -> str:
        kinds = {r.first_kind for r in self.ok_results if r.first_kind}
        return "/".join(sorted(kinds)) if kinds else "-"


def fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def run_benchmark(args) -> list[ModelSummary]:
    """按顺序逐个模型测量，避免并发互相干扰。"""
    load_dotenv(_PROJECT_ROOT / ".env")
    import os

    api_key = args.api_key or os.environ.get("API_KEY") or None

    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = list_models(args.base_url, api_key, args.timeout)
    print(f"待测模型 {len(models)} 个：{', '.join(models)}", flush=True)

    summaries = []
    for model in models:
        summary = ModelSummary(model=model)
        plan = ["warmup"] * args.warmup + [f"run{i + 1}" for i in range(args.runs)]
        for phase in plan:
            result = stream_once(
                args.base_url, api_key, model, args.prompt, args.max_tokens, args.timeout
            )
            if phase != "warmup":
                summary.results.append(result)
            print(
                f"  [{model}] {phase:<6} "
                f"ttft={fmt(result.ttft)}s tokens={result.tokens} "
                f"total={fmt(result.total)}s speed={fmt(result.speed)}tok/s "
                f"{'ERR:' + result.error if result.error else ''}",
                flush=True,
            )
        summaries.append(summary)
    return summaries


def render_report(summaries: list[ModelSummary], args) -> str:
    """把汇总渲染成 Markdown 表格。"""
    lines = [
        f"# 模型首字时间与输出速度测试",
        "",
        f"- 测试时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 接口：`POST {args.base_url.rstrip('/')}/v1/chat/completions`（流式）",
        f"- 每个模型：预热 {args.warmup} 次 + 正式 {args.runs} 次，取中位数",
        f"- max_tokens：{args.max_tokens}",
        f"- Prompt：{args.prompt}",
        "",
        "| 模型 | 首字中位 (s) | 正文首字 (s) | 首字类型 | 输出 tokens | 解码耗时 (s) | 速度 (tok/s) | 状态 |",
        "|---|---:|---:|---|---:|---:|---:|---|",
    ]
    for s in summaries:
        failed = len(s.results) - len(s.ok_results)
        status = "正常" if failed == 0 else f"{failed}/{len(s.results)} 次失败"
        if not s.ok_results:
            first_error = next((r.error for r in s.results if r.error), "无有效结果")
            status = f"失败：{first_error}"
        lines.append(
            f"| {s.model} | {fmt(s.median_ttft)} | {fmt(s.median_content_ttft)} | "
            f"{s.first_kind} | {fmt(s.median_tokens, 0)} | {fmt(s.median_decode)} | "
            f"{fmt(s.median_speed)} | {status} |"
        )

    lines += ["", "## 每次请求明细", "",
              "| 模型 | 轮次 | 首字 (s) | 正文首字 (s) | tokens | 总耗时 (s) | 速度 (tok/s) | finish_reason |",
              "|---|---:|---:|---:|---:|---:|---:|---|"]
    for s in summaries:
        for i, r in enumerate(s.results, 1):
            note = f"ERR {r.error}" if r.error else (r.finish_reason or "-")
            lines.append(
                f"| {s.model} | {i} | {fmt(r.ttft)} | {fmt(r.content_ttft)} | {r.tokens} | "
                f"{fmt(r.total)} | {fmt(r.speed)} | {note} |"
            )

    lines += [
        "",
        "## 口径说明",
        "",
        "- 首字时间（TTFT）：从发出请求到收到第一个携带内容或思维链的增量。",
        "- 正文首字：从发出请求到收到第一个正文（非思维链）增量的时间；纯文本模型与 TTFT 相同。",
        "- 速度：输出 token 数 ÷（总耗时 − 首字时间），即首字后的解码速度，不含首字等待。",
        "- 输出 token 数优先取流末尾 `usage.completion_tokens`（代理估算值），缺失时本地按同一规则估算。",
        "- 思维链 token 计入输出，因此推理模型的“速度”是含思维链的吞吐。",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:31100", help="代理地址")
    parser.add_argument("--api-key", help="代理 API key（或设置 API_KEY 环境变量）")
    parser.add_argument("--models", help="逗号分隔的模型 id，默认取 /v1/models 全部对话模型")
    parser.add_argument("--runs", type=int, default=3, help="每个模型正式测量次数（默认 3）")
    parser.add_argument("--warmup", type=int, default=1, help="每个模型预热次数（默认 1，不计入结果）")
    parser.add_argument("--max-tokens", type=int, default=512, help="单次请求输出上限（默认 512）")
    parser.add_argument("--timeout", type=int, default=300, help="单次请求超时秒数（默认 300）")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="测试用的用户消息")
    parser.add_argument("--output", help="把 Markdown 报告写入该文件")
    args = parser.parse_args(argv)

    summaries = run_benchmark(args)
    report = render_report(summaries, args)
    print()
    print(report)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"报告已写入 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
