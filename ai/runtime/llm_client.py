"""Vendor-agnostic LLM client over the OpenAI-compatible chat-completions API.

Works with a self-hosted model (Ollama, vLLM) or any hosted provider that exposes the
same API. ``LLM_PROVIDER=stub`` (default) raises ``LLMUnavailable`` so the runner uses
the deterministic extractor — the demo needs no key and costs nothing.
"""
from __future__ import annotations

import json

import httpx

from app import config


class LLMUnavailable(RuntimeError):
    pass


def complete(system: str, user: str) -> dict:
    if config.LLM_PROVIDER == "stub":
        raise LLMUnavailable("LLM_PROVIDER=stub")
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    body = {"model": config.LLM_MODEL, "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    try:
        r = httpx.post(config.LLM_BASE_URL.rstrip("/") + "/chat/completions", json=body,
                       headers=headers, timeout=config.LLM_TIMEOUT)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
        raise LLMUnavailable(str(e)[:200]) from e
    try:
        out = json.loads(content)
    except (TypeError, ValueError) as e:
        raise ValueError(f"model returned non-JSON: {e}") from e
    if not isinstance(out, dict):
        raise ValueError("model returned a non-object")
    return out
