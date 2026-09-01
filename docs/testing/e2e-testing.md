# Ace 后端 E2E 批量测试指南

本文档说明如何运行 Ace 的后端端到端测试，以及同事新增功能时如何补充测试场景。

## 1. 这套测试在测什么

Ace 的 E2E 不走 `agent.run()` 或单个工具，而是走真正的产品后端入口
`CrewApp.handle(Envelope)`。因此每次测试会覆盖：

- 提示词拼装、Skill 注入、会话配置读取
- 工作区解析与 workspace instructions 注入
- agent / team / dynamic kanban 路由
- 工具执行、文件写入、Wiki 入库
- 外援 Runtime 探测与执行
- 最终回复与错误帧

所有测试都在临时目录里运行，不会碰真实 `crew_data`、`.crew` 或工作区。

## 2. 环境要求

- Python 3.11+，推荐仓库 `.venv`
- 已安装 `pyyaml`
- 主模型需要可用的 API Key：
  - 读取 `config/.env` 中的 `CREW_MODEL_API_KEY`
  - 或配置里指定的其他 `api_key_env`
- 外援场景需要本机已安装对应 CLI：
  - Codex：`codex`
  - Claude Code：`claude`
  - Kimi：`kimi`
  - 未安装的运行时会被标记为 `skipped`，不会算失败

### 2.1 模型与密钥的实际解析链路

E2E 不需要（也不应该）手动 export 任何 `CREW_*` 变量，解析链路是：

1. `_run_case.py` 把 `CREW_HOME` 指向 case 临时目录（其中没有 `config.yaml`），
   因此 `load_config()` 走内置默认 profile（id=`default`，`api_key_env=CREW_API_KEY`）。
2. `load_config()` 自动按候选路径加载 `.env`（`override=True`），其中包含
   **仓库根的 `config/.env`**——本机的 `CREW_BASE_URL` / `CREW_MODEL` /
   `CREW_MODEL_API_KEY` 就是在这里生效的（例如 ark 端点）。
3. 默认 profile 命中「旧式单模型」分支：`CREW_BASE_URL` / `CREW_MODEL` 覆盖
   profile 的 base_url / model。
4. `_run_case.py` 的兼容补丁：profile 仍无 key 时，用 `CREW_MODEL_API_KEY`
   补齐 `profile.api_key`。

两个容易踩的坑：

- **不要 `source` 其他项目的 `.env`**（例如 Crew 仓库的 `config/.env`）再跑 E2E：
  手动 export 的 `CREW_API_KEY` 会被保留，而 `CREW_BASE_URL` 又被本仓库
  `config/.env` 覆盖，key 和端点错配会直接 401。
- `~/.crew/config.yaml` 里的占位 profile（`api.example.com`）不参与 E2E，
  因为 `CREW_HOME` 已被指到 case 目录；日常手动运行才走那份配置。

## 3. 怎么跑

先激活 Python 环境：

```bash
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
```

跑全部场景：

```bash
python scripts/run_e2e_batch.py
```

按模块跑：

```bash
python scripts/run_e2e_batch.py --category complex_tasks
python scripts/run_e2e_batch.py --category wiki
python scripts/run_e2e_batch.py --category external
```

只跑单个 case：

```bash
python scripts/run_e2e_batch.py --case write_file
python scripts/run_e2e_batch.py --case external_codex,external_claude
```

指定报告目录：

```bash
python scripts/run_e2e_batch.py --report-dir build/e2e/2026-08-13
```

切换模型 profile：

```bash
python scripts/run_e2e_batch.py --profile deepseek
```

跳过也算失败时：

```bash
python scripts/run_e2e_batch.py --fail-on-skip
```

默认报告输出到：

```text
build/e2e/<YYYYMMDD-HHMMSS>/
  index.html
  summary.json
  complex_tasks/write_file/
    case.json
    transcript.jsonl
    llm.jsonl
    crew.log
    workspace-before.json
    workspace-after.json
    result.json
```

## 4. 怎么看一个 case 的日志

失败后先看 `result.json` 里的 `error`，再按下面的顺序排查：

1. `transcript.jsonl`：每一行是一次 `ResponseChunk`，能看出工具调用顺序、最终回复和错误帧。
2. `llm.jsonl`：完整记录发给模型的 prompt 和模型返回，确认模型有没有理解任务。
3. `crew.log`：应用日志，包含 prompt builder、tool search、executor、provider 的性能和异常。
4. `workspace-before.json` / `workspace-after.json`：对比工作区最终状态，确认文件是否落地。

### 4.1 llm.jsonl 到底写在哪

