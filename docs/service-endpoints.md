# JARVIS 서비스 엔드포인트 정리

현재 워크스페이스 기준으로 인증, 회원, AI provider 설정 관련 기능은 하나의 서비스에 모여 있지 않고 `jarvis_gateway`, `jarvis_core`, `jarvis_controller`에 나뉘어 있다.

이 문서는 "현재 실제 구현"과 "외부에서 기준으로 삼아야 할 엔드포인트"를 함께 정리한다.

## 1. 서비스별 책임

### `jarvis_gateway`

- 인증/인가
- 토큰 발급 및 검증
- 사용자 생성
- 세션 및 감사 로그

### `jarvis_core`

- AI provider/model 설정 저장
- realtime/deep용 모델 선택
- 실제 AI 호출 및 채팅 처리

### `jarvis_controller`

- 외부 진입용 오케스트레이션 API
- gateway 인증 결과 연결
- core 호출 결과 조립

## 2. 현재 구현 기준 엔드포인트

### 인증/회원 관련

| 기능 | 권장 호출 서비스 | 메서드 | 경로 | 비고 |
|---|---|---|---|---|
| 로그인 | `jarvis_controller` | `POST` | `/auth/login` | controller가 gateway `/auth/login`을 프록시 |
| 로그아웃 | `jarvis_controller` | `POST` | `/auth/logout` | controller가 gateway `/auth/logout`을 프록시 |
| 내 정보 조회 | `jarvis_controller` | `GET` | `/auth/me` | controller 미들웨어가 gateway `/auth/validate` 사용 |
| 토큰 검증 | `jarvis_gateway` | `GET` | `/auth/validate` | 내부 인증 검증용 |
| 회원가입(실질 구현) | `jarvis_gateway` | `POST` | `/users` | 현재 공개 명칭은 `signup`이 아니라 사용자 생성 |
| 회원가입(legacy/비권장) | `jarvis_core` | `POST` | `/auth/register` | `410 Gone`, gateway 사용하라고 안내 |
| 로그인(legacy/비권장) | `jarvis_core` | `POST` | `/auth/login` | `410 Gone`, gateway 사용하라고 안내 |
| 내 정보 조회(core 내부 컨텍스트) | `jarvis_core` | `GET` | `/auth/me` | gateway 토큰 기반 principal 필요 |

### AI provider/model 설정 관련

| 기능 | 권장 호출 서비스 | 메서드 | 경로 | 비고 |
|---|---|---|---|---|
| 모델 설정 생성 | `jarvis_core` | `POST` | `/chat/model-config` | provider/model/api_key/endpoint 저장 |
| 모델 설정 목록 조회 | `jarvis_core` | `GET` | `/chat/model-config` | 사용자별 설정 목록 반환 |
| realtime/deep 모델 선택 저장 | `jarvis_core` | `POST` | `/chat/model-selection` | realtime/deep 각각 설정 |
| realtime/deep 모델 선택 조회 | `jarvis_core` | `GET` | `/chat/model-selection` | 현재 선택 상태 반환 |

### 대화/실행 관련

| 기능 | 권장 호출 서비스 | 메서드 | 경로 | 비고 |
|---|---|---|---|---|
| 통합 대화 응답 | `jarvis_controller` | `POST` | `/conversation/respond` | planning/realtime/deep orchestration |
| 단건 채팅 | `jarvis_core` | `POST` | `/chat/common_request` | 직접 core 채팅 호출 |
| realtime SSE | `jarvis_core` | `POST` | `/chat/realtime/stream` | 스트리밍 응답 |
| realtime WebSocket | `jarvis_core` | `WS` | `/chat/realtime` | 토큰 필요 |
| 액션 실행 | `jarvis_controller` | `POST` | `/execute` | 현재 mock executor |
| 검증 | `jarvis_controller` | `POST` | `/verify` | 현재 mock verifier |

## 3. 외부 공개 엔드포인트 기준

외부 클라이언트 기준으로는 아래처럼 보는 것이 가장 일관적이다.

### 인증

- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/me`

이 세 개는 `jarvis_controller`를 기본 진입점으로 삼는다.

이유:

- 클라이언트 입장에서 controller가 공식 API entrypoint 역할을 하고 있음
- controller가 이미 gateway 연동을 감추고 있음
- 인증 관련 헤더 전달 규칙을 controller에서 통일할 수 있음

### 회원가입

현재 `jarvis_controller`에는 회원가입 진입점이 있다.

- `POST /auth/signup`

이 엔드포인트는 내부적으로 `jarvis_gateway`의 아래 엔드포인트로 위임한다.

- `POST /users`

다만 실제 의미는 완전한 공개 self-signup보다는 관리자 권한 기반의 사용자 생성에 가깝다.
즉 제품 API 표면에서는 `signup` 이름을 제공하지만, 현재 권한 정책은 gateway `/users`와 동일하게 유지된다.

### AI provider 설정

현재 실 구현과 도메인 책임은 `jarvis_core`에 있다.

표준 엔드포인트는 아래로 보는 것이 맞다.

- `POST /chat/model-config`
- `GET /chat/model-config`
- `POST /chat/model-selection`
- `GET /chat/model-selection`

이 네 개는 AI provider/model 설정 책임과 정확히 맞아 떨어진다.

## 4. 요청 바디 기준

### 로그인

`POST /auth/login`

```json
{
  "username": "admin",
  "password": "admin123"
}
```

### 회원 생성

`POST /users`

```json
{
  "tenant_id": "tenant-1",
  "username": "new-user",
  "password": "secret",
  "role": "member"
}
```

### 모델 설정 생성

`POST /chat/model-config`

```json
{
  "provider_mode": "token",
  "provider_name": "openai",
  "model_name": "gpt-4.1",
  "api_key": "sk-...",
  "endpoint": null,
  "is_default": true,
  "supports_stream": true,
  "supports_realtime": false,
  "transport": "http_sse",
  "input_modalities": "text",
  "output_modalities": "text"
}
```

### 모델 선택 저장

`POST /chat/model-selection`

```json
{
  "realtime_model_config_id": "config-1",
  "deep_model_config_id": "config-2"
}
```

## 5. 정리된 권장 기준

현재 코드베이스를 기준으로 엔드포인트를 정리하면 아래가 가장 명확하다.

- 인증 공개 API는 `jarvis_controller`
- 회원 생성/관리 API는 `jarvis_gateway`
- AI provider/model 설정 API는 `jarvis_core`
- 대화 orchestration API는 `jarvis_controller`
- 실제 AI 채팅/스트리밍 API는 `jarvis_core`

즉, 사용자 관점의 공개 API는 `controller`, 보안/계정 관리는 `gateway`, AI 설정과 실행은 `core`로 정리된다.

## 6. 남아 있는 불일치

현재 구조에는 아래 불일치가 있다.

- `signup`이라는 이름은 표준화됐지만 실제 권한 의미는 여전히 user create에 가까움
- AI provider 설정은 `core`에만 있고 `controller`에는 프록시가 없음
- `jarvis_core`에 legacy auth 라우터가 남아 있지만 실제 사용 경로는 아님

따라서 다음 단계 후보는 명확하다.

- 공개 self-signup 정책이 필요하면 gateway `/users`와 분리된 전용 가입 플로우 설계
- `jarvis_controller`에 AI 설정 프록시 엔드포인트 추가
- legacy `jarvis_core /auth/login`, `/auth/register`는 문서상 deprecated로 고정
