# Ace 项目 crew/security 核心执行路径安全审视报告（Adversarial Review）

> 审查对象：crew/security/{service,launch,runtime_client,broker,background_runner,process_lifecycle,actions}.py（共 3632 行）
> 关联验证：crew/tools/{builtin,process_registry,security_guard,file_tools,managed_tools}.py、crew/gateway/routers/{security,sites}.py、crew/agent/external/*、crew/security/{policy,file_policy}.py、crew/state/home.py、security-runtime/src/*.rs（协议认证与三平台沙箱 env 构造）
> 结论：Rust 原生运行时边界（token 认证/防重放/受限 env/句柄继承）设计扎实；风险集中在 Python 侧授权到执行的解耦处、managed 模式的读边界、以及若干 fail-open 分支。

---

## 1. 安全漏洞与缺陷（分级）

### 1.1 高（High）

**H1｜managed 模式默认全盘可读（full_disk_read=True），凭据 deny 清单极短，且 POSIX/Windows 行为不一致**
- 证据：crew/security/policy.py:63-68 对 REQUEST_APPROVAL / AUTO_REVIEW 两种 managed 模式一律 full_disk_read=True；crew/security/file_policy.py:98-140 的 deny 清单只有 CREW_HOME 内 .auth/.env/.gateway-instance/config.yaml/crew_data/logs、owner 的 .env/config.yaml、SQLite DB 及工作区 .git/.agents/.crew（只读 carve-out）。
- 后果：POSIX（macOS/Linux）下沙箱内**任意命令可读 ~/.ssh、~/.aws、~/.gnupg、~/.config、~/.netrc、~/.gitconfig、浏览器 profile 等**；而 AUTO_REVIEW（替我审批）模式下只读命令自动放行（crew/tools/builtin.py:586-598 分类后 authorize_exec_action 的 requires_approval 取决于分类结果），`cat ~/.ssh/id_rsa` 这类命令可被模型自动执行而不打扰用户。
- 对比：Windows 的 full_disk_read 实现 `_windows_full_disk_read_roots`（crew/security/launch.py:500-540）**明确排除** .ssh/.tsh/.gnupg/.aws/.azure/.kube/.docker/.config/.npm/.pki/.terraform.d —— 说明团队**意图**是排除这些敏感目录，POSIX 侧未做同样排除属于遗漏而非产品决策。
- 建议：POSIX 侧把凭据目录加入 deny（对齐 launch.py:500-517 清单），或将 full_disk_read 改为显式 opt-in；AUTO_REVIEW 的只读自动放行需叠加“敏感路径读取需审批”。

**H2｜spawn_local：shell=True + 全量宿主 env + 授权零绑定（结构性 fail-open）**
- 证据：crew/tools/process_registry.py:270-317：`Popen(command, shell=True, env=dict(os.environ)+child_env, preexec_fn=os.setsid)`。其中 env=dict(os.environ) 把**全部宿主密钥**（API key、token、云凭据）注入每个 shell=True 子进程；后台任务超时默认 86400 秒（crew/security/background_runner.py:34）。
- 关键：spawn_local 的 _security_launch/_security_action（process_registry.py:246-267, 176-179）**只存储、不校验**——registry 层没有“授权证明”概念，任何调用点漏传 launch 即静默以宿主全权执行且无审计。
- crew/tools/builtin.py:763, 802：`spawn = spawn_security if launch is not None else spawn_local` 是**显式 fail-open 分支**。当前 terminal 工具的 launch 恒非 None（builtin.py:691 无条件 compile），但该模式为任何未来调用点/上下文丢失埋下“静默 unconfined”地雷。对比 execute_captured 对 None launch 是 fail-closed（crew/security/launch.py:128-135）——**同一模块两条路径行为相反**。
- 建议：spawn_local 改为要求非空 launch；registry 层保存授权票据（grant_id/digest）并在 spawn 时校验；删除 builtin.py 的 fail-open 分支；后台进程设硬性超时上限。

**H3｜授权与执行解耦，无一次性票据绑定；执行面分散**
- 证据：授权在 service.authorize_exec_action（service.py:411-533），执行在 builtin/registry/launch 三个地方；两者之间只有 builtin.py 的“二次授权消费 once grant”（builtin.py:666-675），但 spawn_security/spawn_local 不消费也不校验 grant。spawn_security 非 managed 分支直接转 spawn_local（process_registry.py:360-367）。
- 裸 subprocess 面（argv 形式、无 shell 注入，但不受统一边界约束）：crew/tools/file_tools.py:109（rg，managed 会话才走 Python 兜底）、crew/tools/managed_tools.py:180、crew/tools/cua_setup.py:451-741、skills 脚本多处。
- external=True 自报家门即降级：crew/security/launch.py:146-152 把 external 调用降为 DISABLED 走宿主 unconfined 路径（cli_adapter.py:1262-1275 传 external=not managed）——信任调用方诚实。
- 建议：建立统一执行入口 + 执行面清单（项目已有 tests/security/test_execution_surface_inventory.py，说明已意识到），所有 spawn 携带授权票据。

### 1.2 中（Medium）

**M1｜verify_helper_integrity：manifest 缺失/损坏即放行（fail-open），无签名** — crew/security/launch.py:634-698
- manifest 不存在 → return None（643-644）；JSON 解析失败 → return None（646-648）；**只有 manifest 存在且 digest 不匹配才 fail-closed**。生产包若 manifest 被删或损坏，完整性门禁静默失效。
- ACE_SECURITY_RUNTIME 环境变量可指向任意绝对路径（616-627），配合自写 manifest 即可完全绕过（需本地写权限，但安装目录通常可写）。docstring 自认 “a signed package is the real fix”。
- 建议：打包态强制 manifest（缺失即失败），仅未构建的 dev 树放宽；中期上代码签名。

**M2｜authorize_user_initiated_exec_action 手势授权过宽** — crew/security/service.py:535-558
- 仅查 DENY 规则即 ALLOW：无模式检查、无冷却、**无“手势到具体命令”绑定**（没有 fresh gesture nonce/digest）。
- 实际调用面：crew/gateway/routers/sites.py:255-303（publish_site）与 crew/cli/content.py。publish_site 的 build_command 来自请求体/存储配置——**用户点击“发布”按钮时，执行的是存储的 build_command，而该命令可能在先前轮次被模型改写**，用户只看到“发布”二字。
- 任何未来模型可达的调用点 = 审批模式完整绕过。建议：Desktop 端对 build_command 做 HMAC 签名绑定，或该路径走完整审批流。

**M3｜broker venv 元数据信任链：pyvenv.cfg / .pth / __editable__*.py 的绝对路径直接授予 readable root** — crew/security/broker.py:120-243
- 只检查入口脚本不在 writable 内（138-141），但 **pyvenv.cfg 与其 site-packages 的 .pth / __editable__*.py 是否在 writable 内不检查**。模型在工作区创建的 venv 若能把解释器链指向其 site-packages，可向沙箱注入任意宿主目录的读权限（当前受“入口点必须在 writable 外”约束，利用面窄，属纵深缺口）。
- 建议：venv root / pyvenv.cfg / .pth / __editable__ 文件同样要求不在 writable_roots 内。

**M4｜compile_runtime_filesystem_roots 丢弃祖先 DENY，依赖“默认拒绝”语义假设** — crew/security/broker.py:406-411
- 仅 capability 握手验证 filesystem_sandbox=true（runtime_client.py:670-674），未验证后端是**默认拒绝**语义；若某后端为 deny-list 语义，丢弃祖先 deny = 权限放大。建议把 default_deny 加入 capability 握手并强制校验。

**M5｜进程树终止缺口** — crew/security/process_lifecycle.py:24-60
- 末尾 `await process.wait()` 无超时（line 60）：不可杀死进程（D 态/僵尸）永久挂起调用方。
- killpg 只覆盖进程组，setsid 逃逸的孙进程（守护进程化）成孤儿——runtime_client.py:866-886 与 process_lifecycle 同理（Windows 依赖 helper 的 Job 对象，security-runtime/src/windows/process.rs:413 注释确认，OK）。
- 建议：wait 加最终超时并把残留 pid 记录供回收；明确孤儿回收策略。

**M6｜Windows full_disk_read 敏感清单缺凭据文件** — crew/security/launch.py:500-517
- _WINDOWS_SENSITIVE_TOP_LEVEL 缺 .netrc、.gitconfig、.wgetrc、.curlrc、.env——full_disk_read 模式下这些含凭据文件暴露给沙箱。与 H1 同源，单独列出以便修复。

### 1.3 低（Low）

- **L1｜交互会话 stderr 帧无 Python 侧总量上限**：runtime_client.py:280-292 每帧 append 到 stderr_lines，_output_bytes 只计 stdout；Rust 端有 max_output_bytes 总预算兜底（security-runtime/src/macos/mod.rs:187、windows/process.rs:489），但 Python 内存仍可达 64MB 量级。
- **L2｜_startup_token 是实例共享可变状态**：runtime_client.py:361-363, 324；同一 client 并发 spawn 会互相覆盖 token（当前生产路径每调用新建实例，风险仅在未来复用）。
- **L3｜交互 read_chunk 超时抛裸 asyncio.TimeoutError，不自动 abort**：runtime_client.py:228-304——会话/进程可能残留，依赖调用方清理。
- **L4｜shell_argv 的 shutil.which 解析依赖 PATH**：launch.py:556-573——宿主 PATH 可被写入时（如技能 bin 前置）可劫持 bash/pwsh 解析。
- **L5｜execute_captured 的 spawn 本身无超时**：launch.py:213-225——spawn 悬挂则整个调用悬挂（罕见但存在）。
- **L6｜mode 在 _decision_lock 外读取**：service.py:331, 424——set_mode 与 authorize 的 TOCTOU，靠 Gateway 停 turn 兜底（gateway/routers/security.py:295-305），防御纵深不足。
- **L7｜_ApprovalWaiter.register 与 decide 竞态**：service.py:298 vs 831——decide 先落地、register 后登记 → 等待方 300s 超时按拒绝（fail-closed 但 UX 损坏；窗口极小）。
- **L8｜is_likely_sandbox_denied 关键词可被输出操纵**：runtime_client.py:70-94——命令输出含 “permission denied” 即被归类 sandbox_denied（仅影响元数据）。
- **L9｜background_runner _write_result 的 result_path 无校验**：background_runner.py:84-93——payload 信任；当前仅宿主构造，未来若 payload 可注入即任意文件写。
- **L10｜_runtime_error_code 未知名错误码默认映射 sandbox_denied**：runtime_client.py:1019-1023——错误诊断语义误导。
- **L11｜execute_captured_sync 在线程中 asyncio.run**：launch.py:295-318——contextvar 继承依赖线程上下文，线程未继承时 fail-closed（方向正确）。
### 1.4 与 Rust 运行时通信协议（总体评价 + 残余项）

做得扎实的部分：
- **认证**：32 字节随机 token 经 env 传递（runtime_client.py:760-762），Rust 端常量时间比较（security-runtime/src/main.rs:94-105, 192-200），错误码统一为 protocol_mismatch 防止区分（main.rs:188-200）。
- **防重放**：Rust 端 nonce 缓存（FIFO eviction，main.rs:154, 202-210, 392-398）+ Python 端随机 nonce 逐请求 + 响应 nonce/seq/version 校验（runtime_client.py:645-654）+ completed 后 EOF 校验（838-846）。交互态每帧 authenticate（main.rs:372-400）。
- **无 socket/命名管道**：匿名 stdio pipe + 句柄继承显式控制（Windows explicit_handle_inheritance capability 强制校验，runtime_client.py:680-691；Rust windows/process.rs:335-337）。
- **沙箱子进程 env 三平台均受限**：macOS env_clear（macos/mod.rs:106-111）、Linux bwrap --clearenv（linux/bwrap.rs:205-215）、Windows restricted_environment（windows/process.rs:837-862）——宿主密钥与 token 不进沙箱。

残余问题：
- **R1（中）**：token 与**全量宿主 env** 同时存在于 helper 进程环境（runtime_client.py:760-762）——helper 崩溃 core dump、未来任何记录 env 的路径都会泄露；Python 端**不验证响应方向身份**（无私钥/MAC，靠私有 pipe + nonce 回显）——应把这条信任边界文档化。
- **R2（低）**：Rust 端 request 大小 2MiB / stdin 1MiB / env 256KiB 限制齐全（runtime_client.py:23-31, 939-983），交互态 stdin 写入上限 _MAX_STDIN_BYTES（216-221）在 helper 侧同样校验（main.rs:307-314）——一致。

---

## 2. 设计缺陷

1. **授权与执行解耦无票据（核心）**：authorize 在 service，spawn 在 builtin/registry/launch 三处，grant 消费与 spawn 无绑定关系——registry 层可以无授权执行，service 层可以授权了没人执行（fake runtime 阶段尤其如此）。
2. **threading.Lock 在 asyncio 上下文持有并做 I/O**：service.py:172 的 _decision_lock 是 threading.RLock，decide（service.py:734-738）在锁内做 rules.create + SQLite audit I/O——阻塞整个事件循环（async 端无 await 点让出）。
3. **fail-open/fail-closed 哲学不一致**：execute_captured 对 None launch fail-closed（launch.py:128-135），builtin/registry 对缺 launch fail-open（builtin.py:763, 802）。
4. **异常处理不一致**：audit_execution_result 吞异常仅告警（launch.py:368-371），authorize 路径的 audit 异常向上传播导致工具失败——同一类故障两种行为。
5. **三份进程终止实现**：process_registry.py:84、process_lifecycle.py:24、runtime_client.py:866 各写一份，语义略有差异（killpg 时机、wait 超时）。
6. **竞态**：L6（mode TOCTOU）、L7（waiter 注册竞态）、L2（token 共享）——均有注释承认，属“用 Gateway 兜底”的临时方案。

## 3. 代码质量问题

- Any 类型滥用：launch.py:49-50 audit/approval_service 均为 Any，绕过静态检查。
- service.py:980 `assert outcome.grant is not None`——生产断言。
- background_runner.py:47,59,99,103 魔法返回码 125/126。
- network_rules 序列化逻辑在 broker.py:308-322 与 process_registry.py:382-393 重复。
- _enabled_capabilities（launch.py:327-333）与 background_runner.py:69-73 capability 过滤重复。
- 注释语言中英混用；部分注释与实际语义有出入（如 launch.py:146-152 external 降级的“opt-in”语义依赖调用方诚实）。

## 4. 测试盲区

1. **manifest 缺失/损坏 JSON** 时 verify_helper_integrity 放行的负面断言（现有测试聚焦 digest 不匹配，tests/security/test_runtime_client.py:543-612）。
2. **builtin.py:763/802 的 launch=None → spawn_local fail-open 分支**——无测试断言该分支应 fail-closed。
3. **user_initiated 手势未绑定命令 digest**——无负面测试。
4. **_startup_token 并发复用竞态**。
5. **交互会话 read 超时后进程残留清理**。
6. **POSIX 全盘可读 vs Windows 敏感清单一致性**——无跨平台断言。
7. **祖先 deny 丢弃在 deny-list 语义后端下的权限放大**——无静态断言。
8. **spawn_local 拒绝 None launch**（registry 层强制）。
9. **broker venv 元数据位于 writable 内的拒绝**。
10. **全量宿主 env 泄漏路径**（spawn_local env=os.environ）——无测试声明该设计并固定敏感变量清单。

---

## 5. 修改建议（按优先级）

**P0（应立即修复）**
1. **POSIX managed 模式凭据 deny 对齐 Windows**：在 file_policy._protected_entries 或 settings_for_mode 补充 ~/.ssh、~/.aws、~/.gnupg、~/.netrc、~/.gitconfig、~/.config（至少对齐 launch.py:500-517 清单）；或让 full_disk_read 显式 opt-in。
2. **堵死 spawn_local fail-open**：process_registry.spawn_local 要求非空 _security_launch；builtin.py:763,802 删除 launch is None 分支（直接失败）；spawn 与授权票据（grant_id/action digest）绑定。
3. **verify_helper_integrity 打包态强制 manifest**：manifest 缺失/损坏一律 fail-closed，仅 dev 树（可判定）放宽。

**P1（尽快）**
4. **user_initiated 手势绑定命令**：Desktop 端对 build_command + argv 做 HMAC 签名，服务端校验后才走 authorize_user_initiated_exec_action。
5. **broker venv 元数据 writable 检查**：pyvenv.cfg/.pth/__editable__ 及其 venv root 不得位于 writable_roots 内。
6. **进程终止加固**：process_lifecycle.py:60 wait 加最终超时；记录无法回收的 pid；_windows_full_disk_read_roots 补充 .netrc/.gitconfig 等。
7. **default-deny 加入 capability 握手**并强制校验。

**P2（持续改进）**
8. 统一执行入口 + 执行面清单，授权票据贯穿。
9. _startup_token 改为 per-session 独立存储；交互 stderr 设 Python 侧总量上限；read 超时自动 abort。
10. mode 读取移入 _decision_lock；_ApprovalWaiter 登记与 decide 串行化。
11. 合并三份 terminate 实现；消除 Any 与重复序列化逻辑。
12. 补充第 4 节列出的测试盲区（尤其 1-3、6、8）。