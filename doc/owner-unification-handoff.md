# Owner 统一化改造 · 交接文档

> 交接人：AHUAMAO 会话（Claude/DSH）
> 日期：2026-09-02
> 分支：**`refactor/llm-provider`**（worktree：`/Users/ahuamao/Documents/Codes/Ace/.worktrees/llm-provider`）
> 基点：`04a7e16`（= master 分支 `refactor/plugin-architecture-stage-0` 的最新提交）
> 当前 HEAD：`5434708`（工作树干净，无未提交改动）

---

## 一、任务目标与最终架构（一句话版）

**彻底删除"非 owner（空 `owner_account_id=""`）"这条历史遗留路径**：空 owner 不再是
一种"模式"，而是被归一为本机 owner `"local"` 的特例。归一化只发生在系统边界，
系统内部 owner 恒非空、无需任何防御分支。

### 用户已拍板的决策（不可推翻，除非 AHUAMAO 亲自改口）

| # | 决策 | 结论 |
|---|---|---|
| ① | 目录布局 | **方案 A**：所有 owner（含 local）统一住 `{CREW_HOME}/accounts/<hash>/`；基础 CREW_HOME 退役为安装级目录（config.yaml、日志、skills 模板） |
| ② | `agent/skills.py` 的伪 owner `"system"` | 并入 `local`（该字段仅审计打标，非存储路径） |
| ③ | key 的跨重启解析机制 | **key 只存 owner overlay 对应的 `.env`（accounts/<hash>/.env），不同步进程环境**；启动期/运行期经 owner 视图 env_map 解析（独立测试探针已验证跨重启可解析）。⚠️ 7121eb0 提交信息与本节初版描述有误，以此为准 |
| ④ | `tools/blueprint_tools.py`、`site_tools.py` 的 fail-closed raise | **保留 raise**（它们是防御不是空路径） |
| ⑤ | `work/service.py` 钩子空跳 | 归一后**照常执行**（静默跳过掩盖漏传 bug） |
| ⑥ | 无绑定渠道入站消息 | **归 local**（拒绝会断渠道消息）；平台原始 uid 严禁充当 owner |
| ⑦ | 渠道 `include_env` | 未绑定（现归 local）渠道**保留 env 凭证启用**；显式绑定 owner 的渠道 False |
| ⑧ | `internal_accounts` 配置时 local 是否强制 internal | **是**（否则本机用户被降权 external）——已实现（`9d9eda3`，local 无条件优先于白名单与 requested 兼容路径）|
| ⑨ | 存量 `''` 行 | 回填 `local`（已实现）；PK 冲突保 local 删空行 |

### 代码中的终极规则（验收标准）

1. `crew/core/runctx.py` 的 `current_owner_account_id` ContextVar **默认值 = `"local"`**；
2. 归一化原语 `normalize_owner_account_id()` / 常量 `LOCAL_OWNER_ACCOUNT_ID` 只允许出现在
   **系统边界**：gateway 认证、CLI 入口、4 个 ContextVar set 点（dispatcher:486 附近 /
   agent runtime 两处 / cron scheduler）、store 创建入口与存量回填；
3. 系统内部不允许出现 `if not owner` / `or "local"` 之类的防御（读取方信任 owner 恒非空）；
4. 公开函数签名一律 `owner_account_id: str` **必填无默认**；
5. 验收 grep（应全部归零）：
   ```sh
   grep -rn 'owner_account_id: str = ""' crew --include='*.py' | wc -l   # 目标 0
   grep -rn 'owner_account_id=""' crew --include='*.py' | wc -l          # 目标 0
   ```
   例外（允许保留）：`owner_account_id: str | None = None` 且函数体内是
   `None → 读 ContextVar` 的**上下文继承**模式（如 `wiki/manager.py:_key`、
   `plan/manager.py:_key`、`compact/pipeline.py:_key`），None 不是空语义；
   以及 dataclass/Protocol 字段确需默认值的（评审时逐个看）。

---

## 二、已完成的提交（按顺序，均在 `refactor/llm-provider`）

