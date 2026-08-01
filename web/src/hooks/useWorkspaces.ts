import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Workspace } from "../types";

export function useWorkspaces() {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);

  const refresh = useCallback(async () => {
    try {
      setWorkspaces(await api.workspaces());
    } catch {
      /* 网关未起时静默 */
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return { workspaces, refresh };
}
