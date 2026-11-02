# Implementation Plan

AGENTS.md의 "First delivery"를 순서대로 쪼갠 것. 구현 순서는 DB schema, domain rules, API contract, graph nodes, UI.

| # | 단계 | 상태 |
|---|---|---|
| 1 | 로컬 Postgres compose, backend uv 프로젝트, 설정, Task/TaskEvent/FieldEvidence/TaskLink 모델과 migration, health API | 완료 (2026-10-30) |
| 2 | Task Domain Service: 생성(요청 멱등성), 조회, 수정(낙관적 동시성), 완료/재오픈, 이벤트와 근거를 한 트랜잭션에 기록 | 완료 (2026-10-30) |
| 3 | Task API (FastAPI): 목록, 상태 변경, 제안 거절. owner는 설정값, request_id는 클라이언트 envelope | 완료 (2026-10-30). 인증은 없고 127.0.0.1 전용 |
| 4 | LangGraph Task workflow: parse, extract, resolve, validate, clarify(`interrupt()`), persist/read. Postgres checkpointer | 동작: parse(대상 후보 포함), create, read, mutate, reply. 되묻기는 대화로 처리, `interrupt()`와 영속 checkpointer는 아직 |
| 5 | 화면 | 완료: `frontend/` React 앱(보드, 그래프, 채팅). 보드와 그래프는 FastAPI(8000), 채팅은 LangGraph(2024)를 부른다 |
| 6 | 자동 실행: Postgres restart 정책, systemd 사용자 유닛(에이전트, 수집기 타이머), Windows 로그인 때 WSL 깨우기 | 완료 (Windows 작업은 스크립트만, 직접 실행 필요) |
| 7 | 업무 자동 트래킹 1: Claude Code 세션 수집기 | 완료 (2026-10-30) |
| 8 | 업무 자동 트래킹 2: 2325 수집기 | 완료 (2026-10-30) |
| 9 | 업무 자동 트래킹 3: Slack 수집기 (멘션, DM, 그룹 DM) | 완료 (2026-10-30). 내 user token 사용 |
| 10 | 브리핑, 제안 거절, 조회 필터, 수집기 실패 표시, DB 백업, 2325 전용 터널 | 완료 (2026-10-30) |
| 11 | 오래된 Task 정리, 출처 간 중복 제안 | 완료 (2026-10-30) |
| 12 | Slack 봇 (브리핑 전달, Slack에서 대화) | 보류 |
| 13 | Slack 답변 초안 | 완료 (2026-10-30) |
| 14 | 2325 개선 에이전트, Context Agent | 미착수 |
| 16 | 온톨로지 그래프: 개념 30개, 관계 초안, 사례 | 2325, 채팅 DB 사례까지 완료 (2026-11-02). 개념 25개에 사례가 있다. 규칙, ai-admin 사례, 에이전트 접근은 아직 |
| 15 | Overmind Adapter (회사 지식 조회가 필요할 때) | 미착수 |

## 1단계에서 정한 것
- 로컬 DB 포트는 `5433` (다른 프로젝트 Postgres와 겹치지 않게). `POSTGRES_PORT`로 바꿀 수 있다.
- DB 드라이버는 asyncpg, SQLAlchemy async. Alembic env도 async.
- 설정은 `PAO_` 접두 환경변수 (`backend/.env.example`).
- 테스트는 실제 Postgres(`pao_test`)에 migration을 적용해 돌린다. 테스트마다 트랜잭션 롤백으로 격리한다. migration과 모델이 어긋나면 `test_migration_matches_models`가 실패한다.