| 提交 | 内容摘要 | 关键文件 |
|---|---|---|
| `7333188` PR1 | 入口归一原语 + 四个 ContextVar set 点 + 各入口读取点归一 | runctx/auth/dispatcher/scheduler/runtime/cli/memory/wiki/cron/builtin/browser/process_registry/logging |
| `5444a9c` PR2 | **存量数据回填**：`backfill_empty_owner_rows`（UPDATE OR IGNORE + 冲突删空行）；主库 9 表 + memory/channel_bindings/plugin_preferences/sites/external 就地回填；cron 智能归属优先；DK 孤儿归 local、歧义行保留隔离 | `_migration.py`、各 store `_init_schema` |
| `c13dc49` 步骤1 | **ContextVar 默认值改 `"local"`**；拆除 PR1 撒在读取点的归一脚手架；logging 归属恒附加 | runctx + 8 个读取点文件 |
| `56976a9` 步骤2·一 | 分支删除第一批：team 双查桥、active_children 全 owner 放行、process_registry 严格相等（**修掉跨 owner kill 越权**）、notifications 恒加 owner 过滤（**修掉跨 owner 已读**）、browser capability 族、DK 六处 warn-no-op、audit purge 拆分、interaction_bridge 回退、skills "system"→local（②）、work 钩子（⑤） | team_manager/team_plan_store/process_registry/notifications/browser/manager/audit/interaction_bridge/skills/work |
| `ab0d596` 步骤2·二 | **渠道归属统一（3B）**：未绑定渠道注册归 local（include_env 显式化⑦）；channel_manager/delivery 的 `{"","local","dev:dev"}` 特判删除；channel_sessions 无绑定入站归 local（⑥）；broadcast None 兜底；dispatcher session_end 归一；channel_bindings get_binding 必填删首条兼容视图；external store 24 处签名收紧 | gateway/app.py、channel_manager、delivery、channel_sessions、broadcast、dispatcher、channel_bindings、agent/external/store |
| `7121eb0` 步骤3 | **模型 CRUD 显式 write_scope**：update/remove 的 `builtin → 写全局层`（不做会静默丢内置模型编辑！）；use_model/add_model 单 owner 路径（写 overlay，不再重建全局 Provider）；`_invalidate_all_owner_team_providers` 拆分；`_apply_api_key_to_env` local 同步进程 env（③） | app.py |
| `040f271` 步骤4·部分 | 签名清扫第一批：core/interfaces + mocks（Session/Workspace/Notification store 协议）owner 必填；conftest 运行器环境隔离；subagent 后台结果补 owner 字段（修生产自动注入错位）；mcp/dispatcher 调用点 | interfaces.py、mocks.py、conftest.py、mcp_server、dispatcher |

### 各层现在的形态

- **原语**（`crew/core/runctx.py`）：`LOCAL_OWNER_ACCOUNT_ID = "local"`、
  `normalize_owner_account_id(value) -> str`（空/None → local，其余 strip 原样）。
  gateway/auth.py 的同名常量是下沉后的再导出。
- **已必填的模块**：team_manager、team_plan_store、process_registry、notifications、
  browser/manager（capability 族）、dynamickanban/manager、agent/external/store、
  core/interfaces、core/mocks、app.py 模型 CRUD 四方法。
- **数据迁移**（PR2，永久有效）：启动时 `inspect_and_backfill_legacy_owners` 自动把
  `owner=''` 行归 local；10+ 张表都有就地回填；`claim-legacy` CLI 保留用于人工认领
  歧义行。

---

## 三、剩余工作（按优先级）

### A. 修复 12 个测试失败（全是已知修法的"重新应用"，预计 1-2 小时）

> 独立测试修正：原稿写 13 个，其中 `test_channel_config_api.py::
test_operation_rejects_during_reconnect[save-config]` 实测是绿的（该条修法的
前提 "record_error 已必填" 不成立——它仍在剩余 336 处清单里），跳过。

> 这些修复在会话中做过一次，因基线对比时的 `git checkout <sha> -- .` 误操作被回卷。
> 每条都验证过独立跑绿。全量基线失败清单见 `doc/baseline-failures-at-04a7e16.txt`
> （46 条），修完后 `git diff` 该文件与当前失败列表应为空集。

