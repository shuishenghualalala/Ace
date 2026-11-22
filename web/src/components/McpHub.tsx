import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type McpServerRow, type McpTransport } from "../api";

type TransportChoice = "stdio" | "http" | "sse";

interface FormState {
  name: string;
  transport: TransportChoice;
  command: string;
  args: string;
  url: string;
  headers: string;
  env: string;
}

const EMPTY_FORM: FormState = {
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  url: "",
  headers: "",
  env: "",
};

const TRANSPORT_LABELS: Record<McpTransport, string> = {
  stdio: "本地 stdio",
  http: "HTTP",
  sse: "SSE",
  unknown: "未知",
};

function parseLines(raw: string, separator: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of raw.split(/\r?\n/)) {
    const index = line.indexOf(separator);
    if (index <= 0) continue;
    const key = line.slice(0, index).trim();
    const value = line.slice(index + 1).trim();
    if (key) result[key] = value;
  }
  return result;
}

function formFromServer(server: McpServerRow): FormState {
  const transport = server.config.transport;
  return {
    name: server.name,
    transport: transport === "http" || transport === "sse" ? transport : "stdio",
    command: server.config.command ?? "",
    args: (server.config.args ?? []).join(", "),
    url: server.config.url ?? "",
    headers: Object.entries(server.config.headers ?? {})
      .map(([key, value]) => `${key}: ${value}`)
      .join("\n"),
    env: Object.entries(server.config.env ?? {})
      .map(([key, value]) => `${key}=${value}`)
      .join("\n"),
  };
}

function payloadFromForm(form: FormState): Record<string, unknown> {
  const payload: Record<string, unknown> = { transport: form.transport };
  if (form.transport === "stdio") {
    payload.command = form.command.trim();
    payload.args = form.args.split(",").map((item) => item.trim()).filter(Boolean);
  } else {
    payload.url = form.url.trim();
    const headers = parseLines(form.headers, ":");
    if (Object.keys(headers).length) payload.headers = headers;
  }
  const env = parseLines(form.env, "=");
  if (Object.keys(env).length) payload.env = env;
  return payload;
}

