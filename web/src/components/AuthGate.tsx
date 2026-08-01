import { useEffect, useState, type FormEvent, type ReactNode } from "react";

type AuthMode = "local" | "remote" | "dev";
type AuthUser = { userId: string; phoneNumber?: string; displayName?: string; providerId?: string };

type AuthConfig = {
  ok: boolean;
  mode: AuthMode;
  configured: boolean;
  providerId: string;
};

async function readJson(response: Response): Promise<Record<string, unknown>> {
  try {
    return await response.json() as Record<string, unknown>;
  } catch {
    return {};
  }
}

function errorText(payload: Record<string, unknown>, fallback: string): string {
  return typeof payload.error === "string" && payload.error.trim() ? payload.error : fallback;
}

export default function AuthGate({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [phoneNumber, setPhoneNumber] = useState("");
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const configResponse = await fetch("/api/auth/config", { credentials: "same-origin" });
        const configPayload = await readJson(configResponse) as unknown as AuthConfig;
        if (!configResponse.ok || configPayload.ok !== true) throw new Error("无法读取认证配置");
        if (cancelled) return;
        setConfig(configPayload);
        if (configPayload.mode !== "remote") {
          setUser({ userId: configPayload.mode === "dev" ? "dev" : "local" });
          return;
        }
        const sessionResponse = await fetch("/api/auth/session", { credentials: "same-origin" });
        if (!sessionResponse.ok) return;
        const sessionPayload = await readJson(sessionResponse);
        const rawUser = sessionPayload.user;
        if (rawUser && typeof rawUser === "object" && !Array.isArray(rawUser)) {
          const nextUser = rawUser as AuthUser;
          if (typeof nextUser.userId === "string" && nextUser.userId) setUser(nextUser);
        }
      } catch (error) {
        if (!cancelled) setMessage((error as Error).message || "认证初始化失败");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const sendCode = async () => {
    if (!phoneNumber.trim()) {
      setMessage("请输入手机号。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/auth/send-code", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ phoneNumber: phoneNumber.trim() }),
      });
      const payload = await readJson(response);
      setMessage(response.ok && payload.ok === true
        ? (typeof payload.message === "string" ? payload.message : "验证码已发送。")
        : errorText(payload, "验证码发送失败。"));
    } catch (error) {
      setMessage((error as Error).message || "验证码发送失败。");
    } finally {
      setBusy(false);
    }
  };

  const login = async (event: FormEvent) => {
    event.preventDefault();
    if (!phoneNumber.trim() || !code.trim()) {
      setMessage("请输入手机号和验证码。");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ phoneNumber: phoneNumber.trim(), code: code.trim() }),
      });
      const payload = await readJson(response);
      const rawUser = payload.user;
      if (!response.ok || payload.ok !== true || !rawUser || typeof rawUser !== "object") {
        setMessage(errorText(payload, "登录失败。"));
        return;
      }
      setUser(rawUser as AuthUser);
    } catch (error) {
      setMessage((error as Error).message || "登录失败。");
    } finally {
      setBusy(false);
    }
  };

  const logout = async () => {
    setBusy(true);
    try {
      const response = await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "same-origin",
      });
      const payload = await readJson(response);
      if (!response.ok || payload.ok !== true) {
        setMessage(errorText(payload, "退出登录失败。"));
        return;
      }
      setUser(null);
      setCode("");
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return <div className="auth-page"><div className="auth-card">正在读取认证配置…</div></div>;
  }
  if (config?.mode === "remote" && !user) {
    return (
      <main className="auth-page">
        <form className="auth-card" onSubmit={login}>
          <div className="auth-brand">Crew</div>
          <h1>登录</h1>
          <p>登录后，会话、模型配置和 Wiki 数据将按用户隔离。</p>
          {!config.configured && <div className="auth-message is-error">请先配置认证服务地址。</div>}
          <label>手机号<input value={phoneNumber} onChange={(e) => setPhoneNumber(e.target.value)} autoComplete="tel" maxLength={32} /></label>
          <label>验证码<span className="auth-code-row"><input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" maxLength={32} /><button type="button" onClick={() => void sendCode()} disabled={busy || !config.configured}>获取验证码</button></span></label>
          {message && <div className="auth-message">{message}</div>}
          <button className="auth-submit" type="submit" disabled={busy || !config.configured}>{busy ? "请稍候…" : "登录"}</button>
        </form>
      </main>
    );
  }
  return (
    <>
      {children}
      {config?.mode === "remote" && (
        <button className="auth-logout" type="button" onClick={() => void logout()} disabled={busy} title={`${config.providerId}:${user?.userId || ""}`}>退出登录</button>
      )}
    </>
  );
}
