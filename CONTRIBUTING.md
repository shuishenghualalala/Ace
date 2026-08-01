# 为 Crew 贡献代码

感谢你参与改进 Crew。欢迎通过 Issue 讨论问题和建议，并通过 Pull Request 提交改动。

## 开始之前

- 对较大的功能、架构或兼容性改动，请先创建 Issue 说明目标和方案。
- 安全漏洞不要提交公开 Issue，请按照 [安全政策](SECURITY.md) 私下报告。
- 请确认你有权提交相关代码、文档和资源，不要提交第三方机密或来源不明的内容。

## 开发环境

环境要求和完整安装步骤见 [README](README.md#环境要求)。从仓库根目录创建 Python 环境：

```bash
uv venv .venv --python 3.11
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
uv pip install -e ".[dev,wiki]"
```

安装前端依赖：

```bash
cd desktop && npm install
cd ../web && npm install
```

本地模型配置、API Key、运行数据和构建产物不得提交。请使用仓库提供的 `.example` 文件创建本地配置。

## 提交前检查

从仓库根目录运行 Python 检查：

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest
```

检查 Desktop：

```bash
cd desktop
npm run check
```

检查 Web：

```bash
cd web
npm run typecheck
npm test
npm run build
```

需要真实模型或网络的端到端测试默认不会运行；仅在已配置测试凭据且明确需要时运行：

```bash
.venv/bin/python -m pytest -m e2e
```

## Pull Request 要求

- 一个 Pull Request 聚焦一个主题，避免混入无关格式化或重构。
- 描述问题、解决方案、验证方式和用户可见影响。
- 行为变更应补充或更新测试；用户使用方式变化时同步更新文档。
- 不要提交 API Key、Token、Cookie、真实用户数据、本地配置、日志、数据库或生成产物。
- 保持提交信息简洁明确；当前不强制 Conventional Commits。

## 贡献许可

除非你明确另行声明，向本项目提交的贡献将按照 [Apache License 2.0](LICENSE) 授权。提交贡献即表示你确认有权按该许可证提供相关内容。
