# Ace Codex 安全能力并行整改计划

## Overview

本计划把
[`codex-security-capability-baseline.md`](codex-security-capability-baseline.md)
中的原子能力 ID 转换为可派发给子智能体的工作任务。

基线当前逐行状态为：

- 唯一原子项：436
- `符合`：102
- `不全面`：274
- `欠缺`：60
- 本计划覆盖全部 334 个非 `符合` ID，任务之间不重复、不遗漏。

ID 是验收粒度，不是开发粒度。每个任务按共享类型、强制边界和模块所有权合并；完成任务后，仍需逐个 ID 对照验收，不能因为一个共享模块修改成功就自动把所有关联 ID 标成 `符合`。

## Execution status

> 最后更新：2026-08-17

- `T01` 已完成实现并通过 `python -m pytest -q tests/security/test_product_boundary_absent.py`（4 passed）。68 个 ID 已写入机器可验证的 `N/A`、`ACE_EQUIV` 或 `APPLICABLE` 处置；执行面库存的完整联跑已由 T15 补齐 `electron-process-callsites.json` artifact。
- `T02` 已完成实现并通过主智能体复跑的四组 focused tests（101 passed）；`git diff --check` 通过。当前仍未更新基线状态，后续需要业务调用链接入和全量回归。
- `T03` 已完成并通过主智能体验收：专属文件测试 `80 passed, 11 skipped`，附件/归档/文件面测试 `33 passed, 3 skipped`，相关 `compileall` 通过。
- `T04` 遗留 resource assertion 已修正，T04 相关回归 `235 passed, 1 skipped`；artifact refresh 后 source-stale 已为 false。`T05` 已通过主验收：Python 网络测试 `149 passed`，`cargo test --all-targets` 通过（installed-fixture 条件跳过项保留）。`T06` 已通过主验收：Secrets/MCP focused tests `58 passed, 1 skipped`，`git diff --check` 通过。`T07` 已完成：focused `75 passed`，logging `15 passed`，lifecycle/checkpoint `2 passed`，runtime diagnostics `7 passed`；`T08` 已完成：Gateway focused `64 passed, 1 skipped`，compile/Ruff/diff check 通过；`T09` 已完成：Gateway focused `122 passed`，Desktop availability `7 passed`，typecheck/build:dev 通过；`T10` 已完成：focused `46 passed, 1 skipped`，compile/diff check 通过；`T11` 已完成 Linux owner 改动并通过 `linux_bwrap_plan`（19 passed），真实 Linux runner 证据保留给 T15；`T13` 已完成 Windows owner 改动，native release `150 passed, 2 ignored`，真实 elevated/install runner 证据保留给 T15；`T12` 已核对现有 macOS Seatbelt 实现覆盖 MAC-001~MAC-012，本轮无代码变更，真实 macOS runner 证据保留给 T15；`T14` 的 Browser/compat/updater 既有 focused 回归保持通过，本轮补齐了截图、reattach 和受控契约夹具，T14 相关 Electron contract 已 `71/71 passed`；最终本地复验为 Desktop Vitest `157 files / 1754 passed`、Python security `935 passed, 20 skipped`、Rust release tests 通过、typecheck/Playwright boundary/runtime verifier/npm audit 通过，Windows 安装矩阵在未提升 PowerShell 下被 native helper 正确拒绝；`T15` 的 32 个 ID 保持 `不全面`，发布闭环仍 `BLOCKED`（待 clean commit-bound checkout、真实三平台 runner、签名/attestation/SBOM 和 package signing policy）。各任务不得回滚或覆盖现有 dirty 文件。

### Final local closure audit (2026-08-17)

| 范围 | 当前状态 | 结论 |
|---|---|---|
| T01–T10 | 本地实现和 focused/integration 回归已完成 | 不再拆新的重复开发任务；最终基线仍按 ID 逐项保留现有 `符合`/`不全面`/`欠缺` 状态 |
| T11 | Linux native 代码与 planner 测试通过 | 缺真实 Linux runner；当前 Windows/target 编译不能替代 |
| T12 | macOS Seatbelt 代码与契约测试通过 | 缺真实 macOS runner |
| T13 | Windows native release/path tests通过 | 2 个 installed-fixture tests ignored；本机非管理员，无法完成 elevated/install 证据 |
| T14 | T14-A/B/C 完成，Electron contract `71/71` | 8 个 T14 ID 仍需逐项真实 Browser/updater/CUA/PDF/Wiki 证据，不能整体升级 |
| T15 | 本地 release/security/verifier 通过 | 缺 clean checkout、三平台 evidence、受信 package signing policy、签名/attestation/SBOM；保持 `BLOCKED` |

## Architecture and ownership decisions

