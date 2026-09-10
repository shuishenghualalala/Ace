/**
 * Wiki 知识库客户端：/api/wiki/* 的类型化封装。
 */
import { getJSON, jsonBody, uploadJSON, withKb } from './transport';
import type { ChatChunk } from './transport';

export type WikiPageStatus = 'published' | 'deprecated';
export type WikiPageType = 'entity' | 'concept' | 'topic' | 'source' | 'comparison' | 'synthesis';
export type WikiConfidence = 'high' | 'medium' | 'low';
export type WikiSourceType = 'upload' | 'url' | 'session' | 'paste' | 'image' | 'video';
export type WikiParseStatus = 'pending' | 'parsed' | 'failed';
export type WikiViewMode = 'timeline' | 'tree' | 'type' | 'graph';

export interface WikiKB {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
}

export interface WikiPage {
  id: string;
  page_type: WikiPageType;
  title: string;
  /** 完整正文；列表接口（brief=1）可能不返回。 */
  content?: string;
  /** 列表摘要（brief=1 时由后端生成），优先用于左侧列表展示。 */
  summary?: string;
  file_path: string;
  sources: string[];
  related: string[];
  status: WikiPageStatus;
  tags: string[];
  created_at: number;
  updated_at: number;
  aliases: string[];
  claims?: WikiClaim[];
  claim_count?: number;
  confidence?: WikiConfidence | null;
  contested?: boolean;
  contradictions?: string[];
  relations?: WikiRelation[];
}

export interface WikiRelation { target: string; relation: string; }
export interface WikiEvidence { source_id: string; locator?: string; excerpt?: string; }
export interface WikiClaim {
  statement: string;
  evidence: WikiEvidence[];
  confidence: WikiConfidence;
  contested: boolean;
  contradictions: string[];
}

export interface WikiSource {
  id: string;
  title: string;
  source_type: WikiSourceType;
  original_path?: string;
  parsed_path?: string;
  file_type?: string;
  size: number;
  created_at: number;
  session_id?: string;
  parse_status: WikiParseStatus;
  parse_error?: string;
  original_sha256?: string;
  content_sha256?: string;
  drift_from?: string;
  is_duplicate: boolean;
  source_url?: string;
}

/** source_id -> 人类可读标题 的映射 */
export type WikiSourceTitles = Record<string, string>;

export interface WikiRelationPage {
  id: string;
  title: string;
  page_type: WikiPageType;
  relation: string;
  direction: 'outgoing' | 'incoming';
}

export interface WikiSourcePage {
  id: string;
  title: string;
  page_type: WikiPageType;
}

/** source_id -> 原始文件元信息 的映射 */
export type WikiSourceFiles = Record<string, { original_path: string; file_type?: string; title?: string }>;

export interface WikiVaultDocument {
  name: 'Home.md' | 'index.md';
  path: string;
  content: string;
  updated_at: number;
}

export interface WikiGraph {
  nodes: WikiGraphNode[];
  edges: WikiGraphEdge[];
}

export interface WikiGraphNode {
  id: string;
  title: string;
  type: WikiPageType | 'source';
}

export interface WikiGraphEdge {
  source: string;
  target: string;
  relation: string;
}

export interface WikiIngestProgress {
  stage: string;
  percent: number;
  label: string;
  source_id: string;
  session_id?: string;
  error?: string;
  detail?: Record<string, unknown>;
}

/** wiki_ingest_progress 帧：/api/wiki/ingest 编译期间经 WS 推送的进度（body 为 WikiIngestProgress）。 */
export interface WikiIngestProgressChunk extends Omit<ChatChunk, 'kind' | 'body'> {
  kind: 'wiki_ingest_progress';
  body: WikiIngestProgress;
}

/** wiki_cards 帧：Wiki Agent 回合结束后经 WS 推送的引用页面卡片（body.pages 为 WikiPage 数组）。 */
export interface WikiCardsChunk extends Omit<ChatChunk, 'kind' | 'body'> {
  kind: 'wiki_cards';
  body: { pages?: WikiPage[]; cards?: WikiPage[] };
}

export interface WikiUploadResult {
  ok: boolean;
  source_id: string;
  title: string;
  source_type?: 'upload' | 'image' | 'video';
  ingested?: boolean;
  needs_confirmation?: boolean;
  /** 解析失败但文件已保存（交给 Wiki Agent 挽救）时返回。 */
  needs_agent_review?: boolean;
  error?: string;
  message?: string;
  pages?: WikiPage[];
  issues?: string[];
}

export interface WikiAgentSessionSummary {
  session_id: string;
  title: string;
  message_count: number;
  updated_at: number;
  created_at?: number;
  workspace_id: 'wiki';
}

