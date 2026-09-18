# Ace Agent 核心缺陷清单（对照 codex 标准，参照 dsh）

> 日期：2026-09-16
> 范围：仅 agent 基础功能 / 基础建设（主循环、工具执行、LLM 流、并发、取消、上下文、持久化、错误处理），不含 channel / gateway / desktop / site 等外围功能。
> 基准：codex（`/Users/ahuamao/Documents/Codes/codex`，Rust 实现，作为标准）；dsh（`/Users/ahuamao/Documents/Codes/deepseek-harness`，TS 实现，作为第二参照）。
> 调研对象：Ace `crew/agent/`（runtime / loop / executor / compact / plan / subagent）、`crew/core/`、`crew/tools/`、`crew/plugins/manager.py`、`crew/state/`、`crew/memory/`。

---

## 修复状态（2026-09-17，分支 fix/agent-core-defects）

> 本表对照下方清单逐条登记修复状态；正文不动。

| 缺陷 | 状态 |
|---|---|
| P0-1.1 插件 sync hook/middleware 内联阻塞事件循环 | 已修复 `71513d9`（同步回调自动线程化，与 tool 对齐；取消路径 2s 有界优雅窗口去 busy-spin） |
| P0-1.2 内置工具无 per-tool 超时/取消看门狗 | 已修复 `4ce7c1b`（执行段看门狗 + interrupt 可取消在途工具，aborted/timed-out 合法输出语义） |
| P0-1.3 整回合无 deadline + 中断检查点稀疏 + interrupt_message 存而不用 | 已修复 `679ace7`（`turn_deadline_seconds` 默认关闭；interrupt_message 消费为 [回合中断] 历史标记 + status 帧；compact 段中断检查）+ `4ce7c1b`/`e99e58e`（工具执行/权限等待内部接入中断竞争） |
| P1-2.1 每轮热路径同步文件 I/O | 已修复 `94b37b5`（prompt 文件 mtime 缓存 + 异步组装、owner env to_thread、llm_trace 队列落盘、媒体读取线程化）+ `679ace7`（estimate_tokens 迭代内缓存）；`build_request_view` 两次全量组装未合并（低优先级遗留） |
| P1-2.2 单回合内严格串行、无流水线 | 未做——架构级改造，另立项评估 |
| P1-2.3 追问/权限确认无期限等待 | 已修复 `e99e58e`（`interaction_timeout_seconds` 默认有界 3600s；interrupt 取消联动默认拒绝） |
| P2-3.1 串行事件入口（Op 队列） | 未做——架构级改造，另立项 |
| P2-3.2 write-ahead 持久化 / resume | 未做——架构级改造，另立项 |
| P2-3.3 错误类型化 | 已修复 `5080c47`（CrewErrorKind + is_retryable 白名单 + ProviderError kind/status/retry_delay + provider 分类收敛 + Retry-After 解析，ADR-0045）+ `81583a2`（重试消费 retry_delay 封顶 60s、error 帧结构化 kind/retryable/retry_delay、outbound/dispatcher 错误分支类型化） |
| P2-3.4 沙箱/审批闭环策略 | 未做——另立项 |
| P2-3.5 asyncio_bridge 死锁陷阱 / time.sleep 残留 / 取消路径 busy-spin | 已修复：`time.sleep` 收进可注入 `_busy_retry_sleep`（`a8aa51b`）、`_lease_callback` busy-spin 去忙等（`71513d9`），`run_sync` 增加桥接循环线程自调用检测（本批次） |
| P3 子 agent 超时收尾顺序写反 | 已修复 `a8aa51b`（先 interrupt 后 aclose） |
| P3 bg_events 泄漏 | 已修复 `a8aa51b`（entry 带完成时间戳，TTL 3600s 顺带清扫） |
| P3 evolution_visible 同步扣留 final 帧 | 未做 |
| P3 delegate_task 批量模式阻塞父回合（不可 steer） | 部分修复：`a8aa51b` 做了单个子任务异常隔离；"父回合等待期间可 steer"未做 |
| P3 token 全量估算每迭代多次调用 | 已修复 `679ace7`（同迭代内 provisional/overflow 合并为一次，视图失配自然失效） |
| §7 「智能体运行环境准备中」遮罩链路 | 第一阶段（探针串行化 + 失败分类 + 双阈值 `cccc05e`、遮罩改非阻断状态横幅 `2ffe5a6`）与第二阶段（health 独立线程 + loop_lag_ms 哨兵 `08f746f`、热路径治理 `94b37b5`、看门狗 `4ce7c1b`、hook 线程化 `71513d9`）已修复；第三阶段（per-session 状态机 + 连接即活性 + 可 resume，消灭全局健康状态）另立项 |
| 清单外遗留：TaskManager 同步写跑在事件循环 | 已修复 `7be86ac`（TaskRuntime 19 个 async 门面方法，事件循环调用点全量迁移：dispatcher / handle_terminal / team delegate_tool / subagent tools / sessions 路由 / history_projection，ADR-0046） |