1. `T01` 先处置产品边界和 `欠缺` 项；不把 Codex 云产品缺面误派成 Ace 代码实现。
2. `T02` 先冻结 action、permission、grant、snapshot 和 typed result 契约；后续任务消费这些契约。
3. `T03`–`T07` 是核心安全边界，可在 `T02` 完成后并行。
4. `T08`–`T10` 是 Gateway、协议和 MCP/plugin 业务接入，依赖核心边界完成后并行。
5. `T11`–`T13` 分别负责 Linux、macOS、Windows native runtime；平台任务只修改各自平台目录。
6. `T14` 负责 Ace 独有的 Browser、CUA、PDF、Wiki/compat 和 updater 适配。
7. `T15` 最后负责三平台证据、制品、CI/release 和基线最终更新。

## Preflight: freeze the current worktree

当前 Ace 工作树已有未提交安全整改，且存在未跟踪的
`security-runtime/security-runtime-bin/`。在派发任何子智能体前，先把当前工作树固定成一个可引用 checkpoint（提交、补丁包或其他可恢复方式均可）。

当前 dirty 文件的任务归属：

| 当前改动 | 后续 owner |
|---|---|
| `crew/security/audit.py` | T07 |
| `crew/security/file_policy.py` | T03 |
| `crew/security/launch.py`、`crew/tools/builtin.py`、Agent file-change 文件 | T04 |
| `crew/state/config.py` | T06 |
| Gateway auth/startup 文件 | T08/T09 |
| `security-runtime/src/windows/wfp.rs` | T13 |
| runtime manifest、Desktop runtime verify 脚本和生成制品 | T15 |

没有 owner 的任务不得修改别的任务已占用的文件。共享文件需要新增接口时，先提交接口需求，等 owner 任务完成后再接后置适配。

## Dependency graph

```text
T01 boundary ledger
  |
  v
T02 shared authorization/result contracts
  |
  +--> T03 filesystem/path
  +--> T04 process/runtime/resource
  +--> T05 network
  +--> T06 secrets
  +--> T07 audit/taint/data
  +--> T11 Linux runtime
  +--> T12 macOS runtime
  +--> T13 Windows runtime
             |
             v
       T08 Gateway identity
       T09 Gateway protocol/Desktop
       T10 MCP/plugin/skills
       T14 Ace product surfaces
             |
             v
       T15 release evidence and baseline update
```

## Phase 1: foundation and scope

### Task T01: 处置产品边界与缺失能力

**Description:** 为所有 `欠缺` 项建立可执行的 `N/A`、`ACE_EQUIV` 或 `APPLICABLE` 处置，并把产品云能力、Code Mode、Remote、managed config 等缺面与真正需要实现的 Ace 能力分开。

**Baseline IDs:** 68 项

```text
ARCH-013
ARG0-001
MCFG-001~MCFG-005
ESCAL-001
EXEC-015~EXEC-016
PROC-006
NET-026, NET-030
AGID-001~AGID-005
AWS-001
CHAT-001
RAP-001
WID-001~WID-004
CMODE-001~CMODE-010
MCP-019
MSRV-001~MSRV-005
IPC-002, IPC-012, IPC-015
REMOTE-001~REMOTE-006
UDS-001~UDS-002
UPD-001, UPD-003
DATA-002
PROD-001, PROD-002, PROD-009~PROD-011
PROD-003~PROD-008, PROD-012
CLOUD-001
```

**Acceptance criteria:**

- [ ] 60 个 `欠缺` ID 均有合法 disposition；`N/A` 有禁入口测试，`ACE_EQUIV` 指向实际存在的 `ACE-*` ID。
- [ ] 没有把仍属于 `APPLICABLE` 的弱失败入口错误登记为 `N/A`。
- [ ] 产品边界测试通过，ID 总账无重复、无遗漏。

**Verification:**

```text
pytest -q tests/security/test_product_boundary_absent.py
pytest -q tests/security/test_execution_surface_inventory.py
```

**Dependencies:** None.

**Files likely touched:**

- `docs/security/codex-security-na-inventory.json`
- `docs/security/codex-security-product-boundary-na-register.md`
- `tests/security/test_product_boundary_absent.py`

**Estimated scope:** M; documentation and contract tests only.

### Task T02: 冻结统一授权、审批、grant、snapshot 和 typed result 契约

**Description:** 把 action scope、审批 digest、grant 生命周期、permission snapshot、owner/session/task 绑定和不可信 ToolResult provenance 固定成统一类型，供所有执行面使用。

**Baseline IDs:**

