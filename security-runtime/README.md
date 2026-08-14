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
| 保护元数据 | 读取 ACE + deny-write ACE | bwrap 只读覆盖 | Seatbelt 写拒绝覆盖宽写根 |
| 身份持久化 | DPAPI 加密凭证 | 不需要 | 不需要 |
| 输出、stdin、协议鉴权 | 统一 v3 契约 | 同 | 同 |

### 1.1 Windows 后端要点

- **两个技术账户**（`identity::setup` 创建）：
  - **offline 账户**：WFP 全断网，仅做无网文件操作。
  - **online 账户**：WFP 仅放行固定 loopback 代理端口（`PROXY_PORT = 43119`），其余阻断。
- **ACL lease**（`acl::AclLease`）：每次命令前，按 `writable_roots` / `readable_roots` / `readonly_roots` / `denied_roots` 精确下发 ACL；命令结束 `Drop` 里回收，跨进程 mutex 防账户间 ACL 串味。
- **restricted token**（`token::create_restricted_token`）：禁用最大特权 + LUA_TOKEN + WRITE_RESTRICTED，只保留 capability SID。
- **WFP 是安装期产物**：稳定 GUID，`install/uninstall/verify_installed` 幂等；过滤器 `FWPM_FILTER_FLAG_PERSISTENT`，重启仍在。
- **保护子目录**：可写根下的 `.git` / `.agents` / `.crew` 自动进入只读 carve-out；账户与 capability SID 可以读取，但写入和删除被显式拒绝。

### 1.2 Linux 后端要点

- **bubblewrap**（`linux/bwrap.rs`）：优先用系统 `bwrap`，缺失时回落到随包 `bwrap`（`ACE_BUNDLED_BWRAP`）。`--ro-bind / /` + 可写根 `--bind` + 受保护路径 `--ro-bind` 覆盖。
- **seccomp**（`linux/seccomp.rs`）：`--inner-seccomp` 子命令在子进程内应用 syscall 过滤；网络默认关闭。
- **WSL**（`linux/wsl.rs`）：检测 WSL 版本；WSL1 不支持 user namespace → 拒绝沙箱执行（不静默降级）。
- **托管网络**：用户态代理（`network/`）+ seccomp 兜底。

### 1.3 macOS 后端要点

- **Seatbelt**（`macos/mod.rs`）：每次执行生成独立 profile，用户路径只通过 `-D` 参数传入。
- **只读 carve-out**：对每个只读目标的 literal 与 subpath 拒绝写入；不存在的 `.git` / `.agents` / `.crew` 也不能借父级写权限创建。
- **受控环境**：从空环境重建 PATH 与 TMPDIR。内置终端在广泛只读基线中使用宿主 HOME 解析用户路径，但不因此获得额外写权限；携带凭据投影的外援仍使用私有 HOME。
- **托管网络**：离线 profile 没有 outbound allow；在线 profile 只能访问本次代理的精确 loopback 端口。
- **无安装步骤**：不创建技术账号、不写系统防火墙 state，也不需要管理员授权。

### 1.4 协议（`protocol.rs`）

NDJSON over stdio，**版本化 + 鉴权 + 防重放**：

