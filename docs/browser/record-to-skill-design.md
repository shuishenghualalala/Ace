# Browser Record → trace v11 → Playwright Replay v3

本文描述当前默认的录制、编译、安装与回放契约。浏览器内核为精确锁定的
`playwright-core 1.62.0`；真实 Electron 合同在
`desktop/scripts/pw-contract.ts` 中已有 **71** 个检查点，其中包括 trace v11、
replay.v3、多页、弹窗、viewport、上传、drop、pointer gesture、dialog 与 download。

新录制默认写 trace v11，并编译为 `crew.browser.replay.v3`。环境变量
`CREW_BROWSER_RECORDING_V11_PHASE_A=0` 是显式回滚开关；除此之外，Host、bridge、
compiler、store 和 replay manager 都启用 v11/v3。

---

## 1. 当前真实数据流

```text
document-world recorder event packet（内部 schema v10）
  → Electron BrowserHost 关联 page / action / effect transaction
  → owner/session trace.jsonl（持久化 schema v11）
  → record_compile（严格解析 + typed atomic IR）
  → crew.browser.replay.v3 artifact
  → record_install（生成技能只保存 workflow_id 与 capabilities）
  → record_replay
  → BrowserManager replay lease
  → 每个 IR step 一次 Host execute_transaction
  → Playwright action + 原子 effect 匹配 + snapshot
```

四个版本号服务不同边界，不能混写：

| 层 | 当前版本 | 含义 |
| --- | --- | --- |
| 页面事件包 | `RECORDER_EVENT_SCHEMA_VERSION = 10` | 注入脚本到 Host 的内部事件协议 |
| 持久化 trace | `schemaVersion = 11` | append-only action/effect transaction 日志 |
| 可执行 artifact | `crew.browser.replay.v3` | 编译后的多页原子 Playwright IR |
| 生成技能策略 | `crew.browser.policy.v2` | 技能 metadata 的既有能力声明格式 |

页面事件包仍叫 v10，不代表新录制落盘为 trace v10。BrowserHost 在共享 recording
ledger 中把页面事件、宿主导航、popup、下载、dialog、page close 等事实编排为 v11
事务，再写入 JSONL。

工作流 artifact 是 owner 绑定、内容寻址、不可变的。生成技能不复制 URL、selector、
录制值或 plan，只保存 `workflow_id` 和由已验证 plan 精确推导的 capabilities；实际
内容由 `record_replay` 按 owner 读取。

---

## 2. trace v11：动作和结果属于同一个事务

每一行都有以下基础字段：

```text
schemaVersion, type, targetId, recordingId,
step, eventIndex, transactionId, transactionKind,
recordKind, pageGuid, timestamp, provenance
```

`recordKind=action` 的行再带 `action` 与 `evidence`；`recordKind=signal` 的行再带
`signal` 与 `details`。

事务形状是硬契约：

- `transactionKind=action`：恰好一条 action，可跟零条或多条 signal；
- `transactionKind=observation`：没有 action，至少一条 signal；
- `eventIndex` 必须从 1 连续递增；
- transaction 第一次出现时，`step` 必须从 1 连续递增；
- 同一 `transactionId` 的 step 与 kind 必须始终一致；
- 持久化行必须已经是 bridge 的 canonical 结果，不能只做到“可被 normalize”。

当前 v11 action 名称为：

```text
openPage
closePage
navigate
x-crew-navigate
x-crew-activatePage
x-crew-resize
hover
click
fill
check / uncheck
select
press
setInputFiles
x-crew-drag
x-crew-drop
x-crew-pointerGesture
x-crew-scroll
```

当前 signal 名称为：

```text
navigation
popup
download
dialog
x-crew-pageClosed
```

signal 不是日志备注。它会编译为 action 的 `effects`，或编译为独立的
`wait_navigation`、`wait_page`、`wait_download`、`wait_dialog`、
`wait_page_closed` step，并在回放中由 Host 原子匹配。

---

## 3. 录制器如何继承 Playwright

### 3.1 selector 生成

