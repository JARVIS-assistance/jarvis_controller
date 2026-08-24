from __future__ import annotations

import json
import os
from collections.abc import Generator
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from jarvis_contracts import ClientAction

from planner.dom_link_resolver import (
    resolve_input_from_dom_output,
    resolve_link_from_dom_output,
)


def sse_event(event: str, payload: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode(
        "utf-8"
    )


def format_action_context(
    *,
    action: ClientAction,
    status: str,
    output: dict[str, object],
    error: str | None,
) -> str:
    return (
        f"[클라이언트 실행: {action.type}/{action.command or ''} ({status})]\n"
        f"설명: {action.description}\n"
        f"결과: {json.dumps(output, ensure_ascii=False)}\n"
        f"오류: {error or ''}"
    )


def _action_plan_step_payload(
    action: ClientAction,
    *,
    action_id: str,
    status: str,
    request_id: str,
) -> dict[str, object]:
    title = action.description.strip() if action.description else ""
    if not title:
        title = f"{action.type}/{action.command}" if action.command else action.type
    return {
        "id": action_id,
        "title": title[:120],
        "description": action.description or f"{action.type} 액션 실행",
        "status": status,
        "request_id": request_id,
    }


def _normalize_plan_step_status(action_status: str) -> str:
    if action_status in {"completed", "failed", "rejected", "timeout", "invalid"}:
        return action_status
    return "failed"


def action_result_payload(
    envelope: Any,
    action_result: Any,
    action: ClientAction,
) -> dict[str, object]:
    return {
        "action_id": envelope.action_id,
        "request_id": envelope.request_id,
        "status": action_result.status,
        "output": action_result.output,
        "error": action_result.error,
        "action": action.model_dump(),
    }


def action_completion_message(
    action_results: list[dict[str, object]],
    *,
    success_content: str,
    success_summary: str,
) -> tuple[str, str]:
    if not action_results:
        return "실행할 클라이언트 액션이 없습니다.", "no client actions dispatched"

    statuses = [str(item.get("status") or "") for item in action_results]
    completed_count = len([status for status in statuses if status == "completed"])
    if completed_count == len(action_results):
        todo_list_content = _todo_list_completion_content(action_results)
        if todo_list_content:
            return todo_list_content, "server todo list completed"
        return success_content, success_summary

    first_error = _first_action_error(action_results)
    non_completed = [status for status in statuses if status != "completed"]

    if completed_count > 0:
        return (
            f"일부 작업만 실행했습니다. "
            f"{completed_count}/{len(action_results)}개 완료, "
            f"첫 오류: {first_error}",
            (
                "client action partially completed "
                f"({completed_count}/{len(action_results)} completed)"
            ),
        )

    if all(status == "timeout" for status in non_completed):
        return (
            f"클라이언트 액션 결과 대기 시간이 초과되었습니다. {first_error}",
            "client action timed out",
        )
    if all(status == "rejected" for status in non_completed):
        return (
            f"사용자가 클라이언트 액션 실행을 거부했습니다. {first_error}",
            "client action rejected",
        )
    if all(status == "failed" for status in non_completed):
        return (
            f"클라이언트 액션 실행에 실패했습니다. {first_error}",
            "client action failed",
        )
    return (
        f"클라이언트 액션을 실행하지 못했습니다. {first_error}",
        f"client action did not complete ({','.join(non_completed)})",
    )


def _first_action_error(action_results: list[dict[str, object]]) -> str:
    for item in action_results:
        error = item.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    for item in action_results:
        output = item.get("output")
        if isinstance(output, dict):
            message = output.get("message") or output.get("error")
            if isinstance(message, str) and message.strip():
                return message.strip()
    return "상세 오류가 전달되지 않았습니다."


def _todo_list_completion_content(
    action_results: list[dict[str, object]],
) -> str | None:
    if len(action_results) != 1:
        return None
    action_result = action_results[0]
    action = action_result.get("action")
    output = action_result.get("output")
    if not isinstance(action, dict) or not isinstance(output, dict):
        return None
    if action.get("type") != "todo" or action.get("command") != "list":
        return None
    if output.get("source") != "server_todo":
        return None
    result = output.get("result")
    if not isinstance(result, dict):
        return None
    action_args = action.get("args")
    if isinstance(action_args, dict) and action_args.get("summary_mode") == "free_time":
        return _todo_free_time_completion_content(result, action_args)
    items = result.get("items")
    if not isinstance(items, list):
        return None
    if not items:
        return "남은 할 일이 없습니다."
    lines = ["남은 할 일입니다."]
    default_timezone = _todo_list_timezone(result, action_args)
    for index, item in enumerate(items[:20], start=1):
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            title = str(item.get("id") or f"할 일 {index}")
        suffix = _todo_due_at_suffix(item, default_timezone=default_timezone)
        lines.append(f"{index}. {title}{suffix}")
    if len(items) > 20:
        lines.append(f"외 {len(items) - 20}개가 더 있습니다.")
    return "\n".join(lines)


def _todo_due_at_suffix(
    item: dict[str, object],
    *,
    default_timezone: ZoneInfo,
) -> str:
    due_at = item.get("due_at")
    if not isinstance(due_at, str) or not due_at.strip():
        return ""
    timezone = _todo_item_timezone(item, default_timezone=default_timezone)
    formatted = _format_todo_due_at(due_at, timezone=timezone)
    return f" - {formatted or due_at.strip()}"


def _format_todo_due_at(value: str, *, timezone: ZoneInfo) -> str | None:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    local = parsed.astimezone(timezone)
    today = datetime.now(timezone).date()
    if local.date() == today:
        return f"오늘 {local:%H:%M}"
    if local.date() == today + timedelta(days=1):
        return f"내일 {local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return f"어제 {local:%H:%M}"
    if local.year == today.year:
        return f"{local.month}월 {local.day}일 {local:%H:%M}"
    return f"{local:%Y-%m-%d %H:%M}"


def _todo_list_timezone(
    result: dict[str, object],
    action_args: object,
) -> ZoneInfo:
    timezone = _zoneinfo_from_value(result.get("timezone"))
    if timezone is not None:
        return timezone
    if isinstance(action_args, dict):
        timezone = _zoneinfo_from_value(action_args.get("timezone"))
        if timezone is not None:
            return timezone
    return ZoneInfo("Asia/Seoul")


def _todo_item_timezone(
    item: dict[str, object],
    *,
    default_timezone: ZoneInfo,
) -> ZoneInfo:
    return _zoneinfo_from_value(item.get("timezone")) or default_timezone


def _zoneinfo_from_value(value: object) -> ZoneInfo | None:
    if isinstance(value, str) and value.strip():
        try:
            return ZoneInfo(value.strip())
        except Exception:
            return None
    return None


def _todo_free_time_completion_content(
    result: dict[str, object],
    args: dict[str, object],
) -> str:
    items = result.get("items")
    if not isinstance(items, list):
        return "할 일 목록을 확인했지만 빈 시간을 계산할 수 없습니다."
    timezone = _timezone_from_args(args)
    intervals = sorted(
        interval
        for item in items
        if isinstance(item, dict)
        for interval in [_todo_busy_interval(item, timezone=timezone)]
        if interval is not None
    )
    merged = _merge_intervals(intervals)
    day_start = datetime.combine(datetime.now(timezone).date(), time(9, 0), timezone)
    day_end = datetime.combine(datetime.now(timezone).date(), time(18, 0), timezone)
    free_slots = _free_intervals(merged, day_start=day_start, day_end=day_end)
    if not intervals:
        return "오늘 등록된 시간 지정 할 일이 없습니다. 09:00-18:00 전체가 비어 있습니다."
    if not free_slots:
        return "오늘 할 일 목록을 확인했습니다. 09:00-18:00 사이에 뚜렷한 빈 시간이 없습니다."
    lines = ["오늘 할 일 목록 기준 빈 시간입니다."]
    for start, end in free_slots:
        lines.append(f"- {start:%H:%M}-{end:%H:%M}")
    return "\n".join(lines)


def _todo_busy_interval(
    item: dict[str, object],
    *,
    timezone: ZoneInfo,
) -> tuple[datetime, datetime] | None:
    due_at = item.get("due_at")
    if not isinstance(due_at, str) or not due_at.strip():
        return None
    value = due_at.strip()
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    try:
        end = datetime.fromisoformat(value)
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone)
    start = end.astimezone(timezone)
    duration_minutes = _duration_minutes(item)
    end = start + timedelta(minutes=duration_minutes)
    return start, end


