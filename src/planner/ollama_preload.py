from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from .action_model_client import (
    _ollama_generate_urls,
    action_model_endpoint,
    post_json_request,
)

logger = logging.getLogger("jarvis_controller.ollama_preload")


def ollama_preload_enabled() -> bool:
    return os.getenv("JARVIS_OLLAMA_PRELOAD_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ollama_preload_models() -> list[str]:
    explicit = os.getenv("JARVIS_OLLAMA_PRELOAD_MODELS", "")
    if explicit.strip():
        return _dedupe_model_names(explicit.split(","))

    defaults = [
        os.getenv("JARVIS_RUNTIME_PROFILE_LLM_MODEL")
        or os.getenv("JARVIS_RUNTIME_PROFILE_LLM_MODEL_NAME")
        or "gemma4:e2b",
    ]
    return _dedupe_model_names(defaults)


def start_ollama_preload_thread() -> threading.Thread | None:
    if not ollama_preload_enabled():
        return None
    thread = threading.Thread(
        target=_preload_ollama_models_loop,
        name="ollama-preload",
        daemon=True,
    )
    thread.start()
    return thread


def _preload_ollama_models_loop() -> None:
    while True:
        preload_ollama_models()
        interval = _float_env("JARVIS_OLLAMA_PRELOAD_INTERVAL_SECONDS", 21600.0)
        if interval <= 0:
            return
        time.sleep(interval)


def preload_ollama_models(
    *,
    models: list[str] | None = None,
    endpoint: str | None = None,
    keep_alive: str | None = None,
    timeout: float | None = None,
    post_json: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, bool]:
    selected_models = models or ollama_preload_models()
    if not selected_models:
        return {}

    selected_endpoint = (
        endpoint
        or os.getenv("JARVIS_OLLAMA_PRELOAD_ENDPOINT")
        or action_model_endpoint()
    )
    selected_keep_alive = (
        keep_alive
        or os.getenv("JARVIS_OLLAMA_PRELOAD_KEEP_ALIVE")
        or os.getenv("JARVIS_ACTION_OLLAMA_KEEP_ALIVE")
        or os.getenv("JARVIS_OLLAMA_KEEP_ALIVE")
        or "-1"
    )
    selected_timeout = timeout or _float_env("JARVIS_OLLAMA_PRELOAD_TIMEOUT_SECONDS", 120.0)
    post = post_json or post_json_request
    results: dict[str, bool] = {}

    for model in selected_models:
        payload = {
            "model": model,
            "prompt": " ",
            "stream": False,
            "keep_alive": selected_keep_alive,
        }
        loaded = False
        last_exc: Exception | None = None
        for url in _ollama_generate_urls(selected_endpoint):
            try:
                post(url, payload, timeout=selected_timeout)
                loaded = True
                logger.info(
                    "preloaded Ollama model=%s keep_alive=%s endpoint=%s",
                    model,
                    selected_keep_alive,
                    url,
                )
                break
            except Exception as exc:  # pragma: no cover - startup resilience
                last_exc = exc
        if not loaded and last_exc is not None:
            logger.warning("failed to preload Ollama model=%s: %s", model, last_exc)
        results[model] = loaded

    return results


def _dedupe_model_names(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        model = value.strip()
        if not model or model in seen:
            continue
        seen.add(model)
        result.append(model)
    return result


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid float env %s=%r", name, raw)
        return default
