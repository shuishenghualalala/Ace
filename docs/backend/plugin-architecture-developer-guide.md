# Ace 插件化架构开发规范

> 定位：所有参与 Ace 开发的协作者的**必读规范**。这份文档回答三个问题：架构为什么长这样、新代码应该放哪、提交前必须验证什么。
> 上位设计：`docs/todo/ace-plugin-architecture-evolution-plan.md`（演进方案，含完整设计论证）。本文只讲**落地后的约定与红线**，与方案冲突时以代码现状为准，并在修订记录中说明。
> 修订记录：2026-09-16 初版（基线 `refactor/plugin-architecture-stage-0`，后端全量 4200+ tests）。

---

## 0. 三十秒版本

1. **除最小 Kernel 外，一切运行能力都是 Feature**：Agent Loop、Tools、Session、Wiki、Team、Channels、外援、看板、目录插件，全部走同一个 `FeatureRuntime` 生命周期。
2. **依赖方向唯一：向下。** Host → Feature → Feature Runtime → Kernel，反向 import 由 `.importlinter` 9 条契约在 CI 直接打红。
3. **谁注册，谁释放。** 任何贡献（Service / Tool / Route / Event / Driver / Preset / 后台任务）必须经 `FeatureInstallContext.register_*` 注册，返回 `RegistrationToken` 归属 Feature Scope，禁止游离资源。
4. **新能力走扩展点，不改中心。** 新增 mode、上下文、事件、路由、UI 入口时，不允许修改 `CrewApp.handle()`、`build_app()`、核心协议枚举、前端中心导航——只允许新增 Feature 并向 Registry 注册贡献。
5. **失败必须响亮。** 配置错误、依赖缺失、激活失败一律 fail-loud / fail-closed，禁止静默降级、静默吞异常、半激活状态。
6. **提交前必跑验证链**：pytest + ruff + lint-imports（前端改动再加 desktop/web 各自门禁），详见 §10。

如果你只来得及读一节，读 §3 架构红线。

---

## 1. 架构十分钟总览

### 1.1 分层与依赖方向

```text
┌─────────────────────────────────────────────────────┐
│ Host 层（产品外壳，不认识具体 Feature 内部）          │
│   CLI · Gateway(FastAPI/WS) · Desktop Shell · Web    │
├─────────────────────────────────────────────────────┤
│ Feature 层（全部运行能力，同一 Runtime 同一生命周期） │
│   必需 Core Features（required_by_product，不可停用） │
│     core.agent-presets · Agent Loop · Tool Runtime … │
│   可选 Product Features（可停用 / 可拆出）            │
│     product.wiki · product.team · product.channels · │
│     product.dynamic-kanban · product.work ·          │
│     product.external-agents · product.cron · sites   │
│   目录插件（同一生命周期，信任层级不同）              │
│     plugin.browser · plugin.feishu ·                 │
│     plugin.wiki_learning · …                         │
├─────────────────────────────────────────────────────┤
│ Feature Runtime 层（crew/features/，中立机制）        │
│   FeatureRuntime / FeatureScope / Generation / Lease │
│   Service Registry（global→workspace→user→session）  │
│   Driver / Context / Route / Event / Preset Registry │
├─────────────────────────────────────────────────────┤
│ Kernel（crew/core/ + crew/security/ + crew/state/）  │
│   Envelope / Protocols / 配置校验 / 安全不变量 / 诊断 │
└─────────────────────────────────────────────────────┘
```

- **Kernel 与 Feature Runtime 不允许 import 任何具体 Feature**。`crew/core`、`crew/features` 里出现 `wiki`、`team` 等业务名，就是架构事故。
- **Feature 之间不允许互相 import 具体实现**，只能通过声明的 Service 契约协作（如 Team 经 `ExternalAgentCatalog` 协议用外援，work 经 `KnowledgeService` lease 用 wiki）。
- **Host 不构造 Feature 内部对象**。CLI/Gateway/前端只消费 Service Lease、Feature Descriptor 和能力快照。

### 1.2 一次请求的路径