验证记录：`docs/testing/agent-core-defects-batch-2026-09-17.html`。

---

## 0. 总体结论

Ace 的 agent 主循环已经是全 async 的，**没有** `asyncio.run` 嵌套 / 事件循环内同步 HTTP 这类恶性反模式，工具并行、prewarm、subagent 并发、LLM 重试等基础能力都具备。但对照 codex，差距集中在：

1. **缺统一取消 / 超时 / deadline 机制**——这是最大的体系性缺陷；
2. **事件循环上仍有每轮必走的同步阻塞点**（插件 hook、同步文件 I/O）；
3. **没有串行事件入口（Op 队列）和 write-ahead 持久化**，状态变更与持久化模型落后一代；
4. **错误没有类型化**，重试 / 审批 / 沙箱没有形成闭环策略。

之前印象中"Ace 有很多同步操作阻塞效率"的结论，现在应修正为：**不是主循环同步，而是少量关键同步点 + 无看门狗导致的"一点卡死、全局冻结"风险**。

---

## 1. P0 缺陷（会导致整体卡死 / 全局阻塞）

### 1.1 插件 sync hook/middleware 内联阻塞事件循环

- `register_tool` 会给同步 handler 自动加 `run_sync_in_thread=True`（`crew/plugins/manager.py:517-521`），但 `register_hook`（manager.py:569）和 `register_middleware`（manager.py:600）**没有传该标志**，同步回调在 `_lease_callback` 里直接内联执行（manager.py:352）。
- 而 hook/middleware 恰好在 agent 主路径上被 await：`pre_llm_call`（`crew/agent/executor/builtin.py:598`）、`apply_llm_request_middleware`（builtin.py:273）、`run_tool_execution_middleware`（`crew/agent/executor/tool_runner.py:834`）、`transform_llm_output`（builtin.py:799）。
- **后果：任何一个插件注册同步 hook，就会冻住整个 gateway 事件循环上的所有会话。**
- codex 对照：所有扩展点都在 tokio task 内异步执行，不存在"同步回调跑在事件循环上"的可能。

### 1.2 内置工具无 per-tool 超时 / 取消看门狗

- `Registry.execute`（`crew/tools/registry.py:422-466`）与 `ToolRunner._execute_one_body`（`crew/agent/executor/tool_runner.py:698-868`）全程没有 `asyncio.wait_for` 包裹；超时全靠各工具自觉（terminal 有、browser 类工具没有）。
- **后果：任意一个 builtin async 工具 hang 住 → 整个回合冻结 → 因为是单事件循环，gateway 上所有会话全部冻结**，且 interrupt 只在安全点轮询（builtin.py:471, 820, 935），卡在工具 await 里时停止按钮无效。
- codex 对照：每个工具调用是独立 `tokio::spawn` task + `AbortOnDropHandle`，`select!` 竞争取消 token，取消后还会把 `"aborted by user after X.Xs"` 作为合法 tool output 回给模型（`core/src/tools/parallel.rs:177-207`）。
- dsh 同样没有框架级统一工具超时（pwsh 的 300s 是工具配置）——这一项 Ace 与 dsh 持平，但 codex 的"取消即合法输出"语义值得直接抄。

### 1.3 整回合无 deadline + 中断检查点稀疏