1. **`tests/gateway/test_internal_interaction_auth.py`**（2 个）
   - `test_interaction_binding_requires_owner`：`create_binding` 加 `owner_account_id=""`
     → 断言仍返回 None（空串被拒）；
   - `test_interaction_binding_inherits_runtime_owner_context`：加
     `owner_account_id="A:uid-a"`（ContextVar 回退已删，executor 现在显式传）。

2. **`tests/test_config_crud.py::test_resolve_writable_env_path_returns_under_crew_home`**
   - 断言改为方案 A 布局：
     `p = resolve_writable_env_path("local")`，期望
     `home / "accounts" / owner_path_segment("local") / ".env"`。

3. **`tests/test_model_capability_review.py::test_explicit_no_tools_subagent_has_empty_tool_filter`**
   - 函数签名加 `monkeypatch: pytest.MonkeyPatch`；`monkeypatch.setenv("KEY_PLAIN", "k-plain")`；
     `_profile("plain", capabilities=["text"], builtin=True)`（子 agent 走 owner 视图，
     全局层模型须 builtin 且 key 从 env 解析）。

4. **`tests/test_state_memory.py::test_set_status_and_not_overwritten_by_save`**
   - 三处调用全部补 `owner_account_id="local"`：两次 `store.save(...)`、
     `store.set_status(...)`（已有）、`store.get_status(...)`、`store.list_sessions(...)`。

5. **`tests/test_task_runtime.py`**（3 个）
   - 所有 `runtime.create_runtime(` 调用补 `owner_account_id="local"`（⚠️ 多行调用注意
     不要与行内已有 owner 重复——曾有教训）；
   - `runtime.wait/get/finish` 补 `owner_account_id="local"`；
   - `runtime.finish(... owner_account_id="", ...)` 改 `"local"`；
   - `mark_running` / `touch_activity` **不接受 owner 参数，不要加**。

6. **`tests/test_session_teardown_cleanup.py`**（2 个）
   - 三处 `save_acp_session_binding(`（约 54/62/127 行）补 `owner_account_id="local"`。

7. **`tests/test_plan_mode.py`**（2 个）
   - `_make_subagent` 能力解析走 owner 视图：测试里 cfg 的模型 profile 需
     `builtin=True` 且设对应 `KEY_*` env（同第 3 条修法）。

8. **`tests/test_context.py::test_complete_path`**
   - 同 `test_context_complete.py` 夹具修法：路径 base 用
     `get_owner_runtime_home("local")`（先 setenv CREW_HOME 再取，见下条教训）。



### B. 完成步骤 4 签名清扫（✅ 已完成，2026-09-03）

清扫已全部落地，crew 内 `owner_account_id: str = ""` 从 335 处归零，仅存
1 处**有意保留的合法例外**：`tools/process_registry.py:156` 的 dataclass
字段（checkpoint 兼容，行内注释已说明）。分批提交：

| 提交 | 批次 | 内容 |
|---|---|---|
| `44bc2cd` | 第二批 | team 模块 55+8 处；communication 删除 3 处 `or "local"` 尾兜底 |
| `eaea43b` | 第三批 | wiki 模块（store 协议/_filesystem/compiler 等 107 处） |
| `97b7b29` | 第四批 | state 存储层 37 处 + interfaces 协议补 save/list_sessions owner 声明 |
| `fd1310a` | 第五批 | tasks/cron/evolution/gateway/work 88 处 |
| `de9c9f9` | 收官 | agent/runtime 3 处 |

经验教训（后续同类清扫必读）：

1. 混合默认参数签名（owner 前面有带默认值的参数）无法直接去默认——按调用方
   传参风格选择 `*, owner_account_id: str`（kw-only）或重排参数；dispatcher
   的 stop/interrupt 与 tasks 的 wait/cancel 均采用 kw-only。
2. 看门狗/后台循环（如 `_monitor_loop`）内部的 TypeError 会被后台任务静默
   吞掉，表现为测试挂死而非报错——内部调用点必须人工逐一排查，pytest 的
   TypeError 清单不含它们。
3. ast 的 end_col_offset 是 UTF-8 字节偏移，中文行按字符切片会错位；批量
   改写用 `line.encode('utf-8')` 后按字节切。
