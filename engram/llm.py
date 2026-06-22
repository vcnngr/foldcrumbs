"""Minimal OpenAI-compatible chat client (stdlib urllib only).

Used solely for async distillation/maintenance — recall never touches the LLM.
Points at ENGRAM_LLM_ENDPOINT (default local MLX server on :8081). Swap to a
remote gateway or OpenRouter by changing the env var. Fail-soft: any error
returns None and the caller degrades to the heuristic path.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config


def chat(
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int = 1024,
) -> str | None:
    """POST /v1/chat/completions. Returns assistant text, or None on failure."""
    url = config.LLM_ENDPOINT.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None


def available() -> bool:
    """Cheap reachability probe for the configured endpoint."""
    url = config.LLM_ENDPOINT.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False
