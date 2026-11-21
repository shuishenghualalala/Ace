//! Installer-only persistent WFP policy for the two Ace sandbox accounts.
//!
//! SID-derived GUIDs make setup/repair/uninstall idempotent per account. Filters
//! are scoped by ALE_USER_ID: the offline account is blocked, while the online
//! account may reach only the fixed loopback proxy port and is otherwise blocked.

use std::ffi::OsStr;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::OsStrExt;
use std::ptr::{null, null_mut};

use sha2::{Digest, Sha256};
use windows_sys::core::GUID;
use windows_sys::Win32::Foundation::{
    LocalFree, FWP_E_ALREADY_EXISTS, FWP_E_FILTER_NOT_FOUND, FWP_E_NOT_FOUND, HANDLE, HLOCAL,
};
use windows_sys::Win32::NetworkManagement::WindowsFilteringPlatform::*;
use windows_sys::Win32::Security::Authorization::{
    BuildSecurityDescriptorW, BuildTrusteeWithSidW, EXPLICIT_ACCESS_W, GRANT_ACCESS,
};
use windows_sys::Win32::Security::{
    EqualSid, GetAce, GetSecurityDescriptorDacl, IsValidSecurityDescriptor, IsValidSid,
    ACCESS_ALLOWED_ACE, ACL, DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR,
};

use super::token::{sid_string_for_account, LocalSid};
use windows_sys::Win32::System::Rpc::RPC_C_AUTHN_DEFAULT;
use windows_sys::Win32::System::Threading::INFINITE;

const PROXY_PORT: u16 = 43119;
const LEGACY_PROVIDER_KEY: GUID = GUID::from_u128(0x4f4f9ca9_7741_4c6a_a230_6d0f24479c20);
const LEGACY_SUBLAYER_KEY: GUID = GUID::from_u128(0x7d54fa23_8d4c_4632_ae37_47d56f1bc75d);
const LEGACY_OFFLINE_V4: GUID = GUID::from_u128(0x60232765_66dd_4813_a070_810a6f51d4e1);
const LEGACY_OFFLINE_V6: GUID = GUID::from_u128(0xe8fd818f_9c95_46cb_8103_f193c8c7d86f);
const LEGACY_ONLINE_PERMIT_V4: GUID = GUID::from_u128(0x7ac9494d_fdef_4eea_9252_b62f9c040944);
const LEGACY_ONLINE_PERMIT_V6: GUID = GUID::from_u128(0x847f5760_3cec_45f7_aac2_7272fa311e87);
const LEGACY_ONLINE_BLOCK_V4: GUID = GUID::from_u128(0xdf12efcc_d5c8_4113_bb2a_d0781ce56599);
const LEGACY_ONLINE_BLOCK_V6: GUID = GUID::from_u128(0xd07399a9_a34e_4858_84ed_dd9f323f6fd1);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AccountRole {
    Offline,
    Online,
}

impl AccountRole {
    fn key(self, sid: &str, object: &str) -> GUID {
        let mut hash = Sha256::new();
        hash.update(b"ace-security-runtime/windows-wfp/v2\0");
        hash.update(match self {
            Self::Offline => &b"offline\0"[..],
            Self::Online => &b"online\0"[..],
        });
        hash.update(object.as_bytes());
        hash.update([0]);
        hash.update(sid.as_bytes());
        let digest = hash.finalize();
        let mut bytes: [u8; 16] = digest[..16]
            .try_into()
            .expect("SHA-256 has a 16-byte prefix");
        // Mark these deterministic identifiers as RFC 9562 custom UUIDs.
        bytes[6] = (bytes[6] & 0x0f) | 0x80;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        GUID {
            data1: u32::from_be_bytes(bytes[0..4].try_into().expect("four bytes")),
            data2: u16::from_be_bytes(bytes[4..6].try_into().expect("two bytes")),
            data3: u16::from_be_bytes(bytes[6..8].try_into().expect("two bytes")),
            data4: bytes[8..16].try_into().expect("eight bytes"),
        }
    }
}

#[derive(Clone, Copy)]
struct AccountWfpKeys {
    provider: GUID,
    sublayer: GUID,
    block_v4: GUID,
    block_v6: GUID,
    permit_v4: GUID,
    permit_v6: GUID,
}

impl AccountWfpKeys {
    fn new(sid: &str, role: AccountRole) -> Self {
        Self {
            provider: role.key(sid, "provider"),
            sublayer: role.key(sid, "sublayer"),
            block_v4: role.key(sid, "block-v4"),
            block_v6: role.key(sid, "block-v6"),
            permit_v4: role.key(sid, "permit-v4"),
            permit_v6: role.key(sid, "permit-v6"),
        }
    }
}

