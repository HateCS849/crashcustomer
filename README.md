# CrashCustomer

행사 신청이라는 목표를 받고 실제 브라우저를 조작해 서비스 계약 위반을 찾습니다. 발견한 동작은 **새 브라우저 + 새 서버 세션에서 재실행**하고, 재현 결과·스크린샷·네트워크 상태·Playwright Trace를 남깁니다.

기존 `../all_total_vue_student`와 별개인 신규 프로젝트입니다.

제출 전 기술 검증은 [평가 기준 대응·동일 입력 전후 실측](docs/SUBMISSION_READINESS.md), [실제 아키텍처](docs/ARCHITECTURE.md), [설계 결정 기록](docs/ADR.md)에 정리합니다. 결과 JSON·실행 ID·반복 횟수와 미검증 범위도 함께 보존합니다. LangSmith 실제 수신이 확인되지 않은 상태를 완료로 표시하지 않습니다.

기능은 세 가지입니다.

| 화면 | 하는 일 |
|---|---|
| 등록한 프로젝트 · **배포 전 장애 사전 점검** | 로컬·스테이징에 띄운 앱의 화면을 브라우저로 수행하며 배포 후 흔한 장애 10종(서버 오류, 인증 만료, 요청 제한, 끊김·지연, 응답 형식 변경, 정적 파일 실패, 지속 장애, 중복 처리, 트래픽 급증, 잘못된 입력)을 주입·측정하고 결함을 재현합니다. |
| 내장 데모 · 행사 신청 | 결함을 심어둔 합성 폼으로 탐색·판정·재현 파이프라인을 검증합니다. |
| 로컬 실험실 · 장애 전파 시뮬레이터 | 외부 지연이 DB 커넥션 풀을 통해 다른 API로 번지는지 실제 HTTP와 SQLite 풀로 개선 전/후를 측정합니다. |

## 내장 데모의 범위

- 웹 대시보드: 목표, 테스트 버전, 실행 방식 선택 / 실행 기록 / 화면 / 결함 / 타임라인 / 보고서 다운로드.
- 테스트 대상: 직접 만든 행사 신청 폼 하나. 외부 예약이나 사용자 데이터 전송은 없습니다.
- 결함: 오류 후 입력 소실, 재시도 버튼 잠김, 실패 요청을 성공으로 표시.
- 모든 버전에서 첫 유효 제출은 동일하게 HTTP 503을 반환하고, 이후에는 성공합니다. 503 자체는 결함으로 판단하지 않습니다.
- LangGraph: `observe → assess → decide → act → observe`. 결함 후보 발견 시 독립 재현을 수행합니다.
- 실제 브라우저 동작: Playwright + Chromium 또는 설치된 Chrome의 새 임시 프로필.
- 저장: SQLite, PNG 증거, 탐색/재현 Trace ZIP, Markdown 보고서.
- Ollama: 실제 LLM이 관찰된 화면과 이력을 보고 다음 행동 하나를 JSON으로 결정합니다.
- 규칙 기반 모드: 이름·이메일·동의 입력과 재시도를 수행하는 **AI가 아닌 기준선**입니다. 모델 없이 브라우저·검증·재현 인프라를 확인할 수 있습니다.

현재 시나리오는 유효한 정보 입력 → 일시적 오류 → 복구/재시도로 고정되어 있습니다. AI가 모든 종류의 테스트 시나리오를 스스로 생성하는 범용 서비스는 아직 아닙니다. 결함 판정은 LLM 의견 대신 명시된 서비스 계약에 대한 코드 검사를 사용합니다.

## 실행

Python 3.11 이상이 필요합니다. 이 머신에서는 Python 3.12 가상환경을 생성했습니다.

```bash
cd crashcustomer
uv sync --extra dev --locked
cp .env.example .env
bash scripts/run.sh
```

