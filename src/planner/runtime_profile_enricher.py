from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import socket
import threading
from typing import Any

from planner.action_model_client import (
    action_intent_model_name,
    action_model_endpoint,
    action_model_provider,
    complete_model_text,
)

logger = logging.getLogger("jarvis_controller.runtime_profile_enricher")

_CACHE_LOCK = threading.Lock()
_ENRICHMENT_CACHE: dict[str, list[dict[str, Any]]] = {}
_MAX_CACHE_ITEMS = 12

_BUILTIN_APP_METADATA: dict[str, dict[str, tuple[str, ...]]] = {
    "com.apple.stocks": {
        "aliases": ("주식", "주식앱", "증권", "Stocks", "stocks"),
        "capabilities": ("stock", "stocks", "finance", "market", "주식", "증권"),
        "categories": ("finance", "stocks"),
        "keywords": ("주식 시세", "증권", "시장"),
    },
    "com.apple.weather": {
        "aliases": ("날씨", "날씨앱", "Weather", "weather"),
        "capabilities": ("weather", "forecast", "날씨", "예보"),
        "categories": ("weather",),
        "keywords": ("오늘 날씨", "지역 날씨"),
    },
}


def enrich_runtime_profile_applications(profile: dict[str, Any]) -> dict[str, Any]:
    applications = profile.get("applications")
    if not isinstance(applications, list) or not applications:
        return profile

    enriched_profile = copy.deepcopy(profile)
    enriched_apps = [
        copy.deepcopy(app) if isinstance(app, dict) else app
        for app in applications
    ]
    enriched_profile["applications"] = enriched_apps

    for app in enriched_apps:
        if isinstance(app, dict):
            _apply_builtin_metadata(app)

    signature = _applications_signature(enriched_apps)
    cached = _cached_enrichment(signature)
    if cached is not None:
        enriched_profile["applications"] = cached
        _mark_enrichment_metadata(
            enriched_profile,
            signature=signature,
            source="cache",
            llm_attempted=False,
            llm_succeeded=True,
        )
        return enriched_profile

    llm_attempted = _llm_enrichment_enabled()
    llm_succeeded = False
    if llm_attempted:
        try:
            llm_succeeded = _apply_llm_metadata(enriched_apps)
        except Exception as exc:
            logger.warning("runtime profile app enrichment failed: %s", exc)

    _store_enrichment(signature, enriched_apps)
    _mark_enrichment_metadata(
        enriched_profile,
        signature=signature,
        source="llm" if llm_succeeded else "builtin",
        llm_attempted=llm_attempted,
        llm_succeeded=llm_succeeded,
    )
    return enriched_profile


def _llm_enrichment_enabled() -> bool:
    return os.getenv("JARVIS_RUNTIME_PROFILE_LLM_ENRICH_ENABLED", "1").strip() not in {
        "0",
        "false",
        "False",
        "no",
        "NO",
    }


def _apply_llm_metadata(applications: list[Any]) -> bool:
    candidates = [
        (index, app)
        for index, app in enumerate(applications[:_max_apps()])
        if isinstance(app, dict) and _string(app.get("name"))
    ]
    if not candidates:
        return False

    provider = action_model_provider()
    endpoint = action_model_endpoint()
    model = (
        os.getenv("JARVIS_RUNTIME_PROFILE_LLM_MODEL")
        or os.getenv("JARVIS_RUNTIME_PROFILE_LLM_MODEL_NAME")
        or action_intent_model_name()
    )
    timeout = _float_env("JARVIS_RUNTIME_PROFILE_LLM_TIMEOUT_SECONDS", 2.5)
    chunk_size = max(1, _int_env("JARVIS_RUNTIME_PROFILE_LLM_CHUNK_SIZE", 60))
    parsed_any = False

    for offset in range(0, len(candidates), chunk_size):
        chunk = candidates[offset : offset + chunk_size]
        payload = _llm_payload(chunk)
        try:
            content = complete_model_text(
                provider=provider,
                endpoint=endpoint,
                model=model,
                payload=payload,
                timeout=timeout,
            )
        except (TimeoutError, socket.timeout):
            raise
        except Exception as exc:
            logger.info("runtime profile app enrichment chunk skipped: %s", exc)
            continue

        for item in _parse_llm_items(content):
            index = item.get("index")
            if not isinstance(index, int) or not 0 <= index < len(applications):
                continue
            app = applications[index]
            if not isinstance(app, dict):
                continue
            parsed_any = True
            _merge_metadata_item(app, item)
    return parsed_any


