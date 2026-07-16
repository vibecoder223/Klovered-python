"""Mistral client — OpenAI-compatible chat completions. Port of lib/mistral.ts.

Per-model rate gate (RPM/TPM/concurrency), since Mistral limits per model, not
account-wide. Gate state is per-process — running more than one drainer needs
a shared gate (Redis); out of scope while there's a single worker.
"""

import asyncio
import json
import os
import re
import time

import httpx

from ..config import get_settings

MODEL = os.getenv("LLM_MODEL", "mistral-large-latest")
MODEL_FAST = os.getenv("LLM_MODEL_FAST", "mistral-small-latest")

_INPUT_PRICE_PER_MTOK = 0.50
_OUTPUT_PRICE_PER_MTOK = 1.50


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * _INPUT_PRICE_PER_MTOK + (
        output_tokens / 1_000_000
    ) * _OUTPUT_PRICE_PER_MTOK


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after_ms: int):
        self.retry_after_ms = retry_after_ms
        super().__init__(message)


def has_llm_key() -> bool:
    return bool(get_settings().llm_key)


def _base_url() -> str:
    return get_settings().llm_base_url.rstrip("/")


def _gate_config(model: str) -> dict:
    if model == MODEL_FAST:
        return {
            "rpm": int(os.getenv("LLM_RPM_FAST", "100")),
            "tpm": int(os.getenv("LLM_TPM_FAST", "100000")),
            "max_concurrency": int(os.getenv("LLM_MAX_CONCURRENCY_FAST", "12")),
            "min_interval_ms": int(os.getenv("LLM_MIN_INTERVAL_MS_FAST", "150")),
        }
    return {
        "rpm": int(os.getenv("LLM_RPM", "15")),
        "tpm": int(os.getenv("LLM_TPM", "400000")),
        "max_concurrency": int(os.getenv("LLM_MAX_CONCURRENCY", "8")),
        "min_interval_ms": int(os.getenv("LLM_MIN_INTERVAL_MS", "0")),
    }


class _GateState:
    def __init__(self):
        self.sem: asyncio.Semaphore | None = None
        self.token_window: list[tuple[float, int]] = []
        self.request_window: list[float] = []
        self.last_request_at = 0.0
        self.lock = asyncio.Lock()


_gate_states: dict[str, _GateState] = {}


def _state_for(model: str) -> _GateState:
    if model not in _gate_states:
        cfg = _gate_config(model)
        s = _GateState()
        s.sem = asyncio.Semaphore(cfg["max_concurrency"] or 1_000_000)
        _gate_states[model] = s
    return _gate_states[model]


def _window_tokens(s: _GateState, now: float) -> int:
    s.token_window = [(t, n) for t, n in s.token_window if now - t < 60]
    return sum(n for _, n in s.token_window)


async def _reserve_slot(model: str, est_tokens: int) -> dict | None:
    cfg = _gate_config(model)
    s = _state_for(model)
    if not cfg["rpm"] and not cfg["tpm"] and not cfg["min_interval_ms"]:
        return None
    while True:
        async with s.lock:
            now = time.monotonic()
            s.request_window = [t for t in s.request_window if now - t < 60]
            req_ok = not cfg["rpm"] or len(s.request_window) < cfg["rpm"]
            tok_ok = (
                not cfg["tpm"]
                or est_tokens >= cfg["tpm"]
                or _window_tokens(s, now) + est_tokens <= cfg["tpm"]
            )
            gap_ok = (
                not cfg["min_interval_ms"]
                or (now - s.last_request_at) * 1000 >= cfg["min_interval_ms"]
            )
            if req_ok and tok_ok and gap_ok:
                s.request_window.append(now)
                s.last_request_at = now
                if cfg["tpm"]:
                    entry = [now, est_tokens]
                    s.token_window.append(entry)
                    return {"entry": entry}
                return None
            if req_ok and tok_ok and not gap_ok:
                wait = cfg["min_interval_ms"] / 1000 - (now - s.last_request_at)
            else:
                oldest_req = s.request_window[0] if s.request_window else now
                oldest_tok = s.token_window[0][0] if s.token_window else now
                oldest = min(oldest_req, oldest_tok)
                wait = max(0.25, 60 - (now - oldest))
                wait = min(wait, 5.0)
        await asyncio.sleep(wait)


