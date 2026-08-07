# security-runtime

Ace 的**原生安全运行时**（Rust，包名 `ace-security-runtime`）。
对托管（MANAGED）对话里的每一条命令，提供 OS 级的文件系统隔离、受限身份执行、
托管网络与进程树回收。是 crew/gateway 之外**唯一**以提升权限运行的可执行文件，
因此也是整个项目最敏感的信任边界。

> 对外口径：本运行时承载 Windows 原生沙箱、Linux bubblewrap 与 macOS Seatbelt；
> 它**共享主机内核**，是对话级隔离边界，不是防内核 0day 的强隔离（那是 Phase 3+ 的 gVisor/Firecracker）。

---

## 1. 能力总览

| 能力 | Windows | Linux | macOS |
|------|---------|-------|-------|
| 文件系统隔离 | 专用技术账户 + ACL lease（capability SID） | bubblewrap 选择性挂载 | Seatbelt 参数化可读/可写/拒绝根 |
| 受限身份执行 | 沙箱账户 + restricted token | 非 root + seccomp | 当前用户 + `deny default` Seatbelt profile |
| 进程树回收 | Kill-On-Close Job Object | 进程组 + bwrap | 独立进程组 + helper 整树终止 |
| 托管网络 | WFP 仅放行 loopback 代理 | 网络命名空间 + 用户态代理 | 仅放行本次随机 loopback 代理端口 |
| 保护元数据 | `.git` / `.agents` / `.crew` 强制 Deny | bwrap 只读覆盖 | Seatbelt deny 覆盖宽写根 |
| 身份持久化 | DPAPI 加密凭证 | 不需要 | 不需要 |
| 输出、stdin、协议鉴权 | 统一 v2 契约 | 同 | 同 |

### 1.1 Windows 后端要点

- **两个技术账户**（`identity::setup` 创建）：
  - **offline 账户**：WFP 全断网，仅做无网文件操作。
  - **online 账户**：WFP 仅放行固定 loopback 代理端口（`PROXY_PORT = 43119`），其余阻断。
- **ACL lease**（`acl::AclLease`）：每次命令前，按 `writable_roots` / `readable_roots` / `denied_roots` 精确下发 ACL；命令结束 `Drop` 里回收，跨进程 mutex 防账户间 ACL 串味。
- **restricted token**（`token::create_restricted_token`）：禁用最大特权 + LUA_TOKEN + WRITE_RESTRICTED，只保留 capability SID。
- **WFP 是安装期产物**：稳定 GUID，`install/uninstall/verify_installed` 幂等；过滤器 `FWPM_FILTER_FLAG_PERSISTENT`，重启仍在。
- **保护子目录**：可写根下的 `.git` / `.agents` / `.crew` 自动建目录并 Deny（含 capability SID），防模型篡改仓库元数据。

### 1.2 Linux 后端要点

- **bubblewrap**（`linux/bwrap.rs`）：优先用系统 `bwrap`，缺失时回落到随包 `bwrap`（`ACE_BUNDLED_BWRAP`）。`--ro-bind / /` + 可写根 `--bind` + 受保护路径 `--ro-bind` 覆盖。
- **seccomp**（`linux/seccomp.rs`）：`--inner-seccomp` 子命令在子进程内应用 syscall 过滤；网络默认关闭。
- **WSL**（`linux/wsl.rs`）：检测 WSL 版本；WSL1 不支持 user namespace → 拒绝沙箱执行（不静默降级）。
- **托管网络**：用户态代理（`network/`）+ seccomp 兜底。

### 1.3 macOS 后端要点

- **Seatbelt**（`macos/mod.rs`）：每次执行生成独立 profile，用户路径只通过 `-D` 参数传入。
- **最小环境**：从空环境重建 PATH、私有 HOME/TMPDIR 和已验证覆盖，避免继承宿主秘密。
- **托管网络**：离线 profile 没有 outbound allow；在线 profile 只能访问本次代理的精确 loopback 端口。
- **无安装步骤**：不创建技术账号、不写系统防火墙 state，也不需要管理员授权。

### 1.4 协议（`protocol.rs`）

NDJSON over stdio，**版本化 + 鉴权 + 防重放**：

