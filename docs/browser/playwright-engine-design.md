# 浏览器自动化引擎：内置 playwright-core（当前决策）

目标：保留 Crew 的 Electron 登录态、面板、owner/session 生命周期和录制体验，同时把
定位、actionability、输入、页面生命周期与通用 Playwright 能力交还给官方
`playwright-core`。

本文以当前代码为准。当前精确版本是 **playwright-core 1.62.0**；真实 Electron
契约位于 `desktop/scripts/pw-contract.ts`，当前覆盖 **71** 个独立检查点。检查点会
继续增加，因此这里不冻结为一个容易过时的精确分母。

---

## 0. 结论

1. **不复刻 Playwright。** Crew 内置官方 `playwright-core`，不维护 selector、
   accessible name、actionability、frame/worker 或 Browser/Context/Page 的分叉实现。
2. **生产 Electron 引擎已经使用 Playwright。** 每个 owner 拥有一个
   `PlaywrightEngine`、一个 `ElectronCdpTransport` 和一个合成 persistent context；
   `BrowserHost` 当前仍在 owner 创建时直接构造 `new PlaywrightEngine()`。
3. **CDP transport 保留，但只做 Electron 适配。** 它把 page 级
   `webContents.debugger` 合成为 Playwright 需要的 browser/root/page/child session
   拓扑，并补齐 Electron 缺失的少量协议语义；它不再实现 selector 或动作算法。
4. **默认功能热路径追求官方语义和低开销。** snapshot、locate、普通动作和 replay
   不计算 security digest、DOM/AX fingerprint，不做逐动作 approval/permit，也不设
   `MAX_FINGERPRINTS`、表单字段数、文件数、MIME 数等产品 cap。仍保留的 owner/tab
   归属、ref generation、参数形状、命令 deadline、strict Locator 和 Playwright
   actionability 是功能正确性边界，不是额外安全审批。
5. **`richMetadata` 仅用于诊断。** BrowserHost 的生产 snapshot 不传
   `richMetadata:true`；普通 ref 只保存 Playwright aria-ref Locator。诊断契约可以显式
   开启 rich metadata，但其 fingerprint、`elementSecurity`、`securityDigest` 不得成为
   默认动作的前置条件。
6. **第二后端已经实现，尚未接入生产会话选择。** `ManagedChromiumEngine` 可以直接
   启动与 1.62.0 配套的官方 Chromium，并暴露完整 public
   Browser/BrowserContext/Page；但现有 Browser RPC、面板与录制器仍固定使用 Electron
   拓扑，不能声称生产已经自动切到 managed Chromium。

---

## 1. 为什么应继承 Playwright，而不是继续补手写 CDP

旧 `browser-host.ts` 曾经同时承担协议、快照、ref、actionability 和输入分发。核心
差距不是多补几个 selector，而是缺少 Playwright 长期维护的整套浏览器语义：

| 旧手写职责 | 当前官方职责 |
| --- | --- |
| AX tree 拍平与 `backendNodeId` ref | `ariaSnapshot({ mode: 'ai' })`、aria-ref Locator |
| 自建 retry / hit target 近似 | Locator strict resolution 与 actionability |
| `Input.dispatchMouseEvent/KeyEvent` 拼动作 | Locator、Page.mouse、Page.keyboard |
| 自建 iframe/shadow DOM 定位 | Playwright selector/frame 链 |
| 导航后重新猜元素身份 | Playwright Locator 每次在当前 DOM 解析 |
| 自建 Page/worker/popup 会话关系 | public BrowserContext/Page/Frame/Worker 生命周期 |

当前 snapshot 会把 Playwright 的 `eN`/`f1eN` 重编号为 Crew 对外的 opaque `@eN`，
但不裁剪 Playwright 返回的层级、文本或 iframe 内容。新 snapshot 整体替换旧 ref 表；
Python 再以 `pN` generation 防止跨快照误用。这是 ref 生命周期一致性，不是目标指纹
授权。

### 1.1 两条 selector 路径

