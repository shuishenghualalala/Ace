# Ace uv workspace 多包化调研报告（阶段 6 立项依据）

> 调研日期：2026-09-11 · 基线分支：`refactor/plugin-architecture-stage-0` · 性质：只读调研，未改任何代码
> 依据：演进计划 §10 阶段 6（uv workspace 多包评估）、§18（工作量与契约冻结）；对照 deepseek-harness 的 pnpm/uv 布局
> 结论速览：**多包化可行，但有 6 项逻辑拆分必须先完成**；建议保持发行版间 import 名全为 `crew.*`；首个试点包选 crew-wiki。

## 1. 当前 Python 包结构（事实）

- 单一发行版 `crew`，hatchling 构建（pyproject.toml:111-116）；CLI 入口 `crew = "crew.cli.main:main"`（:108-109）；核心依赖 15 个（:12-28）；optional extras 7 组（dev/mcp/feishu/weixin/wiki/pdf/docx/xlsx，:30-101），wiki/pdf/docx/xlsx 是 Feature 级依赖、feishu/weixin 是渠道级——天然对应未来"成员包自带 extras"。
- 开发流 `uv venv + uv pip install -e ".[EXTRAS]"`（scripts/install.sh:84-92）；CI 用 `setup-uv` + `uv sync --extra dev --extra wiki` + `uv run lint-imports/pytest`（.github/workflows/ci.yml:27-35）；uv.lock 已存在。
- crew/ 共 27 个子模块约 17.4 万行。角色归类：内核/协议（core/state/security/features/providers）、核心运行时（agent）、宿主/组合根（app.py 3907 行、gateway、cli）、Product Feature（wiki/team/dynamickanban/work/channels/cron/sites/tasks/notifications/evolution/scenarios/memory/browser）、兼容层（plugins/mcp_servers）、数据（skills）。
- .importlinter 7 条契约全部为 forbidden 型且 `allow_indirect_imports = True`——只禁直接 import，间接链不受约束；uv workspace 的价值正是把"禁止"升级为包管理器物理不可达。
- 跨模块 import 热点（全量 grep 实证）：agent→core 75、tools→security 33、gateway→state 32、agent→security 29、team→core 24、gateway→channels 18、gateway→team 15、evolution→agent 15 等。

## 2. 多包化就绪度

### 2.0 审计 P1/P2 状态核对
- P1-1 单一 crew.db：未修（config.py:208 默认 db_path；同一 db 注入全部 Store）。
- P1-2 team→external：未修（修复中）。除审计所列 FK（external_store.py:33,44-45）、JOIN（:252,293）、DELETE（:300,304,308）外，**新发现：`TeamExternalAgentStore(ExternalAgentStore)` 直接继承 external 包的类（:15,20）——Python 级继承耦合，比 FK 更根本**。
- P1-3 state→wiki：已修（6E，第 7 条契约）。
- P2-1/2/3/4/7：P2-4 底座已由 6H 落地（work 试点）；其余未修。

### 2.1 逐包就绪度（跨包导入全部 grep 实证）

**crew-core（core+state+security+features+providers+tools+memory+tasks+plugins+mcp_servers+browser+skills 数据，可含 agent）**
- 包外反向依赖（阻塞项）：① `crew/state/team_member_model.py:9-11` 导入 `crew.agent.external.runtime_profile` 与 `crew.team.agent_profile/roles`——core 状态模块依赖两个 feature 包，**必须先修**；② `crew/state/config.py:18` 导入 `crew.browser.types.BrowserConfig`（P2-7 前半），**必须先修**。
- 包内纠缠（同包不阻塞，建议顺带）：core→tools（core/types.py:163,170 redact）、core→state（core/followup.py:24）、security→tools（security/audit.py:18、launch.py:139）与 tools→security 互相缠绕——建议 redact 下沉 core。

**crew-agent（可先并入 crew-core）**
- 依赖方向全部向下健康；计划所述 runtime 反向依赖 wiki/gateway/team 已清零（M2 里程碑事实）。
- `crew/agent/executor/external.py` 7 处直连 `crew.agent.external`——external 独立拆包前需改 Service Lease（4A-2 的 external_services_acquirer 已铺路）。

