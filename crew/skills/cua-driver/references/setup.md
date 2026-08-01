# CUA Driver 安装与发行边界

## Crew 内的安装入口

Desktop 的“设置 → MCP”提供 CUA Driver 状态检测和按需安装入口。一键安装流程支持
macOS、Windows 和 Linux，完成后会写入本地 MCP 配置、启动后台服务并热加载
`cua-driver__*` 工具。

macOS 安装会使用 CUA Driver 官方安装程序，并通过 `/Applications/CuaDriver.app` 启动后台服务。
首次执行桌面操作前，需要在“系统设置 → 隐私与安全性”中向 CuaDriver 授予辅助功能权限；
使用截图、SOM 或视觉模式时还需要屏幕录制权限。

安装完成后，应先确认设置页显示 MCP 已连接，并确认工具列表中出现 `cua-driver__*`。

## 源码、Skill 与可执行程序

Crew 源码包含：

- `cua-driver` Skill，即模型使用桌面自动化工具时的操作规范；
- CUA Driver 的状态检测、按需安装与 MCP 接入代码。

Crew 源码和安装包不包含 CUA Driver 的第三方可执行程序。用户触发安装时会从
[CUA Driver 官方项目](https://github.com/trycua/cua/tree/main/libs/cua-driver)联网获取对应平台程序，
并自行授予操作系统要求的辅助功能或屏幕录制权限。因此，仓库中存在本 Skill 不代表 CUA Driver
已经安装或获得系统权限。

## 排查顺序

1. 在 Crew 设置页确认 CUA Driver 已安装。
2. 确认后台服务正在运行。
3. 确认 MCP Server 已连接且注册了 `cua-driver__*` 工具。
4. 检查操作系统的辅助功能权限；只有 `som` 或 `vision` 模式需要屏幕录制能力。
5. 重新读取窗口状态，不要沿用先前获得的元素索引。