- **模型即时动作：** `ariaSnapshot` 产生临时 aria-ref，动作直接构造 exact Locator。
- **录制持久化：** 录制器把 1.62.0 包内的官方 InjectedScript/selector generator
  注入每个 document，在 trusted DOM 事件回调里同步调用
  `generateSelectorSimple()`；Host 只补权威 frame-owner 链。

`Locator.normalize()` 和底层 `_selector` 仍由 compat 层提供并由真实契约覆盖，适合
把已有 Locator 转成可持久化 selector。但生产录制器不能在 document-start
instrumentation 后异步调用 `normalize()`：真实 Electron + OOPIF 上，这会卡住
renderer Runtime 通道。因此当前录制不是 Crew 启发式，也不是事后 normalize，而是
**同步运行同版本 Playwright 的官方 selector generator**。生成器不可用时录制被标记
incomplete，不降级写入 `nth-of-type` 猜测。

---

## 2. 真实 Electron 契约：71

基线为 Electron 43 / Chromium 150、playwright-core 1.62.0、真实
`WebContentsView`、进程内 transport、不开 remote debugging port。71 个检查点覆盖的
不是 mock API 形状，而是实际浏览器行为，主要包括：

| 领域 | 已验证行为 |
| --- | --- |
| 握手与后台运行 | connectOverCDP、隐藏窗口、rAF、自动等待、截图 |
| 定位与快照 | AI aria snapshot、aria-ref、主文档/iframe selector、shadow DOM、OOPIF |
| 动作 | click/fill/select/check/upload/hover、键盘、坐标鼠标、wheel、resize |
| 复杂输入 | canvas pointer gesture、pen/touch 元数据、internal drag、external drop |
| BrowserContext | cookies、add/clear cookies、storageState、permissions |
| Worker/PDF | service worker 发现/evaluate/detach；Page.pdf + IO stream |
| 生命周期 | 多标签 late attach、public newPage/close、popup opener、面板来回迁移 |
| 对话框 | alert/confirm/prompt、链式/延迟/onload/popup modal |
| 下载 | click/goto/run_code、同动作多文件、public Download API 与 native 落盘 |
| 通用代码 | public Page façade、route 长驻回调、timeout 撤销、并发归因 |
| 录制/回放 | trace v11、replay.v3、视口、多页导航、popup、iframe/OOPIF、upload |

以下新增协议能力有独立真实契约，不能只按“run_code 理论可达”来宣称：

- **cookies/storageState：** public `BrowserContext.addCookies()`、`cookies()`、
  `storageState()`、`clearCookies()` 映射到 owner 的真实 Electron Session；无页面时
  使用 Session cookie API 保持 persistent context 语义。
- **permissions：** `grantPermissions()` / `clearPermissions()` 在 zero-page 时写入
  有序 journal，下一张真实 Page 建立后重放。
- **service worker：** worker child session 提升到 context，支持 public
  `serviceworker` event、`evaluate()`、去重与 detach。
- **PDF：** public `Page.pdf()` 映射到 Electron `printToPDF`，支持 base64/stream、
  `IO.read`/`IO.close` 和 detach 清理。

---

## 3. Electron transport 的真实边界

`webContents.debugger` 是 page 级会话，Playwright connectOverCDP 需要 browser 级
端点，所以 transport 不是字节转发器。它必须：

- 本地回答 `Browser.getVersion`、`Target.setAutoAttach`、`Target.getTargets` 等 root
  命令；
- 为每个 `WebContentsView` 合成稳定 target/page session；
- 路由 Electron 第四参数携带的 OOPIF/worker sessionId；
- 为 `newCDPSession()` 建立别名 session，并把事件扇出到正确别名；
- 把 public `Target.createTarget` / `closeTarget` 接回 BrowserHost 的真实
  WebContents 生命周期；
- 在边界处实现 cookies、permissions、PDF、service worker 与 download protocol
  translation。

当前 capability 如实声明：

```text
existingPages=true
oopifAndWorkerSessions=true
pageCdpSessions=true
createPage=true
closePage=true
persistentContextCookies=true
persistentContextPermissions=true
pagePdf=true
serviceWorkers=true

createBrowserContext=false
independentAliasEventDomains=false
browserScopedForwarding=false
```