export const wikiApi = {
  /** Wiki Agent 专用会话：默认复用当前 KB 最近会话，也可显式新建。 */
  wikiAgentSession: (kbId?: string, opts?: { forceNew?: boolean }) =>
    getJSON<{ ok: boolean; session_id: string; kb_id: string }>(
      `${withKb('/api/wiki/agent-session', kbId)}${opts?.forceNew ? `${kbId ? '&' : '?'}force_new=true` : ''}`,
      { method: 'POST' },
    ),
  wikiAgentSessions: (kbId?: string) =>
    getJSON<{ ok: boolean; kb_id: string; sessions: WikiAgentSessionSummary[] }>(
      withKb('/api/wiki/agent-sessions', kbId),
    ),
  wikiCancelConfirmation: (confirmationId: string, sessionId: string) =>
    getJSON<{ ok: boolean; cancelled: boolean }>(
      `/api/wiki/confirmations/${encodeURIComponent(confirmationId)}/cancel`,
      { method: 'POST', ...jsonBody({ session_id: sessionId }) },
    ),
  wikiKBs: () => getJSON<{ ok: boolean; kbs: WikiKB[] }>('/api/wiki/kbs'),
  /** 初始化 KB（对齐 web WikiHub：无 KB 时自动初始化 default；后端幂等）。 */
  wikiInit: (kbId?: string) =>
    getJSON<{ ok: boolean }>(withKb('/api/wiki/init', kbId), { method: 'POST' }),
  /** 新建知识库（对齐 web WikiHub：kb_id 必填，name 缺省同 id）。 */
  wikiCreateKB: (payload: { kb_id: string; name?: string }) =>
    getJSON<{ ok: boolean; kb: WikiKB }>('/api/wiki/kbs', { method: 'POST', ...jsonBody(payload) }),
  /** 删除知识库及其全部页面（后端禁止删除 default）。 */
  wikiDeleteKB: (kbId: string) =>
    getJSON<{ ok: boolean }>(`/api/wiki/kbs/${encodeURIComponent(kbId)}`, { method: 'DELETE' }),
  wikiVaultDocument: (name: 'Home.md' | 'index.md', kbId?: string) =>
    getJSON<{ ok: boolean; document: WikiVaultDocument }>(
      withKb(`/api/wiki/vault-documents/${encodeURIComponent(name)}`, kbId),
    ),
  wikiPages: (params?: { limit?: number; offset?: number; kb_id?: string; brief?: boolean }) => {
    const p = new URLSearchParams();
    if (params?.limit !== undefined) p.set('limit', String(params.limit));
    if (params?.offset !== undefined) p.set('offset', String(params.offset));
    if (params?.kb_id) p.set('kb_id', params.kb_id);
    p.set('brief', params?.brief === false ? '0' : '1');
    return getJSON<{
      ok: boolean;
      pages: WikiPage[];
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
    }>(`/api/wiki/pages?${p.toString()}`);
  },
  wikiPage: (id: string, kbId?: string) =>
    getJSON<{
      ok: boolean;
      page: WikiPage;
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
      source_pages: WikiSourcePage[];
      relation_pages: WikiRelationPage[];
    }>(withKb(`/api/wiki/pages/${encodeURIComponent(id)}`, kbId)),
  wikiCreatePage: (
    payload: Pick<WikiPage, 'title' | 'content'> & Partial<Pick<WikiPage, 'page_type' | 'status'>>,
    kbId?: string,
  ) =>
    getJSON<{
      ok: boolean;
      page: WikiPage;
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
    }>(withKb('/api/wiki/pages', kbId), { method: 'POST', ...jsonBody(payload) }),
  wikiUpdatePage: (
    id: string,
    payload: Partial<Pick<WikiPage, 'title' | 'content' | 'tags' | 'sources' | 'relations'>>,
    kbId?: string,
  ) =>
    getJSON<{
      ok: boolean;
      page: WikiPage;
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
      source_pages: WikiSourcePage[];
      relation_pages: WikiRelationPage[];
    }>(withKb(`/api/wiki/pages/${encodeURIComponent(id)}`, kbId), {
      method: 'PUT',
      ...jsonBody(payload),
    }),
  wikiSearch: (query: string, kbId?: string, topK = 5) =>
    getJSON<{
      ok: boolean;
      pages: WikiPage[];
      source_titles: WikiSourceTitles;
      source_files: WikiSourceFiles;
    }>(withKb(`/api/wiki/search?q=${encodeURIComponent(query)}&top_k=${topK}`, kbId)),
  /** 知识图谱（Phase 3）：全量节点 + 关系边，不走分页。 */
  wikiGraph: (kbId?: string) =>
    getJSON<{ ok: boolean; graph: WikiGraph }>(withKb('/api/wiki/graph', kbId)),
  /** 上传本地文件（走主进程 gateway:upload IPC，一次一个文件）。 */
  wikiUpload: (filePath: string, kbId?: string) =>
    uploadJSON<WikiUploadResult>(withKb('/api/wiki/upload', kbId), filePath),
  /** 编译 source 为 Wiki 页面；传 sessionId 时后端经 WS 推送 wiki_ingest_progress。 */
  wikiIngest: (sourceId: string, kbId?: string, sessionId?: string) =>
    getJSON<{ ok: boolean; source_id: string; pages: WikiPage[]; issues: string[] }>(
      withKb('/api/wiki/ingest', kbId),
      { method: 'POST', ...jsonBody({ source_id: sourceId, session_id: sessionId ?? '' }) },
    ),
  /** 把一段正文直接存进 Wiki（如浏览器标签页）；kb_id 缺省时后端回落默认 KB。 */
  wikiCaptureText: (payload: { title: string; content: string; source_url?: string; kb_id?: string }) =>
    getJSON<{ ok: boolean; source_id: string; pages: WikiPage[] }>('/api/wiki/capture', {
      method: 'POST',
      ...jsonBody(payload),
    }),
  wikiCancelIngest: (sourceId: string, kbId?: string) =>
    getJSON<{ ok: boolean; cancelled?: boolean }>(withKb('/api/wiki/ingest/cancel', kbId), {
      method: 'POST',
      ...jsonBody({ source_id: sourceId }),
    }),
  wikiDeletePage: (id: string, kbId?: string) =>
    getJSON<{ ok: boolean }>(withKb(`/api/wiki/pages/${encodeURIComponent(id)}`, kbId), { method: 'DELETE' }),
  wikiDeletePages: (ids: string[], kbId?: string) =>
    getJSON<{ ok: boolean; deleted: string[]; failed: Array<{ id: string; error: string }> }>(
      withKb('/api/wiki/pages', kbId),
      { method: 'DELETE', ...jsonBody({ page_ids: ids }) },
    ),
};