录制器运行在 document world。原因不是偏好，而是 Electron 43 / Chromium 150 的真实
OOPIF 回归：在 document-start instrumentation 之后异步调用 Runtime normalize 或
ariaSnapshot 可能卡住 renderer Runtime 通道。

当前实现从已安装的 Playwright 1.62.0 包中提取官方 InjectedScript source，注入每个
document，并在 trusted DOM event callback 内同步调用
`generateSelectorSimple()`。BrowserHost 只补充权威的 frame-owner selector 链：

- 主文档、普通 iframe、shadow DOM 和 OOPIF 使用同一版本的官方 selector generator；
- selector 保留完整 frame path；
- generator 不可用或 OOPIF frame 无法唯一关联时，录制标记为 incomplete；
- 新录制不会退化成 Crew 自建 `nth-of-type` 或文本猜测。

`Locator.normalize()` 与底层 `_selector` 仍集中在 `playwright-compat.ts`，有真实合同
覆盖，但它不是当前 recorder 的事件热路径。

### 3.2 用户动作与证据

页面 recorder 可观察 click/dblclick、hover、input/change、upload、submit、key、
scroll/wheel、internal drag、external drop、pointer gesture 和页面内 navigate。
Host 还记录地址栏/后退/前进/刷新、页面 activate/close/resize、popup、dialog 与
download。

v11 保存完整功能数据：

- 普通文本、identifier、secret、handoff 字段的录制值都可成为精确默认值；
- `tier` 只是 evidence metadata，不会把该动作替换成 approval 或 takeover；
- query、fragment、跨 host 和签名 URL 按原字符串保存；
- selector、page evidence、文件路径、MIME data 不沿用 v1–v9 的历史截断策略；
- provenance 保留事件来源、capture phase、`browserTrusted` 和 native-input
  correlation，供审计与诊断，不作为逐动作授权门槛。

默认没有表单字段数、文件数、drop MIME 数或 trace 行数的产品自定义 cap。运行时仍受
浏览器、操作系统、内存和传输本身的物理边界约束。

---

## 4. v11 编译为 replay.v3

Compiler 先验证 JSONL 的 canonical 形状、recordingId、连续 eventIndex/step、事务身份、
page 定义顺序与 popup 身份，再按 trace 事务顺序生成 IR。当前动作映射顺序以
`plugins/browser/compile_tool.py::_compile_plan_v11` 为准：

| trace v11 | replay.v3 step |
| --- | --- |
| `openPage` | `open_page`；第一个 root 使用 `reuse_current`，后续 root 使用 `new` |
| `closePage` | `close_page`，且必须有同页 `page_closed` effect |
| `navigate` | `navigate(operation=goto)` |
| `x-crew-navigate` | `navigate(goto/back/forward/reload)` |
| `x-crew-activatePage` | `activate_page` |
| `x-crew-resize` | `resize` |
| `hover` | `hover` |
| `click` | `click` 或按 `clickCount=2` 生成 `dblclick` |
| `fill` | `fill` + text input metadata |
| `check` / `uncheck` | `check(checked=true/false)` |
| `select` | `select` + select input metadata |
| `press` | `press` |
| `setInputFiles` | `upload`；空列表表示 clear |
| `x-crew-drag` | `drag` |
| `x-crew-drop` | `drop` |
| `x-crew-pointerGesture` | `pointer_gesture` |
| `x-crew-scroll` | `scroll` |

观察事务的主 signal 分别映射为：

| signal | replay.v3 step |
| --- | --- |
| `popup` | `wait_page` |
| `navigation` | `wait_navigation` |
| `x-crew-pageClosed` | `wait_page_closed` |
| `download` | `wait_download` |
| `dialog` | `wait_dialog` |

除非 plan 已以 `takeover` 终止，Compiler 会为最后一张仍存活的页面补一个
`snapshot_full`；已有末尾 snapshot 时不会重复添加。

v11 不做旧 v2 的 `fill_form` 合批、last-write-wins 或 URL polling 后置条件改写。
每个录制 transaction 保持原顺序，成为一个原子 replay.v3 step。旧 trace 的归并逻辑
只存在于 legacy v10 → replay.v2 编译分支。

