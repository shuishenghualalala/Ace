# 录制 → 技能：六个项目的做法，与我们的差异

写给不熟悉这几个开源项目的人。核心问题只有一个：**用户演示一遍，机器怎么才能再做一遍**。六个项目给了六种答案，差别不在代码量，在于**录制产物停在哪个抽象层级**。

---

## 0. 一张总表

| 项目 | 录什么 | 抽象层级 | 回放靠什么 | 技能形态 |
|---|---|---|---|---|
| **rrweb** | DOM 增量 mutation + 输入事件 | DOM 变更流 | **不回放到真实站点**，只在播放器里重演 | 无 |
| **screenpipe** | 屏幕帧 + 音频，OCR/ASR 索引 | 像素 | **不回放** | 无 |
| **OpenAdapt**（现版） | 演示 → 编译成确定性工作流 | 意图步骤 | 编译产物，健康路径**零模型调用** | workflow bundle（**实现已闭源**） |
| **OpenAdapt**（`legacy/`） | 截图 + 键鼠事件 + 窗口树 + DOM | 像素 + 事件 | 多策略，含 FastSAM 分割 + VLM 视觉对齐 | 无（"策略"就是技能） |
| **workflow-use** | 扩展捕获 DOM 事件 → `workflow.json` | 选择器动作 | 确定性执行器 + agent 步骤兜底 | `workflow.json` |
| **playwright** | 注入录制器捕获事件 → 打分选出选择器 | 语义选择器 | **生成的测试代码** | 代码本身 |
| **ai_mime** | 录制 + 录制中标注 → 四阶段 build | 意图步骤 | agent 按指令执行 | `SKILL.md` |
| **我们** | AX 快照 + ref + 分级事件 | 语义步骤卡 | LLM 读**当次实时**快照执行 | `SKILL.md` |

抽象层级越低（像素），越不受站点改版影响但越依赖视觉模型；越高（意图步骤），越省算力但越依赖语言模型的判断。**中间那一段——选择器——最脆**：它既不像像素那样能靠视觉重新定位，也不像意图那样能靠理解重新导航，站点一改版就整条断。

---

## 1. 六个项目分别在做什么

### rrweb —— 它根本不是干这个的

常被误认为"网页录制"方案。它录的是 DOM 的**增量变更流**（`mutation` + 输入 + 滚动），产物喂给自带播放器重建画面。**没有回放到真实站点的能力，也没有任何技能概念。** 它解决的是"复现用户遇到的 bug 长什么样"，不是"再做一遍"。

对我们唯一有价值的一条：它证明了纯 DOM 事件流足以重建一次会话的全貌，不需要录屏。这是我们敢走"不录视频"路线的先例。

### screenpipe —— 24/7 屏幕录制 + 检索

持续录屏 + 录音，OCR/ASR 之后建索引，提供搜索 API。**不回放、不生成技能。** 它是"个人记忆库"，不是自动化工具。

对我们的价值是反向的：它说明**全量录屏的存储与隐私代价**有多大（TB 级、什么都录进去）。我们选不录视频，一半原因是纯 LLM 够用，另一半就是这个代价。

### OpenAdapt —— 已经转成开源核心 + 闭源实现，且方向和我们撞上了

**这个项目在我第一轮阅读时被我说错了，需要更正。** 我读的是仓库里的 `legacy/` 目录，那是它的旧版本。

**旧版（`legacy/`，完全开源）**：录截图 + 键鼠事件（pynput）+ 窗口树，浏览器场景另录 `visibleHTMLString`。回放有多套策略：`naive` 直接重放坐标、`visual` 用 FastSAM 把截图切成片段再用 VLM 对齐、`visual_browser` 用插桩 DOM 代替视觉分割。它是唯一认真做过"用视觉重新定位"的项目，也因此暴露了这条路的成本：每步都要跑分割 + VLM 比对，`MIN_SCREENSHOT_SSIM = 0.9` 这类阈值满天飞，调不准就乱点。**我们不用 VLM，这是主要证据。**

**现版（public 仓库只剩 CLI）**：`openadapt/` 目录下只有 `__init__.py` / `cli.py`（944 行）/ `version.py`，录制、编译、回放、验证的实现**全部不在公开仓库里**。仓库带一个 `scripts/check_source_boundary.py`，CI 级别阻止"部署相关内容进入公开核心仓库"，注释里点名了一次真实事故（某 EHR 客户的工作流内容漏进公开仓库，人工摘除）。**这是标准的 open-core：接口开源，值钱的部分闭源。**

