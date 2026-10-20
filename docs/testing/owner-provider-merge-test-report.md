# Owner 统一化 × LLM Provider 凭证库合并回归测试报告

- 日期：2026-09-03
- 测试人：AHUAMAO 会话（DSH）
- 被测版本：`refactor/plugin-architecture-stage-0` @ `9d0ac76`
- 被测范围：merge `22af708`（owner 统一化 ← `refactor/llm-provider`，LLM 厂商档案/凭证库 ← stage-0）及合并后修补链 `388efbb` / `36ff10d` / `4d6b688` / `f443f22` / `9d0ac76`
- 测试动机：两功能同步开发后合并，重点排查合并错误（此前已实际发生两起：`388efbb` key 作用域错位、`9d0ac76` 渠道归属被回卷）

## 1. 结论速览

| 层级 | 内容 | 结果 |
| --- | --- | --- |
| T0 | 静态验收（ruff、验收 grep、conftest 隔离） | ✅ 通过 |
| T1 | 合并完整性专项（重叠文件双侧意图存活） | ✅ 通过 |
| T2 | 两功能定向测试集（18 个文件） | ✅ 704 passed / 1 skipped |
| T3 | 全量 pytest vs 基线（46 条） | ✅ 44 失败 ⊆ 基线，零新增；基线内 2 条已修复 |
| T4 | 真实入口（CLI + 真实模型 e2e） | ✅ CLI 全项通过；e2e 4 通过 / 1 既有 flaky 超时 |
| T5 | 变异抽查（388efbb 修复） | ⚠️ 变异存活 → **发现 F1：该修复无回归测试守护** |

**总体判定：合并本身没有引入回归，可以交付。** 遗留 3 项建议（F1/F2/F3），见第 8 节。

## 2. T0 静态验收

- `ruff check crew/ tests` 全绿。
- 交接文档验收 grep：`owner_account_id=""` 归零 ✅；`owner_account_id: str = ""` 剩 335 处——与交接文档第三节 B"签名清扫未完成"一致，属已知遗留，不判失败。
- `crew/core/runctx.py`：`LOCAL_OWNER_ACCOUNT_ID` / `normalize_owner_account_id` / ContextVar 默认值 `"local"` 在位 ✅。
- `tests/conftest.py` 的 `CREW_*`/`GATEWAY_PORT` autouse 隔离段在位 ✅（交接文档警告不可删除）。
- 已知合法残留：`team/communication.py` 的 `or "local"` ×3（交接文档附录第 6 条已登记）、`app.py:_current_active_owner_id`（1710 行，单租约快照合法例外）。

## 3. T1 合并完整性专项

方法：对 6 个双分支重叠文件（`crew/app.py`、`crew/core/interfaces.py`、`crew/state/config.py`、`crew/tools/builtin.py`、`tests/test_external_agents.py`、`tests/test_team_tasks.py`）逐一验证合并点两侧贡献非空；再对 HEAD 做"双侧意图标记"存活检查。

| 检查项 | 结果 |
| --- | --- |
| 6 个重叠文件在 `22af708` 双侧 diff 均非空（无静默丢弃） | ✅ |
| `app.py`：provider 侧 `store_key`/`delete_stored_key`/`resolve_vendor`/凭证库 import | ✅ 存活 |
| `app.py`：owner 侧模型 CRUD 单 owner 路径 | ✅ 存活 |
| `config.py`：凭证库解析链 `credentials → api_key_env → CREW_API_KEY`（`owner_env_map`） | ✅ 存活 |
| `interfaces.py`：owner 必填签名 + provider 侧 `reasoning_mode`（厂商档案映射） | ✅ 存活 |
| `builtin.py`：owner 侧 `kill_process` owner 必填 + stage-0 侧 terminal_guard 迁移 | ✅ 存活 |
| `write_scope` 标记消失的疑点 | ✅ 查明为 `5434708` 有意重构（改为"内置模型重定向后 owner 归置为全局作用域"的等价机制，`388efbb` 修复配套），非合并丢失 |
| 渠道归属（`9d0ac76` 重放）：`{"","local","dev:dev"}` 特判删除、未绑定渠道归 local + `include_env=True`、`channel_sessions` 入站归 local、`get_binding` owner 必填 | ✅ 全部存活 |
| 后台结果 owner 字段（`subagent/tools.py` 修复） | ✅ 存活 |

## 4. T2 定向测试集

覆盖两功能各自 + 交叉的 18 个测试文件（owner 回填/进程注册表跨 owner/通知跨 owner/external/team/渠道归属组 + 厂商档案/凭证库/模型绑定/CRUD/生命周期）：

```
704 passed, 1 skipped (48.4s)
```

skip 为 `test_credentials_store.py` 的 POSIX 权限位用例在非 Windows 的预期分支。要点用例：

- `test_owner_backfill.py`：存量 `owner=''` 回填 local、PK 冲突保 local 删空行 ✅
- `test_process_registry.py`：跨 owner kill 越权回归（严格相等）✅
- `test_notifications.py`：跨 owner 同 payload 已读正/反向断言 ✅
- `test_credentials_store.py`：`test_add_two_models_keys_do_not_clobber`（最初"多模型 key 互覆"缺陷的钉死用例）、`test_owner_scope_isolated`、解析链优先级、损坏库修复、remove 清理 ✅
- `test_model_binding_providers.py`：会话绑定 → 下一轮 provider 组装、厂商 compat（thinking_format）、owner 凭证库隔离 ✅

## 5. T3 全量回归 vs 基线

命令：`.venv/bin/python -m pytest tests/ --ignore=tests/e2e -q`

```
44 failed, 3707 passed, 37 skipped, 20 deselected (177s)
```