```text
Host → Envelope(mode=开放字符串)
     → 核心运行时：身份 / workspace / 安全上下文
     → Execution Driver Registry 按 mode 解析 Driver
     → Driver 执行（Agent Loop / Team / Kanban 都只是 Driver）
     → Context Contributor 组装模型上下文（核心只遍历，不认识贡献者）
     → 统一流式出口：核心 chunk + feature_event(feature,event,version)
```

未注册或已停用的 mode 返回标准 `capability_unavailable`，不存在"中心 if/else 分支"。

---

## 2. 核心概念速查

所有概念的真实定义都在 `crew/features/`，公开 API 统一从 `crew/features/__init__.py` re-export。改动这些文件前请三思——它们是全部 Feature 共享的契约，改一个字影响全仓。

| 概念 | 位置 | 一句话职责 |
|---|---|---|
| `FeatureDefinition` | `crew/features/manager.py:180` | Feature 的声明：id、install 入口、依赖、stop/update 策略 |
| `FeatureInstallContext` | `crew/features/manager.py:346` | install 期唯一的注册入口，全部 `register_*` 返回 `RegistrationToken` |
| `FeatureRuntime` | `crew/features/manager.py:484` | 总控：discover / activate / update / deactivate / startup_audit / capability_snapshot |
| `FeatureScope` | `crew/features/runtime.py:402` | 一代 Feature 实例的资源所有者：register / acquire_lease / create_task / stop |
| `FeatureGeneration` | `crew/features/runtime.py:64` | 一次配置对应一代实例，诊断名形如 `product.wiki@g3` |
| `FeatureLease` | `crew/features/runtime.py:273` | 请求级租约（async 上下文管理器），drain 后拒绝新租约、在途租约受保护 |
| `RegistrationToken` | `crew/features/runtime.py:343` | 单次释放、可并发 join 的 dispose 句柄；`RegistrationPhase` 分两相：先 contribution 后 resource |
| `FeatureTransaction` | `crew/features/runtime.py:691` | 激活事务：commit 生效，未 commit 自动 LIFO 回滚 |
| `FeatureState` | `crew/features/runtime.py:25` | discovered → waiting → activating → active → draining → stopping → disposed / failed |
| `ServiceKey[T]` | `crew/features/services.py:33` | 服务钥匙，`name + version`，字符形态 `name@v1`；v1/v2 可共存、互不满足 |
| `ServiceRegistry` | `crew/features/services.py` | 作用域注册表：GLOBAL / WORKSPACE / USER / SESSION 四级，就近优先 |
| `FeatureServiceDependencies` | `crew/features/dependencies.py` | 依赖声明：requires（缺了不能活）/ optional（有了更好）/ provides（我提供的） |
| `FeatureStopPolicy` | `crew/features/runtime.py:38` | 停用策略：drain / cancel / immediate / restart_required |
| `FeatureUpdateStrategy` | `crew/features/manager.py` | 配置更新策略：REPLACE（新代就绪后原子切换）/ RESTART（先停旧代再起新代） |

---

## 3. 架构红线（违反即打回）

这一节是多人协作的防漂移底线。每条都给出**机器保障**或**评审依据**；没有机器保障的红线，靠 code review 执行，评审者有权仅凭本节打回 PR。

### 3.1 依赖方向红线（机器强制）

`.importlinter` 现有 9 条 forbidden contract，CI 跑 `uv run lint-imports`：

| 契约 | 禁止 |
|---|---|
| `core-feature-boundary` | `crew.core` → agent.external / gateway / team / wiki |
| `agent-team-boundary` | `crew.agent` → `crew.team` |
| `agent-gateway-boundary` | `crew.agent` → `crew.gateway` |
| `agent-wiki-boundary` | `crew.agent` → `crew.wiki` |
| `external-agent-team-boundary` | `crew.agent.external` → `crew.team` |
| `executor-external-boundary` | `crew.agent.executor` → `crew.agent.external` |
| `security-feature-boundary` | `crew.security` → agent.external / gateway / team / wiki |
| `state-wiki-boundary` | `crew.state` → wiki / team / agent.external / browser / channels |
| `feature-host-boundary` | `crew.{channels,cron,work,wiki,team,dynamickanban}` → `crew.gateway` |

规则：全部 `allow_indirect_imports=True`（允许经 `crew.features` 间接解析，禁止直接 import）。**新增 Feature 时评估是否需要为它加一条契约**——新边界不进 `.importlinter`，等于没有边界。

