# Wiki 公开数据集评测

第一轮用 SciFact 测科研文献检索，用 CMRC2018 开发集固定抽取 200 个中文问题。
入口为 `scripts/run_wiki_benchmark.py`，复用 Ace 的 SQLite FTS5 和 WikiQuerier，
不启动 Gateway。默认只运行词法对照，不读取模型配置；第二阶段可显式指定已有
配置，运行真实向量和精排模型。所有方案都不调用答案生成模型。

## 运行

在仓库根目录执行：

```bash
.venv/bin/python scripts/run_wiki_benchmark.py --download
```

默认设置：

- SciFact：全部 5,183 篇语料，全部有 test qrels 的查询（300 条）。
- CMRC2018：开发集全部去重原文作为共享语料库；查询按 ID 排序后，以 seed=42 抽样 200 条。
- 输出：`outputs/wiki-benchmark/results/{scifact,cmrc2018}/`，已由 `/outputs/` 忽略。
- 下载：仅公开数据文件；SciFact 压缩包核对 BEIR 发布的 MD5。
- 已有缓存时可去掉 `--download` 离线运行；`--data-dir`、`--output` 可指定目录。
- 下载中断可重新运行；数据文件按完整文件缓存，不自动更新。
- Wiki 默认每通道召回 50 个候选，导航权重为 0（只补充不足的候选），最后返回前 10 篇。
- `--candidate-limit`、`--index-weight` 可做候选预算与导航融合的消融；正导航权重启用联合 RRF。

仅运行科研数据集，或先做小样本检查：

```bash
.venv/bin/python scripts/run_wiki_benchmark.py --dataset scifact --download
.venv/bin/python scripts/run_wiki_benchmark.py --dataset scifact --scifact-limit 20
```

## 第一轮比较的方案

| 名称 | 实际行为 |
| --- | --- |
| `fts-bm25-and` | 独立的严格 AND 对照，模拟首轮全词匹配；生产查询调整后保持对照含义稳定 |
| `fts-bm25-or` | 在同一索引上改用显式 OR 匹配，其余分词、字段和 BM25 权重相同；仅作离线对照 |
| `wiki-lexical` | 当前 WikiQuerier：保留全文 BM25 排名、标题/别名精确匹配优先、关键词回退，导航补充候选 |
| `wiki-lexical-neighbors` | 上一方案加当前一跳邻居扩展 |