**crew-external-agents（agent/external，15 文件）**
- 自身就绪（依赖全向下）；前置 = P1-2 全链（含继承耦合）+ executor 直连改 Lease。

**crew-wiki（14.2k 行）**
- 依赖全向下；不占 crew.db（纯文件系统 + 每 KB SQLite）。
- 唯一跨包问题：`crew/wiki/tools.py:867` 导入 Gateway 私有函数 `crew.gateway.context._get_upload_dir`——**必须先修**（上传目录解析移入 core/state 或经 Service 注入）。
- wiki→agent（feature.py:199 subagent preset 解析）：agent 并入 crew-core 则同包无碍。

**crew-team（21.7k 行）**
- 前置 P1-2；另有 team→dynamickanban 类型级导入（graph_planner.py:15-16、team_manager.py:1068-1069 PlanGraph/PlanNode/PlanResult）——可选方案：plan 图类型抽共享契约，或 kanban 暂不拆包。

**crew-kanban（dynamickanban）**
- `crew/dynamickanban/feature.py:199` 导入 `crew.gateway.routers.dynamic_kanban.create_dynamic_kanban_router`——feature 包向上导入宿主 router 工厂，是 feature→gateway 的唯一成环方向，**拆 gateway 前必须理顺**（Route Registry 下应由 gateway 侧发现装配）。
- P2-6 事件命名空间未决。

**crew-channels（channels）**
- channels→gateway 5 条（delivery.py:11-12、channel_sessions.py:10、channel_manager.py:11、broadcast.py:24），均为会话上下文/出口格式化类协议设施——**把 gateway/{session_context,response_filters,outbound,hooks} 下沉 core（或 crew-protocol）后即就绪**。同型：cron（feature.py:23）、work（service.py:199）。
- P2-7：channel_bindings/channel_session_routes 表在 core state 手里，归属错位。

**crew-work**：依赖健康，wiki 为可选依赖（extras 表达）；work→gateway hooks 同型待下沉。

**其余**：cron/sites（依赖 core/state/tools）、browser（无 feature.py，建议先并入 crew-core）、tasks/notifications/memory/scenarios 小；evolution 大量 import crew.agent.skills（skill_optimizer.py:109 等，skills 归属需决策）；cli+app.py+gateway 是最上层宿主。

### 2.2 结论
**workspace 化首批（crew-core / crew-wiki / crew-external-agents）之前必须修：**
1. state→team/agent.external（team_member_model.py:9-11）
2. P1-2 全链（FK/JOIN/DELETE + **Store 继承**）
3. wiki→gateway 私有导入（wiki/tools.py:867）
4. state→browser 类型（config.py:18）
5. gateway 协议设施下沉（session_context/response_filters/outbound/hooks），解除 channels/cron/work/wiki→gateway
6. executor→external 直连改 Service Lease

**可带进 workspace 后续修**：P1-1 拆库（迁出仓库前完成即可）、P2-1 配置节、P2-2/3 ServiceKey（建议契约冻结前顺手做掉）、P2-5 表前缀、P2-6 事件命名空间、team→kanban 类型（kanban 不拆则无影响）、evolution/skills 归属。

## 3. dsh 参照（结构与约束方式提炼）

- **pnpm workspace**：`packages/*/*` 两级按域分组（50+ 包）+ `apps/*` 产品装配层 + `vendor/*`；包间依赖 `workspace:^`；宿主/插件边界用 **peerDependencies + 同名 devDependencies 双列**（"包声明需要谁、由最上层宿主供给"——包级依赖倒置的物理表达）；`allowBuilds` 白名单管 postinstall；根 tsdown 统一构建、包内只写 exports map；根 tsconfig.base 用 paths 源码级解析 + composite project references（每包独立 tsconfig 边界）。
- **Python 侧**：dsh 没用 uv workspace——仅两个独立 uv 项目（python/sdk + python/sdk-runtime），用 `[tool.uv.sources]` path+editable 关联。Ace 是 6-8 个互相依赖的库包，共享单一 uv.lock 的原生 workspace（根 `[tool.uv.workspace]` members）更合适。
- **前端多宿主消费同一协议包**：dsh 的 apps/web 几乎全部依赖 workspace UI/协议包、apps/desktop 是薄壳——验证了 Ace "Desktop 与 Web 消费同一份 Feature 协议包"的形态。

