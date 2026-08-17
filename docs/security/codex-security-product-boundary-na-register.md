# Codex 安全能力产品边界 N/A / ACE_EQUIV 登记

> 总账：`docs/security/codex-security-na-inventory.json`
> 自动门禁：`tests/security/test_product_boundary_absent.py`
> 对照基线：`docs/security/codex-security-capability-baseline.md`

## 1. 目的与判定语义

本登记把 436 个基线 ID 转成一份可计算的适用性总账，避免把 Ace 不会建设的 Codex
业务面长期误报成“待补产品”，也避免用 N/A 掩盖可迁移的安全性质。

有效处置只有三种：

- `APPLICABLE`：默认值。ID 继续按基线的 `符合 / 不全面 / 欠缺` 评估和整改。
- `N/A`：Codex 特定业务面不属于 Ace 产品边界，且对应禁入口静态门禁必须持续为绿。
  一旦入口出现，N/A 自动失去依据；N/A **不等于符合**。
- `ACE_EQUIV`：不复制 Codex 产品形态，但安全责任转交到列出的 `ACE-*` ID。只有这些
  Ace ID 的实现、负测、真实平台和发布证据全部满足时，才可能另行提升状态；
  本登记本身不证明符合。

JSON 对未列入覆盖项的基线 ID统一派生为 `APPLICABLE`，因此总账是闭合的：

| 有效处置 | ID 数 | 含义 |
|---|---:|---|
| `APPLICABLE` | 366 | 默认继续适用 |
| `N/A` | 49 | 产品面缺失，并由禁符号门禁守住 |
| `ACE_EQUIV` | 21 | 转交到明确的 Ace 产品 ID |
| 合计 | 436 | 与基线唯一 ID 数一致 |

原基线三态和统计不因本登记自动改变，尤其不会把任何项改成“符合”。

### T01 范围闭合

T01 的精确 68 项范围和逐项机器处置记录在总账的
`task_scopes.T01.expected_dispositions` 中：

| T01 处置 | ID 数 | 规则 |
|---|---:|---|
| `N/A` | 49 | 产品入口不存在，并由 `absence_rules` 禁止入口回归 |
| `ACE_EQUIV` | 18 | 转交真实存在的 `ACE-*` 基线能力，不能据此宣称已符合 |
| `APPLICABLE` | 1 | `CLOUD-001` 保留认证失败不换路由、不发包的强负测责任 |
| 合计 | 68 | 其中全部 60 个 `欠缺` 项均已离开默认 `APPLICABLE` |

T01 的另外 7 个 `不全面` 项是 `PROD-003`–`PROD-008`、`PROD-012`，均为
`ACE_EQUIV`；`CLOUD-001` 是唯一保留 `APPLICABLE` 的 T01 项。总账中已经存在的
`DATA-005`、`UPD-002`、`UPD-004` 属于其他任务的登记，T01 不覆盖也不删除它们。

## 2. 已确认不建设的产品面

以下是产品决策，不是当前实现能力不足的临时借口：

| 产品面 | 处置 ID |
|---|---|
| caller 可声明的 `ExternalSandbox` 模式 | `ARCH-013` |
| Codex 单二进制 argv0 多身份分发 | `ARG0-001` |
| Unix 沙箱内 escalation server/execve wrapper | `ESCAL-001` |
| Codex execpolicy / Starlark 格式 | `EXEC-015`–`EXEC-016` |
| cloud managed-config 控制面 | `MCFG-001`–`MCFG-005` |
| Codex 云 Agent JWKS | `AGID-003` |
| AWS/ChatGPT/responses-api-proxy 专属客户端 | `AWS-001` `CHAT-001` `RAP-001` |
| workload identity token exchange | `WID-001`–`WID-004` |
| Code Mode、nested tools、Code Mode transport/V8 runtime | `CMODE-001`–`CMODE-010` |
| Codex 式入站 MCP 任意执行/配置覆盖 | `MCP-019` `MSRV-001`–`MSRV-005` |
| 远程执行 relay、Noise/ML-KEM rendezvous、remote-control client/segment 产品 | `IPC-002` `IPC-012` `IPC-015` `REMOTE-001` `REMOTE-002` `REMOTE-005` `REMOTE-006` |
| stdio-to-UDS 无自身鉴权桥 | `UDS-002` |
| PTY/ConPTY 分配、resize 与交互终端产品 | `PROC-006` |
| TLS MITM 凭据注入与产品托管 CA | `NET-026` `NET-030` |
| Codex cloud environments、Codex Security、Trusted Access | `PROD-001` `PROD-002` `PROD-009` `PROD-010` |

每组的理由、证据、禁符号规则和复审触发都以 JSON 为规范来源。