### 4.1 输入 metadata 与默认值

每次 `fill`、`select`、非空 `setInputFiles`，以及带文件的 `x-crew-drop`，按出现顺序
生成 `field_1`、`field_2`……。当前 key 不再从 aria/name/id 派生；这些信息只用于
`display_name`，顺序为 aria-label、name、id、hint，最后回退为 `Field N`。

每个输入包含：

```json
{
  "kind": "text",
  "required": true,
  "display_name": "Username",
  "recorded_hint": "input · text · name=username",
  "default": "recorded value"
}
```

`kind` 为 `text`、`select` 或 `files`。调用者可在 `record_replay.inputs` 中覆盖；
空 inputs 使用录制时保存的精确默认值。只有 artifact 本身没有默认值时，工具才返回
`REPLAY_INPUTS_REQUIRED`。

### 4.2 artifact 与能力集合

Artifact 顶层 schema 是 `crew.browser.replay.v3`。下例只展示字段关系，不是可直接发布
的 canonical artifact（真实 `owner_binding` 与 `workflow_id` 由 Store 计算，plan 与
capabilities 必须互相吻合）：

```json
{
  "schema_version": "crew.browser.replay.v3",
  "owner_binding": "...",
  "hosts": ["app.example", "id.example"],
  "inputs": {},
  "capabilities": ["open_page", "snapshot_full"],
  "plan": [
    {
      "kind": "open_page",
      "page": "p1",
      "url": "https://app.example/",
      "mode": "reuse_current",
      "activate": true,
      "effects": []
    },
    {
      "kind": "snapshot_full",
      "page": "p1",
      "effects": []
    }
  ],
  "workflow_id": "..."
}
```

`hosts` 是录制诊断 metadata，不是执行 allowlist。Store 会重新验证 plan，并要求
capabilities 与动作和 effects 的实际集合完全一致。v3 canonical 顺序为：

```text
open_page, close_page, navigate, activate_page, resize, hover,
click, dblclick, drag, drop, pointer_gesture, press, fill, select,
check, upload, scroll, wait_page, wait_navigation, wait_page_closed,
wait_download, wait_dialog, snapshot_full, takeover,
popup, navigation_effect, download, dialog, page_closed
```

生成技能继续声明 `crew.browser.policy.v2`、`readonly:false` 和上述精确子集。policy
版本没有升级为 v3，因为它与 artifact schema 是两个独立协议。

---

## 5. 多页、弹窗与 viewport

pageGuid 使用 recording-local `pN` 身份贯穿 trace 和 artifact：

- 开始录制时，当前页先写 `openPage`，并保存当时精确 CSS viewport；
- 录制期间 panel bounds 或 resume 引起的尺寸变化写 `x-crew-resize`，相同尺寸去重；
- 录制开始时已存在但未激活的后台 tab 不会被冒充为已录页面；用户第一次激活时才
  lazy-join，补 `openPage`、viewport 与 activate；
- popup 加入同一个共享 ledger，分配新 pageGuid，并保存
  `openerPageGuid + popupIndex + disposition + activate`；
- 显式 activate、close、goto/back/forward/reload 都进入 trace；
- replay.v3 把 pageGuid 绑定到 Host 返回的 immutable targetId，并校验 opener 与 popup
  ordinal，不用 URL 猜哪一个 tab 是本次 popup。

同一动作可以在一个事务里产生 popup、navigation、download、dialog 或 page close 的
组合 effects。短命弹窗即使在下一次 RPC 前已经关闭，也由 Host 在动作执行期间捕获，
不依赖事后 tabs/URL 轮询。

---

## 6. 表单、文件、drag、drop 与 pointer

- `fill` 使用 Locator.fill；`select` 保存完整 option 值数组；checkbox/radio 保存目标
  checked 状态；upload 保存原生文件路径数组，空数组明确表示清空。
- internal drag 保存 source/target selector，以及可选的 source/target position。
  position 使用 Playwright padding-box 坐标，回放走 `Locator.dragTo()`。
