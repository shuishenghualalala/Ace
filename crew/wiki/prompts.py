"""Wiki Agent 提示词与工具描述。"""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------- #
# 工具描述（注册到 registry 时作为 tool 的 description）
# --------------------------------------------------------------------------- #

WIKI_ORIENT_PROMPT = """获取当前 Wiki 知识库的全景信息。

在任何 Wiki 操作（上传、整理、查询、修复）之前，先调用此工具了解：
- 当前知识库的描述和规则（SCHEMA.md）
- 已有页面列表和类型分布
- 最近变更日志
- 页面标题/别名/标签索引

返回结果会帮助你在后续步骤中避免重复建页、正确匹配已有页面、遵守知识库规则。"""

WIKI_BATCH_INGEST_PROMPT = """批量整理当前知识库中已经解析的 RawSource。

一次最多处理 5 份素材；可通过 source_ids 指定范围，也可省略后按 cursor 分批处理全部
parsed source。返回成功、跳过、失败、剩余数量和 next_cursor。每份素材仍遵守整篇最多
5 个 Entity、3 个 Topic 的上限，并复用分块缓存、plan/apply 和来源去重。

wiki.ingest.auto_apply=true 时自动应用本批计划；关闭时先返回整批计划和一次性确认 ID。"""

WIKI_SEARCH_PROMPT = """检索 Wiki 页面并返回与问题相关的证据上下文。

## 使用时机
回答实质问题前优先调用此工具。简单事实查询只搜一次即可；复杂问题按需多次搜索。

## 查询构造（关键）
- **提取关键词再搜，不要传整句**。把用户问题中的核心概念、实体名、专有名词提取出来
  作为查询词。比如用户问"人和 AI 各自擅长什么？"，应搜"人机分工"而非整句。
- 首轮搜索无结果时，尝试同义词、别名、更宽泛或更具体的词重试。
- 涉及两个以上实体比较或跨实体推理时，分别搜索每个实体，再综合结果回答。

## 工具行为
它会融合标题/别名、全文与结构化 index 召回，默认扩展一跳关联页面、按质量信号重排，
并抽取与问题相关的正文和 Claim/Evidence；结果同时展示为前端 Wiki 卡片。
简单定位时可关闭邻居扩展或上下文生成。

## 边界
知识库无结果时如实说明，不得用内部知识补齐。不得用同一查询词反复搜索超过 2 次。"""

WIKI_READ_PROMPT = """读取指定 Wiki 页面的完整内容。

参数 page_id 是页面 ID。当用户要求查看、编辑或讨论某个具体页面时调用。需要沿知识图谱继续
研究时设置 include_neighbors=true，并通过 neighbor_limit 控制返回的关联页面数量。"""

WIKI_LINT_PROMPT = """检查 Wiki 知识库的健康状况。

默认（deep=false）做快速程序化检查：断链、孤立页面、页面格式违规、时效性标记。
deep=true 时会额外调用 LLM 做语义检查：页面间矛盾、概念缺口。

常规用法：先说「检查下我的知识库」走默认检查；如果怀疑有结构性问题，再说「深度检查下我的知识库」并传 deep=true。"""

WIKI_CREATE_KB_PROMPT = """创建一个新的 Wiki 知识库。

参数 kb_id 是知识库的唯一标识，支持中文、其他语言的字母、数字、下划线和连字符；name 是显示名称。创建成功后可以往里添加页面或上传文件。default 知识库已存在，无需创建。"""

WIKI_DELETE_KB_PROMPT = """预检或删除指定 Wiki 知识库。首次调用返回影响范围和一次性 confirmation_id；仅在用户后续确认回合传回同一 ID 才执行。禁止删除内置的 default 与 tutorial，其他知识库删除后不可恢复。"""

WIKI_DELETE_SOURCE_PROMPT = """预检或删除 raw source 及其关联页面。首次调用返回影响范围和一次性 confirmation_id；仅在用户后续确认回合传回同一 ID、source 和 KB 时执行。该操作不可恢复。"""

