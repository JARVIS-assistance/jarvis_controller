"""Korean/English NLU heuristics for todo-related client actions.

Extracted out of router.py, which had grown to mix route handlers with
dozens of these detection helpers in a single 5000+ line file.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis_contracts import ClientAction

from .text_match import _normalized_action_match_key


def _todo_create_action_from_message(message: str) -> ClientAction | None:
    if not _todo_create_requested(message):
        return None
    title, due_at = _todo_title_and_due_at(message)
    if not title:
        return None
    args: dict[str, object] = {
        "title": title,
        "timezone": "Asia/Seoul",
        "metadata": {"source": "conversation_action_template"},
    }
    if due_at is not None:
        args["due_at"] = due_at.isoformat()
    return ClientAction(
        type="todo",
        command="create",
        target=None,
        payload=title,
        args=args,
        description=f"Create todo: {title}",
        requires_confirm=False,
    )


def _todo_list_action_from_message(message: str) -> ClientAction | None:
    free_time_requested = _free_time_check_requested(message)
    if not free_time_requested and not _todo_list_requested(message):
        return None
    folded = message.casefold()
    status = "open"
    if any(term in folded for term in ("완료", "끝낸", "completed", "done")):
        status = "completed"
    args: dict[str, object] = {
        "status": status,
        "include_deleted": False,
        "limit": 50,
        "metadata": {"source": "conversation_action_template"},
    }
    if "오늘" in folded or "today" in folded or free_time_requested:
        args["date_scope"] = "today"
    if free_time_requested:
        args["summary_mode"] = "free_time"
    return ClientAction(
        type="todo",
        command="list",
        target=None,
        payload=None,
        args=args,
        description="List todos",
        requires_confirm=False,
    )


def _todo_delete_action_from_message(message: str) -> ClientAction | None:
    if not _todo_delete_requested(message):
        return None
    query = _todo_delete_query(message)
    args: dict[str, object] = {
        "status": "open",
        "include_deleted": False,
        "limit": 100,
        "metadata": {"source": "conversation_action_template"},
    }
    if query:
        args["query"] = query
    folded = message.casefold()
    if "오늘" in folded or "today" in folded:
        args["date_scope"] = "today"
    due_hours = _todo_due_hour_candidates(message)
    if due_hours:
        args["due_hours"] = due_hours
    return ClientAction(
        type="todo",
        command="delete",
        target=None,
        payload=None,
        args=args,
        description=f"Delete todo: {query}" if query else "Delete todo",
        requires_confirm=False,
    )


def _todo_create_requested(message: str) -> bool:
    folded = message.casefold()
    message_key = _normalized_action_match_key(message)
    has_todo_object = any(
        term in message_key
        for term in (
            "할일",
            "todo",
            "to-do",
            "해야할일",
            "할것",
            "회의",
            "미팅",
            "약속",
            "일정",
        )
    ) or any(term in folded for term in ("task list", "to do list"))
    has_create_verb = any(
        term in folded
        for term in (
            "추가",
            "등록",
            "넣어",
            "만들어",
            "add",
            "create",
            "put",
        )
    )
    return has_todo_object and has_create_verb


def _todo_list_requested(message: str) -> bool:
    folded = message.casefold()
    message_key = _normalized_action_match_key(message)
    has_todo_object = any(
        term in message_key
        for term in ("할일", "todo", "to-do", "해야할일", "할것")
    ) or any(term in folded for term in ("task list", "to do list"))
    has_date_scope = any(
        term in folded for term in ("오늘", "내일", "모레", "today", "tomorrow")
    )
    has_list_verb = any(
        term in folded
        for term in (
            "남은",
            "뭐남",
            "뭐 남",
            "뭐 있",
            "무엇",
            "목록",
            "리스트",
            "리스트업",
            "보여",
            "알려",
            "말해",
            "확인",
            "조회",
            "list",
            "show",
            "tell",
            "remaining",
            "left",
        )
    )
    return has_todo_object and (has_list_verb or has_date_scope)


def _free_time_check_requested(message: str) -> bool:
    folded = message.casefold()
    message_key = _normalized_action_match_key(message)
    has_free_time = any(
        term in message_key
        for term in (
            "빈시간",
            "비는시간",
            "남는시간",
            "가능한시간",
            "시간비어",
            "시간비는",
        )
    ) or any(term in folded for term in ("free time", "available time", "availability"))
    has_check_verb = any(
        term in folded
        for term in (
            "체크",
            "확인",
            "알려",
            "봐",
            "찾아",
            "check",
            "show",
            "find",
        )
    )
    return has_free_time and has_check_verb


def _todo_delete_requested(message: str) -> bool:
    folded = message.casefold()
    message_key = _normalized_action_match_key(message)
    has_delete_verb = any(
        term in folded
        for term in (
            "삭제",
            "지워",
            "제거",
            "없애",
            "빼",
            "빼줘",
            "빼기",
            "delete",
            "remove",
        )
    )
    if not has_delete_verb:
        return False
    has_todo_object = any(
        term in message_key
        for term in (
            "할일",
            "todo",
            "to-do",
            "해야할일",
            "할것",
            "회의",
            "미팅",
            "약속",
            "일정",
        )
    )
    return has_todo_object or _extract_todo_due_at(message)[1] is not None


def _todo_delete_query(message: str) -> str | None:
    query = re.sub(
        r"(?:삭제|지워|제거|없애|빼|빼줘|빼기|delete|remove)"
        r"(?:\s*(?:해|해줘|해줄래|해\s*줄래|해주세요|줘|줄래|부탁해))?"
        r"\s*\??\s*$",
        "",
        message,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\b(?:today)\b", " ", query, flags=re.IGNORECASE)
    query = re.sub(r"(?:오늘|내일|모레)", " ", query)
    query = re.sub(
        r"(?:오전|오후)?\s*\d{1,2}\s*시\s*(?:\d{1,2}\s*분?)?",
        " ",
        query,
    )
    query = re.sub(
        r"(?:할\s*일|해야\s*할\s*일|할것|todo|to-do)(?:\s*(?:목록|리스트))?",
        " ",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\b(?:에서|에|으로|로)\b", " ", query)
    query = re.sub(r"\s+", " ", query).strip(" \t\r\n.。!?")
    if len(query) > 200:
        query = query[:200].rstrip()
    return query or None


def _todo_due_hour_candidates(message: str) -> list[int]:
    match = re.search(
        r"(?P<ampm>오전|오후|am|pm)?\s*(?P<hour>\d{1,2})\s*시",
        message,
        flags=re.IGNORECASE,
    )
    if match is None:
        return []
    hour = int(match.group("hour"))
    if hour > 23:
        return []
    ampm = (match.group("ampm") or "").casefold()
    if ampm in {"오후", "pm"} and hour < 12:
        return [hour + 12]
    if ampm in {"오전", "am"}:
        return [0 if hour == 12 else hour]
    if 1 <= hour <= 11:
        return [hour, hour + 12]
    return [hour]


def _todo_title_and_due_at(message: str) -> tuple[str | None, datetime | None]:
    due_match, due_at = _extract_todo_due_at(message)
    title = message
    if due_match is not None:
        title = f"{message[: due_match.start()]} {message[due_match.end() :]}"
    title = _strip_todo_time_phrase(title)
    title = re.sub(
        r"^\s*(?:할\s*일|해야\s*할\s*일|할것|todo|to-do)"
        r"(?:\s*(?:목록|리스트))?"
        r"(?:에|으로)?\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\s*(?:까지|전까지|by)?\s*"
        r"(?:할\s*일|해야\s*할\s*일|할것|todo|to-do)"
        r"(?:\s*(?:목록|리스트))?"
        r"(?:에|으로)?\s*"
        r"(?:추가|등록|넣어|만들어|add|create)"
        r"(?:\s*(?:해|해줘|해줄래|해\s*줄래|해주세요|줘|줄래|부탁해))?"
        r"\s*\??\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\s*(?:추가|등록|넣어|만들어|add|create)"
        r"(?:\s*(?:해|해줘|해줄래|해\s*줄래|해주세요|줘|줄래|부탁해))?"
        r"\s*\??\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(
        r"\s*(?:까지|전까지)\s*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    title = re.sub(r"\s+", " ", title).strip(" \t\r\n.。!?")
    if len(title) > 200:
        title = title[:200].rstrip()
    return (title or None), due_at


def _strip_todo_time_phrase(message: str) -> str:
    return re.sub(
        r"(?:오전|오후|am|pm)?\s*\d{1,2}\s*(?:시|:)"
        r"\s*(?:\d{1,2}\s*(?:분)?)?\s*(?:에)?",
        " ",
        message,
        flags=re.IGNORECASE,
    )


def _extract_todo_due_at(message: str) -> tuple[re.Match[str] | None, datetime | None]:
    date_patterns = (
        r"(?P<month>\d{1,2})\s*[/-]\s*(?P<day>\d{1,2})"
        r"(?:\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?)?",
        r"(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일"
        r"(?:\s*(?P<hour>\d{1,2})\s*(?:시|:)"
        r"\s*(?P<minute>\d{1,2})?\s*(?:분)?)?",
    )
    for pattern in date_patterns:
        match = re.search(pattern, message)
        if match is not None:
            return match, _todo_datetime_from_match(match)

    relative_patterns = {
        "오늘": 0,
        "내일": 1,
        "모레": 2,
    }
    for word, days in relative_patterns.items():
        match = re.search(word, message)
        if match is not None:
            now = datetime.now(ZoneInfo("Asia/Seoul"))
            hour, minute = _todo_time_from_message(message, default_hour=23, default_minute=59)
            due = (now + timedelta(days=days)).replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            return match, due

    time_match = re.search(
        r"(?P<ampm>오전|오후|am|pm)?\s*(?P<hour>\d{1,2})\s*(?:시|:)"
        r"\s*(?P<minute>\d{1,2})?\s*(?:분)?\s*(?:에)?",
        message,
        flags=re.IGNORECASE,
    )
    if time_match is not None:
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        hour, minute = _todo_time_from_message(message, default_hour=23, default_minute=59)
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        return time_match, due
    return None, None


def _todo_time_from_message(
    message: str,
    *,
    default_hour: int,
    default_minute: int,
) -> tuple[int, int]:
    match = re.search(
        r"(?P<ampm>오전|오후|am|pm)?\s*(?P<hour>\d{1,2})\s*(?:시|:)"
        r"\s*(?P<minute>\d{1,2})?\s*(?:분)?",
        message,
        flags=re.IGNORECASE,
    )
    if match is None:
        return default_hour, default_minute
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if hour > 23 or minute > 59:
        return default_hour, default_minute
    ampm = (match.group("ampm") or "").casefold()
    if ampm in {"오후", "pm"} and hour < 12:
        hour += 12
    elif ampm in {"오전", "am"} and hour == 12:
        hour = 0
    return hour, minute


def _todo_datetime_from_match(match: re.Match[str]) -> datetime:
    zone = ZoneInfo("Asia/Seoul")
    now = datetime.now(zone)
    month = int(match.group("month"))
    day = int(match.group("day"))
    hour = int(match.group("hour") or 23)
    minute = int(match.group("minute") or 59)
    due = datetime(now.year, month, day, hour, minute, tzinfo=zone)
    if due < now:
        due = due.replace(year=now.year + 1)
    return due
