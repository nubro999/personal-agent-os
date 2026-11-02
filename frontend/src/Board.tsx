import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  completeTask,
  dismissDraft,
  dismissSuggestion,
  getDrafts,
  getTasks,
  type Draft,
  type Task,
} from "./api";

const SOURCE: Record<string, string> = {
  natural_language: "직접",
  claude_code: "Claude Code",
  slack: "Slack",
  "2325": "2325",
};
const COLUMNS = [
  { title: "진행 중", status: "in_progress" },
  { title: "할 일", status: "todo" },
  { title: "고객 답 대기", status: "blocked" },
];
const REFRESH_MS = 60_000;

function today(): string {
  return new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
}

function Action({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="rounded border border-line bg-raised px-2 py-0.5 text-xs text-muted hover:border-accent hover:text-accent"
    >
      {label}
    </button>
  );
}

function Card({ task, act }: { task: Task; act: (f: () => Promise<unknown>) => void }) {
  const late = task.due_date !== null && task.due_date <= today();
  return (
    <div className="mb-2 rounded-md border border-line bg-panel p-2.5">
      <div className="break-words leading-snug">{task.title}</div>
      <div className="mt-1 text-xs text-muted">
        {[task.project, SOURCE[task.source] ?? task.source].filter(Boolean).join(" / ")}
        {task.due_date && (
          <span className={late ? "font-semibold text-warn" : ""}> / 마감 {task.due_date}</span>
        )}
      </div>
      {task.suggestion && <div className="mt-1.5 text-xs text-ok">{task.suggestion}</div>}
      <div className="mt-2 flex gap-1.5">
        <Action label="완료" onClick={() => act(() => completeTask(task))} />
        {task.suggestion && (
          <Action label="아니야" onClick={() => act(() => dismissSuggestion(task))} />
        )}
      </div>
    </div>
  );
}

function DraftCard({ draft, act }: { draft: Draft; act: (f: () => Promise<unknown>) => void }) {
  const [text, setText] = useState(draft.draft);
  const [copied, setCopied] = useState(false);
  return (
    <div className="w-96 max-w-full shrink-0 rounded-md border border-line bg-panel p-2.5">
      <div className="mb-1 truncate text-xs text-muted">{draft.task_title ?? "Slack 답변"}</div>
      <textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        rows={4}
        aria-label="답변 초안"
        className="w-full resize-y rounded border border-line bg-bg p-2 text-sm outline-none focus:border-accent"
      />
      <div className="mt-1.5 flex items-center gap-1.5">
        <Action
          label={copied ? "복사했어요" : "복사"}
          onClick={() => void navigator.clipboard.writeText(text).then(() => setCopied(true))}
        />
        <a
          href={draft.slack_url}
          target="_blank"
          rel="noopener"
          className="text-xs text-accent hover:underline"
        >
          Slack에서 열기
        </a>
        <span className="flex-1" />
        <Action label="버리기" onClick={() => act(() => dismissDraft(draft))} />
      </div>
    </div>
  );
}

export function Board() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const [nextTasks, nextDrafts] = await Promise.all([getTasks(), getDrafts()]);
      setTasks(nextTasks);
      setDrafts(nextDrafts);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "불러오지 못했어요");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      // 초안을 고치는 중에는 새로고침으로 화면을 흔들지 않는다.
      if (!root.current?.contains(document.activeElement)) void load();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const act = useCallback(
    (action: () => Promise<unknown>) => {
      action()
        .catch(() => setError("그사이 바뀌었을 수 있어요. 목록을 다시 불러왔어요."))
        .finally(() => void load());
    },
    [load],
  );

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return tasks;
    return tasks.filter((t) => `${t.title} ${t.project ?? ""}`.toLowerCase().includes(needle));
  }, [tasks, query]);

  const suggested = shown.filter((t) => t.suggestion);

  return (
    <div ref={root} className="flex h-full flex-col overflow-hidden p-5">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-lg font-semibold">보드</h1>
        <span className="text-xs text-muted">열린 일 {tasks.length}개</span>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="제목, 프로젝트 찾기"
          aria-label="찾기"
          className="ml-auto w-56 rounded border border-line bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
        />
      </div>
      {error && <div className="mb-3 text-sm text-warn">{error}</div>}

      {drafts.length > 0 && (
        <section className="mb-4">
          <h2 className="mb-1.5 text-xs font-semibold text-muted">답변 초안 {drafts.length}</h2>
          <div className="flex gap-2 overflow-x-auto pb-1">
            {drafts.map((draft) => (
              <DraftCard key={draft.id} draft={draft} act={act} />
            ))}
          </div>
        </section>
      )}

      {suggested.length > 0 && (
        <section className="mb-4">
          <h2 className="mb-1.5 text-xs font-semibold text-muted">
            끝난 것 같은 일 {suggested.length}
          </h2>
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {suggested.map((task) => (
              <Card key={task.id} task={task} act={act} />
            ))}
          </div>
        </section>
      )}

      <div className="grid min-h-0 flex-1 gap-3 md:grid-cols-3">
        {COLUMNS.map((column) => {
          const items = shown.filter((t) => t.status === column.status && !t.suggestion);
          return (
            <section key={column.status} className="flex min-h-0 flex-col">
              <h2 className="mb-1.5 text-xs font-semibold text-muted">
                {column.title} {items.length}
              </h2>
              <div className="min-h-0 flex-1 overflow-y-auto pr-1">
                {items.map((task) => (
                  <Card key={task.id} task={task} act={act} />
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