后三项是 Electron 合成拓扑的边界，不应靠扩大命令白名单假装成完整 Browser endpoint。

---

## 4. 隐藏窗口运行的三个必要条件

窗口全程 `show:false` 可以完整运行，但以下条件必须同时成立：

| 变体 | rAF | click | screenshot |
| --- | --- | --- | --- |
| view 隐藏、无焦点模拟 | ✗ | ✗ timeout | ✗ 0 宽度 |
| view 可见、无焦点模拟 | ✗ | ✗ timeout | ✓ |
| 焦点模拟、view 隐藏 | ✓ | ✗ 视口外 | ✗ 0 宽度 |
| 焦点模拟 + view 可见、但未挂窗口 | ✓ | ✗ 视口外 | ✗ 0 宽度 |
| 焦点模拟 + view 可见 + 挂隐藏窗口 | ✓ | ✓ | ✓ |

三个条件分别是：

1. `Emulation.setFocusEmulationEnabled({ enabled: true })`，让 rAF 推进并满足元素稳定性；
2. `view.setVisible(true)`，获得真实视口；
3. `addChildView` 挂到一个 `BrowserWindow`，窗口本身可以永远不 show。

当前 `AutomationHost` 已实现条件 2、3，`PlaywrightEngine` 实现条件 1，并在 AI/human
切换时可逆开关焦点模拟。view 在隐藏宿主与可见面板之间移动时，真实契约已验证
debugger、targetId 和页面状态保持。

不要“优化”掉三条件中的任意一条，也不要用 `force:true` 掩盖后台 actionability
失败。后者会退回旧手写 CDP 的不可靠语义。

---

## 5. 当前架构

```text
Python BrowserManager
  owner/session、公开 ref generation、replay lease、artifact/下载目录
            │ Browser RPC
            ▼
Electron BrowserHost
  owner/profile/Session、WebContentsView、panel、recorder、download、modal
            │
            ├─ PlaywrightEngine（当前生产）
            │    ├─ AutomationHost
            │    └─ ElectronCdpTransport
            │
            ├─ playwright-snapshot/actions/run-code/network/console
            └─ playwright-compat.ts（唯一 Playwright 版本边界）

ManagedChromiumEngine（已实现、可独立验证）
  playwright-core chromium.launch()
  → public Browser → full BrowserContextOptions → public Page
  → 尚未接 BrowserHost / Browser RPC / recorder / panel
```

per-owner 隔离由拓扑实现：一个 owner 对应一个 transport 和一个 Playwright Browser，
transport 只注册该 owner 的 view。生产没有在 owner 之间共享一个可枚举全部页面的
Browser。

---

## 6. 默认热路径

当前默认调用序列：

| 操作 | 主要 RPC |
| --- | --- |
| snapshot/find | tab list → 一次 aria snapshot |
| ref 动作 | tab list → exact Locator action → tab list → snapshot |
| locate 回放动作 | tab list → persisted selector strict locate → action |
| mouse/keyboard/resize | tab list → public Page input |
| replay.v3 | 一步一个 Host `execute_transaction` |

默认路径的明确不变量：

- snapshot 不传 `richMetadata:true`，不逐 ref `evaluate()`；
- locate 不 normalize、不生成 security key、不计算 fingerprint；
- 动作不读取 `securityDigest` / `elementSecurity`，不走
  `_target_still_matches_snapshot` 或 `_ref_marker_still_matches`；
- permission resolver 只做调用形状与当前 ref/generation 检查，不发 approval challenge；
  `confirm_approval()` 在功能路径恒为 false；
- replay.v3 不创建或消费 per-step permit；
- recorder、trace、表单、文件、drop data 默认没有产品自定义数量/长度 cap。

`page_guard(include_security=false)` 仍可用于等待导航安静或读取 viewport/DPR；这不是
security scan。旧 `securitySurface()` 已从 BrowserHost 删除；只有 snapshot 模块中
显式 `richMetadata:true` 的诊断结构仍能生成 fingerprint，它不进入默认功能路径。

