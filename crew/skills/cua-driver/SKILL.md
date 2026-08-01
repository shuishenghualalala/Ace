---
name: cua-driver
description: 使用 CUA Driver 的 MCP 工具控制本机原生桌面应用，按照观察、操作、验证的顺序完成点击、输入、快捷键和界面读取任务
category: 通用办公
metadata:
  skillCategoryName: 通用办公
  version: 0.1.0
  zh_name: 桌面自动化
  zh_description: 使用 CUA Driver 控制本机原生桌面应用，并在每次操作后验证界面状态
  query_examples:
  - 打开计算器并计算 23 乘 17
  - 在系统设置里找到蓝牙页面
  - 在备忘录中创建一条笔记
python: ">=3.11"
---

# CUA Driver 桌面自动化

本 Skill 用于操作没有合适 CLI 或应用内专用工具的本机原生桌面应用。网页任务应使用
`browser-use` 和 Crew 内置浏览器，不要通过桌面自动化绕开浏览器的可见、可接管流程。

## 使用前检查

1. 确认当前工具列表中存在 `cua-driver__*` 工具。
2. 如果工具不存在，告知用户前往“设置 → MCP”安装并启用 CUA Driver；不要假装已经执行桌面操作。
3. 涉及账号、支付、发送、系统权限或其他重要外部影响时，先取得用户明确授权。

安装与发行包边界见 `references/setup.md`。

## 核心流程

严格遵循“观察 → 操作 → 验证”：

1. 使用 `cua-driver__list_apps` 或 `cua-driver__list_windows` 找到目标应用、`pid` 和 `window_id`。
2. 使用 `cua-driver__get_window_state` 读取当前界面。
3. 根据最新状态返回的 `element_index` 执行一次最小操作。
4. 再次调用 `cua-driver__get_window_state`，确认操作产生了预期结果。
5. 重复以上步骤，直到任务完成或需要用户确认。

不要复用旧的 `element_index`。窗口内容变化后必须重新观察，因为元素索引可能已经失效。

## 模式选择

- `ax`：优先使用。通过 Accessibility Tree 获取结构化界面，适合文本模型且开销较小。
- `som`：需要同时参考 Accessibility Tree 和截图时使用。
- `vision`：只有确实需要纯视觉判断时使用。

能用 `ax` 完成时，不要无故请求截图或屏幕录制权限。

## 常用工具

| 工具 | 用途 |
|------|------|
| `cua-driver__list_apps` | 列出正在运行的应用 |
| `cua-driver__list_windows` | 列出窗口并获取窗口标识 |
| `cua-driver__launch_app` | 启动原生应用 |
| `cua-driver__get_window_state` | 获取当前窗口状态 |
| `cua-driver__get_accessibility_tree` | 仅读取 Accessibility Tree |
| `cua-driver__click` / `cua-driver__right_click` | 点击界面元素 |
| `cua-driver__set_value` | 直接设置可编辑控件的值 |
| `cua-driver__type` | 向当前焦点输入文本 |
| `cua-driver__press_key` | 执行按键或组合键 |
| `cua-driver__scroll` | 滚动窗口内容 |

具体可用工具及参数以当前 MCP Server 返回的 schema 为准；不同 CUA Driver 版本可能略有差异。

## 操作约束

- 文本控件支持 `set_value` 时优先使用它，减少焦点漂移。
- 每次只执行足以推进任务的一项操作，操作后立即验证。
- 不读取或暴露与任务无关的窗口内容、通知、账号信息和敏感数据。
- 发现窗口、焦点或权限状态不符合预期时停止操作，重新观察或向用户说明阻塞原因。
- 最终回复应说明完成了什么；无法验证时应明确写出未验证部分。
