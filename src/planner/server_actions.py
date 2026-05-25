from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from jarvis_contracts import ClientAction, ClientActionResult


def execute_server_action(
    *,
    core_client: Any,
    user_id: str,
    request_id: str,
    action_id: str,
    action: ClientAction,
) -> ClientActionResult | None:
    if action.type != "todo":
        return None
    try:
        output = _execute_todo_action(core_client=core_client, user_id=user_id, action=action)
    except Exception as exc:
        return ClientActionResult(
            action_id=action_id,
            request_id=request_id,
            status="failed",
            error=str(exc),
            output={"source": "server_todo", "command": action.command},
        )
    return ClientActionResult(
        action_id=action_id,
        request_id=request_id,
        status="completed",
        output=output,
    )


def _execute_todo_action(
    *,
    core_client: Any,
    user_id: str,
    action: ClientAction,
) -> dict[str, object]:
    args = action.args if isinstance(action.args, dict) else {}
    command = action.command or "create"
    if command == "create":
        title = _string_arg(args, "title") or action.target or action.payload
        if not title:
            raise ValueError("todo.create requires title")
        body = _todo_body_from_args(args)
        body["title"] = title
        result = core_client.create_todo(user_id=user_id, body=body)
        return _todo_output(command=command, result=result)
    if command == "list":
        result = core_client.list_todos(
            user_id=user_id,
            status=_string_arg(args, "status"),
            include_deleted=bool(args.get("include_deleted", False)),
            limit=_int_arg(args, "limit", default=50),
        )
        result = _filter_todo_list_result(result, args)
        return _todo_output(command=command, result=result)
    if command == "update":
        todo_id = _todo_id(action, args)
        if not todo_id:
            raise ValueError("todo.update requires todo_id")
        body = _todo_body_from_args(args, include_status=True)
        result = core_client.update_todo(user_id=user_id, todo_id=todo_id, body=body)
        return _todo_output(command=command, result=result, todo_id=todo_id)
    if command == "delete":
        todo_id = _todo_id(action, args)
        if not todo_id:
            todo_id = _resolve_todo_delete_match(
                core_client=core_client,
                user_id=user_id,
                args=args,
            )
        result = core_client.delete_todo(user_id=user_id, todo_id=todo_id)
        return _todo_output(command=command, result=result, todo_id=todo_id)
    raise ValueError(f"unsupported todo command: {command}")


def _todo_body_from_args(
    args: dict[str, Any],
    *,
    include_status: bool = False,
) -> dict[str, object]:
    fields = {
        "description",
        "priority",
        "due_at",
        "remind_at",
        "timezone",
        "calendar_provider",
        "calendar_id",
        "calendar_event_id",
        "chat_id",
        "source_message_id",
        "metadata",
    }
    if include_status:
        fields.add("status")
        fields.add("calendar_sync_status")
        fields.add("title")
    return {
        key: value
        for key, value in args.items()
        if key in fields and value is not None
    }


def _todo_output(
    *,
    command: str,
    result: Any,
    todo_id: str | None = None,
) -> dict[str, object]:
    output: dict[str, object] = {
        "source": "server_todo",
        "command": command,
    }
    if isinstance(result, dict):
        output["result"] = result
        resolved_id = result.get("id") or todo_id
        if isinstance(resolved_id, str):
            output["todo_id"] = resolved_id
        title = result.get("title")
        if isinstance(title, str):
            output["title"] = title
        due_at = result.get("due_at")
        if isinstance(due_at, str):
            output["due_at"] = due_at
    else:
        output["result"] = result
        if todo_id:
            output["todo_id"] = todo_id
    return output


def _todo_id(action: ClientAction, args: dict[str, Any]) -> str | None:
    return _string_arg(args, "todo_id") or action.target or action.payload


def _filter_todo_list_result(result: Any, args: dict[str, Any]) -> Any:
    if _string_arg(args, "date_scope") != "today":
        return result
    if not isinstance(result, dict):
        return result
    items = result.get("items")
    if not isinstance(items, list):
        return result

    timezone_name = _string_arg(args, "timezone") or "Asia/Seoul"
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("Asia/Seoul")
    today = datetime.now(timezone).date()
    filtered = [
        item
        for item in items
        if isinstance(item, dict)
        and _todo_due_date(item, timezone=timezone) == today
    ]
    return {
        **result,
        "items": filtered,
        "date_scope": "today",
        "timezone": timezone.key,
    }


def _resolve_todo_delete_match(
    *,
    core_client: Any,
    user_id: str,
    args: dict[str, Any],
) -> str:
    result = core_client.list_todos(
        user_id=user_id,
        status=_string_arg(args, "status") or "open",
        include_deleted=bool(args.get("include_deleted", False)),
        limit=_int_arg(args, "limit", default=100),
    )
    result = _filter_todo_list_result(result, args)
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise ValueError("todo.delete could not inspect todo list")

    query_terms = _query_terms(_string_arg(args, "query"))
    due_hours = _int_list_arg(args, "due_hours")
    timezone = _timezone_from_args(args)
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and _todo_matches_query(item, query_terms)
        and _todo_matches_hours(item, due_hours, timezone=timezone)
    ]
    if not matches:
        raise ValueError("삭제할 할 일을 찾지 못했습니다.")
    if len(matches) > 1:
        raise ValueError("삭제할 할 일이 여러 개입니다. 더 구체적으로 말해주세요.")
    todo_id = matches[0].get("id")
    if not isinstance(todo_id, str) or not todo_id.strip():
        raise ValueError("matched todo has no id")
    return todo_id.strip()


def _query_terms(query: str | None) -> list[str]:
    if not query:
        return []
    return [
        term.casefold()
        for term in query.replace("/", " ").split()
        if term.strip()
    ]


def _todo_matches_query(item: dict[str, Any], query_terms: list[str]) -> bool:
    if not query_terms:
        return True
    haystack = " ".join(
        str(item.get(key) or "")
        for key in ("title", "description")
    ).casefold()
    return all(term in haystack for term in query_terms)


def _todo_matches_hours(
    item: dict[str, Any],
    due_hours: list[int],
    *,
    timezone: ZoneInfo,
) -> bool:
    if not due_hours:
        return True
    due_at = item.get("due_at")
    if isinstance(due_at, str):
        value = due_at.strip()
        if value.endswith("Z"):
            value = f"{value[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone)
            if parsed.astimezone(timezone).hour in due_hours:
                return True
    text = " ".join(str(item.get(key) or "") for key in ("title", "description"))
    return any(f"{hour}시" in text or f"{hour % 12 or 12}시" in text for hour in due_hours)


def _todo_due_date(item: dict[str, Any], *, timezone: ZoneInfo):
    due_at = item.get("due_at")
    if not isinstance(due_at, str) or not due_at.strip():
        return None
    value = due_at.strip()
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone).date()


def _timezone_from_args(args: dict[str, Any]) -> ZoneInfo:
    timezone_name = _string_arg(args, "timezone") or "Asia/Seoul"
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("Asia/Seoul")


def _string_arg(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _int_arg(args: dict[str, Any], key: str, *, default: int) -> int:
    value = args.get(key)
    if isinstance(value, int):
        return value
    return default


def _int_list_arg(args: dict[str, Any], key: str) -> list[int]:
    value = args.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int)]