WIKI_UPDATE_PAGE_PROMPT = """更新指定 Wiki 页面的内容或元数据。

参数 page_id 是要更新的页面 ID。可更新字段：
- content: 页面 Markdown 内容（完全替换）
- related: 相关页面标题列表（会覆盖原有 related，用于建立双向链接）
- tags: 标签列表（覆盖）
- aliases: 别名列表（覆盖）

只传入需要修改的字段；未传入的字段保持原值。常用于修复孤立页面、补充关联关系或修正内容。"""

WIKI_PARSE_SOURCE_PROMPT = """解析指定 RawSource 并生成 parsed markdown。

参数 source_id 是 raw source ID。当 raw source 的 parse_status 为 failed 或 pending、
或你怀疑自动解析丢失了结构时调用。工具会按来源类型自动路由：文档走结构化解析，图片走
视觉理解，视频经一次性外传确认后走云端理解。成功时自动保存 parsed markdown、更新状态，并发布
一个可搜索的
全文 Source 页面。默认上传流程必须继续 wiki_orient → wiki_plan_ingest；plan 使用有界的原子
知识单元提取主张和证据，再确定性聚合为实体、主题和关系的创建/更新/跳过/争议计划。
成功分块会持久化缓存，重试只处理失败块。wiki_plan_ingest 会遵循 wiki.ingest.auto_apply：默认自动应用；
配置关闭时才展示计划并停止等待确认，由后续确认回合调用 wiki_apply_ingest。

如果仍失败，保留 RawSource，并依据工具返回的错误与恢复建议处理；不得读取用户未提供的其他本地路径。"""

WIKI_LIST_SOURCES_PROMPT = """列出当前 Wiki 知识库中的所有 raw sources（上传文件、URL、会话记录或粘贴文本）。

当用户说「把我刚上传的文件编进知识库」「ingest 刚才的 PDF」「编译那个文档」但没有提供 source_id 时，
先调用此工具获取当前知识库的 raw source 列表，然后选择对应的 source_id 调用 wiki_plan_ingest(source_id)。

重要：拿到列表后必须立即选择 source_id 进入下一步（wiki_plan_ingest / wiki_parse_source），
禁止在同一轮中反复调用 wiki_list_sources 做无意义的确认。
如果列表为空或没有用户要找的文档：先检查本轮用户消息是否已带附件（消息开头会列出「附件『文件名』位于: 路径」），
有附件则对每个附件调用 wiki_capture_attachment 捕获为 RawSource 后继续入库流程；
仅当本轮消息确实没有附件时，才请用户通过 Wiki Composer 附件区上传；不得使用通用文件搜索猜测本地路径。

参数 status 可按 parse_status 过滤：all（全部，默认）、parsed、failed、pending。
参数 limit 限制返回数量，默认 50。
参数 kb_id 可指定知识库；未指定时使用当前活跃知识库。"""

WIKI_LIST_KBS_PROMPT = """列出当前用户拥有的所有 Wiki 知识库。

当用户提到"另一个知识库"、"切换到 xx 知识库"或需要跨库操作时，先调用此工具获取所有可用知识库列表。
返回每个知识库的 id、显示名称、页面数和来源数。"""

WIKI_LIST_INBOX_PROMPT = """列出当前知识库中已捕获但尚未深度整理的素材（待整理 inbox）。

当用户说"整理我刚上传的文件"、"看看待整理的素材"、"有哪些素材可以入库"或类似意思时调用。
返回每个素材的 source_id、标题、类型、轻量摘要、标签和系统是否建议整理。用户确认后再调用 wiki_plan_ingest(source_id) 生成深度整理计划。"""

# --------------------------------------------------------------------------- #
# Wiki Agent 系统 prompt
# --------------------------------------------------------------------------- #

def _load_wiki_agent_preset_prompt() -> str:
    """读取 Wiki 预设正文，不导入 ``crew.agent``，避免包初始化循环。"""
    path = Path(__file__).resolve().parents[1] / "agent" / "subagent" / "presets" / "wiki.md"
    content = path.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) == 3 and parts[2].strip():
            return parts[2].strip()
    raise RuntimeError(f"Wiki Agent 预设缺少有效正文: {path}")


WIKI_AGENT_SYSTEM_PROMPT = _load_wiki_agent_preset_prompt()