```
runtime 启动 → 读 ACE_SECURITY_RUNTIME_TOKEN（≥32 字节）
            → stdout 写 {type:"ready", version:3,
                          capabilities:["stdin_once","stream_output","readonly_roots","full_disk_read"]}
host 每行一个请求：
  {version:3, token, nonce,
   request:{op:"run", command, cwd, writable_roots, ...,
            stdin_b64?, env_overrides?}}
  → token 不符 → runtime_protocol_mismatch
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

交互式外援使用同一条已鉴权的 NDJSON 控制通道，但子进程的 ACP/CLI
协议保持不透明：

    host → {request:{op:"interactive_open", command, cwd, ...}}
          ← started(seq=0, ...)
    host → {request:{op:"interactive_write", data_b64}}
          ← stdout|stderr(seq++, data_b64)*
    host → {request:{op:"interactive_close"}}
          ← completed(seq++, exit_code) | error(seq++, code, message)

interactive_write 可以重复发送；interactive_close 只关闭外援子进程
的 stdin，不改变 stdout/stderr 的事件格式。stdin_bidirectional 是
交互式会话能力标志；旧的只支持一次性 run 的 helper 会在打开会话前
被拒绝，不会回退到宿主直接启动。

Crew 的 `crew-interaction` MCP proxy 属于交互式会话的受控回调：MCP
声明中的一次性 `CREW_INTERACTION_*` 变量只用于当前 binding，Native
Runtime 另外接收一个由 Gateway 生成的精确 loopback `host:port` 网络权限。
该权限不等于开放任意 localhost，也不等于开放公网；没有这个系统权限时，
`ask_followup_question` 会安全失败。

### 1.4 外援凭据与 HOME 边界

macOS Seatbelt 不会默认把宿主用户的 `HOME` 暴露给外援。Native Runtime
为每个子进程创建私有临时 HOME，避免外援顺着 `~` 读取宿主配置、密钥和登录态。
受管 profile 启用 `full_disk_read` 且请求没有 `home_files` 时，runtime 把宿主 HOME 作为
子进程 HOME，使 `~/Desktop` 与结构化文件工具指向同一宿主路径；文件仍只按 profile/overlay
获得读写能力，HOME 本身不会变成可写根。携带 `home_files` 的外援始终使用私有 HOME。
`TMPDIR` 继续由平台沙箱控制，不会因为复用 HOME 而改变临时文件边界。

Crew Home 内的数据库、认证密钥、配置凭据和日志仍由不可升级的精确 deny 根保护；
任务 workspace 不再被其父目录 deny 覆盖。受控模式下，运行时 descriptor 可以声明宿主 HOME
下的相对配置文件；宿主侧只读取这些已声明且存在的普通文件。Kimi（`.kimi-code/config.toml`、
OAuth 文件与 credentials JSON）、Codex、Hermes、Claude Code 的路径只是内置 descriptor 的
兼容默认值，不是安全核心里的 Kimi 专用分支；已发现并持久化的 runtime metadata 可以覆盖
`credential_home_paths` 和 `network_endpoints`，自定义 runtime 不会继承内置路径。Native Runtime
通过 `home_files` 写入一次性的私有 HOME，进程结束即清理。该投影不接受绝对路径、`..`、
目录复制或整个 HOME，因此已登录 Kimi、已配置 Hermes 可以继续工作，但不会让外援获得宿主
HOME 的通用读取权。`external_agents.security_enabled` 默认是 `false` 时不执行 HOME 投影，
外援按旧 runtime 直接使用当前用户环境；只有显式打开后才进入上述受管投影。未声明配置的
受管外援仍按自身认证错误返回，ACP 适配器不会把它误报成 `native runtime closed the
protocol stream`。

`run` 请求字段：`command[]`, `cwd`, `writable_roots[]`, `readable_roots[]`,
`readonly_roots[]`, `denied_roots[]`, `full_disk_read`, `network_enabled`, `network_rules[]`, `allow_local_binding`,
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

普通同事**不需要 Rust 工具链**——Desktop 会自动选择仓库里与当前平台和架构一致的预编译产物（见 §4）。

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
`runtime-manifest.json` 的 `source_hash`。协议 v3 增加全局只读能力标记，不兼容旧版协议；Python 源码、runtime
二进制和 manifest 必须作为同一发布单元更新；不允许协议降级或 managed 失败后回退 host。

### 3.2 手动

```bash
cd security-runtime
cargo build --release                         # 本机默认 target
cargo test                                    # 跑契约测试（见 §6）
```

Windows 显式三元组：`cargo build --release --target x86_64-pc-windows-msvc`。

产物路径：`target/<triple>/release/ace-security-runtime[.exe]`。

#### Intel Mac（x86_64）首次编译

仓库目前提交了 Apple Silicon（`darwin-arm64`）预编译文件。若尚未从 GitHub 的
`security-prebuilt` workflow/Release 取得 `darwin-x64` 产物，Intel Mac 需要本机编译一次：

```bash
# 确认输出为 x86_64
uname -m

# 首次安装工具链
xcode-select --install
brew install rustup
export PATH="$(brew --prefix rustup)/bin:$PATH"
rustup default stable

# 在 Ace 仓库根目录执行
cargo build \
  --manifest-path security-runtime/Cargo.toml \
  --release \
  --locked

node desktop/scripts/prepare-security-runtime.mjs \
  --runtime security-runtime/target/release/ace-security-runtime \
  --output desktop/security-runtime-bin

