# Personal Agent OS

개인 개발·운영 업무를 자연어로 관리하고, 필요한 회사 맥락을 조회하며, 검증된 자동화 작업을 승인 후 실행하는 개인 AI 업무 시스템.

> 상태: **설계 확정 / 구현 전** (2026-10-29). 이 저장소의 문서는 개발 인수인계용이며 실행 가능한 제품을 의미하지 않습니다.

## 결정된 방향

- **Frontend:** [LangChain Agent Chat UI](https://github.com/langchain-ai/agent-chat-ui) (Next.js)
- **Agent runtime:** Python + LangGraph
- **API:** FastAPI
- **Storage:** PostgreSQL, SQLAlchemy, Alembic
- **Package management:** uv
- **Development:** WSL Ubuntu, `/home/nubroo/projects/personal-agent-os`
- **Infrastructure:** 초기 Docker Compose, 필요 시 AWS 배포
- **Long-running workflows:** 처음에는 LangGraph Checkpointer + scheduler, 필요할 때 Temporal 추가
- **Company context:** 기존 사내 Overmind를 읽기 전용으로 재사용, 개인 Task의 원본은 자체 DB

## 세 가지 장기 기능

1. **Company Context Agent:** Overmind에 있는 공식 정보를 조회하고 누락·충돌·최신성 공백을 찾아 질문을 생성한다. 모든 지식을 무한 수집하지 않는다.
2. **2325 Improvement Agent:** 요청·Failure 분석, 프롬프트/코드 개선 제안, Eval, 회귀 검사, 사람 승인 후 적용.
3. **Personal Task & Communication Agent:** 자연어 Task 등록·수정·완료, 중요도 추론, 업무 브리핑, Slack 답변 초안. 외부 Calendar/Overmind Task 기능에 의존하지 않는다.

## 첫 MVP

**자연어 Task Agent** 하나를 먼저 구현한다.

- “내일까지 2325 Eval 돌려야 해” → Task 생성
- “이번 주 할 일 알려줘” → Task 검색
- “2325 Eval 끝났어” → 대상 식별 후 완료
- “그거 금요일까지 미뤄줘” → 문맥으로 대상 식별, 모호하면 질문
- 사용자 명시값과 모델 추론값을 구분하여 저장
- 업무 생성/수정은 자체 PostgreSQL에 영구 저장, 변경 이벤트 및 근거 기록
- 추가 질문은 LangGraph `interrupt()` + 체크포인터로 처리

## 개발 시작

1. [AGENTS.md](AGENTS.md)를 먼저 읽는다.
2. [프로젝트 맥락](docs/PROJECT_CONTEXT.md)과 [아키텍처](docs/ARCHITECTURE.md)를 읽는다.
3. [구현 계획](docs/IMPLEMENTATION_PLAN.md)의 첫 단계부터 구현한다.
4. 회사 Overmind 연결은 초기 MVP에서 **모의 Adapter만** 두고 실데이터 접근은 별도 승인 및 환경 구성 후 연결한다.

## 문서

- [Project Context](docs/PROJECT_CONTEXT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Task Agent](docs/TASK_AGENT_DESIGN.md)
- [Database Schema](docs/DATABASE_SCHEMA.md)
- [Overmind Integration](docs/OVERMIND_INTEGRATION.md)
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md)

## 보안

이 저장소는 비공개로 유지한다. 회사의 원본 데이터, 인증정보, 고객별 응대 로그, 내부 코드 복사본을 이 저장소에 커밋하지 않는다. 외부 시스템 변경·메시지 전송·배포는 명시적인 승인 게이트를 거친다.
