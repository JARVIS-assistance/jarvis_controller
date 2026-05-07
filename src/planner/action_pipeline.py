from __future__ import annotations

import json
import os
from collections.abc import Generator
from typing import Any

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
        action_result = action_dispatcher.wait_for_result(
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
