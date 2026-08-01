/**
 * Web Worker：力导向图谱布局计算。
 * 将 O(n²) 的 repulsion + collision 离线到独立线程，避免主线程卡顿。
 */

import type { WikiPageType } from "../types";

export interface LayoutInput {
  nodes: { id: string; title: string; type: WikiPageType | "source" }[];
  edges: { source: string; target: string; relation: string }[];
  width: number;
  height: number;
}

export interface LayoutOutput {
  nodes: {
    id: string;
    title: string;
    type: WikiPageType | "source";
    x: number;
    y: number;
    width: number;
    height: number;
    degree: number;
  }[];
  edges: { source: string; target: string; relation: string }[];
}

const NODE_HEIGHT = 52;
const NODE_PADDING_X = 16;
const NODE_MAX_WIDTH = 260;
const NODE_MIN_WIDTH = 90;

function estimateTextWidth(text: string): number {
  let width = 0;
  for (const ch of text) {
    width += ch.charCodeAt(0) > 127 ? 15 : 8;
  }
  return width;
}

// 大图降低迭代次数以减少计算量
function pickIterations(nodeCount: number): number {
  if (nodeCount <= 30) return 250;
  if (nodeCount <= 100) return 150;
  if (nodeCount <= 300) return 80;
  return 50;
}

function computeLayout(input: LayoutInput): LayoutOutput {
  const { nodes: nodesInput, edges: edgesInput, width, height } = input;

  const nodeIds = new Set(nodesInput.map((n) => n.id));
  const edges = edgesInput
    .filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target))
    .map((e) => ({ source: e.source, target: e.target, relation: e.relation }));

  const degreeMap = new Map<string, number>();
  const seenEdges = new Set<string>();
  const dedupedEdges: LayoutOutput["edges"] = [];
  for (const edge of edges) {
    const key =
      edge.source < edge.target
        ? `${edge.source}-${edge.target}`
        : `${edge.target}-${edge.source}`;
    if (!seenEdges.has(key)) {
      seenEdges.add(key);
      dedupedEdges.push(edge);
      degreeMap.set(edge.source, (degreeMap.get(edge.source) || 0) + 1);
      degreeMap.set(edge.target, (degreeMap.get(edge.target) || 0) + 1);
    }
  }

  const maxDegree = Math.max(1, ...degreeMap.values());
  const paddingX = 120;
  const paddingY = 120;
  const effectiveW = Math.max(width - paddingX * 2, 200);
  const effectiveH = Math.max(height - paddingY * 2, 200);

  const nodes = nodesInput.map((n, i) => {
    const degree = degreeMap.get(n.id) || 0;
    const isSource = n.type === "source";
    const textWidth = estimateTextWidth(n.title);
    const degreeBoost = Math.min(40, (degree / maxDegree) * 40);
    const w = isSource
      ? Math.min(140, Math.max(70, textWidth * 0.7 + NODE_PADDING_X))
      : Math.min(
          NODE_MAX_WIDTH,
          Math.max(NODE_MIN_WIDTH + degreeBoost, textWidth + NODE_PADDING_X * 2),
        );
    return {
      id: n.id,
      title: n.title,
      type: n.type,
      x:
        paddingX +
        effectiveW / 2 +
        Math.cos((i * 2 * Math.PI) / Math.max(nodesInput.length, 1)) * (effectiveW / 3),
      y:
        paddingY +
        effectiveH / 2 +
        Math.sin((i * 2 * Math.PI) / Math.max(nodesInput.length, 1)) * (effectiveH / 3),
      width: w,
      height: isSource ? 32 : NODE_HEIGHT,
      degree,
    };
  });

  const nodeMap = new Map<string, (typeof nodes)[number]>();
  for (const n of nodes) nodeMap.set(n.id, n);

  const iterations = pickIterations(nodes.length);
  const repulsion = 45000;
  const springLength = 220;
  const springStrength = 0.025;
  const centerStrength = 0.01;
  const collisionStrength = 0.8;

  // 每 50 次迭代让出一次主线程消息循环（Worker 内也避免长任务阻塞 Worker 消息处理）
  for (let iter = 0; iter < iterations; iter++) {
    const temp = 1 - iter / iterations;

    // Repulsion: O(n²/2)
    for (let a = 0; a < nodes.length; a++) {
      for (let b = a + 1; b < nodes.length; b++) {
        const na = nodes[a];
        const nb = nodes[b];
        let dx = na.x - nb.x;
        let dy = na.y - nb.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = (repulsion * temp) / (dist * dist);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        na.x += fx;
        na.y += fy;
        nb.x -= fx;
        nb.y -= fy;
      }
    }

    // Springs
    for (const edge of dedupedEdges) {
      const s = nodeMap.get(edge.source)!;
      const t = nodeMap.get(edge.target)!;
      const dx = t.x - s.x;
      const dy = t.y - s.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (dist - springLength) * springStrength;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      s.x += fx;
      s.y += fy;
      t.x -= fx;
      t.y -= fy;
    }

    // Collision
    for (let a = 0; a < nodes.length; a++) {
      for (let b = a + 1; b < nodes.length; b++) {
        const na = nodes[a];
        const nb = nodes[b];
        const dx = na.x - nb.x;
        const dy = na.y - nb.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const minDist = (na.width + nb.width) / 2 + (na.height + nb.height) / 4 + 20;
        if (dist < minDist) {
          const overlap = minDist - dist;
          const fx = (dx / dist) * overlap * collisionStrength;
          const fy = (dy / dist) * overlap * collisionStrength;
          na.x += fx;
          na.y += fy;
          nb.x -= fx;
          nb.y -= fy;
        }
      }
    }

    // Center gravity
    const cx = width / 2;
    const cy = height / 2;
    for (const n of nodes) {
      n.x += (cx - n.x) * centerStrength;
      n.y += (cy - n.y) * centerStrength;
      n.x = Math.max(n.width / 2 + 14, Math.min(width - n.width / 2 - 14, n.x));
      n.y = Math.max(n.height / 2 + 14, Math.min(height - n.height / 2 - 14, n.y));
    }
  }

  return { nodes, edges: dedupedEdges };
}

self.onmessage = (e: MessageEvent<LayoutInput>) => {
  const result = computeLayout(e.data);
  self.postMessage(result);
};
