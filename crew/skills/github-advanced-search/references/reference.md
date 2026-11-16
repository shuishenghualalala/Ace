# GitHub 搜索限定符完整参考

本文件是 `github-advanced-search` 技能的详细参考。SKILL.md 已覆盖核心流程与速查，这里给出每个限定符的语法、说明、示例与注意事项。按需读取。

---

## 一、仓库搜索限定符（Repository Search）

GitHub 域名：`https://github.com/search?q=<查询>&type=repositories`

### 1. `stars:` — 项目热度
- 语法：`stars:>N`（大于）、`stars:<N`（小于）、`stars:N..M`（区间）
- 说明：Star 不等于质量，但适合做第一轮筛选。
- 示例：
  - `stars:>1000` — 只看星标超 1000
  - `stars:100..1000` — 限定范围
- 心法：进陌生领域先看高星；想挖没出圈的新工具，把范围压低，如 `AI agents stars:50..500`——这个区间常藏着已有人用、功能能跑但还没火的项目。

### 2. `language:` — 指定语言
- 语法：`language:python`
- 说明：不同语言对应不同类型项目。Python 常见于 AI / 数据处理 / 自动化；JavaScript、TypeScript 常见于网页应用、浏览器插件、桌面软件。
- 示例：
  - `image processing language:python stars:>500`
  - `browser extension language:typescript`

### 3. `pushed:` — 排除僵尸项目
- 语法：`pushed:>YYYY-MM-DD`
- 说明：只看该日期后还有代码更新的项目。GitHub 上很多高星仓库教程是几年前的、依赖已失效、Issue 全是报错、作者已不回复。
- 示例：`screen recorder stars:>500 pushed:>2026-01-01`
- 注意：日期根据搜索时的时间调整，一般向前推半年到一年。更新时间只是参考——有些稳定小工具几年不更新也能用；有些天天提交却一直是半成品。

### 4. `archived:false` — 排除归档项目
- 语法：`archived:true` / `archived:false`
- 说明：仓库被归档通常意味着作者停止维护、进入只读。归档项目查资料、学代码仍有价值，但想找长期使用的工具最好排除。
- 示例：`OCR language:python stars:>500 archived:false`

### 5. `fork:false` — 排除重复仓库
- 语法：`fork:true` / `fork:false`
- 说明：Fork 是别人把原仓库复制到自己账号。部分做了改进，更多只是副本。不排除则结果里常出现一批内容相同的仓库。
- 示例：`video downloader stars:>100 fork:false`

### 6. `in:name` 与 `in:readme` — 控制搜索位置
- 语法：`关键词 in:name` / `关键词 in:readme`
- 说明：
  - `in:name` — 只在仓库**名称**里搜；
  - `in:readme` — 在 **README** 里搜（名字起得抽象、但 README 写清用途的项目能被找到）。
- 示例：`RAG in:readme stars:>100` 比 `RAG in:name` 能找到更多真正相关的仓库。只想找名称明确带关键词的才用 `in:name`。

### 7. `topic:` — 按主题标签查找
- 语法：`topic:machine-learning`
- 说明：Topic 是作者主动添加的主题标签，结果集中。常见：`topic:large-language-models`、`topic:self-hosted`、`topic:awesome-list`。
- 注意：有些高度相关项目作者没加对应标签，所以不要只用 `topic:`，要和普通关键词、`in:readme` 交叉搜索避免漏掉。

### 8. `license:` — 查看开源协议
- 语法：`license:mit` / `license:apache-2.0` / `license:gpl-3.0`
- 说明：「代码公开」和「随便使用」是两回事。二次开发、放进产品、商用前应先看许可证。
- 注意：用 `license:` 筛选只是第一步，真正使用前最好打开仓库 `LICENSE` 文件确认具体条款。

### 9. `org:` 与 `user:` — 找组织与作者
- 语法：`org:openai` / `user:用户名`
- 说明：特别容易被忽略。偶然发现一个好项目，点进作者主页，往往发现他维护着一整套同类工具。与其回全站盲搜，不如沿靠谱作者/组织继续挖。一个持续维护优质项目的人，往往比单个高星仓库更值得关注。

---

## 二、代码搜索限定符（Code Search）

GitHub 域名：`https://github.com/search?q=<查询>&type=code`

代码搜索解决的是：「这个功能，别人到底是怎么实现的？」

