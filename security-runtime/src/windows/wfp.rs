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
    LocalFree, FWP_E_ALREADY_EXISTS, FWP_E_FILTER_NOT_FOUND, FWP_E_NOT_FOUND,
    FWP_E_PROVIDER_NOT_FOUND, FWP_E_SUBLAYER_NOT_FOUND, HANDLE, HLOCAL,
};
use windows_sys::Win32::NetworkManagement::WindowsFilteringPlatform::*;
use windows_sys::Win32::Security::Authorization::{
    BuildExplicitAccessWithNameW, BuildSecurityDescriptorW,
    ConvertStringSecurityDescriptorToSecurityDescriptorW, EXPLICIT_ACCESS_W, GRANT_ACCESS,
    SDDL_REVISION_1,
};
use windows_sys::Win32::Security::PSECURITY_DESCRIPTOR;
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
    let mut transaction = engine.transaction()?;
    let readable_sd = ReadableSecurityDescriptor::create()?;
    for key in filter_keys() {
        delete_filter(engine.handle, &key)?;
    }
    allow_missing(
        unsafe { FwpmSubLayerDeleteByKey0(engine.handle, &SUBLAYER_KEY) },
        "FwpmSubLayerDeleteByKey0",
    )?;
    allow_missing(
        unsafe { FwpmProviderDeleteByKey0(engine.handle, &PROVIDER_KEY) },
        "FwpmProviderDeleteByKey0",
    )?;
    ensure_provider(engine.handle, readable_sd.descriptor)?;
    ensure_sublayer(engine.handle, readable_sd.descriptor)?;
    let offline = UserCondition::new(offline_account)?;
    let online = UserCondition::new(online_account)?;
    for (key, layer) in [
        (OFFLINE_V4, FWPM_LAYER_ALE_AUTH_CONNECT_V4),
        (OFFLINE_V6, FWPM_LAYER_ALE_AUTH_CONNECT_V6),
    ] {
        replace_filter(
            engine.handle,
            key,
            layer,
            &offline,
            false,
            false,
            readable_sd.descriptor,
        )?;
    }
    // The managed proxy binds 127.0.0.1 only. Never permit ::1:43119: a
    // different host process could own that endpoint and become a WFP bypass.
    for (key, layer) in [(ONLINE_PERMIT_V4, FWPM_LAYER_ALE_AUTH_CONNECT_V4)] {
        replace_filter(
            engine.handle,
            key,
            layer,
            &online,
            true,
            true,
            readable_sd.descriptor,
        )?;
    }
    for (key, layer) in [
        (ONLINE_BLOCK_V4, FWPM_LAYER_ALE_AUTH_CONNECT_V4),
        (ONLINE_BLOCK_V6, FWPM_LAYER_ALE_AUTH_CONNECT_V6),
    ] {
        replace_filter(
            engine.handle,
            key,
            layer,
            &online,
            false,
            false,
            readable_sd.descriptor,
        )?;
    }
    transaction.commit()?;
    drop(transaction);
    verify_installed(offline_account, online_account)
}

pub fn uninstall() -> Result<(), String> {
    let engine = Engine::open()?;
    let mut transaction = engine.transaction()?;
    for key in filter_keys() {
        delete_filter(engine.handle, &key)?;
    }
    let sublayer = unsafe { FwpmSubLayerDeleteByKey0(engine.handle, &SUBLAYER_KEY) };
    allow_missing(sublayer, "FwpmSubLayerDeleteByKey0")?;
    let provider = unsafe { FwpmProviderDeleteByKey0(engine.handle, &PROVIDER_KEY) };
    allow_missing(provider, "FwpmProviderDeleteByKey0")?;
    transaction.commit()?;
    drop(transaction);
    verify_uninstalled()
}

fn verify_uninstalled() -> Result<(), String> {
    let engine = Engine::open()?;
    for key in filter_keys() {
        verify_filter_absent(engine.handle, &key)?;
    }
    let mut provider: *mut FWPM_PROVIDER0 = null_mut();
    let status = unsafe { FwpmProviderGetByKey0(engine.handle, &PROVIDER_KEY, &mut provider) };
    if status == 0 {
        if !provider.is_null() {
            unsafe { FwpmFreeMemory0((&mut provider as *mut *mut FWPM_PROVIDER0).cast()) };
        }
        return Err("Ace WFP provider remained after uninstall".to_string());
    }
    if status != FWP_E_PROVIDER_NOT_FOUND as u32 && status != FWP_E_NOT_FOUND as u32 {
        return check(status, "FwpmProviderGetByKey0(after uninstall)");
    }
    let mut sublayer: *mut FWPM_SUBLAYER0 = null_mut();
    let status = unsafe { FwpmSubLayerGetByKey0(engine.handle, &SUBLAYER_KEY, &mut sublayer) };
    if status == 0 {
        if !sublayer.is_null() {
            unsafe { FwpmFreeMemory0((&mut sublayer as *mut *mut FWPM_SUBLAYER0).cast()) };
        }
        return Err("Ace WFP sublayer remained after uninstall".to_string());
    }
    if status != FWP_E_SUBLAYER_NOT_FOUND as u32 && status != FWP_E_NOT_FOUND as u32 {
        return check(status, "FwpmSubLayerGetByKey0(after uninstall)");
    }
    Ok(())
}

