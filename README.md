# Personal Agent OS

개인 개발·운영 업무를 자연어로 관리하고, 필요한 회사 맥락을 조회하며, 검증된 자동화 작업을 승인 후 실행하는 개인 AI 업무 시스템.

> 상태: **개인용 동작 중** (2026-10-30). 채팅으로 Task 등록, 조회, 완료, 수정, 미루기, 취소, 다시 열기가 된다. 2325 담당 건, Slack 멘션, Claude Code 세션 기록을 30분마다 읽어 Task로 맞춘다. 진행 상황은 [구현 계획](docs/IMPLEMENTATION_PLAN.md)을 본다.

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
4. 회사 데이터는 직접 읽어도 된다. Overmind는 회사 지식 조회가 필요할 때 붙인다.

## 로컬 실행

```bash
# PostgreSQL (127.0.0.1:5433, 개발 DB pao, 테스트 DB pao_test)
docker compose -f infra/docker-compose.yml up -d --wait

cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload   # GET /health

# 에이전트 (LangGraph dev server, http://localhost:2024)
cp -n .env.example .env
unset ANTHROPIC_API_KEY                # 있으면 구독 대신 API로 과금되므로 실행을 거부한다
uv run langgraph dev --no-browser

# 검사
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests migrations
uv run pytest
```

## 자동 실행

```bash
infra/systemd/install.sh   # pao-agent(LangGraph 서버), pao-tracker.timer(30분마다 수집기 전부) 등록
```

- Postgres는 docker `restart: unless-stopped`, 에이전트와 수집기는 systemd 사용자 유닛으로 WSL이 켜지면 같이 뜬다.
- Windows 로그인 때 WSL을 깨우고 계속 켜 두려면 Windows PowerShell에서 `infra/windows/register-wsl-autostart.ps1`을 한 번 실행한다.
- 로그: `journalctl --user -u pao-agent -f`, `journalctl --user -u pao-tracker -n 50`
- 수집기 수동 실행: `cd backend && uv run python -m app.trackers` (하나만: `app.trackers.maintenance`, `app.trackers.slack`, `app.trackers.claude_code`)

## 업무 자동 트래킹 (Claude Code 세션)

`~/.claude/projects/*/*.jsonl` 중 사람이 직접 쓴 세션(Claude Desktop, CLI)만 읽는다. 자동화 실행(sdk)은 건너뛴다.
- 새로 보이는 일: Task로 만든다 (`source=claude_code`, 근거는 `external`).
- 기존 Task와 이어지는 일: 할 일이면 진행 중으로 바꾼다.
- 기존 Task가 끝난 것 같으면: 완료 제안만 남긴다. 할 일을 조회하면 같이 보여 주고, "끝났어"라고 하면 정리된다.
- 마지막 활동 뒤 20분 조용한 세션만 읽고, 읽은 위치부터 이어서 본다. 원문은 저장하지 않고 세션 요약과 근거 한 문장만 남긴다.

## 업무 자동 트래킹 (2325 담당 건)

2325(maintenance_list) DB에서 담당자 `PAO_MAINT_ASSIGNEE_NAME`(기본 신형섭)으로 배정된 건을 읽는다. DB 접속 정보는 `PAO_MAINT_ENV_FILE`(기본 maintenance_list/.env)에서 실행할 때 읽고, 터널(`maintenance_list/run.sh`)이 꺼져 있으면 그 주기는 건너뛴다.
- 2325 단계가 기준이다: 수정중은 진행 중, 고객사 소통중은 막힘, 팀장 확인중과 배포완료는 완료, 반려는 취소. 단계가 없으면 ax 상태로 끝났는지만 본다.
- 처음에는 최근 30일 안에 접수된 열린 건만 Task로 만든다(`PAO_MAINT_LOOKBACK_DAYS`). 만든 Task는 2325 상태가 바뀔 때마다 따라간다.
- 2325 쪽 상태가 그대로면 채팅으로 바꾼 상태를 되돌리지 않는다.
- 2325 수집기가 먼저 돌아서 Claude Code 세션이 CS 번호로 2325 Task에 이어진다.

## 업무 자동 트래킹 (Slack 멘션)

공용 develop 계정 토큰(`SLACK_READ_TOKEN`, `~/.config/genlab-overmind/secrets.json`)으로 나(`PAO_SLACK_USER_ID`)를 멘션한 메시지를 최근 3일치 검색한다. develop이 들어간 채널만 보이고 DM은 안 보인다.
- 스레드 단위로 본다. 나를 멘션한 마지막 메시지가 내 마지막 메시지보다 뒤면 내 차례다.
- 내 차례인 스레드만 Claude가 요청, 해결 보고, 참고로 나눈다.
- 루트에 CS 번호가 있으면 내 2325 Task에 잇는다. 요청이 왔는데 Task가 막힘이면 진행 중으로 바꾸고, 해결 보고면 완료 제안을 남긴다. 내 2325 Task가 없는 CS 스레드는 남의 담당이라 건너뛴다.
- CS 번호가 없는 요청은 새 Task로 만들고, 내가 그 스레드에 답하면 완료 제안을 남긴다.

채팅 UI는 https://agentchat.vercel.app 에서 Deployment URL `http://localhost:2024`, Assistant ID `task_agent`로 연결한다. LangSmith API Key는 비워 둔다.

LLM은 Claude Agent SDK로 로그인된 Claude Code 인증(개인 Claude Max 구독)을 쓴다. 혼자 쓰는 로컬 용도로만 쓴다. 사용량은 claude.ai, Claude Code와 같은 한도를 나눠 쓴다. 모델은 `PAO_LLM_MODEL`(기본 `claude-sonnet-5-5`).

테스트는 `pao_test` DB의 스키마를 매번 지우고 migration을 다시 적용한다. `_test`로 끝나지 않는 DB에서는 실행을 거부한다.

## 문서

- [Project Context](docs/PROJECT_CONTEXT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Task Agent](docs/TASK_AGENT_DESIGN.md)
- [Database Schema](docs/DATABASE_SCHEMA.md)
- [Overmind Integration](docs/OVERMIND_INTEGRATION.md)
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md)

## 보안

이 저장소는 비공개로 유지한다. 회사 데이터는 직접 읽어 로컬 DB에 둘 수 있지만, 인증정보와 원본 데이터(응대 로그, 내부 코드 복사본)는 git에 커밋하지 않는다. 외부 시스템 변경·메시지 전송·배포는 명시적인 승인 게이트를 거친다.
