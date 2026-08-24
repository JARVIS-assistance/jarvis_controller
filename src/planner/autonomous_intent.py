"""Narrow natural-language trigger for the background autonomous loop.

Deliberately isolated from router.py's much larger heuristic pile (see
intent_todo.py for the same pattern) — this only recognizes a handful of
unambiguous "keep watching/doing this until X" phrasings. Anything less
explicit falls through to the normal conversation flow; false negatives are
fine here, false positives (silently launching a background loop the user
didn't ask for) are not.
"""

from __future__ import annotations

_TRIGGER_PHRASES = (
    "계속 지켜보면서",
    "계속 지켜보다가",
    "화면 지켜보면서",
    "다 될 때까지",
    "끝날 때까지 계속",
    "대신 플레이해",
    "대신 플레이 해",
    "대신 깨줘",
    "watch and keep",
    "keep watching and",
    "keep playing until",
    "play this for me until",
)


def autonomous_loop_requested(message: str) -> str | None:
    """Return the goal text (the message itself) if it clearly asks for a
    background observe-act loop, else None.
    """
    if not message or not message.strip():
        return None
    folded = message.casefold()
    if any(phrase in folded for phrase in _TRIGGER_PHRASES):
        return message.strip()
    return None