### 3.2 装配红线

- **禁止在 `CrewApp.handle()` / `build_app()` 增加 `if feature` 分支或手写 Feature 构造。** 新能力 = 新 FeatureDefinition + Registry 贡献。`build_app()` 只声明加载哪些 Feature。
- **禁止新增第二套生命周期。** 不允许出现绕开 FeatureRuntime 的自建 start/stop 清单、自建插件管理器、自建全局回调表。现有旁路（`hooks.py` / `response_filters.py` / `outbound.py`）是有文档化理由的例外，新增例外必须先写 ADR。
- **禁止游离资源。** asyncio Task、线程、子进程、连接、定时器、文件句柄，必须由 `FeatureScope.create_task()` / `register_disposer` 归属到 Generation。`deactivate()` 返回后不允许存在该代的活跃资源。
- **禁止半激活状态。** install 失败 = 事务整体回滚到激活前；provides 声明了就必须兑现（缺一个 `MissingProvidedServicesError` 整事务回滚）；配置错误 fail-loud，不允许"起来了但功能悄悄缺席"。
- **禁止核心协议入股业务概念。** `Envelope` / `ResponseChunk` / 核心枚举里不出现业务名；业务事件一律 `feature_event(feature, event, version, payload)`。

### 3.3 协作红线

- **消费 Service 必须持 Lease，禁止长期持有实例或缓存跨代引用。** 用 `acquire_lease(key, label=...)`，在请求/命令完整生命周期内持有同代租约，用完即放。draining 后拿不到新 lease 是特性不是 bug——消费方要做 fail-closed 处理。
- **禁止跨 Feature 数据库访问。** 不查别人的表、不建跨 Feature 外键、不 JOIN。跨功能只保存不透明 ID，需要数据走对方的 Service。
- **停用 ≠ 删除数据。** deactivate 只撤销运行时装配与资源；`crew_data/` 下的业务数据、迁移记录一律保留。delete data 是独立显式操作。
- **安全能力只经核心 Provider。** 文件、进程、网络、workspace 权限、审批，Feature 不允许自带实现绕过 `crew/security`。
- **确定性调用走 Service，通知才走事件。** 不允许用字符串事件做跨 Feature 的确定性 RPC（事件泛化成隐蔽总线是明确反对的腐化路径）。事件必须有 schema、version、owner 和持久性分类。
- **拦截器与观察者分家。** 安全审批、否决、改写走拦截器（拿到 next、可短路）；遥测、审计、UI 通知走观察者（不得影响执行结果）。观察者不允许通过副作用改变流程。

### 3.4 跨平台红线（macOS / Linux / Windows 一致）

- 路径一律 `pathlib`，禁止拼分隔符、禁止硬编码 `/tmp`。
- 进程创建、信号、文件锁走项目统一抽象；Windows 无 POSIX signal，取消走标准取消协议。
- 不假设大小写敏感文件系统；临时目录用系统 API。
- 平台差异只允许出现在具体 Provider 内部，Feature Runtime 层保持纯跨平台语义。

---

## 4. "我要做 X，该挂哪里？"——扩展点导航

**这是本规范最有价值的一节。动手前先查这张表；查不到对应机制时，先在群里问，不要自己发明第六套机制。**

