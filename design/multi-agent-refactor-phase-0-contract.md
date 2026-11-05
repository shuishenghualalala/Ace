# Ace 多智能体协同重构：阶段 0 实施契约与代码盘点

**状态：** 实施基线；阶段实现时如需更改不变量，应同步更新本文  
**日期：** 2026-11-02  
**上层设计：** [多智能体协同重构设计稿](multi-agent-collaboration-redesign.md)

## 1. 结论与范围

重构团队协作核心，建立单一的新任务决策与执行链路。保留并对接已有的成员执行、模型、交互、会话和产品入口；分清这些能力和旧决策逻辑。开发期不要求旧任务迁移、恢复或历史兼容，也不将旧数据清理作为本阶段操作。

本文是根据当前 `dev` 工作区静态阅读得到的实施契约，不代表端到端行为已验证。现有工作区包含大量未提交修改；实施前应按文件和功能区分已授权的现有工作，不能通过重置、覆盖或大范围删除来清理。

### 1.1 本阶段的决定

1. 使用新的协作领域对象和独立的协作存储表；先以现有 SQLite 基础设施为实现起点，不假设 Dynamic Kanban 的业务表就是新内核的权威状态。
2. 一个用户任务对应一个稳定 `task_id`；任务、计划、单元、尝试和决策都带 owner 范围。会话是用户交互容器，不以 session ID 替代任务身份。
3. 模型输出工作单元与候选建议；所有模式经过相同校验和统一分配器。DAG 为工作依赖图，不承担职责选择和运行状态的全部语义。
4. 新内核内由应用服务协调命令，仓储负责事务提交；数据库是已提交状态的权威来源，内存任务、事件推送和 UI 只能作为执行工具或投影。
5. 计划、分配或执行意图持久化失败时，不得继续宣称已创建或派发成功。外部调用发生在事务外，处理重复和不确定结果，不保证第三方恰好执行一次。
6. 开发期间旧入口可暂留，但一个任务只交由一套调度器；切换完成后移除旧 Team 新任务入口。独立的 Dynamic Kanban 产品及其运行时不在本次删除范围内。

## 2. 现有链路与接口盘点