- `max_iterations = 0`（无限）时靠 auto-compact + guardrail，**没有整回合时间上限**（builtin.py:376-378）。
- `TurnControl.interrupt` 是协作式标志，检查点只有轮初 / 模型后 / 工具后 / 文本 delta（builtin.py:471, 820, 935, 1135）；压缩摘要 LLM 调用、权限等待、工具执行内部都不检查。
- `interrupt_message` 存了但 executor 从不消费（`crew/agent/loop/control.py:51`），消息内容被静默丢弃。
- codex 对照：`CancellationToken` 树（turn → stream → 每个工具 → compact 各拿 child token），中断 = cancel → 等 100ms 优雅退出 → 超时硬杀 → 清理钩子 → 历史写入 interrupted marker（`core/src/tasks/mod.rs:494-522, 880-973`）。

---

## 2. P1 缺陷（每轮热路径上的性能损耗）

### 2.1 每轮同步文件 I/O 跑在事件循环上

以下全部位于"每个会话每轮必走"路径，随会话数线性叠加：

- prompt 构建每次同步读 SOUL / CREW.md / profile 文件，无缓存无 to_thread：`crew/agent/prompt_builder.py:99-119` → `runtime.py:382`。
- 每轮同步刷新 owner env 文件（持 `_ENV_LOCK`，多会话并发互等）：`runtime.py:703` → `crew/state/home.py:604-613`。
- llm_trace 开启时每次工具 start/result 同步写文件：`crew/state/logging.py:289`，热路径在 `tool_runner.py:1004, 1031`。
- 工具媒体落盘 `path.read_bytes()` 同步读 + base64 编码（截图可达数 MB）：`tool_runner.py:613-616`。
- 每迭代两次全量 `build_request_view` + `estimate_tokens`（chars//4 全量估算，O(n) CPU）：builtin.py:490, 502-505, 583。长历史下开销可观。
- codex 对照：rollout 写盘由独立后台 writer task 承担（`rollout/src/recorder.rs:139`），热路径只做 append 到 channel。

### 2.2 单回合内严格串行，无流水线

- LLM 流被完整消费后才执行工具（builtin.py:1088 → 890），工具执行期间无任何 LLM 调用重叠（唯一的重叠是 prewarm：流式期间 safe 工具参数一拼完就提前跑，tool_runner.py:224-261——这是亮点，codex 也有等价机制）。
- compact 压缩是内联阻塞的：溢出时 `force_compact` 直接 await 摘要 LLM 调用（builtin.py:506-547），回合期间用户只能看 compaction 帧。
- codex 对照：工具 future 进 `FuturesOrdered`，**流不等工具、继续消费 SSE**，response Completed 后才 drain（`core/src/session/turn.rs:2226, 2746-2752`）；mid-turn 压缩是一等任务（带 reason/phase/injection 策略）。

### 2.3 无期限的交互等待点

- 追问工具 `wait_for_answer(..., timeout=None)`（`crew/tools/interaction.py:93-98`）——用户不选，回合永远挂起。
- 权限确认同样无限等待（tool_runner.py:574）。
- codex 对照：审批等待挂在**工具 task 内部**的 oneshot 上（`core/src/session/mod.rs:2388-2470`），submission_loop 照常运转；中断时先让 task 观察取消再清 pending approvals；oneshot 断开默认 Abort。Ace 的等待点虽然也是 async await（不阻塞循环），但缺超时和取消联动。

---

## 3. P2 缺陷（架构代差）

### 3.1 没有串行事件入口（Op 队列），状态变更散落各处

- codex 的核心模式：**所有改变会话状态的操作都过一条串行 submission loop**（`core/src/session/handlers.rs:515-693`），turn 在后台 task 跑，启动/中断/审批答复都排队进 loop，天然消除并发竞争。
- dsh 等价物：单 driver 串行 + 持久化 inbox（`agent.ts:225-350`）。
- Ace：状态变更分散在 executor / runtime / dispatcher 各处直接改，同会话靠 `SessionDispatcher` 按 `(owner, session_id)` 持锁串行（`crew/gateway/dispatcher.py:78,134`），但会话内部没有"输入信箱 + 串行消费"模型，steer 只作用于 LLM 轮次边界（父回合在子等子 agent 期间完全不可 steer）。

### 3.2 持久化不是 write-ahead，resume 能力弱

