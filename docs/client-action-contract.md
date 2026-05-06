# Client Action Contract

이 문서는 Controller와 Frontend/Electron runtime 사이에서 사용자 PC 액션을 전달하고 실행하는 표준 규약이다.

Computer-use 스타일 액션 시스템의 장기 설계와 단계별 로드맵은 [Computer Use Action Plan](./computer-use-action-plan.md)을 기준으로 한다.

## 1. 원칙

- Controller는 사용자의 의도를 `ClientAction`으로 변환한다.
- Frontend는 `ClientAction`을 실행하는 유일한 주체다.
- SSE는 대화 UI에 즉시 상태를 보여주는 통로다.
- 실제 액션 전달의 기준 통로는 `/client/actions/pending` 큐다.
- Frontend는 assistant 응답 텍스트 안의 actions fenced code block을 실행하지 않는다.
- Frontend는 `conversation.done.text`, `assistant_done.content` 안의 JSON을 액션으로 해석하지 않는다.
- Frontend가 실행할 수 있는 액션은 Controller가 발급한 `action_id`가 있는 액션뿐이다.
- Frontend는 지원하지 않는 액션을 추측해서 실행하지 않는다.
- destructive/sensitive 액션은 `requires_confirm=true`일 때 반드시 사용자 확인을 거친다.
- Controller는 모델이 실수로 assistant text에 actions JSON을 출력하면 이를 사용자 텍스트로 흘리지 않고, 가능한 경우 `action_dispatcher.enqueue(...)`로 변환한다.

## 2. 전체 흐름

```tex
1. Frontend -> Controller
   POST /conversation/stream

2. Controller
   realtime 응답과 sLLM action intent classifier 병렬 실행

3. Realtime model
   action-like 요청이면 assistant_delta로 정확히 `잠시만요!` 응답

4. direct/direct_sequence 액션이면
   Controller가 `요청하신 작업 시작할게요` assistant_delta를 먼저 emit한 뒤 action_dispatcher.enqueue(...)

5. Controller -> Frontend SSE
   event: action_intent
   event: classification
   event: assistant_delta
   event: thinking
   event: action_dispatch

5. Frontend action poller
   GET /client/actions/pending?limit=20

6. Frontend
   action 실행

7. Frontend -> Controller
   POST /client/actions/{action_id}/result

8. Controller -> Frontend SSE
   event: action_result
   event: actions
   event: assistant_done
```

## 2.1 Authoritative Action Source

액션 실행의 authoritative source는 아래 둘뿐이다.

1. `GET /client/actions/pending?limit=20` 응답
2. SSE `event: action_dispatch`로 전달된 `action_id`가 있는 envelope

Frontend는 assistant가 생성한 자연어 답변, markdown 코드블록, JSON 예시를 실행 대상으로 취급하지 않는다.
Controller도 action-like 요청에서 assistant text 안의 action block을 발견하면 해당 텍스트를 SSE로 그대로 전달하지 않는다. 변환 가능한 경우 아래 정상 실행 대상처럼 queue dispatch로 바꾸고, 변환 불가능하면 실행하지 않았다는 짧은 완료 메시지만 보낸다.

금지 예시:

````md
```actions
[
  {"type": "web_search", "query": "백종원 소불고기 양념"}
]
```
````

위 블록은 화면에 표시할 수는 있지만 실행하면 안 된다.

실행 금지 대상:

- `conversation.done.text` 안의 actions fenced code block
- `assistant_done.content` 안의 actions fenced code block
- action id가 `embedded_...` 형태로 프론트에서 임의 생성된 액션
- 백엔드가 발급하지 않은 action id
- `web_search`를 브라우저 새 탭 열기로 프론트가 임의 변환한 액션

정상 실행 대상:

```json
{
    "contract_version": "1.0",
    "action_id": "act_...",
    "request_id": "req_...",
    "action": {
        "type": "browser_control",
        "command": "extract_dom",
        "target": "active_tab",
        "args": {
            "purpose": "resolve_open_request",
            "query": "백종원 소불고기 양념 맛있게 만드는법",
            "include_links": true,
            "include_elements": true,
            "max_links": 120
        },
        "description": "현재 페이지에서 링크 후보 추출",
        "requires_confirm": false
    }
}
```

정리하면 Frontend는 액션을 판단하지 않고 실행만 한다. 현재 페이지인지 새 검색인지, DOM 결과를 보고 어떤 링크를 클릭할지는 Controller가 결정한다.

## 3. 인증

모든 action endpoint는 일반 Controller API와 동일한 인증을 사용한다.

```http
Authorization: Bearer <access_token>
```

401/403이면 Frontend는 action poller를 중지하고 재인증 플로우로 넘긴다.

## 4. Runtime Context

Controller의 sLLM action classifier는 사용자의 OS, 기본 쉘, 기본 브라우저, 캘린더 설정을 알아야 올바른 action을 만들 수 있다.

Frontend는 `/conversation/stream`, `/conversation/respond`, `/chat/stream` 호출 시 가능한 한 아래 헤더를 함께 보낸다.

```http
X-Client-Platform: macos
X-Client-Shell: zsh
X-Client-Browser: chrome
X-Client-Search-Engine: google
X-Client-Timezone: Asia/Seoul
X-Client-Calendar-Provider: apple_calendar
X-Client-Capabilities: open_url,browser.open,browser.navigate,browser.search,app.open,keyboard.type,keyboard.hotkey,terminal.run,calendar_control
X-Client-Enabled-Capabilities: browser.open,browser.navigate,browser.search
```