pub fn install(offline_account: &str, online_account: &str) -> Result<(), String> {
    let offline_sid = sid_string_for_account(offline_account)?;
    let online_sid = sid_string_for_account(online_account)?;
    let offline_keys = AccountWfpKeys::new(&offline_sid, AccountRole::Offline);
    let online_keys = AccountWfpKeys::new(&online_sid, AccountRole::Online);
    let offline_sid_value = LocalSid::from_string(&offline_sid)?;
    let online_sid_value = LocalSid::from_string(&online_sid)?;
    let engine = Engine::open()?;
    let filter_security = FilterSecurity::for_installing_user(&engine)?;
    let mut transaction = engine.transaction()?;
    ensure_provider(engine.handle, &offline_keys)?;
    ensure_sublayer(engine.handle, &offline_keys)?;
    ensure_provider(engine.handle, &online_keys)?;
    ensure_sublayer(engine.handle, &online_keys)?;
    let offline = UserCondition::for_sid(&offline_sid_value)?;
    let online = UserCondition::for_sid(&online_sid_value)?;
    for (key, layer) in [
        (offline_keys.block_v4, FWPM_LAYER_ALE_AUTH_CONNECT_V4),
        (offline_keys.block_v6, FWPM_LAYER_ALE_AUTH_CONNECT_V6),
    ] {
        replace_filter(
            engine.handle,
            key,
            layer,
            &offline,
            false,
            &filter_security,
            &offline_keys,
        )?;
    }
    for (key, layer) in [
        (online_keys.permit_v4, FWPM_LAYER_ALE_AUTH_CONNECT_V4),
        (online_keys.permit_v6, FWPM_LAYER_ALE_AUTH_CONNECT_V6),
    ] {
        replace_filter(
            engine.handle,
            key,
            layer,
            &online,
            true,
            &filter_security,
            &online_keys,
        )?;
    }
    for (key, layer) in [
        (online_keys.block_v4, FWPM_LAYER_ALE_AUTH_CONNECT_V4),
        (online_keys.block_v6, FWPM_LAYER_ALE_AUTH_CONNECT_V6),
    ] {
        replace_filter(
            engine.handle,
            key,
            layer,
            &online,
            false,
            &filter_security,
            &online_keys,
        )?;
    }
    transaction.commit()?;
    verify_policy(offline_account, false)?;
    verify_policy(online_account, true)?;
    remove_legacy_filters(&offline_sid, &online_sid)?;
    verify_installed(offline_account, false)?;
    verify_installed(online_account, true)
}

pub fn uninstall(
    offline_account: Option<&str>,
    online_account: Option<&str>,
) -> Result<(), String> {
    if offline_account.is_none() && online_account.is_none() {
        return Ok(());
    }
    let engine = Engine::open()?;
    let mut transaction = engine.transaction()?;
    if let Some(account) = offline_account {
        let sid = sid_string_for_account(account)?;
        delete_account_policy(
            engine.handle,
            &AccountWfpKeys::new(&sid, AccountRole::Offline),
        )?;
        remove_legacy_account_filters(engine.handle, &sid, AccountRole::Offline)?;
    }
    if let Some(account) = online_account {
        let sid = sid_string_for_account(account)?;
        delete_account_policy(
            engine.handle,
            &AccountWfpKeys::new(&sid, AccountRole::Online),
        )?;
        remove_legacy_account_filters(engine.handle, &sid, AccountRole::Online)?;
    }
    transaction.commit()
}

/// Verify the filters for the account about to execute a restricted command.
pub fn verify_installed(account: &str, proxy: bool) -> Result<(), String> {
    verify_policy(account, proxy)?;
    verify_no_legacy_filters_for_account(account, proxy)
}

fn verify_policy(account: &str, proxy: bool) -> Result<(), String> {
    let engine = Engine::open()?;
    let sid = sid_string_for_account(account)?;
    let account_sid = LocalSid::from_string(&sid)?;
    let role = if proxy {
        AccountRole::Online
    } else {
        AccountRole::Offline
    };
    let keys = AccountWfpKeys::new(&sid, role);
    for (layer, block, permit) in [
        (
            FWPM_LAYER_ALE_AUTH_CONNECT_V4,
            keys.block_v4,
            keys.permit_v4,
        ),
        (
            FWPM_LAYER_ALE_AUTH_CONNECT_V6,
            keys.block_v6,
            keys.permit_v6,
        ),
    ] {
        if proxy {
            verify_filter(&engine, layer, block, &account_sid, false, &keys)?;
            verify_filter(&engine, layer, permit, &account_sid, true, &keys)?;
        } else {
            verify_filter(&engine, layer, block, &account_sid, false, &keys)?;
        }
    }
    Ok(())
}