- external trusted drop 保存目标 selector、同步可读的所有 MIME 字符串，以及通过
  File wrapper + `DOM.getFileInfo` 取得的多个原生文件路径；回放走 typed drop action。
- `DataTransferItem.getAsString()` 才异步暴露、或被 Chromium/OS 隐藏的数据无法保证
  捕获。实现只保真 trusted drop callback 中同步可见的 types/items/getData/File，不
  虚构缺失 payload。
- pointer gesture 保存 selector、button、modifiers、start、按时间排序的 points，以及
  可用时的 mouse/pen/touch 类型和
  pressure/tangentialPressure/tiltX/tiltY/twist/width/height telemetry。
- mouse 回放走 public `page.mouse`；pen 走 public CDP session 的
  `Input.dispatchMouseEvent(pointerType=pen)`；touch 走 `Input.dispatchTouchEvent`。
  touch 目前只支持单主触点，并用 radiusX/radiusY 恢复 width/height；CDP 的 pen
  mouse-event 协议没有 width/height 字段，因此这两项会保留在轨迹中，但 pen 回放只能
  使用 Chromium 默认接触面。多触点尚未实现；`touch-action:auto` 导致浏览器合法发出
  `pointercancel` 时，不能把它误判为 recorder 丢事件。

这些动作都直接进入 v11/v3，不降级为坐标点击脚本，也不因为字段、文件或采样点数量
触发产品级拒绝。

---

## 7. 下载、dialog 与 effect 身份

普通 click、goto 或 run-code 都可能触发下载，调用方无需预先选择“下载动作”。当前
事实来源是：

- tab 持有 task download directory，popup/new page 继承；
- Electron `will-download` 是 save path 和最终 progress 的唯一落盘事实；
- trace/replay 以 `pageGuid + alias + ordinal + suggestedFilename` 关联预期下载；
- public Playwright Download API 通过 transport 与 native download 配对；
- 同一动作可声明多个 download effects，Host 在事务 deadline 内逐个匹配。

dialog 保存 page、alias、类型、accept/dismiss 和 prompt text。Host 在动作期间安装
observer 并执行录制的处理方式；独立出现的 modal 则编译为 `wait_dialog`。

page close 保存具体 pageGuid 与 reason；popup 保存 opener 和 ordinal。这些都是身份
条件，不是依赖 URL 或出现顺序猜测的提示。

---

## 8. replay.v3：一步一次原子 Host 事务

BrowserManager 对每个 v3 step 发送一次：

```text
execute_transaction({
  schemaVersion: 1,
  transactionId,
  source: { pageGuid, targetId? },
  knownPages,
  action,
  expectedEffects,
  timeoutMs
})
```

Host 在同一个 deadline 内：

1. 验证 source pageGuid/targetId 与 known page bindings；
2. 安装 popup/navigation/download/dialog/page-close observers；
3. 执行一个 Playwright action；
4. 精确匹配全部 expectedEffects；
5. 返回 `matchedEffects`、`pageBindings`、`downloads`、`activePageGuid`、
   `closedPageGuids` 与可选 snapshot。

Manager 只有在返回结构有效且 `matchedEffects === expectedEffects` 后，才提交本地
page alias、closed-page 和 active-page 状态。v3 路径不使用旧 v2 的 tabs/URL/readiness
轮询，也不会把一次动作拆成“先 execute、下一 RPC 再找 effect”。

回放 lease 仍保证 workflow/digest/tool-call/generation 身份、严格 `step_index` 顺序，
并阻止同会话的普通动作与回放交错。它不创建或消费 per-step permit。失败后 lease
终止，同一工具调用不能自动重新开始；错误明确区分 halted、partial 与
outcome-uncertain，并返回最后可靠 snapshot。

---

## 9. 默认功能热路径

当前版本明确以功能稳定、性能和通用性为优先：