```
runtime 启动 → 读 ACE_SECURITY_RUNTIME_TOKEN（≥32 字节）
            → stdout 写 {type:"ready", version:2,
                          capabilities:["stdin_once","stream_output"]}
host 每行一个请求：
  {version:2, token, nonce,
   request:{op:"run", command, cwd, writable_roots, ...,
            stdin_b64?, env_overrides?}}
  → token 不符 → sandbox_denied
  → nonce 重复或过短 → sandbox_denied
  → 版本不符 → runtime_protocol_mismatch
runtime 每请求回连续事件：
  started(seq=0, pid?, capabilities)
  → stdout|stderr(seq++, data_b64)*
  → completed(seq++, exit_code) | error(seq++, code, message)
```

只有启动/设置失败可在 `started` 前返回 `error(seq=0)`。每帧都携带相同
`version/nonce`，`seq` 必须从 0 严格递增；终态后必须 EOF。Python 调用方只在完整校验后
得到最终 `RuntimeCommandResult`，不会收到原始输出 chunk。

`run` 请求字段：`command[]`, `cwd`, `writable_roots[]`, `readable_roots[]`,
`denied_roots[]`, `network_enabled`, `network_rules[]`, `allow_local_binding`,
`max_output_bytes`, `stdin_b64?`, `env_overrides?`。

固定边界：请求帧 2 MiB、响应帧 128 KiB、单输出 chunk 64 KiB、stdin 1 MiB、
环境变量名值合计 256 KiB、默认 stdout+stderr 总量 2 MiB。stdin 只写一次并立即关闭；
未提供 stdin 时子进程获得关闭/空输入。`env_overrides` 只进入最终受限子进程，不进入
runtime/runner；`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`/`NO_PROXY`、
`ACE_SECURITY_*` 和 `ACE_BUNDLED_*` 不允许由调用方覆盖，runtime
生成的代理与安全变量拥有最终优先级。

---

## 2. 环境需求

### 2.1 运行时（普通同事，免 Rust）

| 平台 | 要求 |
|------|------|
| Windows | 10/11 x64；首次开启沙箱需 **UAC 管理员**授权一次（建账户 + 装 WFP） |
| Linux | x64；`bwrap` 可执行文件（系统装或随包）；内核支持 user namespace |
| macOS | `/usr/bin/sandbox-exec`；运行组件随 Desktop 包提供，无安装步骤 |

普通同事**不需要 Rust 工具链**——直接用仓库里 `security-runtime/bin/` 的预编译产物（见 §4）。

### 2.2 编译（改了 Rust 源码的同事）

| 平台 | 工具链 |
|------|--------|
| Windows | `rustup` + **MSVC**（Visual Studio 2022 BuildTools，含 `cl.exe`/`link.exe`）；`vcvarsall.bat` 配好后 `cargo check` 须能跑通 |
| Linux | `rustup` + `gcc`/`libc6-dev`；`bubblewrap` 装好（测试需要） |
| macOS | Rust stable + Xcode Command Line Tools；真实测试不能嵌套在另一个 Seatbelt 会话中 |

依赖见 `Cargo.toml`：`serde/serde_json/rand/base64`（通用）；`libc/seccompiler/sha2`（Linux）；`windows-sys = "0.52"`（Windows，**勿随意升级**——0.59+ 会迁 API 路径，见变更记录）。

---

## 3. 编译

### 3.1 一键脚本（推荐）

```powershell
# Windows
.\scripts\build-security-runtime.ps1
```
```bash
# Linux/macOS（生成当前主机原生产物）
./scripts/build-security-runtime.sh
```

脚本做三件事：`cargo build --release` → 复制到 `security-runtime/bin/` → 重算
`runtime-manifest.json` 的 `source_hash`。协议 v2 不兼容 v1，Python 源码、runtime
二进制和 manifest 必须作为同一发布单元更新；不允许协议降级或 managed 失败后回退 host。

### 3.2 手动

```bash
cd security-runtime
cargo build --release                         # 本机默认 target
cargo test                                    # 跑契约测试（见 §6）
```

Windows 显式三元组：`cargo build --release --target x86_64-pc-windows-msvc`。

产物路径：`target/<triple>/release/ace-security-runtime[.exe]`。

> ⚠️ 手动 `cargo build` 不会更新 `bin/runtime-manifest.json`。启动时 Python/Desktop
> 会用 manifest 里的 `binary_sha256` / `source_hash` 做完整性校验（fail-closed），
> 二进制与 manifest 不匹配则**拒绝运行**。手动构建后请务必跑一遍 §3.1 脚本，或把
> 产物覆盖到 `security-runtime/bin/ace-security-runtime[.exe]` 并重算 manifest。

