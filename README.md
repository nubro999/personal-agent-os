# Personal Agent OS

개인 개발·운영 업무를 자연어로 관리하고, 필요한 회사 맥락을 조회하며, 검증된 자동화 작업을 승인 후 실행하는 개인 AI 업무 시스템.

> 상태: **개인용 동작 중** (2026-10-30). http://localhost:8000 에서 보드와 채팅을 쓴다. 2325 담당 건, Slack 멘션과 DM, Claude Code 세션 기록을 30분마다 읽어 Task로 맞추고, 낮 12시에 브리핑 알림을 띄운다. 진행 상황은 [구현 계획](docs/IMPLEMENTATION_PLAN.md)을 본다.

## 결정된 방향

- **Frontend:** Vite, React, TypeScript 앱 한 개 (보드, 그래프, 채팅). assistant-ui, react-force-graph-2d, Tailwind. 빌드 결과를 FastAPI가 내준다.
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
infra/systemd/install.sh   # 아래 서비스와 타이머 전부 등록
```

- `pao-agent`: LangGraph 서버 (localhost:2024). 대화 기록은 `backend/.langgraph_api/`에 남는다.
- `pao-api`: 앱 화면과 API (localhost:8000). 인증이 없어서 127.0.0.1에만 띄운다.
- `pao-2325-tunnel`: 2325 DB 전용 터널 (127.0.0.1:13316). `run.sh`와 따로 돈다.
- `pao-tracker.timer`: 30분마다 2325, Slack, Claude Code, 정리. 결과는 `tracker_runs`에 남고 실패는 브리핑에 나온다.
- `pao-briefing.timer`: 낮 12시 브리핑 (`~/.local/state/personal-agent-os/briefings/`, Windows 알림). 시각을 바꾸려면 유닛의 `OnCalendar`를 고친다.
- `pao-backup.timer`: 새벽 3시 `pg_dump`를 `Documents/personal-agent-os-backups`로, 14일 보관.
- Postgres는 docker `restart: unless-stopped`로 WSL이 켜지면 같이 뜬다.
- Windows 로그인 때 WSL을 깨우고 계속 켜 두려면 Windows PowerShell에서 `infra/windows/register-wsl-autostart.ps1`을 한 번 실행한다.
- 로그: `journalctl --user -u pao-agent -f`, `journalctl --user -u pao-tracker -n 50`
- 수집기 수동 실행: `cd backend && uv run python -m app.trackers` (하나만: `app.trackers.maintenance`, `app.trackers.slack`, `app.trackers.claude_code`)

## 온톨로지 그래프

그래프 탭은 회사 온톨로지를 이해하는 개념 지도다. 자료는 `~/glab/glab_mt/_ontology/`에 있고 쓰는 법은 그 폴더의 README를 본다. 사례 다시 뽑기: `cd backend && uv run python -m app.ontology.build`

## 업무 자동 트래킹 (Claude Code 세션)

`~/.claude/projects/*/*.jsonl` 중 사람이 직접 쓴 세션(Claude Desktop, CLI)만 읽는다. 자동화 실행(sdk)은 건너뛴다.
- 새로 보이는 일: Task로 만든다 (`source=claude_code`, 근거는 `external`).
- 기존 Task와 이어지는 일: 할 일이면 진행 중으로 바꾼다.
- 기존 Task가 끝난 것 같으면: 완료 제안만 남긴다. 할 일을 조회하면 같이 보여 주고, "끝났어"라고 하면 정리된다.
- 마지막 활동 뒤 20분 조용한 세션만 읽고, 읽은 위치부터 이어서 본다. 원문은 저장하지 않고 세션 요약과 근거 한 문장만 남긴다.

## 업무 자동 트래킹 (2325 담당 건)

2325(maintenance_list) DB에서 담당자 `PAO_MAINT_ASSIGNEE_NAME`(기본 신형섭)으로 배정된 건을 읽는다. DB 계정은 `PAO_MAINT_ENV_FILE`(기본 maintenance_list/.env)에서 실행할 때 읽고, 접속은 전용 터널로 해서 `run.sh`가 꺼져 있어도 된다. 터널이 끊겨 있으면 그 주기는 건너뛰고 브리핑에 표시한다.
- 2325 단계가 기준이다: 수정중은 진행 중, 고객사 소통중은 막힘, 팀장 확인중과 배포완료는 완료, 반려는 취소. 단계가 없으면 ax 상태로 끝났는지만 본다.
- 처음에는 최근 30일 안에 접수된 열린 건만 Task로 만든다(`PAO_MAINT_LOOKBACK_DAYS`). 만든 Task는 2325 상태가 바뀔 때마다 따라간다.
- 2325 쪽 상태가 그대로면 채팅으로 바꾼 상태를 되돌리지 않는다.
- 2325 수집기가 먼저 돌아서 Claude Code 세션이 CS 번호로 2325 Task에 이어진다.

## 업무 자동 트래킹 (Slack 멘션, DM)

내 계정 user token(`SLACK_MY_TOKEN`, `~/.config/genlab-overmind/secrets.json`, Slack 앱 personal-os)으로 나(`PAO_SLACK_USER_ID`)를 멘션한 메시지와 DM, 그룹 DM을 최근 3일치 읽는다. Slack에 쓰기는 하지 않는다. 키를 바꾸려면 `PAO_SLACK_TOKEN_KEY`.
- 스레드 단위로 본다. 나를 멘션한 마지막 메시지가 내 마지막 메시지보다 뒤면 내 차례다.
- 내 차례인 스레드만 Claude가 요청, 해결 보고, 참고로 나눈다.
- 루트에 CS 번호가 있으면 내 2325 Task에 잇는다. 요청이 왔는데 Task가 막힘이면 진행 중으로 바꾸고, 해결 보고면 완료 제안을 남긴다. 내 2325 Task가 없는 CS 스레드는 남의 담당이라 건너뛴다.
- CS 번호가 없는 요청은 새 Task로 만들고, 내가 그 스레드에 답하면 완료 제안을 남긴다.
- 내 차례인 요청에는 답변 초안을 만들어 보드 맨 위에 보여 준다. 고쳐서 복사하고 Slack에서 직접 보낸다. 시스템은 보내지 않는다. 내가 답하면 초안은 사라지고, 모르는 내용은 `[확인 필요: ...]`로 남는다.
- DM과 그룹 DM은 내 마지막 메시지 뒤에 상대 메시지가 있으면 내 차례로 본다. 답하지 않은 메시지 묶음 하나가 Task 하나다. 봇과 Slackbot은 건너뛰고, 잡담이나 공유만 있는 대화는 Task를 만들지 않는다.

화면은 http://localhost:8000 이다. 탭은 보드(`#board`), 그래프(`#graph`), 채팅(`#chat`)이고, 브라우저에서 "앱으로 설치"하면 따로 창으로 뜬다.

프런트엔드를 고친 뒤에는 다시 빌드한다. 서비스 재시작은 필요 없다.

```bash
cd frontend && pnpm install && pnpm build
```

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
