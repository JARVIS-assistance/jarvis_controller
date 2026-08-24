from jarvis_contracts import ClientAction

from planner.server_actions import execute_server_action


class StubCoreClient:
    def __init__(self, *, response=None, error=None):
        self.response = response or {"description": "A game HUD with 100 HP.", "model": "qwen2.5vl:3b"}
        self.error = error
        self.calls = []

    def describe_vision_frame(self, *, user_id, image_base64, prompt=None):
        self.calls.append({"user_id": user_id, "image_base64": image_base64, "prompt": prompt})
        if self.error:
            raise self.error
        return self.response


def _describe_action(prompt: str | None = None) -> ClientAction:
    return ClientAction(
        type="screen_stream",
        command="describe",
        args={"prompt": prompt} if prompt else {},
        description="Describe the current screen",
        requires_confirm=False,
    )


def test_screen_describe_uses_latest_cached_frame():
    core_client = StubCoreClient()
    frames = {"u1": {"frame_base64": "ZmFrZQ==", "captured_at": "t1", "sequence": 3}}

    result = execute_server_action(
        core_client=core_client,
        user_id="u1",
        request_id="req1",
        action_id="act1",
        action=_describe_action(),
        get_latest_vision_frame=frames.get,
    )

    assert result is not None
    assert result.status == "completed"
    assert result.output["description"] == "A game HUD with 100 HP."
    assert result.output["frame_sequence"] == 3
    assert core_client.calls[0]["image_base64"] == "ZmFrZQ=="
    assert core_client.calls[0]["prompt"] is None


def test_screen_describe_forwards_custom_prompt():
    core_client = StubCoreClient()
    frames = {"u1": {"frame_base64": "ZmFrZQ=="}}

    execute_server_action(
        core_client=core_client,
        user_id="u1",
        request_id="req1",
        action_id="act1",
        action=_describe_action(prompt="What color is the health bar?"),
        get_latest_vision_frame=frames.get,
    )

    assert core_client.calls[0]["prompt"] == "What color is the health bar?"


def test_screen_describe_fails_without_a_streamed_frame():
    core_client = StubCoreClient()

    result = execute_server_action(
        core_client=core_client,
        user_id="u1",
        request_id="req1",
        action_id="act1",
        action=_describe_action(),
        get_latest_vision_frame=lambda user_id: None,
    )

    assert result is not None
    assert result.status == "failed"
    assert "start screen_stream" in result.error
    assert core_client.calls == []


def test_screen_describe_without_frame_lookup_configured_fails():
    core_client = StubCoreClient()

    result = execute_server_action(
        core_client=core_client,
        user_id="u1",
        request_id="req1",
        action_id="act1",
        action=_describe_action(),
    )

    assert result is not None
    assert result.status == "failed"


def test_non_todo_non_screen_describe_actions_are_not_handled_server_side():
    result = execute_server_action(
        core_client=StubCoreClient(),
        user_id="u1",
        request_id="req1",
        action_id="act1",
        action=ClientAction(
            type="screen_stream",
            command="start",
            args={},
            description="start",
            requires_confirm=False,
        ),
    )

    assert result is None