```text
GOV-001, GOV-002, GOV-004
ARCH-001, ARCH-004, ARCH-005, ARCH-007
APPROVE-001
PERM-001, PERM-002, PERM-004, PERM-005, PERM-018
EXEC-008, EXEC-010, EXEC-014
ISO-001, ISO-002, ISO-003
```

**Acceptance criteria:**

- [ ] action、command/cwd、filesystem、network、MCP、turn 和 scope 生成稳定 digest。
- [ ] grant 有 owner/workspace/session/task 绑定、TTL、撤销和 single-use 消费语义。
- [ ] 调用方不能绕过统一决策器自行 allow；不可信 result 不能提升为 trusted 或 approval/control。

**Verification:**

```text
pytest -q tests/security/test_security_approvals.py tests/security/test_authorization_snapshot.py tests/security/test_additional_permissions.py tests/security/test_tool_result_boundary.py
```

**Dependencies:** T01.

**Files likely touched:**

- `crew/security/models.py`
- `crew/security/actions.py`
- `crew/security/approvals.py`
- `crew/security/grants.py`
- `crew/security/snapshot.py`
- `crew/core/types.py`
- `crew/tools/pipeline.py`
- `crew/tools/registry.py`

**Estimated scope:** L; one shared security contract, no业务面扩展。

## Checkpoint: foundation

- [ ] T01/T02 的 ID 归属已冻结。
- [ ] 共享类型和授权测试通过。
- [ ] 子智能体收到的文件 owner 清单没有重叠。

## Phase 2: core security boundaries

### Task T03: 文件 capability、路径身份和最终 I/O

**Description:** 统一文件读、写、搜索、grep、patch 和临时文件的路径授权与最终 I/O 校验，覆盖 symlink/reparse、TOCTOU、UNC/device path、大小限制和敏感 metadata。

**Baseline IDs:**

```text
ARCH-014
PERM-008, PERM-009
EXEC-004
FS-001~FS-014
FS-016~FS-020
FS-022
ACE-005, ACE-006
```

**Acceptance criteria:**

- [ ] 所有文件入口在最终 read/write/open handle 前复核同一授权目标。
- [ ] symlink、reparse、UNC、device、父目录、大小写/Unicode 和竞态换件均 fail-closed。
- [ ] 下载、patch、grep、Browser 文件落盘不产生旁路；错误不泄露未授权路径。

**Verification:**

```text
pytest -q tests/security/test_file_policy.py tests/security/test_file_races.py tests/security/test_local_path_reference.py tests/security/test_grep_symlink_escape.py
```

**Dependencies:** T02.

**Files likely touched:**

- `crew/security/local_path.py`
- `crew/security/file_policy.py`
- `crew/tools/file_utils.py`
- `crew/tools/file_tools.py`
- `tests/security/test_file_policy.py`
- `tests/security/test_file_races.py`

**Estimated scope:** L; single filesystem security boundary。

### Task T04: 进程启动、runtime、取消和资源生命周期

**Description:** 统一 helper/runtime 身份、环境 allowlist、executable provenance、进程树、取消、timeout、输出/并发配额，以及 Agent workspace snapshot/file-change 资源预算。

**Baseline IDs:**

```text
HARD-002
RUNTIME-002
ARCH-010
ARG0-003
EXEC-005, EXEC-011, EXEC-012
PROC-002, PROC-004, PROC-005, PROC-009, PROC-011, PROC-012, PROC-013
ISO-011, ISO-014
```

**Acceptance criteria:**

- [ ] helper manifest/source/binary identity在启动前验证，不能回退到无隔离执行。
- [ ] timeout、cancel、pipeline、孙进程、Gateway 重启和 orphan cleanup 形成统一生命周期。
- [ ] workspace snapshot 有文件数、墙钟时间和取消预算，不阻塞主事件循环。

**Verification:**

```text
pytest -q tests/security/test_execution_routing.py tests/security/test_executable_provenance.py tests/security/test_runtime_client.py tests/test_process_registry.py tests/test_agent_loop.py tests/test_file_changes_security.py
```

**Dependencies:** T02.

**Files likely touched:**

- `crew/security/launch.py`
- `crew/security/process_lifecycle.py`
- `crew/security/runtime_client.py`
- `crew/tools/process_registry.py`
- `crew/tools/builtin.py`
- `crew/agent/file_changes.py`
- `crew/agent/loop/tool_runner.py`
- `security-runtime/src/main.rs`
- `security-runtime/src/protocol.rs`

**Estimated scope:** L; 统一执行/资源边界，但不修改 Gateway 协议。

### Task T05: 网络出口、代理、DNS 和 IP 边界

**Description:** 统一 HTTP、SSE、WebSocket、MCP、CONNECT 和 Browser 相关网络请求的最终连接策略、DNS pinning、redirect 重查、proxy 约束和 credential broker 目标绑定。