`uv`를 사용하지 않는 경우:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
bash scripts/run.sh
```

대시보드: <http://127.0.0.1:8710> · API: <http://127.0.0.1:8710/docs>

macOS에서는 `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`이 있으면 자동으로 사용합니다. 개인 Chrome 프로필은 사용하지 않습니다. 다른 환경에서는 Playwright Chromium을 준비하세요.

```bash
.venv/bin/python -m playwright install chromium
```

다른 Chrome 실행 파일을 쓰려면 `.env`의 `CRASHCUSTOMER_BROWSER_EXECUTABLE`에 절대 경로를 지정합니다. 실행 화면을 직접 보려면 `CRASHCUSTOMER_HEADLESS=false`로 설정한 뒤 서버를 다시 시작합니다.

## Ollama AI 모드

기본 모델은 `qwen3:8b`입니다. 모델 선택은 환경설정 예시이며, 최신·최적 모델이라는 의미는 아닙니다. 이미 준비한 다른 모델도 사용할 수 있습니다.

```bash
ollama serve
# 다른 터미널에서, 모델이 없을 때만:
ollama pull qwen3:8b
```

`.env`에서 다음을 설정하고 CrashCustomer 서버를 다시 시작합니다.

```dotenv
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3:8b
CRASHCUSTOMER_BACKEND=ollama
```

화면의 실행 방식을 `Ollama AI`로 선택합니다. 서버 연결이나 모델이 준비되지 않았으면 시작을 거부하며, 규칙 모드로 조용히 바꾸지 않습니다. 모델의 잘못된 JSON, 없는 컨트롤 선택, 브라우저 오류는 `Agent 실행 오류`로 구분합니다. 모델 호출당 90초, 전체 실행 300초 제한이 있습니다.

모델에는 목표·계약·렌더링된 화면 구조·입력값·동작 이력·네트워크 상태·현재 가능한 행동 목록만 전달합니다. 선택한 결함 이름, 테스트 서버 코드, DB, 정답표는 전달하지 않습니다. HTML 필수 입력이 완료되기 전에는 제출을 후보로 제공하지 않으며, 모델은 가능한 행동 중 순서와 입력값을 결정합니다. 이 제약은 이름·이메일·동의가 필요한 현재 폼을 대상으로 하며, 유효하지 않은 입력 자체를 시험하는 탐색은 아직 지원하지 않습니다. 첫 버전은 화면 캡처를 증거로 저장하지만 **이미지를 LLM에 보내는 비전 기반 제어는 아닙니다.**

## 프로젝트 등록과 첨부

대시보드 상단의 **프로젝트 등록**에서 이름·설명과 선택적인 API/브라우저 주소를 저장합니다. 프로젝트 선택 목록에서 내장 데모와 등록한 프로젝트를 전환할 수 있습니다. 등록한 프로젝트에서는 자료 관리 화면이 표시됩니다.

1. 프로젝트를 만들거나 선택합니다.
2. 자료 종류를 선택하고 **파일 선택** 또는 드래그 앤 드롭으로 파일을 고릅니다. 여러 파일을 함께 고를 수 있습니다.
3. **선택한 파일 첨부**를 누릅니다. 파일별로 저장되며 실패한 파일은 이유를 표시하고 재시도 목록에 남깁니다.
4. 첨부 목록에서 파일을 눌러 분석 결과·원문을 확인합니다. 다운로드하거나 첨부 사본을 제거할 수 있습니다. 원본 파일은 수정하지 않습니다.

지원 형식:

| 자료 | 지원 및 처리 |
|---|---|
| OpenAPI | JSON/YAML의 OpenAPI 3.x·Swagger 2.0 인식, API 경로·인증 요구·필수 파라미터 표시. 외부 `$ref`를 가져오지 않습니다. |
| 실험 계획 | JSON/YAML 객체를 저장하고 원문 표시. 별도로 제공하는 `test-plan.yaml` 예시를 다운로드할 수 있습니다. |
| 코드·문서 | Markdown, TXT, Java, Kotlin, Python, JS/TS, HTML/CSS, Gradle, XML, SQL, CSV, properties 원문 미리보기. |
| ZIP | 내부 파일 목록과 크기를 확인하고 보관. 추출·빌드·코드 실행은 수행하지 않습니다. |
| PDF·XLSX | 참고 파일로 보관·다운로드. PDF 페이지나 셀 내용 자동 분석은 미지원입니다. |

파일당 최대 20MB, 텍스트 파일은 2MB, 프로젝트당 30개입니다. 원문 미리보기는 12,000자, API/ZIP 목록 표시는 200개로 제한합니다. ZIP은 항목 1,000개·총 비압축 크기 50MB까지 지원하며 경로 이탈, 심볼릭 링크, 암호화, 중첩 ZIP, `.git`·`.venv`·`node_modules` 등은 거부합니다. YAML 별칭과 실행 태그는 지원하지 않습니다.

개인키·접근키·실제 `.env` 파일은 첨부 전에 제외하세요. 알려진 파일명과 개인키/AWS 접근키 패턴을 검사하지만 모든 종류의 비밀값을 탐지하는 기능은 아닙니다. 소스 폴더는 필요한 파일만 선별해 ZIP으로 첨부하는 것을 권합니다.

프로젝트·첨부 메타데이터는 기존 SQLite DB, 원본 사본은 `data/uploads/<generated-id>`에 저장합니다. 업로드 파일은 공개 정적 폴더에 두지 않고 프로젝트 범위의 다운로드 API로만 제공합니다. 프로젝트 주소는 저장 시 호출하지 않습니다. 첨부 내용은 사전 점검의 계획 에이전트에만 검색 결과(최대 5조각, 조각당 500자)로 전달되며, 참고 데이터로 표시하고 명령으로 실행하지 않습니다.

등록한 프로젝트는 아래 **배포 전 장애 사전 점검**으로 실행합니다. `POST /api/runs`에 프로젝트 ID를 넣으면 409를 반환하며, 내장 데모를 외부 프로젝트의 결과처럼 실행하지 않습니다.

API: `POST/GET /api/projects`, `GET/PATCH /api/projects/{id}`, `POST /api/projects/{id}/attachments`, 첨부별 `/preview`·`/download` 및 `DELETE`. 세부 스키마는 `/docs`에서 확인합니다. 이 앱은 기존과 같이 로컬 실행용이며 다중 사용자 인증·프로젝트 권한 제어는 아직 제공하지 않습니다.

## 배포 전 장애 사전 점검

등록한 앱이 배포 후 자주 겪는 장애에서 화면과 API가 사용자를 어떻게 대하는지 배포 전에 확인합니다. 화면 쪽 장애는 앱 코드를 고치지 않고 브라우저 계층(Playwright route·DevTools)에서 주입하고, API 쪽은 관찰된 요청으로 직접 측정합니다.

| # | 배포 후 흔한 장애 | 재현 방법 |
|---|---|---|
| 1 | 외부 의존성·서버 일시 오류 | 관찰된 API에 HTTP 503 / 500 응답 |
| 2 | 네트워크 불안정 | 연결 끊김, 3초 지연, 화면 전체 느린 네트워크(지연 800ms·50KB/s) |
| 3 | 세션·인증 만료 | HTTP 401 응답 |
| 4 | 요청 제한 | HTTP 429 + Retry-After |
| 5 | 프론트·백엔드 버전 불일치 | 실제 응답의 필드 이름을 바꿔 전달(`order_id` → `order_id_v2`) |
| 6 | 정적 파일 로딩 실패 | 같은 출처 스크립트·스타일 차단 |
| 7 | 지속 장애 | 같은 API에 503을 12회 연속 |
| 8 | 중복 처리 | 응답 지연 중 제출 버튼 두 번 클릭 후 서버가 몇 번 성공 처리했는지 확인 |
| 9 | 트래픽 급증 | 조회 API·화면 주소에 동시 사용자 1→5→10→20명 단계 부하 (GET만, 사전 응답 검증 포함) |
| 10 | 잘못된 입력에 500 | 관찰된 요청 본문에서 필드 누락·형식 오류·1만 자 입력을 만들어 전송 |

서버 재시작·DB 장애·메모리/디스크 고갈·설정 오류·인증서 만료는 인프라 통제가 필요해 아직 재현하지 않습니다.

```bash
# 예시 대상 앱 (다른 터미널)
.venv/bin/python examples/shop_app.py   # http://127.0.0.1:8720/buggy , /fixed
```

1. **프로젝트 등록**에서 브라우저 테스트 주소에 `http://127.0.0.1:8720/buggy`를 입력합니다.
2. 선택으로 `examples/shop-openapi.yaml`, `examples/shop-runbook.md`를 첨부합니다. 시나리오 선정 근거로 검색됩니다.
3. **연결 확인**으로 허용 호스트 여부와 앱 응답을 확인하고, 사용자 목표를 적은 뒤 **사전 점검 시작하기**를 누릅니다.

