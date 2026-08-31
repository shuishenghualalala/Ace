# Crew Desktop

Crew 的 Electron 桌面客户端。它连接本机 Crew Gateway，提供对话、任务、技能、Wiki、浏览器控制、外援与系统管理界面。

> 当前为源码预览版。仓库不提供预编译安装包、代码签名、公证或自动更新服务。

## 快速开始

在仓库根目录执行：

```bash
uv venv .venv --python 3.11
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
uv pip install -e ".[dev,wiki]"

cd desktop
npm install
npm start
```

`npm start` 会构建 Desktop 并自动启动托管 Gateway，无需另开后端进程。它读取仓库根目录的
`config/config.yaml`；默认 `auth.mode: email`，首次打开会显示邮箱登录页。邮箱不需要验证码，
仅用于生成相互隔离的本机租户 Owner。

如果只进行日常开发、不测试邮箱登录流程，使用：

```bash
cd desktop
npm run dev
```

`npm run dev` 会传入 `--dev`，使用隔离的 `dev:dev` Owner 和开发数据目录，不显示邮箱登录页。
普通模式和开发模式都使用托管端口 `28180`；连接成功后，渲染层会记录实际地址。

首次打开后，在“设置 → 模型”中添加模型并将其设为默认。API Key 写入当前用户的本地环境文件，不会返回给渲染层。

## 主要能力

- **对话与会话**：流式回复、thinking、工具调用、停止与追加指令、附件、工作空间、会话归档和 Team 协作。
- **模型**：设置页负责模型 Profile 的新增、编辑、删除和默认模型切换；对话区可为当前会话单独选择模型。
- **任务**：定时任务的创建、筛选、启停和立即执行。
- **技能**：浏览内置、插件和用户 Skill，管理本地安装状态及可选的 Skill 自进化开关。
- **Wiki**：创建本地知识库、导入文件并通过独立 Wiki 会话问答。
- **浏览器与 Computer Use**：在工作区内查看和控制浏览器；所需 `cua-driver` 通过“设置 → MCP 服务”按需安装。
- **外援**：接入外部 Runtime、Agent 和 Team，并从对话输入区发起外援协作。
- **渠道与 MCP**：在设置页配置渠道账号、连接状态和 MCP Server。
- **系统信息**：Gateway 状态、运行日志、Token 用量和审计视图。

## 模型切换规则

- 设置页的“设为默认”会更新当前用户的默认模型，新会话在没有显式绑定时继承该模型。
- 对话区模型选择器只修改当前会话；已有其他会话不受影响。
- 任务运行期间切换会话模型时，新模型从下一条消息开始生效。
- 外部 Agent 是否支持切换模型，由对应 Runtime 的能力声明决定；Team 会话的成员模型由 Team 配置决定。

## Gateway 与身份

- `npm run dev` 使用隔离的托管 Gateway 和 `dev:dev` 开发 owner。
- `npm start` 使用普通托管 Gateway，并按 `config/config.yaml` 的 `auth.mode` 进入邮箱、免登录或远程验证码模式。
- 独立启动 `python -m crew.gateway.server` 时，Gateway 默认监听 `127.0.0.1:8000`。
- Ace 正式配置默认使用 `email` 身份：用户填写邮箱后生成隔离 owner，不校验邮箱所有权；`local` 仍可配置为本机免登录模式。启用 `auth.mode: remote` 后，Desktop 使用手机号验证码，并在主进程安全存储 Gateway 会话 Cookie。
- Browser 控制接口除登录会话外，还使用安装实例密钥派生的 Bearer Token。

## 代码结构

| 路径 | 职责 |
|------|------|
| `src/main/` | Electron 主进程、窗口生命周期、Gateway 管理、认证与 Browser Host |
| `src/shared/` | 主进程、预加载脚本、渲染层和测试共用的类型、常量与 IPC 校验 |
| `src/ui/` | 渲染层入口、Gateway 客户端、状态管理与页面功能 |
| `assets/index.html` | Desktop 页面结构 |
| `assets/styles/` | 主题、布局和组件样式 |
| `tests/unit/` | Vitest 单元与回归测试 |
| `tests/visual/` | Playwright 冷启动与视觉测试 |
| `scripts/check-security.mjs` | Electron/IPC/XSS 等静态安全门禁 |
| `electron-builder.yml` | Linux、Windows 和 macOS 的 Electron `dir` 构建配置 |

### 外援中心迁移边界

`features/agent-hub.ts` 负责 Agent Hub 外壳与目录卡片，`features/agents-page.ts` 负责外援目录、创建流程和派活业务。当前创建 Agent/Team 表单仍由 `agents-page.ts` 提供，属于迁移中的兼容边界；后续收口时应先移除 Hub 对旧表单包装层，再迁移表单渲染与状态，不应直接删除现有创建流程。会话外援身份以 Gateway 返回的 `agent_binding` 为准，旧 Gateway 的 Provider fallback 和旧 `agent-config` 字段兼容逻辑必须集中在适配层，不能继续散落在页面组件中。

渲染层通过 `window.Crew` 的最小预加载桥访问主进程能力。`gateway:fetch` 仅允许本机 Gateway 的 `/api/` 路径；外部链接、文件选择和上传均在主进程执行白名单及大小校验。

## 开发与验证

```bash
npm run dev          # 开发启动
npm run build        # 构建主进程与渲染层
npm run check        # typecheck + lint + unit tests + 安全/CSS 门禁

# 首次运行浏览器级视觉测试前安装 Chromium
npx playwright install chromium
npm run test:visual
```

提交 Desktop 改动前至少运行 `npm run check`。涉及布局、引导或 Browser 面板时，再运行对应的 Playwright 视觉测试。

## 本地预打包

以下命令只生成 Electron 裸目录，不生成面向最终用户的正式安装器：

```bash
npm run dist:linux
npm run dist:win
npm run dist:mac
```

应在与目标一致的操作系统上执行。产物位于 `desktop/release/`；正式分发前仍需配置代码签名、公证、安装权限与更新来源。

包含 Gateway 和运行时的完整安装器构建方式，见根目录 [README](../README.md#构建桌面发行包可选)。
