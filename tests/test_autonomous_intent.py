from planner.autonomous_intent import autonomous_loop_requested


def test_detects_korean_watch_and_keep_going_phrase():
    message = "이 게임 계속 지켜보면서 대신 플레이해줘, 다 깨면 알려줘"
    assert autonomous_loop_requested(message) == message


def test_detects_english_watch_and_keep_phrase():
    message = "watch and keep playing until you win"
    assert autonomous_loop_requested(message) == message


def test_ordinary_message_is_not_detected():
    assert autonomous_loop_requested("오늘 날씨 어때?") is None
    assert autonomous_loop_requested("크롬 열어줘") is None


def test_empty_message_is_not_detected():
    assert autonomous_loop_requested("") is None
    assert autonomous_loop_requested("   ") is None
