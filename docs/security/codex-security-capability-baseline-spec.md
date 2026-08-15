# Spec: Codex 安全能力基线与 Ace 对照矩阵

## Objective

建立一份以当前本地 `D:/MobileWork/codex` 源码为证据、可持续更新的 Codex 安全能力基线，并逐项对照 `D:/MobileWork/Ace`：

- 拆解 Codex 从权限模型、审批、执行策略、操作系统沙箱、文件系统、网络代理、密钥、MCP、远程执行、进程生命周期、日志审计、构建发布到测试门禁的安全能力。
- 每项记录 Codex 的实现证据、保护目标、强制边界、失败行为和验证方式。
- 每项记录 Ace 的对应实现、调用链、测试证据和判定：`符合`、`不全面`、`欠缺`。
- “符合”只表示安全目标、强制边界和失败行为达到安全等价；存在正常路径不等于符合。
- 对 Codex 没有直接对应对象的 Ace 独有执行面（Gateway、Electron/Chromium、CUA、Sites、Wiki、PDF、更新器等）单独标记为 Ace 专属安全审计项，不误称 Codex 已覆盖。

最终产物：

- `docs/security/codex-security-capability-baseline.md`
- `docs/security/codex-security-product-boundary-na-register.md`
- `docs/security/codex-security-na-inventory.json`
- `tests/security/test_product_boundary_absent.py`

基线是审计和整改索引，不替代源代码、测试或发布证据；适用性 JSON 为全部 ID 提供
默认 `APPLICABLE` 加显式覆盖的机器总账。每项必须可回溯到源码路径、测试路径、明确的
“未找到证据”或经自动门禁验证的产品缺面。

## Scope

### Included

1. Codex `codex-rs/` 中所有直接影响安全边界的代码、协议、配置、测试和安全相关文档。
2. Codex 的执行入口及其调用链：统一执行、exec-server、MCP、插件/技能、hooks、远程 environment、网络请求和文件能力。
3. 操作系统安全后端：Linux bubblewrap/seccomp/Landlock/PID/network namespace，macOS Seatbelt，Windows token/ACL/Job Object/WFP/elevated runner。
4. Ace `crew/`、`security-runtime/`、`desktop/`、`scripts/`、`tests/security/`、`tests/gateway/` 和 `docs/security/` 中的对应能力及独有执行面。
5. 安全相关的构建、制品完整性、供应链、发布证据、测试矩阵和 fail-closed 行为。

### Excluded or separately classified

- 与安全边界无关的业务功能、UI 视觉细节和普通性能实现。
- Codex 云端服务、未出现在本地仓库中的服务器端实现；只能记录本地代码可证明的行为。
- 第三方依赖内部实现；只记录 Ace/Codex 对依赖的配置、调用和边界，不假定依赖天然安全。
- Codex 没有同类对象的 Ace 功能不会伪造 Codex 对照结果，而是列入“独有执行面”章节。
- Ace 不新增 Code Mode、Codex 式 caller-controlled 入站 MCP、Codex 云控制面、
  Noise/remote-control 或其他登记为产品缺面的业务；缺面必须以 JSON 禁符号规则持续证明，
  不能只写文档结论。

## Evidence and status rules

### Evidence hierarchy

从强到弱：

1. 真实运行时/系统集成测试和发布 workflow 证据。
2. 可执行测试覆盖的源码调用链。
3. 生产入口源码调用链。
4. 单元测试或协议测试。
5. 文档、注释、设计意图。
6. 仅有类型、枚举、schema、死代码或未接入模块：不算已实现。

### Ace status

| Status | Meaning |
|---|---|
| `符合` | Ace 有可证明的等价保护目标、强制边界、失败行为和验证证据；实现方式可以不同。 |
| `不全面` | Ace 有部分能力或正常路径，但存在未覆盖执行面、弱化边界、兼容旁路、平台缺口、资源/供应链缺口或验证不足。 |
| `欠缺` | Ace 没有对应实现、没有可信调用链，或安全能力在关键场景完全缺失。 |

### Applicability disposition

适用性不替代上述实现三态：

| Disposition | Meaning |
|---|---|
| `APPLICABLE` | 默认继续按基线验收；未显式覆盖的 ID 全部属于此类。 |
| `N/A` | Codex 特定产品面不属于 Ace，且禁入口测试持续证明其不存在；不表示符合。 |
| `ACE_EQUIV` | 不复制 Codex 产品形态，安全责任转交到显式列出的 `ACE-*` ID；不表示该 Ace ID 已符合。 |

Codex 的弱失败或旁路性质不能因同名入口不存在而简单 N/A。至少
`ARG0-002`、`HOOK-001`、`LNX-017`、`NET-027`、`WIN-024` 必须保持
`APPLICABLE` 并登记 Ace 更强负测。

### Important interpretation rules