진행 순서(LangGraph 감독 그래프):

현재 흐름은 `route → recon → gate → retrieve → plan → replan_check → attack → probe → write → validate → finalize`입니다. 일반 사용 안내는 `route → chat_answer → finalize`로 분기합니다. 기준 실행의 목표가 미완료이면 `gate → write → validate → finalize`로 종료하고 장애 주입·부하를 수행하지 않습니다. 계획 누락은 기본 최대 1회 재계획하고, 여전히 누락되면 규칙으로 보충한 출처를 표시합니다. 설정한 시나리오 예산 밖의 장애 유형은 미점검으로 표시합니다.

결과 상태는 `completed`(이번 범위 검증 완료), `partial`(미점검·재현 부족·측정 무효), `inconclusive`(기준 경로 미완료), `error`(실행 오류)로 구분합니다. 최종 validator는 기준선, 실제 장애 적용, 재현 근거, 점검 범위, 측정 유효성, 요약 수치 일치를 검사합니다.

테스트 Bearer 토큰을 입력하면 같은 출처 브라우저와 API 측정에 적용합니다. 토큰은 실행 중 메모리에만 두고 설정·보고서에 저장하지 않으며 종료 시 제거합니다. 인증 사용 실행은 인증 헤더가 포함될 수 있는 Playwright Trace 저장을 끕니다. 화면 캡처와 관찰은 계속 저장되므로 합성 테스트 계정을 사용하세요. OAuth·CAPTCHA 자동 로그인과 쿠키 기반 세션 가져오기는 아직 지원하지 않습니다. 부하는 사전 2xx 응답을 확인하며, 관찰된 JSON API가 HTML 로그인 페이지를 반환하면 무효 처리합니다. 401/403을 정상 성능으로 집계하지 않습니다. JSON 본문의 비즈니스 의미·OpenAPI 스키마 전체 검증까지 제공하는 것은 아닙니다.

