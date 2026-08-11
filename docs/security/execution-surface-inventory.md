# Ace 外部执行面清单

本清单是安全迁移账本，不代表下列入口已经受沙箱保护。`tests/security/test_execution_surface_inventory.py`
会扫描 Python 直接进程创建和 Desktop `child_process` 导入；新增入口未登记时测试失败。

处理类型：

- `broker`：用户或模型可触发，目标处理是迁移到 `SecurityExecutionBroker`；该标签不表示已经
  迁移，条目文字必须明确现状；未迁移时 managed 模式不可用。
- `host-fixed`：仅产品安装、启动、更新、卸载生命周期使用，保留宿主执行，但 argv 和调用来源必须固定。
- `runtime-boundary`：只允许启动签名/随包 native helper 的固定 argv；用户命令仅作为版本化协议数据发送，禁止 shell/PATH 解析或 host fallback。
- `sandbox-descendant`：Skill 脚本只能由已经进入 managed terminal/native runtime 的父进程调用；
  其 Node/Python/browser 后代继承同一技术身份、Job/PID namespace、ACL/mount 与网络边界，不得
  另起 host fallback。随包 `crew/skills` 由宿主作为独立 trusted read-only root 传给 native
  runtime，不通过模型 argv 申请，也不进入可升级 file permission。
- `indirect`：本文件不直接创建进程，但能触发其他执行面，必须纳入集成测试。
- `legacy-poc`：旧 Docker POC；native runtime 验收后删除，不得进入生产安全路径。
- `dev-only`：开发/测试入口，不进入用户安装包。