- codex：**先写 rollout 后投递事件**（`session/mod.rs:2165-2176`），rollout 是 append-only JSONL，resume = 回放事件流重建全部内存状态（含 token 用量回填、compaction 窗口恢复），fork 支持零拷贝引用祖先历史。
- dsh 更彻底：事件溯源，"Every request is derived from the session log"，inbox / 重试计数 / 压缩锁全部持久化，崩溃后可精确重建。
- Ace：`session_store.save_async` 走 to_thread 落 SQLite（做对了线程隔离），但粒度是"回合结束整体保存"，不是事件级 write-ahead；中途崩溃只丢回合内状态。且 `_persist_turn` 注释声称 save_async "不可被取消打断"，实际只 shield 了 `memory.write`（runtime.py:1543），`session_store.save_async`（runtime.py:1526）是普通 await——**注释与行为不符**。

### 3.3 错误没有类型化

- codex：`CodexErr` 是穷举大枚举（Stream / Timeout / UsageLimitReached / ContextWindowExceeded / Sandbox(Denied|Timeout|Signal) / AgentLimitReached…），`is_retryable()` 白名单判定，429/503 的 `Retry-After` 解析进 `retry_delay()`（`protocol/src/error.rs:81, 364-413`）。
- Ace：错误以字符串 / 通用异常传播，重试判定散落在 LLM 层（builtin.py:1302-1318 的指数退避 + fallback 链做得不错），但工具层没有任何自动重试，也没有"可重试 / 不可重试"的统一分类。

### 3.4 沙箱 / 审批没有闭环策略

- codex：`ToolOrchestrator` 的算法是 **approval → select sandbox → attempt → denied 时升级沙箱重试**，且审批结果可沉淀为持久策略（`ApprovedExecpolicyAmendment` 写回 exec_policy，审批即策略学习）（`core/src/tools/orchestrator.rs:121-498`）。
- Ace：权限确认是一次性问答，无沙箱概念（agent 核心层），无"拒绝后升级重试"，无策略沉淀。

### 3.5 asyncio_bridge 留有死锁陷阱

- 防御分支 `asyncio.run_coroutine_threadsafe(coro, _bridge_loop()).result()`（`crew/core/asyncio_bridge.py:56-57`）只检测"有 running loop"，**不检测调用线程是不是那个循环线程**；未来若有人从事件循环线程调用 `run_sync` 即永久死锁，且无告警。
- `time.sleep` 残留：`crew/state/sqlite.py:131` 同步路径 busy 重试（当前无事件循环上的调用方，属隐患）。
- `_lease_callback` 取消路径 busy-spin：同步回调 hang 住时取消方无限 shield（`crew/plugins/manager.py:344-351`），任务泄漏。

---

## 4. P3 缺陷（小而确定的问题）

| 问题 | 位置 | 说明 |
|---|---|---|
| 子 agent 超时收尾顺序写反 | `crew/agent/subagent/tools.py:460-468` | 先 `gen.aclose()` 再 `child.interrupt()`，interrupt 标志永远不可见（无害但逻辑无效） |
| `bg_events` 泄漏 | subagent/tools.py:664, 893 | 永不 collect 的 task_id 不清理 |
| evolution_visible 模式同步扣留 final 帧 | runtime.py:952-954, 1086-1133 | 最长约 15 分钟三阶段扣留 |
| `delegate_task` 批量模式阻塞父回合 | subagent/tools.py:575 | gather 等全部子 agent 完成，期间父回合不可 steer |
| token 全量估算每迭代多次调用 | builtin.py:502-505, 534 | chars//4 粗估可接受，但重复全量调用是纯 CPU 浪费；codex 用真实 usage + 阈值判断时本地估算 |

---

## 5. 已经做对、不应误伤的地方

- 同步工具 handler 统一 `asyncio.to_thread`（registry.py:160-167）；
- 同步 HTTP 下载 / web 工具全部线程池化（managed_tools.py:362、web_tools.py:355,414）；
- LLM 流式是真流式（httpx AsyncClient + delta 透传 + reasoning 流式 + 流中断续写协议，builtin.py:1088-1287）；
- LLM 层重试规范（指数退避 + jitter + fallback provider 链）；
- 工具批内并行 + safe/unsafe 分段 + prewarm（tool_runner.py:215-261, 383-464）；
- subagent 全 async 真并发 + idle/max 双超时（subagent/tools.py:401-428, 546-576）；
- compaction 三层渐进 + 防抖 + 断路器 + "摘要必须更小"事务校验（compact/pipeline.py:56-64, 653-663）；
- 硬取消后 finally + shield 落库（runtime.py:971-1048）。

