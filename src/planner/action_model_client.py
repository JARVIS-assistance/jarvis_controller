from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

PostJson = Callable[[str, dict[str, Any]], dict[str, Any]]


def action_model_endpoint() -> str:
    return os.getenv(
        "JARVIS_ACTION_MODEL_ENDPOINT",
        os.getenv(
            "JARVIS_ACTION_INTENT_MODEL_ENDPOINT",
            "https://qwen.breakpack.cc/engines/v1/chat/completions",
        ),
    )


def action_model_provider() -> str:
    raw = (
        os.getenv("JARVIS_ACTION_MODEL_PROVIDER")
        or os.getenv("JARVIS_INTERNAL_MODEL_PROVIDER")
        or "openai_compat"
    )
    return raw.strip().lower().replace("-", "_")


def action_intent_model_name() -> str:
    return (
        os.getenv("JARVIS_ACTION_INTENT_MODEL_NAME")
        or os.getenv("JARVIS_ACTION_INTENT_MODEL")
        or "docker.io/ai/qwen2.5:1.5B-F16"
    )


def action_compiler_model_name() -> str:
    return (
        os.getenv("JARVIS_ACTION_COMPILER_MODEL_NAME")
        or os.getenv("JARVIS_ACTION_COMPILER_MODEL")
        or os.getenv("JARVIS_ACTION_PLAN_MODEL_NAME")
        or os.getenv("JARVIS_ACTION_PLAN_MODEL")
        or "docker.io/ai/gemma4:E4B"
    )


def complete_model_text(
    *,
    provider: str,
    endpoint: str,
    model: str,
    payload: dict[str, Any],
    timeout: float,
    post_json: Callable[..., dict[str, Any]] | None = None,
) -> str:
    post = post_json or post_json_request
    if provider == "ollama_chat":
        data = post(
            _ollama_chat_url(endpoint),
            _ollama_chat_payload(model=model, payload=payload),
            timeout=timeout,
        )
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and content.strip():
            return content

        data = post(
            _ollama_generate_url(endpoint),
            _ollama_generate_payload(model=model, payload=payload),
            timeout=timeout,
        )
        content = data.get("response")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty Ollama chat/generate response")
        return content

    if provider == "ollama":
        data = post(
            _ollama_generate_url(endpoint),
            _ollama_generate_payload(model=model, payload=payload),
            timeout=timeout,
        )
        content = data.get("response")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty Ollama response")
        return content

    data = post(endpoint, payload, timeout=timeout)
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty OpenAI-compatible response")
    return content


def _ollama_chat_url(endpoint: str) -> str:
    raw = endpoint.rstrip("/")
    path = urllib.parse.urlparse(raw).path
    if path.endswith("/chat") or path.endswith("/api/chat"):
        return raw
    return f"{raw}/chat"


def _ollama_chat_payload(
    *,
    model: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": payload.get("messages", []),
        "stream": False,
        "options": {
            "temperature": payload.get("temperature", 0),
            "num_predict": payload.get("max_tokens"),
        },
    }


def _ollama_generate_url(endpoint: str) -> str:
    raw = endpoint.rstrip("/")
    path = urllib.parse.urlparse(raw).path
    if path.endswith("/api/generate"):
        return raw
    return f"{raw}/api/generate"


def _ollama_generate_payload(
    *,
    model: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    prompt = "\n\n".join(
        f"{str(message.get('role', 'user')).title()}:\n{message.get('content', '')}"
        for message in payload.get("messages", [])
        if isinstance(message, dict)
    )
    return {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": payload.get("temperature", 0),
            "num_predict": payload.get("max_tokens"),
        },
    }


def post_json_request(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "JARVIS/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(
            f"HTTP {exc.code} from action compiler: {error_body[:500]}"
        ) from exc
