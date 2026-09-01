# 工具结果生命周期重构 — 测试计划

对应提交：`48d3b62 refactor(agent): modularize site capabilities and tool result lifecycle`

## 1. 改动范围回顾

| 模块 | 改动 |
|---|---|
| `crew/core/interfaces.py` | 新增 `ToolResultRetention`（TEMPORARY / RESOURCE / INSTRUCTION / IMPORTANT）与 `ToolResultPolicy`；`Tool.result_policy()` / `ToolRegistry.result_policy()` 接口，未知工具默认 IMPORTANT 保护 |
| `crew/tools/registry.py` | `FunctionTool` 支持 `result_retention` / `result_identity_fields` / `result_policy_resolver`；`Registry.result_policy()` 解析失败宁可多保留 |
| `crew/agent/compact/microcompact.py` | L1 不再按时间统一清理：`keep_recent_tools` 只计 TEMPORARY；RESOURCE/INSTRUCTION 按 identity 只留最近版本（旧版替换为 stub）；IMPORTANT 原样保留 |
| `crew/agent/compact/post_compact.py` | 完整压缩后恢复三类受保护结果：Skill 指令（≤5 条，单条 ≤20k）、重要结论（≤8 条，单条 ≤5k）、最近资源（≤N）；附件带 meta，可跨多次压缩存活 |
| `crew/agent/skills.py` | compact skill index 提示：已成功读取的 name/file_path 不要重复读取 |
| `crew/agent/loop/tool_guardrails.py` | `skill_view` 加入幂等工具白名单（不触发无进展拦截） |
| 各工具注册点 | `skill_view`=INSTRUCTION/RESOURCE 动态策略；`file_read`=TEMPORARY（旧分片由 L1 清成信息摘要，压缩后按 path 磁盘重读恢复）；`wiki_read`=RESOURCE；grep/bash/终端/浏览器等=TEMPORARY；`ask_user`、`delegate_task`、`run_agent`、`collect`、团队消息、外部 Agent、蓝图确认等=IMPORTANT |
| `crew/agent/capabilities.py` + `crew/sites/capabilities.py` | Site/Blueprint 能力画像（toolsets + skills + prompt + display），`sites.authoring` 组合 `blueprint.authoring` |
| `crew/plugins/manager.py` | 插件注册工具时可声明 `result_retention` / `result_policy_resolver` |

## 2. 测试目标（对应验收关注点）

- **G1**：Site 场景下连续调用多个 Widget / Canvas / 文件 / 终端工具后，Agent **不会重复读取同一个 Skill**（skill 指令不被临时结果挤掉，guardrail 不误拦，prompt 提示生效）。
- **G2**：**用户选择（ask_user）、同伴/团队消息、外援（external agent）、子 Agent（delegate/run_agent/collect）结论**在日常 L1 压缩和完整 L3 压缩后都不丢失。
- **G3**：文件、Wiki 页面只保留**最近版本**；旧版本有明确替换标记。
- **G4**：完整压缩后 Skill 指令能**恢复**（post-compact 附件），且跨多次压缩存活。
- **G5**：插件可声明自身工具结果生命周期；未声明的插件/未知工具按 IMPORTANT 保护（不误删）。
- **G6**：回归安全：原有压缩管线（L1/L2/L3、断路器、anti-thrash、owner 隔离）行为不回退。

## 3. 分层方案

### Tier 1 — 单元测试（commit 内已新增，基线复跑）

- `tests/test_compact.py`（52）、`tests/test_capability_profiles.py`（8）、`tests/test_agent_loop.py`（guardrail 幂等）、`tests/test_plugins.py`（插件生命周期声明透传）。
- 环境注意：项目要求 Python ≥ 3.11（`crew/tools/policy.py` 用了 `StrEnum`），必须用 `.venv/bin/python` 跑，系统 conda 的 3.10 会在收集阶段 ImportError。
- **状态**：已复跑，`126 passed`。

### Tier 2 — 补充单测（如 Tier 3 暴露 gap 再补；候选清单）

- B1：`micro_compact` 幂等性——`RESOURCE_REPLACED_STUB` / `INSTRUCTION_REPLACED_STUB` 二次压缩不再变化。
- B2：~~`file_read` identity 含 offset/limit——同路径不同分片视为不同资源~~（2026-09-01 起 file_read 改 TEMPORARY，分片按 path 合并，见 4.2）。
- B3：`skill_view` 策略边界——`name` 大小写混合、`file_path` 反斜杠（Windows）归一化后 identity 一致（跨平台要求）。
- B4：post-compact 附件总字符上限 `_MAX_TOTAL_ATTACHMENT_CHARS` 截断后 meta 仍合法、可被下一轮解析。

### Tier 3 — 集成脚本验证（real entry path，无 LLM，CI 可跑）