**Baseline IDs:**

```text
NET-001~NET-025
NET-027~NET-029
CRED-002
```

**Acceptance criteria:**

- [ ] 所有最终 socket/connect 入口经过 host/port/protocol/method 策略。
- [ ] private/loopback/link-local、DNS rebinding、redirect、userinfo、代理逃逸均被拒绝或重新授权。
- [ ] 网络允许/拒绝、重定向和 DNS recheck 产生结构化审计。

**Verification:**

```text
pytest -q tests/security/test_outbound_policy.py tests/security/test_managed_network_contract.py tests/gateway/test_security_outbound.py
```

**Dependencies:** T02.

**Files likely touched:**

- `crew/security/outbound.py`
- `crew/security/provider_proxy.py`
- `crew/gateway/outbound.py`
- `security-runtime/src/network/policy.rs`
- `security-runtime/src/network/proxy.rs`
- `security-runtime/src/network/connector.rs`

**Estimated scope:** L; Python policy与native network connector分层处理。

### Task T06：Secrets、keyring 和凭据生命周期

**Description:** 让 provider/API key、MCP OAuth、session credential 只通过平台安全 backend 持久化和注入，完成 marker、轮换、撤销、原子写入和失败清理。

**Baseline IDs:**

```text
CRED-001
SEC-001, SEC-004, SEC-009, SEC-011, SEC-013, SEC-014, SEC-015, SEC-016
SECRET-001, SECRET-002, SECRET-004
```

**Acceptance criteria:**

- [ ] 磁盘只保存受保护 marker，不保存明文 credential。
- [ ] secret 不进入 env、argv、URL、checkpoint、cache、crash dump、日志或错误响应。
- [ ] secret 读取按 owner/task/host/purpose/TTL 约束，marker/keyring 失败时 fail-closed。

**Verification:**

```text
pytest -q tests/security/test_platform_secret_store.py tests/security/test_dotenv_security.py tests/security/test_mcp_secret_persistence.py
```

**Dependencies:** T02；注入层消费 T04/T05 的接口。

**Files likely touched:**

- `crew/security/secret_store.py`
- `crew/security/mcp_secrets.py`
- `crew/state/config.py`
- `tests/security/test_platform_secret_store.py`
- `tests/security/test_dotenv_security.py`

**Estimated scope:** M。

### Task T07：审计、taint、错误和持久化数据边界

**Description:** 完成安全事件审计链、typed provenance、公开错误脱敏、rollout/cache/feedback 附件访问控制和数据保留边界。

**Baseline IDs:**

```text
EXEC-013
SEC-012, SEC-017, SEC-018
AUD-001~AUD-004
AUD-006~AUD-010
DATA-001
DATA-003~DATA-008
```

**Acceptance criteria:**

- [ ] 审计关联 actor、task、session、turn、action digest、policy/build version 和结果。
- [ ] 不可信内容不能伪造 trusted、approval、control 或跨工具授权信息。
- [ ] 错误、日志、诊断、rollout、cache 和 feedback 不泄露 secret、环境或宿主路径。

**Verification:**

```text
pytest -q tests/security/test_security_audit.py tests/security/test_tool_result_boundary.py tests/test_gateway_helpers.py tests/test_hook_security.py
```

**Dependencies:** T02。

**Files likely touched:**

- `crew/security/audit.py`
- `crew/tools/redact.py`
- `crew/gateway/helpers.py`
- `crew/gateway/response_filters.py`
- `tests/security/test_security_audit.py`

**Estimated scope:** L; audit、taint、diagnostic 是一个共享边界。

## Checkpoint: core boundaries

- [ ] T03–T07 的 targeted tests 通过。
- [ ] 共享 owner 文件没有跨任务修改。
- [ ] 每个任务输出剩余 ID 和证据缺口，不直接修改基线状态。

## Phase 3: business-facing adapters and native platforms

### Task T08：Gateway 认证、instance/session 身份和撤销

**Description:** 统一 Gateway 敏感路由、instance proof、owner/session 绑定、session recovery、权限撤销和断连冻结语义。

**Baseline IDs:**

```text
ACE-001, ACE-003, ACE-017
SEC-006, SEC-007, SEC-008
ISO-005, ISO-007, ISO-009, ISO-010, ISO-012, ISO-013
IPC-004, IPC-006
```

**Acceptance criteria:**

- [ ] 所有敏感 Gateway route 都进入统一 auth/owner/session 决策。
- [ ] instance key、session state、恢复 checkpoint 和 revoke 绑定 owner/version/完整性。
- [ ] 断连、认证过期、权限撤销、Gateway 重启后旧 token/grant/process 不可恢复。

**Verification:**

