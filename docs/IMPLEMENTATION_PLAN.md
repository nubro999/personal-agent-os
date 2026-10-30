# Implementation Plan

AGENTS.md의 "First delivery"를 순서대로 쪼갠 것. 구현 순서는 DB schema, domain rules, API contract, graph nodes, UI.

| # | 단계 | 상태 |
|---|---|---|
| 1 | 로컬 Postgres compose, backend uv 프로젝트, 설정, Task/TaskEvent/FieldEvidence/TaskLink 모델과 migration, health API | 완료 (2026-10-30) |
| 2 | Task Domain Service: 생성(요청 멱등성), 조회, 수정(낙관적 동시성), 완료/재오픈, 이벤트와 근거를 한 트랜잭션에 기록 | 완료 (2026-10-30) |
| 3 | Task CRUD API (FastAPI): owner_id는 인증 컨텍스트에서만, request_id는 요청 envelope에서만 | 미착수 |
| 4 | LangGraph Task workflow: parse, extract, resolve, validate, clarify(`interrupt()`), persist/read. Postgres checkpointer | 동작: parse(대상 후보 포함), create, read, mutate, reply. 되묻기는 대화로 처리, `interrupt()`와 영속 checkpointer는 아직 |
| 5 | Agent Chat UI 연결: LangGraph server와 FastAPI 라우팅 결정 후 문서화 | 데모: 호스팅 UI에서 `langgraph dev` 직접 연결 |
| 6 | 자동 실행: Postgres restart 정책, systemd 사용자 유닛(에이전트, 수집기 타이머), Windows 로그인 때 WSL 깨우기 | 완료 (Windows 작업은 스크립트만, 직접 실행 필요) |
| 7 | 업무 자동 트래킹 1: Claude Code 세션 수집기 | 완료 (2026-10-30) |
| 8 | 업무 자동 트래킹 2: 2325 수집기 | 완료 (2026-10-30) |
| 9 | 업무 자동 트래킹 3: Slack 수집기 | 완료 (2026-10-30). 공용 develop 토큰이라 develop이 들어간 채널만, DM 제외 |
| 10 | Overmind Adapter (회사 지식 조회가 필요할 때) | 미착수 |

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
- 아직 없는 것: `interrupt()` 되묻기, 영속 checkpointer(`langgraph dev`는 메모리 저장이라 서버를 끄면 대화 기록이 사라진다. Task는 Postgres에 남는다), FastAPI Task API, 인증.

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

## 테스트로 확인하는 것 (1단계)
- 같은 owner의 같은 `request_id`로 Task 두 개를 만들 수 없다. owner가 다르면 된다.
- status, priority, estimated_minutes, evidence origin, link relation 제약.
- Task가 자기 자신에게 link될 수 없다.
- 기본값: status `todo`, priority `medium`, 마감 없음.