| 环节 | 当前事实 | 重构处理 |
| --- | --- | --- |
| 应用装配 | [crew/app.py](../crew/app.py) 创建 `InProcessTeamManager`，注入 owner provider、registry、session、memory、external store、interaction bridge 与 Kanban store；同处还单独创建 Dynamic Kanban manager | 将新应用服务接入 TeamManager 边界；保留独立 Dynamic Kanban 装配 |
| Team 接口 | [crew/core/interfaces.py](../crew/core/interfaces.py) 的 `TeamManager` 定义 `interact(envelope)` 和 `destroy`；实际网关还使用若干扩展方法 | 盘点扩展调用并明确新服务接口；不要只替换 ABC 的两个方法就认为接入完整 |
| 组队与保存 | [runtimes.py](../crew/gateway/routers/runtimes.py) 内含候选目录、模型组队、建议、草稿与团队创建接口；[formation.py](../crew/team/formation.py) 负责组队决策；[external/store.py](../crew/agent/external/store.py) 保存团队与成员 | 复用候选查询与团队存储中已确认适用的能力；将组队决策收敛到 formation 服务，网关只做认证、参数转换和响应 |
| 成员构建 | [team_builder.py](../crew/team/team_builder.py) 的 `TeamMemberFactory` 组装成员配置、模型及执行 Agent | 保留为适配器候选，验证内置/外部 Agent、owner 和工具策略，再由 dispatcher 调用 |
| 任务入口 | [team_manager.py](../crew/team/team_manager.py) 的 `interact`、[turn_decision.py](../crew/team/turn_decision.py) 与 [turn_router.py](../crew/team/turn_router.py) 共同判断会话和工作流 | 使用统一 intake；接入现有 `Envelope`/`ResponseChunk`，任务 ID 独立于 session ID |
| 规划和分配 | [graph_planner.py](../crew/team/graph_planner.py) 包含 fast/standard/ai、多种兜底和分配；[workflow_plan.py](../crew/team/workflow_plan.py) 已有 WorkUnit 等结构 | 重新实现规划、校验和唯一分配器；可参考结构，不保留不同模式绕过分配器的路径 |
| 调度执行 | [team_workflow_runtime.py](../crew/team/team_workflow_runtime.py) 通过 host 调用 `InProcessTeamManager`；后者拥有计划、任务、锁、委派和恢复等状态 | 替换为 scheduler + dispatcher + repository 的显式依赖；复用实际执行器而非复用 host 回调集合 |
| 补员 | [runtime_staffing.py](../crew/team/runtime_staffing.py) 关注能力和运行可用性缺口 | 用统一资源决策支持硬缺口和并行/质量优化，职责、能力未知、工具可用性分开 |
| 任务存储 | [team_plan_store.py](../crew/team/team_plan_store.py) 将 TeamPlan 投影到 Kanban；`persist` / `sync_node` 在部分失败情形记录日志；[dynamickanban/store.py](../crew/dynamickanban/store.py) 已有 SQLite、owner view、图创建、CAS、runs 和事件 | 借鉴连接、owner、事务与 CAS 能力，重新定义协作仓储；不依赖“内存先变更、持久化失败后继续”作为权威链路 |
| 用户追问 | [followup.py](../crew/core/followup.py) 使用进程内 Future；[ws.py](../crew/gateway/ws.py) 接收 `followup_answer` | 复用交互传输，增加数据库中的问题和回答；进程重启后 Future 不作为唯一依据 |
| 状态展示 | [sessions.py](../crew/gateway/routers/sessions.py) 有任务视图与节点恢复接口；[桌面协作面板](../desktop/src/ui/features/team-collaboration-board.ts) 和 [网页 TaskBoard](../web/src/components/TaskBoard.tsx) 消费任务/计划投影 | 保留产品入口；改为消费同一协作投影与事件，不以 UI 推测事实 |
| 停止与退出 | [team_manager.py](../crew/team/team_manager.py) 有 `interrupt`、`destroy`、`cancel_owner` 等；[logout.py](../crew/gateway/logout.py) 依赖 owner 取消 | 新服务须显式承接取消、logout、任务句柄和 owner 隔离；不能只实现正常执行 |

上述路径属于开发位置参考，是否按文件整体删除取决于其是否还被其他功能使用。尤其 Dynamic Kanban 是独立功能，不因 Team 重构而删除。

## 3. 领域模型与字段契约

字段是实现所需的最小集合，不限制数据库列与 JSON 表达方式；所有写入和读取必须携带可信的 `owner_account_id`，由服务端鉴权上下文取得，不能只信任请求正文。

