# 多智能体执行内核与项目预审验收

## 本轮范围

从 `dev@d3647137390be0e5e4113c36cc3b7945a7fec633` 继续开发。目标是固定小队的可靠评审闭环，
不是宣称 followup plan 的所有动态补员/成本优化阶段已经完成。

参考 DeepSeek `dsh-superagent-prototype@8c32a9dc5d72c033816c6a8110b37f40cf94fba4`
的 `docs/001-domain.md`、`docs/003-execution.md`，吸收：

- 稳定工作身份与执行尝试分离；
- 外部效果之前提交执行意图；
- 运行状态、真实产物与人工验收分离；
- 不确定结果进入明确的人工核实/恢复路径。

没有复制参考仓库实现，也没有把整个 manager 简单改名当作完成解耦。

## 数据与调度

`crew/collaboration/runtime/store.py` 是执行权威状态；`scheduler.py` 仅依赖存储和异步 executor
接口，不依赖 TeamManager。现有 TeamPlan/Kanban 是兼容投影，不再决定已交接协作任务的派发。

1. 规划结果与分配持久化；
2. 一个 SQLite `BEGIN IMMEDIATE` 事务核对规范化 workflow、认领规划交接并写入执行快照；
3. claim 事务检查依赖、并发、成员槽位、resource keys，保存 attempt 和完整工作包；
4. 事务成功后才调用真实成员；
5. 保存实际结果、错误、executor/call id。空结果不能完成；
6. 两客户端从同一执行日志投影节点、依赖、负责人、尝试历史和状态。

默认并发 2，同一成员同时只执行一个工作包。声明相同 resource key 的工作不能并发；
显式共享 cwd/referenced_paths 的适配器调用保守串行。不保证任意外部系统的全局恰好一次。

## 输入权限与证据边界

所选知识库必须归当前 owner、全部来源已解析且有 Source 页面。执行前固定只读快照，
每个来源保留 source_id/page_id/title/content/sha256。每轮最多 50 份、12 万字符，超限明确失败，
不静默截断。sha256 覆盖整个 content 的 UTF-8 字节，不是人工摘录或页面内的某一小节。

知识库与附件中的指令只作为资料。独立审阅只收到原始快照及其依赖节点的结果，不收到无关草稿。
资料选择只赋予本轮来源访问权，不授予 Wiki 管理权限。该路径使用应用层 owner-scoped
Source 读取，而不是声称 Leader 已调用它并不拥有的 Wiki 工具。

## 评审场景契约

WebSocket 显式 `collaboration_contract=project_review_v1` 启用。本轮模拟样本只服务 A/B/C
三个虚构项目，不把专用约束施加到其他普通任务：

```
Leader 来源核对
  ├─ 技术审阅员 ─┐
  └─ 商业审阅员 ─┴─ Leader 综合复核
```

规划必须满足上述真实依赖和不同成员约束；不能只在 objective 写“等待”而漏掉 depends_on。
非法计划有限重试，仍不合法就失败，不使用硬编码成功拆分兜底。

内置成员通过每轮 `collaboration_readonly` 禁用工具 schema 和工具执行授权，不修改共享 agent
配置。直接交付正文，不以“已写入文件/已登记产物”代替可供下游审阅的成果。通用协作任务不受影响。
外部 CLI 的权限仍由原执行器/安全策略管理，不能把本轮内置验证当作外援只读隔离证明。

最终 JSON 必须：
- 覆盖 A/B/C × 四个精确维度；
- 每项有具体材料主张、分析判断、待确认内容及证据强弱；
- 引用实际文件、精确二级小节和连续逐字原文；
- 明确虚构、人工决定，恰好三个关键追问；
- 不提供自动总排名、总分或录取决定。

通过后生成 Markdown 横向比较和证据卡；拒绝的真实输出仍留在失败 attempt 中以便审计。
**这些校验不证明语义推理必然正确，也不构成专业评委的最终验收。**

## 操作与恢复

- `GET /api/collaboration/sessions/{session}/executions`
- `GET /api/collaboration/executions/{task}`
- `POST /api/collaboration/executions/{task}/control`
  - `pause`：停止启动后续工作，已发调用可继续；
  - `cancel`：取消未启动工作，真实在途结果照常保留；取消后不能通过暂停/恢复绕过终态；
  - `resume`：恢复已暂停任务；
  - `retry` + `unit_id`：原成员新 attempt，保留旧结果；团队版本变更时拒绝静默换人；
  - `reconcile`：本网关 runner 已停止后，将在途记录变成 unknown；
  - unknown 重试还须 `confirm_unknown: true`，先核对实际副作用。

恢复启动同步占位，防止控制请求间的启动窗口误判为无 runner。已完成节点不重新执行，
失败依赖的后继重新判断是否可运行。恢复记录在看板可见，历史对话中的旧失败消息不被篡改。
目前后台恢复不重新广播整段对话流；请在协作看板查看最新结果。

恢复假设单一网关管理一组任务。多个网关共享数据库时，必须先确认旧进程/外部调用已停止；
尚未实现跨进程租约、远端查询结果协调或全局副作用幂等。

## 已知未覆盖

- Hermes/Kimi CLI 外援全链路（本机没有这些可用外援；不静默替换身份）；
- 自动补员、用户批准改派、新计划版本复用已完成成果；
- 任意文档规模的检索、OCR 与跨知识库访问；
- 模型事实准确率或专业评审质量的统计保证；
- 所有历史测试绿灯：基线已有旧 Team 规划夹具/契约失败，须与本轮新增测试分开报告。

真实模型验收不能用 FakeProvider 测试替代。运行方式及新编虚构材料见
`examples/project-review/README.md`，最终运行的原始记录和截图随交付包提供。
