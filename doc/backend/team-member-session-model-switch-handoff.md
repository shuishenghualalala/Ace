# Team 成员 Session 级模型切换交接方案

> 更新时间：2026-08-19
> 交接范围：Team Session 内按成员切换模型
> 当前阶段：已有基础实现，尚未完成完整闭环

## 1. 接手须知

当前仓库存在其他功能的未提交修改。接手时不得执行以下操作：

- `git reset --hard`
- `git checkout -- ...`
- 覆盖或回滚无关文件
- 重新实现一套 Team 专用模型 API、Executor 或 Profile 对象

上一个已完成提交：

```text
70a4b5f fix: close team staffing partial blocking state
```

本任务必须遵守：

- 一阶段一提交；
- 每阶段开发后必须运行测试；
- 每阶段同步更新 Team 文档；
- 不新增 `EffectiveAgentProfile`；
- 不修改 `external_agent.model` 作为 Session 切换结果；
- 不因模型切换自动重组团队或静默修改当前 DAG。

## 2. 当前已经具备的能力

当前代码已经具备以下基础：

1. Team Session 保存成员级 `member_model_bindings`。
2. 已有成员级模型 API：

   ```text
   GET /api/session/{session_id}/model
   GET /api/session/{session_id}/model?member_id={member_id}

   PUT /api/session/{session_id}/model
   {
     "member_id": "...",
     "model_profile_id": "...",
     "expected_revision": 1
   }
   ```

3. 外部成员和内置 Crew 成员均可读取模型目录。
4. 已有 `expected_revision` 版本控制和 Owner 隔离。
5. Desktop 协作看板已有成员卡片模型下拉入口。
6. 已有 AgentProfile V4：`base + model_overlays`。
7. 已有 `RuntimeCapabilities.model_switch`。
8. 切换后会淘汰 Team 缓存，后续执行使用新绑定。
9. 正在执行的 Agent 对象不会被强行替换，当前执行可以继续使用旧模型。

主要代码位置：

- `crew/gateway/routers/sessions.py`
- `crew/state/team_member_model.py`
- `crew/team/team_manager.py`
- `crew/team/agent_profile.py`
- `crew/agent/external/runtime_profile.py`
- `crew/agent/external/store.py`
- `crew/team/graph_planner.py`
- `crew/team/delegate_tool.py`
- `crew/agent/executor/external.py`
- `desktop/src/ui/features/session-model.ts`
- `desktop/src/ui/features/team-collaboration-board.ts`

## 3. 最终产品语义

模型切换的作用域固定为：

```text
当前 Team Session + 当前目标成员 + 后续执行
```

切换模型后：

- 不修改 Agent 默认模型；
- 不影响其他 Team Session；
- 不改变团队成员组成；
- 不自动更换当前 DAG 负责人；
- 已经运行的节点继续使用旧模型；
- 后续新节点使用新模型；
- 只有用户明确要求重新规划时，才生成新的 WorkflowPlan revision。

## 4. 不允许的架构方向

以下方案禁止采用：

1. 新增 `EffectiveAgentProfile`。
2. 新增 Team 专用 Model Executor。
3. 新增另一套 Team 模型切换接口。
4. 通过 provider 名称判断是否支持模型切换。
5. 通过 `protocol in {acp, cli}` 推断模型切换能力。
6. 通过新建原生 Runtime Session 伪装模型切换。
7. 因为一次模型切换自动重新 Formation 或自动重规划。
8. Web 和 Desktop 各自维护不同的模型切换协议。

## 5. 第一阶段：统一模型能力和画像来源

### 目标

解决“UI 看起来能切换，但 DAG 和 Runtime 仍使用旧能力画像”的问题。

### 工作内容

#### 5.1 去掉 provider 特殊推断

Runtime 是否支持模型切换，只能来自：

```text
Runtime Probe
  → RuntimeProfile.models
  → RuntimeCapabilities.model_switch
  → API/UI 是否允许切换
```