| 源文件 | 处理类型 | 当前用途与最终处理 |
|---|---|---|
| `crew/tools/builtin.py` | indirect | terminal 前后台入口；P6 统一经 broker，managed 失败不得回退宿主。 |
| `crew/security/runtime_client.py` | runtime-boundary | 唯一允许的 managed helper host spawn；固定 helper argv、随机启动 token、版本/nonce 校验，用户 command 只进入 stdin 协议。 |
| `crew/security/launch.py` | runtime-boundary | 会话级统一 captured execution；managed 只启动固定 native helper，disabled 才使用当前 OS 用户权限。 |
| `crew/security/process_lifecycle.py` | runtime-boundary | 安全执行边界的固定进程树清理 helper；Windows 只调用固定 `taskkill` argv，POSIX 只终止已启动的进程组。 |
| `crew/tools/process_registry.py` | broker | terminal 后台执行已按 `ProcessLaunch` 分流：disabled 才使用本地 shell；managed 只启动固定 Python bridge，再由 `NativeRuntimeClient` 执行协议内 command，runtime unavailable 不回退宿主。`taskkill/tasklist` 仅用于已登记进程的宿主生命周期清理/探测。 |
| `crew/tools/mcp_client.py` | indirect | 默认禁用 MCP host stdio；只有操作者显式 host 配置可启用，managed transport 未实现时 unavailable。 |
| `crew/agent/external/acp_adapter.py` | broker | ACP 双向 stdio 在 `config/config.yaml` 的 `external_agents.security_enabled=true` 时经 `SecurityExecutionBroker.open_interactive` 由 native runtime 托管；设为 `false` 后仅允许当前用户权限直启。ACP 协议仍是原生 stdin/stdout。Crew `crew-interaction` MCP proxy 的短期 binding 环境保留在 MCP 声明内，并只获得当前 Gateway loopback `host:port` 回调权限。 |
| `crew/agent/external/codex_adapter.py` | broker | Codex app-server 双向 stdio 在配置开关开启时经 `NativeRuntimeClient.open_interactive` 托管，凭据、workspace、精确网络权限和进程树均由 Security Broker 编译；managed 失败不回退宿主，关闭后才允许兼容直启。 |
| `crew/agent/external/cli_adapter.py` | broker | Claude stream-json 双向 stdio 在配置开关开启时经 Native interactive transport 托管，临时 MCP 配置写入 workspace 并随会话清理；普通 CLI 对话调用 `security.launch.execute_captured`，managed 失败不回退宿主，关闭后才按旧 runtime 直启。模型探测只运行已登记 candidate 的固定 argv，属于认证后的宿主控制面发现，不接收模型 argv。 |
| `crew/agent/external/detector.py` | broker | 外部 agent 路径只来自运维环境变量、PATH 或已知 Desktop bundle，版本/能力探测使用固定 argv；managed 会话内版本探测明确 unavailable。仍应在未来有 native probe transport 时收敛，但当前不是模型可控 host command。 |
| `crew/agent/external/process_lifecycle.py` | broker | ACP 与外部 runtime 探测的进程树清理 helper；只允许固定 `taskkill` 或已有进程组信号。 |
| `crew/tools/cua_setup.py` | broker | CUA 安装/daemon 生命周期是 Desktop-proof 保护的显式宿主安装动作，argv 固定；会话内 managed CUA 命令经 `execute_captured` 走 broker。安装制品 digest/签名仍开放（`SEC-P1-004`）。 |
| `crew/tools/managed_tools.py` | host-fixed | ripgrep 下载、SHA-256 校验和版本探测属于宿主工具安装/维护；managed 文件搜索不会调用它，安装来源与固定 digest 继续由供应链门禁约束。 |
| `crew/tools/file_tools.py` | broker | disabled 模式可调用校验后的 host ripgrep；managed 模式 `_resolve_rg()` 固定返回 `None`，改走同一文件授权与 identity-checked Python 读取，不会从 sandbox 旁路启动宿主 rg。 |
| `crew/sites/manager.py` | broker | 站点构建命令经会话 `ProcessLaunch` 和 `execute_captured` 进入统一边界；managed runtime 缺失时失败关闭，Desktop 手动重发也会从认证 workspace 重新编译 launch。 |
| `crew/browser/manager.py` | indirect | Electron 浏览器控制与网络动作；不直接创建进程，继续由 Desktop browser host 与独立网络策略约束。上传只把审批后 identity-checked 字节物化到 owner 私有 `approved-uploads` 根，Host 不接受原工作区/任意宿主路径。 |
| `crew/cron/scheduler.py` | indirect | 定时触发 agent/tool；沿用被调用工具的 broker 和 owner context。 |
| `crew/team/team_manager.py` | indirect | teammate 触发共享工具；沿用被调用工具的 broker 和 owner context。 |
| `crew/skills/html-to-pdf/scripts/convert.cjs` | indirect | Puppeteer 间接启动系统 Chrome/Edge；当前依赖外层 managed terminal 的 OS sandbox，`--no-sandbox` 关闭浏览器自身隔离的风险需独立复核。 |
| `crew/skills/docx/scripts/office/validators/redlining.py` | sandbox-descendant | 仅由 managed terminal 内的文档 Skill 脚本调用；LibreOffice 子进程继承同一 native sandbox，不提供 Gateway host fallback。 |
| `crew/skills/md-to-pdf/scripts/md2pdf.py` | sandbox-descendant | 仅由 managed terminal 内的 PDF Skill 脚本调用；wkhtmltopdf/浏览器后代继承同一 native sandbox。 |
| `crew/skills/pdf/scripts/md2pdf/md2pdf_convert.py` | sandbox-descendant | 仅由 managed terminal 内的 PDF Skill 脚本调用；外部转换器继承同一 native sandbox。 |
| `crew/skills/skill-creator/eval-viewer/generate_review.py` | sandbox-descendant | Skill 开发辅助脚本；只允许从 managed terminal 启动，其浏览器预览后代继承同一 native sandbox。 |
| `crew/skills/skill-creator/scripts/improve_description.py` | sandbox-descendant | Skill 开发辅助脚本；只允许从 managed terminal 启动，其 Codex CLI 后代继承同一 native sandbox。 |
| `crew/skills/skill-creator/scripts/run_eval.py` | sandbox-descendant | Skill 评估辅助脚本；只允许从 managed terminal 启动，其 Codex CLI 后代继承同一 native sandbox。 |
| `crew/skills/xlsx/scripts/office/soffice.py` | sandbox-descendant | 仅由 managed terminal 内的表格 Skill 调用；LibreOffice 后代继承同一 native sandbox。 |
| `crew/skills/xlsx/scripts/office/validators/redlining.py` | sandbox-descendant | 仅由 managed terminal 内的表格 Skill 调用；LibreOffice 后代继承同一 native sandbox。 |
| `crew/skills/xlsx/scripts/recalc.py` | sandbox-descendant | 仅由 managed terminal 内的表格 Skill 调用；LibreOffice 后代继承同一 native sandbox。 |
| `crew/wiki/parser.py` | broker | 旧 Office 转换已使用 `execute_captured_sync`；Gateway 的 `asyncio.to_thread` 会继承可信 `ProcessLaunch`，managed runtime 缺失或上下文丢失时失败关闭。 |
| `scripts/audit_runtime_npm.py` | dev-only | CI/发布期遍历源码锁文件并以固定 npm audit argv 审计生产依赖；不进入用户安装包。 |
| `scripts/check_release_readiness.py` | dev-only | CI/发布期以固定 git argv 绑定验收证据到当前提交；不执行用户输入命令，不进入用户安装包。 |
| `desktop/src/main/index.ts` | host-fixed | Gateway 启停、更新器和 OS 生命周期；固定产品 argv。严格模式要求初始及重定向后最终地址均为 HTTPS、Ed25519 detached signature，并在下载和安装前后校验；兼容模式保留旧更新源能力。 |
| `desktop/src/main/open-with-service.ts` | host-fixed | 仅响应 Desktop 用户“使用其他应用打开”；应用来自当前 OS 已登记清单，命令固定、argv 分离，并限制探测输出与超时；超时或输出超限会终止完整探测进程树。 |
| `desktop/src/main/uninstall.ts` | host-fixed | 卸载清理；仅签名安装包的用户发起卸载流程可达。 |
| `desktop/scripts/check-security.mjs` | dev-only | 构建期 Electron 安全配置检查。 |
| `desktop/scripts/resolve-playwright-candidates.mjs` | dev-only | 定时/手动兼容性 CI 解析候选 npm 版本；不进入用户安装包，也不接收产品用户输入。 |
| `desktop/src/main/security-setup.ts` | installer-only | 仅 Windows 安装包：固定 runtime 绝对路径经编码 PowerShell `RunAs` 请求一次 UAC，用户动作不能提供 executable/argv。 |

## 门禁边界

自动扫描覆盖 `crew/optional-skills/plugins/scripts` 的 Python 标准库进程创建和
`crew/optional-skills/plugins/desktop` 的 JavaScript/TypeScript
`child_process` 导入，以及 Desktop `child_process` 导入。动态依赖内部创建进程、原生扩展、
Shell 脚本文本以及未来其他 runtime 不可能只靠静态扫描证明安全，因此 P6 仍需对每个用户可达入口做
managed 成功、拒绝和 runtime-unavailable 集成测试。