권장 값:

| 헤더                         | 값 예시                                                | 의미                                         |
| ---------------------------- | ------------------------------------------------------ | -------------------------------------------- |
| `X-Client-Platform`          | `macos`, `windows`, `linux`                            | 사용자의 OS                                  |
| `X-Client-Shell`             | `zsh`, `bash`, `powershell`, `cmd`                     | terminal action 기본 쉘                      |
| `X-Client-Browser`           | `chrome`, `safari`, `edge`, `firefox`                  | open_url/browser_control 기본 브라우저       |
| `X-Client-Search-Engine`     | `google`, `naver`, `duckduckgo`                        | `browser.search` URL 생성 기준               |
| `X-Client-Timezone`          | `Asia/Seoul`                                           | 캘린더/상대 시간 계산 기준                   |
| `X-Client-Calendar-Provider` | `apple_calendar`, `outlook`, `google_calendar`, `none` | 사용자가 설정한 캘린더 앱/프로바이더         |
| `X-Client-Capabilities`      | comma-separated list                                   | Frontend가 현재 지원하는 action type/command |
| `X-Client-Enabled-Capabilities` | comma-separated list                                | 사용자가 현재 허용한 v2 capability 목록      |

플랫폼별 기본 매핑:

| Platform  | 기본 shell   | 앱 실행                                | 브라우저              |
| --------- | ------------ | -------------------------------------- | --------------------- |
| `macos`   | `zsh`        | macOS app name 또는 bundle id          | Chrome/Safari/Firefox |
| `windows` | `powershell` | Start Menu app id/name 또는 executable | Chrome/Edge/Firefox   |
| `linux`   | `bash`       | desktop entry/app command              | Chrome/Firefox        |

규칙:

- sLLM은 이 context를 참고해 `terminal.target`, `open_url.args.browser`, `calendar_control.args.provider`를 선택한다.
- Frontend는 헤더에 선언하지 않은 capability의 action이 와도 실행 가능하면 실행해도 된다.
- 실행 불가능하면 `failed`로 결과를 제출한다.
- `X-Client-Calendar-Provider: none`이면 캘린더 생성/수정/삭제 action은 실행하지 않는다.

### 4.1 Runtime Profile 저장

Frontend는 사용자가 로그인한 뒤 현재 기기에서 실행 가능한 앱 목록과 터미널 실행 가능 여부를 Controller에 저장한다.
Controller는 이 정보를 사용자별로 저장하고, 이후 action intent 모델 context에 넣어 앱 실행/터미널 액션을 더 정확하게 생성한다.

```http
PUT /client/runtime-profile
Authorization: Bearer <access_token>
Content-Type: application/json
```

요청:

```json
{
    "platform": "macos",
    "default_browser": "chrome",
    "capabilities": [
        "open_url",
        "browser_control",
        "app_control",
        "keyboard_type",
        "hotkey",
        "terminal",
        "calendar_control"
    ],
    "applications": [
        {
            "id": "com.sublimetext.4",
            "name": "Sublime Text",
            "display_name": "Sublime Text",
            "aliases": ["sublime", "sublimetext", "서브라임"],
            "bundle_id": "com.sublimetext.4",
            "path": "/Applications/Sublime Text.app",
            "executable": "subl",
            "kind": "editor",
            "metadata": {}
        }
    ],
    "terminal": {
        "enabled": true,
        "shell": "zsh",
        "shell_path": "/bin/zsh",
        "cwd": "/Users/user",
        "env": {},
        "supports_pty": true,
        "requires_confirm": true,
        "timeout_seconds": 30
    },
    "metadata": {}
}
```

조회:

```http
GET /client/runtime-profile
Authorization: Bearer <access_token>
```

저장 규칙:

- 이 프로필은 사용자별로 저장된다. 다른 사용자에게 공유하지 않는다.
- Frontend는 앱 시작, 로그인 직후, 앱 목록 변경 감지 시 다시 `PUT /client/runtime-profile`을 호출한다.
- `applications[].name`은 필수다. `aliases`, `bundle_id`, `executable`은 가능한 경우 채운다.
- Windows는 `bundle_id` 대신 앱 사용자 모델 ID, 시작 메뉴 이름, 실행 파일 경로를 `id`, `path`, `executable`에 넣을 수 있다.
- macOS는 `bundle_id`와 `.app` 경로를 가능한 한 채운다.
- Linux는 desktop entry id 또는 실행 command를 `id`, `executable`에 넣는다.
- 터미널을 지원하지 않거나 사용자가 비활성화한 경우 `terminal.enabled=false`로 저장한다.

Action 생성 영향:

- `app_control/open`은 저장된 `applications`의 `name`, `display_name`, `aliases`를 우선 사용한다.
- 앱에 `bundle_id` 또는 `executable`이 있으면 Controller가 `args.bundle_id`/`args.executable`로 내려줄 수 있다.
- `terminal/execute`는 저장된 `terminal.enabled=true`일 때만 생성한다.
- 터미널 액션은 항상 `requires_confirm=true`다.

## 5. SSE 이벤트 규약

### Action-aware realtime acknowledgement

Action-like 사용자 메시지에서는 realtime 응답이 먼저 시작되며, workbench base prompt는 모델이 정확히 `잠시만요!`만 답하도록 요구한다. 이 텍스트는 실행 완료가 아니라 “요청을 받았고 action intent lane이 병렬로 판단 중”이라는 즉시 acknowledgement다.