当前 Kimi 的特殊 fallback 判断只能作为过渡，最终必须删除。需要真实验证 Kimi：

- 是否支持原生会话 resume；
- resume 后传入新模型是否真正生效；
- 是否保持原生上下文；
- 不支持时返回 `runtime_model_switch_unsupported`，不能只因为有模型目录就开放切换。

#### 5.2 统一模型画像入口

Formation、DAG 和 Runtime staffing 都继续复用：

```python
build_agent_profile(agent, model_id=selected_model_id)
```

不新增同义 Profile 对象。

#### 5.3 DAG 使用当前 Session binding

新建 DAG 时，成员模型来源必须是：

```text
member_model_bindings[agent_id].model_id
```

Graph Planner 不能只读取 Formation 时留下的静态 `TeamMemberSpec.capabilities`。

### 验收条件

- Kimi 不依赖 provider 特判；
- 不同模型能够得到不同 AgentProfile；
- 新 DAG 使用当前成员绑定模型；
- Formation、DAG、Runtime staffing 使用同一能力解析入口；
- B → A → B 不重复创建 overlay；
- 现有 Team 模型 API 测试通过。

建议提交：

```text
feat: unify team model capability resolution
```

## 6. 第二阶段：成员锁和执行快照

### 目标

解决模型切换与任务启动之间的竞态，并保证历史和 Observation 按实际模型归因。

### 工作内容

#### 6.1 成员级锁

锁的唯一键：

```text
(owner_account_id, team_session_id, member_id)
```

以下入口必须复用同一把锁：

- 模型切换；
- DAG 节点派发；
- `team_mention(assign)`；
- 用户直接 ask/mention；
- 节点恢复和改派。

#### 6.2 Planning lock

DAG 正在读取成员画像时：

- 阻止模型切换；或
- 返回 `team_planning`；或
- 在短锁内串行完成。

不能读取到一半新、一半旧的画像。

#### 6.3 原子切换流程

```text
获取 planning lock
→ 获取目标成员 lock
→ 重新检查成员状态
→ 读取当前 binding revision
→ 校验 Runtime 能力
→ 解析新模型 Profile
→ 检查当前运行状态
→ CAS 写入 binding
→ 淘汰后续 Team runtime
→ 失败则回滚
```

#### 6.4 execution snapshot

节点真正进入 `running` 时保存不可变快照：

```json
{
  "agent_id": "...",
  "member_id": "...",
  "runtime_id": "...",
  "model_id": "...",
  "model_fingerprint": "...",
  "profile_version": 4,
  "binding_revision": 3
}
```

Observation 必须使用 execution snapshot 的模型信息，不能在任务结束时重新读取当前 Session binding。

### 验收条件

- 目标成员运行时不能切换；
- 其他成员运行不阻塞目标成员切换；
- 模型切换和任务启动不存在 TOCTOU 竞态；
- 切换失败不会造成存储与内存状态不一致；
- 旧任务不会被归因到新模型；
- 重启后 binding 和 execution snapshot 可以恢复。

建议提交：

```text
feat: serialize team member model switching
```

## 7. 第三阶段：DAG 硬兼容检查

### 目标

明确模型切换是否影响当前分工和 DAG。

### 数据约定

继续使用 `TeamPlanNode.metadata`，不新增数据库表：

```json
{
  "required_capabilities": ["testing"],
  "execution_requirements": {
    "tools": true,
    "images": false,
    "min_context_window": 64000
  }
}
```

### 切换规则

#### 7.1 仅能力分数变化

允许切换：

- 成员不变；
- 节点负责人不变；
- 当前 DAG 不变；
- 下一次规划使用新画像；
- UI 可提示能力评分发生变化。

#### 7.2 丢失硬执行能力

拒绝切换：

