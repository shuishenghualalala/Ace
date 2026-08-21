import { describe, expect, it } from "vitest";
import {
  mapHistoryItems,
  mergeHistoryWithLiveMessages,
  normalizeTurnFileChanges,
  preserveLocalProcessDetails,
  type BackendHistoryItem,
} from "./historyMap";
import { mergeTeamInternalMessage } from "./teamMessageMerge";
import type { UiMessage } from "../types";

const teamItem = (overrides: Partial<BackendHistoryItem>): BackendHistoryItem => ({
  role: "team_internal",
  content: "",
  source_session_id: "web_demo::turn::req_1::kk",
  agent_id: "kk",
  event_type: "team_stream",
  node_id: "qa_engineer_plan_1",
  display_mode: "stream",
  timestamp: 1,
  ...overrides,
});

describe("preserveLocalProcessDetails", () => {
  it("keeps thinking and tools when an immediate history reload trails persistence", () => {
    const history: UiMessage[] = [
      { id: "h1", role: "assistant", text: "最终回答" },
    ];
    const local: UiMessage[] = [
      {
        id: "live1",
        role: "assistant",
        text: "最终回答",
        thinking: "先检查项目结构。",
        toolCalls: [{
          toolCallId: "t1",
          name: "file_read",
          status: "done",
          startedAt: 0,
        }],
      },
    ];

    const merged = preserveLocalProcessDetails(history, local);

    expect(merged[0].thinking).toBe("先检查项目结构。");
    expect(merged[0].toolCalls?.[0].name).toBe("file_read");
  });

  it("prefers canonical process details once persistence has completed", () => {
    const history: UiMessage[] = [
      { id: "h1", role: "assistant", text: "最终回答", thinking: "已落库思考" },
    ];
    const local: UiMessage[] = [
      { id: "live1", role: "assistant", text: "最终回答", thinking: "本地思考" },
    ];

    expect(preserveLocalProcessDetails(history, local)[0].thinking).toBe("已落库思考");
  });
});

