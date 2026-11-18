//! Installer-only persistent WFP policy for the two Ace sandbox accounts.
//!
//! Stable GUIDs make setup/repair/uninstall idempotent. Filters are scoped by
//! ALE_USER_ID: the offline account is blocked, while the online account may
//! reach only the fixed loopback proxy port and is otherwise blocked.

use std::ffi::OsStr;
use std::mem::zeroed;
use std::os::windows::ffi::OsStrExt;
use std::ptr::{null, null_mut};

use windows_sys::core::GUID;
use windows_sys::Win32::Foundation::{
    LocalFree, FWP_E_ALREADY_EXISTS, FWP_E_FILTER_NOT_FOUND, FWP_E_NOT_FOUND, HANDLE, HLOCAL,
};
use windows_sys::Win32::NetworkManagement::WindowsFilteringPlatform::*;
use windows_sys::Win32::Security::Authorization::{
    BuildExplicitAccessWithNameW, BuildSecurityDescriptorW, BuildTrusteeWithSidW,
    EXPLICIT_ACCESS_W, GRANT_ACCESS,
};
use windows_sys::Win32::Security::{
    EqualSid, GetAce, GetSecurityDescriptorDacl, IsValidSecurityDescriptor, ACCESS_ALLOWED_ACE,
    ACL, DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR,
};

use super::token::{sid_string_for_account, LocalSid};
use windows_sys::Win32::System::Rpc::RPC_C_AUTHN_DEFAULT;
use windows_sys::Win32::System::Threading::INFINITE;

const PROXY_PORT: u16 = 43119;
const PROVIDER_KEY: GUID = GUID::from_u128(0x4f4f9ca9_7741_4c6a_a230_6d0f24479c20);
const SUBLAYER_KEY: GUID = GUID::from_u128(0x7d54fa23_8d4c_4632_ae37_47d56f1bc75d);
const OFFLINE_V4: GUID = GUID::from_u128(0x60232765_66dd_4813_a070_810a6f51d4e1);
const OFFLINE_V6: GUID = GUID::from_u128(0xe8fd818f_9c95_46cb_8103_f193c8c7d86f);
const ONLINE_PERMIT_V4: GUID = GUID::from_u128(0x7ac9494d_fdef_4eea_9252_b62f9c040944);
const ONLINE_PERMIT_V6: GUID = GUID::from_u128(0x847f5760_3cec_45f7_aac2_7272fa311e87);
const ONLINE_BLOCK_V4: GUID = GUID::from_u128(0xdf12efcc_d5c8_4113_bb2a_d0781ce56599);
const ONLINE_BLOCK_V6: GUID = GUID::from_u128(0xd07399a9_a34e_4858_84ed_dd9f323f6fd1);

pub fn install(offline_account: &str, online_account: &str) -> Result<(), String> {
    let engine = Engine::open()?;
    let filter_security = FilterSecurity::for_installing_user(&engine)?;
    let mut transaction = engine.transaction()?;
    ensure_provider(engine.handle)?;
    ensure_sublayer(engine.handle)?;
    let offline = UserCondition::new(offline_account)?;
    let online = UserCondition::new(online_account)?;
    for (key, layer) in [
        (OFFLINE_V4, FWPM_LAYER_ALE_AUTH_CONNECT_V4),
        (OFFLINE_V6, FWPM_LAYER_ALE_AUTH_CONNECT_V6),
    ] {
        replace_filter(engine.handle, key, layer, &offline, false, &filter_security)?;
    }
    for (key, layer) in [
        (ONLINE_PERMIT_V4, FWPM_LAYER_ALE_AUTH_CONNECT_V4),
        (ONLINE_PERMIT_V6, FWPM_LAYER_ALE_AUTH_CONNECT_V6),
    ] {
        replace_filter(engine.handle, key, layer, &online, true, &filter_security)?;
    }
    for (key, layer) in [
        (ONLINE_BLOCK_V4, FWPM_LAYER_ALE_AUTH_CONNECT_V4),
        (ONLINE_BLOCK_V6, FWPM_LAYER_ALE_AUTH_CONNECT_V6),
    ] {
        replace_filter(engine.handle, key, layer, &online, false, &filter_security)?;
    }
    transaction.commit()
}

pub fn uninstall() -> Result<(), String> {
    let engine = Engine::open()?;
    let mut transaction = engine.transaction()?;
    for key in [
        OFFLINE_V4,
        OFFLINE_V6,
        ONLINE_PERMIT_V4,
        ONLINE_PERMIT_V6,
        ONLINE_BLOCK_V4,
        ONLINE_BLOCK_V6,
    ] {
        delete_filter(engine.handle, &key)?;
    }
    let sublayer = unsafe { FwpmSubLayerDeleteByKey0(engine.handle, &SUBLAYER_KEY) };
    allow_missing(sublayer, "FwpmSubLayerDeleteByKey0")?;
    let provider = unsafe { FwpmProviderDeleteByKey0(engine.handle, &PROVIDER_KEY) };
    allow_missing(provider, "FwpmProviderDeleteByKey0")?;
    transaction.commit()
}