- 录制与回放不计算 security digest、DOM/AX fingerprint 或 target attestation；
- 不做逐动作 approval challenge，不创建 replay permit；
- replay action 不要求 `elementSecurity`、origin allowlist 或 securityKey；
- recorder evidence 的 `tier` 与 provenance 只用于描述/诊断；
- snapshot 的 `richMetadata` 仅供显式诊断合同使用，录制与默认 replay 不开启；
- 不设置 `MAX_FINGERPRINTS`、表单字段数、文件数、MIME 数或多页数量等产品 cap。

仍保留的 canonical schema、严格 selector 唯一性、pageGuid/targetId 绑定、action
shape、deadline、owner/session 拓扑和 Playwright actionability 是正确性约束。删除
它们会让动作落到错误页面或让一次事务的结果不可判定，并不会带来上游 Playwright
语义。

---

## 10. 兼容与回滚

- 默认新链路：内部事件 v10 → trace v11 → `crew.browser.replay.v3`。
- `CREW_BROWSER_RECORDING_V11_PHASE_A=0` 时，Host 写 legacy trace v10，compiler/store/
  manager 使用 replay.v2 兼容路径。
- legacy trace v10 与既有 replay.v2 artifact 保持可读；其 `fill_form` 归并和旧
  postcondition 逻辑不能被描述成 v11 的行为。
- replay.v1 只保留原有有限兼容；新 trace v11 不允许静默降级编译成 v2/v1。
- 开始 v3 replay 时，Manager 要求 Host capability 同时声明 recording schema 11、
  artifact schema `crew.browser.replay.v3` 和 `atomicReplayEffects=true`；旧 Host 明确
  失败，不模拟原子语义。

当前 `ManagedChromiumEngine` 已实现官方 Chromium 启动、完整 BrowserContextOptions
和独立合同，但 BrowserHost、Browser RPC、panel 与 recorder 尚未接入它。生产录制和
回放仍走 Electron。未来若允许 replay 选择 managed Chromium，应显式传递
storageState 并定义 page/context 创建协议，不能宣称当前已经自动切换。

---

## 10.5 凭据方案：完整捕获 + 知情披露 + 一次性码强制交人

这是一次**刻意的取舍**，不是遗漏，写下来是为了让后来的人不必重新推一遍。

### 现状

从 recorder schema v10 起，`tier` 只是描述性元数据：密码原值会完整落进轨迹，
并作为运行时入参的默认值写入 artifact，回放时自动填。理由是回放需要真实值——
登录类工作流没有值就跑不动，而"每次都停下来问用户"会让技能失去意义。

`retainRecorderEvidence()`（曾名 `enforceRecorderPrivacyPolicy`）是这条策略的
唯一落点，也是想改用更严策略时唯一需要动的地方。**名字必须如实**：它此前叫
"enforce privacy policy" 而函数体是 `return record`，任何人读到调用点都会
以为这里在按分级抹值。

### 代价由三处承担

| 机制 | 落点 |
|---|---|
| **一次性码强制交人** | `handoff` 档（短信/邮件验证码、图形码、扫码）在编译期无条件变成 `takeover`。这不是安全考虑，是**正确性**：一次性码用过即废，存下来自动填必然被站点拒绝，而工作流不知道，会继续在登录页上跑完剩下的步骤 |
| **安装前知情披露** | `_approval_scope` 列出"内含 N 个凭据字段的录制原值（字段显示名），安装后每次回放会自动填入"。`credential` 标记来自录制期的分级判定，不是安装期猜字段名；它必须活着穿过 `workflow_store` 的严格字段校验，被静默丢掉就等于披露永远报"安全" |
| **安装必须用户确认** | `install_permission_resolver` 返回 `ask`，`allow_always=False`。用户授权录制、授权编译，都不等于授权"以后自动替我登录"。一次"总是允许"会让之后任意一段录制都能无声安装 |

### 面板文案必须如实

指示条写的是「含 N 处密码原值」，不是「N 处密码已屏蔽」。用户正是在这一屏决定
要不要把轨迹交给模型编译——**在这里说假话，他会因为一句假话做出相反的决定。**

### 挂起与续跑（已实现）