def _llm_payload(chunk: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    apps = [
        {
            "index": index,
            "name": _string(app.get("name")),
            "display_name": _string(app.get("display_name")),
            "bundle_id": _string(app.get("bundle_id")),
            "aliases": _string_list(app.get("aliases"))[:6],
            "path": _string(app.get("path")),
            "kind": _string(app.get("kind")),
        }
        for index, app in chunk
    ]
    return {
        "think": False,
        "temperature": 0,
        "max_tokens": _int_env("JARVIS_RUNTIME_PROFILE_LLM_MAX_TOKENS", 1400),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You enrich local macOS application metadata for action routing. "
                    "Return only compact JSON. Do not add apps. Do not invent unrelated "
                    "capabilities. Include useful Korean aliases when obvious."
                ),
            },
            {
                "role": "user",
                "content": (
                    "For each app, return one flat JSON object: "
                    "{\"applications\":[{\"index\":0,\"aliases\":[],"
                    "\"capabilities\":[],\"categories\":[],\"keywords\":[]}]}.\n"
                    "Keep each list short, max 6 strings. Use exact app meaning. "
                    "Add Korean localized names and common Korean user terms when clear.\n"
                    f"Apps: {json.dumps(apps, ensure_ascii=False, separators=(',', ':'))}"
                ),
            },
        ],
    }


def _parse_llm_items(content: str) -> list[dict[str, Any]]:
    text = _strip_code_fence(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return _flatten_llm_items(data)


def _flatten_llm_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        nested = data.get("applications")
        if isinstance(nested, list):
            return _flatten_llm_items(nested)
        return [data] if isinstance(data.get("index"), int) else []
    if not isinstance(data, list):
        return []

    items: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("index"), int):
            items.append(item)
            continue
        nested = item.get("applications")
        if isinstance(nested, list):
            items.extend(_flatten_llm_items(nested))
    return items


def _strip_code_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _merge_metadata_item(app: dict[str, Any], item: dict[str, Any]) -> bool:
    changed = False
    for key in ("aliases", "capabilities", "categories", "keywords"):
        values = _string_list(item.get(key))
        if not values:
            continue
        before = _string_list(app.get(key))
        merged = list(dict.fromkeys([*before, *values]))[:12]
        if merged != before:
            app[key] = merged
            changed = True
    return changed


def _apply_builtin_metadata(app: dict[str, Any]) -> None:
    profile = _builtin_profile_for_app(app)
    if not profile:
        return
    for key, values in profile.items():
        before = _string_list(app.get(key))
        app[key] = list(dict.fromkeys([*before, *values]))[:12]


def _builtin_profile_for_app(app: dict[str, Any]) -> dict[str, tuple[str, ...]] | None:
    identity = {
        _match_key(value)
        for key in ("bundle_id", "name", "display_name", "executable")
        for value in [app.get(key)]
        if isinstance(value, str) and value.strip()
    }
    for bundle_id, profile in _BUILTIN_APP_METADATA.items():
        aliases = profile.get("aliases", ())
        keys = {_match_key(bundle_id), *(_match_key(alias) for alias in aliases)}
        if identity.intersection(keys):
            return profile
    return None


def _applications_signature(applications: list[Any]) -> str:
    compact = [
        {
            "name": _string(app.get("name")),
            "display_name": _string(app.get("display_name")),
            "bundle_id": _string(app.get("bundle_id")),
            "aliases": _string_list(app.get("aliases")),
            "path": _string(app.get("path")),
            "kind": _string(app.get("kind")),
        }
        for app in applications
        if isinstance(app, dict)
    ]
    payload = json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cached_enrichment(signature: str) -> list[dict[str, Any]] | None:
    with _CACHE_LOCK:
        cached = _ENRICHMENT_CACHE.get(signature)
        return copy.deepcopy(cached) if cached is not None else None


def _store_enrichment(signature: str, applications: list[Any]) -> None:
    apps = [app for app in applications if isinstance(app, dict)]
    with _CACHE_LOCK:
        if len(_ENRICHMENT_CACHE) >= _MAX_CACHE_ITEMS:
            first_key = next(iter(_ENRICHMENT_CACHE))
            _ENRICHMENT_CACHE.pop(first_key, None)
        _ENRICHMENT_CACHE[signature] = copy.deepcopy(apps)


def _mark_enrichment_metadata(
    profile: dict[str, Any],
    *,
    signature: str,
    source: str,
    llm_attempted: bool,
    llm_succeeded: bool,
) -> None:
    metadata = profile.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    else:
        metadata = dict(metadata)
    metadata["app_enrichment"] = {
        "signature": signature,
        "source": source,
        "llm_attempted": llm_attempted,
        "llm_succeeded": llm_succeeded,
    }
    profile["metadata"] = metadata


def _max_apps() -> int:
    return max(1, _int_env("JARVIS_RUNTIME_PROFILE_LLM_MAX_APPS", 240))


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return list(dict.fromkeys(result))


def _match_key(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())
