---
name: github-advanced-search
description: GitHub 高级搜索与开源项目筛选。当用户想高效查找 GitHub 上的开源项目、代码实现、报错解决方案或特定作者/组织仓库，或提到 GitHub 搜索技巧、搜索限定符、stars:/language:/pushed: 等检索语法、评估开源项目是否值得用时触发。不适用于 Git 版本控制操作、PR 协作流程、CI/CD 部署等开发任务本身。
---

# GitHub 高级搜索与开源项目筛选

## 概述

很多人用 GitHub 只停留在「输入关键词 → 在几万条结果里乱翻 → 翻不到就以为没有」的初级模式。
实际上 GitHub 搜索支持一套 **限定符（qualifier）** 语法，能在几十万条结果里直接筛出值得看的项目、代码和讨论。

本技能把 GitHub 真正好用的搜索方法整理成可复用的流程与速查表，让你（或我这个助手）能：
- 根据自然语言需求，**构造精确的搜索查询**；
- 生成**可直接打开的 GitHub 搜索 URL**；
- 判断一个项目**到底值不值得安装/使用**。

> GitHub 不会主动把答案推到面前。会不会搜索，决定了它在你手里是一堆看不懂的代码，还是一个取之不尽的工具库和资料库。

## 何时使用本技能

触发场景（满足任一即可）：
- 用户想「在 GitHub 上找一个 XX 工具/项目/资料」；
- 用户遇到技术报错，想找 Issue、解决方案、踩坑记录；
- 用户想看看某个作者 / 组织（如 openai、facebook）公开了哪些项目；
- 用户问 GitHub 搜索语法、限定符怎么用；
- 用户拿到了一个 GitHub 项目，想判断「这项目还活着吗、能商用吗、稳定吗」。

需要**完整限定符语法与示例**时，读取 `references/reference.md`（仓库 / 代码 / Issue 三大类速查）。本文件是流程与核心速查，足够应对大多数请求。

## 你的工作方式（核心流程）

处理 GitHub 搜索类请求时，按以下步骤推进：

### Step 1 · 拆解需求
先想清楚用户要找什么，向用户确认或自行推断以下维度（不用全问，缺关键信息再问）：
- **找什么类型**：仓库项目 / 代码片段 / Issue 讨论 / 某作者的全部项目？
- **主题关键词**：如 RAG、OCR、screen recorder；
- **语言**：Python / TypeScript / 不限？
- **热度**：是否只看高星（如 stars:>500）？还是想挖低星宝藏（如 stars:50..500）？
- **新鲜度**：是否要求近期有更新（如 pushed:>2026-01-01）？
- **可用性**：是否需要排除归档、Fork、确认有 Release、确认许可证。

### Step 2 · 选择搜索类型
| 用户意图 | 搜索类型 | GitHub 搜索 URL 的 type 参数 |
|---|---|---|
| 找项目 / 工具 / 资料 | 仓库搜索 | `type=repositories` |
| 找「这个功能别人怎么实现」 | 代码搜索 | `type=code` |
| 找报错原因 / 解决方案 / 讨论 | Issue 搜索 | `type=issues` |

### Step 3 · 构造查询（叠加限定符）
把 Step 1 的需求逐项翻译成限定符，用空格连接。
规则：**一层一层往上加**，而不是一上来堆十几个条件。先搜关键词，结果太多再加热度，再加语言，最后补更新时间和状态。这样一旦结果为空，你知道该放宽哪一项。

### Step 4 · 生成可打开的搜索 URL
把查询拼进 URL 并交给用户点击（空格在 URL 中写成 `+`）：
- 仓库：`https://github.com/search?q=<查询>&type=repositories`
- 代码：`https://github.com/search?q=<查询>&type=code`
- Issue：`https://github.com/search?q=<查询>&type=issues`

示例（RAG Python 项目）：
`https://github.com/search?q=RAG+in:readme+stars:%3E500+language:python+pushed:%3E2026-01-01+archived:false+fork:false&type=repositories`

### Step 5 · 评估项目是否值得用
搜到项目只是第一步。按「项目评估清单」帮用户判断，详见下文第四节。

## 一、限定符速查表（核心）

### 仓库搜索限定符
| 限定符 | 作用 | 示例 |
|---|---|---|
| `stars:>N` | 只看星标超过 N 的仓库；也可用 `stars:100..1000` 限定范围 | `stars:>1000` |
| `language:LANG` | 指定主要语言，快速排除无关项目 | `language:python` |
| `pushed:>DATE` | 只看该日期后还有代码更新的项目（排除僵尸项目） | `pushed:>2026-01-01` |
| `archived:false` | 排除已归档（停止维护）的仓库 | `archived:false` |
| `fork:false` | 排除 Fork 副本，找到原始项目 | `fork:false` |
| `in:name` | 只在仓库**名称**里搜关键词 | `RAG in:name` |
| `in:readme` | 在 **README** 里搜关键词（更易找到主题相关项目） | `RAG in:readme` |
| `topic:TAG` | 按主题标签查找（作者主动添加，结果集中） | `topic:machine-learning` |
| `license:MIT` | 按开源协议筛选（商用/二次开发前必看） | `license:apache-2.0` |
| `org:NAME` / `user:NAME` | 找某组织 / 某作者公开的项目 | `org:openai` |

### 代码搜索限定符
| 限定符 | 作用 | 示例 |
|---|---|---|
| `repo:owner/name 关键词` | 限定在某仓库内搜代码 | `repo:facebook/react useEffect` |
| `path:目录或文件` | 只在指定路径搜 | `path:src/components` |
| `symbol:名称` | 定位函数 / 类定义（依赖语言解析） | `symbol:useEffect` |
| `"完整短语"` | 双引号查完整短语（查报错极有用） | `"connection refused"` |
| `关键词 NOT path:目录` | 排除干扰结果 | `"useEffect" NOT path:tests` |