```text
pytest -q tests/gateway/test_auth_contract.py tests/gateway/test_account_isolation.py tests/gateway/test_gateway_instance_auth.py tests/gateway/test_security_api.py
```

**Dependencies:** T02、T04、T07。

**Files likely touched:**

- `crew/gateway/auth.py`
- `crew/gateway/auth_policy.py`
- `crew/gateway/instance_auth.py`
- `crew/gateway/route_auth.py`
- `crew/gateway/routers/security.py`
- `crew/gateway/routers/sessions.py`
- `crew/gateway/server.py`

**Estimated scope:** L; Gateway identity boundary。

### Task T09：Gateway/IPC 协议、流、幂等和 Desktop 启动

**Description:** 统一 JSON/HTTP/IPC/WebSocket/SSE framing、body/stream budget、错误 envelope、分页引用、幂等重试和 Desktop 复用 Gateway 前的重新证明。

**Baseline IDs:**

```text
IPC-003, IPC-011, IPC-013, IPC-014
PROTO-001, PROTO-002, PROTO-004, PROTO-005
PROTO-007~PROTO-014
ELECT-001~ELECT-003
```

**Acceptance criteria:**

- [ ] frame、body、upload、download、SSE、WebSocket 和 PTY stream 有硬上限与超时。
- [ ] parser、编码异常、取消、断线和重试都释放资源且不会重复危险 action。
- [ ] Desktop 只复用已通过 instance/security-state 验证的 Gateway。

**Verification:**

```text
pytest -q tests/gateway/test_ipc_boundary_hardening.py tests/gateway/test_protocol_security.py tests/test_gateway_dispatcher.py
npm --prefix desktop run test -- --run tests/unit/gateway-availability.test.ts
```

**Dependencies:** T02、T07、T08。

**Files likely touched:**

- `crew/gateway/dispatcher.py`
- `crew/gateway/json_budget.py`
- `crew/gateway/json_budget_middleware.py`
- `crew/gateway/ws.py`
- `desktop/src/main/gateway-availability.ts`
- `desktop/src/main/index.ts`

**Estimated scope:** L。

### Task T10：MCP、插件、技能和 capability discovery

**Description:** 让 MCP server、plugin、skill、hook 和 external agent 继承父任务权限、网络、env、资源、审计和 taint 边界。

**Baseline IDs:**

```text
CAP-001
PLUG-003
MCP-002, MCP-003, MCP-007, MCP-008
MCP-010, MCP-012~MCP-018
```

**Acceptance criteria:**

- [ ] MCP/plugin/skill 不能通过输出、manifest、环境或工具 schema 扩大权限。
- [ ] server identity、配置/权限变更、关闭/删除/更新、token 撤销和资源配额可审计。
- [ ] MCP stdio 和 remote env 不继承宿主 PATH/HOME/secret，异常时 fail-closed。

**Verification:**

```text
pytest -q tests/security/test_managed_mcp_stdio.py tests/security/test_mcp_command_integrity.py tests/security/test_plugin_execution_boundary.py tests/gateway/test_mcp_servers_authorization.py tests/gateway/test_plugins_router_security.py
```

**Dependencies:** T02、T05、T07、T08。

**Files likely touched:**

- `crew/tools/mcp_client.py`
- `crew/gateway/mcp_server.py`
- `crew/gateway/routers/mcp_servers.py`
- `crew/plugins/manager.py`
- `crew/tools/skills_tools.py`
- `crew/gateway/routers/plugins.py`

**Estimated scope:** L。

### Task T11：Linux native sandbox

**Description:** 完成 bubblewrap、namespace、proc、mount、seccomp、Landlock、proxy bridge、PDEATHSIG 和不支持平台的拒绝路径。

**Baseline IDs:**

```text
LNX-001~LNX-016
LNX-018~LNX-022
```

**Acceptance criteria:**

- [ ] filesystem、process、network 和 protected metadata 边界由 Linux native 层实际强制。
- [ ] bwrap/proc/network/backend 缺失或不支持时拒绝，不降级到无隔离。
- [ ] 取得真实 Linux runner 证据，而非只依赖源码/单测。

**Verification:**

```text
cargo test --manifest-path security-runtime/Cargo.toml --test linux_bwrap --test linux_bwrap_plan --test linux_fail_closed --test linux_adversarial
```

**Dependencies:** T04 的 runtime protocol。

**Files likely touched:**

- `security-runtime/src/linux/`
- `security-runtime/tests/linux_bwrap.rs`
- `security-runtime/tests/linux_bwrap_plan.rs`
- `security-runtime/tests/linux_fail_closed.rs`
- `security-runtime/tests/linux_adversarial.rs`