Intent 결과가 `direct` 또는 `direct_sequence`로 확정되면 Controller는 action dispatch 전에 별도 `assistant_delta`로 `요청하신 작업 시작할게요`를 emit한다. Frontend는 이 문구 역시 실행 권한으로 해석하지 않고 UI 상태 문구로만 표시한다. 실제 실행 기준은 계속 `action_dispatch.action_id`와 pending queue envelope다.

Controller는 realtime의 `assistant_done` 직전/직후에 intent 결과가 아직 없으면 `JARVIS_ACTION_INTENT_DONE_GRACE_SECONDS`(기본 6.25초)까지 기다릴 수 있다. ActionCompiler 모델 요청 timeout 기본값은 `JARVIS_ACTION_INTENT_MODEL_TIMEOUT_SECONDS=6.0`이다. 테스트에서 “첫 이벤트 즉시성”을 검증할 때는 grace 환경변수를 낮춰 장시간 대기를 피한다.

### `action_intent`

sLLM action intent 판단 결과다. 액션 실행 여부를 UI에 표시하기 위한 이벤트다.

```json
{
    "should_act": true,
    "execution_mode": "direct",
    "intent": "app_control",
    "confidence": 0.93,
    "reason": "open app",
    "action_count": 1
}
```

`execution_mode` 값:

| 값                | 의미                                         |
| ----------------- | -------------------------------------------- |
| `direct`          | 단일 액션. 플래닝 없이 즉시 실행             |
| `direct_sequence` | 짧은 액션 시퀀스. 플래닝 없이 순서대로 실행  |
| `needs_plan`      | 조사/추론/복합 작업이 필요. deepthink로 이동 |
| `no_action`       | 일반 대화                                    |
| `unavailable`     | sLLM action classifier timeout/오류          |

### `action_dispatch`

Controller가 액션을 큐에 넣었음을 알리는 이벤트다. UI 표시용이며, 실제 실행은 반드시 pending queue에서 가져온 envelope 기준으로 한다.

```json
{
    "contract_version": "1.0",
    "action_id": "act_...",
    "request_id": "req_...",
    "action": {
        "type": "app_control",
        "command": "open",
        "target": "Sublime Text",
        "payload": null,
        "args": {},
        "description": "Sublime Text 열기",
        "requires_confirm": false,
        "step_id": null
    }
}
```

### `action_result`

Frontend가 제출한 실행 결과를 대화 stream으로 되돌려주는 이벤트다.

```json
{
    "action_id": "act_...",
    "request_id": "req_...",
    "status": "completed",
    "output": { "opened": "Sublime Text" },
    "error": null,
    "action": {
        "type": "app_control",
        "command": "open",
        "target": "Sublime Text",
        "payload": null,
        "args": {},
        "description": "Sublime Text 열기",
        "requires_confirm": false,
        "step_id": null
    }
}
```

### `actions`

한 대화 턴에서 실행된 전체 액션과 결과 모음이다.

```json
{
    "request_id": "req_...",
    "total": 1,
    "items": [{ "type": "app_control", "command": "open" }],
    "results": [{ "action_id": "act_...", "status": "completed" }]
}
```

### `assistant_done`

direct action에서는 모델 답변 생성을 생략하고 실행 결과 기반 완료 메시지를 보낸다.

```json
{
    "content": "요청한 작업을 실행했습니다.",
    "summary": "direct client action dispatched",
    "has_actions": true,
    "action_count": 1,
    "action_results": []
}
```

결과별 메시지 규칙:

- 전체 `completed`: 성공 메시지
- 일부만 `completed`: partial success 메시지
- 전체 `failed`: 실패 메시지와 첫 유용한 error
- `timeout`: timeout 메시지
- `rejected`: 사용자 거부 메시지
- invalid/suppressed: 실행하지 않았다는 명확한 메시지

실패, timeout, rejected, invalid, suppressed 상태에서 `"요청한 작업을 실행했습니다."`를 보내면 안 된다.

## 6. Queue Endpoint

### Action Registry 조회

Frontend는 하드코딩한 type 목록 대신 Controller registry를 기준으로 action handler를 맞춘다.

```http
GET /client/actions/registry
```

응답:

```json
{
    "contract_version": "1.0",
    "types": [
        {
            "type": "app_control",
            "commands": ["open", "focus", "close"],
            "description": "Open, focus, or close a local application.",
            "args": "{bundle_id?, wait_for_focus?}",
            "direct_intent": true
        }
    ],
    "aliases": {
        "launch_app": "app_control"
    },
    "v2": {
        "plan_modes": ["direct", "direct_sequence", "needs_plan", "no_action"],
        "capabilities": [
            {
                "name": "browser.search",
                "namespace": "browser",
                "args": "{query, browser?, search_engine?}",
                "requires_confirm": false,
                "v1": "open_url"
            }
        ]
    }
}
```

Frontend 규칙:

- registry의 `type`/`commands`를 handler dispatch table의 기준으로 사용한다.
- v2 `capabilities`는 compiler/validator용 표준 action 이름이다. Frontend queue 실행은 여전히 backend가 adapter를 거쳐 발급한 v1 `ClientAction`만 수행한다.
- registry에 없는 `type`은 실행하지 않고 `failed`로 보고한다.
- alias는 표시/마이그레이션 참고용이다. Frontend가 alias action을 직접 만들어 실행하지 않는다.
- 앱 실행은 `launch_app`이 아니라 `type=app_control`, `command=open`, `target=<app name>`이다.