| 我要做… | 正确机制 | 入口 / 真实例子 |
|---|---|---|
| 新增一种执行模式（mode） | Execution Driver | `crew/features/drivers.py`；team 注册 `mode=team`（`crew/app.py:2061` 附近） |
| 给模型上下文加内容（附件/引用/提醒） | Context Contributor | `crew/features/context.py`（phase / priority / predicate / model_visible） |
| 新增流式业务事件 | Feature Event | `crew/features/events.py`；如 `feature_event(feature="kanban", event="board_changed", version=1)` |
| 新增 HTTP API | Route Registry | install 内 `register_api_router`；停用经闸门口径返回 `capability_unavailable`，不做 FastAPI 热卸载 |
| 新增模型工具 | Feature install 内注册工具 | 每个工具注册带 lease + disposer；参考 `packages/crew-wiki/src/crew/wiki/feature.py` |
| 新增 Agent 人格（prompt + 工具策略） | Agent Preset | `crew/features/presets.py`；wiki 从 `presets/wiki.md` 解析（ADR-0024） |
| 新增可被其他 Feature 消费的能力 | Service + ServiceKey | `crew/features/services.py`；如 `KNOWLEDGE_SERVICE_KEY`（`packages/crew-wiki/src/crew/wiki/service.py`） |
| 新增配置项 | `features.<name>.*` 命名空间 | 见 §6.1；模板 `config/config.yaml.example:193` |
| 新增持久化表 | 独立 db + 表前缀 + schema 版本 | 见 §6.2；`crew migrate feature <name>` |
| 新增桌面端入口/页面/事件渲染 | capability 门控 + event reducer | `desktop/src/ui/features/board-capability.ts`、`event-reducer-registry.ts`（见 §7.2） |
| 新增 Web 端入口/页面/事件渲染 | ui-feature-registry + feature-event-dispatcher | `web/src/lib/ui-feature-registry.ts`、`featureFlags.ts`（见 §7.3） |
| 新增平台渠道（飞书类） | Channels Feature 内的 Channel 实现 | `packages/crew-channels/`，Channel ABC 显式生命周期契约 |
| 新增第三方/外部扩展 | `plugins/` 目录插件（plugin.yaml） | 见 §5.4；与第一方 Feature 同一 Runtime |
| 新增安全/审批能力 | 核心安全层，不是 Feature | `crew/security/`，先写 ADR |

---

## 5. 开发一个新 Feature 的完整流程

### 5.0 第 0 步：先写 ADR

任何新 Feature、新扩展点、新契约，先在 `docs/backend/adr/` 落一份 ADR（格式见 §11.1），与代码同 PR 提交。没有 ADR 的架构性 PR 不予评审。

### 5.1 第 1 步：决定代码放哪

| 形态 | 适用 | 位置 | 现状例子 |
|---|---|---|---|
| uv workspace 成员包 | 体量较大、边界稳定、未来可能迁出的产品 Feature | `packages/crew-<name>/src/crew/<name>/` | crew-wiki、crew-team、crew-channels、crew-dynamickanban、crew-external-agents、crew-work |
| 根包内 Feature 模块 | 较小或尚未到拆包时机的 Feature | `crew/<name>/` | crew/cron、crew/sites |
| 目录插件 | 外部可安装、按需启用的扩展 | `plugins/<name>/` + `plugin.yaml` | browser、feishu、wiki_learning |

注意：workspace 成员与根包**共享 `crew.*` 命名空间**（PEP 420），import 名不因拆包变化，消费方零改动。跨成员依赖必须显式声明（如 crew-work → crew-wiki）。新增成员包涉及 pyproject、`[tool.uv.sources]`、CI/打包链路，属于架构性改动，先 ADR。

### 5.2 第 2 步：声明 FeatureDefinition

统一形态：模块内提供 `build_<name>_feature(...)` 工厂，文件尾部构造 `FeatureDefinition`。真实范本：`packages/crew-wiki/src/crew/wiki/feature.py`。

```python
MY_FEATURE_ID = "product.my-feature"
MY_SERVICE_KEY = ServiceKey("my-service", version=1)  # key 常量只在 owning Feature 定义一次

def build_my_feature(...) -> MyFeatureBundle:
    ...
    return MyFeatureBundle(
        definition=FeatureDefinition(
            feature_id=MY_FEATURE_ID,
            install=install,
            dependencies=FeatureServiceDependencies(
                MY_FEATURE_ID,
                requires=(SOME_REQUIRED_KEY,),   # 缺失则 waiting，不激活
                optional=(KNOWLEDGE_SERVICE_KEY,),  # 缺失仍可运行，按声明策略降级
                provides=(MY_SERVICE_KEY,),      # 必须兑现，否则整事务回滚
            ),
            stop_policy=FeatureStopPolicy.DRAIN,          # 选择指南见 §5.6
            update_strategy=FeatureUpdateStrategy.RESTART, # 选择指南见 §5.6
            required_by_product=False,
        ),
        ...
    )
```

Feature ID 命名：`product.*`（产品 Feature）/ `core.*`（必需核心能力）/ `plugin.*`（目录插件，由 PluginManager 适配自动生成）。ID 一经发布视为公共契约。