def _duration_minutes(item: dict[str, object]) -> int:
    value = item.get("duration_minutes")
    if isinstance(value, int) and value > 0:
        return min(value, 24 * 60)
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        metadata_value = metadata.get("duration_minutes")
        if isinstance(metadata_value, int) and metadata_value > 0:
            return min(metadata_value, 24 * 60)
    return 60


def _merge_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        if end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


def _free_intervals(
    busy: list[tuple[datetime, datetime]],
    *,
    day_start: datetime,
    day_end: datetime,
) -> list[tuple[datetime, datetime]]:
    free: list[tuple[datetime, datetime]] = []
    cursor = day_start
    for start, end in busy:
        clipped_start = max(start, day_start)
        clipped_end = min(end, day_end)
        if clipped_end <= day_start or clipped_start >= day_end:
            continue
        if clipped_start > cursor:
            free.append((cursor, clipped_start))
        if clipped_end > cursor:
            cursor = clipped_end
    if cursor < day_end:
        free.append((cursor, day_end))
    return free


def _timezone_from_args(args: dict[str, object]) -> ZoneInfo:
    value = args.get("timezone")
    if isinstance(value, str) and value.strip():
        try:
            return ZoneInfo(value.strip())
        except Exception:
            pass
    return ZoneInfo("Asia/Seoul")