- 与基线 `doc/baseline-failures-at-04a7e16.txt`（46 条）程序化对比（`.pytest_cache/lastfailed`）：**44 条全部在基线内，零新增** ✅。
- 基线中 `tests/test_home.py` × 2 现已通过（conftest CREW_HOME 隔离生效所致），即实际比基线还少 2 条。
- 20 条 deselected 来自 `pyproject.toml` 的 `addopts = "-m 'not e2e'"`（设计如此）。
- **lastfailed 排查记录**：缓存中另 有 53 条"基线外"条目，逐一核实为旧代码状态运行的残留——相关测试在合并基点 `04a7e16` 之前的提交（`36d9329`/`3a925a1`/`ad93ad5` 等）中已改名或移除（如 `..._from_user_boundary` → `..._from_safe_boundary`），另有 e2e 标记文件未随默认套件收集。与本次合并无关。
- 37 个 skipped 全部为环境条件类（symlink/junction ×~12、`lark-oapi` 未装 ×5、Win32 专属、native runtime 未随包等），无密钥门控安全网被静默跳过。

## 6. T4 真实入口

### 6.1 CLI 探针（临时 `CREW_HOME`，不碰真实 `.Crew`）

| 步骤 | 预期 | 结果 |
| --- | --- | --- |
| `python -m crew.cli --version` / `config show` | 真实启动、初始化 home | ✅ |
| 方案 A 布局 | `accounts/<hash("local")>/` 生成 | ✅ `accounts/acct_25bf8e1a2393f110/` |
| `config models add --api-key`（owner 自定义模型） | key 进 owner 私有库 | ✅ `accounts/acct_*/credentials.json` |
| `config models update --id default --api-key`（**388efbb 场景**：owner 更新内置模型） | key 进全局库 | ✅ 全局 `credentials.json`，owner 库无污染 |
| `config models use` | owner overlay `llm.active` 持久化 | ✅ |
| 解析链 | update 后启动从 FakeProvider 变为真实 OpenAIProvider（全局凭证解析成功） | ✅ |
| llm.jsonl owner 恒附加 | 所有记录 `owner_account_id=local` | ✅（279/279） |

探针方法说明：首轮探针中 `use` 的输出被管道截断（SIGPIPE）导致一次"active 未持久化"的假象，全新 home 完整重放 7 步序列全部正确，确认非产品问题。

### 6.2 真实模型 e2e（`scripts/run_e2e_batch.py`，报告在 `build/e2e/merge-20260903/`）

| case | 结果 | 用时 |
| --- | --- | --- |
| complex_tasks/write_file | ✅ passed | 7.8s |
| complex_tasks/team_workflow | ✅ passed | 19.2s |
| complex_tasks/dynamic_kanban | ✅ passed | 36.2s |
| wiki/wiki_retrieve | ✅ passed | 6.0s |
| tool_result_lifecycle/site_skill_survives_compaction | ❌ timeout 660s | — |

超时 case 归因：`llm.jsonl`/`crew.log` 显示 agent 全程正常推进（20 次请求、32 个工具调用、L3 全量摘要与受保护工具结果恢复机制工作正常、防抖"连续 2 次压缩省<10% 跳过"生效），为长任务撞时限。合并前历史报告（`build/e2e/20260901-*`）同 case 6 次仅 1 次通过（378s），其余均为超时/失败——**既有 flaky，非本次合并引入**。见 F3。

## 7. T5 变异抽查（388efbb 修复）

方法：临时 worktree @ `9d0ac76`，仅反向应用 `388efbb` 的 `crew/app.py` hunk（key 写入块改回原始 `owner_account_id`，保留测试），运行 4 个相关测试文件。

结果：**94 passed —— 变异存活**。全库检索确认没有任何测试覆盖"owner 更新内置模型 → key 必须落全局作用域"这条路径：`test_session_bound_vendor_model_used_next_turn` 只覆盖非内置模型进 owner 库，`test_session_binding_builtin_model_global_scope` 是手工预置全局 key 后验证解析，都不经过 `update_model` 的重定向写入。

## 8. 发现与建议

| # | 级别 | 发现 | 建议 |
| --- | --- | --- | --- |
| F1 | 中（测试盲区） | `388efbb` 修复（owner 更新内置模型 key 作用域归一）无任何回归测试守护，变异实验证明下次合并可无声复发 | 在 `test_config_crud.py` 或 `test_model_binding_providers.py` 补一条：owner `update_model` 内置模型 + `api_key` → 断言 `read_stored_key("", model_id)` 命中且 `read_stored_key(owner, model_id)` 为空 |
| F2 | 低（测试卫生） | 标准套件部分用例回退到真实仓库 `.Crew/`，运行后残留 `accounts/acct_*` 调试目录（本次已清理两轮） | 排查未显式设置 `CREW_HOME` 的用例，统一走 tmp home |
| F3 | 低（既有 flaky） | `site_skill_survives_compaction` 合并前 6 次仅 1 次通过，深思考模型上长任务易超 600s 时限 | 单独调优：提高该 case `timeout_seconds`、或降低任务规模/思考档位，与本次合并无关 |

## 9. 环境记录

- Python：仓库 `.venv`（3.12.9）；e2e key 来自 `config/.env`（未 export，由 loader 自动加载）。
- 运行器 shell 存在 `CREW_MODEL`/`CREW_BASE_URL` 泄漏：pytest 由 conftest autouse 隔离；CLI/e2e 探针前手动 unset。
- 清理动作：主 checkout `.Crew/accounts/acct_*` 删除两轮（测试套件运行会再生）；`/tmp/ace-t4*` 探针目录、变异 worktree `.worktrees/mut-test` 已删除；未提交的 `tests/test_feature_manager.py` 改动（features 热更新 wip）按约定保留未动，其用例在全量与定向跑中均通过。