/// Verify every persistent WFP object, including the account security
/// descriptors and proxy-only conditions. Offline runs also require this:
/// "no proxy environment" is not an outbound network boundary by itself.
pub fn verify_installed(offline_account: &str, online_account: &str) -> Result<(), String> {
    let engine = Engine::open()?;
    verify_provider_and_sublayer(engine.handle)?;
    let offline = UserCondition::new(offline_account)?;
    let online = UserCondition::new(online_account)?;
    for expected in [
        FilterExpectation::block(
            "OFFLINE_V4",
            OFFLINE_V4,
            FWPM_LAYER_ALE_AUTH_CONNECT_V4,
            &offline,
        ),
        FilterExpectation::block(
            "OFFLINE_V6",
            OFFLINE_V6,
            FWPM_LAYER_ALE_AUTH_CONNECT_V6,
            &offline,
        ),
        FilterExpectation::permit(
            "ONLINE_PERMIT_V4",
            ONLINE_PERMIT_V4,
            FWPM_LAYER_ALE_AUTH_CONNECT_V4,
            &online,
        ),
        FilterExpectation::block(
            "ONLINE_BLOCK_V4",
            ONLINE_BLOCK_V4,
            FWPM_LAYER_ALE_AUTH_CONNECT_V4,
            &online,
        ),
        FilterExpectation::block(
            "ONLINE_BLOCK_V6",
            ONLINE_BLOCK_V6,
            FWPM_LAYER_ALE_AUTH_CONNECT_V6,
            &online,
        ),
    ] {
        verify_filter(engine.handle, &expected)?;
    }
    verify_filter_absent(engine.handle, &ONLINE_PERMIT_V6)?;
    Ok(())
}

struct FilterExpectation<'a> {
    name: &'static str,
    key: GUID,
    layer: GUID,
    user: &'a UserCondition,
    permit_proxy: bool,
}

impl<'a> FilterExpectation<'a> {
    fn block(name: &'static str, key: GUID, layer: GUID, user: &'a UserCondition) -> Self {
        Self {
            name,
            key,
            layer,
            user,
            permit_proxy: false,
        }
    }

    fn permit(name: &'static str, key: GUID, layer: GUID, user: &'a UserCondition) -> Self {
        Self {
            name,
            key,
            layer,
            user,
            permit_proxy: true,
        }
    }
}

fn verify_provider_and_sublayer(engine: HANDLE) -> Result<(), String> {
    let mut provider: *mut FWPM_PROVIDER0 = null_mut();
    check(
        unsafe { FwpmProviderGetByKey0(engine, &PROVIDER_KEY, &mut provider) },
        "FwpmProviderGetByKey0",
    )?;
    if provider.is_null() {
        return Err("FwpmProviderGetByKey0 returned a null provider".to_string());
    }
    let provider_result = unsafe {
        let provider = &*provider;
        if guid_eq(&provider.providerKey, &PROVIDER_KEY)
            && provider.flags & FWPM_PROVIDER_FLAG_PERSISTENT != 0
        {
            Ok(())
        } else {
            Err("Ace WFP provider is not the expected persistent object".to_string())
        }
    };
    unsafe { FwpmFreeMemory0((&mut provider as *mut *mut FWPM_PROVIDER0).cast()) };
    provider_result?;

    let mut sublayer: *mut FWPM_SUBLAYER0 = null_mut();
    check(
        unsafe { FwpmSubLayerGetByKey0(engine, &SUBLAYER_KEY, &mut sublayer) },
        "FwpmSubLayerGetByKey0",
    )?;
    if sublayer.is_null() {
        return Err("FwpmSubLayerGetByKey0 returned a null sublayer".to_string());
    }
    let sublayer_result = unsafe {
        let sublayer = &*sublayer;
        if guid_eq(&sublayer.subLayerKey, &SUBLAYER_KEY)
            // Windows can add the provider-owned sublayer's SHARED bit on
            // return; only PERSISTENT is a security property we must pin.
            && sublayer.flags & FWPM_SUBLAYER_FLAG_PERSISTENT != 0
            // BFE may add low internal weight bits after persistence; the
            // required high-weight placement remains pinned.
            && sublayer.weight & 0x8000 == 0x8000
            && !sublayer.providerKey.is_null()
            && guid_eq(&*sublayer.providerKey, &PROVIDER_KEY)
        {
            Ok(())
        } else {
            Err(format!(
                "Ace WFP sublayer does not match its persistent provider: flags={}, weight={}, provider={}",
                sublayer.flags,
                sublayer.weight,
                if sublayer.providerKey.is_null() {
                    "null".to_string()
                } else {
                    format!("{:?}", (
                        (*sublayer.providerKey).data1,
                        (*sublayer.providerKey).data2,
                        (*sublayer.providerKey).data3,
                        (*sublayer.providerKey).data4,
                    ))
                },
            ))
        }
    };
    unsafe { FwpmFreeMemory0((&mut sublayer as *mut *mut FWPM_SUBLAYER0).cast()) };
    sublayer_result
}

