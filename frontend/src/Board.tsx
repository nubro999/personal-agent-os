import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  adviseDraft,
  completeTask,
  dismissDraft,
  dismissSuggestion,
  getDrafts,
  getTasks,
  getThread,
  sendDraft,
  type Draft,
  type Task,
  type ThreadMessage,
} from "./api";

const SOURCE: Record<string, string> = {
  natural_language: "직접",
  claude_code: "Claude Code",
  slack: "Slack",
  "2325": "2325",
};
const STATUS: Record<string, string> = {
  in_progress: "진행 중",
  todo: "할 일",
  blocked: "고객 답 대기",
};
const REFRESH_MS = 60_000;
const REWRITE_POLL_MS = 5_000;
const NO_PROJECT = "프로젝트 없음";

type TabId = "reply" | "suggested" | "in_progress" | "blocked" | "todo";
const TABS: { id: TabId; label: string }[] = [
  { id: "reply", label: "답장" },
  { id: "suggested", label: "끝난 것 같음" },
  { id: "in_progress", label: "진행 중" },
  { id: "blocked", label: "고객 답 대기" },
  { id: "todo", label: "할 일" },
];
const KANBAN: TabId[] = ["in_progress", "blocked", "todo"];

type Item =
  | { kind: "draft"; key: string; draft: Draft; task: Task | undefined }
  | { kind: "task"; key: string; task: Task };

const CS = /CS-\d{7}/;

/** 2325 건 제목은 피드백 원문 앞부분이다. 행 번호와 시트 머리를 떼고 피드백 내용부터 보인다. */
export function shortTitle(title: string): { cs: string | null; text: string } {
  const cs = title.match(CS)?.[0] ?? null;
  let text = title.replace(CS, "").replace(/\[\d+행\]/g, "");
  const feedback = text.split("[피드백 내용]")[1]?.replace(/^\s*-?\s*/, "") ?? "";
  if (feedback.replace(/\[[^\]]*\]/g, "").trim().length > 1) text = feedback;
  text = text
    .replace(/\[(수정이 필요한 AI답변|피드백 내용|적정 답변 예시)\]/g, "")
    .replace(/^[\s\-:]+/, "")
    .replace(/\s+/g, " ")
    .trim();
  return { cs, text: text || title };
}

/** Slack 글의 멘션과 링크 꼴을 읽기 좋게 바꾼다. */
function slackText(text: string): string {
  return text
    .replace(/<(https?:[^|>]+)\|([^>]+)>/g, "$2")
    .replace(/<(https?:[^>]+)>/g, "$1");
}

