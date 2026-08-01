import { describe, it, expect } from "vitest";
import { preprocessStreamMarkdown } from "./markdown-stream";

describe("preprocessStreamMarkdown", () => {
  describe("强调标记", () => {
    it("流式中末尾的 ** 应删除以避免闪烁", () => {
      expect(preprocessStreamMarkdown("hello **", true)).toBe("hello ");
    });

    it("流式中未闭合的 ** 应补闭合", () => {
      expect(preprocessStreamMarkdown("hello **world", true)).toBe("hello **world**");
    });

    it("已闭合的 ** 保持不变", () => {
      expect(preprocessStreamMarkdown("hello **world**", true)).toBe("hello **world**");
    });

    it("非流式中末尾的 ** 补闭合", () => {
      expect(preprocessStreamMarkdown("hello **", false)).toBe("hello ****");
    });

    it("单 * 同样处理", () => {
      expect(preprocessStreamMarkdown("hello *world", true)).toBe("hello *world*");
      expect(preprocessStreamMarkdown("hello *", true)).toBe("hello ");
    });

    it("双 __ 同样处理", () => {
      expect(preprocessStreamMarkdown("hello __world", true)).toBe("hello __world__");
      expect(preprocessStreamMarkdown("hello __", true)).toBe("hello ");
    });

    it("下划线在单词内部不被当作强调标记", () => {
      expect(preprocessStreamMarkdown("my_var_name", true)).toBe("my_var_name");
    });

    it("不在单词内部的下划线仍补闭合", () => {
      expect(preprocessStreamMarkdown("hello _world", true)).toBe("hello _world_");
    });
  });

  describe("代码围栏", () => {
    it("未闭合的 ``` 应补闭合", () => {
      expect(preprocessStreamMarkdown("```python\nprint(1)", true)).toBe(
        "```python\nprint(1)\n```",
      );
    });

    it("已闭合的围栏保持不变", () => {
      expect(preprocessStreamMarkdown("```python\nprint(1)\n```", true)).toBe(
        "```python\nprint(1)\n```",
      );
    });

    it("代码块内部的 ** 不触发补闭合", () => {
      expect(preprocessStreamMarkdown("```\na**b\n```", true)).toBe("```\na**b\n```");
    });

    it("inline code 内部的 ** 不触发补闭合", () => {
      expect(preprocessStreamMarkdown("hi `a**b` **x", true)).toBe("hi `a**b` **x**");
    });
  });

  describe("数学公式", () => {
    it("未闭合的单 $ 应补闭合", () => {
      expect(preprocessStreamMarkdown("$E=mc^2", true)).toBe("$E=mc^2$");
    });

    it("未闭合的 $$ 应单独成行补闭合", () => {
      expect(preprocessStreamMarkdown("$$E=mc^2", true)).toBe("$$E=mc^2\n$$");
    });

    it("已闭合的 $ 保持不变", () => {
      expect(preprocessStreamMarkdown("$E=mc^2$", true)).toBe("$E=mc^2$");
    });
  });

  describe("幂等性", () => {
    it("对已完整文本再次调用应不产生样式跳变", () => {
      const source = "# hello\n\n**bold** and `code`";
      expect(preprocessStreamMarkdown(source, true)).toBe(source);
      expect(preprocessStreamMarkdown(source, false)).toBe(source);
    });
  });

  describe("边界", () => {
    it("空字符串安全", () => {
      expect(preprocessStreamMarkdown("", true)).toBe("");
    });

    it("仅空白字符安全", () => {
      expect(preprocessStreamMarkdown("   \n  ", true)).toBe("   \n  ");
    });
  });
});