fn verify_filter(
    engine: &Engine,
    layer: GUID,
    key: GUID,
    account_sid: &LocalSid,
    permit_proxy: bool,
    keys: &AccountWfpKeys,
) -> Result<(), String> {
    let mut filter: *mut FWPM_FILTER0 = null_mut();
    check(
        unsafe { FwpmFilterGetByKey0(engine.handle, &key, &mut filter) },
        "FwpmFilterGetByKey0; run Windows sandbox setup or repair",
    )?;
    let matches = unsafe { filter.as_ref() }
        .is_some_and(|filter| filter_matches(filter, &layer, account_sid, permit_proxy, keys));
    unsafe { FwpmFreeMemory0((&mut filter as *mut *mut FWPM_FILTER0).cast()) };
    if matches {
        Ok(())
    } else {
        Err("Windows sandbox firewall policy does not match the sandbox account; run setup or repair"
            .to_string())
    }
}

fn filter_matches(
    filter: &FWPM_FILTER0,
    layer: &GUID,
    account_sid: &LocalSid,
    permit_proxy: bool,
    keys: &AccountWfpKeys,
) -> bool {
    if !filter_shape_matches(filter, layer, permit_proxy, keys) {
        return false;
    }
    let conditions = unsafe {
        std::slice::from_raw_parts(filter.filterCondition, filter.numFilterConditions as usize)
    };
    conditions.iter().any(|condition| {
        same_key(&condition.fieldKey, &FWPM_CONDITION_ALE_USER_ID)
            && user_condition_matches(
                unsafe { condition.conditionValue.Anonymous.sd.as_ref() }
                    .expect("validated user condition"),
                account_sid,
            )
    })
}

fn filter_shape_matches(
    filter: &FWPM_FILTER0,
    layer: &GUID,
    permit_proxy: bool,
    keys: &AccountWfpKeys,
) -> bool {
    let (action, count, weight) = if permit_proxy {
        (FWP_ACTION_PERMIT, 3, 15)
    } else {
        (FWP_ACTION_BLOCK, 1, 0)
    };
    if !same_key(&filter.layerKey, layer)
        || !same_key(&filter.subLayerKey, &keys.sublayer)
        || !unsafe { filter.providerKey.as_ref() }.is_some_and(|key| same_key(key, &keys.provider))
        || filter.flags & FWPM_FILTER_FLAG_DISABLED != 0
        || filter.action.r#type != action
        || filter.weight.r#type != FWP_UINT8
        || unsafe { filter.weight.Anonymous.uint8 } != weight
        || filter.numFilterConditions != count
        || filter.filterCondition.is_null()
    {
        return false;
    }
    let conditions = unsafe { std::slice::from_raw_parts(filter.filterCondition, count as usize) };
    let user_conditions: Vec<_> = conditions
        .iter()
        .filter(|condition| {
            same_key(&condition.fieldKey, &FWPM_CONDITION_ALE_USER_ID)
                && condition.matchType == FWP_MATCH_EQUAL
                && condition.conditionValue.r#type == FWP_SECURITY_DESCRIPTOR_TYPE
                && unsafe { condition.conditionValue.Anonymous.sd.as_ref() }
                    .is_some_and(user_condition_is_valid)
        })
        .collect();
    if user_conditions.len() != 1 {
        return false;
    }
    if !permit_proxy {
        return true;
    }
    let port_matches = conditions.iter().any(|condition| {
        same_key(&condition.fieldKey, &FWPM_CONDITION_IP_REMOTE_PORT)
            && condition.matchType == FWP_MATCH_EQUAL
            && condition.conditionValue.r#type == FWP_UINT16
            && unsafe { condition.conditionValue.Anonymous.uint16 } == PROXY_PORT
    });
    let loopback_matches = conditions.iter().any(|condition| {
        same_key(&condition.fieldKey, &FWPM_CONDITION_FLAGS)
            && condition.matchType == FWP_MATCH_FLAGS_ALL_SET
            && condition.conditionValue.r#type == FWP_UINT32
            && unsafe { condition.conditionValue.Anonymous.uint32 }
                == FWP_CONDITION_FLAG_IS_LOOPBACK
    });
    port_matches && loopback_matches
}

fn user_condition_is_valid(blob: &FWP_BYTE_BLOB) -> bool {
    const ACCESS_ALLOWED_ACE_TYPE: u8 = 0;
    let descriptor = blob.data.cast();
    let mut present = 0;
    let mut defaulted = 0;
    let mut dacl: *mut ACL = null_mut();
    if blob.data.is_null()
        || unsafe { IsValidSecurityDescriptor(descriptor) } == 0
        || unsafe { GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl, &mut defaulted) }
            == 0
        || present == 0
        || !unsafe { dacl.as_ref() }.is_some_and(|dacl| dacl.AceCount == 1)
    {
        return false;
    }
    let mut ace = null_mut();
    if unsafe { GetAce(dacl, 0, &mut ace) } == 0 {
        return false;
    }
    unsafe { ace.cast::<ACCESS_ALLOWED_ACE>().as_ref() }.is_some_and(|ace| {
        ace.Header.AceType == ACCESS_ALLOWED_ACE_TYPE
            && (ace.Header.AceSize as usize) >= size_of::<ACCESS_ALLOWED_ACE>()
            && ace.Mask == FWP_ACTRL_MATCH_FILTER
            && unsafe { IsValidSid((&raw const ace.SidStart).cast_mut().cast()) } != 0
    })
}