### 5.3 第 3 步：install 体内注册贡献

install 只做一件事：**经 context 注册贡献并登记资源**。推荐顺序（wiki 实际顺序）：

```python
async def install(context: FeatureInstallContext) -> None:
    # 1. 先登记底层资源 disposer（数据库、连接、管理器）
    context.register_disposer(manager.aclose, label="resource:my-manager")
    # 2. 路由
    context.register_api_router(router, prefix="/api/my", label="route:my")
    # 3. Agent Preset / Context Contributor / Event Contributor
    context.register_agent_preset(preset, label="preset:my")
    context.register_context_contributor(contributor, label="context:my")
    # 4. 发布 Service（provides 承诺在此兑现）
    context.register_service(MY_SERVICE_KEY, service, label="service:my-service")
    # 5. 工具（面向模型的入口，消费自己的 Service）
    context.register_tools(tools, label="tool:my_tool")
    # 6. 后台任务必须走 Scope，禁止 asyncio.create_task 裸奔
    scope.create_task(consume_loop(), label="task:my-consume")
```

约束：

- **label 必填且带类型前缀**：`tool:` / `service:` / `route:` / `task:` / `resource:` / `event:` / `driver:` / `preset:` / `context:`。`crew --dump-features` 的诊断树就靠它回答"谁注册了什么"。
- **注册顺序即回滚逆序**：回滚按 LIFO，先 contribution 相后 resource 相。底层资源先注册（后释放），对外贡献后注册（先摘除）。
- install 里**不做跨 Feature import**；需要别人的能力就声明 optional/requires 并在运行期经 lease 解析。

### 5.4 目录插件（plugins/）的额外约定

目录插件与第一方 Feature 共享同一 FeatureRuntime，`plugin.yaml` 由 PluginManager 适配为 `FeatureDefinition(feature_id="plugin.<key>")`。范本：`plugins/browser/plugin.yaml`。字段包括 `name/version/kind/activation_phase/stop_policy/drain_timeout_seconds/update_strategy/requires/optional/provides/provides_tools/ui_hints`。新增目录插件时 stop/update 策略语义与第一方完全一致，不允许在插件管理器侧发明特例。

### 5.5 第 4 步：依赖声明的纪律

- `requires`：缺了就不该活的硬依赖。缺失时 Feature 停在 waiting 并被启动审计点名——这是正确行为，不要用 try/except 掩盖。
- `optional`：有了增强、没了降级。消费 optional service 必须每次经 `acquire_lease` 动态解析（可能随对方启停出现/消失），禁止激活期拿一次就缓存。
- `provides`：install 后必须按**完整 key（含 version）**兑现。版本升级（v1→v2）允许两代共存、互不满足，消费方按需解析。

### 5.6 第 5 步：停用与更新策略选择

| stop_policy | 语义 | 什么时候用 |
|---|---|---|
| `drain` | 拒新租约，等在途请求结束后清理（默认） | Agent、Provider、查询类服务、Team |
| `cancel` | 通知在途任务取消，超时后终止 | 用户可停止的后台运行、长连接渠道、Kanban run |
| `immediate` | 无状态纯注册贡献，立即清理 | 纯 Tool/Prompt 注册 |
| `restart_required` | 资源无法安全热切换，记录待重启 | 进程级/独占资源，由 Host 在重启边界完成 |

| update_strategy | 语义 | 什么时候用 |
|---|---|---|
| `REPLACE` | 新 Generation 就绪后原子切换，旧代 drain | 可双代并存的纯内存服务、Provider |
| `RESTART` | 先停旧代再起新代，失败按旧配置恢复 | 独占端口、长连接、调度器、**工具名寻址不能双代共存**（wiki 因此选 RESTART） |

配置更新永远以 Generation 为单位：不允许"改共享对象字段"式热更新。Runtime 同时维护 desired / effective config revision——更新失败时控制面必须如实显示两者差异，禁止 UI 误报成功。

### 5.7 第 6 步：测试（不是可选项）

每个新 Feature **必须**带一组生命周期契约测试，放 `tests/test_<name>_feature_lifecycle.py`（范本：`tests/test_team_feature_lifecycle.py`、`test_channels_feature_lifecycle.py`）。最低覆盖：

