---
name: crew-guide
description: Crew 使用手册目录——回答平台使用类问题（技能安装等）时，先读本索引再按主题读取对应章节
category: 帮助
metadata:
  zh_name: 使用手册
  zh_description: 回答平台使用问题，如技能安装、卸载、技能存放路径等
  query_examples:
  - 怎么安装技能？
  - 我下载了一个技能压缩包，要放到哪里？
  - 可安装技能怎么用？
  - 如何卸载一个技能？
  - 装完技能为什么列表里看不到？
  skillCategoryName: 通用办公
python: ">=3.8"
---
# Crew 使用手册（目录）

本手册用于回答平台的使用类问题。**回答前先读取对应章节**：根据用户问题在下表找到主题，
调用 `skill_view(name='crew-guide', file_path='<对应文件>')` 读取该章节全文，再据实回答。
不要凭印象编造路径、命令或步骤。

## 章节

| 主题 | 何时读取 | 文件 |
|---|---|---|
| 技能安装 | 用户问"怎么安装/卸载技能""技能放哪个路径""可安装技能怎么用""装完看不到"等 | `references/install-skill.md` |

> 后续新增模块时，在本表加一行，并在 `references/` 下新增对应 `.md` 文件即可。

## 使用方式

1. 根据用户问题匹配上表主题。
2. 调用 `skill_view(name='crew-guide', file_path='references/对应文件.md')` 读取章节。
3. 依据章节内容回答；若问题超出已有章节范围，如实告知并给出已知的最接近指引。