node desktop/scripts/verify-security-runtime.mjs \
  desktop/security-runtime-bin

npm run dev --prefix desktop
```

`desktop/security-runtime-bin/` 是被 Git 忽略的本机 staging 目录，不会污染提交。Intel
维护者若要把验证后的产物提供给所有 Intel 同事，可将 `--output` 改成
`security-runtime/prebuilt/darwin-x64`，并额外传入
`--source-root security-runtime`；随后提交该目录中的二进制、manifest 和环境描述文件。

> ⚠️ 手动 `cargo build` 不会更新 runtime 旁边的 `runtime-manifest.json`。启动时 Python/Desktop
> 会用 manifest 里的 `binary_sha256` / `source_hash` 做完整性校验（fail-closed），
> 二进制与 manifest 不匹配则**拒绝运行**。本机开发请通过
> `desktop/scripts/prepare-security-runtime.mjs` staging；提交团队预编译文件时必须增加
> `--source-root security-runtime`，将源码摘要一起写入 manifest。

### 3.3 自编译替代预编译产物

仓库 `security-runtime/prebuilt/<platform>-<arch>/` 里的预编译二进制是**便捷产物**（让不装 Rust 的人也能跑）。
若你想自行验证或从源码编译，可按 §3.2 构建并 staging。也可设
`ACE_SECURITY_RUNTIME` 环境变量指向带有效 manifest 的绝对路径；显式路径不会绕过摘要、
平台或架构校验。

---

## 4. 分发（团队免 Rust 方案）

`security-runtime/prebuilt/` 按平台和架构提交预编译产物，让团队成员**不装 Rust、不设环境变量**即可启动：

```
security-runtime/prebuilt/
├── darwin-arm64/
│   ├── ace-security-runtime       # Apple Silicon Mach-O
│   └── runtime-manifest.json      # 平台、架构、二进制与源码摘要
├── darwin-x64/                    # Intel Mac 可按相同结构扩展
└── linux-x64/                     # Linux 可按相同结构扩展
```

旧版 `security-runtime/bin/` 不再进入运行时候选，避免过期的跨平台二进制被误加载。开发态只使用当前平台的 `prebuilt/<platform>-<arch>` 或本机 staging；正式产物在对应平台的发布 runner 上重新构建和测试，配置证书时再签名。

`.github/workflows/security-prebuilt.yml` 是四平台统一产物入口：`darwin-arm64`、`darwin-x64`、
`linux-x64`、`win32-x64` 都在对应原生 runner 构建和测试，再生成 tar.gz、SHA-256 manifest 与
GitHub artifact attestation。`v*` tag 会把四份产物和 `SHA256SUMS` 上传到 GitHub Release。
Apple/Windows 证书 secrets 未配置时流程仍正常完成；配置后才会给 helper 增加 Developer ID
或 Authenticode 签名。证书私钥只能放 GitHub Actions secrets，不能提交到仓库。

这两类证书都不要求把应用发布到应用商店。只通过 GitHub 分发源码或未签名开发包时，
维护者现在不必申请证书；代价是首次下载后 macOS Gatekeeper 或 Windows SmartScreen 可能显示
未知发布者警告。如果希望普通用户双击即用，再申请 Apple Developer Program 的 Developer ID
并完成公证，或为 Windows 配置受信任的 Authenticode 签名。当前 workflow 只实现 helper 的
可选签名；完整桌面应用的 macOS 公证仍属于正式发行流程。

### 4.1 gateway / desktop 如何找到它

- **Python gateway**（`crew/security/launch.py:packaged_runtime_argv`）：
  优先 `ACE_SECURITY_RUNTIME` 环境变量和 Desktop staging，否则只选择 `<repo>/security-runtime/prebuilt/<platform>-<arch>/<name>`；不会跨平台回落到旧版 `bin/`。
- **桌面 dev**（`desktop/src/main/index.ts`）：使用相同的平台/架构选择规则，并在启动 Gateway 前校验 manifest。
- **打包态**：从 `process.resourcesPath/` 取随包 exe + 打包 manifest 哈希校验（`packagedSecurityRuntimeEnv`）。

### 4.2 漂移检测（防"改了源码忘重 build"）

启动时 gateway 重算 `security-runtime/{src,tests}/**/*.rs + Cargo.toml + Cargo.lock` 的 SHA256，与所选预编译目录中 `runtime-manifest.json` 的 `source_hash` 对账；不一致 → `/api/security/capabilities` 返回 `runtime_stale=true`，桌面 banner 显示：

> 🔄 runtime 二进制落后于 Rust 源码：改了 security-runtime/ 需重跑 scripts/build-security-runtime 再提交

**结论：凡修改本目录下任何 `.rs`、Rust 测试、`Cargo.toml` 或 `Cargo.lock`，必须重建受影响平台的 runtime，并原子提交对应 prebuilt 目录。**

---

## 5. 启动与集成

### 5.1 Desktop 日常启动（无感）

```powershell
cd desktop
npm start
```

Desktop 会自动启动带安全状态目录环境的托管 Gateway，并从本地 staging 或
`security-runtime/prebuilt/<platform>-<arch>/` 找到 runtime。macOS 无需安装步骤；Windows 首次运行时会提示安装安全沙箱，
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

`crew/security/runtime_client.py:NativeRuntimeClient.execute`：spawn runtime → 校验 v3
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
- `protocol.rs` — v3 事件形状、输入/帧限制、token/nonce 防重放。
- `windows_acl.rs` — ACL lease 仅合并/回收 Ace 自己的 principal。
- `windows_job.rs` — Job Object kill-on-close，Resume 前已 assign。
- `windows_readiness.rs` — native gate 要求已安装 identity fixture。
- `windows_sandbox.rs` — 专用账户写工作区、读取但不修改 readonly、不访问 denied，以及 runner
  stdin/environment/流式协议；真实账户测试需配置
  `ACE_WINDOWS_NATIVE_STATE_DIR`。
- `windows_token.rs` — restricted token 与 handle 契约。
- `linux_bwrap.rs` / `linux_adversarial.rs` — bwrap 隔离、一次性 stdin、环境变量、
  实时 stdout/stderr、共享输出上限与对抗用例；必须在 Linux 运行，Windows 交叉编译
  不能替代运行证据。
- `macos_adversarial.rs` — Seatbelt 工作区写入、显式 denied 外部路径拒绝和元数据可读不可写；
  `tests/security/security_matrix.py --platform macos` 另测离线直连与规则代理。

---

## 7. 目录结构

```
security-runtime/
├── Cargo.toml                 # windows-sys = "0.52"，勿随意升
├── prebuilt/                  # 按 platform-arch 分隔的预编译产物 + manifest
├── bin/                       # 旧版预编译目录（仅保留历史文件，不再参与运行时发现）
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
2. **改源码必须重 build**：否则 prebuilt runtime 与源码漂移，漂移检测会让对应平台的 banner 报 stale。
3. **WFP GUID 稳定**：`wfp.rs` 里 7 个 GUID 是安装期锚点，**不可改**（改了会导致旧过滤器残留）。
4. **Windows runtime 以 UAC 运行**：code review 时改本目录的 PR 必须走严格审查——这是供应链信任的落点。
5. **协议与产物原子升级**：v3 不提供旧版兼容或 host fallback；修改帧语义必须提升
   `PROTOCOL_VERSION`，同时更新 Python、Rust、测试、预编译产物和 manifest。