export default function McpHub() {
  const [servers, setServers] = useState<McpServerRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [editing, setEditing] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [probing, setProbing] = useState(false);
  const [probeMessage, setProbeMessage] = useState("");
  const refreshTimer = useRef<number | null>(null);
  const probeEpoch = useRef(0);
  const probeAbort = useRef<AbortController | null>(null);

  const load = async (): Promise<void> => {
    setLoading(true);
    try {
      const result = await api.mcpServers();
      setServers(result.servers ?? []);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法加载 MCP 服务");
    } finally {
      setLoading(false);
    }
  };

  const refreshSoon = (): void => {
    if (refreshTimer.current != null) window.clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      void load();
    }, 1500);
  };

  const abortProbe = (): void => {
    probeAbort.current?.abort();
    probeAbort.current = null;
  };

  useEffect(() => { void load(); }, []);
  useEffect(() => () => {
    if (refreshTimer.current != null) window.clearTimeout(refreshTimer.current);
    abortProbe();
  }, []);

  const openAdd = (): void => {
    probeEpoch.current += 1;
    abortProbe();
    setEditing("");
    setForm(EMPTY_FORM);
    setProbeMessage("");
    setProbing(false);
    setModalOpen(true);
  };

  const openEdit = (server: McpServerRow): void => {
    probeEpoch.current += 1;
    abortProbe();
    setEditing(server.name);
    setForm(formFromServer(server));
    setProbeMessage("");
    setProbing(false);
    setModalOpen(true);
  };

  const closeModal = (): void => {
    if (saving) return;
    probeEpoch.current += 1;
    abortProbe();
    setProbing(false);
    setModalOpen(false);
    setProbeMessage("");
  };

  const save = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (!form.name.trim()) {
      setError("请填写服务名称");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const payload = payloadFromForm(form);
      if (editing) {
        await api.updateMcpServer(editing, payload);
      } else {
        await api.createMcpServer({ ...payload, name: form.name.trim() });
      }
      setModalOpen(false);
      await load();
      refreshSoon();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存 MCP 服务失败");
    } finally {
      setSaving(false);
    }
  };

  const probe = async (): Promise<void> => {
    if (form.transport === "stdio" || !form.url.trim()) {
      setProbeMessage("测试连接只支持 HTTP/SSE URL");
      return;
    }
    const epoch = ++probeEpoch.current;
    abortProbe();
    const controller = new AbortController();
    probeAbort.current = controller;
    setProbing(true);
    setProbeMessage("");
    try {
      const payload = payloadFromForm(form);
      if (editing) payload.name = editing;
      const result = await api.probeMcpServer(payload, controller.signal);
      if (probeEpoch.current !== epoch) return;
      if (result.ok) {
        const preview = result.tools.slice(0, 8).join(", ");
        setProbeMessage(
          `连接成功（${result.elapsed_ms} ms），发现 ${result.tool_count} 个工具${preview ? `：${preview}` : ""}`,
        );
      } else {
        setProbeMessage(`连接失败：${result.error || "未知错误"}`);
      }
    } catch (cause) {
      if (probeEpoch.current !== epoch || controller.signal.aborted) return;
      setProbeMessage(`连接失败：${cause instanceof Error ? cause.message : "未知错误"}`);
    } finally {
      if (probeEpoch.current === epoch) {
        setProbing(false);
        if (probeAbort.current === controller) probeAbort.current = null;
      }
    }
  };

  const remove = async (server: McpServerRow): Promise<void> => {
    if (!confirm(`确认删除 MCP 服务 "${server.name}" 吗？`)) return;
    try {
      await api.deleteMcpServer(server.name);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "删除 MCP 服务失败");
    }
  };

  const reload = async (server: McpServerRow): Promise<void> => {
    try {
      await api.reloadMcpServer(server.name);
      await load();
      refreshSoon();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "重连 MCP 服务失败");
    }
  };

  return (
    <div className="hub mcp-page">
      <div className="hub__header">
        <div className="hub__header-left">
          <div className="hub__eyebrow">
            <span className="hub__pixel-dot" />
            MCP Bridge
          </div>
          <h1 className="hub__title">MCP 服务</h1>
          <p className="hub__subtitle">管理 HTTP/SSE 与本地 stdio 服务，连接状态保存在当前 Gateway。</p>
        </div>
        <div className="hub__header-right">
          <button className="hub-btn hub-btn--primary" type="button" onClick={openAdd}>添加服务</button>
        </div>
      </div>

      <div className="hub__body">
        {error && <p className="hub__empty" role="alert">{error}</p>}
        {loading ? (
          <p className="hub__empty">正在加载 MCP 服务…</p>
        ) : servers.length === 0 ? (
          <p className="hub__empty">暂无 MCP 服务，点击「添加服务」开始配置。</p>
        ) : (
          <div className="mcp-grid">
            {servers.map((server) => (
              <article key={server.name} className="mcp-card">
                <header className="mcp-card__head">
                  <strong>{server.name}</strong>
                  <span className={server.error ? "mcp-chip is-error" : server.connected ? "mcp-chip is-online" : "mcp-chip"}>
                    {server.connected ? "已连接" : server.error ? "失败" : "未连接"}
                  </span>
                </header>
                <p className="mcp-card__meta">
                  {TRANSPORT_LABELS[server.transport] ?? server.transport}
                  {server.config.url ? ` · ${server.config.url}` : server.config.command ? ` · ${server.config.command}` : ""}
                </p>
                {server.error && <p className="mcp-card__status">{server.error}</p>}
                <p className="mcp-card__tools">
                  {(server.tool_count ?? server.tools.length)
                    ? `工具（${server.tool_count ?? server.tools.length}）：${server.tools.slice(0, 8).join(", ")}`
                    : "未发现工具"}
                </p>
                <div className="mcp-card__actions">
                  <button className="hub-btn" type="button" onClick={() => openEdit(server)}>编辑</button>
                  <button className="hub-btn" type="button" onClick={() => void reload(server)}>重连</button>
                  <button className="hub-btn hub-btn--ghost" type="button" onClick={() => void remove(server)}>删除</button>
                </div>
              </article>
            ))}
          </div>
        )}
      </div>

      {modalOpen && (
        <div className="mcp-overlay" onClick={(event) => { if (event.target === event.currentTarget) closeModal(); }}>
          <form className="mcp-modal" role="dialog" aria-modal="true" onSubmit={(event) => void save(event)}>
            <header className="mcp-modal__head">
              <h2>{editing ? `编辑：${editing}` : "新增 MCP 服务"}</h2>
              <button className="hub-btn hub-btn--ghost" type="button" onClick={closeModal} aria-label="关闭">关闭</button>
            </header>
            <label className="mcp-field">
              <span>服务名称</span>
              <input value={form.name} required aria-label="服务名称" disabled={Boolean(editing)}
                onChange={(event) => setForm({ ...form, name: event.target.value })} />
            </label>
            <label className="mcp-field">
              <span>传输类型</span>
              <select value={form.transport} aria-label="传输类型"
                onChange={(event) => {
                  probeEpoch.current += 1;
                  abortProbe();
                  setForm({ ...form, transport: event.target.value as TransportChoice });
                  setProbeMessage("");
                  setProbing(false);
                }}>
                <option value="stdio">本地 stdio</option>
                <option value="http">HTTP</option>
                <option value="sse">SSE</option>
              </select>
            </label>
            {form.transport === "stdio" && (
              <p className="mcp-probe">保存后会尝试启动该命令并连接；测试连接不会执行命令。</p>
            )}
            {form.transport === "stdio" ? (
              <>
                <label className="mcp-field">
                  <span>Command</span>
                  <input value={form.command}
                    onChange={(event) => setForm({ ...form, command: event.target.value })} />
                </label>
                <label className="mcp-field">
                  <span>Args（逗号分隔）</span>
                  <input value={form.args}
                    onChange={(event) => setForm({ ...form, args: event.target.value })} />
                </label>
              </>
            ) : (
              <>
                <label className="mcp-field">
                  <span>URL</span>
                  <input value={form.url} placeholder="https://mcp.example.com/mcp" aria-label="MCP URL"
                    onChange={(event) => setForm({ ...form, url: event.target.value })} />
                </label>
                <label className="mcp-field">
                  <span>Headers（每行 Key: Value）</span>
                  <textarea rows={3} value={form.headers}
                    onChange={(event) => setForm({ ...form, headers: event.target.value })} />
                </label>
              </>
            )}
            <label className="mcp-field">
              <span>环境变量（每行 KEY=VALUE）</span>
              <textarea rows={2} value={form.env}
                onChange={(event) => setForm({ ...form, env: event.target.value })} />
            </label>
            {probeMessage && <p className="mcp-probe" role="status">{probeMessage}</p>}
            <div className="mcp-modal__actions">
              {form.transport !== "stdio" && (
                <button className="hub-btn" type="button" disabled={probing} onClick={() => void probe()}>
                  {probing ? "测试中…" : "测试连接"}
                </button>
              )}
              <button className="hub-btn hub-btn--primary" type="submit" disabled={saving}>
                {saving ? "保存中…" : "保存"}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