---

## 7. 与 Playwright 1.62.0 官方 78 个 browser tools 的差距矩阵

该矩阵比较的是本地 `playwright-main/packages/playwright-core/src/tools/backend`
注册的 78 个 `browser_*` tools 与 Crew 当前能力面。它是**语义/可达性审计**，不是说
每个 B 项都已有独立真实 Electron 契约。

| 类别 | 数量 | 含义 |
| --- | ---: | --- |
| A | 38 | Crew 有显式 typed action/RPC |
| B | 37 | 没有显式工具，但可从 `browser_run_code_unsafe(page)` 使用 public Playwright API |
| C | 0 | 78 项中没有“Electron 结构上完全不能做、只有 managed 才能做”的项 |
| D | 3 | 当前没有等价实现或存在明确语义缺口 |

### A：38 个显式能力

```text
browser_check browser_click browser_close browser_console_clear
browser_console_messages browser_drag browser_drop browser_evaluate
browser_file_upload browser_fill_form browser_find browser_handle_dialog
browser_hover browser_keydown browser_keyup browser_mouse_click_xy
browser_mouse_down browser_mouse_drag_xy browser_mouse_move_xy browser_mouse_up
browser_mouse_wheel browser_navigate browser_navigate_back browser_navigate_forward
browser_network_clear browser_network_request browser_network_requests
browser_press_key browser_reload browser_resize browser_run_code_unsafe
browser_select_option browser_snapshot browser_tabs browser_take_screenshot
browser_type browser_uncheck browser_wait_for
```

其中三项不能写成“参数完全同构”：Crew 的 `close` 服从自身 tab/owner 生命周期；
snapshot 的可选输出参数更窄；screenshot 目前没有完整暴露上游 WebP/scale 组合。

### B：37 个 public API 可达、但未显式暴露

```text
browser_cookie_clear browser_cookie_delete browser_cookie_get browser_cookie_list
browser_cookie_set browser_generate_locator browser_hide_highlight browser_highlight
browser_localstorage_clear browser_localstorage_delete browser_localstorage_get
browser_localstorage_list browser_localstorage_set browser_network_state_set
browser_pdf_save browser_press_sequentially browser_resume browser_route
browser_sessionstorage_clear browser_sessionstorage_delete browser_sessionstorage_get
browser_sessionstorage_list browser_sessionstorage_set browser_set_storage_state
browser_start_tracing browser_start_video browser_stop_tracing browser_stop_video
browser_storage_state browser_unroute browser_verify_element_visible
browser_verify_list_visible browser_verify_text_visible browser_verify_value
browser_video_chapter browser_video_hide_actions browser_video_show_actions
```

run-code VM 只注入可撤销的 public `page` façade，没有 `require`、`process` 等 Node
globals；`page.context()` 等 public 对象仍可使用。route/event callback 有注册、撤销、
超时与异步错误归因逻辑，但没有显式 typed tool 时，模型发现性、参数校验和结构化返回
仍弱于上游 MCP。

### D：3 个缺口

- `browser_annotate`：Crew 没有同语义 annotation 工具。
- `browser_get_config`：没有暴露上游 MCP 配置快照。
- `browser_route_list`：route/unroute 可执行，但没有等价的可枚举 route registry。

### P0 / P1

- **P0：保证功能闭环。** 保持 run-code 作为完整 public Page 逃生舱；优先把高频且需要
  结构化返回的 B 项提升为 typed RPC（cookies/storageState、route 生命周期、PDF、
  tracing/video、locator verification/generation），并为 D3 明确实现或明确拒绝，不再
  用模糊 fallback。录制/回放继续以 trace v11 → replay.v3 为唯一新主线。
- **P1：补齐官方 Browser/Context 拓扑。** 把已实现的 managed Chromium 通过会话创建
  协议显式接入，服务多 context、完整 BrowserContextOptions、trace/video 和隔离回放。
  录制仍固定 Electron；若回放切 managed，登录态通过显式 storageState 导入导出，不把
  Electron Session 冒充另一个 BrowserContext。