`takeover` 分两类语义，这是"登录（含验证码）→ 读工单 → 汇报"能成为**一个**
工作流的前提：

| 类型 | reason | 计划里其后可有步骤 | 运行期 |
|---|---|---|---|
| **挂起型** | `handoff` / `secret` | ✅ | `lease.suspended = True` + 一次性 `resume_token` |
| **终止型** | `explicit` | ❌ | `lease.terminal = True` |

流程：跑到挂起点 → 切 human 模式 → 返回 `REPLAY_SUSPENDED`（带 `resume_token`、
`next_step`、`remaining_steps`）→ 用户填码 → 模型带 token 再调一次
`record_replay` → `resume_replay` 换回 AI 模式、重绑 tool_call_id、
从 `next_step` 继续。

四条关键约束：

1. **`REPLAY_SUSPENDED` 不是失败。** 与 `REPLAY_HALTED` 严格分开：halted 要排查，
   suspended 要等人。混成一种，模型会对着一个健康的工作流反复重试。
2. **挂起时不能走 `end_replay`。** 租约要活下来等用户。
3. **`user_control(takeover/return/pause)` 不掐挂起中的租约。** 挂起的全部意义就是
   让用户在浏览器里做一件事，而那件事必然伴随 takeover/return——按原来的逻辑，
   用户一接管就把租约掐了，一个为用户介入而设计的机制被用户介入本身摧毁。
4. **`resume_token` 一次性、15 分钟过期。** 续跑跨越工具调用边界，原来那道
   `tool_call_id` 绑定必须放开；token 是"这次续跑对应的正是刚才挂起的那一段"的
   唯一证明，用完立刻作废。过期即回收租约，不留一个永远无法续跑的租约挡住后续回放。

### v11 路径曾经漏掉 tier（已修）

v11 是默认 schema，而它的编译路径（`action_step` / `input_metadata`）原本
**完全不看 `tier`**：验证码会成为运行时默认值被自动填入，密码也不会打
`credential` 标记，于是安装摘要对绝大多数录制永远报"不含凭据原值"。
只给 v10 做修复等于没做。

---

## 10.6 运行期门禁全部移除：授权来自 plan，不来自档位

**产品决定（2026-07-30）：这一版优先通用性、性能、成功率与好用，凡是会产生摩擦
的安全门禁一律移除。** 已删除的东西，连同它们的配套，全部删净而不是留空实现：

| 已删除 | 原本做什么 |
|---|---|
| `_require_capability` + 18 个调用点 | 只读档的动作白名单 |
| `_READONLY_ALLOWED_ACTIONS` / `session.readonly` / `_readonly_leases` / `set_readonly` / `clear_readonly` | 只读租约的存储与下发 |
| `_navigation_is_final_action` + 终态动作词表 | 拒绝导航到 `/approve?id=1` 这类地址 |
| 提交类控件的点击拒绝 | 拒绝点 `[action=submit]` |
| 安装前的用户确认（`ask`） | 装技能要点一次「确认」 |

`_apply_browser_skill_policy` 缩成**纯形状校验**：一份写坏了的 `browser_policy`
应该在激活时被发现，但校验失败只记日志，不阻断激活。

`ref_actions`（宿主下发的提交控件标记）保留为**诊断信息**——不再据此拒绝任何
点击，但"这个 ref 是提交控件"对失败归因很有用（点完就跳走 vs 点了没反应）。

### 现在的授权模型

V2/V3 record-replay 的授权来自**不可变 plan** 与**必须精确等于 plan 的
capabilities 声明**（`replay_tool._validate_executable_capabilities`）。这比
"这个会话只读"这种粗粒度档位准确得多：它是按这一次录制的实际动作推导出来的，
既不会拦住正常流程，也不会给出计划之外的能力。

**代价要说清楚**：自由动作面（`click`/`fill`/`evaluate`/`run_code_unsafe`）对
模型完全开放，没有任何运行期拦截。"读工单但不替用户审批"现在靠的是**录制里
没有那个点击**——用户演示时走到详情页就停了，plan 里就没有审批动作。这依赖
plan 的完整性，不依赖运行期闸门。