用**真实** `Registry` + 真实工具注册函数（`register_file_tools` / `register_skills_tools` / sites capabilities）+ 真实 `ContextCompactor(result_policy_resolver=registry.result_policy)` 组装，构造 Site 会话消息序列驱动压缩，断言从消息列表**外部**读取（验证世界而非自报）。

| 编号 | 场景 | 构造 | 断言 |
|---|---|---|---|
| C1 | Skill 不被临时结果挤掉（G1） | `skill_view(webapp-building)` 成功后接 10+ 条 TEMPORARY 结果（grep/bash/浏览器） | L1 后 skill 指令原文仍在；仅最旧临时结果变摘要/占位符 |
| C2 | 重复 skill_view 去重（G1/G3） | 同一 skill 读取两次（中间隔临时调用） | 旧版本 = `INSTRUCTION_REPLACED_STUB`，最新版完整保留 |
| C3 | file_read 旧分片清理（G3） | `file_read` 对多文件/多 offset 分片各读一次 | 旧分片 = 含 path/offset 的信息摘要（`[已压缩工具摘要] [file_read] ...`），最近 N 条保留原文；分片不再各自积累 |
| C4 | Wiki 页面去重（G3） | `wiki_read` 同 page_id 两次、不同页面各一次 | 同页只留最新；不同页面互不干扰 |
| C5 | 重要结论日常不压（G2） | `ask_user` 答案 + `delegate_task` 结论 + 团队消息 + external agent 结果，后接 10+ 条临时调用 | L1 后四条重要结果全部原文保留 |
| C6 | 完整压缩恢复（G2/G4） | C5 基础上触发 L3 全量压缩（mock provider 或直接调 L1+摘要路径） | 上下文出现 `【压缩后保留的工具结果】` 附件：含 skill 指令（INSTRUCTION meta）、用户选择与子 Agent 结论（IMPORTANT meta）、最近文件（RESOURCE meta） |
| C7 | 附件跨多次压缩存活（G4） | C6 之后再积累新消息，二次完整压缩 | 第二轮附件仍包含首轮的 skill 指令与重要结论（meta 解析存活） |
| C8 | guardrail 防循环（G1） | 连续重复 `skill_view` 同名 | 幂等白名单不误拦正常重读；连续无进展重复达到阈值时给出停止提示（不崩溃、不死循环） |
| C9 | 插件生命周期（G5） | 插件注册未声明工具 + 声明 temporary 工具 | 未声明 → L1 原样保留；声明 temporary → 可被清理；已卸载插件的历史结果 → 按 IMPORTANT 保留 |
| C10 | Site 能力画像（G1 前置） | resolve `sites.authoring` | 组合出 blueprint toolsets + webapp-building/widget/canvas skills + prompt + display badge；无环；不越权 |

### Tier 4 — 真实 e2e（需要模型密钥，人工/定时执行）

真实跑一个 Site 任务（"创建一个灵感 App，用到至少 2 个 Widget、1 个 Canvas，中途我会做一次选择"），把 `compaction_token_budget` 调低（如 8k）迫使压缩提前触发：

- D1：统计会话日志中 `skill_view` 调用次数——同一 skill 名只应出现 1 次（允许内容变更后的合理重读）。
- D2：压缩触发后，Agent 后续 Widget/Canvas 操作仍遵循 webapp-building 的资产链规则（说明指令存活）。
- D3：中途 `ask_user` 的选择，在压缩后的最终产物中被遵守（外部检查产物文件，而非 Agent 自述）。
- D4：若用到 `delegate_task`/`run_agent`，其结论在压缩后仍被最终回答引用。

## 4. 执行顺序与状态

1. ✅ Tier 1 基线复跑（126 passed）。
2. ✅ Tier 3 集成脚本：`tests/test_tool_result_lifecycle.py`（13 个用例，全部通过），
   用真实注册表（builtin + blueprint + wiki + subagent + 团队 + 外援）+ 真实
   `registry.result_policy` + 真实 `ContextCompactor` 驱动：
   - C1 Skill 指令在 Widget/Canvas/终端洪流后原文保留；C2 重复 skill_view 只留最新
     （大小写 / Windows 反斜杠归一）；C3 file_read 按版本与分片去重；C4 wiki_read 按页面去重；
   - C5 用户选择 / 团队消息 / 外援 / 子 Agent 结论日常不压；C6 完整压缩后四类受保护
     结果全部恢复，且重要结果超 8 条预算时按最近优先；C7 附件跨多次完整压缩存活；
   - C8 guardrail 第三次同参重复读取拦截、换参 / 内容变化不拦；C9 未声明与已卸载
     插件工具按重要保护、声明 temporary 可被清理；C10 Site 工具面声明全量审计。
   - 回归：`test_compact` / `test_capability_profiles` / `test_agent_loop` /
     `test_plugins` / `test_subagent` / `gateway/test_wiki_router` 共 240 passed、
     2 skipped（均为缺可选依赖的既有跳过）。
   - 构造要点：压缩切分接受 user/assistant 安全边界（`_safe_split` 不落在
     tool 消息上切断配对）；2026-09-01 之前仅接受 user 边界，旧用例在工具
     调用对之间穿插了 user 消息，该构造仍然兼容。