### 3.3 自编译替代预编译产物

仓库 `security-runtime/bin/` 里的预编译二进制是**便捷产物**（让不装 Rust 的人也能跑）。
若你想自行验证或从源码编译，跑 §3.1 脚本即可用你自己的构建覆盖它--脚本会同步重算
manifest，完整性校验自动通过。也可设 `ACE_SECURITY_RUNTIME` 环境变量指向任意绝对路径
的自构建二进制，跳过仓库内预编译产物。

---

## 4. 分发（团队免 Rust 方案）

`security-runtime/bin/` 提交预编译产物，让团队成员**不装 Rust、不设环境变量**即可启动：

```
security-runtime/bin/
├── ace-security-runtime.exe       # Windows x86_64-pc-windows-msvc
├── ace-security-runtime           # Linux ELF（需 Linux 同事/CI 跑一次 .sh 后提交）
└── runtime-manifest.json                 # source_hash + 元信息
```

### 4.1 gateway / desktop 如何找到它

- **Python gateway**（`crew/security/launch.py:packaged_runtime_argv`）：
  优先 `ACE_SECURITY_RUNTIME` 环境变量（绝对路径），否则回落到 `<repo>/security-runtime/bin/<name>`。
- **桌面 dev**（`desktop/src/main/index.ts` `security:setup`）：同上回落，`repoRoot()/security-runtime/bin/`。
- **打包态**：从 `process.resourcesPath/` 取随包 exe + 打包 manifest 哈希校验（`packagedSecurityRuntimeEnv`）。

### 4.2 漂移检测（防"改了源码忘重 build"）

启动时 gateway 重算 `security-runtime/{src,tests}/**/*.rs + Cargo.toml` 的 SHA256，与 `bin/runtime-manifest.json` 的 `source_hash` 对账；不一致 → `/api/security/capabilities` 返回 `runtime_stale=true`，桌面 banner 显示：

> 🔄 runtime 二进制落后于 Rust 源码：改了 security-runtime/ 需重跑 scripts/build-security-runtime 再提交

**结论：凡修改本目录下任何 `.rs` 或 `Cargo.toml`，必须跑 §3.1 脚本并 `git add security-runtime/bin/`。**

---

## 5. 启动与集成

### 5.1 Desktop 日常启动（无感）

```powershell
cd desktop
npm start
```

Desktop 会自动启动带安全状态目录环境的托管 Gateway，并从
`security-runtime/bin/` 找到 runtime。首次运行时，对话框上方会提示「请安装安全沙箱」，
点击「安装安全沙箱」→ 同意 UAC → 完成安装后即可使用受管命令执行。

`python -m crew.gateway.server` 是 Web 端的独立 Gateway 启动方式；若手动启动的 Gateway
要与 Desktop 复用，必须额外设置与 Desktop `userData/security/` 相同的
`ACE_SECURITY_STATE_DIR`，否则它无法读取 Desktop 完成的沙箱安装状态。详见
`docs/security/docker-sandbox-poc.md`。

### 5.2 CLI 子命令（安装/调试用，正常对话不走这些）

```
ace-security-runtime                                  # 默认：进入 NDJSON 协议主循环（需 ACE_SECURITY_RUNTIME_TOKEN）
ace-security-runtime --windows-setup <stateDir>       # 创建两个技术账户 + 装 WFP（需 UAC）
ace-security-runtime --windows-uninstall <stateDir>   # 卸账户 + 删 WFP
ace-security-runtime --windows-runner                 # 沙箱账户内的子运行器（内部）
ace-security-runtime --inner-seccomp ...              # Linux seccomp 内层（内部）
```

`<stateDir>` 默认 `<userData>/security/`，存 `windows-sandbox-identity.json`（CryptProtectData 加密的账户凭证，version=3 表示 setup 已完成）。

### 5.3 协议调用方

`crew/security/runtime_client.py:NativeRuntimeClient.execute`：spawn runtime → 校验 v2
`ready` 能力 → 发一个 `run` 请求 → 严格收集 `started/output/terminal/EOF` → 返回最终
结果。一个 monotonic deadline 覆盖完整交换；timeout、cancel、协议错误和输出超限均回收
helper/沙箱进程树。host 侧不直接碰沙箱账户/WFP/bwrap。

---

## 6. 测试