---

## 变更记录

- **2026-08-10**：新增跨平台 `readonly_roots` 协议能力；Linux、Windows、macOS 统一实现
  可写根内的只读 carve-out，live probe 与真实平台矩阵验证项目元数据可读不可写。
- **2026-08-10**：新增 `prebuilt/<platform>-<arch>` 分发结构和 Apple Silicon 预编译 runtime；
  Desktop/Gateway 自动选择当前架构并校验平台、架构、二进制摘要与源码摘要，补充 Intel Mac
  本机编译与 staging 流程。
- **2026-08-10**：Codex app-server 与 Claude stream-json 已迁移到 Native Runtime interactive
  transport；`external_agents.security_enabled=true` 时，两个协议的双向 stdin/stdout、凭据
  投影、workspace 内 MCP 配置、精确网络权限和取消/进程树清理均经过 managed boundary，失败不
  回退宿主直启；关闭开关仍保留旧 runtime 兼容路径。
- **2026-08-06**：新增 macOS Seatbelt 文件隔离、精确 loopback 代理联网边界、进程树清理、
  Gateway live probe、安全中心平台展示、DMG runtime staging 与真实 macOS runner 发布证据。
- **2026-08-10**：补齐 Codex app-server 事件流退出与 approval 参数契约：reader 在子进程 EOF 后向事件队列发送内部 `$/processExited`，消费者立即报告退出码与受限 stderr 尾部；approval 将规范化后的真实 `item` 传入统一权限分类器，使 shell `command` 可被检查，不再因参数层级错误被误判为缺失。
- **2026-08-10**：修复 Native Proxy 的 HTTP 响应收尾死锁：任一方向复制遇到 EOF 后向对端传播 TCP 写半关闭，另一方向仍可继续排空；上游使用 `Connection: close` 时客户端能及时收到 EOF，不再在已收到完整响应后等待到超时。
- **2026-08-10**：补齐运行时描述的精确网络 endpoint 契约：Detector 将宿主维护的 `network_endpoints` 与凭据文件布局一并持久化，ACP/CLI 适配器统一合并描述声明和投影配置中的 URL；Kimi 描述声明其 OAuth 刷新服务 `https://auth.kimi.com`，无 provider 条件分支、无需用户逐任务配置。Native Proxy 对未声明目标立即返回 HTTP 403，不再让外援无提示高速重试直至超时。
- **2026-08-10**：修复 managed ACP 超时诊断被清理流程覆盖：Native Runtime 长连接读取超时统一转换为可读的 ACP 错误并完成 pending request，使 adapter 能保留调用阶段与 stderr，而不是在 <code>close()</code> 时泄漏裸 <code>TimeoutError</code>。
- **2026-08-10**：统一 managed 网络代理环境：macOS、Linux、Windows 均由 Native Runtime 在宿主环境覆盖之后写入标准代理变量与 `NODE_USE_ENV_PROXY=1`，使 Node 24+ CLI 自动走同一受控代理；非 Node 外援忽略该变量，外援不能覆盖或绕过 runtime-owned 代理地址。
- **2026-08-10**：修复 macOS Native Proxy 的 CONNECT 隧道中断：代理监听器仍用 non-blocking 模式响应停止信号，但每个 accepted socket 在进入有超时边界的双向转发前统一恢复 blocking，避免 macOS 继承监听器状态后把 CONNECT 与 TLS ClientHello 之间的短暂空档误判为转发结束；该修复不增加任何网络权限。
- **2026-08-10**：补齐 macOS managed 外援的文件监听系统能力：Seatbelt 仅放行精确 `com.apple.FSEvents` Mach service，修复 Node/Kimi 在允许的 workspace 上创建 watcher 时被映射为 `EMFILE` 并提前退出；文件内容读写、网络与其他 Mach service 仍按原规则拒绝。
- **2026-08-10**：统一外援安全开关默认值为关闭：`external_agents.security_enabled=false` 时 ACP、Codex app-server 和 Claude Code 继续使用旧 runtime 直联；打开后才启用 Native Runtime。外援凭据路径继续由通用 descriptor/metadata 声明，不把 Kimi HOME 写死到安全核心。
- **2026-08-10**：外援网络权限从已投影、宿主声明的配置文件中提取精确 HTTPS endpoint，并与 Interaction MCP 的精确 loopback 权限合并；不按 provider 写死域名，拒绝远程明文 HTTP、通配域名与模型输入追加的目标。
- **2026-08-10**：兼容系统代理/VPN 的 fake-IP DNS：只有已命中精确域名规则时才允许其解析到 RFC 2544 `198.18.0.0/15` 合成地址；直接声明该 IP 网段仍按私网拒绝，RFC1918、loopback 与云元数据保护不变。
- **2026-08-09**：补齐 managed 外援脚本运行时依赖边界：Security Broker 静态解析入口 shebang、Python `pyvenv.cfg` 与 editable-install 元数据，只把 venv、基础环境动态库目录和明确声明的包目录加入只读根；工作区脚本不触发推导，原生二进制不额外放行，也不开放用户 Home。Hermes venv ACP 可在受控 cwd 下启动，同时保留 Native Runtime 的显式可写根校验。
- **2026-08-09**：收窄 Crew Home 的保护范围：不再把包含 `accounts/*/task_workspaces` 的整个父目录作为不可升级 deny，改为保护数据库、认证密钥、配置凭据和日志等精确路径，修复 macOS Seatbelt 下外援进程在 `os.getcwd()` 阶段被父级 deny 拒绝的问题。
- **2026-08-09**：修复 managed 外援的工作目录安全上下文断点：未绑定本地目录的工作空间现在由 Gateway 与 SingleAgent 共同使用 Owner 私有 task workspace 作为显式 `writable_root`；外部 session 的隔离子目录因此可以安全进入 Native Runtime。Team 子任务只从父 `ProcessLaunch` 继承覆盖当前 cwd 的最具体可写根，找不到匹配根仍 fail closed；不按 Kimi/Hermes 或其他 provider 增加分支。
- **2026-08-09**：补齐受控外援的凭据/模型配置投影：运行时描述以相对路径声明需要的宿主配置，统一 RuntimeAdapter 读取最小文件集合并通过 Native Runtime `home_files` 临时写入私有 HOME；接入 Kimi、Codex、Hermes 与 Claude Code 的跨平台文件配置，没有 Provider 分支、没有整个 HOME 放行，Python/Rust/三平台后端和边界测试同步更新。Claude Code 在 macOS 默认使用系统 Keychain，文件投影不绕过 Keychain 权限。
- **2026-08-07**：新增通用 managed interactive stdio transport；当前 ACP 在不接管其内部协议
  的前提下，通过 Native Runtime 维持双向 stdin/stdout，CLI 可复用同一接口；同时加入
  `stdin_bidirectional` 能力校验、Team 安全启动上下文继承，以及 Native Runtime 控制环境变量保护。