1. 激活成功 → 贡献可见（service 可解析、route 可用、工具在 schema）。
2. install 中途抛错 → 前 N-1 项贡献全部摘除，状态回到激活前，无孤儿资源。
3. provides 未兑现 → 整事务回滚。
4. required 缺失 → waiting，不产生贡献；补齐后正确激活。
5. optional 缺失 → 激活成功且明确降级。
6. 停用 → 旧贡献不可见、任务/连接已清理、**业务数据仍在**。
7. 在途请求持旧代 lease 完成；新请求解析新代（REPLACE）或 fail-closed（RESTART 窗口）。
8. 重复 activate/deactivate 不产生重复 hook/任务/路由。
9. disposer 自身失败 → 其余 disposer 继续执行，错误聚合报告。

开发方式：**先红后绿**——先写 failing 契约测试，再实现。项目原则：验证世界而非自报（断言外部可观察行为，不断言内部标志位）。

---

## 6. 命名空间与数据边界约定

公共命名空间是多人并行开发最容易互踩的资源。以下约定**一经发布即公共契约**，变更走 ADR。完整审计与现状见 `docs/backend/modules/naming-audit.html`。

### 6.1 六类命名空间

| 资源 | 约定 | 示例 |
|---|---|---|
| 配置 key | `features.<name>.*`；核心保留 `core.*`；Feature 只读写自己的子树 | `features.wiki.capture_attachments` |
| 数据目录 | `crew_data/<name>/` 或独立 `<name>.db`；Feature 不得读写他人目录；统一经 `get_owner_runtime_home()` 派生 | `crew_data/wiki_learning.db` |
| 数据库表 | 统一表前缀 `<name>_`；跨 Feature 不建外键、不 JOIN | `wiki_documents`、`team_tasks` |
| Migration | 每 Feature 独立入口 + 独立 schema 版本表 | `crew migrate feature work` |
| Service Key | 稳定命名 + 主版本；常量只在 owning Feature 定义一次 | `ServiceKey("knowledge", version=1)` |
| Feature Event | `<feature>.<event>` + `version` 字段；feature 段与 Feature ID 尾段一致；新事件先登记 | `kanban.board_changed v1` |

### 6.2 配置读写纪律（ADR-0039）

- 运行时写回统一落 `features.*`；旧顶层节（`wiki:` / `team:` 等）只作遗留回落读取，features 侧优先，**禁止新增旧式顶层配置节**。
- 配置写回必须事务化：候选值 → 校验 → 持久化 → 发布。持久化失败不触发运行资源操作，"已保存但连接失败"如实上报。
- DB 路径等基础设施配置走 `runtime.<feature>_db_path`，不进 features.*。

### 6.3 数据边界纪律（ADR-0038）

- 新 Feature 的表直接放独立库 `crew_data/<name>.db`，主库 `crew.db` 只留 core 状态表。
- 从主库迁出用 copy-on-first-activate：ensure-schema → 版本戳 → 单事务整域复制（注意域内 FK 顺序），旧表保留为回退备份。
- schema 版本用 `crew/state/schema_version.py` 助手（白名单防注入、严格递增、幂等基线），并在 `crew migrate feature` 注册表登记。

---

## 7. 消费方协作规则

### 7.1 后端消费（CLI / Gateway / 其他 Feature）

```python
# 正确：label 说明谁在用，完整调用期持租约，用完即放
binding = await registry.acquire_lease(KNOWLEDGE_SERVICE_KEY, label=f"cli:wiki:{command}")
if binding is None:
    # fail-closed：能力不可用要如实报错/降级，不要假装成功
    raise CapabilityUnavailable("wiki 未激活")
service, lease = binding
async with lease:
    result = await service.query(...)
```

真实范本：CLI `crew/cli/knowledge.py`（`_acquire_service`）；Gateway `crew/gateway/routers/misc.py`；Feature 间消费 `packages/crew-work/src/crew/work/feature.py`（optional 消费 knowledge）。

**禁止**：模块级 import Feature 具体实现（用惰性 import + Service 解析代替）；把 lease 持有跨请求；drain 后重试硬抢。

### 7.2 Desktop 端

