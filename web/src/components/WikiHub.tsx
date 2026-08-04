import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { WikiIngestProgress, WikiKB, WikiPage, WikiRelationPage, WikiSourceFiles, WikiSourceTitles, WikiVaultDocument, WikiViewMode } from "../types";
import WikiPageView from "./WikiPageView";
import WikiFileTree from "./WikiFileTree";
import WikiTimelineView from "./WikiTimelineView";
import WikiTypeView from "./WikiTypeView";
import WikiGraphView from "./WikiGraphView";
import ChatPanel from "./ChatPanel";
import WikiIcon from "./WikiIcon";
import type { Props as ChatPanelProps } from "./ChatPanel";
import { ResizablePanels } from "./ResizablePanels";
import { ancestorPaths, buildFileTree, findPageByTitle, splitHomeQuestions, vaultDocumentLabel } from "../lib/wikiTree";
import MarkdownContent from "./MarkdownContent";

type UploadJobStatus = "uploading" | "ingesting" | "done" | "error" | "cancelled";
const PAGE_LIMIT = 200;

// LLM 分析阶段实际耗时很长，前端用这些本地真实阶段文案循环显示，
// 让用户感觉后台一直在工作，而不是卡在"LLM 分析内容"。
const ANALYZE_VIRTUAL_LABELS = [
  "保存原始页面",
  "提取关键词",
  "提炼概念",
  "生成话题",
  "建立关联",
  "更新索引",
];
const ANALYZE_VIRTUAL_LABEL_INTERVAL_MS = 1800;

interface UploadJob {
  id: string;
  file: File;
  sourceId: string | null;
  title: string;
  stage: string;
  label: string;
  percent: number;
  displayPercent: number;
  status: UploadJobStatus;
  error?: string;
  /** 若存在，错误行会显示“让 AI 处理”按钮，点击后把该 prompt 发给 AI。 */
  aiPrompt?: string;
  // 进入 analyze 阶段的时间戳，用于循环显示虚拟阶段标签。
  analyzeStartedAt?: number;
}

interface Props {
  chatProps: ChatPanelProps;
  kbId: string;
  onKbChange: (id: string) => void;
  sessionId: string;
  wikiProgress?: Record<string, WikiIngestProgress> | null;
}

