from planner.action_model_client import _ollama_chat_payload
from planner.ollama_preload import ollama_preload_models, preload_ollama_models


def test_ollama_preload_defaults_to_runtime_model(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_OLLAMA_PRELOAD_MODELS", raising=False)
    monkeypatch.setenv("JARVIS_RUNTIME_PROFILE_LLM_MODEL", "gemma4:e2b")
    monkeypatch.setenv("JARVIS_OLLAMA_TTS_MODEL", "tts-model:latest")

    assert ollama_preload_models() == ["gemma4:e2b"]


def test_ollama_preload_posts_keep_alive_minus_one(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_post(url: str, payload: dict[str, object], *, timeout: float):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        return {"done": True}

    result = preload_ollama_models(
        models=["gemma4:e2b", "tts"],
        endpoint="http://ollama.example.com/chat",
        keep_alive="-1",
        timeout=3.0,
        post_json=fake_post,
    )

    assert result == {"gemma4:e2b": True, "tts": True}
    assert [call["url"] for call in calls] == [
        "http://ollama.example.com/api/generate",
        "http://ollama.example.com/api/generate",
    ]
    assert [call["payload"] for call in calls] == [
        {"model": "gemma4:e2b", "stream": False, "keep_alive": "-1"},
        {"model": "tts", "stream": False, "keep_alive": "-1"},
    ]
    assert all(call["timeout"] == 3.0 for call in calls)


def test_ollama_action_payload_uses_generic_keep_alive(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_ACTION_OLLAMA_KEEP_ALIVE", raising=False)
    monkeypatch.setenv("JARVIS_OLLAMA_KEEP_ALIVE", "5m")

    payload = _ollama_chat_payload(
        model="gemma4:e2b",
        payload={"messages": [], "temperature": 0, "max_tokens": 16},
    )

    assert payload["keep_alive"] == "5m"