```bash
cd security-runtime
cargo test --target x86_64-pc-windows-msvc    # Windows
cargo test                                    # Linux
cargo test                                    # macOS（包含 Seatbelt 对抗测试）
```

契约测试（`tests/`）覆盖：
- `protocol.rs` — v2 事件形状、输入/帧限制、token/nonce 防重放。
- `windows_acl.rs` — ACL lease 仅合并/回收 Ace 自己的 principal。
- `windows_job.rs` — Job Object kill-on-close，Resume 前已 assign。
- `windows_readiness.rs` — native gate 要求已安装 identity fixture。
- `windows_sandbox.rs` — 专用账户写工作区、不写 denied/protected，以及 runner
  stdin/environment/流式协议；真实账户测试需配置
  `ACE_WINDOWS_NATIVE_STATE_DIR`。
- `windows_token.rs` — restricted token 与 handle 契约。
- `linux_bwrap.rs` / `linux_adversarial.rs` — bwrap 隔离、一次性 stdin、环境变量、
  实时 stdout/stderr、共享输出上限与对抗用例；必须在 Linux 运行，Windows 交叉编译
  不能替代运行证据。
- `macos_adversarial.rs` — Seatbelt 工作区写入、外部读取/写入和受保护目录拒绝；
  `tests/security/security_matrix.py --platform macos` 另测离线直连与规则代理。

---

## 7. 目录结构

```
security-runtime/
├── Cargo.toml                 # windows-sys = "0.52"，勿随意升
├── bin/                       # 预编译产物 + manifest（提交入库）
├── src/
│   ├── main.rs                # CLI 分发 + 协议主循环
│   ├── protocol.rs            # NDJSON 协议、鉴权、防重放
│   ├── network/               # 托管网络代理（connector/policy/proxy）
│   ├── windows/               # Windows 后端（identity/process/token/acl/wfp/job/readiness）
│   ├── linux/                 # Linux 后端（bwrap/seccomp/wsl/proxy_routing）
│   └── macos/                 # macOS 后端（Seatbelt/受管代理/进程回收）
└── tests/                     # 契约测试（见 §6）
```

---

## 8. 安全约束（贡献者必读）

1. **windows-sys 不随意升级**：0.52 的符号路径与 0.59+ 不同；升级会引入 API 迁移，需全量重测。
2. **改源码必须重 build**：否则 `bin/` 的 exe 与源码漂移，漂移检测会让所有人的 banner 报 stale。
3. **WFP GUID 稳定**：`wfp.rs` 里 7 个 GUID 是安装期锚点，**不可改**（改了会导致旧过滤器残留）。
4. **`bin/` 的 exe 以 UAC 运行**：code review 时改本目录的 PR 必须走严格审查——这是供应链信任的落点。
5. **协议与产物原子升级**：v2 不提供 v1 兼容或 host fallback；修改帧语义必须提升
   `PROTOCOL_VERSION`，同时更新 Python、Rust、测试、预编译产物和 manifest。

---

## 变更记录

- **2026-08-06**：新增 macOS Seatbelt 文件隔离、精确 loopback 代理联网边界、进程树清理、
  Gateway live probe、安全中心平台展示、DMG runtime staging 与真实 macOS runner 发布证据。
- **2026-07-26**：协议升级为 v2 流式事件；新增一次性 stdin/EOF、受限子进程环境变量、
  全局序列、started/stdout/stderr/completed/error、严格帧与输出边界，以及 Windows
  runner/Linux bwrap 的实时输出和整树清理。Windows Rust 测试与 Linux 目标交叉编译已通过；
  Linux 原生 bwrap/安全矩阵仍须由 Linux CI 提供发布证据。同步修复 Windows 构建脚本
  manifest 生成器未把 here-string 接到 `python -` stdin、导致无控制台环境进入 pyrepl
  循环的问题。
- **2026-07-24**：windows-sys 符号路径修正（`DATA_BLOB`→`CRYPT_INTEGER_BLOB`、`OpenProcessToken`/`CreateProcessWithLogonW` 实在 `System::Threading`、`FWP_E_*` 在 `Foundation`、`INFINITE` 在 `System::Threading`），`cargo check`/`cargo test` 全绿；未升级依赖。
- **2026-07-24**：新增 `bin/` 预编译分发 + `runtime-manifest.json` 源码哈希漂移检测 + `scripts/build-security-runtime.{ps1,sh}`；桌面加 Codex 式沙箱 banner。
