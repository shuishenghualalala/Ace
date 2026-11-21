# Windows WFP lifecycle

`ace-security-runtime --windows-setup <absolute-state-dir>` 是唯一允许提权的安装/修复入口。
它创建离线/联网技术账号、用 DPAPI 保存随机凭据，并在 WFP transaction 内建立当前身份
专属的 provider、sublayer 和 filters。日常任务不请求 UAC。

固定代理端口为 `127.0.0.1:43119`。联网账号只有该 loopback 目标的高权重 permit filter，随后由
低权重 block-all filter 拦截其他出站；离线账号直接 block-all。所有 filter 都带
`FWPM_CONDITION_ALE_USER_ID`，不会按 Desktop session 或 Ace `owner_id` 动态安装规则。

每个 WFP key 由规范 SID、账号角色和对象类型通过 SHA-256 派生，两个独立状态目录的账户不会
替换彼此的规则。同一身份的修复按派生 key 原子重建规则；卸载只处理该身份的 keys 与经 owner
核验的旧 keys，不枚举或改动其他软件的防火墙对象。运行前按当前账户和角色验证 provider、
sublayer、过滤器动作、权重、地址族及条件；缺失或不匹配时拒绝受管命令。

升级会先在一个事务中写入新身份规则，再确认规则匹配，随后迁移旧版固定 GUID。迁移只清理
Ace 旧 provider/sublayer 下能够确认属于当前 SID 的过滤器；即使规则形状已变化，只要旧过滤器中的
用户 SID 可验证，仍按所属配置迁移。可验证为其他 SID 的旧规则保留；无法读出或验证 SID 的旧 Ace
规则会使准备失败并要求管理员处理，不按固定 GUID 删除；不属于 Ace 旧 provider/sublayer 的对象不会被删除。
运行前校验拒绝当前身份遗留的旧规则或所有权无法验证的旧 Ace 对象。卸载从受保护身份文件取离线/联网账号，
只删除这两个 SID 派生的 WFP 对象和可确认的旧规则；缺少身份信息时不做全局 WFP 删除。迁移完成后空的旧
provider/sublayer 元数据可以留在系统中，不携带有效过滤器。

任一步骤失败都不得转用普通用户 token 直连网络。

原生发布验收必须覆盖：幂等 setup/repair、非管理员日常执行、代理可达、直连/IPv6/DNS/SMB/元数据
不可达、卸载不影响第三方规则，以及重启后的持久性。
