from __future__ import annotations

import sys
from pathlib import Path


def _ensure_core_path() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    core_src = repo_root / "jarvis-core" / "src"
    core_src_str = str(core_src)
    if core_src.exists() and core_src_str not in sys.path:
        sys.path.insert(0, core_src_str)


_ensure_core_path()

from jarvis_core import CoreResponse, run_deep_thinking, run_realtime_conversation

__all__ = ["CoreResponse", "run_deep_thinking", "run_realtime_conversation"]