- **2026-08-08**：补齐 Crew `ask_followup_question` 的 `crew-interaction` MCP 回调；父外援环境
  不再携带 ACE/沙箱控制变量，但 MCP 声明中的短期 binding 环境仍保留，并只为当前 Gateway
  loopback 地址追加受控网络权限。
- **2026-08-09**：修复 macOS managed 外援的 HOME 边界：仅在权限 profile 明确覆盖用户 Home
  时复用真实 HOME，TMPDIR 仍保持私有临时目录；Kimi ACP 能读取已登录状态，且 ACP reader
  透传 native runtime EOF 的原始错误，避免把认证失败误报为协议流关闭。补齐跨平台 native runtime
  构建链路：新增 macOS Seatbelt 平台说明、按主机
  自动选择 Rust target 的构建脚本，以及同时兼容 Gateway 完整性检查和 Desktop staging
  校验的 manifest。macOS 不再错误寻找 Windows `.exe` 制品；managed 外援仍保持 native
  runtime 缺失即拒绝启动，不回退宿主直启。
- **2026-08-09**：补齐 macOS Seatbelt 的通用可用性探测：Gateway 能对 Darwin 执行 live
  capability probe；Native Runtime 在发送 `started` 前验证 Seatbelt 能否真正应用，失败时
  返回带系统诊断的 `sandbox_unavailable`，避免 ACP/CLI 只看到 protocol stream EOF；preflight
  与实际 managed profile 共用 `deny default` / `system.sb` 基础，避免探针自身偏离真实边界。
- **2026-07-26**：协议升级为 v2 流式事件；新增一次性 stdin/EOF、受限子进程环境变量、
  全局序列、started/stdout/stderr/completed/error、严格帧与输出边界，以及 Windows
  runner/Linux bwrap 的实时输出和整树清理。Windows Rust 测试与 Linux 目标交叉编译已通过；
  Linux 原生 bwrap/安全矩阵仍须由 Linux CI 提供发布证据。同步修复 Windows 构建脚本
  manifest 生成器未把 here-string 接到 `python -` stdin、导致无控制台环境进入 pyrepl
  循环的问题。
- **2026-07-24**：windows-sys 符号路径修正（`DATA_BLOB`→`CRYPT_INTEGER_BLOB`、`OpenProcessToken`/`CreateProcessWithLogonW` 实在 `System::Threading`、`FWP_E_*` 在 `Foundation`、`INFINITE` 在 `System::Threading`），`cargo check`/`cargo test` 全绿；未升级依赖。
- **2026-07-24**：新增 `bin/` 预编译分发 + `runtime-manifest.json` 源码哈希漂移检测 + `scripts/build-security-runtime.{ps1,sh}`；桌面加 Codex 式沙箱 banner。