### 两处刻意保留的（因为它们不产生摩擦）

1. **不可信内容边界**。它拦的是"页面内容冒充系统指令把 agent 劫持掉"——被劫持的
   agent 给出错结果，这是**成功率问题**。它对用户不可见、不阻塞任何操作。
   实现同时改进了（见 §10.8）。
2. **网络请求头的凭据脱敏**。同样零摩擦，泄漏的是用户自己的内网会话。

## 10.7 新增能力：状态断言与意外遮挡

两者共用同一条设计口径——**目标只能引用轨迹里的某一步，模型不能提供 selector**。
这是整条编译链"注入面为零"的关键性质：模型的输入只有 step 序号和一个枚举值。

### `assert_state`

```json
{"assert_step": {"source_step": 4, "state": "visible"}}
```

`state` ∈ visible / hidden / enabled / disabled / checked / unchecked / editable。

为什么必须有：一次失败的导航之后，剩下的步骤会在错误的页面上依次执行，产出一串
对不上任何元素的报错，而真正的原因（第 2 步就没进详情页）被埋在最后。

实现要点：
- **不用 `expect()`** ——它在 `@playwright/test` 里，那是 devDependency，主进程
  拿不到，也不该为几个断言把测试框架打进产品。用 `playwright-core` 的公开
  Locator API 复刻 `expect` 的**语义**。
- **断言必须能等。** visible/hidden 走 `waitFor({state})`（Playwright 原生重试）；
  其余状态走有界轮询，每轮重新读。用 `isVisible()` 那种一次性快照做断言，
  等于要求每个断言前面手工塞一个 wait——忘了塞的那次会随机失败，
  而随机失败的断言比没有断言更糟。
- **断言失败是独立的失败类别**（`assertion_failed`）。混进 `stale_ref` 模型会去
  重新观察，混进 `command_timeout` 模型会去重试，而正确的结论是"这一页不是
  预期的那一页，别往下走了"。
- 断言步骤可以重复引用同一个 `source_step`（点之前确认可见、点之后确认消失），
  动作步骤不行。

### `handle_overlay`

```json
{"overlay_step": {"source_step": 3}}
```

含义：第 3 步点击的元素是遮挡层的关闭控件，注册成 `page.addLocatorHandler`
处理器，而不是在那个固定位置执行一次。

为什么：内网系统最常见的回放杀手不是站点改版，是**随机出现的公告弹窗、满意度
调查、版本更新提示**。录制那次没弹、回放这次弹了，于是每个后续点击都被半透明
遮罩吃掉，而报出来的是"元素不可点击"。按固定位置插一步做不到——弹窗什么时候来
是不确定的；注册处理器之后 Playwright 在**每次** actionability 检查前都会清它。

三个刻意的选择：
1. **用 selector 而不是 ref**：处理器要跨越整场回放存活，ref 表每次快照整张替换。
2. **`.first()`**：多个弹窗排队时关闭按钮会有同名兄弟，strict 多匹配会抛，
   而抛在处理器里会让**触发它的那个动作**失败——本该提高稳定性的机制成了新故障源。
3. **处理器内只点击一次且吞掉失败**：执行时间计入触发它的那个动作的超时预算；
   遮挡也可能在我们点它之前就自己消失（动画、倒计时），那不是错误。

---

## 10.8 不可信内容边界：console 与 network 不再例外

`_console_result` 与 `_network_result` 曾显式跳过 `_bounded()`，理由写的是
"原始诊断 API 要字节精确"。但 `_escape_wrapper_markers` 的文档已经说清楚：
**漏掉任何一处，页面就能用字面 `</untrusted_browser_content>` 逃出隔离区并伪造
`<browser_action_result>` 信封谎报动作成功。** 响应体是最好用的注入载体之一。

现在的划分：**需要字节精确的是落盘那条分支（`filename` 参数），那份不进模型
上下文；模型面一律包裹。** 空结果例外——没有内容就没有注入面。