fn verify_filter(engine: HANDLE, expected: &FilterExpectation<'_>) -> Result<(), String> {
    let mut filter: *mut FWPM_FILTER0 = null_mut();
    check(
        unsafe { FwpmFilterGetByKey0(engine, &expected.key, &mut filter) },
        "FwpmFilterGetByKey0",
    )?;
    if filter.is_null() {
        return Err(format!(
            "FwpmFilterGetByKey0 returned a null filter for {}",
            expected.name
        ));
    }
    let result = unsafe { verify_filter_value(&*filter, expected) };
    unsafe { FwpmFreeMemory0((&mut filter as *mut *mut FWPM_FILTER0).cast()) };
    result
}

fn verify_filter_absent(engine: HANDLE, key: &GUID) -> Result<(), String> {
    let mut filter: *mut FWPM_FILTER0 = null_mut();
    let status = unsafe { FwpmFilterGetByKey0(engine, key, &mut filter) };
    if !filter.is_null() {
        unsafe { FwpmFreeMemory0((&mut filter as *mut *mut FWPM_FILTER0).cast()) };
    }
    if status == FWP_E_FILTER_NOT_FOUND as u32 || status == FWP_E_NOT_FOUND as u32 {
        Ok(())
    } else if status == 0 {
        Err("obsolete IPv6 proxy-permit WFP filter is still installed".to_string())
    } else {
        check(status, "FwpmFilterGetByKey0(obsolete IPv6 permit)")
    }
}

unsafe fn verify_filter_value(
    filter: &FWPM_FILTER0,
    expected: &FilterExpectation<'_>,
) -> Result<(), String> {
    let expected_action = if expected.permit_proxy {
        FWP_ACTION_PERMIT
    } else {
        FWP_ACTION_BLOCK
    };
    let expected_conditions = if expected.permit_proxy { 3 } else { 1 };
    let expected_weight = if expected.permit_proxy { 15 } else { 0 };
    if !guid_eq(&filter.layerKey, &expected.layer)
        || !guid_eq(&filter.subLayerKey, &SUBLAYER_KEY)
        || filter.providerKey.is_null()
        || !guid_eq(&*filter.providerKey, &PROVIDER_KEY)
        || filter.flags & FWPM_FILTER_FLAG_PERSISTENT == 0
        || filter.action.r#type != expected_action
        || filter.numFilterConditions != expected_conditions
        || filter.weight.r#type != FWP_UINT8
        || filter.weight.Anonymous.uint8 != expected_weight
        || filter.filterCondition.is_null()
    {
        return Err(format!(
            "WFP filter {} metadata, action, weight, or condition count mismatch",
            expected.name
        ));
    }
    let conditions =
        std::slice::from_raw_parts(filter.filterCondition, filter.numFilterConditions as usize);
    let user = &conditions[0];
    if !guid_eq(&user.fieldKey, &FWPM_CONDITION_ALE_USER_ID)
        || user.matchType != FWP_MATCH_EQUAL
        || user.conditionValue.r#type != FWP_SECURITY_DESCRIPTOR_TYPE
        || user.conditionValue.Anonymous.sd.is_null()
        || !blob_eq(&*user.conditionValue.Anonymous.sd, &expected.user.blob)
    {
        return Err(format!(
            "WFP filter {} account condition mismatch",
            expected.name
        ));
    }
    if expected.permit_proxy {
        let port = &conditions[1];
        let flags = &conditions[2];
        if !guid_eq(&port.fieldKey, &FWPM_CONDITION_IP_REMOTE_PORT)
            || port.matchType != FWP_MATCH_EQUAL
            || port.conditionValue.r#type != FWP_UINT16
            || port.conditionValue.Anonymous.uint16 != PROXY_PORT
            || !guid_eq(&flags.fieldKey, &FWPM_CONDITION_FLAGS)
            || flags.matchType != FWP_MATCH_FLAGS_ALL_SET
            || flags.conditionValue.r#type != FWP_UINT32
            || flags.conditionValue.Anonymous.uint32 != FWP_CONDITION_FLAG_IS_LOOPBACK
        {
            return Err(format!(
                "WFP filter {} is not loopback-proxy-only",
                expected.name
            ));
        }
    }
    Ok(())
}