- 仅 Python/TypeScript 预检查不能等同于 OS sandbox 或网络出口强制。
- 仅 `authorize_*()` 返回 allow 不能证明后续真实 I/O 使用了同一授权目标。
- 仅“缺少 runtime 时通常报错”不能证明所有入口都 fail-closed；必须检查所有 fallback。
- 仅旁置 SHA-256 不能证明制品来源真实性；需区分完整性、真实性和发布证据。
- 仅安全模块有测试不能证明所有调用者都经过安全模块。
- 对 Ace 独有执行面，若没有可比 Codex 能力，状态按 Ace 自身目标评估，不强行标记为 Codex 已覆盖。

## Document structure

最终基线按以下标题组织；每项至少包含：`ID`、`能力`、`保护目标`、`Codex 实现证据`、`Codex 强制边界/失败行为`、`Ace 证据`、`Ace 状态`、`缺口/整改`、`验证方式`。

1. 审计口径、威胁模型和信任边界
2. 安全架构、权限模式和能力生命周期
3. 权限模型、审批、授权、grant 与策略约束
4. 命令解析、执行策略和危险操作控制
5. 进程启动、进程树、PTY、取消、超时和资源限制
6. Linux sandbox：bubblewrap、seccomp、Landlock、namespace
7. macOS sandbox：Seatbelt profile 与网络边界
8. Windows sandbox：token、ACL、Job Object、WFP、elevated runner
9. 文件系统 capability、路径解析、symlink/reparse、竞态和临时目录
10. 网络 sandbox、代理、DNS、IP、重定向、协议和凭据代理
11. Secrets、认证凭据、keyring、DPAPI、环境变量和日志脱敏
12. MCP、插件、技能、hooks、外部 Agent 和工具调用隔离
13. app-server、exec-server、远程 environment、IPC、Noise 和身份绑定
14. 输入验证、协议 framing、输出限制、DoS 和错误处理
15. 多租户/owner/session/task 隔离与状态恢复
16. 审计日志、遥测、诊断、崩溃清理和取证能力
17. 构建、依赖、制品完整性、签名、发布和升级安全
18. 安全测试、真实平台证据和发布门禁
19. Ace 独有执行面：Gateway、Electron、Browser、CUA、Sites、Wiki、PDF、更新器
20. 总结矩阵、阻断项、整改优先级和验收清单

## Commands

### Discovery and audit

```powershell
Set-Location D:\MobileWork\codex
rg -n "sandbox|permission|network|secret|credential|auth|MCP|ACL|WFP|Seatbelt|Landlock|seccomp|bwrap|audit|signature|integrity" codex-rs

Set-Location D:\MobileWork\Ace
rg -n "security|sandbox|permission|network|secret|credential|auth|MCP|ACL|WFP|runtime|audit|signature|integrity" crew security-runtime desktop tests docs
```

### Ace verification

```powershell
Set-Location D:\MobileWork\Ace
python -m pytest -q --basetemp D:\MobileWork\Ace\pytest-codex-baseline tests\security tests\gateway\test_remote_auth.py tests\gateway\test_auth_contract.py
python -m pytest -q --basetemp D:\MobileWork\Ace\pytest-codex-baseline tests\security\test_execution_surface_inventory.py tests\security\test_release_security_workflows.py
cd desktop
npx tsc --noEmit
npm run build
```

### Codex verification

遵循 `D:\MobileWork\codex\AGENTS.md` 和仓库 `just` 入口；不要绕过仓库规定直接执行不受支持的 Cargo 测试命令。平台相关能力必须区分源码/协议测试和真实 OS runner 证据。

## Project structure

```text
docs/security/
├── codex-security-capability-baseline-spec.md   # 本规格
├── codex-security-capability-baseline.md        # 最终全量对照矩阵
├── codex-security-product-boundary-na-register.md # 人可读产品边界登记
├── codex-security-na-inventory.json              # 436-ID 适用性覆盖和禁符号规则
├── execution-surface-inventory.md               # Ace 执行面清单
└── security-test-matrix.md                      # Ace 现有原生 runtime 发布矩阵

codex-rs/
├── sandboxing/                                   # 跨平台 sandbox 编排
├── linux-sandbox/                                # Linux bwrap/namespace/proxy bridge
├── windows-sandbox-rs/                           # Windows token/ACL/WFP/Job/elevated
├── network-proxy/                                # 代理、MITM、policy、credential broker
├── exec-server/                                  # 进程和 capability filesystem 服务
├── rmcp-client/ / codex-mcp/                     # MCP transport、auth、隔离接入
├── core/                                         # 权限、审批、执行、工具路由
├── login/ / secrets/ / keyring-store/             # 身份、密钥和秘密存储
└── app-server*/                                  # 外部协议、认证和远程控制边界

Ace/
├── crew/security/                                # Python 策略、审批、broker、runtime client
├── crew/tools/                                   # 模型可达工具及其安全接入
├── crew/gateway/                                 # Gateway auth、API、owner/session 边界
├── security-runtime/                             # Ace 原生 OS runtime
├── desktop/                                      # Electron 主进程、preload、更新和 browser host
└── tests/security/                               # Ace 安全测试和发布门禁
```