def stream_dispatched_actions(
    *,
    actions: list[ClientAction],
    request_id: str,
    user_id: str,
    action_dispatcher: Any,
    done_content: str,
    done_summary: str,
) -> Generator[bytes, None, None]:
    action_results: list[dict[str, object]] = []
    yield from stream_action_dispatch_events(
        actions=actions,
        request_id=request_id,
        user_id=user_id,
        action_dispatcher=action_dispatcher,
        action_results=action_results,
    )

    yield sse_event(
        "actions",
        {
            "request_id": request_id,
            "total": len(action_results),
            "items": [
                item["action"]
                for item in action_results
                if isinstance(item.get("action"), dict)
            ],
            "results": action_results,
        },
    )
    content, summary = action_completion_message(
        action_results,
        success_content=done_content,
        success_summary=done_summary,
    )
    if content:
        yield sse_event(
            "assistant_delta",
            {
                "content": content,
                "summary": summary,
                "has_actions": True,
                "action_count": len(action_results),
                "action_results": action_results,
            },
        )
    yield sse_event(
        "assistant_done",
        {
            "content": content,
            "summary": summary,
            "has_actions": True,
            "action_count": len(action_results),
            "action_results": action_results,
        },
    )


def stream_action_dispatch_events(
    *,
    actions: list[ClientAction],
    request_id: str,
    user_id: str,
    action_dispatcher: Any,
    action_results: list[dict[str, object]] | None = None,
    all_actions: list[ClientAction] | None = None,
    execution_context: list[str] | None = None,
) -> Generator[bytes, None, None]:
    result_sink = action_results if action_results is not None else []
    action_sink = all_actions if all_actions is not None else []
    pending_actions = list(actions)
    while pending_actions:
        action = pending_actions.pop(0)
        action_index = len(action_sink) + len(result_sink) + 1
        fallback_action_id = f"plan-step-{action_index}"
        action_sink.append(action)
        envelope = action_dispatcher.enqueue(
            user_id=user_id,
            request_id=request_id,
            action=action,
        )
        action_id = envelope.action_id or fallback_action_id
        ready_result = _ready_server_action_result(
            action_dispatcher=action_dispatcher,
            action_id=envelope.action_id,
            request_id=request_id,
        )
        if ready_result is None:
            yield sse_event(
                "plan_step",
                _action_plan_step_payload(
                    action,
                    action_id=action_id,
                    status="queued",
                    request_id=request_id,
                ),
            )
            yield sse_event("action_dispatch", envelope.model_dump())
        yield sse_event(
            "plan_step",
            _action_plan_step_payload(
                action,
                action_id=action_id,
                status="in_progress",
                request_id=request_id,
            ),
        )
        action_result = ready_result or action_dispatcher.wait_for_result(
            action_id=envelope.action_id,
            request_id=request_id,
            timeout_seconds=action_result_timeout_seconds(action),
        )
        action_payload = action_result_payload(envelope, action_result, action)
        result_sink.append(action_payload)
        yield sse_event(
            "plan_step",
            _action_plan_step_payload(
                action,
                action_id=action_id,
                status=_normalize_plan_step_status(action_result.status),
                request_id=request_id,
            ),
        )
        yield sse_event("action_result", action_payload)
        record_action_context(
            action_dispatcher=action_dispatcher,
            user_id=user_id,
            action=action,
            status=action_result.status,
            output=action_result.output,
            action_id=envelope.action_id,
        )
        if execution_context is not None:
            execution_context.append(
                format_action_context(
                    action=action,
                    status=action_result.status,
                    output=action_result.output,
                    error=action_result.error,
                )
            )
        follow_up = follow_up_action_from_result(
            action,
            status=action_result.status,
            output=action_result.output,
        )
        if follow_up is not None:
            pending_actions.append(follow_up)


def _ready_server_action_result(
    *,
    action_dispatcher: Any,
    action_id: str,
    request_id: str,
) -> Any | None:
    result_if_ready = getattr(action_dispatcher, "result_if_ready", None)
    if not callable(result_if_ready):
        return None
    return result_if_ready(action_id=action_id, request_id=request_id)


