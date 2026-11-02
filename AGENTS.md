# Agent Development Instructions

이 저장소는 **Personal Agent OS**의 구현 및 설계 문서다. 작업 전에 `README.md`와 `docs/PROJECT_CONTEXT.md`를 읽는다.

## Product invariants
- 첫 MVP는 자연어 Task CRUD. Overmind/Google Calendar/Notion의 Task는 사용하지 않는다.
- Task DB(PostgreSQL)가 개인 업무의 source of truth. LangGraph state는 실행 중 상태이며 영구 Task DB를 대체하지 않는다.
- 사용자의 **명시적 사실**과 LLM의 **추론·기본값**을 구분해 보관한다.
- 중요 정보가 모호할 때만 질문한다. 지정하지 않은 날짜나 시각은 임의 확정하지 않는다.
- Task 실행과 Task 기록을 분리한다. 코드 변경·운영 DB 쓰기·배포·Slack 전송은 승인 없는 자동 실행 금지.
- 회사 데이터(Slack, 2325, Claude Code 세션 기록 등)는 Overmind를 거치지 않고 직접 읽어도 된다 (2026-10-30 사용자 결정). 읽기는 자유, 외부 시스템 쓰기와 전송은 여전히 승인 게이트를 거친다.
- 비밀키와 회사 원본 데이터는 git에 커밋하지 않는다. 로컬 DB와 `.env`에만 둔다.
- 모든 DB 쓰기는 서버 측 검증, 트랜잭션, 요청 멱등성, 감사 이벤트를 갖춘다.
- LangGraph 재시도 시 중복 Task 생성/상태 전이를 방지한다.

## Technology and layout
- `frontend/`: Vite, React, TypeScript 앱(보드, 그래프, 채팅). `pnpm build` 결과(`dist/`)를 FastAPI가 8000에서 내준다. `dist/`는 커밋하지 않는다.
- `backend/`: FastAPI, LangGraph, Pydantic, SQLAlchemy, Alembic, uv.
- `infra/`: 로컬 PostgreSQL Docker Compose, systemd 유닛, 2325 터널, 백업, Windows 스크립트.
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
- UI: http://localhost:8000 한 앱에 보드, 그래프, 채팅 탭. 채팅은 assistant-ui, 그래프는 react-force-graph-2d, 스타일은 Tailwind. upstream Agent Chat UI는 쓰지 않는다.
- 그래프 탭은 Task 현황이 아니라 회사 온톨로지를 이해하는 개념 지도다. 노드는 개념, 링크는 이름 있는 관계이고 사례는 눌렀을 때 펼친다.
- 온톨로지 자료는 회사 자료라 이 저장소에 두지 않는다. `PAO_ONTOLOGY_DIR`(기본 `~/glab/glab_mt/_ontology`)의 `concepts/*.md`와 `verbs.yaml`이 정본이고 코드(`app/ontology`)만 여기 있다. 관계 이름은 사전의 영어 동사만 쓴다.
- 알림: Slack 봇은 보류. 브리핑은 채팅과 Windows 알림으로 전달한다.
- LLM provider는 설정으로 교체 가능. 첫 provider는 Claude Agent SDK(개인 Claude Max 구독 로그인)이고 회사 업무 트래킹에도 쓴다. 혼자 쓰는 로컬 용도로만 쓰고, 다른 사람에게 제공하거나 클라우드에 배포하려면 API 키 provider로 바꾼다.
- Temporal은 초기 의존성 아님.
- 기본 타임존 `Asia/Seoul`.