---

## 6. 修复优先级建议（按 codex 标准排序）

1. **P0-1**：给 `register_hook` / `register_middleware` 补上 `run_sync_in_thread`，与 tool 防护对齐（改动小，收益大）。
2. **P0-2**：在 `ToolRunner._execute_one_body` 外层加统一 per-tool 超时看门狗（`asyncio.wait_for`，默认值走 config），并把 interrupt 检查点扩展为"工具 await 可被 cancel"。
3. **P0-3**：引入整回合 deadline + 可配置的无限迭代上限；消费 `interrupt_message`。
4. **P1-1**：prompt 文件 / owner env / llm_trace 三类热路径 I/O 全部 to_thread 化或加缓存。
5. **P1-2**：追问 / 权限等待加超时 + 取消联动。
6. **P2**：错误类型化（定义 `CrewError` 枚举层级 + retryable 标记），再在其上统一重试策略。
7. **P2**：会话内引入"输入信箱 + 串行消费"模型（参考 dsh inbox 的 followup/steer/inject 三档），让 steer 在工具执行 / 子 agent 等待期间也能生效。
8. **P3**：修子 agent 收尾顺序、bg_events 清理、去掉 `time.sleep` 残留。

> 说明：3.1（Op 队列）、3.2（write-ahead 事件溯源）、3.4（沙箱编排）属于架构级改造，建议单独立项评估，不在本清单的快速修复范围内。

---

## 7. P0 深挖：「智能体运行环境准备中」遮罩链路（2026-09-16 追加）

用户体感：后端一有报错或桌面端连不上，桌面端就全屏 loading「智能体运行环境准备中」，很影响体验。以下是这条链路的完整设计与根因分析。

### 7.1 链路全貌

```
gateway（FastAPI，单事件循环）
  └─ GET /api/health          crew/gateway/routers/misc.py:170
       ↑ 与 agent 业务（LLM 流、工具执行、hook、压缩）共用同一个事件循环
desktop 主进程
  └─ setInterval 每 1000ms 触发 pollBackendHealth   desktop/src/main/index.ts:1886-1889
       └─ probeGatewayInstance，AbortController 3s 超时   gateway-instance-auth.ts:14 (HEALTH_TIMEOUT_MS=3000)
            └─ 连续 3 次失败 → pushBackendStatus(false) → IPC 'backend:status'   index.ts:1875-1878
renderer
  └─ backend-status-guard 收到 connected=false
       ├─ 全屏遮罩「智能体运行环境准备中，请稍等」   backend-status-guard.ts:60-70；assets/index.html:40
       └─ setTab 一律 return false，所有页面不可切换   app.ts:166-169
```

### 7.2 根因一：健康探测测的是"事件循环活性"，而事件循环会被 agent 业务卡死

`/api/health` 与所有 agent 业务跑在**同一个 asyncio 事件循环**上。前面 P0/P1 清单里的每一项阻塞点，都会直接表现为 health 探测超时：

- 插件 sync hook/middleware 内联执行（P0-1）→ 循环冻结 → health 超时；
- 工具 hang 无看门狗（P0-2）→ 循环冻结 → health 超时；
- 每轮热路径同步文件 I/O（P1-1）→ 循环抖动 → health 偶发超时；
- compact 内联摘要 LLM 调用、evolution_visible 扣留 final（P1-2 / P2）→ 循环繁忙。

也就是说：**遮罩不是"环境在准备"，而是"agent 核心的阻塞缺陷通过 health 端点传导到了 UI"**。主进程注释其实已自知：「gateway 繁忙（加载技能/构建大 prompt/执行工具）时单线程 asyncio 可能 2s 内没响应 health」（index.ts:210-213）——但用的是加重试阈值的补丁，没有治本。

### 7.3 根因二：判定阈值过敏感，且探测会自我叠加

- 间隔 1s、超时 3s、阈值 3 次：`setInterval(() => { void pollBackendHealth() }, 1000)` **不 await**，探针耗时超过 1s 就会叠加。事件循环一旦卡 3s，最多 3 个在途探针几乎同时超时，`healthFailCount` 瞬间到 3 → 遮罩弹出。**实际触发窗口≈一次 3 秒的事件循环卡顿**，而非"持续 3 秒不可用"。
- 恢复路径倒是快的（一次成功立即复位，index.ts:1866-1872），所以体感是"闪一下全屏 loading"——比一直 loading 更烦。