## Code style for the baseline

每项使用短表格，不把推测写成事实：

```markdown
| ID | 能力 | Codex 证据 | Codex 边界 | Ace 证据 | 状态 | 缺口/整改 | 验证 |
|---|---|---|---|---|---|---|---|
| EXEC-SBX-001 | managed exec 必须进入 OS sandbox | `codex-rs/sandboxing/...` | backend 不可用则拒绝 | `crew/security/launch.py:...` | 不全面 | 移除 host fallback | managed/unavailable 集成测试 |
```

路径必须使用仓库相对路径并带行号；对未找到实现的项目写明搜索范围和结论。每项只描述一个可验证安全能力，避免把多个控制拼成不可验收的大项。

## Testing strategy

### Coverage layers

1. **Static inventory**：发现所有直接进程、网络、文件、密钥、IPC、更新和浏览器出口。
2. **Unit/policy tests**：验证解析、规范化、策略优先级、审批、grant、状态机和输入边界。
3. **Contract tests**：验证 Python/TypeScript/Rust 协议、token、frame、manifest、能力报告。
4. **Integration tests**：验证每个执行面 managed 成功、拒绝、runtime 不可用时的行为。
5. **Real OS tests**：在 Linux/macOS/Windows runner 验证实际文件、网络、进程和 ACL 边界。
6. **Release tests**：验证 commit、Cargo.lock、源代码、二进制、manifest、Desktop staging 和签名一致。
7. **Product-boundary tests**：验证 436-ID 总账、N/A/ACE_EQUIV 唯一性、缺面禁符号、
   弱失败保留适用，以及 Ace session/interaction MCP 的精确白名单。

### Definition of done for the baseline

- 每个 Codex 能力至少有一条源码证据和一条边界/失败行为说明。
- 每个 Ace 状态都有源码、测试或明确缺证据的依据。
- 所有直接执行面都能映射到某个安全边界；没有“未登记但可达”的出口。
- `符合` 项不能仅依赖注释、schema 或未调用模块。
- 文档明确区分 Codex 已实现、Codex 当前仓库不可证明、Ace 独有能力和 Ace 缺口。
- 当前已知的 Ace release artifact drift 必须被记录为发布阻断项，而不是被文档隐藏。
- 适用性总账覆盖全部 436 个唯一 ID；N/A 有可执行缺面门禁，ACE_EQUIV 有存在的
  `ACE-*` 目标，弱失败项不能被覆盖移除。

## Boundaries

### Always

- 以本地 Codex/Ace 当前源码和测试为准，所有结论带证据路径。
- 先追踪真实调用链，再判断模块能力。
- 将“正常路径”和“失败路径”分别验证。
- 将兼容模式、开发模式、未安装 runtime、制品漂移和权限拒绝作为独立状态。
- 不把安全能力的存在性误报为安全等价。

### Ask first

- 修改 Ace 运行代码或删除兼容路径。
- 变更 runtime 协议、权限模型、网络出口、Windows ACL 或发布工作流。
- 引入新依赖、改变打包方式、改变认证/多账号模型。

### Never

- 不覆盖或回滚当前工作树已有未提交改动。
- 不读取、粘贴或提交 `.env`、密钥、token、真实认证数据。
- 不为“让测试通过”手工修改 runtime hash、放宽 fail-closed 或删除安全测试。
- 不把文档推测写成 Codex 已实现的事实。

## Success criteria

1. 生成 `docs/security/codex-security-capability-baseline.md`，覆盖上述 20 个主题，目标至少 150 个原子能力项；若源码证据需要，允许超过 300 项。
2. 每项具有唯一 ID、Codex 证据、Ace 证据和三态判定。
3. 能从矩阵反查所有 Ace 直接执行面和安全入口。
4. 对 `符合`、`不全面`、`欠缺` 三类分别统计数量和比例。
5. 文档中明确列出所有发布阻断项、未验证项和需要真实平台 runner 的项。
6. 文档自检通过：路径存在、状态值合法、ID 唯一、章节齐全、没有无证据的“符合”。

## Resolved boundary decisions

- 当前 Codex 本地 checkout 仍是本版唯一源码基线。
- Ace 独有能力继续使用“无直接 Codex 对应”与 `ACE-*` ID，不把它压缩成错误的三态结论。
- 产品适用性立即接入 `tests/security/test_product_boundary_absent.py`；源码证据行号失效的
  更广泛 lint 仍可后续增加。
- 不新增 Code Mode、Codex 式入站执行 MCP、云控制面、Noise/remote-control 等已登记缺面；
  若产品决策改变，必须先复审 JSON、威胁模型和执行面清单。