describe("mapHistoryItems", () => {
  it("normalizes unknown backend roles to a safe status message", () => {
    const [message] = mapHistoryItems([{
      role: "unexpected_role",
      content: "历史状态",
    }]);

    expect(message.role).toBe("status");
    expect(message.text).toBe("历史状态");
  });

  it("clamps non-finite file change counts at the history boundary", () => {
    expect(normalizeTurnFileChanges([
      { path: "/work/app.ts", added: "NaN", removed: Infinity },
    ])).toEqual([
      { path: "/work/app.ts", name: "app.ts", added: 0, removed: 0, status: "modified" },
    ]);
  });

  it("appends streaming thinking chunks without inserting blank lines between every delta", () => {
    const first: UiMessage = {
      id: "m1",
      role: "team_internal" as const,
      text: "",
      sourceSessionId: "web_demo::turn::req_1::crew::builtin",
      agentId: "crew::builtin",
      eventType: "team_stream",
      nodeId: "qa_plan",
      displayMode: "stream",
      thinking: "先",
    };
    const second = {
      ...first,
      id: "m2",
      thinking: "确认测试范围。",
    };

    const messages = mergeTeamInternalMessage([first], second, { append: true });

    expect(messages).toHaveLength(1);
    expect(messages[0].thinking).toBe("先确认测试范围。");
  });

  it("updates planning progress in a single collapsible bubble", () => {
    const first: UiMessage = {
      id: "planning-1",
      role: "team_internal" as const,
      text: "正在理解任务目标",
      sourceSessionId: "web_demo::turn::req_1::planning",
      agentId: "crew::builtin",
      eventType: "team_planning_progress",
      nodeId: "workflow_planning",
      displayMode: "collapsible",
      collapsedTitle: "Crew 正在规划团队协作",
      processText: "- 理解任务目标：进行中",
    };
    const second: UiMessage = {
      ...first,
      id: "planning-2",
      text: "正在推演任务拆分",
      collapsedTitle: "Crew 正在规划团队协作 · 8s",
      processText: "- 理解任务目标：完成\n- 识别工作单元：进行中",
    };

    const messages = mergeTeamInternalMessage([first], second);

    expect(messages).toHaveLength(1);
    expect(messages[0].id).toBe("planning-2");
    expect(messages[0].text).toBe("正在推演任务拆分");
    expect(messages[0].collapsedTitle).toBe("Crew 正在规划团队协作 · 8s");
    expect(messages[0].processText).toContain("识别工作单元：进行中");
  });

  it("keeps a completed team node as one summary bubble with folded process text", () => {
    const items: BackendHistoryItem[] = [
      teamItem({
        content: "让我先看一下相关代码。",
        agent_name: "kk",
        agent_role: "测试方案：测试一下团队协作",
        display_mode: "collapsible",
        collapsed_title: "测试方案：测试一下团队协作 的执行过程",
      }),
      teamItem({
        content: "测试方案：测试一下团队协作提交 Leader 审阅。\n\n测试方案初稿已完成。",
        agent_name: "kk",
        agent_role: "测试方案：测试一下团队协作",
        event_type: "team_submit",
        display_mode: "chat",
        timestamp: 2,
      }),
    ];

    const messages = mapHistoryItems(items);

    expect(messages).toHaveLength(1);
    expect(messages[0].displayMode).toBe("chat");
    expect(messages[0].eventType).toBe("team_submit");
    expect(messages[0].text).toContain("测试方案初稿已完成");
    expect(messages[0].processText).toContain("让我先看一下相关代码");
  });

  it("maps Team member file changes alongside artifacts", () => {
    const messages = mapHistoryItems([{
      role: "team_internal",
      content: "成员已提交",
      source_session_id: "web_demo::turn::req_1::hermes",
      agent_id: "hermes",
      event_type: "team_submit",
      node_id: "build_1",
      artifacts: [{ title: "result.md", path: "/work/result.md", kind: "text" }],
      turn_file_changes: [
        { path: "/work/result.md", name: "result.md", added: 8, removed: 1, status: "modified" },
      ],
    }]);

    expect(messages[0].turnFileChanges).toEqual([
      { path: "/work/result.md", name: "result.md", added: 8, removed: 1, status: "modified" },
    ]);
    expect(messages[0].artifacts).toHaveLength(1);
  });

  it("filters internal plan documents from file cards", () => {
    const messages = mapHistoryItems([{
      role: "assistant",
      content: "完成",
      turn_file_changes: [
        { path: "/work/.crew/plans/plan_demo.md", status: "added" },
        { path: "/work/src/app.ts", status: "modified" },
      ],
    }]);

    expect(messages[0].turnFileChanges?.map((file) => file.path)).toEqual(["/work/src/app.ts"]);
  });

  it("does not merge same node id across different team turns", () => {
    const items: BackendHistoryItem[] = [
      teamItem({
        content: "第一轮 Leader 正在汇总。",
        source_session_id: "web_demo::turn::req_1::leader",
        agent_id: "leader",
        node_id: "leader_summary",
        display_mode: "collapsible",
      }),
      teamItem({
        content: "第二轮 Leader 已汇总。",
        source_session_id: "web_demo::turn::req_2::leader",
        agent_id: "leader",
        event_type: "team_submit",
        node_id: "leader_summary",
        display_mode: "chat",
      }),
    ];

    const messages = mapHistoryItems(items);

    expect(messages).toHaveLength(2);
    expect(messages[0].text).toBe("第一轮 Leader 正在汇总。");
    expect(messages[1].text).toBe("第二轮 Leader 已汇总。");
    expect(messages[0].processText).toBeUndefined();
  });

  it("does not let completion without source session overwrite an existing node", () => {
    const items: BackendHistoryItem[] = [
      teamItem({
        content: "已有执行过程。",
        display_mode: "collapsible",
      }),
      teamItem({
        content: "缺少来源的完成消息。",
        source_session_id: undefined,
        event_type: "team_submit",
        display_mode: "chat",
      }),
    ];

    const messages = mapHistoryItems(items);

    expect(messages).toHaveLength(2);
    expect(messages[0].text).toBe("已有执行过程。");
    expect(messages[1].text).toBe("缺少来源的完成消息。");
  });

  it("keeps team internal thinking and tool calls for the agent timeline", () => {
    const items: BackendHistoryItem[] = [
      teamItem({
        content: "我完成了检查。",
        agent_name: "kk",
        thinking: "先确认运行环境。",
        tool_calls: [
          {
            id: "tool_1",
            name: "terminal",
            ui_label: "运行 node --version",
            arguments: { command: "node --version" },
            result: "v20.0.0",
            status: "done",
          },
        ],
      }),
    ];

    const messages = mapHistoryItems(items);

    expect(messages).toHaveLength(1);
    expect(messages[0].thinking).toBe("先确认运行环境。");
    expect(messages[0].toolCalls).toHaveLength(1);
    expect(messages[0].toolCalls?.[0].uiLabel).toBe("运行 node --version");
  });

  it("merges live team stream timeline events into the same node bubble", () => {
    const items: BackendHistoryItem[] = [
      teamItem({
        agent_role: "实现：2048",
        node_id: "build_1",
        thinking: "先确认要生成单文件游戏。",
      }),
      teamItem({
        agent_role: "实现：2048",
        node_id: "build_1",
        tool_calls: [
          {
            id: "tool_write",
            name: "file_write",
            ui_label: "写入 index.html",
            arguments: { path: "index.html" },
            status: "done",
          },
        ],
        timestamp: 2,
      }),
      teamItem({
        content: "已生成 `index.html`。",
        agent_role: "实现：2048",
        node_id: "build_1",
        timestamp: 3,
      }),
      teamItem({
        content: "@leader 实现：2048 已完成。",
        agent_role: "实现：2048",
        event_type: "team_submit",
        node_id: "build_1",
        display_mode: "chat",
        timestamp: 4,
      }),
    ];

    const messages = mapHistoryItems(items);

    expect(messages).toHaveLength(1);
    expect(messages[0].eventType).toBe("team_submit");
    expect(messages[0].processText).toContain("已生成 `index.html`");
    expect(messages[0].thinking).toContain("先确认要生成单文件游戏");
    expect(messages[0].toolCalls).toHaveLength(1);
    expect(messages[0].toolCalls?.[0].uiLabel).toBe("写入 index.html");
  });

  it("merges direct leader stream into one result without duplicate process text", () => {
    const items: BackendHistoryItem[] = [
      teamItem({
        content: "你好，",
        source_session_id: "web_demo::turn::req_1::leader",
        agent_id: "leader",
        agent_name: "hh",
        agent_role: "leader",
        is_leader: true,
        node_id: "direct_leader_req_1",
        collapsed_title: "Leader 的回复过程",
      }),
      teamItem({
        content: "我是 hh，可以继续帮你处理团队任务。",
        source_session_id: "web_demo::turn::req_1::leader",
        agent_id: "leader",
        agent_name: "hh",
        agent_role: "leader",
        is_leader: true,
        node_id: "direct_leader_req_1",
        timestamp: 2,
      }),
      teamItem({
        content: "你好，我是 hh，可以继续帮你处理团队任务。",
        source_session_id: "web_demo::turn::req_1::leader",
        agent_id: "leader",
        agent_name: "hh",
        agent_role: "leader",
        is_leader: true,
        event_type: "team_summary",
        node_id: "direct_leader_req_1",
        display_mode: "chat",
        collapsed_title: "Leader 的回复过程",
        thinking: "判断为轻量团队聊天。",
        timestamp: 3,
      }),
    ];

    const messages = mapHistoryItems(items);

    expect(messages).toHaveLength(1);
    expect(messages[0].eventType).toBe("team_summary");
    expect(messages[0].text).toBe("你好，我是 hh，可以继续帮你处理团队任务。");
    expect(messages[0].processText).toBeUndefined();
    expect(messages[0].thinking).toBe("判断为轻量团队聊天。");
    expect(messages[0].isLeader).toBe(true);
  });

  it("folds leader review tools into the review bubble instead of showing a standalone tool bubble", () => {
    const items: BackendHistoryItem[] = [
      teamItem({
        source_session_id: "web_demo::turn::req_1::leader",
        agent_id: "leader",
        agent_name: "hh",
        agent_role: "leader",
        is_leader: true,
        node_id: "leader_review_qa_plan",
        tool_calls: [
          {
            id: "tool_review_1",
            name: "delegate",
            ui_label: "审阅测试方案",
            arguments: { node_id: "qa_plan" },
            status: "done",
          },
        ],
      }),
      teamItem({
        content: "@kk @crew 方案已通过 Leader 审阅，开始验证。",
        source_session_id: "web_demo::turn::req_1::leader",
        agent_id: "leader",
        agent_name: "hh",
        agent_role: "leader",
        is_leader: true,
        event_type: "team_review",
        node_id: "leader_review_qa_plan",
        display_mode: "chat",
        timestamp: 2,
      }),
    ];

    const messages = mapHistoryItems(items);

    expect(messages).toHaveLength(1);
    expect(messages[0].eventType).toBe("team_review");
    expect(messages[0].text).toBe("@kk @crew 方案已通过 Leader 审阅，开始验证。");
    expect(messages[0].toolCalls).toHaveLength(1);
    expect(messages[0].toolCalls?.[0].uiLabel).toBe("审阅测试方案");
  });

  it("suppresses generic approve decision when the same leader review already says it passed", () => {
    const items: BackendHistoryItem[] = [
      teamItem({
        content: "@kk @crew 方案已通过 Leader 审阅，开始验证。",
        source_session_id: "web_demo::turn::req_1::leader",
        agent_id: "leader",
        agent_name: "hh",
        agent_role: "leader",
        is_leader: true,
        event_type: "team_review",
        node_id: "leader_review_qa_plan",
        display_mode: "chat",
      }),
      teamItem({
        content: "审阅通过，继续后续流程。",
        source_session_id: "web_demo::turn::req_1::leader",
        agent_id: "leader",
        agent_name: "hh",
        agent_role: "leader",
        is_leader: true,
        event_type: "team_decision",
        node_id: "leader_review_qa_plan",
        mention_intent: "approve",
        display_mode: "chat",
        timestamp: 2,
      }),
    ];

    const messages = mapHistoryItems(items);

    expect(messages).toHaveLength(1);
    expect(messages[0].eventType).toBe("team_review");
    expect(messages[0].text).toBe("@kk @crew 方案已通过 Leader 审阅，开始验证。");
  });

  it("prefers team summary over a duplicate assistant final bubble", () => {
    const assistant: UiMessage = {
      id: "assistant-final",
      role: "assistant",
      text: "项目总共用了 11 分钟 21 秒。",
    };
    const summary: UiMessage = {
      id: "team-summary",
      role: "team_internal",
      text: "项目总共用了 11 分钟 21 秒。",
      eventType: "team_summary",
      nodeId: "leader_summary",
      sourceSessionId: "web_demo::turn::req_1::leader",
      agentId: "leader",
    };

    const messages = mergeTeamInternalMessage([assistant], summary);

    expect(messages).toHaveLength(1);
    expect(messages[0].role).toBe("team_internal");
    expect(messages[0].eventType).toBe("team_summary");
  });
});

