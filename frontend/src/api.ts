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