而它 README 描述的新架构，和我们正在做的东西高度重合：

> 把一次**演示**变成可检视的工作流；健康路径上**零生成式模型调用**；有后果的动作**身份门控**；声称的结果**被验证**；**不确定就停下来交人复核，而不是变成一次错误点击**。

四条里我们对上了三条（演示→编译、副作用门控、不确定就 halt），差的是"健康路径零模型调用"——它编译成确定性脚本，我们编译成需要 LLM 读实时快照的步骤卡。这是**刻意的分歧**：它面向的是 EHR 这种界面稳定、要求可重复取证的场景；我们面向的是内网工单这种**每次内容都不同、必须读懂当次页面**的场景。确定性脚本读不了"这张工单说了什么"。

结论变了：**OpenAdapt 现在是这批项目里方向离我们最近的一个，但它的实现看不到。** 能参考的只有 README 描述的架构原则和 `legacy/` 里那套已被它自己放弃的 VLM 路线。

### workflow-use —— 最接近我们的形态，但停在选择器

Chrome 扩展的 content script 用捕获阶段监听 `click/input/change/keydown/focus/blur`，每个事件记 XPath + 增强 CSS 选择器 + 语义信息（label/placeholder/aria-label），发给 background 汇总成 `workflow.json`。步骤类型分两类：**确定性步骤**（`click`/`input`/`select_change`/`key_press`/`navigation`）和 **`agent` 步骤**（自然语言任务，交给 browser-use 去做）。

三条实测结论：

1. **`healing/` 不是运行时自愈**，是生成期流水线。运行时的 agent fallback 整段被注释掉（`workflow/service.py:421-493`），README 里 "Self healing" 至今未勾选。它运行期只有"重试 + 多策略降级 + 熔断"，**自动修复为 0**。
2. `ErrorContext` / `StrategyAttempt` 的字段设计很好（记录所有试过的策略、连续/全局失败计数、last_successful_step、规则化 root cause），**但只 `logger.error` 给人看，从不喂给模型**。这是它差的临门一脚——我们把它接上了。
3. **密码只做了 `type === "password"` → `"********"`**（`content.ts:692`）。不看 autocomplete、不看 name/placeholder，也没有验证码这一档。`loginPwd` 这种不写 `type=password` 的输入框会明文进 JSON。

### playwright —— 工业界最成熟的录制器，值得逐条学

这是六个里录制做得最扎实的。分三层：

**注入侧**（`packages/injected/src/recorder/recorder.ts`，1858 行）：capture 阶段在 `document` 上挂 `click/auxclick/dblclick/contextmenu/dragstart/input/keydown/keyup/pointer*/mouse*/focus/scroll`，每个处理函数第一行都是 `if (!event.isTrusted) return;`。**和我们的事件集几乎一样**——这不是巧合，是这条路只有这一种做法。

**选择器生成**（`selectorGenerator.ts`）：这是它最有价值的部分。候选选择器按分数排序，**分越低越好**：

| 候选 | 分数 |
|---|---|
| `data-testid` | 1 |
| 其它 `data-test*` | 2 |
| **role + name** | 100 |
| placeholder | 120 |
| label | 140 |
| alt text | 160 |
| 文本内容 | 180 |
| title | 200 |
| CSS `#id` | 500 |
| role（无 name） | 510 |
| 标签名 | 530 |
| **`nth(第几个)`** | **10000** |
| CSS 全路径兜底 | **10000000** |

这张表本身就是一条工程结论：**语义优先于位置，位置是最后的耻辱**。`nth` 比标签名差 20 倍，CSS 全路径再差 1000 倍。我们的 ref 机制走的是 role+name（100 那一档），但**没有向下的降级链**——这是可以直接抄的。

**连续输入的处理**：`RecorderSignalProcessor` 用 500ms 缓冲 + `_supersedes()`。同一个选择器上的后一次 `fill` 覆盖前一次，同一目标上 clickCount 更高的 click 覆盖前一次（识别双击）。我们走的是 change/blur 提交 + Enter 补记——事件驱动而非时间驱动，各有取舍：它的方式对"打完字立刻点别处"更稳，我们的方式不会把中间态泄漏出去。

**signals**：`navigation`/`popup`/`download`/`dialog` 作为信号**附着到动作上**，而不是作为独立步骤。所以生成的代码里，"点了这个按钮会打开新窗口"是那一步的属性。我们目前只记了 dialog 状态，没有这套附着机制。