function span(iso: string): string {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60_000));
  if (minutes < 60) return `${minutes}분`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}시간`;
  return `${Math.round(hours / 24)}일`;
}

function clock(iso: string): string {
  return new Date(iso).toLocaleString("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function today(): string {
  return new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
}

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
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

function Row({
  item,
  selected,
  onSelect,
}: {
  item: Item;
  selected: boolean;
  onSelect: () => void;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (selected) ref.current?.scrollIntoView({ block: "nearest" });
  }, [selected]);
  const task = item.task;
  const title = item.kind === "draft" ? (item.draft.task_title ?? "Slack 답변") : item.task.title;
  const { cs, text } = shortTitle(title);
  const late = task?.due_date != null && task.due_date <= today();
  return (
    <button
      ref={ref}
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      className={`block w-full border-b border-l-2 border-b-line px-3 py-2 text-left ${
        selected ? "border-l-accent bg-raised" : "border-l-transparent hover:bg-panel"
      }`}
    >
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate leading-snug">{text}</span>
        {item.kind === "draft" && item.draft.origin === "case" && (
          <span className="shrink-0 rounded bg-accent/15 px-1.5 text-xs text-accent">미리 씀</span>
        )}
        {item.kind === "draft" && item.draft.reminders > 0 && (
          <span className="shrink-0 rounded bg-warn/15 px-1.5 text-xs text-warn">
            리마인드 {item.draft.reminders}
          </span>
        )}
      </div>
      <div className="mt-0.5 flex gap-1.5 truncate text-xs text-muted">
        {cs && <span>{cs}</span>}
        {task?.project && <span>{task.project}</span>}
        {item.kind === "draft" ? (
          item.draft.waiting_since ? (
            <span className="text-warn">{span(item.draft.waiting_since)} 기다림</span>
          ) : (
            <span>{span(item.draft.updated_at)} 전</span>
          )
        ) : (
          <>
            <span>{SOURCE[item.task.source] ?? item.task.source}</span>
            <span>{span(item.task.updated_at)} 전</span>
          </>
        )}
        {item.kind === "draft" && item.draft.rewriting && (
          <span className="text-accent">다시 쓰는 중</span>
        )}
        {late && <span className="font-semibold text-warn">마감 {task?.due_date}</span>}
      </div>
    </button>
  );
}

function Notes({ notes }: { notes: string[] }) {
  if (notes.length === 0) return null;
  return (
    <details className="mt-3 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-xs">
      <summary className="cursor-pointer select-none text-warn">확인할 것 {notes.length}</summary>
      <ul className="mt-2 list-disc space-y-1 pl-4 leading-relaxed text-text/85">
        {notes.map((note) => (
          <li key={note}>{note}</li>
        ))}
      </ul>
    </details>
  );
}

function Thread({ draft }: { draft: Draft }) {
  const [messages, setMessages] = useState<ThreadMessage[] | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    getThread(draft)
      .then((next) => alive && setMessages(next))
      .catch((cause: unknown) => {
        if (alive) setFailed(cause instanceof Error ? cause.message : "스레드를 읽지 못했어요");
      });
    return () => {
      alive = false;
    };
    // 목록을 다시 읽을 때마다 draft 객체가 바뀐다. 다른 초안을 열 때만 다시 읽는다.
  }, [draft.id]);
  if (failed) return <div className="mt-3 text-xs text-warn">{failed}</div>;
  if (!messages) return <div className="mt-3 text-xs text-muted">스레드 읽는 중</div>;
  return (
    <div className="mt-3 space-y-1.5 border-l-2 border-line pl-3">
      {messages.map((m, index) => (
        <div key={m.ts} className="text-xs leading-relaxed">
          <span className={m.me ? "font-semibold text-accent" : "font-semibold text-text"}>
            {m.me ? "나" : m.who}
          </span>
          <span className="ml-1.5 text-muted">{clock(m.at)}</span>
          <div
            className={`whitespace-pre-wrap break-words ${
              index === 0 && messages.length > 1 ? "line-clamp-2 text-muted" : "text-text/85"
            }`}
          >
            {slackText(m.text)}
          </div>
        </div>
      ))}
    </div>
  );
}

function DraftPanel({
  draft,
  task,
  act,
  reload,
}: {
  draft: Draft;
  task: Task | undefined;
  act: (f: () => Promise<unknown>) => void;
  reload: () => void;
}) {
  const [text, setText] = useState(draft.draft);
  const [copied, setCopied] = useState(false);
  const [advice, setAdvice] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const isThread = draft.slack_url.includes("/thread/");
  const { cs, text: title } = shortTitle(draft.task_title ?? "Slack 답변");

  const send = async () => {
    const body = text.trim();
    if (!body || sending || draft.rewriting) return;
    const who = isThread ? "나를 부른 사람을 멘션해서 이 스레드에" : "이 대화에";
    if (!window.confirm(`${who} 내 이름으로 보낼까요?\n\n${body}`)) return;
    setSending(true);
    setSendError(null);
    try {
      await sendDraft(draft, body);
      reload();
    } catch (cause) {
      setSendError(cause instanceof Error ? cause.message : "보내지 못했어요");
      setSending(false);
    }
  };
  const advise = () => {
    const note = advice.trim();
    if (!note || draft.rewriting) return;
    setAdvice("");
    (document.activeElement as HTMLElement | null)?.blur();
    act(() => adviseDraft(draft, note));
  };

  return (
    <div className="max-w-3xl">
      <div className="text-xs text-muted">
        {[cs, task?.project, draft.origin === "case" ? "에이전트 결과로 미리 쓴 회신" : "답장 초안"]
          .filter(Boolean)
          .join(" / ")}
      </div>
      <h2 className="mt-1 text-base font-semibold leading-snug">{title}</h2>
      <div className="mt-1 flex gap-2 text-xs">
        {draft.waiting_since && (
          <span className="text-warn">
            {span(draft.waiting_since)} 기다림
            {draft.reminders > 0 && `, 리마인드 ${draft.reminders}번`}
          </span>
        )}
        <a
          href={draft.slack_url}
          target="_blank"
          rel="noopener"
          className="text-accent hover:underline"
        >
          Slack에서 열기
        </a>
      </div>

      <Thread draft={draft} />

      <textarea
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            void send();
          }
        }}
        rows={Math.min(16, Math.max(6, Math.ceil(text.length / 48) + text.split("\n").length))}
        aria-label="답변 초안"
        disabled={draft.rewriting}
        className="mt-4 w-full resize-y rounded border border-line bg-bg p-3 text-sm leading-relaxed outline-none focus:border-accent disabled:opacity-60"
      />
      <div className="mt-2 flex items-center gap-1.5">
        <button
          onClick={() => void send()}
          disabled={sending || draft.rewriting}
          title="Ctrl+Enter"
          className="rounded border border-accent bg-accent px-3 py-1 text-xs font-semibold text-bg hover:opacity-90 disabled:opacity-50"
        >
          {sending ? "보내는 중" : "보내기"}
        </button>
        <Action
          label={copied ? "복사했어요" : "복사"}
          onClick={() => void navigator.clipboard.writeText(text).then(() => setCopied(true))}
        />
        <span className="flex-1" />
        <Action label="버리기" onClick={() => act(() => dismissDraft(draft))} />
      </div>
      {sendError && <div className="mt-1.5 text-xs text-warn">{sendError}</div>}

      <Notes notes={draft.notes} />

      <div className="mt-3">
        {draft.advice.length > 0 && (
          <div className="mb-1.5 space-y-0.5 text-xs text-ok">
            {draft.advice.map((note, index) => (
              <div key={index}>조언: {note}</div>
            ))}
          </div>
        )}
        <div className="flex gap-1.5">
          <input
            value={advice}
            onChange={(event) => setAdvice(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.nativeEvent.isComposing) advise();
            }}
            disabled={draft.rewriting}
            placeholder={draft.rewriting ? "다시 쓰는 중이에요" : "조언 남기면 다시 써요"}
            aria-label="초안 조언"
            className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-1 text-xs outline-none focus:border-accent disabled:opacity-60"
          />
          <Action label={draft.rewriting ? "쓰는 중" : "다시 쓰기"} onClick={advise} />
        </div>
      </div>
    </div>
  );
}

function TaskPanel({ task, act }: { task: Task; act: (f: () => Promise<unknown>) => void }) {
  const { cs, text } = shortTitle(task.title);
  return (
    <div className="max-w-3xl">
      <div className="text-xs text-muted">
        {[cs, task.project, SOURCE[task.source] ?? task.source, STATUS[task.status] ?? task.status]
          .filter(Boolean)
          .join(" / ")}
      </div>
      <h2 className="mt-1 text-base font-semibold leading-snug">{text}</h2>
      <div className="mt-1 text-xs text-muted">
        {span(task.updated_at)} 전 바뀜{task.due_date && ` / 마감 ${task.due_date}`}
      </div>
      {task.suggestion && (
        <div className="mt-3 rounded border border-ok/40 bg-ok/10 px-3 py-2 text-sm text-ok">
          {task.suggestion}
        </div>
      )}
      <div className="mt-3 flex gap-1.5">
        <Action label="완료 (e)" onClick={() => act(() => completeTask(task))} />
        {task.suggestion && (
          <Action label="아니야" onClick={() => act(() => dismissSuggestion(task))} />
        )}
      </div>
      <div className="mt-4 whitespace-pre-wrap break-words rounded border border-line bg-bg p-3 text-sm leading-relaxed text-text/90">
        {task.description ?? task.title}
      </div>
    </div>
  );
}

function groupByProject(items: Item[]): [string, Item[]][] {
  const groups = new Map<string, Item[]>();
  for (const item of items) {
    const name = item.task?.project ?? NO_PROJECT;
    groups.set(name, [...(groups.get(name) ?? []), item]);
  }
  return [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
}

export function Board() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<TabId | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [grouped, setGrouped] = useState(false);
  const [kanban, setKanban] = useState(false);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);

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
      if (!root.current?.contains(document.activeElement) || !typing(document.activeElement))
        void load();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const rewriting = drafts.some((d) => d.rewriting);
  useEffect(() => {
    // 다시 쓰는 초안이 있으면 끝났는지 자주 본다. 열어 둔 초안은 key가 같아 입력이 유지된다.
    if (!rewriting) return;
    const timer = setInterval(() => void load(), REWRITE_POLL_MS);
    return () => clearInterval(timer);
  }, [rewriting, load]);

  const act = useCallback(
    (action: () => Promise<unknown>) => {
      action()
        .catch(() => setError("그사이 바뀌었을 수 있어요. 목록을 다시 불러왔어요."))
        .finally(() => void load());
    },
    [load],
  );

  const lists = useMemo(() => {
    const byId = new Map(tasks.map((t) => [t.id, t]));
    const needle = query.trim().toLowerCase();
    const hit = (text: string) => !needle || text.toLowerCase().includes(needle);
    const taskItems = (keep: (t: Task) => boolean): Item[] =>
      tasks
        .filter((t) => keep(t) && hit(`${t.title} ${t.project ?? ""} ${t.description ?? ""}`))
        // 새로 들어온 일부터. 수집기가 제목만 고쳐도 updated_at이 바뀌어 순서로 쓰지 않는다.
        .sort((a, b) => b.created_at.localeCompare(a.created_at))
        .map((t) => ({ kind: "task", key: `task:${t.id}`, task: t }));
    // 답장은 리마인드가 많은 것, 그다음 오래 기다린 것부터.
    const reply: Item[] = drafts
      .map((d) => ({
        kind: "draft" as const,
        key: `draft:${d.id}`,
        draft: d,
        task: d.task_id ? byId.get(d.task_id) : undefined,
      }))
      .filter((i) => hit(`${i.draft.task_title ?? ""} ${i.task?.project ?? ""} ${i.draft.draft}`))
      .sort(
        (a, b) =>
          b.draft.reminders - a.draft.reminders ||
          (a.draft.waiting_since ?? a.draft.updated_at).localeCompare(
            b.draft.waiting_since ?? b.draft.updated_at,
          ),
      );
    return {
      reply,
      suggested: taskItems((t) => Boolean(t.suggestion)),
      in_progress: taskItems((t) => t.status === "in_progress" && !t.suggestion),
      blocked: taskItems((t) => t.status === "blocked" && !t.suggestion),
      todo: taskItems((t) => t.status === "todo" && !t.suggestion),
    } satisfies Record<TabId, Item[]>;
  }, [tasks, drafts, query]);

  // 답장할 것이 있으면 답장부터 연다.
  const current: TabId = tab ?? (drafts.length > 0 ? "reply" : "in_progress");
  const items = lists[current];
  const groups = useMemo(
    () => (grouped ? groupByProject(items) : [["", items] as [string, Item[]]]),
    [grouped, items],
  );
  // 키보드로 오가는 순서. 접은 묶음은 건너뛴다.
  const visible = useMemo(
    () => groups.flatMap(([name, group]) => (folded.has(name) ? [] : group)),
    [groups, folded],
  );
  // 고른 것이 사라지면(보냄, 완료) 목록 맨 위를 연다.
  const open = visible.find((i) => i.key === selected) ?? visible[0];

  const pick = (tabId: TabId, key: string | null = null) => {
    setTab(tabId);
    setSelected(key);
    setKanban(false);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        (document.activeElement as HTMLElement | null)?.blur();
        return;
      }
      if (typing(event.target) || event.ctrlKey || event.metaKey || event.altKey) return;
      const index = open ? visible.indexOf(open) : -1;
      if (event.key === "j" || event.key === "ArrowDown") {
        const next = visible[Math.min(visible.length - 1, index + 1)];
        if (next) setSelected(next.key);
      } else if (event.key === "k" || event.key === "ArrowUp") {
        const prev = visible[Math.max(0, index - 1)];
        if (prev) setSelected(prev.key);
      } else if (event.key === "/") {
        search.current?.focus();
      } else if (event.key === "e" && open?.kind === "task") {
        act(() => completeTask(open.task));
      } else if (/^[1-5]$/.test(event.key)) {
        pick(TABS[Number(event.key) - 1].id);
      } else {
        return;
      }
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible, open, act]);

  const toggleFold = (name: string) =>
    setFolded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });

  return (
    <div ref={root} className="flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-1 border-b border-line px-4 py-2">
        <h1 className="mr-3 text-lg font-semibold">보드</h1>
        {TABS.map((t, index) => (
          <button
            key={t.id}
            onClick={() => pick(t.id)}
            title={`${index + 1}`}
            aria-current={!kanban && current === t.id ? "page" : undefined}
            className={`rounded px-2.5 py-1 text-sm ${
              !kanban && current === t.id ? "bg-raised text-accent" : "text-muted hover:text-text"
            }`}
          >
            {t.label} <span className="text-xs">{lists[t.id].length}</span>
          </button>
        ))}
        <span className="mx-2 h-4 w-px bg-line" />
        <button
          onClick={() => setGrouped((g) => !g)}
          aria-pressed={grouped}
          className={`rounded px-2 py-1 text-xs ${grouped ? "bg-raised text-accent" : "text-muted hover:text-text"}`}
        >
          브랜드로 묶기
        </button>
        <button
          onClick={() => setKanban((k) => !k)}
          aria-pressed={kanban}
          className={`rounded px-2 py-1 text-xs ${kanban ? "bg-raised text-accent" : "text-muted hover:text-text"}`}
        >
          칸반
        </button>
        <input
          ref={search}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="CS 번호, 브랜드, 내용 찾기 (/)"
          aria-label="찾기"
          className="ml-auto w-64 rounded border border-line bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
        />
      </div>
      {error && <div className="px-4 pt-2 text-sm text-warn">{error}</div>}

      {kanban ? (
        <div className="grid min-h-0 flex-1 gap-3 p-4 md:grid-cols-3">
          {KANBAN.map((status) => (
            <section key={status} className="flex min-h-0 flex-col">
              <h2 className="mb-1.5 text-xs font-semibold text-muted">
                {STATUS[status]} {lists[status].length}
              </h2>
              <div className="min-h-0 flex-1 overflow-y-auto rounded border border-line">
                {lists[status].map((item) => (
                  <Row
                    key={item.key}
                    item={item}
                    selected={false}
                    onSelect={() => pick(status, item.key)}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <div className="max-h-[45%] shrink-0 overflow-y-auto border-b border-line md:max-h-none md:w-[420px] md:border-b-0 md:border-r">
            {items.length === 0 && (
              <div className="px-4 py-6 text-sm text-muted">
                {query ? "찾는 것이 없어요" : "여기는 비어 있어요"}
              </div>
            )}
            {groups.map(([name, group]) => (
              <div key={name || "all"}>
                {grouped && (
                  <button
                    onClick={() => toggleFold(name)}
                    className="sticky top-0 z-10 flex w-full items-center gap-1.5 border-b border-line bg-panel px-3 py-1.5 text-left text-xs font-semibold text-muted hover:text-text"
                  >
                    <span>{folded.has(name) ? "▸" : "▾"}</span>
                    {name}
                    <span className="font-normal">{group.length}</span>
                  </button>
                )}
                {!folded.has(name) &&
                  group.map((item) => (
                    <Row
                      key={item.key}
                      item={item}
                      selected={item.key === open?.key}
                      onSelect={() => setSelected(item.key)}
                    />
                  ))}
              </div>
            ))}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-5">
            {open?.kind === "draft" && (
              <DraftPanel
                key={`${open.draft.id}:${open.draft.draft}`}
                draft={open.draft}
                task={open.task}
                act={act}
                reload={() => void load()}
              />
            )}
            {open?.kind === "task" && <TaskPanel key={open.task.id} task={open.task} act={act} />}
            {!open && (
              <div className="text-sm text-muted">
                j, k로 오가고 /로 찾아요. 1~5로 탭을 바꿔요.
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