4. 连接注册表/投递与迁移测试的 owner 语义是 `""`（全局注册表/故意构造遗留
   行），不是 `"local"`——批量插参前先核对每处测试的语义。

验收命令（当前仅剩合法例外 1 处）：

```sh
grep -rn 'owner_account_id: str = ""' crew --include='*.py'
# 仅剩 tools/process_registry.py:156（dataclass 字段，注释说明保留）
```

### C. 两个"合法例外"要显式标注（勿删）

- `app.py::_current_active_owner_id`（约 1712 行）：ContextVar 空→单租约快照，
  是启动/无请求上下文唯一合法的空 owner 消费者（审计结论：保留并注释）；
- `_migrate_workflow_ownership`（dk store）的 `legacy_ambiguous` 隔离行：
  多 owner 歧义，留给 `python -m crew.cli migrate claim-legacy --account <owner>` 人工认领。

### D. 收尾三件套

1. **文档**（按 AGENTS 规则，本功能属大更新）：
   - `docs/backend/modules/providers.html`：补"多模型 API Key 隔离/派生 env 变量"小节
     （若做 llm-provider 的 key 修复）与 owner 归属说明；
   - `docs/backend/modules/state.html`：owner 数据布局（方案 A 目录图）、六步回填表；
   - `docs/backend/modules/gateway.html`：渠道归属统一、入站绑定语义（决策⑥⑦）；
   - 建议新增 owner 架构总览（原语、边界、合法例外清单）。
2. **验收**：
   - 上面两条 grep 归零；
   - `pytest tests/ --ignore=tests/e2e` 全量，失败集合 ⊆ 基线集合
     （`doc/baseline-failures-at-04a7e16.txt`，46 条，多为沙箱/环境类）；
   - `ruff check crew/ tests` 全绿。
3. **可选后续**：llm-provider 分支最初的任务——多模型 apikey 隔离
   （派生专属 `api_key_env`，owner 统一后只剩单路径，改动很小，见第一次会话方案）。

---

## 四、环境与坑（务必读）

1. **Python 环境**：worktree 没有 `.venv`。用主 checkout 的：
   `/Users/ahuamao/Documents/Codes/Ace/.venv/bin/python`（3.12.9，含全部依赖；
   cwd 在 worktree 时 `crew` 正确解析到 worktree）。
2. **运行器 shell 会泄 CREW_***：本会话环境有 `CREW_MODEL=deepseek-v4-flash`、
   `CREW_BASE_URL=...` 等。已加 `tests/conftest.py` 的 autouse 隔离（setup 时 pop
   全部 `CREW_*/GATEWAY_PORT`，teardown 还原）。**不要删这段**。
3. **worktree 的 `.Crew/` 已被测试调试污染**（存在 `accounts/acct_25bf8e1a2393f110/
   config.yaml`，其 overlay 写了 `active: next`、models 空）。这是 local owner 的
   真实 overlay——它会让"本机开发跑真实网关"的默认模型变成 next（不存在）。
   处置：直接删 `.Crew/accounts/acct_25bf8e1a2393f110/` 或手工改回即可。
4. **基线即有的失败**（46 条，见 `doc/baseline-failures-at-04a7e16.txt`）：
   - 6 个 `test_cron` PermissionError + `test_cli::test_security_sandbox_run` +
     `test_execution_routing` 2 个 Seatbelt——都是本会话沙箱拦文件写/沙箱执行；
   - 其余为 branch 基点真实的旧失败（archive_pin 423、browser schema 等）。
   修测试时**先 `git stash` 或对照基线**确认归属，别把旧账算到新改动头上。
5. **`git checkout <sha> -- .` 陷阱**：脏工作树时执行会把全部文件卷回目标提交版本
   （本次会话两次踩坑：一次丢 external sweep、一次丢 interaction_auth 修复）。
   对比基线请用 `git worktree add /tmp/xxx <sha>` 独立目录。
6. **`_owner_matches` / `mark_read_by_payload` 已严格化**：任何"空 owner 放行"的新
   代码都是回归，评审时见到 `not owner or` 直接打回。
7. **f-string 嵌套引号**：仓库 ruff 目标含 py3.11 兼容，f-string 内不要复用外层引号
   （test_process_registry 踩过）。
