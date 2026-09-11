"""Measure the real context window of a GenAI model.

The GenAI platform proxies many models, each with its own context window. The
frontend (e.g. Claude Code) needs to know how much prompt it can safely send.
This tool measures the *usable* context length with a front-marker recall test:

    put a unique marker (ZEBRA) at the very front of the input, then ask the
    model to reproduce it.

If the model can recall the marker, the whole context window was retained. If
the backend rejects the prompt, its error message usually names the exact
context limit (e.g. "maximum context length is 524288 tokens").

Usage:
    pixi run context-probe --model chatglm --tokens 8192,524288,1048576
    pixi run context-probe --model GPT-5.6-SOL --find
    pixi run context-probe --all              # quick sweep of every LLM model
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

# Make the project root importable no matter how the tool is launched.
# `python tools/context_probe.py` puts tools/ on sys.path, not the root, so the
# project modules (auth, config) would otherwise be invisible.
try:
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
except NameError:
    _PROJECT_ROOT = Path.cwd()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from auth.token_manager import TokenManager
from config import GENAI_URL, build_genai_headers, model_registry

logger = logging.getLogger(__name__)

# Unique marker placed at the very front of the input.
MARKER = "ZEBRA"
# Filler sentence; each English word approximates one token in the model.
FILL = "the quick brown fox jumps over the lazy dog near the river bank while a small bird sings a cheerful tune "
FILL_WORDS = len(FILL.split())

# Models we should never probe (image-only or not a chat model).
SKIP_MODELS = {"gpt-image-1.5", "GPT-Image-2", "GPT-Image-3"}

# Default token targets for a quick characterization run.
DEFAULT_TOKENS = [8192, 32768, 131072, 524288, 1048576]

# Stop the --find upper-bound search here to keep requests bounded.
FIND_HI_CAP = 2097152


def build_history(target_tokens: int) -> str:
    """Build the history message of roughly `target_words` tokens.

    `target_tokens` is treated as an approximate word count; the model's own
    token counter (returned as total_tokens) is the authoritative measurement.
    """
    if target_tokens < 2:
        target_tokens = 2
    reps = (target_tokens // FILL_WORDS) + 2
    words = (FILL * reps).split()
    text = " ".join(words[: target_tokens - 1])
    return f"{MARKER} {text}"


def extract_context_limit(error: str) -> int | None:
    """Pull the numeric context limit out of a backend error message."""
    if not error:
        return None
    m = re.search(r"context length (?:is|of) ([\d,]+)", error)
    if not m:
        m = re.search(r"maximum context length[^0-9]*([\d,]+)", error)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def probe(model: str, root: str, token: str, target_tokens: int,
          max_output: int = 300, timeout: int = 300) -> dict:
    """Send one front-marker recall probe and return a result dict."""
    hist = build_history(target_tokens)
    ask = ("The very first word of the message above is a codeword. "
           "Reply with EXACTLY that codeword and nothing else.")
    body = {
        "chatInfo": ask,
        "messages": [{"role": "user", "content": hist}],
        "type": "3",
        "stream": True,
        "aiType": model,
        "aiSecType": "1",
        "promptTokens": 0,
        "rootAiType": root,
        "maxToken": max_output,
    }
    started = time.monotonic()
    status = None
    content = ""
    reasoning = ""
    error = ""
    total_tokens = None
    try:
        resp = requests.post(
            GENAI_URL, headers=build_genai_headers(token), json=body,
            stream=True, timeout=timeout,
        )
        status = resp.status_code
        if status != 200:
            return {
                "status": status, "total_tokens": None, "answered": False,
                "error": f"HTTP {status}: {resp.text[:200]}",
                "elapsed": round(time.monotonic() - started, 1),
            }

        for line in resp.iter_lines():
            if not line:
                continue
            raw = line.decode("utf-8", "replace")
            if not raw.startswith("data:"):
                continue
            data_str = raw[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("success") is False:
                error = data.get("message") or error
            if data.get("errMsg"):
                error = data["errMsg"]
            if data.get("error"):
                err = data["error"]
                error = (err.get("message") if isinstance(err, dict) else str(err)) or error
            if "choices" in data and data["choices"]:
                delta = data["choices"][0].get("delta", {})
                content += delta.get("content", "") or ""
                reasoning += (delta.get("reasoning_content") or "") or (delta.get("reasoning") or "")
            if data.get("usage"):
                total_tokens = data["usage"].get("total_tokens")
            other = data.get("other")
            if other and total_tokens is None:
                try:
                    total_tokens = json.loads(other).get("totalTokens")
                except (json.JSONDecodeError, TypeError):
                    pass
    except requests.exceptions.RequestException as exc:
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # keep the tool alive on any upstream/parse hiccup
        error = f"{type(exc).__name__}: {exc}"

    answered = MARKER in (content + " " + reasoning)
    return {
        "status": status,
        "total_tokens": total_tokens,
        "answered": answered,
        "content_tail": (content or "").strip()[-40:],
        "error": error,
        "elapsed": round(time.monotonic() - started, 1),
    }


def classify(res: dict) -> tuple:
    """Return (verdict, detail) for a probe result."""
    if res.get("status") != 200:
        return "HTTP_ERROR", res.get("error", "")
    limit = extract_context_limit(res.get("error", ""))
    if limit is not None:
        return "OVER_LIMIT", f"exact limit={limit}"
    if res.get("answered"):
        return "PASS", "marker recalled"
    if res.get("total_tokens") is not None:
        return "EMPTY", "no content returned (overflow)"
    return "ERROR", res.get("error", "")


def print_row(model: str, target: int, res: dict) -> None:
    """Print one human-readable probe row."""
    verdict, detail = classify(res)
    toks = res.get("total_tokens")
    toks_s = f"{toks}" if toks is not None else "-"
    print(f"  {model:12s} ~{target:>9,} tok | status={res.get('status')} "
          f"actual={toks_s:>10} | {verdict:<10} {detail}")


def run_probes(model: str, root: str, token: str, targets, max_output, timeout) -> None:
    """Probe the model at a list of token targets and print a summary."""
    print(f"\n=== {model} (root={root}) ===")
    results = []
    for target in targets:
        res = probe(model, root, token, target, max_output, timeout)
        results.append((target, res))
        print_row(model, target, res)
    # Summarise the best PASS and any exact limit found.
    best = None
    limit = None
    for target, res in results:
        verdict, _ = classify(res)
        if verdict == "PASS" and (best is None or (res.get("total_tokens") or 0) > (best.get("total_tokens") or 0)):
            best = res
        if verdict == "OVER_LIMIT":
            limit = extract_context_limit(res.get("error", ""))
    summary = []
    if best and best.get("total_tokens"):
        summary.append(f"works up to at least {best['total_tokens']:,} tokens")
    if limit:
        summary.append(f"hard limit {limit:,} tokens")
    print(f"  -> {model}: {', '.join(summary) if summary else 'no limit found in tested range'}")


def find_limit(model: str, root: str, token: str, low=8192, hi_cap=FIND_HI_CAP,
               max_output=300, timeout=300) -> None:
    """Binary-search the context limit starting from a doubling upper bound.

    Doubles `low` until a probe fails or `hi_cap` is reached, then binary
    searches between the last passing and the first failing size.
    """
    print(f"\n=== {model} (root={root}) searching limit in [{low}, {hi_cap}] ===")

    def ok(tokens):
        res = probe(model, root, token, tokens, max_output, timeout)
        print_row(model, tokens, res)
        verdict, _ = classify(res)
        return verdict == "PASS", res

    # Phase 1: find an upper bound by doubling; clamp the step to hi_cap so we
    # never skip past the cap without probing it (which would hide a failure).
    lo_ok = low
    last_pass_res = None
    cur = low
    hi_fail = None
    while True:
        passed, res = ok(cur)
        if not passed:
            hi_fail = cur
            break
        lo_ok = cur
        last_pass_res = res
        if cur >= hi_cap:
            break
        cur = min(cur * 2, hi_cap)

    if hi_fail is None:
        tok = last_pass_res.get("total_tokens") if last_pass_res else lo_ok
        print(f"  -> {model}: handled >= {tok:,} tokens (no limit within cap {hi_cap:,})")
        return

    # Phase 2: binary search between the last passing and the first failing size.
    # Stop once the bracket is within ~2% (or at least 1024 tokens) of the size;
    # over-refining would burn a lot of expensive requests for little precision.
    lo = lo_ok
    hi = hi_fail
    tolerance = max(1024, int(lo * 0.02))
    while lo + 1 < hi and (hi - lo) > tolerance:
        mid = (lo + hi) // 2
        passed, _ = ok(mid)
        if passed:
            lo = mid
        else:
            hi = mid

    # Confirm the boundary with fresh probes and report the real token counts.
    last_pass = probe(model, root, token, lo, max_output, timeout)
    print_row(model, lo, last_pass)
    first_fail = probe(model, root, token, hi, max_output, timeout)
    print_row(model, hi, first_fail)
    lo_tok = last_pass.get("total_tokens") or lo
    hi_tok = first_fail.get("total_tokens") or extract_context_limit(first_fail.get("error", "")) or hi
    print(f"  -> {model}: usable up to ~{lo_tok:,} tokens, fails at ~{hi_tok:,} tokens")


def list_models(token: str) -> list:
    """Return (model_id, root_ai_type) pairs for all chat models."""
    models = model_registry.get_models(token)
    out = []
    for mid, info in models.items():
        if mid in SKIP_MODELS or info.root_ai_type in ("image",):
            continue
        out.append((mid, info.root_ai_type))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="model id to probe")
    parser.add_argument("--all", action="store_true", help="quick sweep every chat model")
    parser.add_argument("--tokens", help="comma-separated target token sizes (default: a benchmark set)")
    parser.add_argument("--find", action="store_true", help="binary-search the exact context limit")
    parser.add_argument("--low", type=int, default=8192, help="lower bound for --find")
    parser.add_argument("--high", type=int, default=FIND_HI_CAP, help="upper bound cap for --find")
    parser.add_argument("--max-output", type=int, default=300, help="max output tokens per probe")
    parser.add_argument("--timeout", type=int, default=300, help="per-request timeout in seconds")
    parser.add_argument("--token", help="JWT or student_id@password (overrides GENAI_TOKEN env)")
    args = parser.parse_args(argv)

    # Keep chatty auth/network INFO logs quiet; the printed result table is the output.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    try:
        root = Path(__file__).resolve().parent.parent
    except NameError:
        root = Path.cwd()
    load_dotenv(root / ".env")

    token_input = args.token or os.environ.get("GENAI_TOKEN")
    if not token_input:
        parser.error("token required via --token or GENAI_TOKEN env")
    token_manager = TokenManager(token_input)
    token = token_manager.get_token()

    if args.tokens:
        targets = [int(x) for x in args.tokens.split(",") if x.strip()]
    else:
        targets = DEFAULT_TOKENS

    models = dict(list_models(token))
    if args.model:
        targets_models = [(args.model, models.get(args.model) or model_registry.get_root_ai_type(args.model, token))]
    elif args.all:
        targets_models = list(models.items())
    else:
        parser.error("specify a model with --model, or --all to sweep every chat model")

    for model, root in targets_models:
        if args.find:
            find_limit(model, root, token, args.low, args.high, args.max_output, args.timeout)
        else:
            run_probes(model, root, token, targets, args.max_output, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
