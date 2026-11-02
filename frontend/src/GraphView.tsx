import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import {
  getConcept,
  getGaps,
  getInstances,
  getNeighbors,
  getOverview,
  type Concept,
  type ConceptDetail,
  type Expansion,
  type InstanceNode,
  type Overview,
  type Relation,
} from "./api";

const GROUP_COLOR: Record<string, string> = {
  주체: "#a88bfa",
  요청: "#e8a15c",
  "응대 사실": "#5fb3c9",
  "봇 설정": "#7dc99b",
  "브랜드 지식": "#e879a6",
  작업: "#d9c26a",
};
const DIM = "#3a3a3a";
const FALLBACK = "#8a8d93";
const LABEL_ZOOM = 1.4;
const CHARGE = -420;
const LINK_DISTANCE = 90;
const FIT_MS = 500;
const FIT_PADDING = 70;
// 배치가 좁을 때 맞춤이 과하게 확대하면 원이 화면을 덮는다.
const MAX_FIT_ZOOM = 1.6;
const NODE_ZOOM_LIMIT = 1.6;
const INSTANCE_LINK = "instance_of";

type Node = {
  id: string;
  label: string;
  layer: "concept" | "instance";
  group: string;
  count: number;
  degree: number;
  concept?: Concept;
  instance?: InstanceNode;
  x?: number;
  y?: number;
};
type LinkEnd = string | Node;
type Link = {
  key: string;
  source: LinkEnd;
  target: LinkEnd;
  name: string;
  confidence: string;
  basis: string;
};

const endId = (end: LinkEnd) => (typeof end === "string" ? end : end.id);
const conceptNode = (id: string) => `concept:${id}`;

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

function radius(node: Node) {
  if (node.layer === "concept") return 5 + Math.log10(node.count + 1) * 2.4;
  return 2 + Math.min(4, Math.log10(node.degree + 1) * 1.6);
}

function ConfidenceTag({ value }: { value: string }) {
  const tone = value === "확정" ? "text-ok" : value === "모름" ? "text-warn" : "text-muted";
  return <span className={`shrink-0 text-xs ${tone}`}>{value}</span>;
}