아래 표는 각 단계의 역할을 설명합니다:

| 단계 | 에이전트 | 내용 |
|---|---|---|
| recon | 탐색 | 장애 없이 목표 경로를 수행하고 화면이 호출한 같은 출처 API를 기록합니다. 이 실행이 기준선입니다. |
| retrieve | 검색 (RAG) | 첨부한 OpenAPI·운영 문서를 BM25(설정 시 임베딩 혼합)로 검색합니다. |
| plan | 계획 | 관찰된 API × 장애 유형 후보 중 시나리오를 고릅니다. AI 모드는 후보 ID enum으로 제한된 JSON만 출력할 수 있습니다. |
| attack | 탐색 + 검증 | 시나리오마다 새 브라우저에서 장애를 주입하고 같은 목표를 수행합니다. 계약 위반이 나오면 새 브라우저에서 재현합니다. |
| probe | API 측정 | 관찰된 요청 본문으로 입력 검증(최대 30건), 조회 API·화면 주소로 단계별 부하를 측정합니다. 5xx는 재전송해 반복될 때만 보고합니다. |
| summarize | 요약 | 시나리오별 결함·재현 여부·위험 점수·LLM 사용량을 집계하고 Markdown 보고서를 만듭니다. |

판정하는 계약(코드 검사, LLM 의견 아님):

| 코드 | 결함 |
|---|---|
| `silent_failure` | 요청이 실패했는데 오류 안내가 없음 |
| `input_loss` | 실패 후 입력값·선택 상태 소실 |
| `retry_locked` | 실패 처리 후 제출 버튼이 잠김 |
| `false_success` | 실패한 요청을 성공 문구로 표시 |
| `double_submit` | 응답 대기 중 같은 버튼을 다시 누를 수 있음 |
| `unhandled_error` | 실패 처리 중 처리되지 않은 스크립트 예외 |
| `stuck_loading` | 8초가 지나도 `aria-busy` 로딩이 끝나지 않음 |
| `auth_expiry_unhandled` | 401에 다시 로그인하라는 안내 없이 일반 오류만 표시 |
| `broken_render` | 응답 형식이 바뀌자 `undefined`·`NaN`·`null`을 화면에 표시 |
| `retry_storm` | 실패한 요청을 한 화면 갱신 사이에 5회 넘게 즉시 재시도 |
| `duplicate_processing` | 같은 변경 요청에 2번 이상 2xx 응답. 실제 데이터 중복 생성은 별도 검증 필요 |
| `asset_failure_silent` | 스크립트·스타일을 불러오지 못했는데 안내가 없음 |
| `input_validation_500` | 잘못된 요청 본문에 4xx 대신 5xx (재전송에서도 반복) |
| `load_slo_breach` | 단계 부하에서 오류율 5% 또는 P95 목표(기본 1초) 초과 |