| 对象 | 最小字段 | 约束 |
| --- | --- | --- |
| TeamSnapshot | `team_id`, `version`, `name`, `goal_original?`, `members[]`, `policies`, `created_at` | 任务创建时固定快照；系统推断的目标与用户原文分开 |
| Member | `member_id`, `executor_ref`, `responsibility_original?`, `responsibility_structured?`, `source`, `required`, `excluded`, `capability_evidence[]` | 来源为 `user / confirmed_suggestion / system / unknown`；同一成员不能同时必选和排除 |
| Task | `task_id`, `owner_id`, `session_id`, `team_snapshot`, `user_goal`, `constraints[]`, `inputs[]`, `state`, `active_plan_id?`, `revision`, timestamps | session 可包含多个任务；任务明确关联 team 快照 |
| Plan | `plan_id`, `task_id`, `version`, `status`, `units[]`, `edges[]`, `reason`, `created_at` | `task_id + version` 唯一；只允许一个活动版本；有效图无环 |
| WorkUnit | `unit_id`, `plan_id`, `objective`, `inputs[]`, `expected_outputs[]`, `completion_criteria[]`, `required_tools[]`, `required_capabilities[]`, `state` | 具体、可交付；ID 在计划内唯一；工具硬需求与能力推测分开 |
| Assignment | `assignment_id`, `task_id`, `plan_id`, `unit_id`, `member_id`, `source`, `reason`, `responsibility_ref?`, `confirmation?` | 始终通过统一分配器；同一单元同一时刻只有一个生效的负责人 |
| Attempt | `attempt_id`, `task_id`, `plan_id`, `unit_id`, `assignment_id`, `executor_ref`, `state`, `request_key`, `input_refs`, `output_refs`, `error?`, timestamps | 每次重试新 ID；同一单元默认只有一个未结束的尝试 |
| Artifact | `artifact_id`, `task_id`, `unit_id`, `attempt_id`, `kind`, `ref`, `summary?`, `status` | 引用实际来源；验证状态不得由模型自报替代 |
| Decision | `decision_id`, `task_id`, `plan_id?`, `unit_id?`, `kind`, `question`, `options`, `state`, `answer?`, `applicability`, timestamps | 问题先持久化；答案与决策绑定；重复提交按 ID 幂等 |
| StaffingDecision | `id`, `task_id`, `unit_ids`, `trigger`, `reason`, `candidates`, `selected?`, `scope`, `state` | `trigger = capability_gap / parallelism / independent_review`；临时成员仅当前任务 |
| Event / Effect | `event_id`, `task_id`, `sequence`, `kind`, `payload`, `created_at`；效果另有 `effect_id`, `idempotency_key`, `status` | 事件用于追踪和投影；外部调用意图与状态同事务保存，实际调用在事务外 |

### 3.1 身份与引用

- `task_id`, `plan_id`, `unit_id`, `attempt_id`, `decision_id` 均由服务端生成，不能依赖自然语言或列表位置；历史版本的 ID 不复用给含义变化的对象。
- 用户回答只有在任务、问题和工作目标仍适用时复用；新计划应记录旧单元到新单元的关联和适用判断，否则重新请求决策。
- 调整后的工作使用新计划版本；已完成的成果可被新计划引用，但不得将旧尝试改写成新计划实际执行。
- 用户本次明确指令高于团队长期职责；团队目标、用户职责和系统建议记录来源。未知能力不是能力缺失，也不是工具已可用的证据。

## 4. 命令与状态契约

### 4.1 核心命令

| 命令 | 输入 | 成功结果 / 行为 |
| --- | --- | --- |
| `suggest_team` | owner、名称、可选目标、候选、必选/排除、职责 | 建议阵容、原因、未满足项；无可用成员返回可解释失败 |
| `save_team` | owner、用户确认的配置、预期版本 | 更新团队版本；用户原文、来源和策略保留 |
| `create_task` | owner、session、team、请求 ID、目标、输入 | 原子创建任务和团队快照；同一请求 ID 重放返回同一任务 |
| `submit_plan` | task、基于的任务 revision、工作单元和依赖 | 校验 DAG 和任务版本，再原子提交计划；版本冲突重新读取后处理 |
| `assign` | plan、unit、候选和依据 | 统一确定负责人；有实质歧义则创建 decision，不直接派发 |
| `claim_and_dispatch` | task、plan、unit、预期状态 | 原子认领单元、创建 attempt 和执行意图；事务成功后才能触发外部调用 |
| `record_attempt_result` | attempt、结果或失败、产物 | 只接受与当前尝试匹配的结果；重复结果不覆盖已确认结果 |
| `answer_decision` | owner、decision ID、答案、幂等键 | 持久化答案并标记已解决；冲突或已过期问题返回明确错误 |
| `revise_plan` | task、旧计划版本、变更和原因 | 提交新计划，标明成果和决策的复用关系；处理正在执行工作 |
| `stop_task` | task、原因 | 阻止新派发，通知可取消执行器，记录各执行结果；不假定外部副作用可撤销 |
| `recover_task` | task、恢复动作 | 根据持久化状态和外部调用可查询结果决定继续、重试或待核实 |

### 4.2 状态与转换

