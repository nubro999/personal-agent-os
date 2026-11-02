import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import { getGraph, type Graph, type GraphNode } from "./api";

const COLOR: Record<GraphNode["kind"], string> = {
  task: "#8a8d93",
  project: "#a88bfa",
  source: "#5fb3c9",
};
const KIND_LABEL: Record<GraphNode["kind"], string> = {
  task: "할 일",
  project: "프로젝트",
  source: "출처",
};
const DIM = "#3a3a3a";
const LABEL_ZOOM = 1.6;
const CHARGE = -220;
const LINK_DISTANCE = 55;
const FIT_MS = 500;
const FIT_PADDING = 70;

type Node = GraphNode & { x?: number; y?: number; degree: number };
type LinkEnd = string | Node;
type Link = { source: LinkEnd; target: LinkEnd; kind: string };

const endId = (end: LinkEnd) => (typeof end === "string" ? end : end.id);

function useSize() {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => {
      setSize({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return { ref, size };
}

export function GraphView() {
  const [graph, setGraph] = useState<Graph | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set());
  const { ref, size } = useSize();
  const fg = useRef<ForceGraphMethods | undefined>(undefined);

  useEffect(() => {
    getGraph()
      .then(setGraph)
      .catch((cause: Error) => setError(cause.message));
  }, []);

  // force-graph가 노드와 링크 객체를 고치므로 표시용 복사본을 만든다.
  const data = useMemo(() => {
    if (!graph) return { nodes: [] as Node[], links: [] as Link[] };
    const visible = graph.nodes.filter((n) => !hiddenKinds.has(n.kind));
    const ids = new Set(visible.map((n) => n.id));
    const links = graph.links
      .filter((l) => ids.has(l.source) && ids.has(l.target))
      .map((l) => ({ ...l }));
    const degree = new Map<string, number>();
    for (const link of links) {
      degree.set(link.source, (degree.get(link.source) ?? 0) + 1);
      degree.set(link.target, (degree.get(link.target) ?? 0) + 1);
    }
    const nodes = visible.map((n) => ({ ...n, degree: degree.get(n.id) ?? 0 }));
    return { nodes, links: links as Link[] };
  }, [graph, hiddenKinds]);

  // 기본값은 노드가 가운데에 뭉쳐서 글자가 겹친다. 밀어내는 힘과 링크 길이를 키운다.
  useEffect(() => {
    const graphRef = fg.current;
    if (!graphRef) return;
    graphRef.d3Force("charge")?.strength(CHARGE);
    graphRef.d3Force("link")?.distance(LINK_DISTANCE);
    graphRef.d3ReheatSimulation();
  }, [data, size.width > 0]);

  const neighbors = useMemo(() => {
    const map = new Map<string, Set<string>>();
    const add = (a: string, b: string) => {
      if (!map.has(a)) map.set(a, new Set());
      map.get(a)!.add(b);
    };
    for (const link of data.links) {
      const a = endId(link.source);
      const b = endId(link.target);
      add(a, b);
      add(b, a);
    }
    return map;
  }, [data]);

  const focus = hover ?? selected;
  const needle = query.trim().toLowerCase();
  const isLit = (node: Node) => {
    if (needle) return node.label.toLowerCase().includes(needle);
    if (!focus) return true;
    return node.id === focus || (neighbors.get(focus)?.has(node.id) ?? false);
  };

  const current = data.nodes.find((n) => n.id === selected) ?? null;
  const linked = current
    ? data.nodes.filter((n) => neighbors.get(current.id)?.has(n.id))
    : [];

  const toggleKind = (kind: string) =>
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });

  return (
    <div className="flex h-full">
      <div ref={ref} className="relative min-w-0 flex-1 overflow-hidden">
        <div className="absolute left-4 top-4 z-10 flex items-center gap-2">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="노드 찾기"
            aria-label="노드 찾기"
            className="w-48 rounded border border-line bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
          />
          {(Object.keys(KIND_LABEL) as GraphNode["kind"][]).map((kind) => (
            <button
              key={kind}
              onClick={() => toggleKind(kind)}
              className={`flex items-center gap-1.5 rounded border border-line bg-panel px-2 py-1 text-xs ${
                hiddenKinds.has(kind) ? "text-muted opacity-50" : ""
              }`}
            >
              <span className="h-2 w-2 rounded-full" style={{ background: COLOR[kind] }} />
              {KIND_LABEL[kind]}
            </button>
          ))}
        </div>
        {error && <div className="absolute left-4 top-14 text-sm text-warn">{error}</div>}
        {size.width > 0 && (
          <ForceGraph2D
            ref={fg as never}
            onEngineStop={() => fg.current?.zoomToFit(FIT_MS, FIT_PADDING)}
            width={size.width}
            height={size.height}
            graphData={data as never}
            backgroundColor="#1e1e1e"
            cooldownTime={4000}
            linkColor={(link) => {
              const l = link as unknown as Link;
              if (!focus || needle) return "#3f3f3f";
              const touches = endId(l.source) === focus || endId(l.target) === focus;
              return touches ? "#a88bfa" : "#2c2c2c";
            }}
            linkWidth={0.6}
            nodeRelSize={1}
            onNodeHover={(node) => setHover((node as Node | null)?.id ?? null)}
            onNodeClick={(node) => setSelected((node as Node).id)}
            onBackgroundClick={() => setSelected(null)}
            nodePointerAreaPaint={(raw, color, ctx) => {
              const node = raw as unknown as Node;
              ctx.fillStyle = color;
              ctx.beginPath();
              ctx.arc(node.x ?? 0, node.y ?? 0, 3 + Math.sqrt(node.degree) * 1.6, 0, 2 * Math.PI);
              ctx.fill();
            }}
            nodeCanvasObject={(raw, ctx, scale) => {
              const node = raw as unknown as Node;
              const lit = isLit(node);
              const radius = 2 + Math.sqrt(node.degree) * 1.4;
              const x = node.x ?? 0;
              const y = node.y ?? 0;
              ctx.beginPath();
              ctx.arc(x, y, radius, 0, 2 * Math.PI);
              ctx.fillStyle = lit ? COLOR[node.kind] : DIM;
              ctx.fill();
              const showLabel =
                lit && (scale >= LABEL_ZOOM || node.kind !== "task" || Boolean(focus) || Boolean(needle));
              if (!showLabel) return;
              const text = node.label.length > 26 ? `${node.label.slice(0, 26)}…` : node.label;
              ctx.font = `${11 / scale}px Pretendard, "Malgun Gothic", sans-serif`;
              ctx.textAlign = "center";
              ctx.textBaseline = "top";
              ctx.fillStyle = "#dcddde";
              ctx.fillText(text, x, y + radius + 2 / scale);
            }}
          />
        )}
      </div>
      {current && (
        <aside className="w-72 shrink-0 overflow-y-auto border-l border-line bg-panel p-4">
          <div className="mb-1 text-xs text-muted">{KIND_LABEL[current.kind]}</div>
          <h2 className="mb-2 break-words font-semibold leading-snug">{current.label}</h2>
          {current.detail && <div className="mb-3 text-xs text-muted">{current.detail}</div>}
          <div className="mb-1 text-xs font-semibold text-muted">연결 {linked.length}</div>
          {linked.map((node) => (
            <button
              key={node.id}
              onClick={() => setSelected(node.id)}
              className="block w-full truncate rounded px-1.5 py-1 text-left text-sm hover:bg-raised"
            >
              {node.label}
            </button>
          ))}
        </aside>
      )}
    </div>
  );
}