**안전장치**: 실행 대상은 `localhost`·`127.0.0.1`·`::1`·`*.localhost`·`*.test`만 기본 허용합니다. 스테이징은 `.env`의 `CRASHCUSTOMER_ALLOWED_HOSTS`에 직접 추가해야 하며, CrashCustomer 자신의 주소는 거부합니다. 시작 전 앱 응답을 확인하고, 브라우저는 대상과 같은 출처로만 요청할 수 있습니다. 장애는 브라우저가 실제로 호출한 API에만 주입합니다. 사전 점검은 한 번에 1개입니다. 장애 시나리오는 점검별로 기본 2개씩(`scenario_concurrency`, 최대 4) 동시에 실행하고, 서버 전체 브라우저 수는 `CRASHCUSTOMER_BROWSER_SLOTS`(기본 2, 최대 4)로 제한합니다. 부하는 GET만, 최대 동시 50명·단계당 사용자별 3요청으로 제한하고, 입력 검증은 잘못된 본문만 최대 30건 보냅니다. **운영 주소를 허용 목록에 넣지 마세요.** 점검은 실제로 폼을 제출하며, 응답 형식 변경·중복 클릭 시나리오와 서버가 느슨하게 받아들인 검증 요청은 실제 데이터를 만들 수 있습니다.

평가: `scripts/evaluate_campaign.py --backend rule|ollama`가 buggy/fixed를 각각 점검하고 정답표와 비교합니다. 측정 결과는 [평가 기록](docs/EVALUATION.md)에 있습니다.

API: `GET /api/projects/{id}/connection`, `POST/GET /api/projects/{id}/campaigns`, `GET /api/campaigns/{id}`, `GET /api/campaigns/{id}/report`.

## 로컬 실험실

프로젝트 선택에서 `로컬 실험실 · 장애 전파 시뮬레이터`를 고릅니다. 로그인 사용자 수·커넥션 풀 크기·외부 응답 지연 등을 정하면, 같은 입력으로 **개선 전**(DB 커넥션을 잡은 채 외부 호출, 동기 오류 로그)과 **개선 후**(외부 호출 시간 제한, 호출 후 커넥션 획득, 제한된 비동기 로그 큐)를 실제 HTTP 요청과 SQLite 커넥션 풀로 측정합니다. API별 P95·최대 응답 시간, 풀 점유·획득 대기 초과, 로그 보존·유실을 비교하고 선택적으로 Ollama가 측정값만 근거로 가설을 제안합니다. Python 구조 재현 모델이며 Spring/Hikari 성능 측정이 아닙니다. 개선 후 로그인은 외부 호출 제한으로 빠르게 실패할 수 있어 응답 시간 목표 충족과 로그인 성공은 따로 확인해야 합니다.

## 실시간 진행 기록 (SSE)

`GET /api/events/{kind}/{id}` (`kind`: `runs`, `campaigns`, `lab`)가 실행 스냅샷을 SSE로 전송합니다. 프론트는 EventSource로 수신하며, 스냅샷을 교체해 타임라인 중복을 방지합니다. 이벤트 ID는 저장된 스냅샷의 해시이며 Last-Event-ID 재연결을 지원합니다. 변경이 없으면 heartbeat를 보내고 최종 상태에서 연결을 닫습니다. 목록·완료 기록 조회에는 저빈도 polling이 남아 있습니다. 단계·행동 진행 기록 스트리밍이며 Ollama의 JSON 토큰 스트리밍은 아닙니다.

상단 자연어 안내는 `/api/assistant/route`로 사용 안내·프로젝트 점검·구조 실험 경로를 구분합니다. 안내만으로 테스트를 자동 시작하지 않습니다.

## MCP 서버

대시보드와 같은 API를 MCP 도구로 제공합니다. CrashCustomer 서버를 띄운 상태에서, 프로젝트 폴더에서 아래 명령으로 MCP 클라이언트에 등록합니다.

```bash
claude mcp add crashcustomer -- "$(pwd)/.venv/bin/python" -m crashcustomer.mcp_server
```

도구: `list_projects`, `check_connection`, `start_campaign`, `get_campaign`, `get_campaign_report`, `start_demo_run`, `get_run`, `start_lab_experiment`, `get_lab_report`. 다른 주소의 서버를 쓰려면 `CRASHCUSTOMER_API`를 지정합니다.

### 그래프에서 실제 사용하는 MCP

