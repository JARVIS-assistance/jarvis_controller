# Action Lifecycle

현재 액션 실행 표준은 `ClientAction` queue 기반이다.

상세 규약은 아래 문서를 기준으로 한다.

- [Client Action Contract](./client-action-contract.md)

요약 흐름:

1. Frontend가 `POST /conversation/stream`으로 사용자 메시지를 전송한다.
2. Controller가 sLLM action intent classifier로 액션 여부를 판단한다.
3. `direct` 또는 `direct_sequence`이면 Controller가 action queue에 `ClientActionEnvelope`를 넣는다.
4. Controller는 SSE `action_dispatch`로 UI에 액션 발생을 알린다.
5. Frontend action poller가 `GET /client/actions/pending`으로 pending action을 가져온다.
6. Frontend가 로컬 PC/브라우저에서 액션을 실행한다.
7. Frontend가 `POST /client/actions/{action_id}/result`로 결과를 제출한다.
8. Controller가 SSE `action_result`, `actions`, `assistant_done`으로 결과를 흘려준다.