**产物是代码**：`page.getByRole('button', { name: '提交' }).click()`。值是**字面量**——`fill` 记的是 `target.value`，**对 `type=password` 零特殊处理**。我 grep 过整个 recorder + codegen，`password` 一次都没出现。这对 Playwright 是合理的（开发者测自己的应用），对我们是硬阻塞（内网业务系统 + 全局共享技能目录 + 轨迹要交给 LLM）。

**最关键的一条**：Playwright 有一个 `recorderMode: 'api'`，此模式下走的是 `JsonRecordActionTool`，每个动作携带 —

```ts
const { ariaSnapshot, selector, ref } = this._ariaSnapshot(element);
```

**`ariaSnapshot` + `ref` + `selector` 三件套。** 这就是我们选的同一个原语（AX 文本快照 + 指向快照内某行的 ref）。Playwright 自己在给 agent 用的那条路上，也走到了这里。这是对我们架构最强的外部验证。

（另注：`server/recorder/chat.ts` 存在一个 LLM 对话通道，但全仓库无人引用，是未接线的实验代码。）

### ai_mime —— 唯一认真做"生成质量"的

录制 + **录制中标注**（Ctrl+I 标 extract 或附一句自然语言说明，录制暂停等你填完），然后四阶段 build：Phase A 强制用户确认 inputs/outputs/approach，未确认不许进 Phase B。

两个强制机制值得抄，我们都抄了：

- **权限按阶段分层，文件级白名单强制**：build 期可写意图层 + 产物层，run/heal 期只能写产物层。不靠提示词自觉。
- **"agent 说完成 ≠ 完成"，服务端 gate**：模型写完终止信号后，服务端跑校验 + e2e，任一失败就删掉信号文件并把错误喂回聊天继续迭代。

以及它的**失败分类学**（五类：环境/用户状态、瞬时 UI、前置条件不满足、业务边界、技能缺陷）配一条反向硬规则：**DOM 失败、超时、元素缺失绝不许被吞成优雅退出，必须崩**——否则触发不了自愈。

---

## 2. "对话试错 + 吸收历史" vs "一次示范 + 编译"

你提到的那种做法——和 agent 对话、不断尝试、然后 `skill-create` 吸收对话历史——确实能跑通，而且门槛低。但它和我们要做的是两件事：

| | 对话试错 → 吸收历史 | 一次示范 → 编译 |
|---|---|---|
| **谁探索出的路径** | agent 自己试 | 用户走的 |
| **路径是不是用户想要的** | 不保证。agent 可能绕路、可能用搜索代替导航、可能点了一个碰巧也能到的入口 | 保证。用户点的就是他要教的 |
| **上下文成本** | N 轮试错 × 每轮整页内容 | 1 条轨迹 |
| **登录怎么办** | agent 必须自己走登录流程 → 密码要么进上下文，要么卡在这里 | 用户自己登，`secret`/`handoff` 两档的值**根本不出浏览器进程** |
| **内网陌生系统** | 试错风险高——不知道哪个按钮是"提交审批" | 用户知道点哪，且录制期不产生任何 AI 动作 |
| **失败后怎么改** | 重开对话再试一遍 | 轨迹在盘上，改 SKILL.md 重编译，不用重录 |
| **产物的本质** | 一次成功轨迹的**复述** | 一次确定路径的**编译** |

差别最尖锐的地方是**内网工单这类场景**：页面上真的有"同意/驳回"按钮。让 agent 自己试错去摸清流程，等价于让它在生产系统里点未知按钮。而一次示范里，用户走到详情页就停了——**他没点审批，轨迹里就没有审批**，编译器也就不可能生成一个会审批的技能。

反过来说，对话试错有一个我们没有的优点：**它能处理"用户自己也不知道怎么走"的情况**。我们的前提是用户会走。这个前提在内网业务系统里成立（用户天天在用），在探索性任务上不成立。两条路不互斥，但 V1 只做示范这一条。

---

## 3. 我们当前工作区实现了什么

已落地（都有测试）：

