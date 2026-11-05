# crew/mcp_servers：Ace 内置 MCP server 脚本目录

本目录承载 **Ace 自有的 MCP server 可执行脚本**（Python 脚本，作为 stdio
MCP server 运行），与 `crew/tools/mcp_client.py`（消费外部 server 的客户端）
职责相对：那边是"连别人的"，这里是"给别人连的"。

## 定位与约定

- 每个脚本是一个独立可执行的 stdio MCP server，经 `config.yaml` 的
  `mcp_servers` 段配置接入，command 用 `"${CREW_PYTHON}"` 引用解释器——
  PyInstaller 冻结态下该变量指向打包内嵌的 Python 运行时（见
  `crew/state/home.py` 的 `bundled_python_executable`），避免把 gateway
  二进制当解释器导致进程递归繁殖。
- 打包发行：`deb-package/pack_mac.sh` / `pack_exe.ps1` 已通过
  `--add-data` 把本目录原样打进二进制分发（发布矩阵测试守护该约定）。
- 脚本必须仅依赖标准库 + `mcp` 包，不得 import `crew` 运行时——它们以
  子进程形态被拉起，不在主进程上下文中。

## 现状

当前为空（仅有本说明）：尚无内置 server 落地。后续新增内置 server 时，
直接把脚本放进来并在 `config/config.yaml.example` 补一条
`mcp_servers` 示例配置即可，无需改动打包与装配代码。