- **能力门控**：读后端 `/api/config` 的 `feature_capabilities`（`FeatureRuntime.capability_snapshot()` 投影），按 Feature 独立推导可用性做安装/卸载。范本：`desktop/src/ui/features/board-capability.ts`（team / kanban 两板独立启停、事务化 install/dispose、失败隔离）。
- **事件渲染**：`desktop/src/ui/features/event-reducer-registry.ts`，按 `{feature, event, version}` 注册 reducer，返回幂等 disposer；未知事件安全忽略并 warn。
- **新增 UI Feature 路径**：定义幂等 `initXxx/disposeXxx` 对 → 注册 event reducer → 用 capability 订阅驱动装卸 → app.ts 只做总装。**禁止**在中心导航/`activateTab` 加业务分支。

### 7.3 Web 端

- `web/src/lib/featureFlags.ts`：`featureCapabilityAvailable()` 统一推导（config 未加载或字段缺席按可用处理，与后端 legacy-safe 规则一致）。
- `web/src/lib/ui-feature-registry.ts`：页面/导航贡献注册表，register 返回 disposer。
- `web/src/lib/feature-event-dispatcher.ts` + `web/src/features/feature-events.ts`：事件 reducer 体系，与 Desktop 同一事件协议。
- **双端一致性是硬要求**：同一 feature_event 版本在 Desktop 和 Web 行为一致；改事件 schema 必须双端同 PR 或按版本兼容。

### 7.4 事件协议

- 线上只有 `feature_event(feature, event, version, payload)` 一种业务帧形态；旧 kind 枚举已全部退役，**禁止复活旧帧/新增兼容映射**。
- 会影响刷新后 UI 或模型上下文的事件必须有持久投影（Session Event），不能只发瞬时 WS 帧。

---

## 8. 诊断工具（出问题先自查，不要先读中心装配代码）

| 工具 | 用途 |
|---|---|
| `crew --dump-features`（`--json`） | 全量 Feature 状态：state / generation / missing_required / 每条注册的 `phase:label [state]`；healthy 退 0，blocked 退 1 |
| 启动审计 | 启动收敛后自动点名 waiting / failed 的 Feature 及缺失 ServiceKey（fail-loud） |
| `FeatureRuntime.capability_snapshot()` | 运行期能力快照，经 `/api/config` 的 `feature_capabilities` 暴露给前端 |
| `crew migrate feature <name>` | 核对/推进 Feature 级 schema 版本 |
| `crew migrate orphans` | 只读诊断主库孤儿表（无属主代码的残留表） |

"我的 Feature 为什么没起来"的标准排查顺序：`--dump-features` 看状态 → waiting 看 missing_required → failed 看聚合错误 → 还没有再看日志。

---

## 9. 测试布局

- `tests/test_*.py` 扁平为主；领域子目录：`tests/wiki/`、`tests/gateway/`、`tests/browser/`、`tests/security/`、`tests/skills/`、`tests/e2e/`。
- Runtime 原语测试：`test_feature_runtime.py` / `test_feature_manager.py` / `test_feature_services.py` 等——改 `crew/features/` 必跑。
- Feature 生命周期契约：`test_<name>_feature_lifecycle.py`。
- 配置命名空间：`tests/test_config_features_namespace.py`；拆库：`tests/test_feature_db_split.py`。
- e2e marker 默认跳过；`tests/security` 需 Rust 运行时，由专门 CI 覆盖。

---

## 10. 提交前验证链

### 10.1 后端

```bash
uv sync --extra dev --extra wiki        # 安装依赖（含 workspace 成员）
uv run pytest -q --ignore=tests/security   # 全量（e2e 默认跳过）
uv run pytest -q -m e2e                 # 需要时单独跑 e2e
uv run ruff check .                     # lint（E4/E7/E9/F，line-length 100）
uv run lint-imports                     # 9 条架构契约，9 kept 0 broken
```

### 10.2 Desktop（改动 desktop/ 时）

```bash
cd desktop
npm test          # vitest 全量
npm run check     # 完整门禁：tsc --noEmit + eslint + vitest + stylelint + 静态安全/审计脚本
```

### 10.3 Web（改动 web/ 时）

```bash
cd web
npm run build     # 内含 typecheck（tsc -p 双 tsconfig）
npm test          # vitest 全量
```

