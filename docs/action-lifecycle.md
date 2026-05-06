# Action Lifecycle

현재 액션 실행 표준은 `ClientAction` queue 기반이다.

상세 규약은 아래 문서를 기준으로 한다.

- [Client Action Contract](./client-action-contract.md)

요약 흐름:

1. Frontend가 `POST /conversation/stream`으로 사용자 메시지를 전송한다.
2. Controller가 비어있지 않은 메시지를 ActionCompiler model에 보내 `ClientActionPlan` v2를 받는다. 자연어를 keyword/regex/fallback 코드로 해석하지 않는다.
3. Controller가 `ActionValidator`로 v2 plan의 capability, args, policy, confirmation requirement를 검증한다. invalid output은 validation errors와 함께 compiler retry를 한 번 수행한다.
4. valid `direct` 또는 `direct_sequence`이면 v2-to-v1 adapter가 기존 `ClientAction`으로 변환하고 Controller가 action queue에 `ClientActionEnvelope`를 넣는다.
5. Controller는 SSE `action_dispatch`로 UI에 액션 발생을 알린다.
6. Frontend action poller가 `GET /client/actions/pending`으로 pending action을 가져온다.
7. Frontend가 로컬 PC/브라우저에서 액션을 실행한다.
8. Frontend가 `POST /client/actions/{action_id}/result`로 결과를 제출한다.
9. Controller가 SSE `action_result`, `actions`, `assistant_done`으로 결과를 흘려준다.

완료 메시지 규칙:

- 전체 completed: 성공 메시지
- 일부 completed: partial success 메시지와 첫 오류
- 전체 failed: 실패 메시지와 첫 오류
- timeout: timeout 메시지
- rejected: rejected 메시지
- invalid/suppressed: 실행하지 않았다는 명확한 메시지

`assistant_done.content`는 실행 결과를 사실대로 반영해야 하며, failed/timeout/rejected/suppressed에 `"요청한 작업을 실행했습니다."`를 보내면 안 된다.
