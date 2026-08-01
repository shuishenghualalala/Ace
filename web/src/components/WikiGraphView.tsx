import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { WikiGraph, WikiGraphNode, WikiPage } from "../types";
import type { LayoutInput, LayoutOutput } from "./wikiGraphLayout.worker";
import WikiIcon from "./WikiIcon";

interface Props {
  kbId: string;
  pages: WikiPage[];
  selectedId: string | null;
  onSelectPage: (page: WikiPage) => void;
}

interface NodeLayout extends WikiGraphNode {
  x: number;
  y: number;
  width: number;
  height: number;
  degree: number;
}

interface EdgeLayout {
  source: string;
  target: string;
  relation: string;
}

interface ViewTransform {
  x: number;
  y: number;
  scale: number;
}

const CANVAS_WIDTH = 1600;
const CANVAS_HEIGHT = 1100;

export default function WikiGraphView({ kbId, pages, selectedId, onSelectPage }: Props) {
  const [graph, setGraph] = useState<WikiGraph | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showSources, setShowSources] = useState(true);
  const [transform, setTransform] = useState<ViewTransform>({ x: 0, y: 0, scale: 1 });
  const svgRef = useRef<SVGSVGElement | null>(null);
  const panRef = useRef<{ startX: number; startY: number; tx: number; ty: number } | null>(null);
  const workerRef = useRef<Worker | null>(null);
  const layoutRequestId = useRef(0);

  // 布局结果（由 Worker 异步返回）
  const [layout, setLayout] = useState<{
    nodes: NodeLayout[];
    edges: EdgeLayout[];
    nodeById: Map<string, NodeLayout>;
    stats: { nodeCount: number; edgeCount: number; hiddenSources: number };
  }>({
    nodes: [],
    edges: [],
    nodeById: new Map(),
    stats: { nodeCount: 0, edgeCount: 0, hiddenSources: 0 },
  });
  const [computingLayout, setComputingLayout] = useState(false);

  // 创建 / 销毁 Worker
  useEffect(() => {
    const worker = new Worker(
      new URL("./wikiGraphLayout.worker.ts", import.meta.url),
      { type: "module" },
    );
    worker.onmessage = (e: MessageEvent<LayoutOutput>) => {
      const result = e.data;
      const nodeById = new Map<string, NodeLayout>();
      for (const node of result.nodes) {
        nodeById.set(node.id, node);
      }
      setLayout({
        nodes: result.nodes,
        edges: result.edges,
        nodeById,
        stats: {
          nodeCount: result.nodes.length,
          edgeCount: result.edges.length,
          hiddenSources: 0,
        },
      });
      setComputingLayout(false);
    };
    // Worker 出错时回退：清空布局但不报错
    worker.onerror = () => {
      setComputingLayout(false);
    };
    workerRef.current = worker;
    return () => {
      worker.terminate();
      workerRef.current = null;
    };
  }, []);

  // graph / showSources 变化时交给 Worker 计算布局
  useEffect(() => {
    if (!graph || graph.nodes.length === 0) {
      setLayout({
        nodes: [],
        edges: [],
        nodeById: new Map(),
        stats: { nodeCount: 0, edgeCount: 0, hiddenSources: 0 },
      });
      setComputingLayout(false);
      return;
    }
    const filtered = showSources
      ? graph.nodes
      : graph.nodes.filter((n) => n.type !== "source");
    const hiddenSources = graph.nodes.length - filtered.length;

    const reqId = ++layoutRequestId.current;
    setComputingLayout(true);

    if (workerRef.current) {
      const input: LayoutInput = {
        nodes: filtered.map((n) => ({ id: n.id, title: n.title, type: n.type })),
        edges: graph.edges,
        width: CANVAS_WIDTH,
        height: CANVAS_HEIGHT,
      };
      workerRef.current.postMessage(input);
      // 存储 hiddenSources 以便 onmessage 时写回 stats
      const worker = workerRef.current;
      worker.onmessage = (e: MessageEvent<LayoutOutput>) => {
        if (reqId !== layoutRequestId.current) return; // 忽略过期请求
        const result = e.data;
        const nodeById = new Map<string, NodeLayout>();
        for (const node of result.nodes) {
          nodeById.set(node.id, node);
        }
        setLayout({
          nodes: result.nodes,
          edges: result.edges,
          nodeById,
          stats: { nodeCount: result.nodes.length, edgeCount: result.edges.length, hiddenSources },
        });
        setComputingLayout(false);
      };
    }
  }, [graph, showSources]);

  const loadGraph = useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    api
      .wikiGraph(kbId)
      .then((res) => {
        if (cancelled) return;
        setGraph(res.graph);
        setTransform({ x: 0, y: 0, scale: 1 });
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kbId]);

  useEffect(() => {
    const cancel = loadGraph();
    return cancel;
  }, [loadGraph]);

  const pageById = useMemo(() => {
    const map = new Map<string, WikiPage>();
    for (const p of pages) map.set(p.id, p);
    return map;
  }, [pages]);

  // 数据加载或过滤条件变化后，自动适配一次画布
  useEffect(() => {
    if (loading || layout.nodes.length === 0) return;
    const id = requestAnimationFrame(() => handleFit());
    return () => cancelAnimationFrame(id);
  }, [loading, layout.nodes.length]);

  const hasGraph = graph && graph.nodes.length > 0;

  const handleZoom = (factor: number, centerX?: number, centerY?: number) => {
    const rect = svgRef.current?.getBoundingClientRect();
    const scaleBase = rect ? CANVAS_WIDTH / rect.width : 1;
    const cx = centerX !== undefined ? centerX * scaleBase : CANVAS_WIDTH / 2;
    const cy = centerY !== undefined ? centerY * scaleBase : CANVAS_HEIGHT / 2;
    setTransform((prev) => {
      const nextScale = Math.min(5, Math.max(0.25, prev.scale * factor));
      return {
        scale: nextScale,
        x: prev.x + cx * (prev.scale - nextScale),
        y: prev.y + cy * (prev.scale - nextScale),
      };
    });
  };

  const handleFit = () => {
    if (!svgRef.current || layout.nodes.length === 0) {
      setTransform({ x: 0, y: 0, scale: 1 });
      return;
    }
    const rect = svgRef.current.getBoundingClientRect();
    const viewW = rect.width;
    const viewH = rect.height;
    const xs = layout.nodes.map((n) => n.x - n.width / 2);
    const xe = layout.nodes.map((n) => n.x + n.width / 2);
    const ys = layout.nodes.map((n) => n.y - n.height / 2);
    const ye = layout.nodes.map((n) => n.y + n.height / 2);
    const minX = Math.min(...xs) - 40;
    const maxX = Math.max(...xe) + 40;
    const minY = Math.min(...ys) - 40;
    const maxY = Math.max(...ye) + 40;
    const contentW = maxX - minX;
    const contentH = maxY - minY;
    // 目标：让 content 在屏幕中留 20px 边距
    // 屏幕缩放比 = scale * (viewW / CANVAS_WIDTH)
    // 需要 contentW * scale * (viewW / CANVAS_WIDTH) = viewW - 40
    const scaleX = ((viewW - 40) / contentW) * (CANVAS_WIDTH / viewW);
    const scaleY = ((viewH - 40) / contentH) * (CANVAS_HEIGHT / viewH);
    const scale = Math.min(scaleX, scaleY, 1.5);
    const tx = 20 * (CANVAS_WIDTH / viewW) - minX * scale;
    const ty = 20 * (CANVAS_HEIGHT / viewH) - minY * scale;
    setTransform({ x: tx, y: ty, scale });
  };

  const handleWheel = (e: React.WheelEvent<SVGSVGElement>) => {
    e.preventDefault();
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const scaleBase = CANVAS_WIDTH / rect.width;
    const factor = e.deltaY < 0 ? 1.15 : 0.87;
    handleZoom(factor, (e.clientX - rect.left) * scaleBase, (e.clientY - rect.top) * scaleBase);
  };

  const handleMouseDown = (e: React.MouseEvent<SVGSVGElement>) => {
    // 仅在点击画布空白处（target 为 svg 本身）时开始平移
    if (e.target !== svgRef.current) return;
    e.preventDefault();
    panRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      tx: transform.x,
      ty: transform.y,
    };
  };

  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!panRef.current || !svgRef.current) return;
    const rect = svgRef.current.getBoundingClientRect();
    const scaleBase = CANVAS_WIDTH / rect.width;
    const dx = (e.clientX - panRef.current.startX) * scaleBase;
    const dy = (e.clientY - panRef.current.startY) * scaleBase;
    setTransform((prev) => ({ ...prev, x: panRef.current!.tx + dx, y: panRef.current!.ty + dy }));
  };

  const endPan = () => {
    panRef.current = null;
  };

  const handleDoubleClick = (e: React.MouseEvent<SVGSVGElement>) => {
    if (e.target !== svgRef.current) return;
    handleFit();
  };

  return (
    <div className="wiki-graph-view">
      <div className="wiki-graph-view__toolbar">
        <div className="wiki-graph-view__controls">
          <button type="button" onClick={loadGraph} title="刷新图谱">
            刷新
          </button>
          <label className="wiki-graph-view__toggle" title="隐藏来源节点可减少杂乱">
            <input
              type="checkbox"
              checked={showSources}
              onChange={(e) => setShowSources(e.target.checked)}
            />
            <span>显示来源节点</span>
          </label>
        </div>
        <span className="wiki-graph-view__zoom">
          {hasGraph
            ? `${layout.stats.nodeCount} 节点 · ${layout.stats.edgeCount} 关系${layout.stats.hiddenSources > 0 ? ` · ${layout.stats.hiddenSources} 来源已隐藏` : ""} · ${Math.round(transform.scale * 100)}%`
            : ""}
        </span>
      </div>
      <div className="wiki-graph-view__canvas">
        {loading && <div className="wiki-graph-view__overlay">加载图谱中…</div>}
        {!loading && error && (
          <div className="wiki-graph-view__overlay">
            加载图谱失败：{error}
            <button type="button" className="wiki-graph-view__retry" onClick={loadGraph}>
              重试
            </button>
          </div>
        )}
        {!loading && !error && !hasGraph && (
          <div className="wiki-graph-view__overlay">当前知识库没有页面，无法生成图谱。</div>
        )}
        {computingLayout && hasGraph && (
          <div className="wiki-graph-view__overlay">计算布局中…</div>
        )}
        {!computingLayout && hasGraph && (
          <svg
            ref={svgRef}
            className={["wiki-graph-view__svg", panRef.current ? "wiki-graph-view__svg--panning" : ""].join(" ")}
            viewBox={`0 0 ${CANVAS_WIDTH} ${CANVAS_HEIGHT}`}
            preserveAspectRatio="xMidYMid meet"
            onWheel={handleWheel}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={endPan}
            onMouseLeave={endPan}
            onDoubleClick={handleDoubleClick}
          >
            <defs>
              <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
                <polygon points="0 0, 10 3.5, 0 7" fill="var(--border-strong)" />
              </marker>
            </defs>
            <g transform={`translate(${transform.x}, ${transform.y}) scale(${transform.scale})`}>
              {layout.edges.map((edge, idx) => {
                const s = layout.nodeById.get(edge.source);
                const t = layout.nodeById.get(edge.target);
                if (!s || !t) return null;
                return (
                  <line
                    key={`${edge.source}-${edge.target}-${edge.relation}-${idx}`}
                    className={`wiki-graph-edge wiki-graph-edge--${edge.relation}`}
                    x1={s.x}
                    y1={s.y}
                    x2={t.x}
                    y2={t.y}
                    markerEnd={edge.relation === "source_of" ? "url(#arrowhead)" : undefined}
                  />
                );
              })}
              {layout.nodes.map((node) => {
                const selected = node.id === selectedId;
                const isSourceNode = node.type === "source";
                const page = pageById.get(node.id);
                const visualType =
                  node.type === "comparison" || node.type === "synthesis"
                    ? "topic"
                    : node.type || "topic";
                const typeClass = `wiki-graph-node--${visualType}`;
                return (
                  <g
                    key={node.id}
                    className={[
                      "wiki-graph-node",
                      typeClass,
                      selected ? "wiki-graph-node--active" : "",
                      isSourceNode ? "wiki-graph-node--source" : "",
                    ].join(" ")}
                    transform={`translate(${node.x - node.width / 2}, ${node.y - node.height / 2})`}
                    onClick={() => {
                      if (page) onSelectPage(page);
                    }}
                    style={{ cursor: page ? "pointer" : "default" }}
                  >
                    {selected && (
                      <rect
                        className="wiki-graph-node__ring"
                        x={-4}
                        y={-4}
                        width={node.width + 8}
                        height={node.height + 8}
                        rx={14}
                        ry={14}
                      />
                    )}
                    <rect
                      className="wiki-graph-node__rect"
                      width={node.width}
                      height={node.height}
                      rx={12}
                      ry={12}
                    />
                    <foreignObject x={0} y={0} width={node.width} height={node.height}>
                      <div className="wiki-graph-node__label" title={node.title}>
                        <WikiIcon name={isSourceNode ? "source" : (visualType as "entity" | "topic")} size={isSourceNode ? 13 : 16} />
                        <span>{node.title}</span>
                      </div>
                    </foreignObject>
                  </g>
                );
              })}
            </g>
          </svg>
        )}
        {hasGraph && (
          <div className="wiki-graph-view__zoombar">
            <button type="button" title="放大" onClick={() => handleZoom(1.2)}>+</button>
            <button type="button" title="缩小" onClick={() => handleZoom(0.83)}>−</button>
            <button type="button" title="适应画布" onClick={handleFit}>适应</button>
            <button type="button" title="重置" onClick={() => setTransform({ x: 0, y: 0, scale: 1 })}>1:1</button>
          </div>
        )}
      </div>
      <div className="wiki-graph-view__hint">
        滚轮缩放，拖拽平移，双击空白处自适应。节点按类型着色，来源节点可隐藏。
      </div>
    </div>
  );
}
