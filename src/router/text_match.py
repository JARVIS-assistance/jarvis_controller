from __future__ import annotations

import re


def _normalized_action_match_key(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().casefold())