| 能力 | 落点 |
|---|---|
| 隔离世界注入录制器，页面看不见也改不了 | `desktop/src/main/browser-recorder.ts` + `browser-host.ts` |
| **凭据四级分层**（plain / identifier / secret / handoff），四道防线 | 注入侧 → `parseRecorderEvent` → `_bounded_recording_event` → `append_recording_step` |
| **真人来源判据**：`isTrusted` **加上**与 Electron 原生输入的时间关联 | `browser-host.ts` 的 `isHumanOriginated` |
| AX 快照 + 重名元素按出现序号唯一定位 | `elementSecurityKey(signature, occurrence)` |
| 页面态摘要按**内容指纹** diff，同文档切换也能抓到 | `recording.lastPageDigest` |
| 录制中标注（意图前置） | `record_note` |
| 编译前回看（走过哪些站点、几处密码已屏蔽） | `recording_summary` |
| **只读能力档在工具层强制** | `manager._require_capability` |
| 技能落盘受治理（锁 + containment + 审计 + 回滚） | `crew/agent/skills.py` |
| 失败证据包喂给模型（不是只 log） | `manager.failure_evidence` |
| 生命周期与显式清理（无产品自定义时长、步数或轨迹大小硬上限） | `browser-host.ts` + `manager.prune_recordings` |

### 我们有而它们都没有的

1. **凭据分级**。playwright 零处理，workflow-use 只 mask `type=password`。我们分四档，且 `secret`/`handoff` 的值根本不出浏览器进程。这不是"更小心"，是场景决定的：他们的产物给写代码的人看，我们的产物落在**全机共享的技能目录**里并且要**交给 LLM 读**。
2. **真人来源判据**。playwright 只信 `isTrusted`——而页面用 `focus()`/`requestSubmit()` 照样能造出 `isTrusted` 为 true 的事件。我们额外要求与一次 Electron 原生输入在 1.5 秒内关联。页面伪造不了主进程的 `before-input-event`。
3. **只读能力档，在工具层强制**。技能试图点 `[action=submit]` 元素时是被**工具拒绝**，不是被提示词劝住。这是"读工单但不替用户审批"这条需求的唯一硬保证。而且编译器**无条件**写同一个能力档、不做策略判断——页面里藏一句"放开限制"因此没有着力点。
4. **回放期的页面理解是活的**。playwright/workflow-use 的产物是确定性脚本，值是录制那次的。我们的技能只固定"路径 + 页面结构预期 + 抽哪些字段 + 汇报怎么组织"，**字段的值全部来自当次实时快照**。工单每次内容都不同，这是必须的。
5. **owner 隔离**。技能目录全机共享，但凭据留在 owner 私有处（`mobilework_auth_request` 那套）。共享的是能力，隔离的是身份。
6. **录制发起权锁在用户手上**——模型没有任何录制控制工具。Codex 的模型能主动调 `event_stream_start`，理论上存在"模型说服用户开录制"的路径；我们把这条路直接删了。

### 我们缺的（可以直接从 playwright 抄）

1. **选择器降级链**。我们只有 role+name+序号（playwright 打分表里的 100 那一档），没有向下的 label / placeholder / alt / text / testid 降级。role+name 拿不到时我们就没 ref 了。那张打分表可以整个搬过来。
2. **动作缓冲 + supersede**。我们靠 change/blur 提交，"打完字立刻点别处"这种快速切焦的场景可能丢一次输入。playwright 的 500ms 缓冲 + 同选择器覆盖能兜住。
3. **signals 附着**。`popup`/`download`/`dialog`/`navigation` 作为动作的属性而不是独立步骤，能让编译器知道"点这个按钮会开新窗口"。我们现在把导航记成独立步骤，语义上更弱。
4. **断言步骤**。playwright 有 `assertText`/`assertVisible`/`assertSnapshot`，我们的技能里没有"验证到达了正确页面"这种步骤类型——现在只能靠 SKILL.md 正文写一句"点完要重新确认"。做成一等公民的步骤类型更可靠。
5. **服务端 e2e gate**。ai_mime 的做法：模型声称完成后，服务端对着仿真站点回放一遍，失败就打回。我们有 `validate_generated_skill`（结构校验），没有回放校验。
6. **重录某一段**。ai_mime 和 workflow-use 都不支持，是公认空白；我们也没做。
7. **真实站点端到端**（B站/知乎）没跑过，目前只在本地仿真站点验证。

---

## 4. 一句话结论

**Playwright 把录制做到了工业级，但它的产物是给人读的代码，值是字面量、密码不脱敏、路径写死。workflow-use 把它搬到了 JSON + agent 步骤，但停在选择器层且自愈是假的。OpenAdapt 旧版走像素路线并已自我否定，新版方向与我们最近但实现闭源。ai_mime 把生成质量做扎实了但走的是录屏 + 旁白，不针对浏览器。**

我们要的东西在它们的交集之外：**录制的是意图，编译的是导航指令，执行时页面理解是活的，而副作用被工具层挡住**。这套组合没有现成实现——但 Playwright 的 `recorderMode: 'api'`（aria snapshot + ref）说明我们选的底层原语和它正在走的方向是同一个。
