from __future__ import annotations

from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.config import get_config

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _is_retryable(exc: BaseException) -> bool:
    """Retry only on network errors and 5xx server errors.

    A 4xx (bad key, bad payload, content-filter, rate-limit on bad request)
    will not improve on retry — retrying just multiplies cost.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=10),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
async def chat(
    system: str,
    user: str,
    *,
    temperature: float = 0.3,
    model: str | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    cfg = get_config()
    if not cfg.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    payload: dict[str, Any] = {
        "model": model or cfg.openrouter_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    headers = {
        "Authorization": f"Bearer {cfg.openrouter_api_key}",
        "HTTP-Referer": "https://github.com/local/binance-trading-bot",
        "X-Title": "binance-trading-bot",
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]
