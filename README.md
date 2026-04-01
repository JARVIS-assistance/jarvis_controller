# jarvis_controller

`jarvis_controller`는 사용자 요청이 처음 도착하는 엔드포인트 계층이자, 전체 처리 흐름을 조정하는 orchestration 레이어다.

이 모듈은 직접 무거운 AI 추론을 수행하는 곳이 아니라, 요청을 받아 적절한 흐름으로 연결하고 결과를 응답 계약에 맞게 조립하는 역할을 맡는다.

## 역할

- 외부 엔드포인트 제공
- 인증 결과를 사용자 컨텍스트에 연결
- 대화 요청 라우팅
- realtime / deep / planning 흐름 제어
- execute / verify 엔드포인트 관리
- core 결과를 최종 응답으로 조립

## 책임 범위

`jarvis_controller`는 아래를 책임진다.

- 사용자가 호출하는 API를 안정적으로 제공
- 요청 성격에 따라 어떤 모드로 처리할지 결정
- 인증이 필요한 요청은 `jarvis_gateway`와 연결
- 실제 판단이나 AI 호출은 `jarvis_core`에 위임
- planning 결과나 실행 결과를 응답 계약 형태로 정리

즉, `jarvis_controller`는 "언제 무엇을 호출할지"를 관리하는 계층이다.

## 다른 모듈과의 관계

- `jarvis_gateway`
  - 인증/인가와 사용자 권한 확인 위임
- `jarvis_core`
  - realtime/deep 코어 처리 위임
- `jarvis_contracts`
  - conversation, execute, verify 응답 포맷 공유

플래닝에 대해서는 현재 기준을 다음처럼 둔다.

- 플래닝 제어, 사용자 응답 조립, 흐름 orchestration은 `jarvis_controller`
- 향후 고도화될 플래닝 지능과 코어 판단은 `jarvis_core`

## 현재 코드 기준 구성

- `src/app.py`
  - FastAPI 앱 구성과 gateway auth middleware 등록
- `src/router/router.py`
  - 주요 엔드포인트 정의
- `src/router/conversation_routing.py`
  - 대화 모드 판정
- `src/planner/conversation_orchestrator.py`
  - conversation 흐름 오케스트레이션
- `src/planner/planning_engine.py`
  - 현재 planning 응답 조립
- `src/planner/executor.py`
  - execute / verify 처리
- `src/middleware/gateway_client.py`
  - gateway 연동
- `src/middleware/core_bridge.py`
  - core 연동

보조 문서:

- `docs/action-lifecycle.md`

## 제공 엔드포인트

- `GET /health`
- `POST /auth/login`
- `POST /auth/logout`
- `GET /auth/me`
- `POST /conversation/respond`
- `POST /execute`
- `POST /verify`

## 설계 원칙

- controller는 얇고 명확한 orchestration 계층으로 유지한다.
- 인증 정책은 gateway에 중복 구현하지 않는다.
- 코어 추론과 DB 처리는 core로 넘긴다.
- 응답은 공통 계약 모델에 맞춰 일관되게 반환한다.

## Install

```bash
python3.12 -m pip install -r requirements.txt
python3.12 -m pip install -r requirements-dev.txt
```

## Run

```bash
python3.12 -m uvicorn jarvis_controller.app:app --reload --port 8001
```

## Test

```bash
python3.12 -m pytest
```

## Lint

```bash
ruff check .
```

## 할 일

- planning orchestration과 planning intelligence의 경계를 더 명확히 분리
- gateway/core 연동부를 인터페이스 레벨로 정리해 교체 가능성 확보
- 현재 mock 기반 execute/verify를 실제 실행기 구조로 확장
- conversation 라우팅 기준을 설정 파일 또는 정책 객체로 분리
- 엔드포인트별 요청/응답 예시와 에러 시나리오 문서화