기존 `mcp_server.py`는 외부 클라이언트가 CrashCustomer를 제어하는 방향입니다. 별도 `mcp_context_server.py`는 그래프가 조회하는 읽기 전용 계획 서버입니다. 캠페인마다 실제 stdio MCP 세션을 열어 `qa_plan` Prompt, `crashcustomer://runbooks/{project_id}` Resource, `search_project_references`·`check_plan_coverage` Tools를 호출합니다. API 제어 서버를 다시 호출하는 재귀 구조가 아닙니다. 호출 이력은 화면·보고서에 남습니다. 첨부 내용은 참고 자료이며 실행하지 않습니다.

## LangSmith 추적

`.env`에 `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`를 설정하면 탐색·사전 점검·실험실 그래프와 Ollama 호출(`explorer.decide`, `planner.plan`, `lab.ai_analysis`)이 실행 ID 메타데이터와 함께 기록됩니다. 활성화하면 목표·화면 관찰·첨부 검색 결과·모델 출력이 LangSmith로 전송되므로 합성·테스트 데이터에서만 사용하세요. 설정하지 않으면 외부로 전송하지 않습니다. 활성화 여부는 `GET /api/config`의 `tracing`에서 확인합니다.

설정 여부와 실제 수신 여부는 구분합니다. `GET /api/tracing/status`는 설정·최근 수신 검증을 보여줍니다. 화면의 **합성 추적 전송·수신 검증** 또는 `POST /api/tracing/verify`는 프로젝트 데이터 없이 합성 Run을 생성·종료한 뒤 같은 ID와 출력값을 다시 조회합니다. 조회까지 일치한 경우에만 `verified`로 기록하며 결과는 `data/tracing-verification.json`에 보관합니다. 키가 없으면 `not_configured`를 반환하고 전송하지 않습니다. SDK 전송 오류만으로 실제 서비스 결함을 판정하지 않습니다. 합성 수신 검증 성공과 실제 캠페인 전체 Trace 수집 성공도 별도로 확인해야 합니다.

## 구조

```text
crashcustomer/
  app.py        FastAPI API, 대시보드, 테스트 세션, 백그라운드 실행
  agent.py      LangGraph 관찰/판단/행동, 독립 재현, 보고서
  browser.py    제한된 Playwright 도구, 화면 구조/응답/증거 수집
  policy.py     규칙 기준선과 Ollama 다음 행동 결정
  checks.py     관찰 근거에 대한 서비스 계약 검사
  demo.py       정상/결함 행사 신청 페이지
  store.py      실행/격리 세션 SQLite 저장
  models.py     실행 요청, 허용 행동, 계약 정의
  attachments.py 파일 검사, OpenAPI 요약, ZIP 목록, 첨부 템플릿
  campaign.py   배포 전 사전 점검 감독 그래프, 계획 에이전트(규칙/Ollama), 보고서
  targets.py    실행 대상 허용 목록과 응답 확인
  retrieval.py  첨부 자료 검색 (BM25, 선택적 임베딩 혼합)
  tracing.py    선택적 LangSmith 추적
  mcp_server.py MCP 도구 서버 (stdio)
  lab.py        로컬 실험실 그래프·측정·판정, lab_service.py 커넥션 풀 모델
examples/       사전 점검 대상 샘플 앱, OpenAPI, 운영 메모
static/         빌드 없는 대시보드 HTML/CSS/JS
scripts/        실행과 평가
tests/          계약 오탐, 세션 초기화, 입력 검증, 대상 허용, 장애 주입, RAG, 계획 테스트
data/           실행 중 생성. Git 제외
```

스크린샷과 로그는 `data/artifacts/<run-id>/`에 저장됩니다. 재현 시 브라우저 저장소뿐 아니라 서버의 제출 횟수와 신청 데이터까지 초기화합니다. 실행 중 서버가 종료되면 다음 시작 시 해당 기록을 `interrupted`로 표시합니다. 동시에 실행하는 브라우저 작업은 `CRASHCUSTOMER_BROWSER_SLOTS`(기본 2)개까지이며, 데모 실행은 대기 포함 최대 3개입니다.

## 검증 및 평가

```bash
.venv/bin/python -m pytest -q
# 서버가 실행 중인 다른 터미널에서:
.venv/bin/python scripts/evaluate.py --backend rule
# 실제 모델이 준비되었을 때 별도 측정:
.venv/bin/python scripts/evaluate.py --backend ollama --output data/evaluation-ollama.json
```