## 데모 (2026-10-30)
- 그래프: `parse`(LLM이 TaskIntent 하나를 구조화 출력) 다음 `create`, `read`, `reply` 중 하나. LLM은 도구를 쓰지 않고 저장은 도메인 서비스가 한다.
- LLM provider: `app/llm/` 인터페이스 뒤에 Claude Agent SDK 구현. 도구, 사용자 설정, CLAUDE.md를 모두 끄고 호출한다. Sonnet 기준 한 번에 6~10초.
- 날짜는 모델이 계산하지 않도록 이번 주, 다음 주 달력을 프롬프트에 넣는다. 요일만 말하면 오늘 포함 가장 가까운 그 요일.
- 요청 멱등성: `request_id`는 LangGraph `run_id`. 같은 run이 재시도돼도 Task가 하나만 생긴다.
- owner는 설정값 `PAO_OWNER_ID`(기본 `me`). 인증이 붙기 전까지 로컬 단일 사용자.
- 아직 없는 것: `interrupt()` 되묻기, 인증. 대화 기록은 Postgres가 아니라 `langgraph dev`가 `.langgraph_api/`에 파일로 저장한다. 재시작해도 남는 것을 확인했다. Postgres checkpointer는 `langgraph dev`에 끼울 수 없고 별도 서버 구성이 필요하다.

## 대상 찾기와 변경 (2026-10-30)
- `parse`가 열린 Task와 최근 14일 안에 끝낸 Task를 `T1, T2 ...` ref로 프롬프트에 넣는다. LLM은 ref만 고르고, 목록에 없는 ref는 버린다.
- 대상이 하나로 안 정해지면(`ambiguous`) 후보를 보여 주고 되묻는다. 다음 말에서 대화 기록으로 다시 고른다.
- 끝난 일은 수정이나 완료를 하지 않고 다시 열어 달라고 안내한다.
- 변경은 `update_task`: 같은 `request_id` 이벤트가 있으면 다시 반영하지 않고, `version`이 다르면 conflict.
- 완료나 취소가 되면 남아 있던 완료 제안은 accepted로 정리한다.

## Claude Code 수집기 (2026-10-30)
- `app/trackers/claude_code.py`, systemd `pao-tracker.timer` 30분마다.
- 사람이 쓴 세션만(`entrypoint`가 claude-desktop, cli). sdk 세션은 건너뛴다. 에이전트 자신의 Agent SDK 호출은 `--no-session-persistence`로 세션 파일을 남기지 않는다.
- 읽은 위치는 `activity_sessions.processed_lines`. LLM이 실패하면 위치를 올리지 않아 다음 실행에서 다시 본다.
- 처음 실행은 최근 24시간(`PAO_TRACKER_LOOKBACK_HOURS`), 활동 뒤 20분 조용한 세션만(`PAO_TRACKER_IDLE_MINUTES`).
- 수집기가 직접 하는 변경은 Task 생성과 todo에서 진행 중으로 바꾸기뿐이다. 완료는 `task_suggestions`에 제안만 남긴다.

## 2325 수집기 (2026-10-30)
- `app/trackers/maintenance.py`. `maintenance_assignment` + `assignee` + `wi_ax_item` + `maintenance_status`를 읽기만 한다.
- Task 하나가 2325 건 하나를 따라간다. `request_id = 2325:{item_key}`라 같은 건으로 Task가 두 번 생기지 않는다.
- 마지막으로 본 2325 상태(`stage|status_group`)를 `activity_sessions.summary`에 둔다. 바뀐 건만 처리해서 849건 중 실제 쓰기는 변화가 있을 때뿐이다.
- 수집기들은 `python -m app.trackers`로 한 이벤트 루프에서 차례로 돈다(async DB 엔진이 루프에 묶인다). 하나가 실패해도 나머지는 돈다.

## Slack 수집기 (2026-10-30)
- `app/trackers/slack.py`. `search.messages`로 멘션을 찾고 `conversations.replies`로 스레드 전체를 읽는다. 쓰기 API는 부르지 않는다.
- 스레드마다 `max(내 마지막 ts, 나를 멘션한 마지막 ts)`를 `activity_sessions.summary`에 두고, 바뀐 스레드만 다시 본다. 이 스레드로 만든 열린 Task는 내가 답했는지 보러 매번 다시 읽는다.
- 내 차례인 스레드는 한 번의 Claude 호출로 묶어서 분류한다.
- 처음 돌렸을 때 CS 스레드 11개가 "리마인드 확인" 같은 제목으로 Task가 됐는데, 9개는 남의 담당이고 2개는 이미 배포완료였다. 그래서 내 2325 Task가 없는 CS 스레드는 건너뛰게 했다.
- 수집기 순서: 2325, Slack, Claude Code. 앞에서 만든 Task를 뒤에서 CS 번호로 잇는다.
- DM: `conversations.list`로 대화방을 얻고, 최근 3일 안에 활동이 있는 방만 `conversations.history`로 읽는다. 상태는 대화방당 하나(`{channel}:dm`), Task는 답하지 않은 묶음의 첫 메시지 ts 기준(`slack:{channel}:{ts}`).
- 분류 경계: 명시적 요청 없이 현황만 공유하는 DM은 fyi로 분류될 수 있다. 같은 메시지가 실행마다 다르게 분류된 적이 있다. 마지막 메시지 기준으로 한 번만 분류하므로 새 메시지가 오기 전에는 다시 보지 않는다.