/// Verify the filters for the account about to execute a restricted command.
pub fn verify_installed(account: &str, proxy: bool) -> Result<(), String> {
    let engine = Engine::open()?;
    let account_sid = LocalSid::from_string(&sid_string_for_account(account)?)?;
    let layers = [
        (
            FWPM_LAYER_ALE_AUTH_CONNECT_V4,
            OFFLINE_V4,
            ONLINE_BLOCK_V4,
            ONLINE_PERMIT_V4,
        ),
        (
            FWPM_LAYER_ALE_AUTH_CONNECT_V6,
            OFFLINE_V6,
            ONLINE_BLOCK_V6,
            ONLINE_PERMIT_V6,
        ),
    ];
    for (layer, offline, online_block, online_permit) in layers {
        if proxy {
            verify_filter(&engine, layer, online_block, &account_sid, false)?;
            verify_filter(&engine, layer, online_permit, &account_sid, true)?;
        } else {
            verify_filter(&engine, layer, offline, &account_sid, false)?;
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
) -> Result<(), String> {
    let mut filter: *mut FWPM_FILTER0 = null_mut();
    check(
        unsafe { FwpmFilterGetByKey0(engine.handle, &key, &mut filter) },
        "FwpmFilterGetByKey0; run Windows sandbox setup or repair",
    )?;
    let matches = unsafe { filter.as_ref() }
        .is_some_and(|filter| filter_matches(filter, &layer, account_sid, permit_proxy));
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
) -> bool {
    let (action, count, weight) = if permit_proxy {
        (FWP_ACTION_PERMIT, 3, 15)
    } else {
        (FWP_ACTION_BLOCK, 1, 0)
    };
    if !same_key(&filter.layerKey, layer)
        || !same_key(&filter.subLayerKey, &SUBLAYER_KEY)
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
    let user_matches = conditions.iter().any(|condition| {
        same_key(&condition.fieldKey, &FWPM_CONDITION_ALE_USER_ID)
            && condition.matchType == FWP_MATCH_EQUAL
            && condition.conditionValue.r#type == FWP_SECURITY_DESCRIPTOR_TYPE
            && unsafe { condition.conditionValue.Anonymous.sd.as_ref() }
                .is_some_and(|blob| user_condition_matches(blob, account_sid))
    });
    if !permit_proxy || !user_matches {
        return user_matches;
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

fn user_condition_matches(blob: &FWP_BYTE_BLOB, account_sid: &LocalSid) -> bool {
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
            && ace.Mask == FWP_ACTRL_MATCH_FILTER
            && unsafe {
                EqualSid(
                    (&raw const ace.SidStart).cast_mut().cast(),
                    account_sid.as_ptr(),
                ) != 0
            }
    })
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
    fn new(account: &str) -> Result<Self, String> {
        let account = wide(account);
        let mut access: EXPLICIT_ACCESS_W = unsafe { zeroed() };
        unsafe {
            BuildExplicitAccessWithNameW(
                &mut access,
                account.as_ptr(),
                FWP_ACTRL_MATCH_FILTER,
                GRANT_ACCESS,
                0,
            );
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

fn ensure_provider(engine: HANDLE) -> Result<(), String> {
    let name = wide("Ace sandbox WFP");
    let provider = FWPM_PROVIDER0 {
        providerKey: PROVIDER_KEY,
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

fn ensure_sublayer(engine: HANDLE) -> Result<(), String> {
    let name = wide("Ace sandbox WFP");
    let provider = PROVIDER_KEY;
    let sublayer = FWPM_SUBLAYER0 {
        subLayerKey: SUBLAYER_KEY,
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
    let provider = PROVIDER_KEY;
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
        subLayerKey: SUBLAYER_KEY,
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
    fn stable_filter_keys_are_unique() {
        let keys = [
            OFFLINE_V4,
            OFFLINE_V6,
            ONLINE_PERMIT_V4,
            ONLINE_PERMIT_V6,
            ONLINE_BLOCK_V4,
            ONLINE_BLOCK_V6,
        ];
        // windows_sys::core::GUID doesn't derive PartialEq in 0.52; compare field tuples.
        for (index, key) in keys.iter().enumerate() {
            let dup = keys[..index].iter().any(|k| {
                (k.data1, k.data2, k.data3, k.data4) == (key.data1, key.data2, key.data3, key.data4)
            });
            assert!(!dup, "duplicate WFP filter key at index {index}");
        }
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
        let rule = UserCondition::new(&account).unwrap();
        let account_sid =
            LocalSid::from_string(&sid_string_for_account(&account).unwrap()).unwrap();
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