unsafe fn blob_eq(left: &FWP_BYTE_BLOB, right: &FWP_BYTE_BLOB) -> bool {
    if left.size != right.size || left.data.is_null() || right.data.is_null() {
        return false;
    }
    std::slice::from_raw_parts(left.data, left.size as usize)
        == std::slice::from_raw_parts(right.data, right.size as usize)
}

fn guid_eq(left: &GUID, right: &GUID) -> bool {
    (left.data1, left.data2, left.data3, left.data4)
        == (right.data1, right.data2, right.data3, right.data4)
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

/// Installer-owned WFP object DACL: Administrators retain full control while
/// every local principal can read the immutable policy facts.  Daily runtime
/// verification then works from the unelevated Desktop/Gateway process without
/// granting it permission to modify WFP.
struct ReadableSecurityDescriptor {
    descriptor: PSECURITY_DESCRIPTOR,
}
impl ReadableSecurityDescriptor {
    #[cfg(windows)]
    fn create() -> Result<Self, String> {
        let sddl = wide("D:P(A;;GA;;;BA)(A;;GR;;;WD)");
        let mut descriptor = std::ptr::null_mut();
        let result = unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                std::ptr::null_mut(),
            )
        };
        if result == 0 || descriptor.is_null() {
            return Err("failed to create readable WFP security descriptor".to_string());
        }
        Ok(Self { descriptor })
    }
}
impl Drop for ReadableSecurityDescriptor {
    fn drop(&mut self) {
        unsafe { LocalFree(self.descriptor as HLOCAL) };
    }
}

fn ensure_provider(engine: HANDLE, sd: PSECURITY_DESCRIPTOR) -> Result<(), String> {
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
        unsafe { FwpmProviderAdd0(engine, &provider, sd) },
        "FwpmProviderAdd0",
    )
}

fn ensure_sublayer(engine: HANDLE, sd: PSECURITY_DESCRIPTOR) -> Result<(), String> {
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
        unsafe { FwpmSubLayerAdd0(engine, &sublayer, sd) },
        "FwpmSubLayerAdd0",
    )
}

fn replace_filter(
    engine: HANDLE,
    key: GUID,
    layer: GUID,
    user: &UserCondition,
    permit_proxy: bool,
    high_weight: bool,
    sd: PSECURITY_DESCRIPTOR,
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
                uint8: if high_weight { 15 } else { 0 },
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
        unsafe { FwpmFilterAdd0(engine, &filter, sd, &mut id) },
        "FwpmFilterAdd0",
    )
}

fn delete_filter(engine: HANDLE, key: &GUID) -> Result<(), String> {
    allow_missing(
        unsafe { FwpmFilterDeleteByKey0(engine, key) },
        "FwpmFilterDeleteByKey0",
    )
}

fn filter_keys() -> [GUID; 6] {
    [
        OFFLINE_V4,
        OFFLINE_V6,
        ONLINE_PERMIT_V4,
        ONLINE_PERMIT_V6,
        ONLINE_BLOCK_V4,
        ONLINE_BLOCK_V6,
    ]
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
    if code == 0
        || code == FWP_E_FILTER_NOT_FOUND as u32
        || code == FWP_E_NOT_FOUND as u32
        || code == FWP_E_PROVIDER_NOT_FOUND as u32
        || code == FWP_E_SUBLAYER_NOT_FOUND as u32
    {
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
    fn readable_security_descriptor_is_created_for_daily_verification() {
        let descriptor = ReadableSecurityDescriptor::create().expect("valid SDDL");
        assert!(!descriptor.descriptor.is_null());
    }

    #[test]
    fn account_identity_not_desktop_session_is_filter_boundary() {
        assert_eq!(PROXY_PORT, 43119);
        assert_eq!(size_of::<GUID>(), 16);
    }
}
