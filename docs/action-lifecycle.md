# Action Lifecycle

현재 액션 실행 표준은 `ClientAction` queue 기반이다.

상세 규약은 아래 문서를 기준으로 한다.

- [Client Action Contract](./client-action-contract.md)

요약 흐름:

1. Frontend가 `POST /conversation/stream`으로 사용자 메시지를 전송한다.
2. Controller가 realtime 응답과 ActionCompiler intent classifier를 병렬로 시작한다. action-like 요청이면 realtime prompt는 빠르게 `잠시만요!`만 출력해야 한다.
3. Controller가 비어있지 않은 메시지를 ActionCompiler model에 보내 `ClientActionPlan` v2를 받는다. 자연어를 keyword/regex/fallback 코드로 해석하지 않는다. 기본 classifier timeout은 `JARVIS_ACTION_INTENT_MODEL_TIMEOUT_SECONDS=6.0`이고, 모델은 `JARVIS_ACTION_INTENT_MODEL_NAME` / `JARVIS_ACTION_INTENT_MODEL` 계열 설정을 계속 사용한다.
4. realtime stream이 먼저 `assistant_done`에 도달하더라도 Controller는 `JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS`(기본 6.25초) 동안 action intent 결과를 기다릴 수 있다. 빠른 응답성 검증에서는 이 값을 낮춰야 한다.
5. Controller가 `ActionValidator`로 v2 plan의 capability, args, policy, confirmation requirement를 검증한다. invalid output은 validation errors와 함께 compiler retry를 한 번 수행한다.
6. valid `direct` 또는 `direct_sequence`이면 Controller는 core의 일반 realtime 답변을 중단하고 `assistant_delta`로 `잠시만요!`, SSE `action_intent`, `assistant_delta`로 `요청하신 작업 시작할게요`를 보낸 뒤, v2-to-v1 adapter가 기존 `ClientAction`으로 변환한 action을 queue에 넣는다.
7. Controller는 SSE `action_dispatch`로 UI에 액션 발생을 알린다.
8. Frontend action poller가 `GET /client/actions/pending`으로 pending action을 가져온다.
9. Frontend가 로컬 PC/브라우저에서 액션을 실행한다.
10. Frontend가 `POST /client/actions/{action_id}/result`로 결과를 제출한다.
11. Controller가 SSE `action_result`, `actions`, `assistant_done`으로 결과를 흘려준다.

완료 메시지 규칙:

- 전체 completed: 성공 메시지
- 일부 completed: partial success 메시지와 첫 오류
- 전체 failed: 실패 메시지와 첫 오류
- timeout: timeout 메시지
- rejected: rejected 메시지
- invalid/suppressed: 실행하지 않았다는 명확한 메시지

`assistant_done.content`는 실행 결과를 사실대로 반영해야 하며, failed/timeout/rejected/suppressed에 `"요청한 작업을 실행했습니다."`를 보내면 안 된다.
