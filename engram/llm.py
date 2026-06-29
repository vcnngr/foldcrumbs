"""Minimal OpenAI-compatible chat client (stdlib urllib only).

Used solely for async distillation/maintenance — recall never touches the LLM.
Points at ENGRAM_LLM_ENDPOINT (default local MLX server on :8081). Swap to a
remote gateway or OpenRouter by changing the env var. Fail-soft: any error
returns None and the caller degrades to the heuristic path.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from . import config


def chat(
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int = 1024,
    json_schema: dict | None = None,
) -> str | None:
    """Return assistant text for ``messages``, or None on failure.

    Routes to the configured backend: the Claude CLI (print mode) when
    ``ENGRAM_LLM_BACKEND=claude-cli``, otherwise an OpenAI-compatible HTTP
    endpoint. Either way, any failure returns None so the caller degrades to
    the heuristic path.
    """
    if config.llm_backend() == "claude-cli":
        return _chat_claude_cli(messages)
    return _chat_openai(messages, temperature, max_tokens, json_schema)


def _chat_openai(
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int = 1024,
    json_schema: dict | None = None,
) -> str | None:
    """POST /v1/chat/completions. Returns assistant text, or None on failure.

    When ``json_schema`` is given, request OpenAI structured output
    (``response_format``). Servers that ignore the field still work — the
    caller's parser is tolerant — so this is a best-effort quality nudge.
    """
    url = config.LLM_ENDPOINT.rstrip("/") + "/v1/chat/completions"
    payload: dict = {
        "model": config.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "memories",
                "schema": json_schema,
                "strict": True,
            },
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


def _chat_claude_cli(messages: list[dict[str, str]]) -> str | None:
    """Run the Claude CLI in print mode (`claude -p`) and return its text.

    For machines with no local LLM server. The system/user messages are
    flattened into one prompt (print mode has no system role). Returns None on
    any failure so the caller falls back to the heuristic path.
    """
    # Never recurse: a `claude -p` we spawned must not spawn another distill.
    if config.DISABLED:
        return None
    bin_ = config.claude_bin()
    binpath = shutil.which(bin_) or bin_
    prompt = "\n\n".join(m.get("content", "") for m in messages if m.get("content"))
    if not prompt.strip():
        return None
    env = dict(os.environ)
    env["ENGRAM_DISABLE"] = "1"  # kill-switch for the nested session's hooks
    try:
        proc = subprocess.run(
            [binpath, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=config.LLM_TIMEOUT,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def available() -> bool:
    """Cheap reachability probe for the configured backend."""
    if config.llm_backend() == "claude-cli":
        # Unavailable inside an engram-spawned session (recursion guard) or when
        # the CLI isn't found.
        if config.DISABLED:
            return False
        return shutil.which(config.claude_bin()) is not None
    url = config.LLM_ENDPOINT.rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False
