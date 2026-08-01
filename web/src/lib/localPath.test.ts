import { describe, expect, it } from "vitest";
import { localPathDirectory } from "./localPath";

describe("localPathDirectory", () => {
  it("returns the containing folder for POSIX and Windows paths", () => {
    expect(localPathDirectory("/Users/demo/task/plan.md")).toBe("/Users/demo/task");
    expect(localPathDirectory("C:\\Users\\demo\\task\\plan.md")).toBe("C:\\Users\\demo\\task");
  });

  it("returns empty string when the path has no containing folder", () => {
    expect(localPathDirectory("plan.md")).toBe("");
    expect(localPathDirectory("")).toBe("");
  });
});
