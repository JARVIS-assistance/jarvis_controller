# jarvis-controller

How 계층: 실행/검증 엔진(현재 mock)을 제공하는 FastAPI 서비스.

## Features

- `GET /health`
- `POST /execute`
- `POST /verify`
- click/type/scroll mock 실행기
- 실패 시 표준 에러 포맷(`ErrorResponse`)

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