工作单元：`pending → ready → running → completed`；根据事件可转入 `waiting_user`、`blocked`、`failed`、`cancelled`。`failed/blocked/waiting_user → ready` 必须有明确恢复或决策；重试创建新 Attempt。已完成单元不原地重新执行，后续工作使用新版本或新单元。

Attempt：`created → dispatching → running → completed / failed / cancelled / unknown`。`unknown` 表示无法确认已发出的调用是否生效；不能自动当作失败重发带副作用的操作。

Task：`understanding → planning → running → completed`；可处于 `waiting_user / blocked / partially_completed / failed / cancelled`。可并行工作仍能推进时，任务保持 `running` 并在投影中标出等待事项。验收单独记录 `not_requested / pending / accepted / changes_requested`。

决策：`open → answered / cancelled / expired`；过期或取消后的答案不触发执行。状态变化统一由应用服务在仓储事务内校验并提交，禁止 UI 自行改写权威状态。

### 4.3 失败处理

- 规划模型故障或结构无效：有限重试，仍失败则明确报告；可提供用户选择的单成员尝试，不伪造拆分。
- 数据库提交失败：返回可重试错误，不推送“已成功”、不启动外部调用。
- 进程在调用前退出：由持久化 effect 判定是否可安全派发。
- 进程在调用中退出：先根据执行器或外部系统记录对账；无法确认时进入 `unknown`，由用户或明确策略决定后续。
- 回答重复到达：返回已保存结果；不同答案冲突时拒绝覆盖并返回当前决策状态。
- owner 不匹配、成员无权访问或资源不可用：拒绝相应操作并返回结构化错误，不跨 owner 查询或借用 provider。

## 5. 模块和适配器边界

建议在 `crew/collaboration/` 建立新包：

```text
domain/         对象、状态转换和不变量；无网关或模型依赖
formation/      候选选择、职责建议、用户约束校验
intake/         消息归属和任务创建
planning/       模型规划、工作单元和 DAG 校验
assignment/     职责匹配、统一分配和决策请求
runtime/        调度、执行尝试、补员、恢复和取消
application/    命令协调、事务边界、owner 和版本校验
storage/        协作仓储、事件和待执行意图
adapters/       provider、成员执行器、团队目录、交互和会话适配
projection/     桌面/网页共用的任务视图
```

不要求一次创建全部空文件；按阶段实现。依赖方向为网关/应用 → 协作服务 → 领域与端口；模型、数据库、Agent 的具体实现通过适配器注入。禁止新 scheduler 依赖整个旧 manager 作为 `host`。

### 5.1 现有接入需保留的行为

1. `Envelope` / `ResponseChunk`、流式输出与停止传播。
2. owner 模型选择、成员绑定模型、工具注册与权限策略。
3. 内置及外部成员执行、workspace 和会话隔离规则。
4. Team 消息、交互问题、用户回答、产物与文件变更展示。
5. 会话删除、用户退出、任务停止时的取消与资源释放。
6. 独立 Dynamic Kanban 功能继续使用其自身管理器与表。

以上为接入要求，具体实现必须逐条追踪调用方；不能因保留了接口名字就认定行为已保留。

## 6. 存储与事件方案

### 6.1 建议的第一版

在现有应用 SQLite 数据库中使用独立 `collab_*` 表，覆盖 task、plan、unit、assignment、attempt、artifact、decision、staffing、event、effect；团队配置可继续由团队目录提供，任务保存不可变快照。通过单个协作仓储控制 owner、事务、唯一性和版本检查。复用 `crew/state/sqlite.py` 连接及写辅助的可行性需在阶段 1/2 实现时确认，不能默认不同 store 的调用共享同一事务。

最小约束：`owner + task` 查询隔离；`task + plan_version` 唯一；`task + event_sequence` 唯一；请求/效果幂等键唯一；通过事务和条件更新防止重复认领。字段与索引需要随实际 SQLite 版本、并发模型和数据库位置核定。

### 6.2 提交与推送顺序

```text
校验 owner / revision / 状态
  → 同一数据库事务写状态、事件和待执行意图
  → 提交成功
  → 推送界面事件 / 异步执行外部效果
  → 保存效果结果并产生后续事件
```