`llm.jsonl` 由 `crew/state/logging.py` 的 `_setup_llm_trace(log_file)` 写入，
**固定放在主日志（`log_file`）的同目录**，不是 `crew_home/logs/`：

- E2E：`cfg.log_file = <case目录>/crew.log` → trace 在 `<case目录>/llm.jsonl`
  （每个 case 隔离，报告目录里看到的就是它）。
- 日常运行：`log_file` 默认 `logs/crew.log` → trace 在 `logs/llm.jsonl`；
  只有 `log_file` 为空时才 fallback 到相对进程 cwd 的 `.crew/logs/llm.jsonl`。

排查压缩/上下文问题时注意：**压缩只作用于发给 LLM 的视图，不写入持久化
会话历史**，所以 `【历史摘要】` 和 `【压缩后保留的工具结果】` 这类标记只能在
`llm.jsonl` 的请求消息里看到，在 session_store 里是找不到的。

## 5. 新增一个测试场景

新增场景只需要编辑：

```text
tests/e2e/scenarios.yaml
```

在对应模块下加一个 case：

```yaml
complex_tasks:
  my_new_case:
    title: 新功能的描述
    mode: agent
    workspace:
      instructions: 这是 E2E 玩具工作区。
      files:
        "toy_project/input.txt": "hello\n"
    prompt: |
      请读取 toy_project/input.txt，然后写入 toy_project/output.txt。
    expected_tools: [file_read, file_write]
    expected_files:
      - path: "toy_project/output.txt"
        contains: ["hello"]
    expected_final_keywords: ["output.txt"]
    timeout_seconds: 240
```

常用字段：

| 字段 | 说明 |
|------|------|
| `mode` | `agent`、`team`、`dynamic_kanban`、`external` |
| `workspace.instructions` | 注入 workspace 的指令 |
| `workspace.files` | 工作区初始文件，key 是相对路径 |
| `config` | 测试运行前的 Config 覆盖，例如 `team_config.required_workflow` |
| `session_config` | 会话级 agent config，例如 Wiki 预设或外援绑定 |
| `attachments` | 模拟本轮上传附件 |
| `expected_tools` | 期望至少调用其中某个工具 |
| `expected_files` | 期望最终存在的文件及内容关键词 |
| `expected_files[].search` | 为 `true` 时在工作区子目录递归搜索同名文件（Dynamic Kanban 产物在 `workflows/<id>/` 下） |
| `expected_final_keywords` | 最终回复必须包含的关键词 |
| `expect_wiki_sources` / `expect_wiki_pages` | Wiki 场景断言来源/页面数量 |
| `followups` | 多轮会话：首个 prompt 完成后在同一 session 依次发送的后续 prompt（制造 user 边界，多轮压缩场景必需） |
| `max_skill_view_per_name` | 同一 `(name, file_path)` 的 `skill_view` 调用次数上限，防止压缩后重读循环；同一 Skill 的不同引用文件是不同资源，不算重复 |
| `expect_compaction_recovery` | 断言 case 的 `llm.jsonl` 真实请求中出现了完整压缩摘要和受保护结果恢复附件 |
| `timeout_seconds` | 单 case 超时 |

改完后先只跑新 case：

```bash
python scripts/run_e2e_batch.py --case my_new_case
```

## 6. 新增特殊 setup

普通场景只需要 YAML。如果新功能需要特殊初始化，例如新的知识库格式、新的外部运行时，则扩展：

```text
tests/e2e/_run_case.py
```

在 `_run_case()` 里增加一个 `_setup_xxx()`，例如：

- Wiki 种子：`_setup_wiki()`
- 外援运行时：`_setup_external()`
- 附件：`_setup_attachments()`

然后在 YAML 里用字段触发，例如：

```yaml
setup: wiki_seed
wiki_seed:
  kb_id: e2e
  pages:
    - title: 测试页
      page_type: topic
      content: 唯一标识：xxxx
```

## 7. 跨平台注意

- 脚本只使用 Python 标准库和 `yaml`，没有 bash 依赖。
- 批量入口会自动找 `.venv`：
  - macOS / Linux：`.venv/bin/python`
  - Windows：`.venv\Scripts\python.exe`
- 工作区路径统一用 `Path`，YAML 里的相对路径统一使用 `/`。
- 外部 CLI 通过 `shutil.which` 探测，找不到时 skip。

## 8. 建议

- 真实 LLM 测试会消耗额度，不要放进默认 CI；建议单独维护一个 E2E job。
- 场景断言不要写死模型输出全文，尽量断言工具调用、文件产物和关键词。
- 模型行为有波动时，先看 `llm.jsonl` 判断是 prompt 问题还是产品问题，不要直接改断言掩盖。