**Estimated scope:** L；仅 Linux 目录。

### Task T12：macOS Seatbelt sandbox

**Description:** 完成 Seatbelt 默认 deny、filesystem/network/socket scope、proxy 异常拒绝和真实设备证据。

**Baseline IDs:**

```text
MAC-001~MAC-012
```

**Acceptance criteria:**

- [ ] profile 按 task/process 生成，filesystem、network、DNS、Unix socket 精确受限。
- [ ] sandbox/profile/proxy 启动失败时拒绝执行。
- [ ] 取得真实 macOS runner 证据。

**Verification:**

```text
cargo test --manifest-path security-runtime/Cargo.toml --test macos_profile --test macos_adversarial
```

**Dependencies:** T04 的 runtime protocol。

**Files likely touched:**

- `security-runtime/src/macos/`
- `security-runtime/tests/macos_profile.rs`
- `security-runtime/tests/macos_adversarial.rs`

**Estimated scope:** M。

### Task T13：Windows native sandbox、ACL、Job 和 WFP

**Description:** 完成 restricted/elevated token、DACL、Job Object、WFP、private desktop、reparse path、隐藏账户和 Windows attach/recovery 边界。

**Baseline IDs:**

```text
WIN-001~WIN-024
```

**Acceptance criteria:**

- [ ] filesystem、network、process tree、token、ACL 和 metadata 边界由 Windows native 层实际强制。
- [ ] restricted/elevated 两条路径、安装/卸载、取消/恢复和 WFP 失败都 fail-closed。
- [ ] 取得真实 installed Windows runner 证据。

**Verification:**

```text
cargo test --manifest-path security-runtime/Cargo.toml --tests
pytest -q tests/security/test_windows_runtime_contract.py tests/security/test_native_runtime_contract.py
```

**Dependencies:** T04。

**Files likely touched:**

- `security-runtime/src/windows/`
- `security-runtime/tests/windows_acl.rs`
- `security-runtime/tests/windows_job.rs`
- `security-runtime/tests/windows_sandbox.rs`
- `security-runtime/tests/windows_token.rs`
- `security-runtime/tests/windows_paths.rs`
- `security-runtime/tests/windows_readiness.rs`

**Estimated scope:** L；仅 Windows 目录和对应 runner。

### Task T14：Ace Browser、CUA、PDF、Wiki/compat 和 updater 适配

**Description:** 把 Ace 独有 Browser、CUA、HTML-to-PDF、Wiki/compat 和 updater 入口接入已完成的授权、网络、runtime、审计和制品边界。

**Baseline IDs:**

```text
ACE-010~ACE-014
BROW-002
UPD-002, UPD-004
```

**Acceptance criteria:**

- [ ] Browser navigation/download/upload/page script 共享 owner/scope/network/audit 边界。
- [ ] CUA、PDF、Wiki/compat 不拥有独立旁路权限。
- [ ] updater 固定 HTTPS host、大小、签名、pinning、回滚和失败行为闭环。

**Verification:**

```text
pytest -q tests/gateway/test_upload.py tests/security/test_headless_security.py tests/security/test_html_to_pdf_hardening.py tests/gateway/test_cua_setup_authorization.py
npm --prefix desktop run test:pw-contract
```

**Dependencies:** T03、T05、T08、T09、T10、T13。

**Files likely touched:**

- `crew/gateway/routers/browser.py`
- `crew/tools/cua_setup.py`
- `crew/gateway/routers/wiki.py`
- `desktop/src/main/browser/`
- Desktop updater modules

**Estimated scope:** L；Ace 独有执行面集中处理。

### T14 execution evidence (2026-08-17, current worktree)

T14 当前状态：`READY_FOR_T15_REVIEW`。Electron 合同已经能够完整跑完并正常退出；此前由持久化 Electron profile 锁导致的 Windows 临时目录清理挂起已在合同 harness 中处理，不把“没有跑完”误判成通过。

```text
CREW_PW_CONTRACT_FILTER='BrowserHost screenshot/evaluate/tab-close/network-clear 用户输出契约' npm run test:pw-contract
→ 1/1 passed；public Page/ref PNG/JPEG、evaluate、close、network clear

npm run test:pw-contract
→ 71/71 passed；进程正常退出

npm --prefix desktop test -- --run tests/unit/electron-cdp-transport.test.ts
→ 52/52 passed

npm --prefix desktop run typecheck
→ passed
```

T14 的 3 个并行子任务均已完成：