### Pending 조회

```http
GET /client/actions/pending?limit=20
```

응답:

```json
[
    {
        "contract_version": "1.0",
        "action_id": "act_...",
        "request_id": "req_...",
        "action": {}
    }
]
```

Frontend 규칙:

- 같은 `action_id`는 한 번만 실행한다.
- 실행 시작 전에 로컬 in-flight set에 기록한다.
- 앱 재시작 등으로 같은 action이 다시 보이면 이미 완료한 action인지 확인한다.
- 빈 배열이면 다음 poll interval까지 대기한다.
- 429이면 backoff를 적용한다.
- 401/403이면 poller를 중지한다.
- pending queue에 없는 action은 실행하지 않는다.
- SSE `action_dispatch`를 받았더라도 가능하면 pending queue에서 같은 `action_id` envelope를 확인한 뒤 실행한다.
- UI 반응성을 위해 SSE envelope를 즉시 실행할 수는 있지만, 이 경우에도 반드시 백엔드 발급 `action_id`가 있어야 한다.

### 실행 결과 제출

```http
POST /client/actions/{action_id}/result
```

요청:

```json
{
    "contract_version": "1.0",
    "status": "completed",
    "output": {},
    "error": null
}
```

`status` 값:

| 값          | 의미                              |
| ----------- | --------------------------------- |
| `completed` | 실행 성공                         |
| `failed`    | 실행 시도했지만 실패              |
| `rejected`  | 사용자 거부 또는 보안 정책상 거부 |
| `timeout`   | Frontend 실행 제한 시간 초과      |

## 7. ClientAction 공통 스키마

```json
{
    "type": "open_url",
    "command": null,
    "target": "https://www.google.com",
    "payload": null,
    "args": { "browser": "chrome" },
    "description": "Chrome에서 Google 열기",
    "requires_confirm": false,
    "step_id": null
}
```

필드 의미:

| 필드               | 필수 | 의미                                          |
| ------------------ | ---: | --------------------------------------------- |
| `type`             |  yes | 액션 namespace                                |
| `command`          |   no | namespace 안의 구체 명령                      |
| `target`           |   no | URL, 앱 이름, 파일 경로, active_tab 등 대상   |
| `payload`          |   no | 입력 텍스트, 파일 내용, 클립보드 내용 등 body |
| `args`             |  yes | 구조화된 추가 인자                            |
| `description`      |  yes | 사용자에게 보여줄 설명                        |
| `requires_confirm` |  yes | 사용자 확인 필요 여부                         |
| `step_id`          |   no | deepthink plan step id                        |

## 8. Command 정의 원칙

`type`은 실행 영역이고 `command`는 그 영역 안의 verb다.

예:

```json
{"type": "browser_control", "command": "scroll"}
{"type": "app_control", "command": "open"}
{"type": "open_url", "command": null}
```

정의 규칙:

- 새 기능은 기존 `type`에 들어갈 수 있으면 `command`만 추가한다.
- 완전히 다른 실행 영역일 때만 새 `type`을 추가한다.
- `args`는 command별 JSON object로 고정한다.
- 의미가 같은 command를 중복으로 만들지 않는다.
- Frontend가 모르는 `type/command`는 실행하지 않고 `failed`로 보고한다.
- 위험 액션은 기본적으로 `requires_confirm=true`로 둔다.

## 9. 지원 액션 범위

현재 목표로 하는 1차 지원 범위는 아래다.

| 범위                        | action type                                                          | 주요 command                                                                                           | 예시 요청                                        |
| --------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------ |
| 브라우저 열기/검색          | `open_url`                                                           | `null`                                                                                                 | "브라우저 열어서 두부조림 검색해줘"              |
| 브라우저 탭/창 조작         | `browser_control`                                                    | `new_tab`, `new_window`, `close_tab`, `focus_address_bar`, `search`                                    | "새 탭 열어줘", "새 창에서 네이버 열어줘"        |
| 브라우저 페이지 조작        | `browser_control`                                                    | `scroll`, `back`, `forward`, `reload`, `extract_dom`, `click_element`, `type_element`, `select_result` | "두번째 결과 들어가줘", "스크롤 내려줘"          |
| 앱 실행                     | `app_control`                                                        | `open`                                                                                                 | "Sublime Text 열어줘"                            |
| 앱 실행 후 텍스트 작성      | `app_control` + `keyboard_type`                                      | `open` + typing                                                                                        | "Sublime Text 열어서 안녕하세요 작성해줘"        |
| 캘린더 앱/프로바이더 제어   | `calendar_control`                                                   | `open`, `list_events`, `create_event`, `update_event`, `delete_event`                                  | "내일 3시에 회의 일정 추가해줘"                  |
| 터미널/PowerShell 명령 실행 | `terminal`                                                           | `execute`                                                                                              | "PowerShell에서 npm 버전 확인해줘"               |
| 화면/키보드/단축키          | `screenshot`, `mouse_click`, `mouse_drag`, `keyboard_type`, `hotkey` | capture, click, drag, typing, hotkey                                                                   | "화면 캡처해줘", "현재 창에 안녕하세요 입력해줘" |
| 알림/클립보드               | `notify`, `clipboard`                                                | notify/copy/paste                                                                                      | "이 텍스트 클립보드에 복사해줘"                  |

## 10. 표준 Command Registry

### `open_url`

URL을 브라우저 또는 OS 기본 앱으로 연다.

