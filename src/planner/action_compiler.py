from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
from dataclasses import dataclass
from typing import Any

from jarvis_contracts import (
    ClientAction,
    ClientActionPlan,
    ClientActionValidationIssue,
    format_action_v2_registry_for_prompt,
)

from planner.action_adapter import V2ToV1ActionAdapter
from planner.action_gate import (
    ActionIntentGate,
    intent_gate_payload,
    intent_gate_prompt,
    parse_intent_gate,
)
from planner.action_model_client import (
    action_compiler_model_name,
    action_intent_model_name,
    action_model_endpoint,
    action_model_provider,
    complete_model_text,
    post_json_request,
)
from planner.action_plan_parser import (
    EmbeddedActionParseResult,
    coerce_client_actions_from_text,
    parse_embedded_actions_from_text,
    parse_plan,
)
from planner.action_templates import (
    fast_action_templates,
    materialize_gate_template,
    required_action_names_for_gate,
    template_key_for_gate,
)
from planner.action_validator import ActionValidator

logger = logging.getLogger("jarvis_controller.action_compiler")

DIRECT_EXECUTION_MODES = {"direct", "direct_sequence"}

__all__ = [
    "DIRECT_EXECUTION_MODES",
    "ActionCompiler",
    "ActionIntentDecision",
    "EmbeddedActionParseResult",
    "action_compiler_prompt_payload",
    "classify_client_action_intent_decision",
    "coerce_client_actions_from_text",
    "compile_action_decision_from_model_text",
    "parse_embedded_actions_from_text",
    "should_try_client_action_classifier",
]


@dataclass(frozen=True)
class ActionIntentDecision:
    should_act: bool
    execution_mode: str
    intent: str | None
    confidence: float
    reason: str | None
    actions: list[ClientAction]
    plan: ClientActionPlan | None = None
    validation_errors: list[ClientActionValidationIssue] | None = None


