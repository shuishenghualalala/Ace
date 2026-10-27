# 桌面聊天原生交互：实现与验收记录

日期：2026-10-26；最终验收：2026-10-27
状态：A/B/C/D 已完成，真实会话、完整重启恢复、明暗主题和宽度变化验收通过。原浅色设置及会话侧栏已恢复。

## 固定目标

A. 实际回复中自然显示图表和控件。
B. 参数本地更新，明确点击后带当前参数继续分析。
C. 重开会话恢复状态，并在实际桌面中验证。
D. 常见图表/表单使用原生组件和结构化协议，模型无需逐次编写 HTML/JS。

## 实现

- `crew/sites/native_visual.py`：64 KB 以内的声明校验；最多 8 个参数、4 个系列、120 个点；支持折线/柱状图与 range/number/text/select。表达式是有界算术树，不执行任意脚本。
- `crew/tools/blueprint_tools.py`：`Widget.render(spec)` 返回 version 2 原生内容；不创建网页资产或文件。
- `crew/agent/loop/tool_result_display.py` 和 `crew/gateway/routers/sessions.py`：实时结果和历史回放保留完整描述。
- `desktop/src/ui/features/native-visual.ts`：内置 DOM 控件及 SVG 图表；本地计算、图例显隐、指针/键盘逐点读数、非法值反馈、可选续问动作；参数按账户/会话/工具调用保存在当前设备。
- `conversation-renderer.ts`：稳定内容块位于本轮最终解释之前；流式文本更新保留控件节点。
- `app.ts` 绑定已有发送/队列入口，续问保留聊天草稿和附件；可见摘要与提交给模型的文字相同。
- `web-messages.css`：共享主题与字体，透明背景、无外层卡片和嵌套滚动，紧凑输入；最终修整为整洁坐标刻度及正文左对齐。
- `crew/sites/capabilities.py`、`crew/skills/widget/SKILL.md`、`references/native-visual.md`：常见交互默认走原生展示，复杂独立 App 保留原有路径。

## 实际桌面证据

环境：`cd desktop && npm run dev`，桌面托管 Gateway，开发账户 dev，deepseek-v4-flash；通过原生桌面 UI 输入自然语言，没有注入模型回复或提交回调。

1. 会话 `请在这条回复里做一个可调参数的12个月收`：自然请求生成成本滑块、收入/投入数字输入和三系列累计收益图。实际工具数量为 2（读取说明、展示）；没有网页文件卡或自动浏览器侧栏。
2. 默认 6,000 / 8,000 / 3,000 参数结果为 -3,000 / 38,536.24 / 114,771.26；UI 修改收入 12,000，用滑块键盘操作将成本调到 7,000，结果即时变为 21,000 / 83,304.35 / 197,656.90。解锁后补充鼠标拖拽实测：成本从 7,000 拖到 8,000，结果变为 9,000 / 71,304.35 / 185,656.90；随后恢复成本 7,000。
3. 点击图例成功隐藏/恢复乐观系列。调参和图例变化没有新增聊天轮次；显式点击续问后只出现一条可读摘要，携带 7,000 / 12,000 / 3,000；模型续答表格给出 +21,000 / +83,304 / +197,657。输入框中的测试草稿仍在。
4. 会话 `用柱状图比较甲、乙、丙三个方案的四季度完`：真实生成三系列四季度柱状图，未添加输入框或续问动作。
5. 完整重启：先 SIGTERM 停止 Gateway PID 7241，再退出 Electron，运行 `npm run dev`。解锁后通过 UI 打开收益会话，确认成本 7,000、收入 12,000、投入 3,000 恢复；三组结果为 21,000 / 83,304.35 / 197,656.90。图表刻度为整洁的 50,000 间隔，左侧与正文对齐。
6. 逐点读数：图表获焦后按左键显示第 11 月；指针移到第 7 月后同时显示 11,000 / 39,949.55 / 81,842.05。静态柱状图历史也正确恢复，轴为 0/25/50/75/100，三系列完整，无输入控件和续问按钮。

证据文件：
- `output/playwright/native-visual/followup-ax.txt`：实际图表参数、结果、图例、可读续问、模型答复及保留草稿。
- `output/playwright/native-visual/followup.png`：实际模型续答与未发送草稿。
- `output/playwright/native-visual/restored-ax.txt`：完整重启后的参数及结果恢复。
- `output/playwright/native-visual/restored-parameters.png`、`restored-chart.png`：最终控件和折线图；后者含指针第 7 月读数。
- `output/playwright/native-visual/chart-inspection-ax.txt`：第 7 月跨系列读数。
- `output/playwright/native-visual/static-bar.png`、`static-bar-ax.txt`：实际历史柱状图。
- `output/playwright/native-visual/static-bar-dark.png`：2026-10-27 实际深色主题图表，系列、轴、网格及读数清晰。
- `output/playwright/native-visual/static-bar-expanded.png`：收起会话侧栏后，图表随可用宽度正确重排。
- `output/playwright/native-visual/theme-restored-ax.txt`：最终外观设置已恢复选中浅色；会话侧栏也已展开恢复原状。

## 自动验证

- TypeScript typecheck 通过。
- Vitest：native-visual 7、render-chat-diff 21、chat-controller-send 5，共 33 项通过。覆盖计算、非法值、单次提交、会话隔离、恢复、流式节点稳定、静态图、文本/选择器。
- Python：test_native_visual、test_sites、test_tool_result_display、test_capability_profiles，共 49 项通过；补充非法 param 类型校验后，test_native_visual 5 项复验通过。
- 修改文件 ESLint、Stylelint、git diff --check 通过。
- happy-dom 不支持旧版 ace-site iframe 的诊断来自既有 HTML 兼容测试，33 项断言均通过；新原生路径不使用 iframe。

## 交付边界

仅桌面端；常见图表/表单为原生协议，历史 HTML 回答兼容保留。参数保存在当前设备，不跨设备同步。每次工具调用为独立历史快照，修订在新回复中创建，不改写旧回答；内容位于本轮最终解释之前，未实现任意 Markdown 字符位置插入。当前支持数值 x 轴；文本和选择器用于续问参数，不支持作为算术条件。代码仍在当前工作区，未提交或发布。

## 验收结论

2026-10-27 解锁后完成最后主题检查：深色图表已截图，恢复浅色并确认实际界面；收起/展开会话侧栏时图表随宽度变化重排，最终恢复侧栏。此前锁屏阻塞已解除，无剩余验收项。

A 的证据为真实折线/柱状图截图；B 为 followup-ax.txt 与 chart-inspection-ax.txt；C 为 restored-ax.txt 与 restored-parameters.png；D 为 Widget.render 实际工具结果链路、无资产创建回归测试及 native-visual 协议实现。当前仍为工作区改动，未提交或发布。