```json
{
    "type": "open_url",
    "command": null,
    "target": "https://www.google.com/search?q=tofu",
    "args": { "browser": "chrome", "query": "두부조림 레시피" },
    "requires_confirm": false
}
```

Frontend 실행:

- `target` URL을 연다.
- `args.browser`가 있으면 해당 브라우저를 우선한다.
- 지원하지 않는 브라우저면 기본 브라우저로 fallback 가능하다.
- 현재 페이지의 검색 결과/링크를 선택하는 요청에는 `open_url`을 직접 만들지 않는다.
- 사용자가 "들어가줘", "열어줘", "선택해줘", "클릭해줘"처럼 현재 페이지의 항목을 지칭하면 `browser_control/extract_dom` 경로를 사용한다.

권장 output:

```json
{ "opened": "https://www.google.com/search?q=tofu", "browser": "Google Chrome" }
```

### `app_control/open`

앱을 실행하거나 포커스한다.

```json
{
    "type": "app_control",
    "command": "open",
    "target": "Sublime Text",
    "args": {},
    "requires_confirm": false
}
```

권장 output:

```json
{ "app": "Sublime Text", "opened": true, "focused": true }
```

### `browser_control/scroll`

현재 active tab 또는 브라우저 viewport를 스크롤한다.

```json
{
    "type": "browser_control",
    "command": "scroll",
    "target": "active_tab",
    "args": { "direction": "down", "amount": "page" },
    "requires_confirm": false
}
```

`args`:

- `direction`: `up` 또는 `down`
- `amount`: `page`, `half_page`, 숫자 pixel

권장 output:

```json
{ "scrolled": true, "direction": "down", "amount": "page" }
```

### `browser_control/back`, `forward`, `reload`

브라우저 navigation 명령이다.

```json
{
    "type": "browser_control",
    "command": "back",
    "target": "active_tab",
    "args": {},
    "requires_confirm": false
}
```

권장 output:

```json
{ "navigated": "back" }
```

### `browser_control/new_tab`, `new_window`, `close_tab`, `focus_address_bar`, `search`

브라우저의 탭/창/주소창/검색을 제어한다.

```json
{
    "type": "browser_control",
    "command": "new_tab",
    "target": "active_tab",
    "args": { "url": "https://www.naver.com" },
    "requires_confirm": false
}
```

```json
{
    "type": "browser_control",
    "command": "search",
    "target": "active_tab",
    "args": { "query": "두부조림 레시피", "new_tab": false },
    "requires_confirm": false
}
```

Frontend 규칙:

- `new_tab.args.url` 또는 `new_window.args.url`이 있으면 열면서 이동한다.
- URL 없이 `new_tab`/`new_window`가 오면 빈 탭/창을 연다.
- `search`는 현재 브라우저 context에서 검색한다. 새 Google 검색 결과 페이지를 바로 여는 동작은 `open_url`이 우선이다.
- `focus_address_bar`는 플랫폼별 주소창 단축키를 사용해도 된다.

### `browser_control/extract_dom`

현재 페이지에서 링크/입력 요소 후보를 추출한다. Controller는 이 결과를 보고 follow-up action을 만든다.

이 command는 현재 페이지를 기준으로 사용자의 지시를 해결하기 위한 1차 액션이다.

대표 요청:

- "초간단 마카롱 만들기 들어가줘"
- "백종원 소불고기 양념 맛있게 만드는법 열어줘"
- "두 번째 결과 선택해줘"
- "이 페이지에서 로그인 버튼 눌러줘"
- "검색창에 마카롱 입력해줘"

이런 요청에서 Frontend는 새 Google 검색을 실행하면 안 된다. Controller가 먼저 `extract_dom`을 요청하고, Frontend는 active tab의 DOM snapshot만 반환한다.

```json
{
    "type": "browser_control",
    "command": "extract_dom",
    "target": "active_tab",
    "args": {
        "purpose": "resolve_open_request",
        "query": "초간단 마카롱 만들기",
        "include_links": true,
        "include_elements": true,
        "max_links": 120
    },
    "requires_confirm": false
}
```

`purpose`:

| 값                     | 의미                           | follow-up                       |
| ---------------------- | ------------------------------ | ------------------------------- |
| `resolve_open_request` | 현재 페이지에서 열 링크 찾기   | `click_element` 또는 `open_url` |
| `resolve_type_request` | 현재 페이지에서 입력 요소 찾기 | `type_element`                  |

Frontend output:

```json
{
    "url": "https://www.google.com/search?q=...",
    "title": "Google Search",
    "links": [
        {
            "ai_id": 1,
            "text": "초간단 마카롱 만들기",
            "href": "https://example.com/macaron",
            "title": "초간단 마카롱 만들기",
            "ariaLabel": null
        }
    ],
    "elements": [
        {
            "ai_id": 10,
            "tag": "input",
            "role": "textbox",
            "type": "text",
            "text": "",
            "label": "검색어",
            "placeholder": "검색어 입력",
            "name": "q",
            "value": ""
        }
    ]
}
```

Result submit 예시:

```json
{
    "contract_version": "1.0",
    "status": "completed",
    "output": {
        "url": "https://www.google.com/search?q=...",
        "title": "검색 결과",
        "links": [
            {
                "ai_id": 1,
                "text": "백종원 소불고기 양념 맛있게 만드는법",
                "href": "https://example.com/recipe",
                "title": "백종원 소불고기 양념 맛있게 만드는법"
            }
        ],
        "elements": [
            {
                "ai_id": 10,
                "tag": "a",
                "role": "link",
                "text": "백종원 소불고기 양념 맛있게 만드는법",
                "href": "https://example.com/recipe"
            }
        ]
    },
    "error": null
}
```