## 4. 前端 pnpm workspace 现状

- desktop：Electron + esbuild（main bundle cjs/node18）+ vitest + Playwright + electron-builder，vanilla TS 无 React；web：Vite 6 + React 18 + vitest。两套 npm 项目独立 lockfile，**零共享代码、无共享 tsconfig base**。
- 双端重复实现盘点：Feature Event Registry（desktop event-reducer-registry.ts:122-162 vs web feature-event-dispatcher.ts:75-116，逻辑同构但 Effect 类型不同：desktop 是 upsert 列表、web 是函数式）、compat 映射（web compatFeatureEvent + desktop 6D 的 toLegacyEventFrame + 后端 event_compat.py——**三份同表**，协议包最高价值收敛点）、导航/页面 Registry（理念一致接口不同）、Chunk/UiMessage 类型不同源。
- 落点建议：`packages/feature-protocol`（纯 TS 零 DOM/React/Electron 依赖：event-key 规则、parseFeatureEventBody、compat 映射单表、payload DTO、泛型 Registry 基类）→ 第二步 `packages/ui-feature-contract`（导航/页面贡献接口）→ Feature UI 包按 wiki→外援→team 跟进。
- 风险：npm→pnpm 两套 lockfile 合并、electron/esbuild postinstall 白名单、两端 tsconfig 差异（exactOptionalPropertyTypes 等）需 base 归一。

## 5. 迁移风险与分步顺序

**受影响面（事实引用）**：scripts/install.sh:84-92（改 workspace-aware uv sync）；ci.yml:27-35（lint-imports 的 root_package=crew 在保持 `crew.*` 命名空间下不受影响）；Dockerfile.pack:48,68-82（COPY 各成员 + PyInstaller datas/pathex 同步，保持 `crew.*` import 名则改动最小）；desktop spawn `PYTHONPATH=仓库根`（desktop/src/main/index.ts:1382-1386，包移出 crew/ 目录会断——维持命名空间 + editable 安装即不断）；tests/ conftest.py:54 直掏 crew.app._LIVE_APPS（改公开入口）；根 ruff/pytest 配置归属。

**建议顺序**：
1. 逻辑收尾：§2.2 六项必修 + 契约冻结（ServiceKey 版本、feature event、session-context DTO 三组）。
2. 宿主设施下沉（gateway 协议设施 → core）。
3. 建 uv workspace，**首个试点 crew-wiki**（计划指定的首个 Feature 试点、不占 crew.db、依赖最干净），成员目录 `packages/crew-wiki/src/crew/wiki`，import 名保持 `crew.*`；验证 uv sync 单锁 / CI / Dockerfile.pack / desktop spawn 四条链。
4. 第二批 crew-external-agents（P1-2 修完后）。
5. 第三批粗粒度收尾：crew-core、crew-team、crew-kanban、crew-channels、crew-work、小包就近并入；gateway+app.py+cli 组成最上层宿主包——依赖方向由包管理器物理强制，import-linter 契约退化为物理事实。
6. 前端：pnpm workspace → feature-protocol（先 compat 表）→ ui-feature-contract → Feature UI 包。
7. 迁出仓库前提：P1-1 拆库 + per-feature migrate（6H 底座已就位）。

## 6. 包划分建议总表

| 包 | 内容 | 前置修复 |
|---|---|---|
| crew-core | core, state, security, features, providers, tools(redact 下沉), browser, tasks, memory, plugins, mcp_servers, agent（可后续再分）, scenarios | state→team/external、state→browser、core↔tools/security 微调 |
| crew-wiki | wiki | wiki→gateway 私有导入 |
| crew-external-agents | agent/external | P1-2 全链（含继承）；executor 直连改 Lease |
| crew-team | team | P1-2；team→kanban 类型处置 |
| crew-kanban | dynamickanban | feature→gateway router 工厂方向 |
| crew-channels | channels | gateway 协议设施下沉 |
| crew-work | work（wiki 可选依赖） | work→gateway hooks 下沉 |
| cron/sites/evolution/notifications | 就近并入或随批拆 | evolution→agent.skills 归属决策 |
| crew-host | gateway, app.py, cli, 装配 | 最后迁；协议设施先下沉 |