### 7.4 根因三：UI 策略是二值全屏阻断，没有降级模式

- `connected` 是个布尔：进程崩溃、端口冲突、事件循环卡顿 3 秒、冷启动慢，**四种性质完全不同的事共用同一个全屏遮罩**。
- 遮罩阻断一切（含只读页面），没有"降级可用"概念——历史会话、本地设置这类不依赖实时后端的内容本可继续浏览。
- 文案误导：真实原因是"后端繁忙/报错/崩溃"，显示的却是"准备中"，用户无从判断该等还是该修。
- 已有的容错（20s 后升级为"仍在准备中 + 查看日志/重试/继续等待"，backend-status-guard.ts:88-99；组件 failed 只 toast 不遮罩，backend-status-guard.ts:191-197）说明方向对，但第一响应仍是全屏挡。

### 7.5 修复建议（按 codex 模式分三层，2026-09-16 修订）

> 修订说明：初版本节是"优化遮罩"思路；7.6 的 codex 调研证明更彻底的做法是"消灭遮罩"。故本节重写为三阶段路线——短期止血保留遮罩，中期把错误归属到会话/turn，长期按 codex 模式删掉全局健康状态。

**第一阶段 · 止血（保留遮罩，消除误伤，纯桌面端改动，1~2 天量级）：**

1. 探针串行化：上一次 probe 未结束不发起下一次，消除自我叠加（index.ts:1886-1889 改为链式 setTimeout）；
2. 区分失败类型：连接拒绝（进程死了）/ 超时（循环忙）/ 认证失败，分别对应"自动重启中" / "后端繁忙，稍候" / 错误提示；
3. 遮罩降级为**非阻断横幅 + 仅聊天输入区禁用**，只读页面（历史会话、设置）放行；文案按真实状态区分"正在启动 / 后端无响应（已等 Ns）/ 后端崩溃重启中"；
4. 冷启动期与稳定运行期用不同阈值：启动宽容、稳定后敏感。

**第二阶段 · 治本（后端，对应 P0，遮罩出现频率大幅下降）：**

5. 修掉 P0-1（插件 hook/middleware 线程池化）、P0-2（per-tool 超时看门狗）、P1-1（热路径同步 I/O），让事件循环不被业务卡死；
6. `/api/health` 与业务循环隔离：独立端口/线程服务，或加"循环活性哨兵"（后台 task 每秒戳时间戳，health 直接读时间戳，不排队等业务任务）——健康探测不再被 agent 工作量传导。

**第三阶段 · 消灭遮罩（对齐 codex，依赖 P2 地基，单独立项）：**

7. 会话持久化做到可 resume（P2-3.2：事件级 write-ahead + 回放重建），这是"断线恢复不丢状态"的地基；
8. 错误类型化（P2-3.3），协议上区分 `will_retry` 瞬时错误（内联一行、自动消失）与终态错误（标记该会话/turn）；
9. per-session 状态机通知（类似 codex `thread/status/changed`）替代全局 `backendConnected` 布尔：错误归属到具体会话，其他会话照常；
10. WS 连接即活性：撤掉 1s 后台轮询，gateway 进程死了由桌面端在下次操作发起点 probe-and-restart 吸收；只读界面永远不被阻断。

> 取舍说明：第一、二阶段独立可做、互不阻塞，建议并行；第三阶段是架构改造，依赖 7、8 两块地基，但它是唯一能把"智能体运行环境准备中"这个组件从代码库里删掉的路径。

### 7.6 对照：codex 怎么处理同一个问题

前提：codex 的桌面 app 本体是闭源的（`codex-rs/cli/src/desktop_app/mac.rs:12-16` 只是按 bundle id 查找/下载安装），但它与 core 之间的链路全开源：桌面 app 是 **app-server** 的客户端（JSON-RPC over unix-socket WebSocket），TUI 是参照客户端。结论如下（证据见括号）：