推送丢失时客户端通过带 owner 的任务快照和事件序列重建视图；推送不能作为唯一数据来源。外部副作用的结果可能未知，需要对账或人工决策。

## 7. 前后端与网关契约

API 路由名称可以在实现时沿用或调整，语义必须覆盖：

- 团队：建议、创建/更新、读取，输出成员职责和来源。
- 任务：创建、获取快照、列表、补充、停止、恢复。
- 决策：列出待处理问题、提交选择；携带 `decision_id` 和幂等键。
- 事件：按 task 和序列获取或订阅；事件含 owner 范围内的 `task_id`、`sequence`、类型和相关对象 ID。
- 执行视图：返回当前目标、工作单元、计划负责人、实际执行者、尝试、产物、阻塞原因、补员和决策。

基础错误类型建议：`invalid_input`、`not_found`、`forbidden`、`version_conflict`、`planning_failed`、`no_available_member`、`decision_expired`、`storage_unavailable`、`execution_unknown`。界面展示可理解的原因和可用操作；日志保留诊断上下文，避免把模型猜测展示成确定事实。

现有桌面 `followup_answer` 可作为传输适配层；服务端必须先核对并写入对应 decision，不能只唤醒内存 Future。`ResponseChunk` 可继续承载消息，但最终视图以仓储投影为准。

## 8. 阶段交付与替换顺序

| 阶段 | 实施交付 | 阶段完成的可观察条件 |
| --- | --- | --- |
| 1 自动组队 | 候选目录适配、formation 服务、团队配置及桌面组队接入 | 只填名称能从真实可用成员组队；用户修改职责被保存；候选为空明确失败 |
| 2 规划分配 | 任务与快照、协作仓储基础、intake、统一 planner/validator/allocator | 简单任务可一个单元，复杂任务有具体拆分；统一规则解释指派；规划失败不伪造拆分 |
| 3 执行闭环 | scheduler/dispatcher、attempt/产物、状态投影及桌面主流程 | 任务真实委派给计划成员并交付，状态和调用记录一致 |
| 4 动态协作 | 双类型补员、结构化决策、计划版本、恢复、取消与对账 | 有能力缺口可补员；可并行时可提速补员；决策和失败恢复可追踪 |
| 5 产品收口 | 网页/桌面完整接入、旧 Team 入口与无用实现退役 | 新任务只由新内核执行；两端显示同一事实；独立 Dynamic Kanban 保留 |

阶段 1–4 开发中，新实现可使用受控开发入口；切换后不将旧新路径作为长期运行模式。新旧任务无需互相兼容，但绝不能对同一个任务重复调度。

## 9. 阶段 1 前需要落实的首批接口

1. 从现有 `_formation_agent_catalog` 明确 owner 可用 Agent、运行环境和工具证据的来源及失败语义。
2. 将组队输入定义为名称、可选目标、必选/排除成员、用户职责与调配策略；输出成员、职责来源、理由和不可满足项。
3. 定义无选中成员、名称含义模糊、无可用成员、必选成员不可用和用户编辑职责的产品状态。
4. 明确 `ExternalAgentStore` 团队写入可复用字段与版本处理，保留用户已填写的信息。
5. 桌面端先接入最小可用组队流程；同步确定网页端契约，阶段 5 做完整体验收口。

## 10. 验收与遗留决策

验收需同时核对界面、保存的团队配置、计划、实际委派、尝试与产物。覆盖：只有名称、无可用成员、明确职责、同义表达、简单/复杂任务、并行、两类补员、答案复用、失败恢复、owner 隔离、重复派发和双端一致。具体自动化和人工验收安排在实施阶段依据用户要求确定；本阶段未运行应用或测试。

仍需结合当前环境确定：可用 Agent 和工具的权威数据源；并发/成本默认值；SQLite 的具体事务和连接整合；各执行器对幂等、取消和对账的支持；新旧任务入口切换的具体代码位置。不能把这些尚未验证的能力写成已实现保证。