## Slack 답변 초안 (2026-10-30)
- 전송 코드는 없다. `reply_drafts`에 초안만 저장하고 보드에서 복사한다.
- 초안 대상: 내 차례이고 요청으로 분류됐고 내 Task가 있는 대화. 남의 CS 스레드나 참고는 만들지 않는다. 한 번 실행에 최대 5개(`PAO_SLACK_DRAFT_LIMIT`).
- 말투: `from:<@나>` 검색으로 최근 메시지를 읽어 짧은 답변을 샘플로 넘긴다. 샘플과 대화 원문은 저장하지 않는다. Slack 답변은 합쇼체다. 검토 의견의 "~함" 메모체를 쓰지 않는다.
- 새 메시지가 오면 초안을 다시 만들고, 내가 답하면 `used`로 치운다.
- 초안 품질에 대한 사람의 평가는 아직 없다. 임시 owner로 2건을 눈으로 봤다.

## 온톨로지 검증 (2026-11-02)
- `app/ontology/trial.py`: 끝난 건의 처리 기록을 숨기고, 에이전트에게 온톨로지 조회 도구 네 개만 줘서 처리하게 한 뒤 실제 수정과 비교한다. 결과는 `_ontology/검증/`.
- 세 건 결과: 구조(건, 턴, 매뉴얼, step, 정책)는 잘 따라갔지만, 내용이 필요한 오류는 못 맞혔다. 빠진 것은 정보와 step 본문, 봇 설정 전체 목록, 응답 칸의 뜻, 대화 원문과 함수 반환값.
- 1차 시험은 정답이 샜다. 그 건을 고치며 새로 만든 정보가 변경 기록에서 사례로 올라와 있었다. 숨긴 기록에만 근거가 있는 노드도 숨기게 고쳤다.
- config_tree: `admin --env prod export`(읽기)로 37개 봇의 카테고리, step, 정보, 멘트, 함수 본문과 uuid를 붙였다(`app/ontology/source_admin.py`, `instances_admin.py`). 번들(export-bundle)은 옛 형식이라 쓰지 않는다. 사례 89,983개, 관계 61개 중 51개 입증.
- 시험에서는 config_tree가 고친 뒤 내용이라, 고친 대상을 2325 변경 항목의 고치기 전 본문으로 바꿔 보여 준다. 3차 결과: 본문이 들어가자 색상 오류 건의 원인을 맞혔고, 날짜 해석 건은 함수 반환값과 칸 뜻이 없어 여전히 못 풀었다.
- 응답 칸: `tools/run api.fields`(회사 도구, genlab-tools 스펙과 코드)로 35개 봇의 응답 칸 2,536개를 붙이고 상태 사전 값을 더했다(`source_fields.py`, `instances_fields.py`). GitHub 회사 계정 인증이 필요하다. CS-0004252는 칸 뜻만으로는 못 풀었고, 함수 반환값과 피드백 전문이 필요했다.
- 고객사 담당자(contact)와 팀(team) 개념을 더했다. 담당자는 `communication.people`, 팀은 2325 담당자 칸의 사람이 아닌 이름에서 온다.

