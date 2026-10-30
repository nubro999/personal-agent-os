# Implementation Plan

AGENTS.md의 "First delivery"를 순서대로 쪼갠 것. 구현 순서는 DB schema, domain rules, API contract, graph nodes, UI.

| # | 단계 | 상태 |
|---|---|---|
| 1 | 로컬 Postgres compose, backend uv 프로젝트, 설정, Task/TaskEvent/FieldEvidence/TaskLink 모델과 migration, health API | 완료 (2026-10-30) |
| 2 | Task Domain Service: 생성(요청 멱등성), 조회, 수정(낙관적 동시성), 완료/재오픈, 이벤트와 근거를 한 트랜잭션에 기록 | 미착수 |
| 3 | Task CRUD API (FastAPI): owner_id는 인증 컨텍스트에서만, request_id는 요청 envelope에서만 | 미착수 |
| 4 | LangGraph Task workflow: parse, extract, resolve, validate, clarify(`interrupt()`), persist/read. Postgres checkpointer | 미착수 |
| 5 | Agent Chat UI 연결: LangGraph server와 FastAPI 라우팅 결정 후 문서화 | 미착수 |
| 6 | Overmind Adapter interface와 fake | 미착수 |

## 1단계에서 정한 것
- 로컬 DB 포트는 `5433` (다른 프로젝트 Postgres와 겹치지 않게). `POSTGRES_PORT`로 바꿀 수 있다.
- DB 드라이버는 asyncpg, SQLAlchemy async. Alembic env도 async.
- 설정은 `PAO_` 접두 환경변수 (`backend/.env.example`).
- 테스트는 실제 Postgres(`pao_test`)에 migration을 적용해 돌린다. 테스트마다 트랜잭션 롤백으로 격리한다. migration과 모델이 어긋나면 `test_migration_matches_models`가 실패한다.

## 테스트로 확인하는 것 (1단계)
- 같은 owner의 같은 `request_id`로 Task 두 개를 만들 수 없다. owner가 다르면 된다.
- status, priority, estimated_minutes, evidence origin, link relation 제약.
- Task가 자기 자신에게 link될 수 없다.
- 기본값: status `todo`, priority `medium`, 마감 없음.