def _estimate_tokens(system: str, user: str, max_tokens: int) -> int:
    return (len(system) + len(user)) // 4 + max_tokens


async def _send_with_retries(body: dict, model: str) -> tuple[str, dict]:
    key = get_settings().llm_key
    if not key:
        raise RuntimeError("No LLM API key set (LLM_API_KEY / MISTRAL_API_KEY).")
    attempt = 0
    max_retries = 2
    async with httpx.AsyncClient(timeout=90.0) as client:
        while True:
            res = await client.post(
                f"{_base_url()}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=body,
            )
            if res.status_code == 429:
                retry_after = float(res.headers.get("retry-after", "5"))
                base = max(1.0, retry_after)
                if attempt < max_retries and base <= 30:
                    await asyncio.sleep(base * (0.7 + 0.6 * (os.urandom(1)[0] / 255)))
                    attempt += 1
                    continue
                raise RateLimitError(
                    f"LLM 429 on {model} after {max_retries} retries", int(base * 1000)
                )
            if res.status_code >= 400:
                raise RuntimeError(f"LLM {res.status_code}: {res.text[:300]}")

            j = res.json()
            msg = (j.get("choices") or [{}])[0].get("message", {})
            raw = (msg.get("content") or "").strip() or msg.get("reasoning", "")
            usage = j.get("usage") or {}
            return raw, {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            }


async def _call(
    system: str, user: str, max_tokens: int = 1500, json_mode: bool = False, model: str | None = None
) -> tuple[str, dict]:
    model = model or MODEL
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    s = _state_for(model)
    async with s.sem:
        reservation = await _reserve_slot(model, _estimate_tokens(system, user, max_tokens))
        raw, usage = await _send_with_retries(body, model)
        actual = usage["input_tokens"] + usage["output_tokens"]
        if reservation and actual > 0:
            reservation["entry"][1] = actual
        return raw, usage


def _salvage_truncated_array(s: str) -> list | None:
    if not s.startswith("["):
        return None
    depth = 0
    in_str = False
    esc = False
    last_good_end = -1
    for i, c in enumerate(s):
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 1 and c == "}":
                last_good_end = i
    if last_good_end < 0:
        return None
    try:
        v = json.loads(s[: last_good_end + 1] + "]")
        return v if isinstance(v, list) else None
    except json.JSONDecodeError:
        return None


async def call_mistral_json(
    system: str, user: str, max_tokens: int = 4096, model: str | None = None, mode: str = "json_object"
) -> tuple[object, dict]:
    sys_prompt = system if re.search(r"json", system, re.I) else f"{system}\n\nReturn valid JSON."
    raw, usage = await _call(
        sys_prompt, user, max_tokens=max_tokens, json_mode=(mode == "json_object"), model=model
    )
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"```\s*$", "", cleaned).strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"[\[{][\s\S]*[\]}]", cleaned)
        if not m:
            raise ValueError(f"LLM response not valid JSON: {cleaned[:200]}")
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            salvaged = _salvage_truncated_array(m.group(0))
            if salvaged is None:
                raise ValueError(f"LLM response not parseable JSON: {m.group(0)[:200]}")
            parsed = salvaged

    if isinstance(parsed, dict):
        array_values = [v for v in parsed.values() if isinstance(v, list)]
        if len(array_values) == 1:
            parsed = array_values[0]

    return parsed, usage


async def call_mistral_text(
    system: str, user: str, max_tokens: int = 1500, model: str | None = None
) -> tuple[str, dict]:
    raw, usage = await _call(system, user, max_tokens=max_tokens, model=model)
    return raw.strip(), usage


def fast_lane_saturated(est_tokens: int) -> bool:
    cfg = _gate_config(MODEL_FAST)
    s = _state_for(MODEL_FAST)
    now = time.monotonic()
    s.request_window = [t for t in s.request_window if now - t < 60]
    rpm_full = bool(cfg["rpm"]) and len(s.request_window) >= cfg["rpm"]
    tpm_full = (
        bool(cfg["tpm"])
        and est_tokens < cfg["tpm"]
        and _window_tokens(s, now) + est_tokens > cfg["tpm"]
    )
    return rpm_full or tpm_full