| 子任务 | 独占修改边界 | 当前失败与验收 |
|---|---|---|
| `T14-A` 输出截图生产链 | `desktop/src/main/browser/electron-cdp-transport.ts`、browser output focused tests | 已通过 native hidden viewport、full-page 分段 bitmap 合成、JPEG/PNG 编码和滚动恢复；transport `52/52`，完整 contract `71/71` |
| `T14-B` 合同夹具与受控文件 | `desktop/scripts/pw-contract.ts` 及其 fixture server；不放宽生产 URL/upload 校验 | 已将遗漏的 `data:` active-close 夹具改为本地 HTTP route；Page 生命周期/recorder focused contract 各 `1/1`，完整 contract `71/71` |
| `T14-C` Playwright reattach 生命周期 | `desktop/src/main/browser/playwright-engine.ts` 及对应 focused unit test | 已完成 debugger detach 后旧 Page retirement、新 Page 绑定及 generation/focus/chooser 清理；Engine+Transport `57 passed` |

实现要点：Electron 43 hidden 页面上的 `Page.captureScreenshot` 在 viewport/fullPage 请求可能卡住，T14-A 现在在可验证边界内优先使用 `capturePage`；fullPage 先尝试原生整页，必要时按 viewport 捕获 BGRA bitmap 并合成，最后恢复原滚动位置。viewport clip 对 layout viewport 与 Playwright clip 的小幅滚动条误差做受限归一化，不放宽任意跨页面 clip。T14-B 只调整 fixture URL/root，未修改 `safeUrl` 或上传目录策略。T14 相关基线 ID 仍等待 T15 按逐项证据更新，不因 contract 全绿自动整体标记为 `符合`。

## Checkpoint: business surfaces

- [ ] T08–T14 的 targeted tests 通过。
- [ ] Linux/macOS/Windows 任务分别产出平台证据或明确拒绝证据。
- [ ] Browser、MCP、Gateway 没有绕过 T02–T07 的共享契约。

## Phase 4: release evidence and baseline closure

### Task T15：安全测试、制品、CI/release 和基线收口

**Description:** 汇总所有任务的源码、测试、平台 runner、runtime artifact、manifest、签名、SBOM、clean checkout 和 CI 门禁证据，并最终更新基线状态。

**Baseline IDs:**

```text
GOV-010, GOV-011
REL-001, REL-002
SUP-001, SUP-002
SUP-004~SUP-007
SUP-009~SUP-014
TEST-001~TEST-008
TEST-010
TEST-012~TEST-016
ACE-018, ACE-020
```

**Acceptance criteria:**

- [ ] source、binary、manifest、测试结果、签名/attestation 和发布包属于同一 commit/target。
- [ ] 任一安全测试、digest、SBOM、clean tree、签名或 runner identity 缺失都会阻断 release。
- [ ] 逐个 ID 回填实现证据、失败行为、验证命令和最终状态；不因“代码存在”跳过真实平台证据。

**Verification:**

```text
pytest -q tests/security/test_release_security_workflows.py tests/security/test_release_paths.py
npm --prefix desktop run security:verify
npm --prefix desktop run typecheck
pytest -q -p no:cacheprovider tests/security
```

**Dependencies:** T01–T14。

**Files likely touched:**

- `.github/workflows/`
- `scripts/build-security-runtime.ps1`
- `scripts/build-security-runtime.sh`
- `desktop/scripts/verify-security-runtime.mjs`
- `security-runtime/bin/runtime-manifest.json`
- `docs/security/codex-security-capability-baseline.md`

**Estimated scope:** L；只做发布/证据/基线收口，不重新实现业务逻辑。

### T15 execution evidence (2026-08-17, current worktree)

发布结论：`BLOCKED`。以下矩阵只证明本地 Windows 目标与源码/manifest 的一致性，不把 dirty worktree 当成 release checkout，也不替代真实三平台或签名证据。

