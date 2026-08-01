import { describe, expect, it } from "vitest";
import { compactTaskSummary, compactTaskSummaryItems } from "./taskSummary";

describe("compactTaskSummary", () => {
  it("keeps board summaries short and skips markdown tables", () => {
    const summary = compactTaskSummary(`
## 当前成果
已完成贪吃蛇安全验证方案，写入 \`snake-security-plan.md\`，覆盖五个维度：

| 维度 | 风险等级 | 关键发现 |
|------|----------|----------|
| **权限** | 低 | 未调用任何浏览器权限 API |

建议：按当前方案进入验证。
`);

    expect(summary).toContain("已完成贪吃蛇安全验证方案");
    expect(summary).toContain("建议：按当前方案进入验证");
    expect(summary).not.toContain("|");
    expect(summary).not.toContain("------");
  });

  it("removes flattened markdown table fragments from node conclusions", () => {
    const summary = compactTaskSummary(
      "## 当前成果 贪吃蛇安全验证已全部完成，结论：可验收。 ### 验证执行清单 | 验证项 | 方法 | 结果 | |--------|------|------| | 静态代码审计 | grep 扫描危险 API | 通过 |",
    );

    expect(summary).toBe("贪吃蛇安全验证已全部完成，结论：可验收。");
    expect(summary).not.toContain("###");
    expect(summary).not.toContain("|");
    expect(summary).not.toContain("验证执行清单");
  });

  it("returns readable bullet items for long board conclusions", () => {
    const items = compactTaskSummaryItems(
      "结论：可以验收。关键依据：核心路径、回归用例和安全检查均已通过；风险：测试钩子建议后续移除；建议：先验收，再排期清理中风险项。",
    );

    expect(items).toEqual([
      "结论：可以验收。关键依据：核心路径、回归用例和安全检查均已通过",
      "风险：测试钩子建议后续移除",
      "建议：先验收，再排期清理中风险项。",
    ]);
  });
});