export default function WikiHub({
  chatProps,
  kbId,
  onKbChange,
  sessionId,
  wikiProgress,
}: Props) {
  const [kbs, setKbs] = useState<WikiKB[]>([]);
  const [pages, setPages] = useState<WikiPage[]>([]);
  /** 已加载完整正文的页面（pageId -> WikiPage），列表只返回 brief。 */
  const [pageDetails, setPageDetails] = useState<Record<string, WikiPage>>({});
  const [relationPages, setRelationPages] = useState<Record<string, WikiRelationPage[]>>({});
  const [pageOffset, setPageOffset] = useState(0);
  const [hasMorePages, setHasMorePages] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [sourceTitles, setSourceTitles] = useState<WikiSourceTitles>({});
  const [sourceFiles, setSourceFiles] = useState<WikiSourceFiles>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedDocumentName, setSelectedDocumentName] = useState<"Home.md" | "index.md" | null>(null);
  const [vaultDocument, setVaultDocument] = useState<WikiVaultDocument | null>(null);
  const [initializedKbId, setInitializedKbId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(
    new Set(["wiki", "wiki/sources"]),
  );
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const [viewMode, setViewMode] = useState<WikiViewMode>("timeline");
  const [highlightedIds, setHighlightedIds] = useState<Set<string>>(new Set());
  const [uploadJobs, setUploadJobs] = useState<UploadJob[]>([]);
  const [uploadJobsExpanded, setUploadJobsExpanded] = useState(true);
  // 右侧知识库面板（目录+详情）展开/收起，持久化到 localStorage。
  const [browserOpen, setBrowserOpen] = useState(() => {
    try {
      return window.localStorage.getItem("crew:wiki-browser-open") !== "0";
    } catch {
      return true;
    }
  });
  const toggleBrowser = () => {
    setBrowserOpen((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem("crew:wiki-browser-open", next ? "1" : "0");
      } catch {
        // ignore storage errors
      }
      return next;
    });
  };

  const { activeCount, hasDoneJobs } = useMemo(() => {
    const activeCount = uploadJobs.filter((j) => j.status === "uploading" || j.status === "ingesting").length;
    const hasDoneJobs = uploadJobs.some((j) => j.status === "done" || j.status === "error" || j.status === "cancelled");
    return { activeCount, hasDoneJobs };
  }, [uploadJobs]);

  const [pendingMedia, setPendingMedia] = useState<{
    source_id: string;
    title: string;
    source_type: "image" | "video";
    needs_confirmation: boolean;
  } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const hasAutoInitRef = useRef(false);
  const stopBatchRef = useRef(false);
  const loadMoreRef = useRef<HTMLDivElement>(null);

  const refreshKbs = useCallback(async (options?: { targetKbId?: string }) => {
    try {
      const res = await api.wikiKBs();
      setKbs(res.kbs);
      const target = options?.targetKbId;
      if (target && res.kbs.some((k) => k.id === target)) {
        if (target !== kbId) {
          onKbChange(target);
        }
      } else if (res.kbs.length > 0 && !res.kbs.some((k) => k.id === kbId)) {
        onKbChange(res.kbs[0].id);
      }
    } catch (err) {
      setMessage(`加载知识库失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }, [kbId, onKbChange]);

  const _mergePageBatch = useCallback((prev: WikiPage[], batch: WikiPage[]) => {
    const seen = new Set(prev.map((p) => p.id));
    const merged = [...prev];
    for (const p of batch) {
      if (!seen.has(p.id)) {
        merged.push(p);
        seen.add(p.id);
      }
    }
    return merged;
  }, []);

  const refreshPages = useCallback(async () => {
    setLoading(true);
    setPageOffset(0);
    setHasMorePages(true);
    try {
      const res = await api.wikiPages({ limit: PAGE_LIMIT, offset: 0, kb_id: kbId, brief: true });
      setPages(res.pages);
      setPageOffset(res.pages.length);
      setHasMorePages(res.pages.length >= PAGE_LIMIT);
      setSourceTitles(res.source_titles || {});
      setSourceFiles(res.source_files || {});
      setSelectedId((prev) => (prev && res.pages.some((p) => p.id === prev) ? prev : null));
    } catch {
      setMessage("加载页面失败");
    } finally {
      setLoading(false);
    }
  }, [kbId, _mergePageBatch]);

  const loadMorePages = useCallback(async () => {
    if (loadingMore || !hasMorePages) return;
    setLoadingMore(true);
    try {
      const res = await api.wikiPages({ limit: PAGE_LIMIT, offset: pageOffset, kb_id: kbId, brief: true });
      setPages((prev) => _mergePageBatch(prev, res.pages));
      const newOffset = pageOffset + res.pages.length;
      setPageOffset(newOffset);
      setHasMorePages(res.pages.length >= PAGE_LIMIT);
      setSourceTitles((prev) => ({ ...prev, ...(res.source_titles || {}) }));
      setSourceFiles((prev) => ({ ...prev, ...(res.source_files || {}) }));
    } catch {
      setMessage("加载更多页面失败");
    } finally {
      setLoadingMore(false);
    }
  }, [kbId, hasMorePages, loadingMore, pageOffset, _mergePageBatch]);

  useEffect(() => {
    refreshKbs();
  }, [refreshKbs]);

  // 新用户首次进入 Wiki 时，若没有任何知识库，自动初始化 default
  useEffect(() => {
    if (hasAutoInitRef.current) return;
    if (kbs.length === 0) {
      hasAutoInitRef.current = true;
      api.wikiInit("default")
        .then(() => refreshKbs({ targetKbId: "default" }))
        .catch((err) => {
          setMessage(`初始化默认知识库失败：${err instanceof Error ? err.message : String(err)}`);
        });
    }
  }, [kbs, refreshKbs]);

  useEffect(() => {
    let cancelled = false;
    setInitializedKbId(null);
    // 已存在的旧 KB 可能没有 Vault 根文件；先走幂等初始化，再加载页面。
    api.wikiInit(kbId)
      .then(async () => {
        if (cancelled) return;
        await refreshPages();
        if (!cancelled) setInitializedKbId(kbId);
      })
      .catch((err) => {
        if (!cancelled) {
          setMessage(`初始化知识库失败：${err instanceof Error ? err.message : String(err)}`);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [kbId, refreshPages]);

  // 左侧滚动到底时自动加载下一页
  useEffect(() => {
    if (!hasMorePages || loadingMore || loading) return;
    const el = loadMoreRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          loadMorePages();
        }
      },
      { root: null, rootMargin: "200px", threshold: 0 },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasMorePages, loadingMore, loading, loadMorePages]);

  // 每个上传任务的显示百分比平滑动画：后端阶段是离散的，前端以 1% 粒度补间；
  // 若真实值长时间不变（如 LLM analyze），自动缓慢涓流前进，避免卡死。
  useEffect(() => {
    const timer = setInterval(() => {
      setUploadJobs((prev) => {
        let changed = false;
        const next = prev.map((job) => {
          if (job.status === "done" || job.status === "error" || job.status === "cancelled") {
            return job;
          }
          const target = job.percent;
          // "等待上传" 阶段 percent 为 0，不应提前往前走，保持 0% 直到真正开始上传。
          if (job.label === "等待上传") {
            if (job.displayPercent !== 0) {
              changed = true;
              return { ...job, displayPercent: 0 };
            }
            return job;
          }
          const isStall = target <= job.displayPercent;
          // 后端 analyze 阶段会平滑推进；若仍长时间停滞，前端也继续往前"涓流"一段。
          // analyze 阶段上限固定为 99%，与分析阶段真实上限对齐；
          // 其他阶段上限放宽到 target + 25（不超过 99）。
          const trickleCeiling = job.stage === "analyze" ? 99 : Math.min(target + 25, 99);
          const step = isStall ? 0.5 : Math.max(1, Math.ceil((target - job.displayPercent) / 20));
          const ceiling = isStall ? trickleCeiling : target;
          const nextDisplay = Math.min(job.displayPercent + step, ceiling);
          // analyze 阶段耗时很长，用真实阶段文案循环显示 label，让用户感觉后台一直在工作。
          let nextLabel = job.label;
          if (job.stage === "analyze" && job.analyzeStartedAt) {
            const elapsed = Date.now() - job.analyzeStartedAt;
            const idx = Math.floor(elapsed / ANALYZE_VIRTUAL_LABEL_INTERVAL_MS) % ANALYZE_VIRTUAL_LABELS.length;
            nextLabel = ANALYZE_VIRTUAL_LABELS[idx];
          }
          if (nextDisplay !== job.displayPercent || nextLabel !== job.label) {
            changed = true;
            return { ...job, displayPercent: nextDisplay, label: nextLabel };
          }
          return job;
        });
        return changed ? next : prev;
      });
    }, 80);
    return () => clearInterval(timer);
  }, []);

  // 把 WebSocket 推送的进度匹配到对应任务。
  // wikiProgress 现在是按 source_id 维护的 map，因此多文件同时上传时
  // 每个文件的进度互不覆盖，中间阶段也不会被其他文件挤掉。
  useEffect(() => {
    if (!wikiProgress) return;
    setUploadJobs((prev) => {
      let changed = false;
      const next = prev.map((job) => {
        if (!job.sourceId) return job;
        const p = wikiProgress[job.sourceId];
        if (!p) return job;
        const patched = { ...job };
        if (p.stage === "done") {
          if (p.error) {
            patched.status = "error";
            patched.error = p.error;
            patched.label = "编译失败";
          } else {
            patched.status = "done";
            patched.label = "编译完成";
            patched.percent = 100;
          }
          patched.stage = "done";
        } else {
          patched.status = "ingesting";
          // 首次进入 analyze 阶段时记录开始时间，用于虚拟标签循环；
          // 离开 analyze 阶段时清除，恢复真实标签。
          if (p.stage === "analyze" && job.stage !== "analyze") {
            patched.analyzeStartedAt = Date.now();
          } else if (p.stage !== "analyze") {
            patched.analyzeStartedAt = undefined;
          }
          patched.stage = p.stage;
          patched.label = p.label || p.stage;
          patched.percent = p.percent;
        }
        changed = true;
        return patched;
      });
      return changed ? next : prev;
    });
  }, [wikiProgress]);

  // 页面列表变化后，过滤掉已不存在的选中项
  useEffect(() => {
    setSelectedIds((prev) => {
      const next = new Set(
        [...prev].filter((id) => pages.some((p) => p.id === id)),
      );
      return next.size === prev.size ? prev : next;
    });
  }, [pages]);

  // 选中页面时，自动展开其所在目录
  useEffect(() => {
    if (!selectedId) return;
    const page = pages.find((p) => p.id === selectedId);
    if (!page?.file_path) return;
    const paths = ancestorPaths(page.file_path);
    if (paths.length === 0) return;
    setExpandedPaths((prev) => {
      const next = new Set(prev);
      for (const p of paths) next.add(p);
      return next;
    });
  }, [selectedId, pages]);

  const handleKbChange = (id: string) => {
    onKbChange(id);
    setSelectedId(null);
    setSelectedDocumentName(null);
    setVaultDocument(null);
    setSelectedIds(new Set());
    setPageDetails({});
    setRelationPages({});
    setExpandedPaths(new Set(["wiki", "wiki/sources"]));
  };

  const handleCreateKb = async () => {
    const raw = window.prompt("新建知识库 ID（英文/数字/下划线）：", "");
    if (!raw) return;
    const id = raw.trim().replace(/\s+/g, "_");
    if (!id) return;
    try {
      await api.wikiCreateKB({ kb_id: id, name: id });
      setMessage(`已创建知识库：${id}`);
      await refreshKbs({ targetKbId: id });
      setSelectedId(null);
      setSelectedDocumentName(null);
      setVaultDocument(null);
      setSelectedIds(new Set());
    } catch (err) {
      setMessage(`创建失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleDeleteKb = async () => {
    if (kbId === "default" || kbId === "tutorial") {
      setMessage("内置知识库不可删除");
      return;
    }
    if (!window.confirm(
      `确定删除知识库「${kbId}」吗？其中的全部页面、原始素材和专属 Wiki 问答历史都会永久删除，此操作不可恢复。`,
    )) return;
    try {
      await api.wikiDeleteKB(kbId);
      setMessage("已删除知识库");
      await refreshKbs({ targetKbId: "default" });
      setSelectedId(null);
      setSelectedDocumentName(null);
      setVaultDocument(null);
      setSelectedIds(new Set());
    } catch (err) {
      setMessage(`删除失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const activeSourceId = useMemo(
    () => uploadJobs.find((j) => j.status === "ingesting" && j.sourceId)?.sourceId || null,
    [uploadJobs],
  );

  const updateJob = (id: string, patch: Partial<UploadJob>) => {
    setUploadJobs((prev) => {
      const idx = prev.findIndex((j) => j.id === id);
      if (idx === -1) return prev;
      const next = [...prev];
      next[idx] = { ...next[idx], ...patch };
      return next;
    });
  };

  const processFile = async (file: File, jobId: string) => {
    updateJob(jobId, { status: "uploading", stage: "upload", label: "上传中", percent: 5, displayPercent: 0 });
    try {
      const res = await api.wikiUpload(file, kbId);
      updateJob(jobId, {
        sourceId: res.source_id,
        status: "uploading",
        stage: "upload",
        label: "上传完成",
        percent: 10,
      });

      // 图片/视频：后端可能已自动理解并 ingest
      if (res.source_type === "image" || res.source_type === "video") {
        const mediaIssues = res.issues || [];
        const mediaHasPages = (res.pages || []).length > 0;
        if (res.ingested && mediaHasPages && mediaIssues.length === 0) {
          const newIds = new Set(res.pages!.map((p) => p.id));
          setHighlightedIds((prev) => new Set([...prev, ...newIds]));
          setViewMode("timeline");
          updateJob(jobId, { status: "done", stage: "done", label: "编译完成", percent: 100, displayPercent: 100 });
          setMessage(`已上传并编译：${res.title}`);
          await refreshPages();
        } else if (res.source_type === "video" && res.needs_confirmation) {
          setPendingMedia({
            source_id: res.source_id,
            title: res.title,
            source_type: res.source_type,
            needs_confirmation: true,
          });
          updateJob(jobId, { status: "done", stage: "done", label: "等待确认", percent: 100, displayPercent: 100 });
          setMessage(`视频「${res.title}」已上传。由于需要上传到外部云端分析，请在下方确认风险后由 AI 处理。`);
        } else if (mediaIssues.length > 0 || !mediaHasPages) {
          const errorText = mediaIssues.join("；") || "自动理解后没有生成任何页面";
          updateJob(jobId, {
            status: "error",
            stage: "done",
            label: "编译失败",
            error: errorText,
            aiPrompt: `我上传「${file.name}」到 Wiki（图片/视频）时自动理解失败，错误信息：${errorText}。请帮我分析原因并重新处理这个文件（source_id: ${res.source_id}）。`,
            percent: 0,
          });
          setMessage(`上传失败：${errorText}`);
        } else {
          updateJob(jobId, { status: "done", stage: "done", label: "等待处理", percent: 100, displayPercent: 100 });
          setMessage(`已上传：${res.title}，等待进一步处理。`);
        }
        return;
      }

      // 文本/文档：调用 ingest
      updateJob(jobId, { status: "ingesting", stage: "load", label: "读取文档", percent: 10 });
      const ingest = await api.wikiIngest(res.source_id, kbId, sessionId);
      const issues = ingest.issues || [];
      const hasPages = (ingest.pages || []).length > 0;
      if (issues.length > 0 || !hasPages) {
        const errorText = issues.join("；") || "编译后没有生成任何页面";
        updateJob(jobId, {
          status: "error",
          stage: "done",
          label: "编译失败",
          error: errorText,
          aiPrompt: `我上传「${file.name}」到 Wiki 时编译失败，错误信息：${errorText}。请帮我分析原因并重新处理这个文件（source_id: ${res.source_id}）。`,
          percent: 0,
        });
        setMessage(`上传失败：${errorText}`);
        return;
      }
      const newIds = new Set(ingest.pages.map((p) => p.id));
      setHighlightedIds((prev) => new Set([...prev, ...newIds]));
      setViewMode("timeline");
      updateJob(jobId, { status: "done", stage: "done", label: "编译完成", percent: 100, displayPercent: 100 });
      setMessage(`已上传并编译：${res.title}`);
      await refreshPages();
    } catch (err) {
      if (err instanceof ApiError && err.body?.error_code === "MISSING_DEPENDENCY") {
        const dep = err.body.dependency || "相关依赖";
        const cmd = err.body.install_command || 'uv pip install -e ".[wiki]"';
        updateJob(jobId, {
          status: "error",
          stage: "done",
          label: "缺少依赖",
          error: `缺少 ${dep}`,
          aiPrompt: `我上传「${file.name}」到 Wiki 时提示缺少 ${dep} 依赖（安装命令：${cmd}），请帮我安装并重新处理这个文件。`,
          percent: 0,
        });
        setMessage(`上传失败：缺少 ${dep}，Wiki 无法解析 ${file.name}。`);
      } else if (err instanceof Error && err.name === "AbortError") {
        updateJob(jobId, { status: "cancelled", stage: "done", label: "已取消", percent: 0 });
      } else {
        const error = err instanceof Error ? err.message : String(err);
        updateJob(jobId, {
          status: "error",
          stage: "done",
          label: "处理失败",
          error,
          aiPrompt: `我上传「${file.name}」到 Wiki 时编译失败，错误信息：${error}。请帮我分析原因并重新处理这个文件。`,
          percent: 0,
        });
        setMessage(`上传失败：${error}`);
      }
    }
  };

  const handleFiles = async (files: FileList) => {
    setHighlightedIds(new Set());
    setPendingMedia(null);
    stopBatchRef.current = false;

    const newJobs: UploadJob[] = Array.from(files).map((file, i) => ({
      id: `job-${Date.now()}-${i}`,
      file,
      sourceId: null,
      title: file.name,
      stage: "upload",
      label: "等待上传",
      percent: 0,
      displayPercent: 0,
      status: "uploading" as UploadJobStatus,
    }));

    setUploadJobs((prev) => [...prev, ...newJobs]);

    for (const job of newJobs) {
      if (stopBatchRef.current) {
        updateJob(job.id, { status: "cancelled", stage: "done", label: "已取消", percent: 0 });
        continue;
      }
      await processFile(job.file, job.id);
    }
  };

  const handleCancelIngest = async (sourceId: string) => {
    try {
      await api.wikiCancelIngest(sourceId, kbId);
      setMessage("已取消编译");
    } catch (err) {
      setMessage(`取消失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleCancelAll = async () => {
    stopBatchRef.current = true;
    if (activeSourceId) {
      await handleCancelIngest(activeSourceId);
    }
  };

  const handleClearDoneJobs = () => {
    setUploadJobs((prev) => prev.filter((j) => j.status !== "done" && j.status !== "error" && j.status !== "cancelled"));
  };

  const askAI = (prompt: string, onSent?: () => void) => {
    chatProps.onSend(prompt, []);
    onSent?.();
    setMessage("已把问题发送给 AI，请在左侧对话中查看。");
  };

  const handleConfirmMediaUpload = () => {
    if (!pendingMedia) return;
    const prompt =
      pendingMedia.source_type === "video"
        ? `请理解 Wiki 知识库里的视频 source "${pendingMedia.source_id}"，发布可搜索的 Source 页面，然后调用 wiki_plan_ingest 完成深度整理。该视频需要上传到已配置的外部媒体分析服务，我已了解数据外传风险并同意上传。`
        : `请理解 Wiki 知识库里的图片 source "${pendingMedia.source_id}"，发布可搜索的 Source 页面，然后调用 wiki_plan_ingest 完成深度整理。`;
    askAI(prompt, () => setPendingMedia(null));
  };

  useEffect(() => {
    if (highlightedIds.size === 0) return;
    const timer = setTimeout(() => setHighlightedIds(new Set()), 4000);
    return () => clearTimeout(timer);
  }, [highlightedIds]);

  const handleDelete = async (page: WikiPage) => {
    if (!window.confirm(`确定删除页面「${page.title}」？`)) return;
    try {
      await api.wikiDeletePage(page.id, kbId);
      setMessage("已删除页面");
      if (selectedId === page.id) {
        setSelectedId(null);
      }
      refreshPages();
    } catch (err) {
      setMessage(`删除失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleToggleSelect = (page: WikiPage) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(page.id)) next.delete(page.id);
      else next.add(page.id);
      return next;
    });
  };

  const loadingDetailsRef = useRef<Set<string>>(new Set());

  const loadPageDetail = useCallback(
    async (pageId: string) => {
      if (loadingDetailsRef.current.has(pageId)) return;
      loadingDetailsRef.current.add(pageId);
      try {
        const res = await api.wikiPage(pageId, kbId);
        setPageDetails((prev) => ({ ...prev, [pageId]: res.page }));
        setRelationPages((prev) => ({ ...prev, [pageId]: res.relation_pages ?? [] }));
      } catch (err) {
        setMessage(`加载页面详情失败：${err instanceof Error ? err.message : String(err)}`);
      } finally {
        loadingDetailsRef.current.delete(pageId);
      }
    },
    [kbId],
  );

  const loadVaultDocument = useCallback(async (name: "Home.md" | "index.md") => {
    setSelectedId(null);
    setSelectedDocumentName(name);
    setVaultDocument(null);
    try {
      const res = await api.wikiVaultDocument(name, kbId);
      setVaultDocument(res.document);
    } catch (err) {
      setSelectedDocumentName(null);
      setMessage(`加载 ${name} 失败：${err instanceof Error ? err.message : String(err)}`);
    }
  }, [kbId]);

  /**
   * 点击正文 [[Wiki 双链]]：先按标题/别名在已加载页面里精确匹配，
   * 找不到再走搜索接口兜底（对齐桌面端 resolveAndOpenWikiPage）。
   */
  const handleWikiLink = useCallback(
    async (title: string) => {
      const openPage = (pageId: string) => {
        setSelectedDocumentName(null);
        setVaultDocument(null);
        setSelectedId(pageId);
      };
      const local = findPageByTitle(pages, title);
      if (local) {
        openPage(local.id);
        return;
      }
      try {
        const res = await api.wikiSearch(title, kbId, 8);
        const target = findPageByTitle(res.pages, title);
        if (!target) {
          setMessage(`未找到 Wiki 页面：${title}`);
          return;
        }
        setPages((prev) => (prev.some((p) => p.id === target.id) ? prev : [...prev, target]));
        setPageDetails((prev) => ({ ...prev, [target.id]: target }));
        setSourceTitles((prev) => ({ ...prev, ...(res.source_titles || {}) }));
        setSourceFiles((prev) => ({ ...prev, ...(res.source_files || {}) }));
        openPage(target.id);
      } catch (err) {
        setMessage(`打开 Wiki 页面失败：${err instanceof Error ? err.message : String(err)}`);
      }
    },
    [pages, kbId],
  );

  useEffect(() => {
    if (initializedKbId === kbId) {
      void loadVaultDocument("Home.md");
    }
  }, [initializedKbId, kbId, loadVaultDocument]);

  useEffect(() => {
    if (!selectedId) return;
    const brief = pages.find((p) => p.id === selectedId);
    if (brief && !brief.content) {
      loadPageDetail(selectedId);
    }
  }, [selectedId, pages, loadPageDetail]);

  const selectedPage = useMemo(
    () => (selectedId ? pageDetails[selectedId] || pages.find((p) => p.id === selectedId) || null : null),
    [pages, pageDetails, selectedId],
  );

  const treeRoot = useMemo(() => buildFileTree(pages), [pages]);

  const handleTogglePath = (path: string) => {
    setExpandedPaths((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const handleSelectAll = () => setSelectedIds(new Set(pages.map((p) => p.id)));
  const handleDeselectAll = () => setSelectedIds(new Set());

  const renderLeftView = () => {
    const common = {
      pages,
      selectedId,
      selectedIds,
      highlightedIds,
      onSelectPage: (p: WikiPage) => {
        setSelectedDocumentName(null);
        setVaultDocument(null);
        setSelectedId(p.id);
      },
      onToggleSelect: handleToggleSelect,
      onDelete: handleDelete,
    };
    switch (viewMode) {
      case "tree":
        return (
          <WikiFileTree
            nodes={treeRoot.children}
            expandedPaths={expandedPaths}
            onTogglePath={handleTogglePath}
            selectedDocumentName={selectedDocumentName}
            onSelectDocument={loadVaultDocument}
            {...common}
          />
        );
      case "type":
        return <WikiTypeView {...common} />;
      case "graph":
        return (
          <WikiGraphView
            kbId={kbId}
            pages={pages}
            selectedId={selectedId}
            onSelectPage={(p) => setSelectedId(p.id)}
          />
        );
      case "timeline":
      default:
        return <WikiTimelineView {...common} />;
    }
  };

  const handleBulkDelete = async () => {
    const ids = [...selectedIds];
    if (ids.length === 0) {
      setMessage("没有选中的页面可删除");
      return;
    }
    if (!window.confirm(`确定删除选中的 ${ids.length} 个页面？`)) return;
    try {
      const res = await api.wikiDeletePages(ids, kbId);
      setMessage(`已删除 ${res.deleted.length} 个页面`);
      if (selectedId && ids.includes(selectedId)) setSelectedId(null);
      setSelectedIds(new Set());
      refreshPages();
    } catch (err) {
      setMessage(`批量删除失败：${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const viewTabs: { key: WikiViewMode; label: string }[] = [
    { key: "timeline", label: "时间" },
    { key: "tree", label: "文件夹" },
    { key: "type", label: "类型" },
    { key: "graph", label: "图谱" },
  ];

  return (
    <div className="wiki-hub wiki-hub-layout">
      <div className="wiki-hub__header">
        <h2 className="wiki-hub__title">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H19a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H6.5a1 1 0 0 1 0-5H20" />
          </svg>
          Wiki 知识库
        </h2>
        <div className="wiki-hub__toolbar">
          <select
            className="wiki-kb-select"
            value={kbId}
            onChange={(e) => handleKbChange(e.target.value)}
            title="选择知识库"
          >
            {kbs.map((kb) => (
              <option key={kb.id} value={kb.id}>
                {kb.name}
              </option>
            ))}
            {kbs.length === 0 && <option value="default">默认知识库</option>}
          </select>
          <button className="wiki-card__btn" onClick={handleCreateKb} type="button">
            新建知识库
          </button>
          <button
            className="wiki-card__btn"
            onClick={handleDeleteKb}
            type="button"
            disabled={kbId === "default" || kbId === "tutorial"}
          >
            删除知识库
          </button>
          <div className="wiki-hub__bulk">
            <button className="wiki-card__btn" onClick={handleSelectAll} type="button">
              全选
            </button>
            <button className="wiki-card__btn" onClick={handleDeselectAll} type="button" disabled={selectedIds.size === 0}>
              取消全选
            </button>
            <button
              className="wiki-card__btn wiki-card__btn--danger"
              onClick={handleBulkDelete}
              type="button"
              disabled={selectedIds.size === 0}
            >
              删除选中
            </button>
          </div>

          <button
            className="wiki-card__btn"
            onClick={() => fileRef.current?.click()}
            type="button"
            disabled={!sessionId}
          >
            上传文件
          </button>
          <button
            className={`wiki-card__btn ${browserOpen ? "wiki-card__btn--primary" : ""}`}
            onClick={toggleBrowser}
            type="button"
            title={browserOpen ? "收起知识库面板" : "展开知识库面板"}
          >
            知识库面板
          </button>
          <input
            ref={fileRef}
            type="file"
            multiple
            style={{ display: "none" }}
            accept=".txt,.md,.pdf,.docx,.xlsx,.pptx,.jpg,.jpeg,.png,.webp,.bmp,.gif,.mp4,.mov,.webm,.avi,.mkv"
            onChange={(e) => {
              const files = e.target.files;
              if (files && files.length > 0) handleFiles(files);
              e.currentTarget.value = "";
            }}
          />
        </div>
      </div>

      {message && (
        <div className="wiki-hub__message">
          <span>{message}</span>
          {pendingMedia && pendingMedia.needs_confirmation && (
            <button
              onClick={handleConfirmMediaUpload}
              type="button"
              className="wiki-hub__message-action"
            >
              <WikiIcon name="sparkles" size={13} />
              让 AI 确认并分析
            </button>
          )}
          <button onClick={() => setMessage("")} type="button">×</button>
        </div>
      )}

      {uploadJobs.length > 0 && (
        <div className={`wiki-upload-jobs ${uploadJobsExpanded ? "" : "wiki-upload-jobs--collapsed"}`}>
          <div className="wiki-upload-jobs__header">
            <button
              type="button"
              className="wiki-upload-jobs__toggle"
              onClick={() => setUploadJobsExpanded((v) => !v)}
              title={uploadJobsExpanded ? "收起" : "展开"}
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                style={{ transform: uploadJobsExpanded ? "rotate(180deg)" : "rotate(0deg)", transition: "transform .15s" }}
              >
                <polyline points="6 9 12 15 18 9" />
              </svg>
              <span>
                上传任务
                <span className="wiki-upload-jobs__count">
                  {activeCount > 0
                    ? ` (${activeCount} 进行中 / ${uploadJobs.length})`
                    : ` (${uploadJobs.length})`}
                </span>
              </span>
            </button>
            <div className="wiki-upload-jobs__actions">
              {activeCount > 0 && (
                <button
                  type="button"
                  className="wiki-upload-jobs__action"
                  onClick={handleCancelAll}
                >
                  全部取消
                </button>
              )}
              {hasDoneJobs && (
                <button
                  type="button"
                  className="wiki-upload-jobs__action"
                  onClick={handleClearDoneJobs}
                >
                  完成
                </button>
              )}
            </div>
          </div>
          {uploadJobsExpanded && (
            <div className="wiki-upload-jobs__list">
              {uploadJobs.map((job) => (
                <div
                  key={job.id}
                  className={`wiki-upload-job ${
                    job.status === "error"
                      ? "wiki-upload-job--error"
                      : job.status === "done"
                      ? "wiki-upload-job--done"
                      : job.status === "cancelled"
                      ? "wiki-upload-job--cancelled"
                      : ""
                  }`}
                >
                  <div className="wiki-upload-job__header">
                    {(job.status === "uploading" || job.status === "ingesting") && (
                      <span className="wiki-upload-job__spinner" />
                    )}
                    <span className="wiki-upload-job__title" title={job.title}>
                      {job.title}
                    </span>
                    <span className="wiki-upload-job__label">{job.label}</span>
                    <span className="wiki-upload-job__percent">{Math.round(job.displayPercent)}%</span>
                    {job.status === "ingesting" && job.sourceId && (
                      <button
                        type="button"
                        className="wiki-upload-job__cancel"
                        onClick={() => handleCancelIngest(job.sourceId!)}
                      >
                        取消
                      </button>
                    )}
                  </div>
                  <div className="wiki-upload-job__bar">
                    <div
                      className="wiki-upload-job__fill"
                      style={{ width: `${job.displayPercent}%` }}
                    />
                  </div>
                  {job.error && (
                    <div className="wiki-upload-job__error">
                      <span>{job.error}</span>
                      {job.aiPrompt && (
                        <button
                          type="button"
                          className="wiki-upload-job__action"
                          onClick={() => askAI(job.aiPrompt!)}
                        >
                          <WikiIcon name="sparkles" size={12} />
                          让 AI 处理
                        </button>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {loading && pages.length === 0 ? (
        <div className="wiki-hub__empty">加载中…</div>
      ) : (
        <ResizablePanels
          storageKey="wiki-hub-layout"
          className="wiki-hub-layout__body"
        >
          {/* 对话为主区域（flexible，不传 defaultWidth），知识库目录+详情收进右侧扩展面板 */}
          <ResizablePanels.Panel id="chat" className="wiki-hub__chat">
            {sessionId ? (
              <ChatPanel {...chatProps} />
            ) : (
              <div className="wiki-hub__empty">正在连接 Wiki Agent…</div>
            )}
          </ResizablePanels.Panel>

          {browserOpen && (
          <ResizablePanels.Panel
            id="browser"
            defaultWidth={620}
            minWidth={420}
            maxWidth={1100}
            className={`wiki-browser ${viewMode === "graph" ? "wiki-browser--graph" : ""}`}
          >
          <div className="wiki-browser__catalog wiki-tree">
            <div className="wiki-view-switcher">
              {viewTabs.map((tab) => (
                <button
                  key={tab.key}
                  className={`wiki-view-switcher__tab ${viewMode === tab.key ? "wiki-view-switcher__tab--active" : ""}`}
                  onClick={() => setViewMode(tab.key)}
                  type="button"
                >
                  {tab.label}
                </button>
              ))}
            </div>
            {pages.length === 0 && viewMode !== "tree" ? (
              <div className="wiki-tree__empty wiki-tree__empty--guide">
                {/* 空知识库引导：纸飞机 + 虚线弧线指向右上「上传」入口（对齐桌面端） */}
                <div className="wiki-tree__empty-art" aria-hidden="true">
                  <svg className="wiki-tree__empty-arc" viewBox="0 0 120 90" fill="none">
                    <path d="M14 84 C 30 40, 66 26, 104 16" stroke="currentColor" strokeWidth="1.5" strokeDasharray="5 5" strokeLinecap="round" />
                  </svg>
                  <svg className="wiki-tree__empty-plane" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M22 2 11 13" />
                    <path d="M22 2 15 22l-4-9-9-4 20-7z" />
                  </svg>
                </div>
                <p className="wiki-tree__empty-text">知识库还没有内容</p>
                <p className="wiki-tree__empty-hint">点击右上角「上传」，或直接拖拽文件到左侧问答栏</p>
              </div>
            ) : (
              renderLeftView()
            )}
            {(hasMorePages || loadingMore) && pages.length > 0 && (
              <div
                ref={loadMoreRef}
                className="wiki-tree__load-more"
                style={{ padding: "12px", textAlign: "center", color: "var(--text-3)", fontSize: "12px" }}
              >
                {loadingMore ? "加载中…" : "滚动加载更多"}
              </div>
            )}
          </div>

          <div className="wiki-browser__detail wiki-panel">
            {selectedDocumentName ? (
              vaultDocument ? (
                <div className="wiki-page-view wiki-page-view--inline">
                  <div className="wiki-page-view__header">
                    <div className="wiki-page-view__badges">
                      <span className="wiki-card__type wiki-card__type--muted">文件</span>
                    </div>
                    <h2 className="wiki-page-view__title">{vaultDocumentLabel(vaultDocument.name)}</h2>
                  </div>
                  <div className="wiki-page-view__content">
                    {(() => {
                      // Home.md 的「推荐问题」小节渲染成可点击的提问按钮（对齐桌面端），
                      // 其余文档按普通 markdown 渲染；正文 [[双链]] 均可点击跳转。
                      const sections =
                        vaultDocument.name === "Home.md" ? splitHomeQuestions(vaultDocument.content) : null;
                      if (!sections) {
                        return <MarkdownContent content={vaultDocument.content} fold onWikiLink={handleWikiLink} />;
                      }
                      return (
                        <>
                          {sections.before.trim() && (
                            <MarkdownContent content={sections.before} fold onWikiLink={handleWikiLink} />
                          )}
                          <div className="wiki-ask-chips">
                            {sections.questions.map((question) => (
                              <button
                                key={question}
                                className="wiki-ask-chip"
                                type="button"
                                onClick={() => askAI(question)}
                              >
                                {question}
                              </button>
                            ))}
                          </div>
                          {sections.after.trim() && (
                            <MarkdownContent content={sections.after} fold onWikiLink={handleWikiLink} />
                          )}
                        </>
                      );
                    })()}
                  </div>
                </div>
              ) : (
                <div className="wiki-panel__empty"><p>加载文档中…</p></div>
              )
            ) : selectedPage ? (
              <WikiPageView
                page={selectedPage}
                sourceTitles={sourceTitles}
                sourceFiles={sourceFiles}
                kbId={kbId}
                inline
                onNavigate={(pageId) => setSelectedId(pageId)}
                onWikiLink={handleWikiLink}
                pages={pages}
                relationPages={relationPages[selectedPage.id] ?? []}
              />
            ) : (
              <div className="wiki-panel__empty">
                <p>选择左侧页面查看详情</p>
              </div>
            )}
          </div>
          </ResizablePanels.Panel>
          )}
        </ResizablePanels>
      )}

    </div>
  );
}