Ace 当前已有管理员鉴权的远程插件 bundle 下载、解包、暂存和激活入口，因此
`PLUG-001`–`PLUG-003` 保持 `APPLICABLE`，必须按真实供应链攻击面验收，不能以
“没有 Codex marketplace”标为 N/A。

## 3. ACE_EQUIV 转交

不复制 Codex 产品形态，但 Ace 已有对应业务面时，责任转交如下：

| Codex ID | Ace 产品边界 ID |
|---|---|
| `AGID-001` `AGID-004` `AGID-005` | `ACE-015` `ACE-017` |
| `AGID-002` | `ACE-003` |
| `REMOTE-003` `REMOTE-004` | `ACE-017` |
| `UDS-001` | `ACE-003` `ACE-017` |
| `UPD-001`–`UPD-004` | `ACE-013` `ACE-020` |
| `DATA-002` | `ACE-017` `ACE-018` |
| `DATA-005` | `ACE-016` `ACE-018` |
| `PROD-003` `PROD-012` | `ACE-002` `ACE-008` `ACE-009` `ACE-018` `ACE-019` |
| `PROD-004` | `ACE-018` `ACE-019` |
| `PROD-005` `PROD-011` | `ACE-010` |
| `PROD-006` | `ACE-011` |
| `PROD-007` | `ACE-017` |
| `PROD-008` | `ACE-018` |

`ACE_EQUIV` 只表示“去哪里验收”，不表示被映射的 Ace ID 当前已安全等价。

## 4. Ace 入站 MCP 的精确例外

Ace 已有两个受限的 production MCP server 构造点，不能用“无入站 MCP”笼统删除：

1. `crew/gateway/mcp_server.py` 的 `MCPServer("crew", ...)`：owner-scoped session/team 工具。
2. 同文件的 `MCPServer("crew-interaction", ...)`：服务端 binding/token 约束的最小交互代理。

门禁只允许该文件中现有的两次 import 和两个 literal server 构造；在其他 production
路径新增 `MCPServer` / `FastMCP`，或增加第三个构造点，测试都会失败。该例外不得扩展为
Codex 式 caller override：禁止从 MCP 工具接收 model、cwd、approval/sandbox policy、
instructions、analytics 或 arbitrary config 来创建任意 Agent 执行。

Ace 作为 **MCP client** 连接外部 server、以及把固定 MCP 配置注入外部 runtime，仍属于
现有 `MCP-*` / `ACE-008` / `ACE-009` 适用面，不因这里的入站产品边界而 N/A。

## 5. 不得 N/A 的 Codex 弱失败

下列项保留 `APPLICABLE`，并在 JSON 的 `required_stronger_negative` 中登记 Ace 必须做的
更强负测：

| ID | Ace 必须证明 |
|---|---|
| `ARG0-002` | 不存在可绕过文件授权/会话边界的 patch/write 别名或备用入口 |
| `CLOUD-001` | 认证失败不会切换到默认/直连代理并继续发送 |
| `HOOK-001` | 畸形、未知、不支持的权限结果默认拒绝，原输入和改写输入都不执行 |
| `LNX-017` | profile 组合不会静默跳过必需的文件、进程或网络强制层 |
| `NET-027` | 已配置 proxy 也不能跳过最终非公网目标检查 |
| `WIN-021` | cwd/junction/reparse 准备失败时拒绝，不回退原路径启动 |
| `WIN-024` | WFP 安装/验证失败时 managed network 不可用，且没有子进程或直连 |

这些登记不是“Codex 行为照抄”；目标是让 Ace 的失败行为更强、更容易负测。

## 6. 自动门禁与复审

`tests/security/test_product_boundary_absent.py` 检查：

1. JSON schema、固定计数和 436-ID 闭合总账。
2. 覆盖 ID 必须存在于基线且不得重复；`ACE_EQUIV` 必须引用存在的 `ACE-*` ID。
3. 弱失败 ID 必须留在 `APPLICABLE`，不得进入 N/A/ACE_EQUIV 覆盖。
4. production 源码中产品缺面的禁符号不得出现。
5. Ace session/interaction MCP 只允许精确白名单构造。

出现以下任一情况必须复审 JSON 和本登记，而不是先放宽测试：

- 新增执行入口、listener、transport、云端控制面、身份交换、远程 bundle 或 updater。
- 现有 Ace MCP server 增加新的构造点或 caller-controlled 执行配置。
- `ACE-*` 映射 ID 被重命名、拆分、删除或改变安全边界。
- 基线 ID 数、ID 名称或产品范围变化。
- 禁符号命中；此时先判断产品决策是否已改变，再由安全评审显式修改总账。