### 10.4 提交边界纪律

每个 PR 应具备（与演进方案 §16 一致）：

- **单一架构目的**：不把业务整理和机制改动混在一起；大规模 rename / 拆包单独成 PR。
- **兼容路径或明确的删除决策**：破坏性行为变更要在 PR 描述写明。
- **对应测试**：先红后绿；新 Feature 带生命周期契约测试（§5.7）。
- **ADR 与文档**：架构性改动同 PR 落 ADR；模块行为变化更新对应 `docs/backend/modules/*.html`。
- **跨平台说明**：涉及进程/路径/信号的改动，PR 描述写明三平台行为；CI python 门禁是 ubuntu/windows/macos 三平台矩阵。
- **回退方式**：配置类、迁移类改动必须说明如何回退。

---

## 11. 决策与文档机制

### 11.1 ADR（架构决策记录）

- **什么时候写**：新 Feature / 新扩展点 / 新契约 / 跨 Feature 依赖变化 / 数据边界变化 / 命名空间新增。一句话：**会影响其他开发者怎么写代码的决定，都要 ADR**。
- **位置与命名**：`docs/backend/adr/NNNN-kebab-case-title.html`，NNNN 取当前最大编号 +1（现存 0001–0044）。
- **固定结构**：背景 → 候选方案（含被否决的及原因）→ 决定 → 跨平台影响 → 回退方式。候选方案一节必填，不许只写"决定是什么"。
- ADR 写**当前状态**，不写变更故事；历史叙事留在 commit / PR / 测试记录里。

### 11.2 模块文档

- 模块文档在 `docs/backend/modules/*.html`（HTML 格式，模板 `_template.html`），索引 `docs/backend/modules/index.html`，总入口 `docs/index.html`。
- 单篇大纲：职责 / 功能拆解 / 关键文件 / 数据模型与接口 / 当前存在的问题 / 扩展点 / 契约一致性测试 / 变更记录。
- 改了某模块的行为，同 PR 更新它的模块文档；小更新不需要新增文档，但既有文档描述过时的必须同步修正。

### 11.3 验证记录

- 重要批次在 `docs/testing/` 落 HTML 验证记录（测试命令 + 结果 + 环境）。
- 全量测试的 flake 要如实标注"原因未确认"并单跑复验，禁止含糊带过。

---

## 12. 架构漂移预警信号（评审自查清单）

在 PR 里看到以下任何一条，请停下来问"这是不是该走扩展点/契约"：

- [ ] `crew/app.py`、`crew/features/`、`crew/core/` 的 diff 里出现了业务 Feature 的名字
- [ ] 新增了模块级 `from crew.<其他feature> import ...`
- [ ] `asyncio.create_task` / 线程 / 子进程没有挂在 Scope 上
- [ ] 拿到 Service 实例后存成长期字段，或跨请求复用 lease
- [ ] 新的顶层配置节、新表落进 `crew.db` 主库、跨 Feature 的 SQL JOIN
- [ ] `except Exception: pass` 或静默 fallback（空 catch 必须注释吞掉了什么、为什么安全）
- [ ] 新的全局单例 / 全局回调注册表 / 第二套"插件管理器"
- [ ] 停用某 Feature 后：工具还在 schema、路由还可达、任务还在跑、UI 入口还在——任一成立即事故
- [ ] 前端中心文件（导航 / activateTab / 全局 reducer / backend-client）出现新业务分支
- [ ] "先这样，后面再补测试/ADR"——不允许，先红后绿、ADR 同 PR

---

## 13. 参考文档索引

| 文档 | 用途 |
|---|---|
| `docs/todo/ace-plugin-architecture-evolution-plan.md` | 上位设计方案与全量设计论证 |
| `docs/backend/adr/` | 44+ 份架构决策记录，按编号读 |
| `docs/backend/modules/naming-audit.html` | 命名空间六类约定与审计矩阵 |
| `docs/backend/modules/*.html` | 各模块领域文档 |
| `docs/index.html` | 文档总入口与推荐阅读顺序 |
| `AGENTS.md`（根） | 项目行为准则与开发规则 |

---

*本规范本身也是契约：发现规范与代码现状脱节时，改代码或改规范必须同 PR 说明，并在修订记录留痕。*