fn user_condition_matches(blob: &FWP_BYTE_BLOB, account_sid: &LocalSid) -> bool {
    if !user_condition_is_valid(blob) {
        return false;
    }
    let descriptor = blob.data.cast();
    let mut present = 0;
    let mut defaulted = 0;
    let mut dacl: *mut ACL = null_mut();
    if unsafe { GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl, &mut defaulted) }
        == 0
    {
        return false;
    }
    let mut ace = null_mut();
    if unsafe { GetAce(dacl, 0, &mut ace) } == 0 {
        return false;
    }
    unsafe { ace.cast::<ACCESS_ALLOWED_ACE>().as_ref() }.is_some_and(|ace| unsafe {
        EqualSid(
            (&raw const ace.SidStart).cast_mut().cast(),
            account_sid.as_ptr(),
        ) != 0
    })
}

fn remove_legacy_filters(offline_sid: &str, online_sid: &str) -> Result<(), String> {
    let engine = Engine::open()?;
    let mut transaction = engine.transaction()?;
    remove_legacy_account_filters(engine.handle, offline_sid, AccountRole::Offline)?;
    remove_legacy_account_filters(engine.handle, online_sid, AccountRole::Online)?;
    transaction.commit()
}

fn verify_no_legacy_filters_for_account(account: &str, proxy: bool) -> Result<(), String> {
    let sid = sid_string_for_account(account)?;
    let account_sid = LocalSid::from_string(&sid)?;
    let role = if proxy {
        AccountRole::Online
    } else {
        AccountRole::Offline
    };
    let engine = Engine::open()?;
    for key in legacy_filter_keys(role) {
        let disposition = inspect_legacy_filter(engine.handle, key, &account_sid)?;
        if legacy_filter_requires_migration(disposition) {
            return Err(
                "legacy Windows sandbox firewall rules remain; run setup or repair".to_string(),
            );
        }
        if disposition == LegacyFilterDisposition::MalformedAcePolicy {
            return Err(
                "legacy Windows sandbox firewall rule has no verifiable account identity; administrator cleanup is required"
                    .to_string(),
            );
        }
    }
    Ok(())
}

