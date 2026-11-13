# Ace 回答内交互组件：可行性与后续实施方案

日期：2026-10-22
状态：本文件保留首次可行性评估。用户随后授权仅实现桌面端，完成记录见 `handoff-desktop-inline-interactions-implemented.md`；Web 阶段未启动。

## 本轮目标

A. 确认 Ace 能否支持回答内图表与丰富交互。
B. 结合现有代码找出接入方式。
C. 给出实现范围与验收明确的方案。

结论：可以实现，现有 Widget/Canvas 能力可复用，但目前的侧边展示与回答内组件并非同一条完整链路。

## 代码证据

- `desktop/src/ui/chat-render.ts` 的 `renderTurnInspirationSurfaceCard`：从工具结果提取 surface，目前取最近一个结果并渲染“打开”入口，尚非正文中多个可交互块。
- `desktop/src/ui/features/blueprint-surface.ts` 的 `widgetFrame`：使用 iframe 加载 Widget，并已有生成中占位、版本字段、Canvas viewState 处理。
- `crew/sites/blueprint.py` 的 `runtime_html`：注入 DaimonWidget/DaimonCanvas，已有 emit 和 setViewState 接口。
- `crew/gateway/routers/sessions.py` 的 `_tool_result_for_history`：已有 Widget/Canvas show 结果的历史恢复。
- `crew/gateway/routers/sites.py` 的 `emit_widget_event`：当前事件需要 Binding，并触发 Automation，不能直接视为“向当前聊天提交参数”的现成接口。
- `desktop/src/ui/markdown.ts`：Markdown 原始 HTML 转义并经 DOMPurify 过滤。保持此边界，通过独立组件宿主加载交互。
- `desktop/src/ui/components/transcript.ts` 的 `patchTranscriptMarkdown`：会替换 Markdown 子节点；交互宿主需要独立稳定节点，防止流式更新重置输入。
- `web/src/components/MessageItem.tsx`、`MarkdownContent.tsx`：Web 使用另一套 React 消息渲染；桌面端改动不会自动覆盖 Web。

## 推荐产品体验

回答可以组合文字、图表、指标卡、输入控件和操作按钮。以截图中的收益分析为验收用例：文字解释之后出现三条情景折线；用户调整成本或收入参数，数字卡和曲线即时更新；点击“用这些参数继续分析”才把当前参数作为用户输入交给 Agent。

本地交互（拖滑块、切换情景、输入数字、排序）在组件内执行，不调用模型。需要进一步推理或执行的动作通过明确提交回到当前对话。表单提交反馈应显示处理中、成功、失败，并防止重复提交。

## 后续 Plan：第一阶段桌面端闭环

目标：在同一条回答内展示可操作组件，并能保存、回放和继续对话。

1. 定义带版本的消息组件描述：componentId、sessionId、messageId、resourceId、resourceRevision、类型、标题、摘要、初始状态和允许动作。由已验证的工具结果产生；明确正文锚点和顺序，支持多个组件。
2. 为 Widget show 增加内联展示方式；复用现有资源服务和运行时。新增稳定的消息组件宿主，组件加载独立于 Markdown 流式重绘，并允许展开到已有侧栏。
3. 实现图表与表单示例。第一版可复用 Widget 内 HTML/JS/SVG，无需先构建通用图表组件库。常用图表后续再抽成受控组件，复杂自定义内容继续用隔离 Widget。
4. 新增明确的聊天提交事件桥接，不将现有 Automation emit 直接等同于聊天发送。由宿主绑定会话与消息，校验 frame 来源、组件身份、动作及载荷大小，使用当前聊天的发送/排队机制。
5. 按账户、会话、消息、组件保存用户状态；资源需有可回放版本或不可变快照。只保存 revision 数字不足以保证旧内容仍可加载；资源缺失时保留摘要和恢复提示。
6. 隔离生成代码，不开放桌面 preload、主进程或任意文件权限；审查现有 sandbox 和资源源隔离是否适用于正文嵌入。处理高度调整、可见时加载、组件错误边界和卸载。
7. 补充 Agent 使用说明：何时选择可视化、如何生成并展示组件、怎样解释参数单位和计算规则；纯文本回答仍为默认可用路径。

### 验收条件

- 一个收益分析回答内同时出现说明、折线图、数字卡、滑块、数字输入和提交按钮，无须先点击“打开”。
- 调整有效参数后图表与数字卡一致更新；本地调整不产生模型请求；无效输入显示明确反馈。
- 点击继续分析仅发送一次参数快照到所属聊天；切换聊天后，旧组件不会向新聊天误发送。
- 模型流式续写不会重置已输入值；同一回答内两个组件状态彼此独立。
- 重开应用和历史会话能恢复组件及保存值；后续修改 Widget 不会静默改写历史展示版本。
- 伪造跨 frame 事件被拒绝；生成组件不能访问桌面桥接或其他会话数据；加载失败不会破坏正文。
- 通过相关消息渲染、状态回放、事件桥接测试，并在桌面实际验证上述场景。
- 如需启动验证，遵守 AGENTS.md：先停现有 Gateway，再从 desktop 执行 npm run dev，不能另起多个 Gateway。

## 第二阶段：Web 一致性

目标：React Web 端消费同一组件描述与状态协议，提供相同的本地交互与聊天提交。
验收：同一会话在桌面和 Web 展示一致、窄屏可操作、历史状态恢复正确；部署资源地址不依赖桌面的 ace-site 协议。明确跨端并发修改的状态版本冲突处理。

## 本轮边界

仅检查代码并编写方案；上述验收均为后续实现要求，不代表已经验证通过。未改动当前已有的打包、发布和桌面主进程未提交修改。