1. **连接即活性，不轮询 health**。app-server 没有 HTTP health 端点；活性判断 = 连接是否断开 + 握手是否成功 + RPC 是否超时，外加 WebSocket 协议层 ping/pong（`app-server-daemon/src/client.rs:34-60`、`app-server-transport/src/transport/websocket.rs:362-368`）。不存在"每 N 秒探测、失败弹遮罩"的设计。
2. **探活只在启动屏障里发生**。daemon 拉起时 50ms 一次、最长 10s probe 握手（`app-server-daemon/src/lib.rs:297-333, 451-466`），是启动动作，不是后台常驻监控。进程崩溃后没有客户端自动重连退避，靠"下次操作时按需 probe→拉起"吸收。
3. **错误按 thread 粒度标记，永不全屏阻断**。每个 thread 有独立状态机（`ThreadWatchManager`，`app-server/src/thread_status.rs:225-256`），系统性错误只给该 thread 打 `has_system_error` 标记并推 `thread/status/changed` 通知（thread_status.rs:173-181）；其他会话照常。
4. **瞬时错误与终态错误分开**。可重试的流错误包成 `ErrorNotification { will_retry: true }`（`bespoke_event_handling.rs:982-996`），TUI 渲染成对话流里一行"Reconnecting... 1/5"，非模态、自动消失；终态错误才标红该 turn。请求级错误走类型化 JSON-RPC 错误码（`app-server/src/error_code.rs`），天然 inline。
5. **断线恢复靠 rollout 持久化，不靠重放事件流**。事件流是单次消费 fire-and-forget，断线期间消息丢弃（`transport.rs:143`）；恢复 = 重连 → `resumeThread(id)` → 从 rollout 重建历史并下发 `SessionConfiguredEvent { initial_messages }`（`session/session.rs:1459-1488`），被中断的 turn 还能原地复活（`codex_thread.rs:298-327`）。

**设计哲学差异一句话**：codex 把"后端健康"从全局 UI 状态中删掉，下沉为三个正交机制——连接活性（被动）、per-thread 状态机 + 类型化错误通知（错误归属到具体会话的具体 turn）、rollout 支撑的 resume（恢复地基）。UI 永远只面对"某会话某 turn 出了某个具体错误"，永远没有"整个后端不可达所以全屏 loading"这种状态。

**对 Ace 的启示（按依赖顺序）**：① 会话持久化做到可 resume（地基，对应 P2-3.2）；② 协议里区分 will_retry 瞬时错误与终态错误（依赖 P2-3.3 错误类型化）；③ per-session 状态通知替代全局 health 遮罩；④ 连接即活性，只在操作发起点 probe-and-start，撤掉后台 1s 轮询。这与 7.5 的桌面端兜底建议方向一致，但 codex 证明了更彻底的做法：**遮罩这个组件本身可以被消灭，而不是被优化**。

### 7.7 补充：遮罩的历史成因（为什么它存在）

从 `docs/frontend/desktop-frontend.html` 的变更记录可还原：遮罩最初是**冷启动闸门**，不是运行时监控。

- 2026-07-21：Windows 安全软件病态扫描 `cacert.pem` 可使打包版 gateway 就绪超过 60s；无遮罩时 UI 骨架已出但所有页面依赖后端 API，用户面对"每个操作都报错"的假活界面。遮罩 + `waitForHealthApi` 去时间天花板 + 20s 升级按钮即为此而加。
- 2026-07-22：托管 gateway 崩溃后自动重拉（500ms~30s 退避），重启窗口期也需要遮挡；同期修过"端口扫描选出新端口但进程未拉起 → 遮罩永不消失"的死循环。
- 做成全屏阻断的合理性：Ace 桌面端**没有本地降级模式**，几乎所有页面数据来自 gateway，后端死了 app 就是空壳，"启动时全屏等"是托管后端 Electron 应用的常见 launcher 模式。

问题在演化：为"启动/重启"设计的闸门被复用为"运行时健康监控"（1s 轮询 + 3 次失败即弹），启动闸门合理，运行时监控敏感且二值，叠加事件循环阻塞缺陷后变成"后端一忙就全屏 loading"。

改造原则（供第三阶段参考）：**保留启动闸门职责**（冷启动/崩溃重启期间显示进度与日志入口是合理且必要的），**拆除运行时监控职责**（稳定运行后的健康判定交给 WS 连接活性 + per-session 状态机，不再回弹遮罩）。