**转义只针对闭合标记本身，不做全量转义。** 早先把 `& < >` 全部转义，边界牢固但
代价落在正文上：JSON 里的 `<` 变成 `&lt;`、query 里的 `&` 变成 `&amp;`，模型读到
一份被改花的文档，照抄出来的 URL 带着 `amp;`——直接伤成功率。实际需要挡住的只有
"正文里出现字面结束标记、把后续内容顶到边界外冒充 Crew 信封"这一件事，那就只
转义那个字面串：不含它的正文一个字节都不动。三个标签名全部转义，不只当前那一个。

标签分三种（`untrusted_browser_content` / `_console` / `_network`）：三者可信度
一样低，但排障时含义完全不同。

网络请求头另加凭据脱敏：`Cookie`/`Authorization`/`Set-Cookie`/`X-Api-Key` 等
按**小写全名精确匹配**（不做前缀匹配，`x-request-cookie-policy` 这类无害头不该
被抹），只报 `<redacted N bytes>`。保留长度是刻意的：调试 401 时"有
Authorization 且长 N 字节"与"没有"是两个不同的结论，而这个区别不泄漏身份。

**残留风险（未处理）**：请求/响应**体**不脱敏。登录 POST 的 body 里有密码。
这是有意的——body 是这个 API 的全部意义所在，而录制器本来就会捕获密码，
body 的边际风险更低。要更严的话，这里是唯一需要加过滤的地方。

---

## 11. 发布复核清单

发布至少应以真实 Electron 而非 mock 覆盖：

- 初始 viewport、录制中 resize、resume 与 panel/hidden host 往返；
- 主文档、iframe、shadow DOM、OOPIF 的官方 selector 与 incomplete 处理；
- fill/select/check/upload，包含 password/OTP 默认值与调用时 override；
- click/dblclick/hover/press/scroll/internal drag/external drop/pointer gesture；
- 已有后台 tab lazy-join、显式 activate/close、多个 root page；
- popup opener/ordinal/disposition/activate 与短命 popup；
- goto/back/forward/reload、query/hash、跨 host/SSO；
- 一动作多 effect，以及独立 navigation/popup/download/dialog/page-close observation；
- 同动作多下载、public Download API、native 落盘与 popup 继承目录；
- v11 eventIndex/step/transaction/page identity 的拒绝用例；
- v11 编译保持事务顺序，不进入 v2 `fill_form`/URL polling；
- v3 `matchedEffects` 精确一致后才提交 page state；
- timeout、transport loss、partial/uncertain 不自动重放；
- 空 inputs 使用精确默认值，override 按 `field_N` 生效；
- 末尾真实 `snapshot_full`；
- 热路径没有 security digest/fingerprint/approval/permit/cap；
- 显式 gate 回滚仍能读取 legacy v10/v2；
- **安装不弹确认，范围摘要（含凭据字段计数）随 INSTALL_OK 回给模型用于汇报；**
- **`handoff` 档一律编译成 takeover（v10 与 v11 两条路径都要），
  且不留下无人消费的入参；**
- **挂起 → 用户手工完成 → 带 resume_token 续跑，跨越 takeover/return 不丢租约，
  token 一次性、过期回收；**
- **`REPLAY_SUSPENDED` 与 `REPLAY_HALTED` 在真机上确实是两种不同的返回；**
- **`assert_state` 在真实页面上能等到状态成立，且失败报 `assertion_failed`；**
- **`handle_overlay` 在真实弹窗出现时自动清除，且弹窗自行消失时不报错；**
- **console/network 返回给模型的文本落在不可信包裹里，落盘那份仍字节精确；
  且正文除闭合标记外一个字节不改（JSON 的 `<`、query 的 `&` 原样保留）；**
- **网络请求头里的 Cookie/Authorization 只报长度。**

`desktop/scripts/pw-contract.ts` 的 71 项是当前实测下限，不是“所有网站已经覆盖”的
声明。继续增加跨 OS/CPU、长时录制、复杂编辑器、拖放框架和真实 SSO/OAuth 回归，才
能维持通用性。