fn remove_legacy_account_filters(
    engine: HANDLE,
    sid: &str,
    role: AccountRole,
) -> Result<(), String> {
    let account_sid = LocalSid::from_string(sid)?;
    for key in legacy_filter_keys(role) {
        match inspect_legacy_filter(engine, key, &account_sid)? {
            LegacyFilterDisposition::CurrentIdentity => delete_filter(engine, &key)?,
            LegacyFilterDisposition::MalformedAcePolicy => {
                return Err(
                    "legacy Windows sandbox firewall rule has no verifiable account identity; administrator cleanup is required"
                        .to_string(),
                );
            }
            LegacyFilterDisposition::Missing
            | LegacyFilterDisposition::Foreign
            | LegacyFilterDisposition::OtherIdentity => {}
        }
    }
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum LegacyFilterDisposition {
    Missing,
    Foreign,
    CurrentIdentity,
    OtherIdentity,
    MalformedAcePolicy,
}

fn inspect_legacy_filter(
    engine: HANDLE,
    key: GUID,
    account_sid: &LocalSid,
) -> Result<LegacyFilterDisposition, String> {
    let mut filter: *mut FWPM_FILTER0 = null_mut();
    let status = unsafe { FwpmFilterGetByKey0(engine, &key, &mut filter) };
    if status == FWP_E_FILTER_NOT_FOUND as u32 || status == FWP_E_NOT_FOUND as u32 {
        return Ok(LegacyFilterDisposition::Missing);
    }
    check(
        status,
        "FwpmFilterGetByKey0 while inspecting legacy sandbox rules",
    )?;
    let Some(value) = (unsafe { filter.as_ref() }) else {
        unsafe { FwpmFreeMemory0((&mut filter as *mut *mut FWPM_FILTER0).cast()) };
        return Err("Windows returned an empty legacy sandbox filter".to_string());
    };
    let owned_by_legacy_policy = unsafe { value.providerKey.as_ref() }
        .is_some_and(|provider| same_key(provider, &LEGACY_PROVIDER_KEY))
        && same_key(&value.subLayerKey, &LEGACY_SUBLAYER_KEY);
    let disposition = if !owned_by_legacy_policy {
        LegacyFilterDisposition::Foreign
    } else {
        match filter_user_condition_matches(value, account_sid) {
            Some(true) => LegacyFilterDisposition::CurrentIdentity,
            Some(false) => LegacyFilterDisposition::OtherIdentity,
            None => LegacyFilterDisposition::MalformedAcePolicy,
        }
    };
    unsafe { FwpmFreeMemory0((&mut filter as *mut *mut FWPM_FILTER0).cast()) };
    Ok(disposition)
}

fn legacy_filter_requires_migration(disposition: LegacyFilterDisposition) -> bool {
    disposition == LegacyFilterDisposition::CurrentIdentity
}

fn legacy_filter_keys(role: AccountRole) -> Vec<GUID> {
    match role {
        AccountRole::Offline => vec![LEGACY_OFFLINE_V4, LEGACY_OFFLINE_V6],
        AccountRole::Online => vec![
            LEGACY_ONLINE_PERMIT_V4,
            LEGACY_ONLINE_PERMIT_V6,
            LEGACY_ONLINE_BLOCK_V4,
            LEGACY_ONLINE_BLOCK_V6,
        ],
    }
}

fn filter_user_condition_matches(filter: &FWPM_FILTER0, account_sid: &LocalSid) -> Option<bool> {
    const MAX_INSPECTED_CONDITIONS: u32 = 16;
    if filter.filterCondition.is_null()
        || filter.numFilterConditions == 0
        || filter.numFilterConditions > MAX_INSPECTED_CONDITIONS
    {
        return None;
    }
    let conditions = unsafe {
        std::slice::from_raw_parts(filter.filterCondition, filter.numFilterConditions as usize)
    };
    let mut user_conditions = conditions
        .iter()
        .filter(|condition| same_key(&condition.fieldKey, &FWPM_CONDITION_ALE_USER_ID));
    let condition = user_conditions.next()?;
    if user_conditions.next().is_some()
        || condition.matchType != FWP_MATCH_EQUAL
        || condition.conditionValue.r#type != FWP_SECURITY_DESCRIPTOR_TYPE
    {
        return None;
    }
    let descriptor = unsafe { condition.conditionValue.Anonymous.sd.as_ref() }?;
    if !user_condition_is_valid(descriptor) {
        return None;
    }
    Some(user_condition_matches(descriptor, account_sid))
}

fn delete_account_policy(engine: HANDLE, keys: &AccountWfpKeys) -> Result<(), String> {
    for key in [keys.block_v4, keys.block_v6, keys.permit_v4, keys.permit_v6] {
        delete_filter(engine, &key)?;
    }
    allow_missing(
        unsafe { FwpmSubLayerDeleteByKey0(engine, &keys.sublayer) },
        "FwpmSubLayerDeleteByKey0",
    )?;
    allow_missing(
        unsafe { FwpmProviderDeleteByKey0(engine, &keys.provider) },
        "FwpmProviderDeleteByKey0",
    )
}

fn same_key(left: &GUID, right: &GUID) -> bool {
    left.data1 == right.data1
        && left.data2 == right.data2
        && left.data3 == right.data3
        && left.data4 == right.data4
}

struct Engine {
    handle: HANDLE,
}
impl Engine {
    fn open() -> Result<Self, String> {
        let mut session: FWPM_SESSION0 = unsafe { zeroed() };
        let name = wide("Ace sandbox WFP");
        session.displayData.name = name.as_ptr() as *mut _;
        session.txnWaitTimeoutInMSec = INFINITE;
        let mut handle = 0;
        check(
            unsafe {
                FwpmEngineOpen0(
                    null(),
                    RPC_C_AUTHN_DEFAULT as u32,
                    null(),
                    &session,
                    &mut handle,
                )
            },
            "FwpmEngineOpen0",
        )?;
        Ok(Self { handle })
    }
    fn transaction(&self) -> Result<Transaction<'_>, String> {
        check(
            unsafe { FwpmTransactionBegin0(self.handle, 0) },
            "FwpmTransactionBegin0",
        )?;
        Ok(Transaction {
            engine: self,
            committed: false,
        })
    }
}
impl Drop for Engine {
    fn drop(&mut self) {
        unsafe {
            FwpmEngineClose0(self.handle);
        }
    }
}
struct Transaction<'a> {
    engine: &'a Engine,
    committed: bool,
}
impl Transaction<'_> {
    fn commit(&mut self) -> Result<(), String> {
        check(
            unsafe { FwpmTransactionCommit0(self.engine.handle) },
            "FwpmTransactionCommit0",
        )?;
        self.committed = true;
        Ok(())
    }
}
impl Drop for Transaction<'_> {
    fn drop(&mut self) {
        if !self.committed {
            unsafe {
                FwpmTransactionAbort0(self.engine.handle);
            }
        }
    }
}

struct FilterSecurity(PSECURITY_DESCRIPTOR);

