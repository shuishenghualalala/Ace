import { describe, expect, it } from "vitest";
import type { Task, UiMessage } from "../types";
import {
  collectSessionFileItems,
  currentTurnNodesForBoard,
  makeNodes,
  makeTurnGroups,
  nodeLogs,
  nodeTitle,
  teamDagInfo,
} from "./TaskBoard";

const task = (overrides: Partial<Task>): Task => ({
  id: "task_1",
  assignee: "kk",
  title: "实现节点",
  status: "completed",
  result: "",
  ...overrides,
});

describe("nodeTitle", () => {
  it("uses concise Team DAG semantic titles instead of repeating the user prompt", () => {
    const prompt = "请写一个贪吃蛇小游戏，要求支持开始暂停、分数展示、碰撞结束，并输出测试报告";
    const teamTask = task({
      kind: "team",
      title: `Leader 拆分任务：${prompt}`,
      progress: {
        source: "team_plan",
        plan_node_id: "leader_plan",
        workflow_lane: "lead",
      },
    });

    expect(nodeTitle(teamTask, 0)).toBe("Leader 拆分任务");
  });

  it("uses backend display titles before workflow lane labels", () => {
    expect(nodeTitle(task({
      kind: "team",
      title: "针对选定的第一个城市，挖掘至少一种隐藏小吃，并提供一句本地话点评",
      progress: {
        source: "team_kanban",
        plan_node_id: "research_city1",
        workflow_lane: "plan",
        display_title: "调研城市 1",
      },
    }), 1)).toBe("调研城市 1");
  });

  it("shortens unknown long Team titles instead of repeating full prompts", () => {
    expect(nodeTitle(task({
      kind: "team",
      title: "围绕用户给出的复杂需求，拆解关键路径、风险和交付物",
      progress: {
        source: "team_kanban",
        plan_node_id: "custom_node",
        workflow_lane: "plan",
      },
    }), 1)).toBe("围绕用户给出的复杂需求");
  });

  it("falls back to workflow lane titles when Team plan nodes have no concrete title", () => {
    expect(nodeTitle(task({
      kind: "team",
      title: "",
      progress: {
        source: "team_kanban",
        plan_node_id: "custom_node",
        workflow_lane: "verify",
      },
    }), 1)).toBe("测试验证");
  });
});

describe("blocked Team node ownership", () => {
  it("shows an unassigned blocked node as waiting for assignment", () => {
    const nodes = makeNodes([task({
      kind: "team",
      assignee: "",
      status: "blocked",
      progress: {
        source: "team_kanban",
        plan_node_id: "verify",
        runtime_blocking: { status: "blocked" },
        previous_assignee: "kk",
      },
    })]);

    expect(nodes[0].owner).toBe("待分配");
  });
});

describe("currentTurnNodesForBoard", () => {
  it("uses the latest Team turn for DAG metadata instead of mixing old fallback turns", () => {
    const nodes = makeNodes([
      task({
        id: "old_1",
        task_id: "old_1",
        title: "旧轮次任务",
        created_at: 1,
        kind: "team",
        progress: {
          source: "team_kanban",
          turn_session_id: "web_demo::turn::old",
          plan_node_id: "old_1",
          plan_strategy: "standard_role_dag",
          workflow_lane: "plan",
        },
      }),
      task({
        id: "new_1",
        task_id: "new_1",
        title: "最新轮次调研",
        created_at: 2,
        kind: "team",
        progress: {
          source: "team_kanban",
          turn_session_id: "web_demo::turn::new",
          plan_node_id: "new_1",
          plan_strategy: "standard_semantic_dag",
          workflow_lane: "plan",
        },
      }),
    ]);
    const turnGroups = makeTurnGroups(nodes);
    const currentNodes = currentTurnNodesForBoard(nodes, turnGroups);

    expect(currentNodes.map((node) => node.id)).toEqual(["new_1"]);
    expect(teamDagInfo(currentNodes, [turnGroups[turnGroups.length - 1]])?.label).toBe("Standard Semantic DAG");
  });
});