class ActionCompiler:
    """Compile natural language into ActionContract v2 plans via the model."""

    def __init__(
        self,
        *,
        validator: ActionValidator | None = None,
        adapter: V2ToV1ActionAdapter | None = None,
    ) -> None:
        self.validator = validator or ActionValidator()
        self.adapter = adapter or V2ToV1ActionAdapter(validator=self.validator)

    def compile_plan(
        self,
        *,
        message: str,
        context: dict[str, Any] | None = None,
        latest_observation: dict[str, Any] | None = None,
        validation_errors: list[ClientActionValidationIssue] | None = None,
        intent_gate: ActionIntentGate | None = None,
    ) -> ClientActionPlan | None:
        if not message.strip():
            return ClientActionPlan(
                mode="no_action", confidence=1.0, reason="empty message"
            )
        if not _enabled():
            return None

        endpoint = action_model_endpoint()
        model = action_compiler_model_name()
        provider = action_model_provider()
        timeout = _float_env("JARVIS_ACTION_COMPILER_MODEL_TIMEOUT_SECONDS", 20.0)
        max_tokens = int(_float_env("JARVIS_ACTION_COMPILER_MODEL_MAX_TOKENS", 320))
        logger.info(
            "action plan compiler request provider=%s endpoint=%s model=%s "
            "timeout=%.1fs message=%s",
            provider,
            endpoint,
            model,
            timeout,
            message[:200],
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message,
                            "runtime_context": context or {},
                            "latest_observation": latest_observation or {},
                            "intent_gate": intent_gate_payload(intent_gate),
                            "action_templates": fast_action_templates(),
                            "validation_errors": [
                                issue.model_dump()
                                for issue in (validation_errors or [])
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens,
        }

        try:
            content = complete_model_text(
                provider=provider,
                endpoint=endpoint,
                model=model,
                payload=payload,
                timeout=timeout,
                post_json=_post_json,
            )
            plan = parse_plan(content)
            logger.info(
                "action plan compiler response mode=%s confidence=%.2f actions=%d",
                plan.mode,
                plan.confidence,
                len(plan.actions),
            )
            return plan
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            logger.warning(
                "action plan compiler failed endpoint=%s model=%s timeout=%.1fs error=%s",
                endpoint,
                model,
                timeout,
                exc,
            )
            return None
        except Exception as exc:
            logger.warning("action compiler failed: %s", exc)
            return None

    def compile_intent_gate(
        self,
        *,
        message: str,
        context: dict[str, Any] | None = None,
        latest_observation: dict[str, Any] | None = None,
    ) -> ActionIntentGate | None:
        if not message.strip():
            return ActionIntentGate(
                should_act=False,
                intent="none",
                confidence=1.0,
                reason="empty message",
            )
        if not _enabled():
            return None
        endpoint = action_model_endpoint()
        model = action_intent_model_name()
        provider = action_model_provider()
        timeout = _float_env("JARVIS_ACTION_INTENT_MODEL_TIMEOUT_SECONDS", 2.0)
        max_tokens = int(_float_env("JARVIS_ACTION_INTENT_MODEL_MAX_TOKENS", 192))
        logger.info(
            "action intent gate request provider=%s endpoint=%s model=%s timeout=%.1fs message=%s",
            provider,
            endpoint,
            model,
            timeout,
            message[:200],
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": intent_gate_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": message,
                            "runtime_context": context or {},
                            "latest_observation": latest_observation or {},
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        try:
            content = complete_model_text(
                provider=provider,
                endpoint=endpoint,
                model=model,
                payload=payload,
                timeout=timeout,
                post_json=_post_json,
            )
            gate = parse_intent_gate(content)
            logger.info(
                "action intent gate response should_act=%s intent=%s confidence=%.2f",
                gate.should_act,
                gate.intent,
                gate.confidence,
            )
            return gate
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            logger.warning(
                "action intent gate failed endpoint=%s model=%s timeout=%.1fs error=%s",
                endpoint,
                model,
                timeout,
                exc,
            )
            return None
        except Exception as exc:
            logger.warning("action intent gate failed: %s", exc)
            return None

    def compile_decision(
        self,
        *,
        message: str,
        context: dict[str, Any] | None = None,
        latest_observation: dict[str, Any] | None = None,
        validation_errors: list[ClientActionValidationIssue] | None = None,
        max_retries: int = 1,
    ) -> ActionIntentDecision | None:
        gate = self.compile_intent_gate(
            message=message,
            context=context,
            latest_observation=latest_observation,
        )
        if (
            gate is not None
            and not gate.should_act
            and gate.confidence >= _action_intent_confidence_threshold()
        ):
            return ActionIntentDecision(
                should_act=False,
                execution_mode="no_action",
                intent=gate.intent or "none",
                confidence=gate.confidence,
                reason=gate.reason,
                actions=[],
                plan=None,
                validation_errors=[],
            )
        if gate is None:
            logger.warning(
                "action intent gate unavailable; trying plan compiler fallback message=%s",
                message[:200],
            )
        elif not gate.should_act:
            logger.info(
                "action intent gate confidence below threshold; trying plan compiler "
                "fallback confidence=%.2f threshold=%.2f message=%s",
                gate.confidence,
                _action_intent_confidence_threshold(),
                message[:200],
            )

        plan = self.compile_plan(
            message=message,
            context=context,
            latest_observation=latest_observation,
            validation_errors=validation_errors,
            intent_gate=gate,
        )
        if plan is None:
            return None

        if (
            gate is not None
            and gate.should_act
            and plan.mode == "no_action"
            and gate.confidence >= _action_intent_confidence_threshold()
            and max_retries > 0
        ):
            if _has_working_context_followup_state(context):
                reused_text_error = _gate_template_reuses_working_text_issue(
                    gate,
                    context,
                )
                if reused_text_error is not None:
                    return ActionIntentDecision(
                        should_act=False,
                        execution_mode="invalid",
                        intent=gate.intent or "action",
                        confidence=gate.confidence,
                        reason="contextual follow-up text was not transformed",
                        actions=[],
                        plan=plan,
                        validation_errors=[reused_text_error],
                    )
                template_decision = self._decision_from_gate_template(
                    gate,
                    context=context,
                )
                if template_decision is not None and template_decision.should_act:
                    return template_decision

            retry_errors = [
                _issue(
                    "intent_gate_contradiction",
                    "Intent gate classified this request as actionable; "
                    "compile an action plan instead of no_action.",
                    field="mode",
                    details=intent_gate_payload(gate),
                )
            ]
            plan = self.compile_plan(
                message=message,
                context=context,
                latest_observation=latest_observation,
                validation_errors=retry_errors,
                intent_gate=gate,
            )
            if plan is None:
                return None
            max_retries -= 1

        if (
            gate is None
            and plan.mode == "no_action"
            and max_retries > 0
            and _has_working_context_followup_state(context)
        ):
            retry_errors = [
                _issue(
                    "working_context_followup_check",
                    "Short-term working context is available. Re-check whether "
                    "the user message is a follow-up operation on the previous "
                    "app/browser/typed content. If it is only ordinary chat, "
                    "recommendation, advice, or explanation, keep no_action.",
                    field="runtime_context.working_context",
                    details=_working_context_retry_details(context),
                )
            ]
            plan = self.compile_plan(
                message=message,
                context=context,
                latest_observation=latest_observation,
                validation_errors=retry_errors,
                intent_gate=gate,
            )
            if plan is None:
                return None
            max_retries -= 1

        if (
            gate is not None
            and gate.should_act
            and plan.mode == "no_action"
            and gate.confidence >= _action_intent_confidence_threshold()
        ):
            reused_text_error = _gate_template_reuses_working_text_issue(
                gate,
                context,
            )
            if reused_text_error is not None:
                repair_plan = self.compile_plan(
                    message=message,
                    context=context,
                    latest_observation=latest_observation,
                    validation_errors=[reused_text_error],
                    intent_gate=gate,
                )
                if repair_plan is None:
                    return None
                plan_reuse_error = _plan_reuses_working_text_issue(
                    repair_plan,
                    context,
                )
                if plan_reuse_error is not None or repair_plan.mode == "no_action":
                    return ActionIntentDecision(
                        should_act=False,
                        execution_mode="invalid",
                        intent=gate.intent or "action",
                        confidence=gate.confidence,
                        reason="contextual follow-up text was not transformed",
                        actions=[],
                        plan=repair_plan,
                        validation_errors=[plan_reuse_error or reused_text_error],
                    )
                plan = repair_plan
            else:
                template_decision = self._decision_from_gate_template(
                    gate,
                    context=context,
                )
                if template_decision is not None:
                    return template_decision

        template_completeness_error = _intent_template_completeness_issue(gate, plan)
        if template_completeness_error is not None and max_retries > 0:
            plan = self.compile_plan(
                message=message,
                context=context,
                latest_observation=latest_observation,
                validation_errors=[template_completeness_error],
                intent_gate=gate,
            )
            if plan is None:
                return None
            max_retries -= 1
            if (
                gate is not None
                and gate.should_act
                and plan.mode == "no_action"
                and gate.confidence >= _action_intent_confidence_threshold()
            ):
                template_decision = self._decision_from_gate_template(
                    gate,
                    context=context,
                )
                if template_decision is not None:
                    return template_decision
            template_completeness_error = _intent_template_completeness_issue(gate, plan)

        if (
            template_completeness_error is not None
            and gate is not None
            and gate.should_act
            and gate.confidence >= _action_intent_confidence_threshold()
        ):
            template_decision = self._decision_from_gate_template(
                gate,
                context=context,
            )
            if template_decision is not None:
                return template_decision

        decision = self._decision_from_plan(plan, message=message, context=context)
        retries_left = max_retries
        while decision is not None and decision.validation_errors and retries_left > 0:
            retry_plan = self.compile_plan(
                message=message,
                context=context,
                latest_observation=latest_observation,
                validation_errors=decision.validation_errors,
                intent_gate=gate,
            )
            if retry_plan is None:
                break
            decision = self._decision_from_plan(
                retry_plan,
                message=message,
                context=context,
            )
            retries_left -= 1
        return decision

    def _decision_from_gate_template(
        self,
        gate: ActionIntentGate,
        *,
        context: dict[str, Any] | None,
    ) -> ActionIntentDecision | None:
        materialized = materialize_gate_template(gate, context=context)
        plan = materialized.plan
        issues = materialized.issues
        if isinstance(plan, ClientActionPlan):
            logger.info(
                "action compiler using intent template fallback template=%s intent=%s",
                template_key_for_gate(gate),
                gate.intent,
            )
            return self._decision_from_plan(plan, message="", context=context)
        if issues:
            return ActionIntentDecision(
                should_act=False,
                execution_mode="invalid",
                intent=gate.intent or "action",
                confidence=gate.confidence,
                reason="intent template fallback could not be materialized",
                actions=[],
                plan=None,
                validation_errors=issues,
            )
        return None

    def _decision_from_plan(
        self,
        plan: ClientActionPlan,
        *,
        message: str = "",
        context: dict[str, Any] | None,
    ) -> ActionIntentDecision:
        if plan.mode == "no_action":
            return ActionIntentDecision(
                should_act=False,
                execution_mode="no_action",
                intent="none",
                confidence=plan.confidence,
                reason=plan.reason,
                actions=[],
                plan=plan,
                validation_errors=[],
            )

        adapted = self.adapter.adapt_plan(plan, context=context)
        if adapted.valid:
            execution_mode = plan.mode
            if execution_mode not in {"direct", "direct_sequence", "needs_plan"}:
                execution_mode = (
                    "direct_sequence" if len(adapted.actions) > 1 else "direct"
                )
            return ActionIntentDecision(
                should_act=bool(adapted.actions),
                execution_mode=execution_mode,
                intent=_intent_from_plan(plan),
                confidence=plan.confidence,
                reason=plan.reason,
                actions=adapted.actions,
                plan=plan,
                validation_errors=[],
            )

        return ActionIntentDecision(
            should_act=False,
            execution_mode="invalid",
            intent=_intent_from_plan(plan),
            confidence=plan.confidence,
            reason=plan.reason,
            actions=[],
            plan=plan,
            validation_errors=adapted.issues,
        )


def classify_client_action_intent_decision(
    message: str,
    *,
    context: dict[str, Any] | None = None,
    latest_observation: dict[str, Any] | None = None,
    validation_errors: list[ClientActionValidationIssue] | None = None,
) -> ActionIntentDecision | None:
    return ActionCompiler().compile_decision(
        message=message,
        context=context,
        latest_observation=latest_observation,
        validation_errors=validation_errors,
    )


def compile_action_decision_from_model_text(
    content: str,
    *,
    context: dict[str, Any] | None = None,
) -> ActionIntentDecision | None:
    try:
        plan = parse_plan(content)
    except Exception as exc:
        logger.warning("action compiler fallback response parse failed: %s", exc)
        return None
    return ActionCompiler()._decision_from_plan(plan, message="", context=context)


_parse_intent_gate = parse_intent_gate
_intent_gate_payload = intent_gate_payload


def action_compiler_prompt_payload(
    *,
    message: str,
    context: dict[str, Any] | None = None,
    validation_errors: list[ClientActionValidationIssue] | None = None,
) -> str:
    return (
        _system_prompt()
        + "\n\nInput JSON:\n"
        + json.dumps(
            {
                "message": message,
                "runtime_context": context or {},
                "action_templates": fast_action_templates(),
                "validation_errors": [
                    issue.model_dump() for issue in (validation_errors or [])
                ],
            },
            ensure_ascii=False,
        )
    )


def should_try_client_action_classifier(message: str) -> bool:
    """Only skip empty messages. No keyword or phrase gate is allowed."""
    return bool(message.strip())


_fast_action_templates = fast_action_templates
_materialize_gate_template = materialize_gate_template
_template_key_for_gate = template_key_for_gate


_parse_plan = parse_plan


def _system_prompt() -> str:
    registry = format_action_v2_registry_for_prompt()
    return f"""You are the JARVIS Action Compiler.
Return exactly one JSON object. No markdown. No prose.

Compile the user message into an ActionContract v2 ClientActionPlan.
Do not answer the user. Do not emit v1 action types.
If the request is ordinary conversation or information-only, return mode "no_action".
If validation_errors are provided, fix the structured plan according to those errors.
If input intent_gate.should_act is true, treat the request as actionable and do not
return no_action unless the requested operation is impossible under the registry or
disabled runtime capabilities.

Rules:
- Use only capability names from the registry.
- Return no_action for greetings, thanks, casual chat, or information-only requests.
- A request is actionable only when the user asks JARVIS to operate the local computer,
  browser, application, shell, keyboard, mouse, files, clipboard, or screen.
- runtime_context.working_context is short-term action session state. Use it to
  resolve follow-up requests that refer to the immediately previous app, browser,
  typed text, or visible action result.
- Recommendation, advice, explanation, summary, brainstorming, or menu suggestions are
  answer-generation requests. Return no_action unless the user explicitly asks to
  search the web, open a browser, click, type, or use another local computer action.
- Use app.open/app.focus only for concrete local applications.
- When runtime_context.available_applications is present, app targets must use an
  exact listed application name. Aliases are hints only; never emit aliases as targets.
- Never use target "browser", "default_browser", or "web_browser" for app actions.
- Use browser.open/browser.navigate/browser.search for browser work.
- Use browser.extract_dom before browser.click/browser.type when the element id is unknown.
- Use browser.select_result only when the user asks to open a numbered result
  already visible in current search results.
- If the user asks to search for something and "go in/open it" without specifying
  a numbered result, use direct_sequence: browser.search then browser.select_result
  with args.index=1.
- Do not simplify multi-operation requests. Preserve every distinct requested
  operation as ordered action steps.
- If input intent_gate.template_key is app_open_type, return app.open followed by
  keyboard.type. If intent_gate.slots.text is present, type that concrete text.
  If the user asks to write/create/compose generated content in the app, generate
  the final content and place that concrete final text in keyboard.type args.text.
- For contextual writing follow-ups, use runtime_context.working_context.active_app
  as the app target and working_context.last_typed_text as the source content.
  Translate, rewrite, continue, shorten, or otherwise transform that source only
  when the user asks for that follow-up operation. If the user does not clearly ask
  to replace existing content, append/type the new text without select-all.
- If the user clearly asks to replace existing content, the plan may press
  keyboard.hotkey with keys "command,a" on macOS or "ctrl,a" otherwise before
  keyboard.type.
- If validation_errors contains code intent_template_incomplete, include every
  missing action named in details.missing_actions while preserving the expected
  order in details.expected_actions.
- terminal.run and calendar create/update/delete require requires_confirm=true.
- Disabled or unavailable capabilities in runtime_context must not be used.
- Do not invent missing URLs. If only a query is known, use browser.search with args.query.
- Prefer an action_templates entry when it fits the request. Replace placeholders
  such as <query>, <text>, and <exact app name>; do not return placeholders.

Korean action examples:
- "점심 메뉴 추천해줘" => mode no_action, actions [].
- "점심 메뉴 추천해줘. 브라우저에서 검색해줘" => mode direct,
  action browser.search, args {{"query":"점심 메뉴 추천"}}.
- "브라우저 열어줘" => mode direct, action browser.open, args {{}}.
- "브라우저에서 연어장 레시피 검색해줘" => mode direct, action browser.search,
  args {{"query":"연어장 레시피"}}.
- "네이버 웹툰 검색해서 들어가줘" => mode direct_sequence,
  actions browser.search args {{"query":"네이버 웹툰"}}, then browser.select_result
  args {{"index":1}}.
- "Sublime Text 열어서 안녕하세요 작성해줘" => mode direct_sequence,
  actions app.open target "Sublime Text", then keyboard.type args {{"text":"안녕하세요"}}.
- "텍스트 편집기 열어서 너의 소개 작성해줘" => mode direct_sequence,
  actions app.open target exact listed app name, then keyboard.type with generated
  final introduction text in args.text.
- Given runtime_context.working_context.active_app "Sublime Text" and
  last_typed_text "안녕하세요. 저는 JARVIS입니다.", "영어로 작성해봐" =>
  mode direct_sequence, actions app.open or app.focus target "Sublime Text",
  then keyboard.type with the English rewritten final text.
- "현재화면 캡쳐해서 사진으로 띄워줘" => mode direct, action screen.screenshot,
  args {{}}.

Fast JSON templates:
{json.dumps(fast_action_templates(), ensure_ascii=False)}

Capability registry:
{registry}

JSON schema:
{{
  "mode": "direct|direct_sequence|needs_plan|no_action",
  "goal": "short goal or null",
  "confidence": 0.0,
  "reason": "short reason",
  "actions": [
    {{
      "name": "browser.search",
      "target": null,
      "payload": null,
      "args": {{"query": "search terms"}},
      "description": "human readable action",
      "requires_confirm": false
    }}
  ]
}}
"""


_intent_gate_prompt = intent_gate_prompt


def _post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    return post_json_request(url, payload, timeout=timeout)


def _intent_from_plan(plan: ClientActionPlan) -> str | None:
    if not plan.actions:
        return "none" if plan.mode == "no_action" else None
    namespaces = {action.name.split(".", 1)[0] for action in plan.actions}
    return next(iter(namespaces)) if len(namespaces) == 1 else "multi_action"


def _intent_template_completeness_issue(
    gate: ActionIntentGate | None,
    plan: ClientActionPlan,
) -> ClientActionValidationIssue | None:
    if (
        gate is None
        or not gate.should_act
        or gate.confidence < _action_intent_confidence_threshold()
        or plan.mode == "no_action"
    ):
        return None
    expected = required_action_names_for_gate(gate)
    if len(expected) < 2:
        return None
    actual = tuple(action.name for action in plan.actions)
    missing = _missing_ordered_action_names(expected, actual)
    if not missing:
        return None
    return _issue(
        "intent_template_incomplete",
        "Intent gate selected a multi-step action template, but the compiled "
        "plan omitted one or more required action steps.",
        action_index=_first_missing_action_index(expected, actual),
        action_name=missing[0],
        field="actions",
        details={
            "template_key": template_key_for_gate(gate),
            "intent_gate": intent_gate_payload(gate),
            "expected_actions": list(expected),
            "actual_actions": list(actual),
            "missing_actions": list(missing),
        },
    )


def _has_working_context_followup_state(context: dict[str, Any] | None) -> bool:
    working_context = (context or {}).get("working_context")
    if not isinstance(working_context, dict):
        return False
    active_app = working_context.get("active_app")
    active_browser = working_context.get("active_browser")
    last_typed_text = working_context.get("last_typed_text")
    recent_actions = working_context.get("recent_actions")
    last_typed_target = working_context.get("last_typed_target")
    last_user_visible_output = working_context.get("last_user_visible_output")
    has_active_surface = (
        isinstance(active_app, str)
        and bool(active_app.strip())
        or isinstance(active_browser, str)
        and bool(active_browser.strip())
    )
    has_recent_context = (
        isinstance(last_typed_text, str)
        and bool(last_typed_text.strip())
        or isinstance(last_typed_target, str)
        and bool(last_typed_target.strip())
        or isinstance(last_user_visible_output, str)
        and bool(last_user_visible_output.strip())
        or isinstance(recent_actions, list)
        and bool(recent_actions)
    )
    return has_active_surface and has_recent_context


def _working_context_retry_details(
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    working_context = (context or {}).get("working_context")
    if not isinstance(working_context, dict):
        return {}
    details: dict[str, Any] = {}
    for key in (
        "active_surface",
        "active_app",
        "active_browser",
        "last_typed_target",
        "last_user_visible_output",
    ):
        value = working_context.get(key)
        if isinstance(value, str) and value.strip():
            details[key] = value.strip()
    last_typed_text = working_context.get("last_typed_text")
    if isinstance(last_typed_text, str) and last_typed_text.strip():
        details["last_typed_text_available"] = True
        details["last_typed_text_chars"] = len(last_typed_text)
    recent_actions = working_context.get("recent_actions")
    if isinstance(recent_actions, list):
        details["recent_action_count"] = len(recent_actions)
    return details


def _missing_ordered_action_names(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> tuple[str, ...]:
    missing: list[str] = []
    cursor = 0
    for expected_name in expected:
        try:
            match_index = actual.index(expected_name, cursor)
        except ValueError:
            missing.append(expected_name)
            continue
        cursor = match_index + 1
    return tuple(missing)


def _first_missing_action_index(
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> int | None:
    cursor = 0
    for expected_index, expected_name in enumerate(expected):
        try:
            match_index = actual.index(expected_name, cursor)
        except ValueError:
            return min(expected_index, len(actual))
        cursor = match_index + 1
    return None


def _gate_template_reuses_working_text_issue(
    gate: ActionIntentGate,
    context: dict[str, Any] | None,
) -> ClientActionValidationIssue | None:
    if not gate.should_act:
        return None
    template_key = template_key_for_gate(gate)
    if template_key != "app_open_type":
        return None
    source_text = _working_context_last_typed_text(context)
    if source_text is None:
        return None
    slots = gate.slots or {}
    text = slots.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if _normalized_context_text(text) != _normalized_context_text(source_text):
        return None
    return _issue(
        "working_context_text_reused",
        "The generated text for a contextual follow-up is identical to the previous typed text. "
        "Generate the requested transformed or continued final text instead of copying the source, "
        "unless the user explicitly requested an exact repeat.",
        field="slots.text",
        details={
            "template_key": template_key,
            "active_app": _working_context_string(context, "active_app"),
            "last_typed_text_available": True,
            "last_typed_text_chars": len(source_text),
        },
    )


def _plan_reuses_working_text_issue(
    plan: ClientActionPlan,
    context: dict[str, Any] | None,
) -> ClientActionValidationIssue | None:
    source_text = _working_context_last_typed_text(context)
    if source_text is None or plan.mode == "no_action":
        return None
    normalized_source = _normalized_context_text(source_text)
    for index, action in enumerate(plan.actions):
        if action.name != "keyboard.type":
            continue
        text = action.args.get("text")
        if not isinstance(text, str) or not text.strip():
            text = action.payload
        if not isinstance(text, str) or not text.strip():
            continue
        if _normalized_context_text(text) == normalized_source:
            return _issue(
                "working_context_text_reused",
                "The compiled keyboard.type text is identical to the previous typed text. "
                "Generate the requested transformed or continued final text instead of copying the source, "
                "unless the user explicitly requested an exact repeat.",
                action_index=index,
                action_name=action.name,
                field="actions.args.text",
                details={
                    "last_typed_text_available": True,
                    "last_typed_text_chars": len(source_text),
                    "actual_text_chars": len(text),
                },
            )
    return None


def _working_context_last_typed_text(
    context: dict[str, Any] | None,
) -> str | None:
    working_context = (context or {}).get("working_context")
    if not isinstance(working_context, dict):
        return None
    text = working_context.get("last_typed_text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text if text else None


def _working_context_string(
    context: dict[str, Any] | None,
    key: str,
) -> str | None:
    working_context = (context or {}).get("working_context")
    if not isinstance(working_context, dict):
        return None
    value = working_context.get(key)
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    return None


def _normalized_context_text(value: str) -> str:
    return " ".join(value.split()).strip().casefold()


def _enabled() -> bool:
    raw = os.getenv("JARVIS_ACTION_INTENT_MODEL_ENABLED", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _action_intent_confidence_threshold() -> float:
    return max(
        0.0,
        min(
            1.0,
            _float_env("JARVIS_ACTION_INTENT_CONFIDENCE_THRESHOLD", 0.72),
        ),
    )


def _issue(
    code: str,
    message: str,
    *,
    action_index: int | None = None,
    action_name: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> ClientActionValidationIssue:
    return ClientActionValidationIssue(
        code=code,
        message=message,
        action_index=action_index,
        action_name=action_name,
        field=field,
        details=details or {},
    )
