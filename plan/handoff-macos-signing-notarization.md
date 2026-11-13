# macOS 安装包正式签名与公证：后续计划

## 目标

让 GitHub 预发布中的 arm64、x64 DMG 下载后可直接通过 macOS Gatekeeper，并能正常双击打开。

## 当前证据（2026-10-21）

- v0.28.36 arm64 原版 DMG 的 SHA-256 与 GitHub Release 资产摘要一致，`hdiutil verify` 通过；下载未损坏。
- 原版 `.app` 的 `codesign --verify --deep --strict` 报 `code has no resources but signature indicates they must be present`。打包脚本在 electron-builder 签名后继续裁剪 locales、嵌入 Gateway 与运行时，破坏了封存。
- 已在 `deb-package/pack_mac.sh` 完成最终 ad hoc 签封与严格校验；本地修复版 DMG 重新挂载后校验通过，但 `spctl` 仍拒绝 ad hoc 应用。
- 曾在 ad hoc 签名时启用 Hardened Runtime，导致 `dyld` 报主程序与 Electron Framework 的 Team ID 不匹配、应用启动即退出；已从 ad hoc 路径移除 Hardened Runtime。修正后的本机安装版显示 Crew 窗口，Gateway 的 `/api/health` 返回 `ok: true`。正式签名时所有嵌套代码必须使用同一 Developer ID 团队身份。
- 当前打包脚本已加入最终签名、复制后签名、DMG 校验和 Electron 最小启动探针；探针在正确的 ad hoc 签名下返回成功标记，在故意复现的 Hardened Runtime / Team ID 冲突下无法返回标记。若检测到已有 Apple 团队签名，脚本会中止，避免被 ad hoc 签名覆盖。
- GitHub 仓库 Actions Secrets 页面显示无 repository secrets；本机钥匙串显示 0 个有效代码签名身份。`security-prebuilt.yml` 仅为安全运行组件预留了可选的 `MACOS_CERTIFICATE` 和 `MACOS_CERTIFICATE_PASSWORD`，桌面版工作流尚未接入正式签名或公证。

## 实施条件与步骤

1. 由 Apple Developer 账号持有人创建或确认 `Developer ID Application` 证书，以及 App Store Connect 公证 API Key。将证书与公证凭据配置为 GitHub Actions Secrets；不要提交到仓库或聊天。
2. 在 macOS 打包的**全部内容组装完成之后**，按内到外顺序签署内嵌 Python/Node 原生程序和库、Electron 子应用与 Framework，最后签署主 `.app`，启用 Hardened Runtime。正式签名不使用 `codesign --deep --force` 代替逐层签名。
3. 对最终 DMG 提交 Apple 公证、等待通过并 stapling；在发布前校验签名、公证票据和镜像完整性。任一步失败即阻止 macOS 资产发布。
4. 在干净的 macOS Apple 芯片和 Intel 机器上，从 GitHub 预发布下载对应 DMG，确认 `spctl` 接受且无需系统设置手动放行即可双击启动。

## 验收标准

- 两种架构的最终 `.app` 均有正确的 Developer ID 团队标识，`codesign --verify --deep --strict` 通过。
- 两个 DMG 均完成公证和 stapling，`spctl --assess --type execute` 接受从 DMG 安装的应用。
- 从 GitHub 实际下载的两个包均可在启用 Gatekeeper 的对应架构 macOS 上直接打开。
- 发布说明移除当前 ad hoc 手动放行提示。