export function GraphView() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConceptDetail | null>(null);
  const [query, setQuery] = useState("");
  const [hiddenGroups, setHiddenGroups] = useState<Set<string>>(new Set());
  const [firstTierOnly, setFirstTierOnly] = useState(false);
  const [gapsOn, setGapsOn] = useState(false);
  const [gapsText, setGapsText] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(0);
  const [totals, setTotals] = useState<Record<string, number>>({});
  const { ref, size } = useSize();
  const fg = useRef<ForceGraphMethods | undefined>(undefined);

  // force-graph가 위치를 객체에 적으므로, 펼쳐도 자리가 유지되게 객체를 계속 쓴다.
  const nodes = useRef(new Map<string, Node>());
  const links = useRef(new Map<string, Link>());

  useEffect(() => {
    getOverview()
      .then((data) => {
        nodes.current.clear();
        links.current.clear();
        for (const concept of data.concepts) {
          nodes.current.set(conceptNode(concept.id), {
            id: conceptNode(concept.id),
            label: concept.name,
            layer: "concept",
            group: concept.group,
            count: concept.count,
            degree: 0,
            concept,
          });
        }
        data.relations.forEach((relation: Relation) => {
          const key = `${relation.source}|${relation.name}|${relation.target}`;
          links.current.set(key, {
            key,
            source: conceptNode(relation.source),
            target: conceptNode(relation.target),
            name: relation.name,
            confidence: relation.confidence,
            basis: relation.basis,
          });
        });
        setOverview(data);
      })
      .catch((cause: Error) => setError(cause.message));
  }, []);

  const groupOf = useCallback(
    (conceptId: string) => nodes.current.get(conceptNode(conceptId))?.group ?? "",
    [],
  );

  const merge = useCallback(
    (expansion: Expansion) => {
      for (const node of expansion.nodes) {
        if (!nodes.current.has(node.id)) {
          nodes.current.set(node.id, {
            id: node.id,
            label: node.label,
            layer: "instance",
            group: groupOf(node.concept),
            count: 0,
            degree: node.degree,
            instance: node,
          });
        }
        const toConcept = `${node.id}|${INSTANCE_LINK}`;
        if (!links.current.has(toConcept)) {
          links.current.set(toConcept, {
            key: toConcept,
            source: node.id,
            target: conceptNode(node.concept),
            name: INSTANCE_LINK,
            confidence: "확정",
            basis: "",
          });
        }
      }
      for (const link of expansion.links) {
        const key = `${link.source}|${link.name}|${link.target}`;
        if (
          !links.current.has(key) &&
          nodes.current.has(link.source) &&
          nodes.current.has(link.target)
        ) {
          links.current.set(key, { key, ...link, basis: "" });
        }
      }
      setExpanded((value) => value + 1);
    },
    [groupOf],
  );

  const collapse = () => {
    for (const [id, node] of nodes.current) {
      if (node.layer === "instance") nodes.current.delete(id);
    }
    for (const [key, link] of links.current) {
      if (!endId(link.source).startsWith("concept:") || !endId(link.target).startsWith("concept:")) {
        links.current.delete(key);
      }
    }
    setSelected(null);
    setTotals({});
    setExpanded((value) => value + 1);
  };

  const data = useMemo(() => {
    const shown = [...nodes.current.values()].filter(
      (n) =>
        !hiddenGroups.has(n.group) &&
        !(firstTierOnly && n.layer === "concept" && n.concept?.tier !== 1),
    );
    const ids = new Set(shown.map((n) => n.id));
    return {
      nodes: shown,
      links: [...links.current.values()].filter(
        (l) => ids.has(endId(l.source)) && ids.has(endId(l.target)),
      ),
    };
    // nodes, links는 ref라서 expanded로 다시 계산한다.
  }, [overview, expanded, hiddenGroups, firstTierOnly]);

  // 기본값은 노드가 가운데에 뭉쳐서 글자가 겹친다. 밀어내는 힘과 링크 길이를 키운다.
  // 그래프가 그려진 다음에 걸어야 해서 한 프레임 미룬다.
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const graphRef = fg.current;
      if (!graphRef) return;
      graphRef.d3Force("charge")?.strength(CHARGE);
      graphRef.d3Force("link")?.distance(LINK_DISTANCE);
      graphRef.d3ReheatSimulation();
    });
    return () => cancelAnimationFrame(frame);
  }, [data, size.width > 0]);

  const fitView = () => {
    const graphRef = fg.current;
    if (!graphRef) return;
    graphRef.zoomToFit(FIT_MS, FIT_PADDING);
    setTimeout(() => {
      if (graphRef.zoom() > MAX_FIT_ZOOM) graphRef.zoom(MAX_FIT_ZOOM, FIT_MS);
    }, FIT_MS + 50);
  };

  const neighbors = useMemo(() => {
    const map = new Map<string, Set<string>>();
    const add = (a: string, b: string) => {
      if (!map.has(a)) map.set(a, new Set());
      map.get(a)!.add(b);
    };
    for (const link of data.links) {
      add(endId(link.source), endId(link.target));
      add(endId(link.target), endId(link.source));
    }
    return map;
  }, [data]);

  const focus = hover ?? selected;
  const needle = query.trim().toLowerCase();
  const isLit = (node: Node) => {
    if (gapsOn) return node.layer === "concept" && node.count === 0;
    if (needle) return node.label.toLowerCase().includes(needle);
    if (!focus) return true;
    return node.id === focus || (neighbors.get(focus)?.has(node.id) ?? false);
  };
  const linkLit = (link: Link) => {
    if (gapsOn) return link.confidence !== "확정";
    if (needle || !focus) return !needle;
    return endId(link.source) === focus || endId(link.target) === focus;
  };

  const current = selected ? (nodes.current.get(selected) ?? null) : null;

  useEffect(() => {
    setDetail(null);
    if (!current?.concept) return;
    let alive = true;
    getConcept(current.concept.id)
      .then((value) => alive && setDetail(value))
      .catch((cause: Error) => setError(cause.message));
    return () => {
      alive = false;
    };
  }, [current]);

  const related = useMemo(() => {
    if (!current) return [];
    return data.links.filter(
      (l) =>
        l.name !== INSTANCE_LINK &&
        (endId(l.source) === current.id || endId(l.target) === current.id),
    );
  }, [current, data]);

  const expandConcept = (concept: Concept) => {
    getInstances(concept.id)
      .then((expansion) => {
        merge(expansion);
        setTotals((prev) => ({ ...prev, [concept.id]: expansion.total }));
      })
      .catch((cause: Error) => setError(cause.message));
  };

  const expandInstance = (id: string) => {
    getNeighbors(id)
      .then(merge)
      .catch((cause: Error) => setError(cause.message));
  };

  const toggleGroup = (group: string) =>
    setHiddenGroups((prev) => {
      const next = new Set(prev);
      if (next.has(group)) next.delete(group);
      else next.add(group);
      return next;
    });

  const showGaps = () => {
    const next = !gapsOn;
    setGapsOn(next);
    if (next) {
      setSelected(null);
      getGaps()
        .then(setGapsText)
        .catch((cause: Error) => setError(cause.message));
    }
  };

  const hasInstances = data.nodes.some((n) => n.layer === "instance");
  const nameOf = (id: string) => nodes.current.get(id)?.label ?? id;

  return (
    <div className="flex h-full">
      <div ref={ref} className="relative min-w-0 flex-1 overflow-hidden">
        <div className="absolute left-4 right-4 top-4 z-10 flex flex-wrap items-center gap-1.5">
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="개념 찾기"
            aria-label="개념 찾기"
            className="w-40 rounded border border-line bg-panel px-2 py-1 text-sm outline-none focus:border-accent"
          />
          {Object.entries(GROUP_COLOR).map(([group, color]) => (
            <button
              key={group}
              onClick={() => toggleGroup(group)}
              className={`flex items-center gap-1.5 rounded border border-line bg-panel px-2 py-1 text-xs ${
                hiddenGroups.has(group) ? "text-muted opacity-50" : ""
              }`}
            >
              <span className="h-2 w-2 rounded-full" style={{ background: color }} />
              {group}
            </button>
          ))}
          <button
            onClick={() => setFirstTierOnly((value) => !value)}
            className={`rounded border border-line bg-panel px-2 py-1 text-xs ${
              firstTierOnly ? "text-accent" : "text-muted"
            }`}
          >
            1차만
          </button>
          <button
            onClick={showGaps}
            className={`rounded border border-line bg-panel px-2 py-1 text-xs ${
              gapsOn ? "text-warn" : "text-muted"
            }`}
          >
            빈 부분
          </button>
          {hasInstances && (
            <button
              onClick={collapse}
              className="rounded border border-line bg-panel px-2 py-1 text-xs text-muted hover:text-accent"
            >
              사례 접기
            </button>
          )}
        </div>
        {error && <div className="absolute left-4 top-14 z-10 text-sm text-warn">{error}</div>}
        <div className="absolute bottom-3 left-4 z-10 text-xs text-muted">
          속이 빈 원은 사례가 없는 개념, 점선은 확정 못 한 관계
        </div>
        {size.width > 0 && (
          <ForceGraph2D
            ref={fg as never}
            onEngineStop={() => {
              if (!hasInstances) fitView();
            }}
            width={size.width}
            height={size.height}
            graphData={data as never}
            backgroundColor="#1e1e1e"
            cooldownTime={5000}
            nodeRelSize={1}
            linkCurvature={(raw) => {
              const link = raw as unknown as Link;
              return endId(link.source) === endId(link.target) ? 0.6 : 0.08;
            }}
            linkColor={(raw) => {
              const link = raw as unknown as Link;
              if (link.name === INSTANCE_LINK) return "#2c2c2c";
              return linkLit(link) ? "#6b6b6b" : "#2a2a2a";
            }}
            linkLineDash={(raw) => {
              const link = raw as unknown as Link;
              return link.confidence === "확정" ? null : [3, 3];
            }}
            linkWidth={0.7}
            linkDirectionalArrowLength={(raw) =>
              (raw as unknown as Link).name === INSTANCE_LINK ? 0 : 3
            }
            linkDirectionalArrowRelPos={0.92}
            linkCanvasObjectMode={() => "after"}
            linkCanvasObject={(raw, ctx, scale) => {
              const link = raw as unknown as Link;
              if (link.name === INSTANCE_LINK || !focus || !linkLit(link)) return;
              const source = link.source as Node;
              const target = link.target as Node;
              if (source.x === undefined || target.x === undefined) return;
              const x = (source.x + target.x) / 2;
              const y = ((source.y ?? 0) + (target.y ?? 0)) / 2;
              ctx.font = `${10 / scale}px Pretendard, "Malgun Gothic", sans-serif`;
              ctx.textAlign = "center";
              ctx.textBaseline = "middle";
              ctx.fillStyle = "#a88bfa";
              ctx.fillText(link.name, x, y);
            }}
            onNodeHover={(node) => setHover((node as Node | null)?.id ?? null)}
            onNodeClick={(node) => {
              setGapsOn(false);
              setSelected((node as Node).id);
            }}
            onBackgroundClick={() => setSelected(null)}
            nodePointerAreaPaint={(raw, color, ctx) => {
              const node = raw as unknown as Node;
              ctx.fillStyle = color;
              ctx.beginPath();
              ctx.arc(node.x ?? 0, node.y ?? 0, radius(node) + 2, 0, 2 * Math.PI);
              ctx.fill();
            }}
            nodeCanvasObject={(raw, ctx, scale) => {
              const node = raw as unknown as Node;
              const lit = isLit(node);
              // 확대해도 원은 일정 크기 이상 커지지 않는다.
              const r = radius(node) * Math.min(1, NODE_ZOOM_LIMIT / scale);
              const x = node.x ?? 0;
              const y = node.y ?? 0;
              const color = lit ? (GROUP_COLOR[node.group] ?? FALLBACK) : DIM;
              ctx.beginPath();
              ctx.arc(x, y, r, 0, 2 * Math.PI);
              if (node.layer === "concept" && node.count === 0) {
                ctx.strokeStyle = color;
                ctx.lineWidth = 1.4 / Math.max(1, scale);
                ctx.fillStyle = "#1e1e1e";
                ctx.fill();
                ctx.stroke();
              } else {
                ctx.fillStyle = color;
                ctx.fill();
              }
              const showLabel =
                lit && (node.layer === "concept" || scale >= LABEL_ZOOM || node.id === focus);
              if (!showLabel) return;
              const text = node.label.length > 24 ? `${node.label.slice(0, 24)}…` : node.label;
              ctx.font = `${(node.layer === "concept" ? 12 : 10) / scale}px Pretendard, "Malgun Gothic", sans-serif`;
              ctx.textAlign = "center";
              ctx.textBaseline = "top";
              ctx.fillStyle = "#dcddde";
              ctx.fillText(text, x, y + r + 2 / scale);
            }}
          />
        )}
      </div>

      {gapsOn && gapsText && (
        <aside className="w-96 shrink-0 overflow-y-auto whitespace-pre-wrap border-l border-line bg-panel p-4 text-xs leading-relaxed">
          {gapsText}
        </aside>
      )}

      {current && !gapsOn && (
        <aside className="w-80 shrink-0 overflow-y-auto border-l border-line bg-panel p-4">
          <div className="mb-1 text-xs text-muted">
            {current.layer === "concept"
              ? `개념 / ${current.group}`
              : `사례 / ${nameOf(conceptNode(current.instance?.concept ?? ""))}`}
          </div>
          <h2 className="mb-2 break-words text-base font-semibold leading-snug">{current.label}</h2>

          {detail && (
            <>
              <div className="mb-3 whitespace-pre-wrap break-words text-sm leading-relaxed">
                {detail.body.replace(/^# .*\n+/, "")}
              </div>
              <dl className="mb-3 text-xs">
                <dt className="text-muted">키</dt>
                <dd className="mb-1 break-words">{detail.key}</dd>
                <dt className="text-muted">정본</dt>
                <dd className="break-words">{detail.owner}</dd>
              </dl>
              {detail.count > 0 ? (
                <button
                  onClick={() => expandConcept(detail)}
                  className="mb-3 w-full rounded border border-line bg-raised px-2 py-1 text-sm hover:border-accent hover:text-accent"
                >
                  사례 펼치기 ({detail.count.toLocaleString()}개)
                </button>
              ) : (
                <div className="mb-3 text-xs text-warn">아직 사례를 가져오지 못한 개념이에요.</div>
              )}
              {(totals[detail.id] ?? 0) > 120 && (
                <div className="mb-3 text-xs text-muted">
                  연결이 많은 120개만 펼쳤어요. 전체 {totals[detail.id].toLocaleString()}개.
                </div>
              )}
            </>
          )}

          {current.instance && (
            <>
              {current.instance.detail && (
                <p className="mb-2 break-words text-sm leading-relaxed">{current.instance.detail}</p>
              )}
              {current.instance.source && (
                <p className="mb-1 break-words text-xs text-muted">{current.instance.source}</p>
              )}
              {current.instance.real_key && (
                <p className="mb-1 text-xs text-muted">키 {current.instance.real_key}</p>
              )}
              <div className="mb-3" />
              <button
                onClick={() => expandInstance(current.id)}
                className="mb-3 w-full rounded border border-line bg-raised px-2 py-1 text-sm hover:border-accent hover:text-accent"
              >
                주변 펼치기 ({current.instance.degree}개)
              </button>
            </>
          )}

          <div className="mb-1 text-xs font-semibold text-muted">관계 {related.length}</div>
          <p className="mb-1 text-xs text-muted">마우스를 올리면 동사 뜻과 근거가 나와요.</p>
          {related.map((link) => {
            const outgoing = endId(link.source) === current.id;
            const other = outgoing ? endId(link.target) : endId(link.source);
            return (
              <button
                key={link.key}
                onClick={() => setSelected(other)}
                title={[overview?.verbs[link.name], link.basis].filter(Boolean).join("\n")}
                className="flex w-full items-baseline gap-1.5 rounded px-1.5 py-1 text-left text-sm hover:bg-raised"
              >
                <span className="min-w-0 flex-1 break-words">
                  {outgoing ? (
                    <>
                      <code className="text-xs text-accent">{link.name}</code> {nameOf(other)}
                    </>
                  ) : (
                    <>
                      {nameOf(other)} <code className="text-xs text-accent">{link.name}</code>
                    </>
                  )}
                </span>
                <ConfidenceTag value={link.confidence} />
              </button>
            );
          })}
        </aside>
      )}
    </div>
  );
}
