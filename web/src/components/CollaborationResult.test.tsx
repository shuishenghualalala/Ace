import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import CollaborationResult from "./CollaborationResult";

describe("durable collaboration result", () => {
  it("renders complete long tables without loading images or model HTML", () => {
    const html = renderToStaticMarkup(<CollaborationResult completed text={
      "# 完整报告\n\n|项目|结论|\n|---|---|\n|A|待核实|\n\n" + "长文内容。".repeat(500)
      + "\n\n末尾的第三个追问\n\n<script>alert(1)</script>\n\n![外部图片](https://example.com/x.png)"
    } />);
    expect(html).toContain("<table>");
    expect(html).toContain("末尾的第三个追问");
    expect(html).toContain("待人工复核");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<img");
  });
  it("labels rejected output as unaccepted rather than a final report", () => {
    expect(renderToStaticMarkup(<CollaborationResult completed={false} text="invalid" />))
      .toContain("未通过验收的原始输出");
  });
});
