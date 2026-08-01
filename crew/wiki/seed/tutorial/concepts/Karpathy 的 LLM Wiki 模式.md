---
page_type: topic
title: Karpathy 的 LLM Wiki 模式
file_path: topics/Karpathy 的 LLM Wiki 模式.md
sources: []
related: [LLM Wiki 是什么, LLM Wiki 与 RAG 的区别, 人机分工原则]
tags: [理论]
aliases: [Karpathy Wiki, LLM Wiki 模式]
---

# Karpathy 的 LLM Wiki 模式

Crew 的 LLM Wiki 功能源自 Andrej Karpathy 提出的一个模式：用 LLM 把原始资料**编译**成一批互相链接的 Markdown 文件，构成一个持久的、会复利增长的知识库。原始出处是他的公开 gist（github.com/karpathy 的 LLM Wiki gist）。

## 核心思想

- **编译一次，持续复用**：资料进来时做一次深度整理，产出的 Wiki 页面之后被反复查询复用。交叉引用已经建好，矛盾已经标记，综合结论反映了所有已摄入的资料。
- **交叉引用**：页面之间用双方括号链接（形如 `［［页面名］］`），知识不是孤立的笔记堆，而是一张网。
- **矛盾显式标记**：新信息和旧内容冲突时，不悄悄覆盖，而是标注两种说法及其来源和日期，留给人来裁决。

这与传统 RAG「每次查询重新检索一遍」的路线有本质不同，详见 [[LLM Wiki 与 RAG 的区别]]。

## 人机分工

模式里明确划定了分工：**人策展和指导，agent 整理和维护**。

- 人决定哪些资料值得收录、审核 AI 整理的结果、删除不要的页面。
- Agent 负责摘要、交叉引用、归档、保持一致性。

在 Crew 里的具体落地见 [[人机分工原则]]。

## 文件即数据库

整个 Wiki 就是一个 Markdown 文件目录：没有数据库，没有专用工具锁。页面用 YAML frontmatter 存元数据，用双方括号 wikilink 表达关系，可以用任何编辑器打开。Crew 在此之上提供了 WikiHub 界面和对话式操作，见 [[WikiHub 界面导览]]。

## 对 Crew 的启发

Crew 把这个模式产品化：上传文档、粘贴文本自动整理成互链页面，对话里一句话就能把结论记成页面，同时保留「人最终把关」的原则 —— 你可以随时编辑、删除任何页面。