def dispatch_actions_sync(
    *,
    actions: list[ClientAction],
    request_id: str,
    user_id: str,
    action_dispatcher: Any,
    execution_context: list[str] | None = None,
) -> tuple[list[ClientAction], list[dict[str, object]]]:
    all_actions: list[ClientAction] = []
    action_results: list[dict[str, object]] = []
    pending_actions = list(actions)
    while pending_actions:
        action = pending_actions.pop(0)
        all_actions.append(action)
        envelope, action_result = action_dispatcher.dispatch_and_wait(
            user_id=user_id,
            request_id=request_id,
            action=action,
            timeout_seconds=action_result_timeout_seconds(action),
        )
        action_payload = action_result_payload(envelope, action_result, action)
        action_results.append(action_payload)
        record_action_context(
            action_dispatcher=action_dispatcher,
            user_id=user_id,
            action=action,
            status=action_result.status,
            output=action_result.output,
            action_id=envelope.action_id,
        )
        if execution_context is not None:
            execution_context.append(
                format_action_context(
                    action=action,
                    status=action_result.status,
                    output=action_result.output,
                    error=action_result.error,
                )
            )
        follow_up = follow_up_action_from_result(
            action,
            status=action_result.status,
            output=action_result.output,
        )
        if follow_up is not None:
            pending_actions.append(follow_up)
    return all_actions, action_results


CONFIRM_LIKELY_ACTION_TYPES = {
    "keyboard_type",
    "hotkey",
    "terminal",
    "file_write",
    "mouse_click",
    "mouse_drag",
}

CONFIRM_LIKELY_BROWSER_COMMANDS = {"click_element", "type_element"}

CONFIRM_LIKELY_CALENDAR_COMMANDS = {
    "create_event",
    "update_event",
    "delete_event",
}


def action_result_timeout_seconds(action: ClientAction) -> float | None:
    """Allow UI confirmation latency before marking risky actions timed out."""
    if not _confirmation_likely(action):
        return None
    raw = os.getenv("JARVIS_CLIENT_ACTION_CONFIRM_TIMEOUT_SECONDS", "45")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 45.0


def _confirmation_likely(action: ClientAction) -> bool:
    action_type = str(action.type or "")
    command = str(action.command or "")
    return (
        bool(action.requires_confirm)
        or action_type in CONFIRM_LIKELY_ACTION_TYPES
        or (action_type == "browser_control" and command in CONFIRM_LIKELY_BROWSER_COMMANDS)
        or (
            action_type == "calendar_control"
            and command in CONFIRM_LIKELY_CALENDAR_COMMANDS
        )
    )


def record_action_context(
    *,
    action_dispatcher: Any,
    user_id: str,
    action: ClientAction,
    status: str,
    output: dict[str, object],
    action_id: str | None = None,
) -> None:
    store = getattr(action_dispatcher, "context_store", None)
    if store is None:
        return
    store.record_action_result(
        user_id=user_id,
        action=action,
        status=status,
        output=output,
        action_id=action_id,
    )


def follow_up_action_from_result(
    action: ClientAction,
    *,
    status: str,
    output: dict[str, object],
) -> ClientAction | None:
    if status != "completed":
        return None
    if action.type != "browser_control" or action.command != "extract_dom":
        return None
    if not isinstance(action.args, dict):
        return None
    purpose = action.args.get("purpose")
    raw_query = action.args.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip():
        return None
    if purpose == "resolve_open_request":
        resolved = resolve_link_from_dom_output(output, query=raw_query)
        if resolved is None:
            return None
        if resolved.ai_id is not None:
            return ClientAction(
                type="browser_control",
                command="click_element",
                target="active_tab",
                args={"ai_id": resolved.ai_id},
                description=(
                    f"현재 페이지에서 '{raw_query}'에 가장 가까운 요소 클릭: "
                    f"{resolved.title or resolved.href}"
                ),
                requires_confirm=False,
            )
        return ClientAction(
            type="open_url",
            command=None,
            target=resolved.href,
            args={"browser": "chrome"},
            description=(
                f"현재 페이지에서 '{raw_query}'에 가장 가까운 링크 열기: "
                f"{resolved.title or resolved.href}"
            ),
            requires_confirm=False,
        )
    if purpose == "resolve_type_request":
        raw_text = action.args.get("text")
        if not isinstance(raw_text, str) or not raw_text:
            return None
        resolved_input = resolve_input_from_dom_output(output, query=raw_query)
        if resolved_input is None:
            return None
        return ClientAction(
            type="browser_control",
            command="type_element",
            target="active_tab",
            payload=raw_text,
            args={
                "ai_id": resolved_input.ai_id,
                "enter": bool(action.args.get("enter", False)),
            },
            description=(
                f"현재 페이지의 '{resolved_input.label or raw_query}' 입력란에 텍스트 입력"
            ),
            requires_confirm=False,
        )
    return None
