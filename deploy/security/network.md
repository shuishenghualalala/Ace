# Managed network deployment

Ace 的 managed 进程默认没有宿主网络。联网任务使用本地 HTTP 代理；每个新连接按精确
`host + protocol + port` 重新判定，deny 优先。代理解析目标后检查每个 IP，并直接连接已经检查的
`SocketAddr`，避免“检查域名后再次解析”的时间窗。

Linux 后端始终创建独立 network namespace。宿主代理通过权限为 0700 的临时目录和 Unix socket
桥接到 namespace 内的固定 loopback 端口；删除 `HTTP_PROXY` 变量不会恢复直连。任务退出后临时
socket 目录被删除。

Windows 后端由安装器创建离线和联网两个非管理员技术账号。持久 WFP 规则按 `ALE_USER_ID` 绑定
账号 SID：离线账号阻断出站，联网账号只允许 `127.0.0.1:43119`，其他连接阻断。目标规则仍在
每任务代理中执行。WFP setup/repair/uninstall 只操作 Ace 的稳定 GUID。

边界：

- 当前只实现普通 HTTP absolute-form 和 HTTPS/TCP `CONNECT`，不实现 SOCKS/UDP。
- 不做 HTTPS MITM，不安装根证书；HTTPS 只能约束 CONNECT authority、解析后的 IP 和端口，不能
  检查加密的 path、method 或正文。
- localhost、私网和 link-local 必须使用显式 `allow_private`；云元数据地址永久拒绝。
- Windows `allow_local_binding=true` 在独立 bind-capable 技术账号落地前失败关闭；默认不能监听。
- 发布前必须在 Linux namespace 和 Windows elevated WFP 测试机执行原生对抗门禁；源码契约测试
  不能替代目标平台测试。
