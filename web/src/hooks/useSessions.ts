import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Session } from "../types";

export function useSessions() {
  const [sessions, setSessions] = useState<Session[]>([]);

  const refresh = useCallback(async () => {
    try {
      setSessions(await api.sessions());
    } catch {
      /* 网关未起时静默 */
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { sessions, refresh };
}