```json
{
  "ok": false,
  "code": "pending_work_incompatible",
  "incompatible_nodes": [
    {
      "node_id": "verify",
      "title": "验证",
      "missing": ["tools"]
    }
  ]
}
```

当前 binding 和 DAG 均不能改变。

#### 7.3 当前节点正在运行

返回 `member_busy`，不写 pending，不改变当前执行。

#### 7.4 用户明确要求重新规划

只有显式 replan 才执行：

```text
切换模型
→ 新建 WorkflowPlan revision
→ 使用新模型 Profile
→ 重新做负责人能力准入
→ 保留旧 revision 审计
```

### 验收条件

- 不兼容切换被拒绝；
- 返回具体不兼容节点；
- 当前 DAG 不被静默改写；
- 语义能力变化不会自动改派；
- 用户显式 replan 才产生新 revision；
- 运行时补员仍复用统一能力覆盖入口。

建议提交：

```text
feat: validate team model switch against workflow requirements
```

## 8. 第四阶段：UI、历史和真实 Runtime 验收

### UI 边界

- Desktop：协作看板成员卡片提供模型切换；
- Team Composer：不显示“团队统一模型”；
- Web TaskBoard：不新增第二套模型协议；
- 如果 Web 需要切换，复用同一 Session Model API。

### 工作内容

1. 成员卡片展示当前模型、运行状态、是否可切换和不可切换原因。
2. 统一展示 `member_busy`、`team_planning`、Runtime 不支持、DAG 不兼容和 revision 冲突。
3. 历史消息与节点详情显示 execution snapshot 中的实际模型。
4. 对 Kimi、ACP、Claude、Codex 等 Runtime 做真实 smoke。

### 验收条件

- UI 与后端状态一致；
- 切换成功、失败、并发冲突都有明确反馈；
- 历史记录不会用当前模型覆盖旧任务模型；
- 真实 Runtime 行为与 `RuntimeCapabilities.model_switch` 一致。

建议提交：

```text
feat: complete team model switch observability
```

## 9. 测试范围

重点测试文件：

- `tests/gateway/test_gateway_api.py`
- `tests/test_external_agents.py`
- `tests/test_team_tasks.py`
- `desktop/tests/unit/team-collaboration-board.test.ts`
- `desktop/tests/unit/inspector-session-model.test.ts`

每阶段至少覆盖：

- 单成员切换；
- 多成员独立切换；
- 目标成员 busy；
- 其他成员 busy；
- Team planning 中切换；
- 旧 Session 不受影响；
- Agent 默认模型不被修改；
- Kimi 能力探测；
- 模型删除；
- 模型别名迁移；
- CAS revision 冲突；
- DAG 硬兼容失败；
- execution snapshot 归因；
- 重启恢复。

## 10. 文档更新要求

每阶段完成后更新：

- `doc/backend/modules/team.html`
- `doc/backend/team-data-model-principles.html`
- 本文件的阶段状态和验收结果

如修改 Desktop/Web UI，再同步更新：

- `docs/frontend/desktop-frontend.html`
- `docs/frontend/web-frontend.html`

`doc/` 目录被仓库忽略，提交文档时需要使用：

```bash
git add -f doc/backend/team-member-session-model-switch-handoff.md
```

## 11. 完成定义

只有以下条件全部满足，Team 成员模型切换才能标记为完成：

1. 作用域确实是 Session 级、成员级。
2. Agent 默认模型、Team 模板和其他 Session 不受影响。
3. 目标成员运行时不可切换，其他成员运行不阻塞。
4. Formation、DAG、Runtime staffing 使用统一模型画像来源。
5. 当前 DAG 不被静默修改。
6. 硬执行要求不兼容时能够提前拒绝。
7. Executor 实际使用的模型与 binding 一致。
8. NodeAttempt、历史和 Observation 都使用不可变 execution snapshot。
9. 重启、并发、模型删除、Runtime 不可用和跨 Owner 场景均通过验收。
