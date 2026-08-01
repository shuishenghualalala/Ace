# Crew Skill 自进化模块

`crew.evolution` 是 Crew 的实验性 Skill 持续改进模块。它可以从会话中提取轨迹、分析已有 Skill 的使用情况，并生成优化建议或新的 Skill。

> [!IMPORTANT]
> 自动进化默认关闭。轨迹日志可能包含原始对话、thinking、工具参数与工具结果；完整进化还会修改或创建 `SKILL.md`。启用前请确认数据边界，并备份需要保护的用户 Skill。

## 默认行为

发布配置中的默认值为：

```yaml
agent:
  evolution:
    auto_trigger: false
    auto_full_cycle: false
    visible: false
```

| 配置 | 当前行为 |
|------|----------|
| `auto_trigger: false` | 对话结束后不自动运行 Evolution |
| `auto_trigger: true`、`auto_full_cycle: false`、`visible: false` | 后台异步提取轨迹，只写 Evolution 日志 |
| `auto_trigger: true`、`auto_full_cycle: true`、`visible: false` | 后台运行完整周期，可能修改已有 Skill 并创建新 Skill |
| `visible: true` | 在当前响应中同步展示三个阶段；当前实现会执行完整周期，可能修改或创建 Skill |

`visible` 当前不只是显示开关。即使 `auto_full_cycle` 为 `false`，`visible: true` 仍会进入完整的提取、优化和生成流程。

## 处理流程

```text
SessionStore
    │
    ▼
TrajectoryExtractor ──► EvolutionLogStore
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
             SkillOptimizer       SkillGenerator
                    │                   │
                    └─────────┬─────────┘
                              ▼
                       EvolutionManager
```

| 组件 | 职责 |
|------|------|
| `TrajectoryExtractor` | 从会话读取消息，生成轨迹条目、统计与结构化摘要 |
| `EvolutionLogStore` | 保存轨迹 JSON 和查询索引 |
| `SkillOptimizer` | 分析 Skill 使用记录，生成或应用优化补丁 |
| `SkillGenerator` | 聚类用户意图，生成新 Skill 或进化已有 Skill |
| `EvolutionManager` | 编排提取、优化、生成与查询接口 |
| `EvolutionQueue` | 不同会话并行、同一会话 FIFO 串行地执行后台任务 |

## 数据与写入边界

| 调用 | 是否写 Evolution 日志 | 是否可能修改 Skill |
|------|------------------------|----------------------|
| `extract_session()` / `extract_trajectories()` | 是 | 否 |
| `get_optimization_suggestions()` | 否 | 否 |
| `optimize_skill(..., dry_run=True)` | 否 | 否，只返回 patch |
| `optimize_all(dry_run=True)` | 否 | 否，只返回 patch |
| `generate_proposals()` | 读取已有日志 | **可能**；跨轮次关联分支会直接进化已有 Skill |
| `create_skill()` | 读取已有日志 | 是，创建 `SKILL.md` |
| `run_full_cycle()` | 是 | **可能**；即使使用默认参数，提案阶段也可能进入已有 Skill 的进化分支 |
| 自动完整周期 | 是 | 是，可能修改已有 Skill 并创建新 Skill |

因此，不应把 `run_full_cycle()` 或 `generate_proposals()` 当作严格只读预览接口。只想检查建议时，使用 `get_optimization_suggestions()`；只想预览补丁时，使用 `dry_run=True` 的优化接口。

## 最小用法

### 提取轨迹与预览优化

```python
from crew.evolution import EvolutionManager
from crew.state.session_store import SQLiteSessionStore

store = SQLiteSessionStore(db_path="path/to/sessions.db")
manager = EvolutionManager(session_store=store)

# 保存指定会话的轨迹日志，不修改 Skill
log_id = manager.extract_session("session-id", owner_account_id="local")

# 只分析建议
suggestions = manager.get_optimization_suggestions("my-skill")

# 生成补丁预览，不写 SKILL.md
patches = manager.optimize_skill("my-skill", dry_run=True)
```

### 显式执行完整周期

```python
report = manager.run_full_cycle(
    owner_account_id="local",
    session_id="session-id",
    dry_run_optimize=False,
    auto_create=True,
)
```

上述调用会写 Evolution 日志，并允许修改已有 Skill、创建新 Skill。返回报告包含提取数量、优化补丁、提案、已创建 Skill、跨轮次进化结果和分阶段错误。

## 轨迹数据

`TrajectoryEntry` 保留原始消息内容、thinking 和工具调用结果；摘要是 `TrajectoryLog` 上的独立字段，不会替换原始条目。

结构化摘要当前包含以下字段：

- `user_intent`
- `tools_used`
- `skills_activated`
- `operations`
- `results`
- `error_analysis`
- `evidence`

同一 `session_id + owner_account_id` 会生成稳定的 `log_id`，重复提取时更新同一条日志。

## 主要 API

| 方法 | 说明 |
|------|------|
| `extract_session(session_id, owner_account_id)` | 提取单个会话并保存日志 |
| `extract_trajectories(...)` | 批量提取会话 |
| `list_logs(...)` | 按 Skill、工具、会话或错误状态查询日志 |
| `get_skill_stats()` / `get_tool_stats()` | 汇总 Skill 与工具使用情况 |
| `get_optimization_suggestions(slug)` | 获取优化建议，不写 Skill |
| `optimize_skill(slug, dry_run)` | 预览或应用单个 Skill 的补丁 |
| `optimize_all(dry_run)` | 预览或应用所有相关 Skill 的补丁 |
| `generate_proposals(...)` | 生成新 Skill 提案；可能触发已有 Skill 进化 |
| `create_skill(proposal, ...)` | 根据提案创建 `SKILL.md` |
| `run_full_cycle(...)` | 编排提取、优化和生成 |
| `delete_log(log_id)` / `clear_all_logs()` | 删除单条或全部 Evolution 日志 |

## 存储位置

所有路径都以 `CREW_HOME` 为基准：

| 路径 | 内容 |
|------|------|
| `${CREW_HOME}/evolution/logs/{log_id}.json` | 完整轨迹日志 |
| `${CREW_HOME}/evolution/index.json` | 日志查询索引 |
| `${CREW_HOME}/evolution/clusters.json` | 聚类状态 |
| `${CREW_HOME}/skills/<slug>/SKILL.md` | 用户 Skill |

源码运行时，`CREW_HOME` 默认是仓库根目录下的 `.Crew/`；打包运行时默认是用户目录下的 `~/.Crew/`。也可以通过 `CREW_HOME` 环境变量显式覆盖。

不要提交或公开 `${CREW_HOME}`。删除日志前可使用 `list_logs()` 确认范围；`clear_all_logs()` 会清空全部 Evolution 日志。

## 运行与失败处理

- 自动后台模式按 session 维护队列：同一 session 串行，不同 session 可并行。
- 后台结果在下一轮交互时读取；没有新增或进化结果时不显示完成提示。
- 提取、优化和生成阶段分别捕获错误；单阶段失败不会阻止主对话完成。
- LLM 不可用时，部分分析会使用本地启发式降级；需要语义生成的步骤可能返回空结果。

从仓库根目录运行 `pytest` 可执行默认测试集；需要真实模型或网络的用例默认跳过。