Frontend DOM 추출 규칙:

- `ai_id`는 현재 DOM snapshot 안에서만 유효하다.
- `ai_id`는 clickable/input 가능한 요소에 부여한다.
- 숨김 요소, disabled 요소, 화면 밖 의미 없는 요소는 제외한다.
- 링크는 `href`, `text`, `title`, `ariaLabel`을 가능한 한 채운다.
- 입력 요소는 `tag`, `role`, `type`, `label`, `placeholder`, `name`, `value`를 가능한 한 채운다.
- 검색 결과 페이지에서는 광고/탭/필터보다 organic result 링크가 먼저 오도록 정렬한다.
- 동일한 href가 여러 번 나오면 가장 사람이 읽기 좋은 text/title을 가진 항목을 우선한다.
- DOM snapshot을 반환한 뒤 Controller의 follow-up action을 기다린다.
- Frontend가 DOM 결과를 보고 임의로 클릭하지 않는다.

### `browser_control/click_element`

`extract_dom`에서 받은 `ai_id` 요소를 클릭한다.

```json
{
    "type": "browser_control",
    "command": "click_element",
    "target": "active_tab",
    "args": { "ai_id": 1 },
    "requires_confirm": false
}
```

권장 output:

```json
{ "clicked": true, "ai_id": 1, "url": "https://example.com/macaron" }
```

실패 조건:

- `ai_id`가 현재 snapshot에 없음
- 요소가 사라짐
- 요소가 disabled
- 클릭 후 navigation이 timeout

### `browser_control/type_element`

`extract_dom`에서 받은 입력 요소에 텍스트를 입력한다.

```json
{
    "type": "browser_control",
    "command": "type_element",
    "target": "active_tab",
    "payload": "안녕하세요",
    "args": { "ai_id": 10, "enter": false },
    "requires_confirm": false
}
```

권장 output:

```json
{ "typed": true, "ai_id": 10, "text_length": 5, "submitted": false }
```

### `browser_control/select_result`

검색 결과 페이지의 N번째 결과를 선택한다. DOM 기반 `extract_dom -> click_element`가 우선이며, 이 command는 compatibility 용도다.

```json
{
    "type": "browser_control",
    "command": "select_result",
    "target": "active_tab",
    "args": { "index": 2 },
    "requires_confirm": false
}
```

### `keyboard_type`

현재 활성 창에 텍스트를 입력한다.

```json
{
    "type": "keyboard_type",
    "command": null,
    "payload": "안녕하세요",
    "args": { "enter": false },
    "requires_confirm": false
}
```

권장 output:

```json
{ "typed": true, "text_length": 5, "enter": false }
```

### `hotkey`

단축키를 입력한다.

```json
{
    "type": "hotkey",
    "command": null,
    "args": { "keys": "cmd,l" },
    "requires_confirm": false
}
```

권장 output:

```json
{ "pressed": "cmd,l" }
```

플랫폼별 key 이름:

| Platform  | 예시                                    |
| --------- | --------------------------------------- |
| `macos`   | `cmd,l`, `cmd,c`, `cmd,v`               |
| `windows` | `ctrl,l`, `ctrl,c`, `ctrl,v`, `alt,tab` |
| `linux`   | `ctrl,l`, `ctrl,c`, `ctrl,v`, `alt,tab` |

### `screenshot`, `mouse_click`, `mouse_drag`

화면 기반 제어다. 웹페이지 내부 요소 선택은 가능한 한 `browser_control/extract_dom -> click_element/type_element`가 우선이고, 좌표 기반 제어가 필요한 경우에만 사용한다.

```json
{
    "type": "screenshot",
    "command": null,
    "args": { "region": null },
    "requires_confirm": false
}
```

```json
{
    "type": "mouse_click",
    "command": null,
    "args": { "x": 500, "y": 300, "button": "left", "clicks": 1 },
    "requires_confirm": false
}
```

```json
{
    "type": "mouse_drag",
    "command": null,
    "args": { "start_x": 100, "start_y": 100, "end_x": 400, "end_y": 400 },
    "requires_confirm": false
}
```

Frontend 규칙:

- 좌표는 현재 디스플레이 좌표계를 기준으로 한다.
- 다중 모니터 환경에서는 output에 사용한 display id/scale 정보를 가능하면 포함한다.
- destructive UI 클릭 가능성이 있으면 Frontend local policy로 confirmation을 추가할 수 있다.

### `terminal/execute`

사용자의 기본 shell에서 명령을 실행한다.

```json
{
    "type": "terminal",
    "command": "execute",
    "target": "powershell",
    "payload": "Get-ChildItem",
    "args": {
        "cwd": null,
        "timeout": 30,
        "env": {},
        "elevated": false
    },
    "requires_confirm": true
}
```

플랫폼별 target:

| Platform  | target       | payload 예시    |
| --------- | ------------ | --------------- |
| `windows` | `powershell` | `Get-ChildItem` |
| `windows` | `cmd`        | `dir`           |
| `macos`   | `zsh`        | `ls -la`        |
| `linux`   | `bash`       | `ls -la`        |

규칙:

- `payload`는 실행할 command string이다.
- `target`은 shell 이름이다. 없으면 `X-Client-Shell`을 사용한다.
- `terminal/execute`는 기본적으로 `requires_confirm=true`다.
- `elevated=true` 요청은 Frontend policy로 별도 확인하거나 거부한다.
- output에는 stdout/stderr 전체를 무제한으로 넣지 않고 필요한 길이로 제한한다.

권장 output:

```json
{
    "exit_code": 0,
    "stdout": "v10.9.0",
    "stderr": "",
    "shell": "powershell",
    "cwd": "C:\\Users\\user"
}
```

### `calendar_control/open`

사용자가 설정한 캘린더 앱 또는 프로바이더를 연다.

```json
{
    "type": "calendar_control",
    "command": "open",
    "target": "default",
    "args": { "provider": "apple_calendar" },
    "requires_confirm": false
}
```

### `calendar_control/list_events`

지정 기간의 일정을 조회한다.

```json
{
    "type": "calendar_control",
    "command": "list_events",
    "target": "default",
    "args": {
        "provider": "google_calendar",
        "calendar_id": "primary",
        "start": "2026-04-30T00:00:00+09:00",
        "end": "2026-04-30T23:59:59+09:00",
        "timezone": "Asia/Seoul"
    },
    "requires_confirm": false
}
```

권장 output:

```json
{
    "events": [
        {
            "event_id": "evt_1",
            "calendar_id": "primary",
            "title": "회의",
            "start": "2026-04-30T15:00:00+09:00",
            "end": "2026-04-30T16:00:00+09:00"
        }
    ]
}
```

### `calendar_control/create_event`

새 일정을 생성한다.

```json
{
    "type": "calendar_control",
    "command": "create_event",
    "target": "default",
    "args": {
        "provider": "outlook",
        "calendar_id": "primary",
        "title": "회의",
        "start": "2026-04-30T15:00:00+09:00",
        "end": "2026-04-30T16:00:00+09:00",
        "timezone": "Asia/Seoul",
        "location": "Zoom",
        "notes": "프로젝트 회의"
    },
    "requires_confirm": true
}
```

### `calendar_control/update_event`

기존 일정을 수정한다.

```json
{
    "type": "calendar_control",
    "command": "update_event",
    "target": "default",
    "args": {
        "provider": "google_calendar",
        "calendar_id": "primary",
        "event_id": "evt_1",
        "title": "변경된 회의",
        "start": "2026-04-30T16:00:00+09:00",
        "end": "2026-04-30T17:00:00+09:00",
        "timezone": "Asia/Seoul"
    },
    "requires_confirm": true
}
```

### `calendar_control/delete_event`

일정을 삭제한다.

```json
{
    "type": "calendar_control",
    "command": "delete_event",
    "target": "default",
    "args": {
        "provider": "apple_calendar",
        "calendar_id": "primary",
        "event_id": "evt_1"
    },
    "requires_confirm": true
}
```

Calendar 규칙:

- `provider`가 없으면 `X-Client-Calendar-Provider`를 사용한다.
- `timezone`이 없으면 `X-Client-Timezone`을 사용한다.
- `create_event`, `update_event`, `delete_event`는 항상 확인이 필요하다.
- `calendar_provider=none`이면 `failed` 또는 `rejected`로 보고한다.
- 상대 날짜는 Controller/sLLM이 ISO-8601로 변환해서 보내는 것을 목표로 한다.
- 시간이 불명확하면 action을 실행하지 않고 사용자에게 clarification이 필요하다.

### `clipboard`

클립보드에 텍스트를 복사하거나 붙여넣는다.

```json
{
    "type": "clipboard",
    "command": "copy",
    "payload": "복사할 텍스트",
    "args": {},
    "requires_confirm": false
}
```

### `notify`

사용자에게 알림을 표시한다.

```json
{
    "type": "notify",
    "command": null,
    "payload": "작업이 완료되었습니다.",
    "args": { "level": "info" },
    "requires_confirm": false
}
```

### `screenshot`

화면 캡처를 수행한다. 이미지가 크면 output에는 메타데이터 또는 저장 경로를 우선 사용한다.

```json
{
    "type": "screenshot",
    "command": null,
    "args": { "region": null },
    "requires_confirm": false
}
```

권장 output:

```json
{
    "captured": true,
    "mime_type": "image/png",
    "image_base64": "<optional>",
    "path": "<optional local path>"
}
```

### `file_write`, `file_read`, `web_search`

이 타입들은 contract에는 존재하지만 direct action classifier의 기본 출력 대상은 아니다.

Frontend 정책:

- 구현되어 있지 않으면 `failed`로 보고한다.
- `file_write`는 기본적으로 `requires_confirm=true`를 요구한다.
- 파일 경로는 sandbox/허용 디렉터리 정책을 Frontend가 적용한다.
- `web_search`를 받았다고 해서 프론트가 임의로 브라우저 새 탭을 열면 안 된다.
- 브라우저에서 검색 페이지를 여는 동작은 `open_url` 액션으로만 수행한다.
- 현재 페이지의 검색 결과/링크 선택은 `web_search`가 아니라 `browser_control/extract_dom`으로 처리한다.
- assistant text 안의 `web_search` 예시는 표시 전용이며 실행 금지다.

## 11. Confirmation 규약

`requires_confirm=true`이면 Frontend는 실행 전에 사용자에게 확인 UI를 띄운다.

사용자 승인:

- 액션 실행
- 결과를 `completed` 또는 `failed`로 제출

사용자 거부:

```json
{
    "contract_version": "1.0",
    "status": "rejected",
    "output": {},
    "error": "user rejected action"
}
```

Controller는 현재 confirm 승인용 별도 endpoint를 요구하지 않는다. 확인은 Frontend local policy로 처리하고, 최종 result만 제출한다.

## 12. Timeout 규약

Frontend는 command별 timeout을 둔다.

권장 기본값:

| command                         | timeout |
| ------------------------------- | ------: |
| `open_url`                      |     10s |
| `app_control/open`              |     10s |
| `browser_control/scroll`        |      3s |
| `browser_control/new_tab`       |      5s |
| `browser_control/new_window`    |      5s |
| `browser_control/close_tab`     |      3s |
| `browser_control/search`        |      8s |
| `browser_control/extract_dom`   |      5s |
| `browser_control/click_element` |      8s |
| `browser_control/type_element`  |      5s |
| `screenshot`                    |      5s |
| `mouse_click`                   |      3s |
| `mouse_drag`                    |      5s |
| `keyboard_type`                 |      5s |
| `terminal/execute`              |     30s |
| `calendar_control/open`         |     10s |
| `calendar_control/list_events`  |     10s |
| `calendar_control/create_event` |     15s |
| `calendar_control/update_event` |     15s |
| `calendar_control/delete_event` |     15s |

timeout 시:

```json
{
    "contract_version": "1.0",
    "status": "timeout",
    "output": {},
    "error": "client action execution timed out"
}
```

## 13. Error 규약

`failed`는 실행 시도 후 실패한 경우다.

```json
{
    "contract_version": "1.0",
    "status": "failed",
    "output": {
        "type": "browser_control",
        "command": "click_element"
    },
    "error": "ai_id not found in current DOM snapshot"
}
```

Frontend는 error를 사람이 읽을 수 있게 작성한다. Controller는 error 문자열을 그대로 SSE `action_result`에 포함한다.

## 14. Idempotency

- `action_id`는 액션 실행의 idempotency key다.
- Frontend는 같은 `action_id`를 중복 실행하지 않는다.
- 결과 제출이 네트워크 오류로 실패하면 같은 result body로 재시도할 수 있다.
- 이미 완료된 action이 다시 pending에 보이면 실행하지 않고 마지막 결과 제출을 재시도한다.

## 15. Security

Frontend는 다음을 반드시 지킨다.

- 알 수 없는 `type/command`는 실행하지 않는다.
- assistant 응답 텍스트 안의 액션 JSON은 실행하지 않는다.
- Controller가 발급한 `action_id` 없는 액션은 실행하지 않는다.
- 프론트가 `embedded_...` action id를 만들어 실행하지 않는다.
- `requires_confirm=true`는 사용자 승인 전 실행하지 않는다.
- `terminal`, `file_write`, 외부 앱 실행, 민감한 URL 열기는 local policy를 적용한다.
- `calendar_control/create_event`, `update_event`, `delete_event`는 사용자 확인 전 실행하지 않는다.
- `terminal/execute`는 사용자 확인 전 실행하지 않는다.
- DOM `ai_id`는 현재 페이지 snapshot에 한정한다.
- 페이지가 바뀐 뒤 과거 `ai_id`로 클릭하지 않는다.
- output에 secret, token, full file content를 무분별하게 넣지 않는다.

## 16. Frontend Handler Interface 권장 형태

```ts
type ClientActionEnvelope = {
    contract_version: "1.0";
    action_id: string;
    request_id: string;
    action: ClientAction;
};

type ClientAction = {
    type: string;
    command?: string | null;
    target?: string | null;
    payload?: string | null;
    args: Record<string, unknown>;
    description: string;
    requires_confirm: boolean;
    step_id?: string | null;
};

type ClientActionResult = {
    contract_version: "1.0";
    status: "completed" | "failed" | "rejected" | "timeout";
    output: Record<string, unknown>;
    error?: string | null;
};

type ActionHandler = (
    envelope: ClientActionEnvelope,
) => Promise<ClientActionResult>;
```

Dispatcher 예시:

```ts
async function executeAction(
    envelope: ClientActionEnvelope,
): Promise<ClientActionResult> {
    const { action } = envelope;

    if (action.requires_confirm) {
        const approved = await confirmAction(action.description);
        if (!approved) {
            return {
                contract_version: "1.0",
                status: "rejected",
                output: {},
                error: "user rejected action",
            };
        }
    }

    const key = `${action.type}/${action.command ?? ""}`;
    const handler = handlers[key] ?? handlers[action.type];

    if (!handler) {
        return {
            contract_version: "1.0",
            status: "failed",
            output: { type: action.type, command: action.command },
            error: `unsupported action: ${key}`,
        };
    }

    return handler(envelope);
}
```

## 17. Poller 권장 형태

```ts
async function pollActions() {
    const pending = await getPendingActions({ limit: 20 });

    for (const envelope of pending) {
        if (seenActionIds.has(envelope.action_id)) continue;
        seenActionIds.add(envelope.action_id);

        const result = await executeAction(envelope);
        await submitActionResult(envelope.action_id, result);
    }
}
```

## 18. 버전 관리

현재 contract version은 `1.0`이다.

호환성 규칙:

- 필드 추가는 minor-compatible로 본다.
- `type/command` 의미 변경은 breaking change다.
- breaking change가 필요하면 `contract_version`을 올린다.
- Frontend는 모르는 필드를 무시할 수 있어야 한다.