describe("nodeLogs", () => {
  it("does not use node detail handoff text as execution log fallback", () => {
    const node = {
      id: "leader_plan",
      title: "Leader 拆分任务",
      owner: "leader",
      agents: ["Leader"],
      status: "completed" as const,
      summary: "",
      summaryItems: [],
      raw: task({
        kind: "team",
        detail: "leader 将按 DAG 节点「Leader 审阅方案：贪吃蛇小游戏吧」执行。",
        progress: {
          source: "team_plan",
          plan_node_id: "leader_plan",
          workflow_lane: "lead",
        },
      }),
    };

    expect(nodeLogs(node, [])).toEqual([]);
  });

  it("filters legacy handoff execution events from node logs", () => {
    const node = {
      id: "fast_execute",
      title: "成员执行任务",
      owner: "kk",
      agents: ["kk"],
      status: "completed" as const,
      summary: "",
      summaryItems: [],
      raw: task({
        kind: "team",
        progress: {
          source: "team_plan",
          execution_events: [{
            kind: "status",
            event_title: "节点承接",
            event_text: "kk 将按 DAG 节点「快速执行：2048小游戏」执行。",
          }],
        },
      }),
    };

    expect(nodeLogs(node, [])).toEqual([]);
  });

  it("keeps structured execution events as log entries", () => {
    const node = {
      id: "verify",
      title: "测试验证",
      owner: "kk",
      agents: ["kk"],
      status: "completed" as const,
      summary: "",
      summaryItems: [],
      raw: task({
        kind: "team",
        progress: {
          source: "team_plan",
          execution_events: [{
            kind: "tool",
            title: "运行测试",
            message: "npm test -- --run snake.test.ts\n2 passed",
          }],
        },
      }),
    };

    expect(nodeLogs(node, [])).toEqual([
      expect.objectContaining({
        kind: "tool",
        title: "运行测试",
        body: "npm test -- --run snake.test.ts\n2 passed",
      }),
    ]);
  });
});

describe("collectSessionFileItems", () => {
  it("does not expose the internal agent_turn task JSON as a user artifact", () => {
    expect(collectSessionFileItems([
      task({ output_ref: "/owner/.crew/tasks/task_internal.json" }),
    ], [])).toEqual([]);
  });

  it("collects node artifact paths and message artifacts into one session file list", () => {
    const tasks: Task[] = [
      task({
        id: "node_build",
        progress: {
          plan_node_title: "实现小游戏",
          artifact_paths: ["/tmp/session/snake.html"],
        },
      }),
    ];
    const messages: UiMessage[] = [{
      id: "team_summary",
      role: "team_internal",
      text: "完成",
      agentName: "Leader",
      artifacts: [{
        title: "测试报告",
        path: "/tmp/session/report.md",
        summary: "验收说明",
        kind: "text",
      }],
    }];

    expect(collectSessionFileItems(tasks, messages)).toEqual([
      expect.objectContaining({
        title: "snake.html",
        path: "/tmp/session/snake.html",
        source: "node",
        sourceLabel: "实现小游戏",
      }),
      expect.objectContaining({
        title: "测试报告",
        path: "/tmp/session/report.md",
        source: "message",
        sourceLabel: "Leader",
        summary: "验收说明",
      }),
    ]);
  });

  it("dedupes repeated paths across node and message artifacts", () => {
    const shared = "/tmp/session/shared.txt";
    const tasks: Task[] = [
      task({ progress: { artifact_paths: [shared] } }),
    ];
    const messages: UiMessage[] = [{
      id: "msg_1",
      role: "team_internal",
      text: "",
      artifacts: [{ title: "重复文件", path: shared }],
    }];

    expect(collectSessionFileItems(tasks, messages)).toHaveLength(1);
  });
});
