import { describe, expect, it } from "vitest";

import { resolveWikiAgentSessionId } from "./App";

describe("resolveWikiAgentSessionId", () => {
  it("does not expose the previous knowledge base session while switching", () => {
    const previous = { kbId: "old-kb", sessionId: "wiki-old" };

    expect(resolveWikiAgentSessionId(previous, "new-kb")).toBeNull();
    expect(resolveWikiAgentSessionId(previous, "old-kb")).toBe("wiki-old");
  });
});