3. Tier 2 按需补充（Tier 3 已覆盖 B2/B3，B1/B4 暂未发现 gap）。
4. ✅ Tier 4 真实 e2e：`tests/e2e/scenarios.yaml` 新增 `tool_result_lifecycle` 类
   `site_skill_survives_compaction`，真实模型（deepseek-v4-flash）3 轮 Site 会话通过
   （`build/e2e/20260901-031554/`）：
   - 38 次工具调用（Canvas×9 / Widget×7 / 文件×9 / 终端×2 / skill_view×7），
     7 个 `(name, file_path)` 键**零重复**——无重读循环（D1 ✅）；
   - 完整压缩真实触发（78 条旧消息→摘要），压缩视图含 **13 个恢复附件**
     （5 条 Skill 指令：webapp-building/blueprint/canvas/widget/widgetdesign，
     3 条资源、5 条重要结论）（D2 ✅）；
   - 产物文件齐备（D3 的文件面 ✅；ask_user 交互与 delegate 子 Agent 由 Tier 3
     C5/C6 覆盖，e2e 不含交互式提问）。
   - 运行方式：`python scripts/run_e2e_batch.py --category tool_result_lifecycle`
     （需要密钥，无密钥自动 skip；模型走 `config/.env`，load_config 自动加载）。
   - 配套 harness 扩展（`tests/e2e/_run_case.py`）：`followups` 多轮会话、
     `max_skill_view_per_name`（按 name+file_path 去重）、`expect_compaction_recovery`
     （从 case 目录 llm.jsonl 真实请求观测压缩视图）。

## 4.1 Tier 4 过程发现

- **单用户回合内 L3 已修复（2026-09-01）**：原 `_safe_split` 只接受 user 边界，
  单个长回合（一次 prompt + 几十次工具迭代）里 old 段恒为 0/1 条，回合内
  只能靠 provider 报错后的 `force_compact` 兜底。现已接受 assistant 安全边界，
  回合内早期迭代可被 L3 摘要（`test_compact_view_compacts_within_single_long_turn`）。
- **防重读的观测口径**：判断"重复读取"必须按 `(name, file_path)` 对——同一
  Skill 的不同引用文件是不同资源（widgetdesign 的 12 份 references 是合法读取）。
- **llm trace 路径**：`llm.jsonl` 写在主日志同目录（`state/logging._setup_llm_trace`），
  不是 `crew_home/logs/`。E2E 的密钥解析链路与 trace 路径细节见
  `docs/testing/e2e-testing.md` 的 2.1 / 4.1 节。

## 4.2 2026-09-01 压缩体系修订

对照 Open-Claude-Code（压缩无回合边界概念、按 path 重读文件恢复）与 codex
（工具输出压缩即蒸发、模型自行重读）的方案，本轮修订：

- `_safe_split` 接受 assistant 边界 → 回合内 L3 可触发（见 4.1）。
- `file_read` 由 RESOURCE（path+offset+limit 分片 identity）改 TEMPORARY：
  旧分片由 L1 清成一行信息摘要；压缩后恢复按 path 去重、从磁盘**重读最新
  内容**（`read_verified_bytes` 身份校验句柄，防 TOCTOU），不再回放旧快照，
  分片不再各自占用恢复预算。
- post-compact 恢复预算（指令 5 条×20k、重要 8 条×5k、总量 140k）从
  `post_compact.py` 常量移入 `agent.compaction.post_compact_*` 配置。
- 摘要消息尾部附 canonical 历史回溯指引（会话数据库路径 + session_id）。

5. 收尾：`ace-pre-push-checks` 选最小检查集；本计划按模块归入 `docs/testing/`。

## 5. 风险与边界

- post-compact 恢复预算（指令 5 条×20k、重要 8 条×5k、总量 140k，均可经
  `agent.compaction.post_compact_*` 配置）：**超过上限的重要结果会被丢弃**——C6 需覆盖"重要结果 >8 条"时按最近优先保留的行为是否符合预期。
- ~~`file_read` identity 含 offset/limit：同一文件的不同分片不会被去重~~——已于 2026-09-01 解决：file_read 改 TEMPORARY，分片按 path 合并、恢复时磁盘重读（见 4.2）。
- `max_tool_result_chars` 截断对所有 retention 生效（包括 IMPORTANT 的单条超长结果），与 post-compact 单条上限是两层截断，注意断言别混淆。
- guardrail 白名单让 `skill_view` 重读不被拦，防循环依赖 skill index prompt（模型行为）+ 无进展重复阈值（规则兜底），e2e 才能完整验证，单测只能验证规则半边。