describe("mergeHistoryWithLiveMessages", () => {
  it("drops websocket replay copies already present in Team history", () => {
    const history = mapHistoryItems([
      {
        role: "team_internal",
        content: "审阅未通过，@kk 请继续修订。",
        event_type: "team_decision",
        node_id: "leader_review",
        agent_id: "leader",
        source_session_id: "session::leader",
      },
    ]);
    const replay = { ...history[0], id: "live-copy" };
    const laterDecision = {
      ...history[0],
      id: "live-approve",
      text: "审阅通过，继续后续流程。",
    };

    const merged = mergeHistoryWithLiveMessages(history, [replay, laterDecision]);

    expect(merged).toHaveLength(2);
    expect(merged.map((message) => message.text)).toEqual([
      "审阅未通过，@kk 请继续修订。",
      "审阅通过，继续后续流程。",
    ]);
  });

  it("drops a live assistant final when history already has the team summary", () => {
    const history = mapHistoryItems([
      {
        role: "team_internal",
        content: "项目总共用了 11 分钟 21 秒。",
        event_type: "team_summary",
        node_id: "leader_summary",
        agent_id: "leader",
        source_session_id: "web_demo::turn::req_1::leader",
      },
    ]);
    const liveFinal: UiMessage = {
      id: "assistant-final",
      role: "assistant",
      text: "项目总共用了 11 分钟 21 秒。",
    };

    const merged = mergeHistoryWithLiveMessages(history, [liveFinal]);

    expect(merged).toHaveLength(1);
    expect(merged[0].role).toBe("team_internal");
    expect(merged[0].eventType).toBe("team_summary");
  });

  it("preserves direct user mention correlation fields from Team history", () => {
    const [message] = mapHistoryItems([{
      role: "team_internal",
      content: "我当前使用 K3 模型。",
      agent_id: "coder",
      communication_kind: "user_mention_answer",
      communication_status: "answered",
      request_id: "mention_req_1",
      reply_to: "bus_msg_1",
      communication_request_text: "你使用的是什么模型？",
    }]);

    expect(message.communicationKind).toBe("user_mention_answer");
    expect(message.communicationStatus).toBe("answered");
    expect(message.requestId).toBe("mention_req_1");
    expect(message.replyTo).toBe("bus_msg_1");
    expect(message.communicationRequestText).toBe("你使用的是什么模型？");
  });

  it("replaces a waiting direct mention with the terminal answer by request id", () => {
    const waiting: UiMessage = {
      id: "waiting",
      role: "team_internal",
      text: "正在询问 coder…",
      agentId: "coder",
      communicationKind: "user_mention_answer",
      communicationStatus: "waiting_reply",
      requestId: "mention_req_waiting",
      communicationRequestText: "你使用的是什么模型？",
    };
    const answered: UiMessage = {
      ...waiting,
      id: "answered",
      text: "当前使用 K3 模型。",
      communicationStatus: "answered",
      replyTo: "bus_msg_waiting",
    };

    const merged = mergeTeamInternalMessage([waiting], answered);

    expect(merged).toHaveLength(1);
    expect(merged[0].text).toBe("当前使用 K3 模型。");
    expect(merged[0].communicationStatus).toBe("answered");
  });

  it("merges direct mention streaming frames into one answer timeline", () => {
    const waiting: UiMessage = {
      id: "waiting-stream",
      role: "team_internal",
      text: "正在询问 coder…",
      agentId: "coder",
      communicationKind: "user_mention_answer",
      communicationStatus: "waiting_reply",
      requestId: "mention_req_stream",
    };
    const stream: UiMessage = {
      id: "stream",
      role: "team_internal",
      text: "当前使用 ",
      agentId: "coder",
      communicationKind: "user_mention_answer",
      communicationStatus: "delivered",
      requestId: "mention_req_stream",
      eventType: "team_stream",
      displayMode: "stream",
      collapsedTitle: "coder 的回答过程",
      thinking: "先确认模型配置。",
      toolCalls: [{
        toolCallId: "runtime-info-1",
        name: "runtime_info",
        args: "{}",
        status: "running",
        startedAt: 0,
      }],
    };
    const answered: UiMessage = {
      ...stream,
      id: "answered-stream",
      text: "当前使用 Kimi Code/K3。",
      eventType: "team_communication",
      displayMode: "chat",
      communicationStatus: "answered",
    };

    const streaming = mergeTeamInternalMessage([waiting], stream, { append: true });
    const merged = mergeTeamInternalMessage(streaming, answered);

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      text: "当前使用 Kimi Code/K3。",
      communicationStatus: "answered",
      eventType: "team_communication",
      processText: "当前使用 ",
    });
    expect(merged[0].thinking).toBe("先确认模型配置。");
    expect(merged[0].toolCalls?.[0]).toMatchObject({ toolCallId: "runtime-info-1", status: "running" });
  });

  it("merges cumulative Team stream frames without duplicating text or losing timing", () => {
    const base: UiMessage = {
      id: "stream-base",
      role: "team_internal",
      text: "先检查",
      sourceSessionId: "web_demo::turn::merge_req::coder",
      agentId: "coder",
      nodeId: "build_1",
      eventType: "team_stream",
      displayMode: "stream",
      processText: "先检查",
      turnStartedAt: 100,
      timestamp: 101,
    };
    const cumulative: UiMessage = {
      ...base,
      id: "stream-cumulative",
      text: "先检查模型配置",
      processText: "先检查\n读取模型配置",
      timestamp: 103,
    };
    const final: UiMessage = {
      ...cumulative,
      id: "stream-final",
      text: "模型配置正常。",
      processText: "先检查\n读取模型配置",
      eventType: "team_submit",
      displayMode: "chat",
      turnDurationMs: 4200,
      timestamp: 104,
    };

    const streaming = mergeTeamInternalMessage([base], cumulative, { append: true });
    const merged = mergeTeamInternalMessage(streaming, final);

    expect(streaming[0].text).toBe("先检查模型配置");
    expect(streaming[0].processText).toBe("先检查\n读取模型配置");
    expect(merged[0].text).toBe("模型配置正常。");
    expect(merged[0].turnStartedAt).toBe(100);
    expect(merged[0].turnDurationMs).toBe(4200);
  });

  it("deduplicates a live user mention answer already restored from history", () => {
    const item = teamItem({
      content: "我当前使用 K3 模型。",
      event_type: "team_communication",
      node_id: undefined,
      source_session_id: "web_demo::turn::mention_req_2::coder",
      communication_kind: "user_mention_answer",
      communication_status: "answered",
      request_id: "mention_req_2",
      reply_to: "bus_msg_2",
    });
    const [historyMessage] = mapHistoryItems([item]);
    const liveMessage: UiMessage = {
      ...historyMessage,
      id: "live-mention-answer",
    };

    const merged = mergeHistoryWithLiveMessages([historyMessage], [liveMessage]);

    expect(merged).toHaveLength(1);
    expect(merged[0].requestId).toBe("mention_req_2");
    expect(merged[0].communicationStatus).toBe("answered");
  });
});