## 웹 앱 (2026-11-02)
- 단일 HTML 페이지를 `frontend/` React 앱으로 바꿨다. 탭은 보드, 그래프, 채팅이고 URL 해시로 고른다.
- 보드: 답변 초안, 끝난 것 같은 일, 진행 중, 할 일, 고객 답 대기. 찾기 칸이 있다. 스타일은 shadcn/ui 없이 Tailwind로 직접 썼다.
- 그래프: `react-force-graph-2d`. 개념 지도다. 호버와 클릭으로 연결 강조, 옆 패널, 사례 펼치기, 찾기, 묶음별 끄기, 빈 부분 보기.
- 채팅: `assistant-ui` + LangGraph 런타임. 노드가 완성된 메시지를 돌려주므로 `streamMode: values`를 쓴다. 스레드 id는 브라우저에 저장한다. 대화 목록과 마크다운 렌더링은 아직 없다.
- 확인: headless Edge로 보드와 그래프 화면을 찍어 봤고, 채팅은 "브리핑"을 실제로 보내 19초 만에 답이 표시됐다. 버튼을 사람이 눌러 본 것은 예전 페이지 기준이다.

## 온톨로지 그래프 (2026-11-02)
- 재료: `_review/온톨로지_객체_정의.md`의 객체 30개. 관계 58개는 정의서, 2325 표 구조, 매뉴얼 데이터를 보고 초안을 썼다. 관계 이름과 신뢰도는 사람의 검토가 아직 없다.
- 사례 출처: S3 `index/brand-map.json`, `brands/context`, S3 `context/`, 2325 DB(전용 터널, 읽기 전용 세션). 원본을 옮기지 않고 위치와 짧은 설명만 남긴다.
- 관계 이름은 `verbs.yaml`의 영어 동사 27개만 쓴다(2026-11-02). 동사마다 정의와 쓸 수 있는 개념 짝이 있고, 전체와 부분은 부분이 주어다. 사전에 없는 동사나 짝, 개념 파일에 없는 사례 링크가 있으면 빌드가 멈춘다.
- 관계 58개 중 실제 사례로 입증된 것은 45개다. 나머지는 ai-admin 사례가 들어와야 확인된다.
- 채팅 DB: 건의 chat_id로 찾은 대화 2,323개, 턴 18,531개만 읽는다. DB 전체를 읽지 않는다. 고른 매뉴얼은 `manual_usage.used`(판정 기록이 있는 턴 6%뿐이라 옛 턴은 비어 있다), 문제 턴은 회사 `tools.chat.target.find`. 인용 근거가 있는 문제 턴은 19%이고 나머지는 추정이다. queenit은 표 모양이 달라 읽지 못했다.
- 건 원문과 오응대 원인 글은 240자까지 가리지 않고 저장한다(사용자 결정). 자료는 `_ontology/`에만 있고 git에 넣지 않는다. 채팅 원문은 그래프에 필요 없어 저장하지 않는다.
- 2325 실측: 건 → 브랜드, 고객사 98%, 실행 → 건 99%, 변경 항목 → 카테고리, 정보 93%, 담당 → 사용자 83%(나머지는 플랫폼, 툴콜 같은 팀 이름), 판정 → 사용자 100%. 건과 브랜드의 이름 대조에서 어긋난 조합은 꿈꾸는요셉, 무유 하나였고 ax 값 그대로다.
- 변경 기록으로 올린 카테고리와 정보가 있다. `brands/context`는 함수를 부르는 매뉴얼만 담아서다.
- 카테고리 진짜 키는 수정안의 `orig_id`로 273개를 채웠다.
- API: `/api/ontology`(개념층), `/concepts/{id}`, `/instances`, `/neighbors`, `/gaps`. `instances.json`은 파일 시각이 바뀌면 다시 읽는다.
- 브랜드 매칭 37개는 하나씩 눈으로 확인했다.
- 사례가 많아지면 맞춤 확대가 과해져 원이 화면을 덮었다. 맞춤 배율과 원 크기에 상한을 뒀다.

## 테스트로 확인하는 것 (1단계)
- 같은 owner의 같은 `request_id`로 Task 두 개를 만들 수 없다. owner가 다르면 된다.
- status, priority, estimated_minutes, evidence origin, link relation 제약.
- Task가 자기 자신에게 link될 수 없다.
- 기본값: status `todo`, priority `medium`, 마감 없음.