평가기는 정상 버전과 결함 3개를 각각 실행하고 정답표와 비교합니다. 탐지율, 독립 재현율, 오탐 수, 실행 오류 수, 행동 수와 시간을 `data/evaluation.json`에 기록합니다. 정답표는 평가기에서만 사용하며 actor에는 제공하지 않습니다. 정상 버전은 결함 0건뿐 아니라 신청 완료까지 확인해야 통과합니다.

규칙 기준선과 AI 결과는 각각 기록해야 합니다. 합성 폼의 지정된 결함을 찾은 결과를 임의 웹서비스 전반의 탐지 성능으로 해석하면 안 됩니다. 각 결함은 격리되어 실행되며, 여러 결함이 동시에 발생하는 경우는 아직 평가하지 않았습니다.

실제 초기 실행 결과와 행동 제약 적용 전후 비교는 [평가 기록](docs/EVALUATION.md)에 보존했습니다.

## 제약 및 다음 개발

- 등록 앱은 로컬·허용 호스트만 실행합니다. 로그인이 필요한 화면, 결제, CAPTCHA는 지원하지 않습니다.
- 사전 점검은 첫 화면에서 시작해 버튼과 라벨이 있는 입력만 다룹니다. 링크 이동, 여러 페이지 흐름은 아직 다루지 않습니다.
- 이전 AI 계획 모드는 장애 유형을 고르게 덮는다는 보장이 없었습니다. 샘플 평가에서 규칙 기반은 11/11, AI는 9/11 유형을 재현했습니다([평가 기록](docs/EVALUATION.md)). 현재는 누락 검사·제한 재계획·규칙 보완을 추가했습니다. 시나리오 예산이 유형 수보다 작거나 실제 적용에 실패하면 부분 완료로 표시합니다. 기존 평가 수치를 새 버전의 성능으로 간주하지 마세요.
- 부하 측정은 단일 프로세스 HTTP 클라이언트의 단계 부하입니다. 쓰기 요청 부하, 분산 부하, 장시간 부하는 측정하지 않습니다.
- `retry_storm`·`silent_failure`는 화면이 안정될 때까지(네트워크 유휴 + 진행 중 요청 종료 + 0.3초) 기다린 뒤 판정합니다. 0.5초 넘게 기다렸다가 다시 요청하는 화면은 판정 시점이 어긋날 수 있습니다.
- 등록 앱의 서버 데이터는 실행 사이에 초기화하지 않습니다. 기준 실행과 시나리오마다 실제 주문·신청이 생성되며, 재현 결과가 서버 상태의 영향을 받을 수 있습니다.
- 성공·오류 문구는 한국어·영어 키워드로 판단합니다. 다른 표현을 쓰는 화면에서는 `false_success`·`silent_failure`를 놓치거나 잘못 판정할 수 있습니다.
- 클릭 가능한 버튼과 라벨이 있는 필드 중심입니다. iframe, 캔버스, 드래그, 시각적 레이아웃 결함은 미지원입니다.
- 현재 계약의 성공 문구와 제출 의미는 내장 서비스에 맞춰져 있습니다. 새로운 서비스는 별도 계약과 판정 도구가 필요합니다.
- 응답 실패와 성공 메시지의 모순은 확인하지만 내부 코드의 근본 원인까지 확정하지 않습니다.
- `goal_completed`는 이번 경로의 완료입니다. 버그가 없다는 전역 판정이 아닙니다.
- 재현 보고서에 테스트용 이름과 이메일, 화면이 포함됩니다. 이 버전은 합성 데이터만 사용합니다.
- SQLite는 실행 기록과 관찰을 보관합니다. LangGraph 중간 상태 체크포인트와 재시작 후 이어하기는 아직 없습니다.
- 다음 순서: Docker 기반 서버 재시작·리소스 제한 → 의존성(DB) 장애 주입 프록시 → OpenAPI 명세 기반 응답 스키마 검사 → 재현용 Playwright 테스트 파일 생성 → 여러 페이지 흐름.

## 참고

- [Playwright Python Trace Viewer](https://playwright.dev/python/docs/trace-viewer)
- [Playwright 접근성 스냅샷](https://playwright.dev/python/docs/aria-snapshots)
- [Ollama Chat API](https://docs.ollama.com/api/chat)