8. **`update_model`/`remove_model` 的 `write_scope="global"` 分支是故意的**：内置模型
   归共享 config.yaml，`persist_owner_model_profiles` 不序列化 builtin——若有人
   提议"删掉全局分支"，那是会静默丢内置模型编辑的 bug，打回。

---

## 五、本次会话修复的真实产品缺陷（供 review 重点关注）

1. `tools/process_registry.py::_owner_matches`：空 owner 放行一切会话
   → `kill_process` 可跨 owner 杀进程（已改严格相等）；
2. `notifications/store.py::mark_read_by_payload`：空 owner 跨 owner 匹配已读
   （已改恒加过滤）；
3. `agent/subagent/tools.py` 后台结果缺 `owner_account_id` → 自动注入队列
   owner 错位（已补 result 字段 + 回调入参归一）；
4. llm-provider 分支的初始 bug（多模型 key 互覆）根因即本 campaign 的子集，
   CRUD 单路径后派生专属 `api_key_env` 即可根治（见"遗留事项"）。

## 六、遗留事项（本 campaign 未覆盖，建议后续排期）

- **llm-provider apikey 修复本体**（派生专属 env 变量名 + update 保留原 key）——
  owner 统一后改动集中在 `app.py` 单路径，很小；
- 文件系统层的 wiki 旧全局根（`wiki_lib/`、`wiki_sessions/legacy/`）向
  `accounts/<hash("local")>/` 的磁盘搬迁（PR2 只迁了 SQLite；方案 A 的完整落地方向，
  涉及磁盘迁移脚本 + Windows 验证）；
- `runctx` 之外的防御清理复查：全库 `grep -rn 'or "local"' crew` 应只剩边界调用；
- `docs/backend/modules/*.html` 模块文档更新（见 D）。

---

## 附：独立测试报告（AHUAMAO）后的修正记录

独立测试（被测 `d158b82`，只测不改）结论：**交付基本属实，全量失败集
56 = 46 基线 − 2 + 12，零基线外新失败；探针 19/19 通过**。据此本日追加：

1. **偏差①（中）已修**：交接文档"13 个测试修复"修正为 **12 个**，删除
   `test_channel_config_api` 条目（实测绿，且其前提 `record_error` 必填
   不成立——`channel_manager.py` 的 `record_error` 仍在剩余 336 清单中）。
2. **偏差②（低）已定案**：决策③ 机制以实测为准——`_apply_api_key_to_env`
   现行为 `sync_process_env=not bool(owner)`（local 亦不同步进程环境），
   key 跨重启经 owner 视图 env_map 解析（探针验证通过）。**接手人不要给
   local 加回 `sync_process_env=True`**（7121eb0 提交信息描述不准，以本节为准）。
3. **回归盲区已补**：`test_notifications.py::test_store_mark_read_by_payload`
   增加跨 owner 同 payload 的正/反向断言（变异测试验证：旧实现下必失败）。
   签名清扫继续动 notifications 前先跑它。
4. **代码瑕疵已修**：`agent/subagent/tools.py` 重复赋值行；
   `state/config.py` `env_home` 死赋值（保留 strip 版本，修掉未 strip 的
   空白值构造怪路径问题）。
5. **环境清理已批准**：worktree `.Crew/accounts/` 的 9 个调试账号目录
   （不止早前点名的 1 个）全部可删，由独立测试方执行。
6. **防御残留新基线**（文档"遗留事项"补充）：`or "local"` ×3
   （`team/communication.py`）、`if not owner` ×59（含决策④合法 raise，
   逐个甄别）、`subagent/tools.py:852` 的 `or ""` 兜底——签名清扫时一并处理。

---

## 七、快速上手命令

```sh
cd /Users/ahuamao/Documents/Codes/Ace/.worktrees/llm-provider
git log --oneline -8                          # 看提交链
PY=/Users/ahuamao/Documents/Codes/Ace/.venv/bin/python
$PY -m pytest tests/ -q --ignore=tests/e2e    # 全量（~8 分钟）
$PY -m pytest tests/test_owner_backfill.py -q # 迁移语义速查
grep -rn 'owner_account_id: str = ""' crew --include='*.py' | wc -l   # 验收指标
```