四种方案都没有向量模型，不能把这些结果称为混合向量检索分数。
FTS5 的空格分隔查询默认要求所有 token 同时匹配，长问题可能导致大量空结果。首轮沿用
该方式；改进后的生产查询对长问题使用 OR，对 1–6 字的纯中文关键词保持完整词组匹配。
每个查询 token 作为字面量加引号，AND/OR/NOT 等输入不会被解释成操作符。OR 对照
可区分查询匹配限制与 BM25 排序能力；该配置并不是 BEIR 官方 Elasticsearch BM25 基线。
参见 [SQLite FTS5 查询语法](https://www.sqlite.org/fts5.html#fts5_boolean_queries)。
一份原文对应一个 Source 页面时，没有整理生成的实体关系，因此邻居版可能与普通版相同。

## 报告和指标

- `report.md`：汇总表。
- `report.json`：指标、查询 ID 列表、数据/页面快照/来源映射/代码 SHA-256、抽样设置。
- `queries.jsonl`：每个方案的每个查询，返回页面 ID、投影后的原文 ID、召回通道、逐题指标和耗时。

输出 Recall、Hit、MRR、nDCG 在 @5 和 @10 的宏平均。Recall 的分母是该问题所有标注
相关文档数，Hit 只衡量是否至少命中一个。nDCG 使用线性相关性增益，重复返回同一原文
只计一次；空结果以零分计入平均。
检索调用 `top_k=max(cutoffs)`（默认 10），@5 取该次排名的前 5 篇原文。当前默认候选池
与最终 top_k 分开；首轮候选数随 top_k 变化。报告记录调用参数、候选设置与页面关系数。

基准将语料转换成带唯一标题的 Source 页面，使用内存中的不可变页面快照避免反复解析
Markdown；检索排序复用生产代码。计时不含 Markdown 磁盘加载或语料向量建库；模型版
查询计时包含在线模型调用或缓存读取，词法版不调用模型。
这些耗时不能当作桌面端/Gateway 的端到端延迟。CMRC2018 被改造成共享语料检索任务，且采用
抽样查询；这些分数用于 Ace 内部版本比较，不直接与官方榜单比较。

标准答案只保留在数据集对象和数据指纹中，不输入索引。本轮不生成答案，不输出问答
EM/F1 或幻觉分数。后续模型问答评测应记录使用的模型、提示词、引用证据和消耗。

## 首轮结果与科研实验的起点

2026-10-09 的改进前基线，按原文生成 Source 页面；未编译实体/主题页面。
旧报告保留在 `outputs/wiki-benchmark/results/`，下表记录当时的检索实现。

| 方案 | SciFact Recall@5 | SciFact Recall@10 | CMRC Recall@5 | CMRC Recall@10 |
| --- | ---: | ---: | ---: | ---: |
| `fts-bm25-and` | 4.58% | 4.58% | 1.50% | 1.50% |
| `fts-bm25-or` | 74.37% | 81.33% | 98.50% | 99.00% |
| `wiki-lexical` | 35.28% | 42.31% | 94.50% | 94.50% |
| `wiki-lexical-neighbors` | 35.28% | 42.31% | 94.50% | 94.50% |

首轮 AND 查询在 SciFact 的 300 条查询中有 284 条空结果，CMRC 的 200 条中有 197 条。
OR 查询两组均无空结果；这说明需要把查询匹配方式与 BM25 排序算法分开评价。
SciFact 的正文搜索候选（最多 20 页）有 163 条查询至少命中一篇相关文献，融合后
前 10 页只有 134 条命中，候选召回和融合排序都值得分别做消融。

CMRC 的当前 Wiki 分数主要来自导航召回：正文搜索候选命中 7 条查询，导航候选命中
192 条，最终前 10 页命中 189 条。它与 SciFact 的检索机制表现不同，不能只报告平均分。
这些是相应候选预算下的 Hit 查询数，不能与表中的宏平均 Recall 混用。

原文页没有关系边，因此邻居扩展相同；该实验尚未验证整理后的 Wiki 或图检索收益。
科研实验应继续分别比较原文与编译 Wiki、词法与向量融合、邻居开关，再评价回答和
句子级引用证据。保留 OR 词法对照，避免把很弱的 AND 查询当作唯一基线。

## 第一阶段改进与验证

生产代码的第一阶段调整：

- 长问题使用 OR 召回，中文短关键词保留词组匹配。
- Store 保留 BM25 排名；精确标题和别名优先，关键词和语义信号用于未命中全文的候选。
- WikiQuerier 每通道默认保留 50 个候选，融合后再截断；直接候选不重复按词频加分。
- 默认导航只补充不足的候选；显式正权重可启用联合 RRF，向量通道原有融合权重保留。
- 返回的 retrieval 记录候选预算、通道权重和导航补充的页面 ID。

SciFact 使用训练集的固定 100 题（seed=42），与 test 查询无交集。比较候选数
20/50/100 和导航权重 0/0.1/0.3/0.9。导航仅作补充时，三种预算的 nDCG@10 都为
0.7520、Recall@5 都为 0.8000；预算 50 时，权重 0.1/0.3/0.9 的 nDCG@10 分别为
0.6777/0.6035/0.5199。选择导航补充策略，保留 50 个候选给 Wiki/别名候选留出空间。

验证报告、逐题结果和验证脚本保存在 `outputs/wiki-benchmark/phase1-validation/`。
该脚本按预算取同一生产通道候选的前缀比较排序，不将缓存耗时视为线上延迟。
已查看过 test 结果，因此后续 test 分数作为回归检查；论文最终结果需要额外留出集。

使用训练集单独复现当前默认策略，或改变参数做消融：

```bash
.venv/bin/python scripts/run_wiki_benchmark.py \
  --dataset scifact --scifact-split train --scifact-limit 100 --seed 42 \
  --variants wiki-lexical --output outputs/wiki-benchmark/validation-replay
```

完整回归保留首轮报告，并另存改进后结果：

```bash
.venv/bin/python scripts/run_wiki_benchmark.py \
  --output outputs/wiki-benchmark/phase1-results
```

同一语料、页面快照和查询 ID 的回归结果：

| 数据集 | Recall@5 改进前 → 后 | Recall@10 改进前 → 后 | MRR@10 改进前 → 后 |
| --- | ---: | ---: | ---: |
| SciFact | 35.28% → 75.04% | 42.31% → 81.33% | 0.3139 → 0.6593 |
| CMRC2018 | 94.50% → 98.50% | 94.50% → 99.00% | 0.9337 → 0.9265 |

SciFact 的 127 题 Recall@5 改善、2 题下降；CMRC 的 9 题改善、1 题下降。
CMRC 的 MRR 略降，部分前排排序仍需改善。比较报告及退步样例保存在
`outputs/wiki-benchmark/phase1-results/comparison.md`，409 项 Wiki 测试全部通过。
本轮仍不评测答案质量，也不将没有关系边的邻居版分数用于证明图检索收益。

## 第二阶段：真实向量和独立精排

| 新增方案 | 实际行为 |
| --- | --- |
| `wiki-vector` | 生产向量通道的原始排名，作为诊断对照 |
| `wiki-hybrid` | 正文与向量候选按加权 RRF 合并，默认向量权重 0.9 |
| `wiki-lexical-rerank` | 词法候选交给独立 query/document 精排模型 |
| `wiki-hybrid-rerank` | 混合候选交给同一个精排模型 |

词法对照使用单独的无 embedding Store，避免 `store.search` 内部的语义回退污染
对照。精排处理候选池后再截取 top_k；精确标题和别名仍优先，模型同分时保留原顺序。
生产查询在服务不可用、返回数量/索引不一致或 NaN/Inf 分数时保留原召回顺序，
在 `retrieval.rerank` 记录失败。科研评测遇到这类失败会拒绝输出成功报告，不能把
降级的词法结果冒充精排结果。
服务的 429、500/502/503/504 和连接错误默认最多重试两次，重试共享 timeout 预算；
4xx 鉴权/参数错误不重试。失败记录仅保留异常类型和安全的 HTTP 状态，不回显服务响应体。

复用已有 Ace 配置，不在命令行传入密钥：

```bash
.venv/bin/python scripts/run_wiki_benchmark.py \
  --dataset scifact --scifact-split train --scifact-limit 100 \
  --variants wiki-lexical wiki-vector wiki-hybrid wiki-lexical-rerank wiki-hybrid-rerank \
  --model-config /path/to/owner/config.yaml --model-env /path/to/owner/.env \
  --output outputs/wiki-benchmark/phase2-validation

.venv/bin/python scripts/run_wiki_benchmark.py \
  --dataset all \
  --variants wiki-lexical wiki-vector wiki-hybrid wiki-lexical-rerank wiki-hybrid-rerank \
  --model-config /path/to/owner/config.yaml --model-env /path/to/owner/.env \
  --output outputs/wiki-benchmark/phase2-results
```

`--model-config` 读取 `wiki.semantic` 的模型、端点与凭据变量名；`--model-env` 只读，
省略时查找配置同目录的 `.env`。精排端点和凭据变量默认继承同账号的 embedding
配置，可用 `wiki.rerank.base_url` 和 `api_key_env` 分别覆盖。
评测显式选择模型方案时仅在内存开启模型，不修改现有 Ace 配置。
`--rerank-model` 默认 `BAAI/bge-reranker-v2-m3`，`--rerank-limit` 默认 50；
`max_chunks_per_doc` 默认 8，长文档由服务切分并取片段最高分。
接口说明见 [SiliconFlow embeddings](https://docs.siliconflow.cn/docs/api/embeddings-post) 和
[rerank](https://docs.siliconflow.cn/docs/api/rerank-post)。

模型输出缓存于 `outputs/wiki-benchmark/model-cache/`。缓存键含端点、模型和文本；
查询/文档向量分别缓存，精排以 query/document 对与片段策略缓存。报告不包含密钥，
缓存不接收标准答案或相关性标签。远程服务没有提供固定权重 revision，因此复用冻结
缓存可复现本次排名，未来新建缓存的服务结果可能变化。平均查询耗时会受缓存命中率
影响，不能用混合冷热缓存的表格证明性能加速。
离线评测默认采用 `--rerank-tpm-budget 300000` 控制一分钟窗口内的精排输入，
优先读取服务实际 token 统计，缺失时保守估算；遇到 429 冷却一分钟再补齐，最多两次
冷却重试。此策略仅用于后台评测，生产查询保留 30 秒预算与两次短重试。
平台同时限制 RPM 与 TPM，参见 [SiliconFlow Rate Limits](https://docs.siliconflow.cn/docs/userguide/faqs/rate-limit-and-upgradation)。
报告中 `openai:BAAI/bge-m3` 的前缀表示 OpenAI 兼容 embedding 适配器；本次实际端点
为已有的 SiliconFlow 服务，并非 OpenAI 的模型。

生产能力在 `wiki.rerank.enabled: true` 时启用，默认关闭；按账号读取各自配置与密钥，
关闭 App 时统一释放客户端。`wiki.query.retrieval_candidates` 和 `rerank_candidates`
默认均为 50。该阶段验证检索排名，还没有验证回答正确性、证据引用或编译 Wiki 的增益。

### 第二阶段结果（2026-10-09）

复用 Ace 已配置的 SiliconFlow `BAAI/bge-m3`（1024 维），精排使用同一服务的
`BAAI/bge-reranker-v2-m3`。候选与精排预算均为 50，向量 RRF 权重 0.9，
导航仅补充不足候选。未修改用户配置，未启动或重启 Gateway。
SciFact train 固定抽样 100 题先验证；其查询 ID 和规范化问题文本与 test 无交集。
训练验证的词法/混合加精排 Recall@5 分别为 80.00%/88.50%，nDCG@10 为
0.7520/0.8056。下表在相同语料、快照、来源映射和查询上比较五个方案，
词法对照与第一阶段逐项指标一致。

| 方案 | SciFact Recall@5 | SciFact nDCG@10 | CMRC Recall@5 | CMRC nDCG@10 |
| --- | ---: | ---: | ---: | ---: |
| 词法 | 75.04% | 0.6910 | 98.50% | 0.9423 |
| 向量 | 69.49% | 0.6373 | 99.00% | 0.9816 |
| 混合 | 79.42% | 0.7092 | 99.50% | 0.9789 |
| 词法加精排 | 80.21% | 0.7380 | 99.50% | 0.9968 |
| 混合加精排 | 81.04% | 0.7434 | 100.00% | 0.9969 |

混合加精排的 Recall@10 为 SciFact 85.33%、CMRC 100.00%；MRR@10 为
0.7140/0.9960。相对词法的 nDCG@10 增量，固定 seed=42、10,000 次配对
bootstrap 的 95% 区间分别为 [+0.0250, +0.0804]、[+0.0336, +0.0782]。
这些区间反映当前查询的波动，多个方案比较未做多重检验校正。

SciFact 候选池的相关原文覆盖率由 90.02% 增至 93.03%，单独向量低于词法；
CMRC 两种候选池均已覆盖全部标注相关原文，主要收益来自排序。
SciFact 的 29 题 Recall@5 改善、7 题下降，CMRC 为 3 题改善、0 题下降。
科研集最大的排序退步样例仍包含标注相关原文，下一轮应在训练/新验证集上
分析精排对否定命题与相似论文的区分，不据本轮 test 再调整参数。

全部成功结果保存于 `outputs/wiki-benchmark/phase2-results/`，包含逐题模型分数、
数据/代码指纹、候选池和退步案例；完整比较见 `comparison.md` 与 `comparison.json`。
运行该目录的 `compare.py` 可离线重建比较报告。最终评测两组均无精排失败；
此前遇到服务错误和 HTTP 429 时立即停止，补充有限重试与离线 token 节奏控制后
利用成功输出缓存完成重跑，没有混入降级结果。
报告的新增线上输入数仅统计该次完成运行，先前缓存的成功调用另计；查询耗时
含缓存和限额等待，不用于声称生产响应速度改善。

513 项检索、模型适配、配置、账号隔离及配置 API 测试通过，修改的检索文件
Ruff 检查通过。该测试集此前用于诊断，因此这些结果是回归与消融检查；论文最终
结论需要新留出集。CMRC 分数仅对应这 200 条问题的检索改造，未评测答案或图关系。

## 对比已经整理过的 Wiki 快照

可以将离线导出的 WikiPage（`page.to_dict()`）保存为 JSONL，再提供来源映射：

```json
{"raw_source_id_1": "original_corpus_document_id_1"}
```

```bash
.venv/bin/python scripts/run_wiki_benchmark.py \
  --dataset scifact --pages /path/to/pages.jsonl --source-map /path/to/source-map.json \
  --output outputs/wiki-benchmark/compiled-results
```

该入口复制页面数据到独立临时检索快照，不修改用户知识库。页面 sources 和 claim evidence
中的每个来源都必须映射到本语料的原文 ID，缺失映射时拒绝评测，避免凭标题猜测出处。
返回页面按排名展开来源，每个页面内部的来源按 ID 排序，再对原文去重，按前 K 篇原文
计分；来源投影不读取 gold qrels。

综合页面关联多个原文时，该评测只衡量来源覆盖，不能证明某段答案确实受到所引用句子
支持。原文版与整理版还应比较证据片段、答案正确性，以及遗漏/冲突处理。

## 数据来源

- SciFact / BEIR：<https://github.com/beir-cellar/beir>，下载
  <https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip>。
- CMRC2018：<https://github.com/ymcui/cmrc2018>，使用 `squad-style-data/cmrc2018_dev.json`。
  CMRC2018 为 CC BY-SA 4.0；数据不打包进 Ace 源码，下载后保留来源。

指标、数据适配、来源映射和生产检索接入的离线测试：

```bash
.venv/bin/python -m pytest tests/wiki -q
```
