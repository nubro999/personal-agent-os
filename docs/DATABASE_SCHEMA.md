# Task Persistence Schema — Draft v1

PostgreSQL is the Task SSOT.

**적용 상태 (2026-10-30):** 아래 4개 테이블과 인덱스는 `backend/app/persistence/models.py`와 migration `0001`로 구현됐다. 차이점:
- `pgcrypto` 확장은 만들지 않는다. PostgreSQL 13부터 `gen_random_uuid()`가 기본 내장이고 로컬은 17을 쓴다.
- 제약 이름은 naming convention을 따른다 (`ck_tasks_status`, `uq_tasks_owner_id_request_id` 등).
- `updated_at`은 DB trigger 없이 ORM `onupdate`로 갱신한다. Core `UPDATE`를 직접 쓸 때는 값을 같이 넣어야 한다.
- migration `0002`로 설계에 없던 테이블 두 개를 더했다.
  - `task_suggestions`: 수집기가 낸 완료 제안. `(task_id, kind, source_reference)` unique, status는 pending, accepted, dismissed.
  - `reply_drafts`(0004): Slack 답변 초안. `(owner_id, reference)` unique, status는 pending, used, dismissed.
  - `tracker_runs`(0003): 수집기 실행 결과. 브리핑의 실패 표시용, 14일 보관.
  - `activity_sessions`: 수집기가 세션을 어디까지 읽었는지(`processed_lines`)와 세션 요약. `(owner_id, source, external_id)` unique. 원문은 저장하지 않는다.

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  request_id TEXT,
  title TEXT NOT NULL,
  description TEXT,
  project TEXT,
  status TEXT NOT NULL DEFAULT 'todo'
    CHECK (status IN ('todo','in_progress','blocked','done','cancelled')),
  priority TEXT NOT NULL DEFAULT 'medium'
    CHECK (priority IN ('low','medium','high','urgent')),
  due_date DATE,
  due_at TIMESTAMPTZ,
  scheduled_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  estimated_minutes INTEGER CHECK (estimated_minutes IS NULL OR estimated_minutes > 0),
  source TEXT NOT NULL DEFAULT 'natural_language',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (owner_id, request_id)
);

CREATE TABLE task_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES tasks(id),
  request_id TEXT,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  before_data JSONB,
  after_data JSONB,
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE task_field_evidence (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id UUID NOT NULL REFERENCES tasks(id),
  field_name TEXT NOT NULL,
  field_value JSONB NOT NULL,
  origin TEXT NOT NULL CHECK (origin IN ('explicit','inferred','default','external')),
  source_reference TEXT,
  explanation TEXT,
  confirmed BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE task_links (
  parent_task_id UUID NOT NULL REFERENCES tasks(id),
  child_task_id UUID NOT NULL REFERENCES tasks(id),
  relation TEXT NOT NULL CHECK (relation IN ('subtask','blocks','related')),
  PRIMARY KEY (parent_task_id, child_task_id, relation),
  CHECK (parent_task_id <> child_task_id)
);

CREATE INDEX idx_tasks_owner_status_due ON tasks(owner_id,status,due_date);
CREATE INDEX idx_task_events_task_created ON task_events(task_id,created_at DESC);
```

## Constraints and implementation details
- `due_date` is date-only deadlines; `due_at` is precise timestamp. Do not fabricate 23:59.
- `request_id` must come from authenticated request envelope, not LLM output. Unique per owner. For update/event operations, introduce an operation idempotency ledger if needed; `tasks.request_id` alone only protects creation.
- Task mutation and audit event insert run in **one transaction**.
- Use optimistic concurrency: `UPDATE ... WHERE id=:id AND version=:expected` and bump version. Return conflict on stale updates.
- For all Task reads/writes constrain `owner_id` from auth context, never LLM-produced user IDs.
- On completion set `completed_at`; on reopen clear it; keep events.
- Validate no `task_links` dependency cycles in application/domain service.
- Redact sensitive text from traces; avoid storing unrestricted raw chat content in `task_events`.
- LangGraph checkpointer tables are managed by checkpointer library separately from domain Task tables.
