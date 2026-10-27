# 桌面回答内交互组件：实现记录

日期：2026-10-22。范围：桌面端，Web 不在本轮范围。

2026-10-26 后续校准：本文记录首版 HTML 内联链路及其真实验收，不代表用户后来要求的常见原生组件路线已经完成。用户要求重点参考 Codex / ChatGPT 的呈现效果；差异、实施顺序与新验收标准见 `desktop-conversation-ui-reference.md`。新改造尚未实施。

## 目标与证据

A. 回答内交互组件：已实现。
- `crew/tools/blueprint_tools.py`：Widget.show 接受 display=inline，返回版本化 HTML 快照（128 KB 上限）。
- `desktop/src/ui/features/inline-widget.ts`：图表、表单、滑块和按钮的隔离页面宿主。
- `desktop/src/ui/features/conversation-renderer.ts`：独立 keyed 消息块，支持多个组件；正文更新不重建 iframe。
- `desktop/src/main/site-preview-protocol.ts`、`desktop/src/shared/inline-widget-shell.ts`：独立文档、严格 CSP 与 opaque-origin sandbox；不放宽主界面 CSP。

B. 参数交互与对话提交：已实现。
- AceInteractive.state/setState 保存参数，本地计算不调用模型。
- 宿主按钮校验输入后提交 JSON 快照；校验 frame 来源与请求 ID，阻止自动发送、跨会话发送及重复提交。
- `chat-controller.ts` 的 sendInlineWidgetMessage 复用发送/排队链路，不消耗草稿、附件和批注。
- `inline-widget-capability.ts` 为普通桌面内置 Agent 启用现有 Blueprint capability；保留模型配置并遵守服务端权限过滤，外部执行器配置不变。
- `crew/skills/widget/references/inline-runtime.md` 说明实际 API；`examples/inline-revenue.html` 提供完整样例。

C. 状态恢复与验证：已实现。
- HTML 快照随工具结果保存，live/history 均保留完整内容；后续源文件编辑不改写旧回答。
- 参数按账户、会话、工具调用保存在本机 localStorage（16 KB 上限），重开恢复；存储失败显示提示。
- 加载失败与脚本错误显示提示，不破坏消息正文。

## 验证结果

- TypeScript typecheck、桌面 build、修改文件 ESLint、消息样式 Stylelint、git diff --check 通过。
- 桌面相关单测共 99 项通过：inline-widget（6）、inline-widget-capability（2）、render-chat-diff（20）、render-agent-turn（63）、chat-controller-send（5）、chat-channel-session（3）。新增错误边界用例在最终局部回归中验证。
- 后端 test_capability_profiles、test_sites、test_tool_result_display 共 43 项通过。
- `node desktop/scripts/inline-widget-smoke.cjs`：独立真实 Electron 窗口，使用实际桌面 CSP 与 ace-site 协议，验证曲线重算、参数恢复、一次提交、无效输入拦截、父页面/桌面桥接隔离及网络阻断。
- 实测截图：`output/playwright/inline-widget/desktop-interactive.png`。

初次验证边界：独立 Electron 实测注入提交回调，未调用模型；当时不足以代表真实会话验收。后续真实模型及重启验证见文末。happy-dom 不支持 ace-site，DOM 测试会打印协议加载诊断；真实协议已通过 Electron 验证。

## 使用

重启新版桌面与 Gateway 后，在内置 Agent 对话中提出：“在回答里做一个可调整成本和收入的收益分析图，带滑块和数字输入。”
Agent 按 Widget skill 创建 HTML、validate，然后调用 Widget.show(display=inline)。
重启遵守 AGENTS.md：先停止现有 Gateway，再 `cd desktop && npm run dev`，不要单独创建多个 Gateway。

第一版支持自包含页面，不加载 CDN 或联网资源；参数只存当前设备。外部执行器能力配置保持原样。
没有启动 Web 阶段，没有更改原有发布/打包/桌面主进程未提交改动。

## 2026-10-23 真实会话复验（历史阻塞，已解除）

用户指出独立窗口不足以验收，验收状态改为待真实会话验证，前述“已实现”不代表真实模型端到端通过。
已通过 npm run dev 启动实际项目桌面及其托管 Gateway，使用 UI 在新聊天发送内联收益图请求。当前 deepseek-v4-flash 在生成前返回 401 authentication_error（凭据无效），A/B/C 的真实会话验收均未通过，等待用户在模型设置修复凭据。
检查真实工具链发现：Registry 默认 50,000 字符限制会将较大 HTML 快照替换成文件预览，破坏 surface JSON。已为 Widget 配置 1,000,000 字符输出上限（容纳 128 KB 源码与 JSON 转义），增加约 84 KB HTML 经 Registry → UI → history 的回归用例。相关后端 35 项测试通过。

## 2026-10-26 真实桌面会话验收完成

目标清单：A. 实际聊天内显示交互组件；B. 调整参数并提交继续分析；C. 重新打开会话验证状态恢复并修复实测问题。

环境：实际 `npm run dev` 桌面及其托管 Gateway，开发账户 dev，模型 deepseek-v4-flash。用户修复凭据后已解除 401。没有注入模型回复或提交回调。

- A：通过实际聊天请求生成 `widget_cb60770af1a0`，回复中出现三条折线、收益卡片、成本滑块和数字输入框。原始 live 展示与重新打开后的 history 展示均可见，iframe 使用 `ace-site://widget_cb60770af1a0/?inline_shell=1`。证据：`output/playwright/inline-widget/real-chat-parameters.png`。
- B：通过桌面 UI 将收入从 8,000 改为 12,000，并实际拖动成本滑块从 6,000 到 7,000。收益即时更新为 21,000 / 83,304 / 197,657。点击宿主“用当前参数继续分析”后，聊天出现真实用户消息，parameters 与 controls 均携带 7,000 / 12,000 / 3,000；模型用 38 秒、1 次计算工具调用完成续答，明确引用这些参数并给出相同收益。证据：`output/playwright/inline-widget/real-chat-followup.png` 和 `real-chat-restored-ax.txt`。
- C：10 月 26 日确认旧开发 Gateway 进程已退出、端口没有监听，再使用 `npm run dev` 重启桌面及托管 Gateway。打开 10 月 23 日测试会话，原组件恢复成本 7,000、收入 12,000、投入 3,000，卡片与折线仍为 21,000 / 83,304 / 197,657。证据：`output/playwright/inline-widget/real-chat-restored-parameters.png`、`real-chat-restored-chart.png` 和 `real-chat-restored-ax.txt`。

会话侧栏标题为 `请在这条聊天回复里直接嵌入可交互的“12`。模型在原轮后台校验后又 show 了一个修订快照，因此历史中有两个独立卡片：本次操作的原卡片保留修改值，另一张保持默认值，符合按工具调用独立保存的实现。此项仅存档，不追加范围。

验收结论：桌面端 A/B/C 均通过。代码仍在当前工作区，未提交或发布；当前开发桌面停留在恢复后的原组件供检查。参数保存范围仍为当前设备。