impl FilterSecurity {
    fn for_installing_user(engine: &Engine) -> Result<Self, String> {
        // A normal host token can open BFE but needs FWPM_ACTRL_READ on each
        // filter. Preserve the filter container's default permissions and add
        // only that read right for the trusted Windows user who runs Ace.
        let mut descriptor = null_mut();
        check(
            unsafe {
                FwpmFilterGetSecurityInfoByKey0(
                    engine.handle,
                    null(),
                    DACL_SECURITY_INFORMATION,
                    null_mut(),
                    null_mut(),
                    null_mut(),
                    null_mut(),
                    &mut descriptor,
                )
            },
            "FwpmFilterGetSecurityInfoByKey0",
        )?;
        let result = Self::with_user_read(descriptor);
        unsafe { FwpmFreeMemory0(&mut descriptor) };
        result
    }

    fn with_user_read(descriptor: PSECURITY_DESCRIPTOR) -> Result<Self, String> {
        let mut user = super::state::current_user_sid()?;
        let mut access: EXPLICIT_ACCESS_W = unsafe { zeroed() };
        access.grfAccessPermissions = FWPM_ACTRL_READ;
        access.grfAccessMode = GRANT_ACCESS;
        unsafe { BuildTrusteeWithSidW(&mut access.Trustee, user.as_mut_ptr().cast()) };
        let mut length = 0;
        let mut updated = null_mut();
        check(
            unsafe {
                BuildSecurityDescriptorW(
                    null(),
                    null(),
                    1,
                    &access,
                    0,
                    null(),
                    descriptor,
                    &mut length,
                    &mut updated,
                )
            },
            "BuildSecurityDescriptorW for Ace WFP filter",
        )?;
        Ok(Self(updated))
    }
}

impl Drop for FilterSecurity {
    fn drop(&mut self) {
        unsafe { LocalFree(self.0 as HLOCAL) };
    }
}

struct UserCondition {
    descriptor: PSECURITY_DESCRIPTOR,
    blob: FWP_BYTE_BLOB,
}
impl UserCondition {
    fn for_sid(sid: &LocalSid) -> Result<Self, String> {
        let mut access: EXPLICIT_ACCESS_W = unsafe { zeroed() };
        access.grfAccessPermissions = FWP_ACTRL_MATCH_FILTER;
        access.grfAccessMode = GRANT_ACCESS;
        unsafe {
            BuildTrusteeWithSidW(&mut access.Trustee, sid.as_ptr());
        }
        let mut descriptor = null_mut();
        let mut length = 0;
        check(
            unsafe {
                BuildSecurityDescriptorW(
                    null(),
                    null(),
                    1,
                    &access,
                    0,
                    null(),
                    null_mut(),
                    &mut length,
                    &mut descriptor,
                )
            },
            "BuildSecurityDescriptorW",
        )?;
        Ok(Self {
            descriptor,
            blob: FWP_BYTE_BLOB {
                size: length,
                data: descriptor as *mut u8,
            },
        })
    }
}
impl Drop for UserCondition {
    fn drop(&mut self) {
        unsafe {
            LocalFree(self.descriptor as HLOCAL);
        }
    }
}

fn ensure_provider(engine: HANDLE, keys: &AccountWfpKeys) -> Result<(), String> {
    let name = wide("Ace sandbox WFP identity");
    let provider = FWPM_PROVIDER0 {
        providerKey: keys.provider,
        displayData: FWPM_DISPLAY_DATA0 {
            name: name.as_ptr() as *mut _,
            description: null_mut(),
        },
        flags: FWPM_PROVIDER_FLAG_PERSISTENT,
        providerData: empty_blob(),
        serviceName: null_mut(),
    };
    allow_exists(
        unsafe { FwpmProviderAdd0(engine, &provider, null_mut()) },
        "FwpmProviderAdd0",
    )
}

fn ensure_sublayer(engine: HANDLE, keys: &AccountWfpKeys) -> Result<(), String> {
    let name = wide("Ace sandbox WFP identity");
    let provider = keys.provider;
    let sublayer = FWPM_SUBLAYER0 {
        subLayerKey: keys.sublayer,
        displayData: FWPM_DISPLAY_DATA0 {
            name: name.as_ptr() as *mut _,
            description: null_mut(),
        },
        flags: FWPM_SUBLAYER_FLAG_PERSISTENT,
        providerKey: &provider as *const _ as *mut _,
        providerData: empty_blob(),
        weight: 0x8000,
    };
    allow_exists(
        unsafe { FwpmSubLayerAdd0(engine, &sublayer, null_mut()) },
        "FwpmSubLayerAdd0",
    )
}