### Issue / PR 搜索限定符
| 限定符 | 作用 | 示例 |
|---|---|---|
| `is:issue` / `is:pr` | 限定类型 | `is:issue` |
| `is:closed` / `is:open` | 按状态筛选（关闭通常代表有结论） | `is:issue is:closed` |
| `label:bug` | 按标签筛选（含空格加引号） | `label:"good first issue"` |
| `comments:>N` | 找讨论充分的（复现/排查信息多） | `comments:>5` |

完整语法、组合示例与注意事项见 `references/reference.md`。

## 二、组合搜索原则

单独一个限定符只能解决一个问题；真正的差距在于**组合**。

示例（找近期活跃、靠谱的 RAG Python 项目）：
```
RAG in:readme stars:>500 language:python pushed:>2026-01-01 archived:false fork:false
```
拆解：
1. README 提到 RAG；
2. Star > 500；
3. 主要用 Python；
4. 2026 年后仍有更新；
5. 未归档、非 Fork。

**关键心法**：不要一开始就堆一堆限定符。一层层加，好处是结果太少时你能立刻判断该放宽哪一项；反之若一上来十几个条件全空，你反而不知道问题在哪。搜索不是条件越多越高级，而是**每一个条件都应有明确作用**。

## 三、生成搜索 URL 的要点

- 三类搜索对应三个 `type`：`repositories` / `code` / `issues`；
- 查询里的空格写成 `+`，`>` 写成 `%3E`，`:` 保留；
- 优先把「可直接点击的链接」给用户，而不是只甩一串语法——用户要的是答案，不是考试。

## 四、项目评估清单（搜到项目后必看）

一个项目值不值得装，不能只看 Star。逐项检查：

1. **README 是否说清楚**：解决什么问题 / 怎么安装 / 基本用法 / 限制 / 反馈渠道。连安装都讲不清的项目，普通用户后续很难自救。
2. **最近是否还在维护**：同时看「最近提交、最近 Release、Issue 有无回复、严重问题有无处理」。只看提交日期易误判——天天提交却不稳定，或很久没改却已成熟，都正常。看**维护行为**而非一个日期。
3. **Issue 区状态**：Issue 多不代表差（用户多自然问题多）。要警惕的是：新问题无人回、严重 Bug 长期挂起、同一种错误反复出现、问题无明确结论。大量 Issue 挂几个月没人理，即使 Star 高也要谨慎。
4. **有无正式 Release**：普通用户优先选有 Release / 安装包的项目；只有源码无 Release 的可能要自己编译。
5. **许可证是否清楚**：没许可证 ≠ 可随便复制商用。二次开发、分发、商用前务必打开仓库 `LICENSE` 文件确认条款。「开源」不等于「什么都能做」。

## 五、GitHub CLI（gh）速查

常驻终端的用户可装官方 `gh` 工具，避免开几十个网页：

```bash
# 搜 Python 机器学习项目，按 Star 排序，取前 10
gh search repos "machine learning" --language python --sort stars --limit 10

# 查看某仓库信息
gh repo view owner/repository

# 列出自己的仓库
gh repo list --limit 20
```

典型流程：先用 `gh search repos` 找十几个候选 → 用 `gh repo view` 看介绍与 README → 检查更新时间、Release、Issue。

## 六、让 AI（也就是我）帮你搜 GitHub

不要只说「帮我找几个好用的 GitHub 项目」——「好用」没有标准，我大概率只是把几个最知名的仓库重列一遍，其中有些或许早已停更。

**更有效的说法**（条件越明确，结果越有用）：
> 帮我搜 GitHub 上与 RAG 有关的 Python 项目，要求：Star > 500、最近半年有更新、排除归档和 Fork、按 Star 从高到低排、整理项目介绍/更新时间/许可证、检查 README/Release/Issue、最后选出最值得尝试的 5 个。

你负责提标准，我负责按标准执行搜索、筛选与整理。

## 示例

**示例 1 · 找工具（仓库搜索）**
用户：「帮我找个最近还在更新的 OCR 工具，Python 写的。」
→ 查询：`OCR language:python stars:>500 pushed:>2026-01-01 archived:false`
→ URL：`https://github.com/search?q=OCR+language:python+stars:%3E500+pushed:%3E2026-01-01+archived:false&type=repositories`

**示例 2 · 找报错解法（Issue 搜索）**
用户：「postgres 报 connection refused 怎么解决？」
→ 查询：`"connection refused" is:issue is:closed`
（知道具体仓库时加 `repo:owner/name` 更准）
→ URL：`https://github.com/search?q=%22connection+refused%22+is:issue+is:closed&type=issues`

**示例 3 · 找某作者的全部项目**
用户：「openai 都开源了哪些东西？」
→ 查询：`org:openai`
→ URL：`https://github.com/search?q=org:openai&type=repositories`

**示例 4 · 挖低星宝藏**
用户：「想找点还没火起来的 AI agent 项目。」
→ 查询：`AI agents stars:50..500 pushed:>2026-01-01`
（50~500 星区间常藏着已能跑、未出圈的项目）

## 常见误区

- **只盯几万星大项目**：真正好玩的常只有一两百星，别错过低星区间。
- **把所有关键词塞进搜索框**：用限定符分流，比堆关键词有效得多。
- **只搜仓库名**：名字起得抽象的项目，靠 `in:readme` 才能找到。
- **只看 Star 判断质量**：Star 是热度不是质量；要结合更新、Issue、Release、License。
- **查报错复制整段日志**：找出最独特的一句，用双引号包起来搜，路径/版本号反而可能让你搜不到同类问题。