WIKI_AGENT_CONTEXT_REMINDER = """[Wiki Agent 当前上下文]
- 当前活跃知识库（active_kb_id）：{active_kb_id}
- 当前活跃知识库名称：{active_kb_name}
- 可用知识库列表：{kb_list}

本轮约束：默认操作活跃知识库；跨库时显式传入 `kb_id`。不要猜测或自行填写 `kb_id`（包括字面量 "default"）：用户未明确要求跨库时，所有工具调用都必须省略 `kb_id` 参数，由系统按活跃知识库处理。附件默认 capture → parse → orient → plan；parse 先发布全文 Source 页面，plan 再生成实体/主题/关系的创建、更新和跳过计划。概念统一归入实体。plan 工具遵循 wiki.ingest.auto_apply：默认自动 apply，配置关闭时才展示计划并停止等待确认。本轮消息已附带的文件路径在用户文本开头给出，需要入库时直接逐个 `wiki_capture_attachment`，不要让用户重新上传。
"""

# --------------------------------------------------------------------------- #
# P0 新增工具
# --------------------------------------------------------------------------- #

WIKI_PLAN_INGEST_PROMPT = """对 raw source 执行轻量知识单元提取，确定性生成页面创建/更新/跳过/争议计划。

每个知识单元只包含一个规范 subject、一条主张、短证据和必要关系；工具不会让每个分块撰写完整
页面或生成无上限知识图谱。只有核心主题，或由两条以上独立主张支持的辅助主题才会新建页面。
成功分块按 source 持久化，重复调用会复用缓存并只重试未完成块；截断响应保留已闭合的完整
unit。返回的 analysis_stats 提供 total_chunks、analyzed_chunks、cache_hits、failed_chunks、
truncated_chunks 与 elapsed_ms。

执行边界由 config.yaml 的 wiki.ingest.auto_apply 控制：
- true（默认）：计划成功后立即自动应用，并返回 auto_applied=true 和实际写入结果。
- false：只返回计划与一次性 confirmation_id，展示后等待用户确认，后续再调用 wiki_apply_ingest。

可选参数：
- chunk_size（整数）：长文档分块分析的字符阈值。未指定时使用系统默认值。
- use_chunking（布尔）：是否强制启用/禁用分块分析。未指定时系统按文档长度自动判断。

通常保持默认分块即可；只有用户明确要求诊断切分质量时才调整 chunk_size/use_chunking。"""

WIKI_APPLY_INGEST_PROMPT = """执行已生成的编译计划，将 raw source 写入 Wiki 页面。

调用前必须先使用 wiki_plan_ingest(source_id) 生成变更计划；apply_ingest 会读取该计划并按计划创建/更新/跳过页面。

参数 approved_titles 可选：如果用户提供，则只应用列表中的页面标题；未提供时应用整个计划。

可选参数 chunk_size / use_chunking 仅在未找到已有 plan、回退到完整 ingest 时生效；正常情况下 plan 已在 plan_ingest 阶段确定。

执行成功后工具会自动维护 index、log、搜索索引和摘要状态，不要重复调用内部收尾工具。"""

WIKI_FETCH_URL_PROMPT = """抓取指定 URL 的网页内容，自动将 HTML 转为 Markdown，并创建 wiki raw source。

传入 url 参数即可。系统会自动：
1. 抓取 URL 内容
2. HTML 页面自动转换为结构化的 Markdown
3. 创建 raw source（source_type=url），原始内容不可变
4. 保存转换后的 Markdown 为 parsed 内容

返回 source_id，后续调用 wiki_plan_ingest；该工具会按 auto_apply 配置自动应用或返回确认请求。"""

WIKI_REFRESH_SOURCE_PROMPT = """重新抓取一个 URL RawSource，并与当前版本比较内容 hash。

内容未变化时不创建新来源；发生变化时创建不可变的新 RawSource，写入 drift_from 版本关系，
发布新的全文 Source 页面，并返回新 source_id。后续对新版本调用 wiki_plan_ingest。"""

WIKI_DIGEST_PROMPT = """基于当前知识库至少两个独立来源生成持久化的跨来源报告。

topic 是综合主题；mode 可为 auto、comparison 或 synthesis。auto 会根据用户是否表达
比较意图选择页面类型。证据不足两个 RawSource 时不会生成页面。"""