| 证据 | 结果 |
|---|---|
| source / commit | `HEAD 491c914643eb857c7de421465b3417f1920a4727`; worktree dirty，故不满足 clean commit-bound release |
| Windows target | `x86_64-pc-windows-msvc`; `cargo build --release --target x86_64-pc-windows-msvc` passed |
| binary | `security-runtime/bin/ace-security-runtime.exe`; SHA-256 `dc55074e7225bbcb7dce5943231eb16e17e901f0804fbee95570f11c389a8f81`; Desktop staged digest identical |
| manifest | `security-runtime/bin/runtime-manifest.json`; schema 2, `win32/x64`, source hash `f332d2ab080f2753ed4c923b695896d16ce1e64043e671f840a854bbcbb0f7f5`, binary digest matches |
| Desktop verifier | `npm --prefix desktop run security:verify` passed; `runtime_source_stale=False` |
| Desktop dependency audit | `npm audit` full and production scopes both report `0` vulnerabilities after `dompurify 3.4.13`, `mermaid 11.16.1`, `nanoid 3.3.18` refresh |
| inventory | `tests/security/test_execution_surface_inventory.py`: `15 passed`; `docs/security/electron-process-callsites.json` contains 18 registered callsites with owner, enforcement, identity, test and review metadata |
| data-flow registry | `docs/security/data-flow-registry.json` contains 10 real/explicitly absent flows; registry contract `2 passed`; exact `.gitignore` exception added |
| release workflow/path gate | release tests: `61 passed, 1 warning` using an explicit external basetemp; all 4 committed lockfiles are discovered and audited, including `crew/skills/html-to-pdf/package-lock.json`; exact Desktop Playwright `1.62.0` boundary and `npm ci` verification passed |
| Desktop typecheck / whitespace | `npm --prefix desktop run typecheck` passed; `git diff --check` passed (CRLF warnings only) |
| native Rust | `cargo fmt --check` passed; Windows-target `cargo test --tests --release --locked`: `150 passed, 2 ignored`; Windows path release tests `6/6` |
| Python security full run | `935 passed, 20 skipped` using an explicit external basetemp and `--no-cacheprovider`; process inventory now includes the dev-only contract cleanup callsite |
| Gateway smoke | `python -m crew.gateway.server`, bounded local smoke: `/health` HTTP 200, startup/ready logs present, exact spawned PID stopped |
| Desktop smoke | `npm start` in `desktop`, bounded run: `build:dev` and Electron passed, managed Gateway `28180/api/health` returned 200 with `startup/cron/security_state=ready`; exact Electron/Gateway child PIDs stopped |
| current startup recheck | Separate `python -m crew.gateway.server` smoke on `GATEWAY_PORT=28180` returned `/health` 200; separate `npm start` rebuilt and Electron connected to the existing loopback Gateway via `wait-existing-instance`; both were stopped by their owning smoke session |
| signatures / attestations / other platforms | unavailable in this Windows worktree; fail-closed, not fabricated |

T15 did not change any baseline status to `符合`. The 32 T15 IDs remain `不全面`; `N/A`/`ACE_EQUIV`/`APPLICABLE` handling remains governed by the T01 register and was not changed. Remaining blockers are clean commit-bound checkout, real Linux/macOS/Windows runner evidence, and trusted package signatures/attestation/SBOM; local Windows build, default Desktop verifier, release gate, native tests and Python security suite are now green.

## ID coverage check

| Task | ID count |
|---|---:|
| T01 | 68 |
| T02 | 19 |
| T03 | 26 |
| T04 | 16 |
| T05 | 29 |
| T06 | 12 |
| T07 | 20 |
| T08 | 14 |
| T09 | 19 |
| T10 | 14 |
| T11 | 21 |
| T12 | 12 |
| T13 | 24 |
| T14 | 8 |
| T15 | 32 |
| **Total** | **334** |

## Shared-file conflict rules

默认一个文件只有一个写 owner：

| File | Owner |
|---|---|
| `crew/tools/builtin.py` | T04 |
| `crew/security/launch.py` | T04 |
| `crew/security/audit.py` | T07 |
| `crew/gateway/routers/security.py` | T08 |
| `desktop/src/main/index.ts` | T09 |
| `security-runtime/src/windows/wfp.rs` | T13 |
| `docs/security/codex-security-capability-baseline.md` | T15 |

其他任务如需共享文件中的新能力，只能消费已冻结接口或提交后置适配，不得同时直接修改同一 hub 文件。

## Sub-agent handoff contract

每个子智能体完成任务时必须返回：

1. 修改文件清单。
2. 已处理的基线 ID。
3. 每个 ID 对应的测试/runner/发布证据。
4. 仍未满足的 ID 及原因。
5. 需要其他任务提供的接口或后续证据。

子智能体不得直接把基线状态改成 `符合`；状态更新统一由 T15 在集成证据齐全后完成。

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| 共享 hub 文件发生并行修改 | High | 单文件 owner；其他任务只读或提交后置适配 |
| 把 `欠缺` 当成普通代码缺口 | High | T01 先完成 N/A/ACE_EQUIV/APPLICABLE 处置 |
| Python 预检查被误当成 native 强制 | High | T11–T13 必须有真实平台 runner |
| 代码改完但 artifact/manifest 漂移 | High | T15 绑定 source、binary、manifest、commit 和 target |
| 单项测试通过但 sibling entry 未覆盖 | Medium | 每个任务验收必须包含执行面矩阵和最终副作用断言 |

## Definition of done

一个 ID 只有同时满足实现、统一强制点、允许/拒绝/错误/超时/取消/重启验证、平台证据、审计关联和 CI/release 阻断条件，才能在基线中从 `不全面` 或 `欠缺` 更新为 `符合`。
