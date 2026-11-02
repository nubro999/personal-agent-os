export type Task = {
  id: string;
  version: number;
  title: string;
  project: string | null;
  status: string;
  priority: string;
  due_date: string | null;
  source: string;
  updated_at: string;
  suggestion: string | null;
};

export type Draft = {
  id: string;
  task_title: string | null;
  draft: string;
  slack_url: string;
};

export type GraphNode = {
  id: string;
  label: string;
  kind: "task" | "project" | "source";
  status: string | null;
  detail: string | null;
};

export type GraphLink = { source: string; target: string; kind: string };
export type Graph = { nodes: GraphNode[]; links: GraphLink[] };

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`요청 실패 (${response.status})`);
  return response.json() as Promise<T>;
}

export const getTasks = () => fetch("/api/tasks").then(json<Task[]>);
export const getDrafts = () => fetch("/api/drafts").then(json<Draft[]>);
export const getGraph = () => fetch("/api/graph").then(json<Graph>);

export const completeTask = (task: Task) =>
  fetch(`/api/tasks/${task.id}/status`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ status: "done", version: task.version, request_id: crypto.randomUUID() }),
  }).then(json<Task>);

export const dismissSuggestion = (task: Task) =>
  fetch(`/api/tasks/${task.id}/suggestion/dismiss`, { method: "POST" }).then(json);

export const dismissDraft = (draft: Draft) =>
  fetch(`/api/drafts/${draft.id}/dismiss`, { method: "POST" }).then(json);
