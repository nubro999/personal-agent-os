export type Task = {
  id: string;
  version: number;
  title: string;
  description: string | null;
  project: string | null;
  status: string;
  priority: string;
  due_date: string | null;
  source: string;
  created_at: string;
  updated_at: string;
  suggestion: string | null;
};

export type Draft = {
  id: string;
  task_id: string | null;
  task_title: string | null;
  draft: string;
  slack_url: string;
  notes: string[];
  advice: string[];
  rewriting: boolean;
  updated_at: string;
  waiting_since: string | null;
  reminders: number;
  origin: "mention" | "case";
};

export type ThreadMessage = {
  ts: string;
  at: string;
  who: string;
  me: boolean;
  text: string;
};

export type Concept = {
  id: string;
  name: string;
  group: string;
  tier: number;
  key: string;
  owner: string;
  count: number;
};

export type ConceptDetail = Concept & { body: string };

export type Relation = {
  source: string;
  target: string;
  name: string;
  confidence: string;
  basis: string;
};

export type Overview = {
  concepts: Concept[];
  relations: Relation[];
  verbs: Record<string, string>;
  generated_at: string | null;
};

export type InstanceNode = {
  id: string;
  label: string;
  concept: string;
  detail: string;
  source: string;
  degree: number;
  real_key: string;
};

export type InstanceLink = { source: string; target: string; name: string; confidence: string };
export type Expansion = { nodes: InstanceNode[]; links: InstanceLink[]; total: number };

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) throw new Error(`요청 실패 (${response.status})`);
  return response.json() as Promise<T>;
}

export const getTasks = () => fetch("/api/tasks").then(json<Task[]>);
export const getDrafts = () => fetch("/api/drafts").then(json<Draft[]>);
export const getFailedTrackers = () => fetch("/api/trackers/failed").then(json<string[]>);

export type TrackerState = { running: boolean; last_run: string | null };
export const getTrackerState = () => fetch("/api/trackers/state").then(json<TrackerState>);

// 수집기(2325, Slack, 미리 쓰는 회신)를 바로 돌린다. 끝나기는 getTrackerState로 기다린다.
export const runTrackers = async () => {
  const response = await fetch("/api/trackers/run", { method: "POST" });
  const body = (await response.json().catch(() => ({}))) as { detail?: string };
  if (!response.ok) throw new Error(body.detail ?? `수집기를 켜지 못했어요 (${response.status})`);
};
export const getOverview = () => fetch("/api/ontology").then(json<Overview>);
export const getConcept = (id: string) =>
  fetch(`/api/ontology/concepts/${id}`).then(json<ConceptDetail>);
export const getInstances = (concept: string) =>
  fetch(`/api/ontology/instances?concept=${encodeURIComponent(concept)}`).then(json<Expansion>);
export const getNeighbors = (id: string) =>
  fetch(`/api/ontology/neighbors?id=${encodeURIComponent(id)}`).then(json<Expansion>);
export const getGaps = () =>
  fetch("/api/ontology/gaps").then((response) => {
    if (!response.ok) throw new Error(`요청 실패 (${response.status})`);
    return response.text();
  });

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

export const getThread = (draft: Draft) =>
  fetch(`/api/drafts/${draft.id}/thread`).then(json<ThreadMessage[]>);

// 내 이름으로 Slack에 보낸다. 실패하면 서버가 준 이유를 그대로 보여 준다.
export const sendDraft = async (draft: Draft, text: string) => {
  const response = await fetch(`/api/drafts/${draft.id}/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  const body = (await response.json().catch(() => ({}))) as { detail?: string; sent?: string };
  if (!response.ok) throw new Error(body.detail ?? `보내지 못했어요 (${response.status})`);
  return body.sent ?? text;
};

export const adviseDraft = (draft: Draft, text: string) =>
  fetch(`/api/drafts/${draft.id}/advice`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }).then(json);
