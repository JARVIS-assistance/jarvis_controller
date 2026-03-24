# Action Lifecycle

1. Core가 `ExecuteRequest` 생성 후 Controller `/execute` 호출
2. Controller가 action type 검증
3. Mock executor가 결과 생성 (`success/detail/output`)
4. 실패 시 `ErrorResponse`로 표준 응답 반환
5. Core가 `/verify`로 기대값 검증 요청
6. Controller가 `VerifyResult.passed` 반환
