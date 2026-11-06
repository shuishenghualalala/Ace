# 三项目预审演示（新编虚构样本）

此目录是根据论证稿中的项目类型重新编写，不是原稿未附的旧材料。
导入 materials 下 13 份文件。框架不含赛事权重，不输出名次。

团队建议：内置 Crew Leader（资料核对/综合），技术与 AI 证据审阅员，商业与反证审阅员。
若机器上配置了真实 Kimi/Hermes，可指定对应成员；否则须明确展示实际模型，不能把角色名冒充执行器。

验收：运行 `uv run python scripts/run_project_review.py --output /absolute/path`。
读取 REVIEW_API_KEY / REVIEW_BASE_URL / REVIEW_MODEL；默认使用已配置的 KIMI_API_KEY / KIMI_BASE_URL，
采用 Anthropic 兼容协议，默认模型 kimi-for-coding。凭据只从环境读取，绝不导出。
脚本通过 Crew 的实际 Wiki 文件存储读取来源，真实模型分别执行两个独立专业角色并由 Leader 综合；
运行记录区分这种“同模型多角色真实调用”与完整桌面外援集成验收。

自动检查只核对结构、来源名称/小节/逐字引文、维度覆盖及虚构声明。
不能证明所有推理正确；必须人工审阅。不存在来源就拒绝输出成功，不生成示范答案填空。

## 桌面全链路（推荐作为功能验收）

1. `uv sync --extra dev` 安装后端。以 `config/config.yaml.example` 为基础配置真实模型；
   凭据使用环境变量。把本目录 `team-config.yaml` 的小队设置合并到本机配置。
2. 在 `desktop` 目录执行 `pnpm install`，安装 Electron 后执行 `pnpm dev`。
   启动器会移除 IDE 可能继承的 `ELECTRON_RUN_AS_NODE`，不会把 Electron 当 Node CLI 启动。
3. 在仓库根执行：
   ```
   uv run python scripts/run_project_review_gateway.py --output /absolute/path/to/acceptance
   ```
   默认连接本机开发网关 `http://127.0.0.1:28180`；其他端口使用 `--base-url`。
   需要鉴权时从 `REVIEW_GATEWAY_TOKEN` 环境变量读取，不把 token 写入文件。
4. 脚本通过正式 HTTP API 创建独立知识库、导入 13 份材料、绑定内置小队，然后通过 WebSocket
   启用评审契约发送任务。它不是伪造 UI，也不直写产品数据库。运行会消耗真实模型额度。
5. 桌面刷新会话列表，打开“三项目预审｜真实 Kimi 协作”，打开看板 → 协作。
   核对四节点、并行审阅、每个真实 attempt、最终意见和待人工复核提示。

这是通过公开网关接口触发的专用验收场景；当前聊天输入框没有专用的“三项目证据契约”开关。
普通 UI 团队消息仍走同一协作内核，但不自动施加固定四节点/三项目 JSON 契约。
不要把一般聊天中的“看起来格式正确”当作已经通过这个专用校验。

脚本保存 sources.json、events.jsonl、executions.json、run.json 和 final.md。
只有全部节点 completed 才返回成功；模型失败、等待用户或校验失败都保留原始证据并非零退出。
即使成功，也需要人工核对引文含义、证据强度与遗漏项。

首次运行的知识库首页摘要编译可能慢于 Source 入库。Source 已解析不等于首页摘要编译成功；
本轮依据已解析的原始 Source 快照进行评审，不以摘要编译代替原始材料。

普通聊天不自动启用这个固定三项目契约；继续提出澄清问题时可走普通团队消息。
恢复 API 和边界见 `docs/collaboration-runtime.md`。

## 完成后离线审计

```bash
uv run python scripts/audit_review_execution.py /absolute/path/to/executions.json
```

也接受恢复后保存的单个 execution.json。脚本不调用模型、不生成评审答案；
检查原成员身份、尝试/成果一致性、真实时间区间、依赖工作包、来源哈希、逐字引用及最终结构。
只有退出码 0 且 `passed=true` 才通过这些机器可检查项；专业结论仍需人复核。
`acceptance=pending` 是正常的“等待人工验收”，不是任务执行失败。

长推理模型的输出预算需要覆盖推理与正文。本轮 Kimi 本地验收配置使用
`max_tokens: 24000`、`timeout: 360`；这不是所有模型的通用最优值。
预算过小可能出现空回复/截断。运行时会保留续写前缀，但仍不把校验失败的残缺报告算作成功；
检查错误后显式重试失败节点，已完成审阅不必重跑。