不建议为追求“78 个名字相同”复制 upstream tool handler。Crew 应复用 public API，并
只为产品需要的 typed surface 写薄适配。

---

## 8. playwright-core 官方更新兼容策略

当前唯一支持的升级方式：

1. `desktop/package.json` 精确同步锁定 `playwright-core` 与 `@playwright/test`；当前均为
   1.62.0，不使用 `^`/`~`。
2. 所有源码只从包根导入，且仅 `playwright-compat.ts` 可以直接 import
   `playwright-core`。静态门禁拒绝其它文件和 `playwright-core/lib/**` import。
3. compat 集中收口升级敏感点：
   - `Locator.normalize()` 结果的 `_selector`；
   - `aria-ref` selector engine；
   - 从 `lib/coreBundle` 结构化提取官方 InjectedScript source。
4. `check:playwright-boundary` 校验 manifest/lock/node_modules 版本一致、compat
   结构、esbuild external 与真实契约覆盖。
5. managed Chromium 的 Chromium、headless shell、FFmpeg 按 OS/CPU staging，marker
   同时锁 core 版本、平台和架构；缺任一 artifact 即失败。
6. 升级 PR 必须跑 typecheck、unit、真实 hidden Electron contract、managed Chromium
   contract。CI 还会在 macOS/Linux/Windows 上测试 pinned 版本，并定时用 exact
   candidate 测 latest/next。

这套策略不承诺私有布局永远不变；它保证变化时在升级流水线显式失败，而不是上线后
静默退化到 Crew 启发式。

---

## 9. 录制、回放和下载的生产边界

- 新录制默认写 trace v11，编译为 `crew.browser.replay.v3`；详见
  `docs/browser-record-to-skill-design.md`。
- v11 保存 pageGuid、opener/popup 顺序、动作与 effect transaction、初始 viewport 和
  resize；回放每一步由 Host 原子匹配 popup/navigation/download/dialog/page-close。
- internal drag 保存 source/target selector 以及 Playwright padding-box 坐标；
  external drop 保存目标 selector、同步可读 MIME data 和通过 `DOM.getFileInfo` 取得的
  原生文件路径。
- 普通动作无需调用方预判下载。任务下载目录由 tab 持有并被 popup/newPage 继承；
  Electron `will-download` 是唯一落盘事实源，public Download API 通过 transport 与其
  配对。
- 同名多文件自动唯一化，没有产品数量或大小 quota；当前显式 download RPC 的单文件
  字节上界仍受既有 wire signed-int 协议约束，而不是 Playwright 产品策略。

---

## 10. 已知边界

- managed Chromium 已经能启动、staging、打包并运行独立契约，但尚未接入生产
  BrowserHost/Browser RPC；这项状态必须如实保留。
- Electron 后端是一个 owner 对应一个合成 persistent context，不支持 public
  `browser.newContext()`；需要多 context 时应选 managed，而不是扩张 transport 伪装。
- B 类能力大多只有 run-code 可达，不等于都有稳定 typed UX 或 71 项契约中的独立覆盖。
- document-start recorder + OOPIF 不能安全地在事件后调用 async normalize/ariaSnapshot；
  当前同步官方 selector generator 是有真实失败用例支撑的取舍。
- external drop 只能保真浏览器在 trusted drop 回调中同步暴露的
  `DataTransfer.types/items/getData()` 与 File wrapper。只通过异步
  `DataTransferItem.getAsString()` 或被 OS/Chromium 隐藏的数据不能宣称已捕获。
- pointer gesture 当前支持 mouse、pen 与单主触点 touch。mouse 使用 public
  `page.mouse`，pen/touch 使用 public CDP session；pen 的 CDP mouse-event 协议不能
  恢复 width/height，多触点尚未实现，`touch-action:auto` 也可能按浏览器语义触发
  `pointercancel`。
- 仍需持续补真实复杂站点、长时录制、跨 OS/CPU 安装包回归；71 是当前契约下限，
  不是“所有网站已经证明”的营销数字。
