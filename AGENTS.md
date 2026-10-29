# Agent Development Instructions

이 저장소는 **Personal Agent OS**의 구현 및 설계 문서다. 작업 전에 `README.md`와 `docs/PROJECT_CONTEXT.md`를 읽는다.

## Product invariants
- 첫 MVP는 자연어 Task CRUD. Overmind/Google Calendar/Notion의 Task는 사용하지 않는다.
- Task DB(PostgreSQL)가 개인 업무의 source of truth. LangGraph state는 실행 중 상태이며 영구 Task DB를 대체하지 않는다.
- 사용자의 **명시적 사실**과 LLM의 **추론·기본값**을 구분해 보관한다.
- 중요 정보가 모호할 때만 질문한다. 지정하지 않은 날짜나 시각은 임의 확정하지 않는다.
- Task 실행과 Task 기록을 분리한다. 코드 변경·운영 DB 쓰기·배포·Slack 전송은 승인 없는 자동 실행 금지.
- 외부 회사 데이터는 승인된 읽기 전용 Overmind Adapter를 거친다. 회사 원본/고객 데이터와 비밀키를 개인 저장소에 추가하지 않는다.
- 모든 DB 쓰기는 서버 측 검증, 트랜잭션, 요청 멱등성, 감사 이벤트를 갖춘다.
- LangGraph 재시도 시 중복 Task 생성/상태 전이를 방지한다.

## Technology and layout
- `frontend/`: upstream Agent Chat UI를 도입하고 가능한 한 작은 변경으로 유지.
- `backend/`: FastAPI, LangGraph, Pydantic, SQLAlchemy, Alembic, uv.
- `infra/`: 로컬 PostgreSQL용 Docker Compose.
- `docs/`: 설계 결정과 실제 구현 상태 기록.

## Engineering workflow
1. 저장소 상태와 기존 변경을 점검한다. 사용자 수정은 보존한다.
2. 해당 기능의 acceptance criteria와 실패 시나리오를 먼저 식별한다.
3. DB schema / domain rules / API contract / graph nodes 순으로 구현한다.
4. 새로운 기능에는 단위·통합·E2E 테스트를 추가한다.
5. 변경 후 타입 검사, 린트, 관련 테스트를 실행하고 결과를 보고한다.
6. 계획과 실제 구현이 다르면 docs를 업데이트한다. 미구현 기능을 완료라고 기재하지 않는다.
7. 커밋/푸시는 요청 범위에서만 수행한다.

## First delivery
- PostgreSQL DB, Task/TaskEvent/FieldEvidence 모델 및 migration
- Task CRUD service and API
- LangGraph create/read/update/complete path with conditional clarification
- Agent Chat UI에서 채팅으로 작업 등록 및 조회
- 중복 요청, 잘못된 Task 대상, 날짜 파싱, 질문 재개를 검증하는 테스트

## Known decisions
- Task UI: **Agent Chat UI**. 커스텀 보드/Inbox는 후속.
- LLM provider는 설정으로 교체 가능. 개인 Claude Max 구독을 회사 운영 API로 가정하지 않는다.
- Temporal은 초기 의존성 아님.
- 기본 타임존 `Asia/Seoul`.