fn replace_filter(
    engine: HANDLE,
    key: GUID,
    layer: GUID,
    user: &UserCondition,
    permit_proxy: bool,
    security: &FilterSecurity,
    keys: &AccountWfpKeys,
) -> Result<(), String> {
    delete_filter(engine, &key)?;
    let mut conditions = vec![FWPM_FILTER_CONDITION0 {
        fieldKey: FWPM_CONDITION_ALE_USER_ID,
        matchType: FWP_MATCH_EQUAL,
        conditionValue: FWP_CONDITION_VALUE0 {
            r#type: FWP_SECURITY_DESCRIPTOR_TYPE,
            Anonymous: FWP_CONDITION_VALUE0_0 {
                sd: &user.blob as *const _ as *mut _,
            },
        },
    }];
    if permit_proxy {
        conditions.push(FWPM_FILTER_CONDITION0 {
            fieldKey: FWPM_CONDITION_IP_REMOTE_PORT,
            matchType: FWP_MATCH_EQUAL,
            conditionValue: FWP_CONDITION_VALUE0 {
                r#type: FWP_UINT16,
                Anonymous: FWP_CONDITION_VALUE0_0 { uint16: PROXY_PORT },
            },
        });
        conditions.push(FWPM_FILTER_CONDITION0 {
            fieldKey: FWPM_CONDITION_FLAGS,
            matchType: FWP_MATCH_FLAGS_ALL_SET,
            conditionValue: FWP_CONDITION_VALUE0 {
                r#type: FWP_UINT32,
                Anonymous: FWP_CONDITION_VALUE0_0 {
                    uint32: FWP_CONDITION_FLAG_IS_LOOPBACK,
                },
            },
        });
    }
    let name = wide(if permit_proxy {
        "Ace permit local proxy"
    } else {
        "Ace block sandbox outbound"
    });
    let provider = keys.provider;
    let filter = FWPM_FILTER0 {
        filterKey: key,
        displayData: FWPM_DISPLAY_DATA0 {
            name: name.as_ptr() as *mut _,
            description: null_mut(),
        },
        flags: FWPM_FILTER_FLAG_PERSISTENT,
        providerKey: &provider as *const _ as *mut _,
        providerData: empty_blob(),
        layerKey: layer,
        subLayerKey: keys.sublayer,
        weight: FWP_VALUE0 {
            r#type: FWP_UINT8,
            Anonymous: FWP_VALUE0_0 {
                uint8: if permit_proxy { 15 } else { 0 },
            },
        },
        numFilterConditions: conditions.len() as u32,
        filterCondition: conditions.as_mut_ptr(),
        action: FWPM_ACTION0 {
            r#type: if permit_proxy {
                FWP_ACTION_PERMIT
            } else {
                FWP_ACTION_BLOCK
            },
            Anonymous: FWPM_ACTION0_0 {
                filterType: GUID::from_u128(0),
            },
        },
        Anonymous: FWPM_FILTER0_0 { rawContext: 0 },
        reserved: null_mut(),
        filterId: 0,
        effectiveWeight: FWP_VALUE0 {
            r#type: FWP_EMPTY,
            Anonymous: unsafe { zeroed() },
        },
    };
    let mut id = 0;
    check(
        unsafe { FwpmFilterAdd0(engine, &filter, security.0, &mut id) },
        "FwpmFilterAdd0",
    )
}