### 1. `repo:` — 限定仓库
- 语法：`repo:owner/repository 关键词`
- 说明：大型项目成百上千文件，一个目录一个目录点效率低。用 `repo:` 限定后可在整个项目内查关键词。
- 示例：`repo:facebook/react useEffect`

### 2. `path:` — 搜文件和目录
- 语法：`path:src/components` 或 `path:requirements.txt`
- 说明：定位具体配置文件或目录。
- 示例：
  - `path:.github/workflows language:yaml` — 参考别人怎么写 GitHub Actions
  - `postgres path:docker-compose.yml language:yaml` — 看 PostgreSQL 的 Docker 配置
- 心法：配置文件没必要每次从零写。找几个维护良好的项目看别人怎么组织，再按自己需求改，通常比对着文档硬啃快。

### 3. `symbol:` — 搜函数和类
- 语法：`symbol:useEffect` / `symbol:类名`
- 说明：不是简单文本匹配，而是尝试定位函数、类或其他代码符号。依赖 GitHub 对不同编程语言的解析，不同语言/仓库结果可能有差异。

### 4. 双引号 — 查完整短语
- 语法：`"connection refused"`
- 说明：搜索完整短语，查报错时极有用。遇到大段错误日志，**不要整段复制**，找出最独特、最核心的一句用双引号包起来，结果更准。日志越长不代表信息越有效；路径、版本号、设备名反而可能让你搜不到同类问题。

### 5. `NOT` — 排除干扰结果
- 语法：`"useEffect" NOT path:tests NOT path:__tests__`
- 说明：先看噪声主要来自哪里，再针对性排除。全是测试文件就排除 `tests`；全是示例项目就排除 `examples`。没必要一开始就写一长串 NOT——筛选是为减少干扰，不是让语句更复杂。
- 进阶：`"useEffect" NOT path:tests NOT path:examples NOT path:dist`

---

## 三、Issue / PR 搜索限定符

GitHub 域名：`https://github.com/search?q=<查询>&type=issues`

遇到报错，先用项目自己的 Issue 区，比搜中文教程更可靠。Issue 里常能看到完整过程：谁在什么系统/版本遇到、开发者如何判断、有无复现、哪个版本修复、该改哪项配置。这种一手讨论比被搬运几次的答案更可信。

### 1. `is:issue` / `is:pr` / `is:open` / `is:closed`
- 语法：`is:issue is:closed`
- 说明：关闭不一定代表 100% 解决，但通常意味着维护者给了某种结论。
- 配合报错：`"connection refused" is:issue is:closed`
- 知道具体仓库时加范围更准：`repo:owner/repository "connection refused" is:issue`

### 2. `label:` — 按标签筛选
- 语法：`label:bug` / `label:documentation` / `label:enhancement` / `label:"good first issue"`
- 说明：含空格的标签要加双引号。

### 3. `comments:` — 找讨论充分的
- 语法：`is:issue is:closed comments:>5`
- 说明：评论多不代表答案对，但通常意味着更多人参与复现和排查。系统兼容、版本冲突、安装失败类问题，有用信息常藏在后面评论而非 Issue 正文。

---

## 四、组合搜索进阶示例

```
# 找近期活跃、靠谱的 RAG Python 项目
RAG in:readme stars:>500 language:python pushed:>2026-01-01 archived:false fork:false
```

逐步叠加逻辑（推荐方式）：
1. `RAG` — 结果太多
2. `RAG stars:>100` — 加热度
3. `RAG stars:>100 language:python` — 要 Python
4. `RAG stars:>100 language:python pushed:>2026-01-01 archived:false` — 补更新时间与状态

好处：一旦结果太少，你知道该放宽哪一项。限定符不是越多越好，每个条件都应有明确作用。

---

## 五、搜索 URL 构造速记

| 类型 | URL 模板 |
|---|---|
| 仓库 | `https://github.com/search?q=<查询>&type=repositories` |
| 代码 | `https://github.com/search?q=<查询>&type=code` |
| Issue | `https://github.com/search?q=<查询>&type=issues` |

URL 编码要点：空格 → `+`；`>` → `%3E`；`:` 保留原样；双引号短语 → `%22短语%22`。

示例（查询 `RAG in:readme stars:>500 language:python pushed:>2026-01-01 archived:false fork:false`）：
```
https://github.com/search?q=RAG+in:readme+stars:%3E500+language:python+pushed:%3E2026-01-01+archived:false+fork:false&type=repositories
```
