# Windows WFP lifecycle

`ace-security-runtime --windows-setup <absolute-state-dir>` 是唯一允许提权的安装/修复入口。
它创建离线/联网技术账号、用 DPAPI 保存随机凭据，并在一个 WFP transaction 内建立 Ace
自有 provider、sublayer 和 filters。日常任务不请求 UAC。

固定代理端口为 `127.0.0.1:43119`。联网账号只有该 loopback 目标的高权重 permit filter，随后由
低权重 block-all filter 拦截其他出站；离线账号直接 block-all。所有 filter 都带
`FWPM_CONDITION_ALE_USER_ID`，不会按 Desktop session 或 Ace `owner_id` 动态安装规则。

修复重复使用同一组 GUID 并先删除同 key filter；卸载只删除这些 GUID，不枚举或改动其他软件的
防火墙对象。任一步骤失败都不得转用普通用户 token 直连网络。

原生发布验收必须覆盖：幂等 setup/repair、非管理员日常执行、代理可达、直连/IPv6/DNS/SMB/元数据
不可达、卸载不影响第三方规则，以及重启后的持久性。