fn delete_filter(engine: HANDLE, key: &GUID) -> Result<(), String> {
    allow_missing(
        unsafe { FwpmFilterDeleteByKey0(engine, key) },
        "FwpmFilterDeleteByKey0",
    )
}
fn check(code: u32, operation: &str) -> Result<(), String> {
    if code == 0 {
        Ok(())
    } else {
        Err(format!("{operation} failed: 0x{code:08X}"))
    }
}
fn allow_exists(code: u32, operation: &str) -> Result<(), String> {
    if code == 0 || code == FWP_E_ALREADY_EXISTS as u32 {
        Ok(())
    } else {
        check(code, operation)
    }
}
fn allow_missing(code: u32, operation: &str) -> Result<(), String> {
    if code == 0 || code == FWP_E_FILTER_NOT_FOUND as u32 || code == FWP_E_NOT_FOUND as u32 {
        Ok(())
    } else {
        check(code, operation)
    }
}
fn empty_blob() -> FWP_BYTE_BLOB {
    FWP_BYTE_BLOB {
        size: 0,
        data: null_mut(),
    }
}
fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value
        .as_ref()
        .encode_wide()
        .chain(std::iter::once(0))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::mem::size_of;
    #[test]
    fn policy_keys_are_stable_and_scoped_to_the_account_identity() {
        let first = AccountWfpKeys::new("S-1-5-21-100", AccountRole::Offline);
        let repeated = AccountWfpKeys::new("S-1-5-21-100", AccountRole::Offline);
        let other_account = AccountWfpKeys::new("S-1-5-21-200", AccountRole::Offline);
        let online_role = AccountWfpKeys::new("S-1-5-21-100", AccountRole::Online);
        let keys = [
            first.provider,
            first.sublayer,
            first.block_v4,
            first.block_v6,
            first.permit_v4,
            first.permit_v6,
            other_account.provider,
            other_account.sublayer,
            other_account.block_v4,
            other_account.block_v6,
            other_account.permit_v4,
            other_account.permit_v6,
            online_role.provider,
            online_role.sublayer,
            online_role.block_v4,
            online_role.block_v6,
            online_role.permit_v4,
            online_role.permit_v6,
        ];
        assert!(same_key(&first.provider, &repeated.provider));
        for (index, key) in keys.iter().enumerate() {
            let duplicate = keys[..index].iter().any(|other| same_key(other, key));
            assert!(
                !duplicate,
                "duplicate per-identity WFP key at index {index}"
            );
        }
        assert_eq!(size_of::<GUID>(), 16);
    }

    #[test]
    fn legacy_migration_requires_confirmed_current_identity() {
        assert!(legacy_filter_requires_migration(
            LegacyFilterDisposition::CurrentIdentity
        ));
        assert!(!legacy_filter_requires_migration(
            LegacyFilterDisposition::MalformedAcePolicy
        ));
        assert!(!legacy_filter_requires_migration(
            LegacyFilterDisposition::OtherIdentity
        ));
        assert!(!legacy_filter_requires_migration(
            LegacyFilterDisposition::Foreign
        ));
        assert!(!legacy_filter_requires_migration(
            LegacyFilterDisposition::Missing
        ));
    }

    #[test]
    fn legacy_rule_owner_is_readable_even_when_its_shape_changed() {
        let account = std::env::var("USERNAME").unwrap();
        let account_sid =
            LocalSid::from_string(&sid_string_for_account(&account).unwrap()).unwrap();
        let unrelated_sid = LocalSid::from_string("S-1-1-0").unwrap();
        let user = UserCondition::for_sid(&account_sid).unwrap();
        let mut condition = FWPM_FILTER_CONDITION0 {
            fieldKey: FWPM_CONDITION_ALE_USER_ID,
            matchType: FWP_MATCH_EQUAL,
            conditionValue: FWP_CONDITION_VALUE0 {
                r#type: FWP_SECURITY_DESCRIPTOR_TYPE,
                Anonymous: FWP_CONDITION_VALUE0_0 {
                    sd: &user.blob as *const _ as *mut _,
                },
            },
        };
        let mut filter: FWPM_FILTER0 = unsafe { zeroed() };
        filter.numFilterConditions = 1;
        filter.filterCondition = &mut condition;

        assert_eq!(
            filter_user_condition_matches(&filter, &account_sid),
            Some(true)
        );
        assert_eq!(
            filter_user_condition_matches(&filter, &unrelated_sid),
            Some(false)
        );
        filter.numFilterConditions = 17;
        assert_eq!(filter_user_condition_matches(&filter, &account_sid), None);
    }

    #[test]
    fn filter_security_grants_only_metadata_read_to_the_installing_user() {
        let security = FilterSecurity::with_user_read(null_mut()).unwrap();
        let mut present = 0;
        let mut defaulted = 0;
        let mut dacl = null_mut();
        assert_ne!(
            unsafe {
                GetSecurityDescriptorDacl(security.0, &mut present, &mut dacl, &mut defaulted)
            },
            0
        );
        assert_ne!(present, 0);
        assert_eq!(unsafe { (*dacl).AceCount }, 1);
        let mut ace = null_mut();
        assert_ne!(unsafe { GetAce(dacl, 0, &mut ace) }, 0);
        let ace = unsafe { &*ace.cast::<ACCESS_ALLOWED_ACE>() };
        assert_eq!(ace.Mask, FWPM_ACTRL_READ);
        let mut user = super::super::state::current_user_sid().unwrap();
        assert_ne!(
            unsafe {
                EqualSid(
                    (&raw const ace.SidStart).cast_mut().cast(),
                    user.as_mut_ptr().cast(),
                )
            },
            0
        );
    }

    #[test]
    fn verification_rejects_a_filter_bound_to_another_account() {
        let account = std::env::var("USERNAME").unwrap();
        let account_sid =
            LocalSid::from_string(&sid_string_for_account(&account).unwrap()).unwrap();
        let rule = UserCondition::for_sid(&account_sid).unwrap();
        let everyone = LocalSid::from_string("S-1-1-0").unwrap();
        assert!(user_condition_matches(&rule.blob, &account_sid));
        assert!(!user_condition_matches(&rule.blob, &everyone));
    }

    #[test]
    fn account_identity_not_desktop_session_is_filter_boundary() {
        assert_eq!(PROXY_PORT, 43119);
        assert_eq!(size_of::<GUID>(), 16);
    }
}
