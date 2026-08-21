use std::collections::{BTreeMap, BTreeSet};

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use subtle::ConstantTimeEq;

pub const PROTOCOL_VERSION: u16 = 3;
pub const MAX_REQUEST_FRAME_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_STDIN_BYTES: usize = 1024 * 1024;
pub const MAX_ENV_BYTES: usize = 256 * 1024;
pub const MAX_HOME_FILE_BYTES: usize = 1024 * 1024;
pub const MAX_HOME_TOTAL_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_HOME_FILES: usize = 64;
pub const MAX_RESPONSE_FRAME_BYTES: usize = 128 * 1024;
pub const MAX_OUTPUT_CHUNK_BYTES: usize = 64 * 1024;
pub const DEFAULT_MAX_OUTPUT_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_OUTPUT_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_STDIO_INPUT_FRAME_BYTES: usize = 1024 * 1024;
pub const MAX_STDIO_INPUT_BYTES: usize = 16 * 1024 * 1024;
pub const READY_CAPABILITIES: [&str; 7] = [
    "deny_read_glob_v1",
    "stdin_once",
    "stream_output",
    "duplex_stdio_v1",
    "stdin_bidirectional",
    "readonly_roots",
    "full_disk_read",
];
const STDIO_MAC_CONTEXT: &[u8] = b"ace-runtime-stdio-v1\0";

/// Stable error codes for the managed-network layer (spec §13).
///
/// These are the only codes the network subsystem is allowed to surface; the
/// host can rely on them to decide whether to retry, reconfigure policy, or
/// fail the sandbox. Previously `policy.resolve_allowed` / `connector::connect`
/// returned free-form `String` errors that the platform backends then flattened
/// into `sandbox_unavailable` / `sandbox_denied`, hiding the real reason (N8).
#[derive(Clone, Copy, Debug, Serialize, PartialEq, Eq)]
pub enum NetworkErrorCode {
    #[serde(rename = "policy_denied")]
    PolicyDenied,
    #[serde(rename = "network_unavailable")]
    NetworkUnavailable,
    #[serde(rename = "sandbox_denied")]
    SandboxDenied,
}

impl NetworkErrorCode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::PolicyDenied => "policy_denied",
            Self::NetworkUnavailable => "network_unavailable",
            Self::SandboxDenied => "sandbox_denied",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct NetworkRule {
    pub host: String,
    pub port: u16,
    pub protocol: String,
    pub allow: bool,
    #[serde(default)]
    pub allow_private: bool,
    #[serde(default = "default_escalatable")]
    pub escalatable: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum FilesystemGlobAccess {
    DenyRead,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FilesystemGlobRule {
    pub root: String,
    pub pattern: String,
    pub access: FilesystemGlobAccess,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RequestEnvelope {
    pub version: u16,
    pub token: String,
    pub nonce: String,
    pub request: RuntimeRequest,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
#[serde(deny_unknown_fields)]
pub enum RuntimeRequest {
    ClassifyShell {
        shell_kind: String,
        executable: String,
        raw_command: String,
    },
    Run {
        command: Vec<String>,
        cwd: String,
        #[serde(default)]
        writable_roots: Vec<String>,
        #[serde(default)]
        readable_roots: Vec<String>,
        #[serde(default)]
        readonly_roots: Vec<String>,
        #[serde(default)]
        denied_roots: Vec<String>,
        #[serde(default)]
        filesystem_globs: Vec<FilesystemGlobRule>,
        #[serde(default)]
        full_disk_read: bool,
        #[serde(default)]
        network_enabled: bool,
        #[serde(default)]
        network_rules: Vec<NetworkRule>,
        #[serde(default)]
        allow_local_binding: bool,
        #[serde(default = "default_max_output_bytes")]
        max_output_bytes: usize,
        #[serde(default)]
        stdin_b64: Option<String>,
        #[serde(default)]
        env_overrides: BTreeMap<String, String>,
        #[serde(default)]
        home_files: BTreeMap<String, String>,
    },
    RunStdio {
        command: Vec<String>,
        cwd: String,
        #[serde(default)]
        writable_roots: Vec<String>,
        #[serde(default)]
        readable_roots: Vec<String>,
        #[serde(default)]
        denied_roots: Vec<String>,
        #[serde(default)]
        filesystem_globs: Vec<FilesystemGlobRule>,
        #[serde(default)]
        network_enabled: bool,
        #[serde(default)]
        network_rules: Vec<NetworkRule>,
        #[serde(default)]
        allow_local_binding: bool,
        #[serde(default = "default_max_output_bytes")]
        max_output_bytes: usize,
        #[serde(default = "default_max_stdio_input_bytes")]
        max_input_bytes: usize,
        #[serde(default)]
        env_overrides: BTreeMap<String, String>,
    },
    InteractiveOpen {
        command: Vec<String>,
        cwd: String,
        #[serde(default)]
        writable_roots: Vec<String>,
        #[serde(default)]
        readable_roots: Vec<String>,
        #[serde(default)]
        readonly_roots: Vec<String>,
        #[serde(default)]
        denied_roots: Vec<String>,
        #[serde(default)]
        full_disk_read: bool,
        #[serde(default)]
        network_enabled: bool,
        #[serde(default)]
        network_rules: Vec<NetworkRule>,
        #[serde(default)]
        allow_local_binding: bool,
        #[serde(default = "default_max_output_bytes")]
        max_output_bytes: usize,
        #[serde(default)]
        env_overrides: BTreeMap<String, String>,
        #[serde(default)]
        home_files: BTreeMap<String, String>,
    },
    InteractiveWrite {
        data_b64: String,
    },
    InteractiveClose,
}

#[derive(Debug)]
pub enum RuntimeControl {
    Write(Vec<u8>),
    Close,
}

#[derive(Debug, Serialize)]
pub struct ReadyFrame {
    #[serde(rename = "type")]
    pub frame_type: &'static str,
    pub version: u16,
    pub capabilities: [&'static str; 7],
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StdioInputFrame {
    pub version: u16,
    pub nonce: String,
    pub seq: u64,
    #[serde(rename = "type")]
    pub frame_type: String,
    #[serde(default)]
    pub data_b64: String,
    pub mac: String,
}

#[derive(Debug)]
pub enum StdioInputMessage {
    Data(Vec<u8>),
    Close,
    Abort,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum RuntimeEvent {
    Classified {
        version: u16,
        nonce: String,
        seq: u64,
        classification: crate::shell::ShellClassification,
    },
    Started {
        version: u16,
        nonce: String,
        seq: u64,
        pid: Option<u32>,
        capabilities: RuntimeCapabilities,
    },
    Stdout {
        version: u16,
        nonce: String,
        seq: u64,
        data_b64: String,
    },
    Stderr {
        version: u16,
        nonce: String,
        seq: u64,
        data_b64: String,
    },
    Completed {
        version: u16,
        nonce: String,
        seq: u64,
        exit_code: i32,
    },
    Error {
        version: u16,
        nonce: String,
        seq: u64,
        code: &'static str,
        message: String,
    },
}

#[derive(Debug)]
pub enum RuntimeMessage {
    Classified(crate::shell::ShellClassification),
    Started {
        pid: Option<u32>,
        capabilities: RuntimeCapabilities,
    },
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
    Completed(i32),
    Error {
        code: &'static str,
        message: String,
    },
}

#[derive(Debug, PartialEq, Eq)]
pub struct ProcessInput {
    pub stdin: Option<Vec<u8>>,
    pub env_overrides: BTreeMap<String, String>,
    pub home_files: BTreeMap<String, Vec<u8>>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct InputValidationError {
    pub code: &'static str,
    pub message: &'static str,
}

#[derive(Clone, Debug, Serialize)]
pub struct RuntimeCapabilities {
    pub backend: &'static str,
    pub filesystem_sandbox: bool,
    pub process_tree_cleanup: bool,
    pub managed_network: bool,
    pub full_disk_read: bool,
    pub system_bwrap: bool,
    pub bundled_bwrap: bool,
    pub wsl_version: Option<u8>,
    // Spec §13 capability matrix: fine-grained fields so the host can disable only the
    // profile that depends on a missing capability (e.g. WFP absent ⇒ managed-network
    // unavailable, while the offline filesystem sandbox stays usable) instead of
    // all-or-nothing trial-and-error.
    pub local_binding_control: bool,
    pub explicit_handle_inheritance: bool,
    pub windows_restricted_token: bool,
    pub windows_acl: bool,
    pub windows_job: bool,
    pub windows_wfp: bool,
}

fn default_max_output_bytes() -> usize {
    DEFAULT_MAX_OUTPUT_BYTES
}

fn default_max_stdio_input_bytes() -> usize {
    MAX_STDIO_INPUT_BYTES
}

fn default_escalatable() -> bool {
    true
}

#[cfg(test)]
pub fn validate_process_inputs(
    stdin_b64: Option<&str>,
    env_overrides: &BTreeMap<String, String>,
) -> Result<ProcessInput, InputValidationError> {
    validate_process_inputs_with_home_files(stdin_b64, env_overrides, &BTreeMap::new())
}

pub fn validate_process_inputs_with_home_files(
    stdin_b64: Option<&str>,
    env_overrides: &BTreeMap<String, String>,
    home_files: &BTreeMap<String, String>,
) -> Result<ProcessInput, InputValidationError> {
    let stdin = match stdin_b64 {
        Some(encoded) => {
            let value = BASE64_STANDARD
                .decode(encoded)
                .map_err(|_| InputValidationError {
                    code: "sandbox_denied",
                    message: "invalid stdin payload",
                })?;
            if value.len() > MAX_STDIN_BYTES {
                return Err(InputValidationError {
                    code: "sandbox_denied",
                    message: "stdin payload exceeds the size limit",
                });
            }
            Some(value)
        }
        None => None,
    };

    let mut encoded_size = 0usize;
    let mut normalized_names = BTreeSet::new();
    for (name, value) in env_overrides {
        if !valid_environment_name(name) || value.contains('\0') {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "invalid environment entry",
            });
        }
        let normalized = name.to_ascii_uppercase();
        if !normalized_names.insert(normalized.clone()) || disallowed_environment_name(&normalized)
        {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "disallowed environment entry",
            });
        }
        encoded_size = encoded_size
            .checked_add(name.len())
            .and_then(|size| size.checked_add(value.len()))
            .ok_or(InputValidationError {
                code: "sandbox_denied",
                message: "environment overrides exceed the size limit",
            })?;
        if encoded_size > MAX_ENV_BYTES {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "environment overrides exceed the size limit",
            });
        }
    }

    if home_files.len() > MAX_HOME_FILES {
        return Err(InputValidationError {
            code: "sandbox_denied",
            message: "projected HOME has too many files",
        });
    }
    let mut decoded_home_files = BTreeMap::new();
    let mut total_home_bytes = 0usize;
    for (relative_path, encoded) in home_files {
        let components: Vec<&str> = relative_path.split('/').collect();
        if relative_path.is_empty()
            || relative_path.starts_with('/')
            || relative_path.contains('\\')
            || relative_path.contains(':')
            || components
                .iter()
                .any(|part| part.is_empty() || *part == "." || *part == "..")
        {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "projected HOME path must be relative",
            });
        }
        let decoded = BASE64_STANDARD
            .decode(encoded)
            .map_err(|_| InputValidationError {
                code: "sandbox_denied",
                message: "invalid projected HOME file encoding",
            })?;
        if decoded.len() > MAX_HOME_FILE_BYTES {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "projected HOME file exceeds the size limit",
            });
        }
        total_home_bytes =
            total_home_bytes
                .checked_add(decoded.len())
                .ok_or(InputValidationError {
                    code: "sandbox_denied",
                    message: "projected HOME exceeds the size limit",
                })?;
        if total_home_bytes > MAX_HOME_TOTAL_BYTES {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "projected HOME exceeds the size limit",
            });
        }
        decoded_home_files.insert(relative_path.clone(), decoded);
    }

    Ok(ProcessInput {
        stdin,
        env_overrides: env_overrides.clone(),
        home_files: decoded_home_files,
    })
}

/// Validate request-wide resource budgets before any platform backend starts.
/// Platform modules enforce OS policy; this boundary owns the protocol-level
/// memory and output limits so malformed requests cannot reach them with an
/// unbounded command, path list, or capture budget.
static INTERACTIVE_EMPTY_GLOBS: Vec<FilesystemGlobRule> = Vec::new();

pub fn validate_request_limits(request: &RuntimeRequest) -> Result<(), InputValidationError> {
    let (
        command,
        cwd,
        writable_roots,
        readable_roots,
        denied_roots,
        filesystem_globs,
        network_rules,
        max_output_bytes,
        max_input_bytes,
        _stdin_b64,
        _env_overrides,
    ) = match request {
        RuntimeRequest::ClassifyShell {
            shell_kind,
            executable,
            raw_command,
        } => {
            if shell_kind.len() > 64
                || executable.len() > 16 * 1024
                || raw_command.len() > MAX_REQUEST_FRAME_BYTES
                || shell_kind.contains('\0')
                || executable.contains('\0')
                || raw_command.contains('\0')
            {
                return Err(InputValidationError {
                    code: "sandbox_denied",
                    message: "classification request exceeds the size limit",
                });
            }
            return Ok(());
        }
        RuntimeRequest::Run {
            command,
            cwd,
            writable_roots,
            readable_roots,
            denied_roots,
            filesystem_globs,
            network_rules,
            max_output_bytes,
            stdin_b64,
            env_overrides,
            ..
        } => (
            command,
            cwd,
            writable_roots,
            readable_roots,
            denied_roots,
            filesystem_globs,
            network_rules,
            max_output_bytes,
            &0,
            stdin_b64,
            env_overrides,
        ),
        RuntimeRequest::RunStdio {
            command,
            cwd,
            writable_roots,
            readable_roots,
            denied_roots,
            filesystem_globs,
            network_rules,
            max_output_bytes,
            max_input_bytes,
            env_overrides,
            ..
        } => (
            command,
            cwd,
            writable_roots,
            readable_roots,
            denied_roots,
            filesystem_globs,
            network_rules,
            max_output_bytes,
            max_input_bytes,
            &None,
            env_overrides,
        ),
        RuntimeRequest::InteractiveOpen {
            command,
            cwd,
            writable_roots,
            readable_roots,
            denied_roots,
            network_rules,
            max_output_bytes,
            env_overrides,
            ..
        } => (
            command,
            cwd,
            writable_roots,
            readable_roots,
            denied_roots,
            &INTERACTIVE_EMPTY_GLOBS,
            network_rules,
            max_output_bytes,
            &0,
            &None,
            env_overrides,
        ),
        RuntimeRequest::InteractiveWrite { data_b64 } => {
            // The decoded-size ceiling is enforced where the payload is
            // consumed; here we only bound the frame itself.
            if data_b64.len() > MAX_REQUEST_FRAME_BYTES {
                return Err(InputValidationError {
                    code: "sandbox_denied",
                    message: "interactive write payload exceeds the size limit",
                });
            }
            return Ok(());
        }
        RuntimeRequest::InteractiveClose => return Ok(()),
    };

    let command_bytes = command
        .iter()
        .try_fold(0usize, |total, value| total.checked_add(value.len()));
    let bounded_path_list = |values: &[String]| {
        values
            .iter()
            .all(|value| !value.is_empty() && value.len() <= 16 * 1024 && !value.contains('\0'))
    };
    if command.len() > 256
        || command
            .iter()
            .any(|value| value.is_empty() || value.len() > 16 * 1024 || value.contains('\0'))
        || !matches!(command_bytes, Some(total) if total <= MAX_REQUEST_FRAME_BYTES)
        || cwd.is_empty()
        || cwd.len() > 16 * 1024
        || cwd.contains('\0')
        || *max_output_bytes == 0
        || *max_output_bytes > MAX_OUTPUT_BYTES
        || *max_input_bytes > MAX_STDIO_INPUT_BYTES
        || writable_roots.len() + readable_roots.len() + denied_roots.len() > 256
        || !bounded_path_list(writable_roots)
        || !bounded_path_list(readable_roots)
        || !bounded_path_list(denied_roots)
        || filesystem_globs.len() > 256
        || filesystem_globs.iter().any(|rule| {
            rule.root.is_empty()
                || rule.root.len() > 16 * 1024
                || rule.root.contains('\0')
                || rule.pattern.is_empty()
                || rule.pattern.len() > 16 * 1024
                || rule.pattern.contains('\0')
        })
        || network_rules.len() > 256
    {
        return Err(InputValidationError {
            code: "sandbox_denied",
            message: "runtime request exceeds a resource limit",
        });
    }
    Ok(())
}

fn disallowed_environment_name(normalized: &str) -> bool {
    matches!(
        normalized,
        "ACE_SANDBOX"
            | "ALL_PROXY"
            | "BASH_ENV"
            | "COMSPEC"
            | "ENV"
            | "GIT_CONFIG_GLOBAL"
            | "HOME"
            | "HOMEDRIVE"
            | "HOMEPATH"
            | "HTTP_PROXY"
            | "HTTPS_PROXY"
            | "NODE_OPTIONS"
            | "NO_PROXY"
            | "PATH"
            | "PERL5OPT"
            | "PYTHONHOME"
            | "PYTHONPATH"
            | "PYTHONSTARTUP"
            | "RUBYOPT"
            | "SYSTEMROOT"
            | "TEMP"
            | "TMP"
            | "TMPDIR"
            | "USERNAME"
            | "USERPROFILE"
            | "WINDIR"
    ) || normalized.starts_with("ACE_SECURITY_")
        || normalized.starts_with("ACE_BUNDLED_")
        || normalized.starts_with("DYLD_")
        || normalized.starts_with("LD_")
}

pub fn validate_stdio_input_frame(
    frame: StdioInputFrame,
    startup_token: &str,
    expected_nonce: &str,
    expected_seq: u64,
    remaining_bytes: usize,
) -> Result<(StdioInputMessage, usize), InputValidationError> {
    if frame.version != PROTOCOL_VERSION
        || frame.seq != expected_seq
        || !bool::from(frame.nonce.as_bytes().ct_eq(expected_nonce.as_bytes()))
    {
        return Err(InputValidationError {
            code: "runtime_protocol_mismatch",
            message: "invalid stdio input frame identity",
        });
    }
    if startup_token.len() < 32
        || !valid_stdio_mac(
            startup_token,
            &frame.nonce,
            frame.seq,
            &frame.frame_type,
            &frame.data_b64,
            &frame.mac,
        )
    {
        return Err(InputValidationError {
            code: "runtime_protocol_mismatch",
            message: "invalid stdio input frame authentication",
        });
    }
    match frame.frame_type.as_str() {
        "stdin" => {
            let value =
                BASE64_STANDARD
                    .decode(&frame.data_b64)
                    .map_err(|_| InputValidationError {
                        code: "runtime_protocol_mismatch",
                        message: "invalid stdio input encoding",
                    })?;
            if value.is_empty()
                || value.len() > MAX_STDIO_INPUT_FRAME_BYTES
                || value.len() > remaining_bytes
            {
                return Err(InputValidationError {
                    code: "sandbox_denied",
                    message: "stdio input exceeds the configured limit",
                });
            }
            let retained = value.len();
            Ok((StdioInputMessage::Data(value), retained))
        }
        "stdin_close" if frame.data_b64.is_empty() => Ok((StdioInputMessage::Close, 0)),
        _ => Err(InputValidationError {
            code: "runtime_protocol_mismatch",
            message: "invalid stdio input frame type",
        }),
    }
}

fn valid_stdio_mac(
    startup_token: &str,
    nonce: &str,
    seq: u64,
    frame_type: &str,
    data_b64: &str,
    encoded_mac: &str,
) -> bool {
    let Ok(expected) = hex_decode(encoded_mac) else {
        return false;
    };
    let Ok(mut mac) = Hmac::<Sha256>::new_from_slice(startup_token.as_bytes()) else {
        return false;
    };
    mac.update(STDIO_MAC_CONTEXT);
    mac.update(nonce.as_bytes());
    mac.update(b"\0");
    mac.update(seq.to_string().as_bytes());
    mac.update(b"\0");
    mac.update(frame_type.as_bytes());
    mac.update(b"\0");
    mac.update(data_b64.as_bytes());
    mac.verify_slice(&expected).is_ok()
}

fn hex_decode(value: &str) -> Result<Vec<u8>, ()> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(());
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let text = std::str::from_utf8(pair).map_err(|_| ())?;
            u8::from_str_radix(text, 16).map_err(|_| ())
        })
        .collect()
}

fn valid_environment_name(name: &str) -> bool {
    let mut bytes = name.bytes();
    matches!(bytes.next(), Some(b'A'..=b'Z' | b'a'..=b'z' | b'_'))
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

#[cfg(test)]
mod tests {
    use super::{
        validate_process_inputs, validate_request_limits, validate_stdio_input_frame,
        FilesystemGlobAccess, RequestEnvelope, RuntimeCapabilities, RuntimeEvent, RuntimeRequest,
        StdioInputFrame, StdioInputMessage, DEFAULT_MAX_OUTPUT_BYTES, MAX_ENV_BYTES,
        MAX_OUTPUT_BYTES, MAX_OUTPUT_CHUNK_BYTES, MAX_REQUEST_FRAME_BYTES,
        MAX_RESPONSE_FRAME_BYTES, MAX_STDIN_BYTES, PROTOCOL_VERSION, READY_CAPABILITIES,
        STDIO_MAC_CONTEXT,
    };
    use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
    use base64::Engine;
    use hmac::{Hmac, Mac};
    use sha2::Sha256;
    use std::collections::BTreeMap;

    fn capabilities() -> RuntimeCapabilities {
        RuntimeCapabilities {
            backend: "test",
            filesystem_sandbox: true,
            process_tree_cleanup: true,
            managed_network: false,
            full_disk_read: false,
            system_bwrap: false,
            bundled_bwrap: false,
            wsl_version: None,
            local_binding_control: false,
            explicit_handle_inheritance: false,
            windows_restricted_token: false,
            windows_acl: false,
            windows_job: false,
            windows_wfp: false,
        }
    }

    #[test]
    fn protocol_limits_match_the_public_contract() {
        assert_eq!(PROTOCOL_VERSION, 3);
        assert_eq!(MAX_REQUEST_FRAME_BYTES, 2 * 1024 * 1024);
        assert_eq!(MAX_STDIN_BYTES, 1024 * 1024);
        assert_eq!(MAX_ENV_BYTES, 256 * 1024);
        assert_eq!(MAX_RESPONSE_FRAME_BYTES, 128 * 1024);
        assert_eq!(MAX_OUTPUT_CHUNK_BYTES, 64 * 1024);
        assert!(READY_CAPABILITIES.contains(&"deny_read_glob_v1"));
        assert!(READY_CAPABILITIES.contains(&"duplex_stdio_v1"));
    }

    #[test]
    fn run_protocol_binds_only_deny_read_glob_rules() {
        let value = serde_json::json!({
            "version": PROTOCOL_VERSION,
            "token": "token",
            "nonce": "nonce",
            "request": {
                "op": "run",
                "command": ["true"],
                "cwd": "/workspace",
                "filesystem_globs": [{
                    "root": "/workspace",
                    "pattern": "**/*.pem",
                    "access": "deny_read"
                }]
            }
        });
        let envelope: RequestEnvelope = serde_json::from_value(value.clone()).unwrap();
        let RuntimeRequest::Run {
            filesystem_globs, ..
        } = envelope.request
        else {
            panic!("expected run request");
        };
        assert_eq!(filesystem_globs.len(), 1);
        assert_eq!(filesystem_globs[0].root, "/workspace");
        assert_eq!(filesystem_globs[0].pattern, "**/*.pem");
        assert_eq!(filesystem_globs[0].access, FilesystemGlobAccess::DenyRead);

        let mut invalid = value;
        invalid["request"]["filesystem_globs"][0]["access"] = serde_json::json!("allow_read");
        assert!(serde_json::from_value::<RequestEnvelope>(invalid).is_err());
    }

    #[test]
    fn request_budget_and_unknown_fields_fail_closed() {
        let value = serde_json::json!({
            "version": PROTOCOL_VERSION,
            "token": "token",
            "nonce": "nonce",
            "request": {
                "op": "run",
                "command": ["true"],
                "cwd": "/workspace",
                "max_output_bytes": DEFAULT_MAX_OUTPUT_BYTES
            }
        });
        let envelope: RequestEnvelope = serde_json::from_value(value.clone()).unwrap();
        assert!(validate_request_limits(&envelope.request).is_ok());

        let mut oversized = value.clone();
        oversized["request"]["max_output_bytes"] = serde_json::json!(MAX_OUTPUT_BYTES + 1);
        let parsed: RequestEnvelope = serde_json::from_value(oversized).unwrap();
        assert!(validate_request_limits(&parsed.request).is_err());

        let mut unknown = value;
        unknown["request"]["untrusted_field"] = serde_json::json!(true);
        assert!(serde_json::from_value::<RequestEnvelope>(unknown).is_err());
    }

    #[test]
    fn arbitrary_stdin_bytes_round_trip_through_base64() {
        let bytes = [0, 1, 2, 127, 128, 254, 255];
        let encoded = BASE64_STANDARD.encode(bytes);
        let input = validate_process_inputs(Some(&encoded), &BTreeMap::new()).unwrap();
        assert_eq!(input.stdin.as_deref(), Some(bytes.as_slice()));
    }

    #[test]
    fn process_input_limits_and_reserved_environment_are_rejected() {
        let oversized_stdin = BASE64_STANDARD.encode(vec![0; MAX_STDIN_BYTES + 1]);
        assert_eq!(
            validate_process_inputs(Some(&oversized_stdin), &BTreeMap::new())
                .unwrap_err()
                .message,
            "stdin payload exceeds the size limit"
        );

        for name in [
            "INVALID-NAME",
            "ACE_SANDBOX",
            "HTTP_PROXY",
            "ace_security_runtime_token",
            "ACE_BUNDLED_BWRAP",
            "PATH",
            "HOME",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "BASH_ENV",
            "NODE_OPTIONS",
            "PYTHONSTARTUP",
        ] {
            let environment = BTreeMap::from([(name.to_string(), "value".to_string())]);
            assert!(validate_process_inputs(None, &environment).is_err());
        }
        let duplicate_environment = BTreeMap::from([
            ("SAFE_NAME".to_string(), "one".to_string()),
            ("safe_name".to_string(), "two".to_string()),
        ]);
        assert!(validate_process_inputs(None, &duplicate_environment).is_err());

        let oversized_environment =
            BTreeMap::from([("LARGE".to_string(), "x".repeat(MAX_ENV_BYTES))]);
        assert!(validate_process_inputs(None, &oversized_environment).is_err());
    }

    #[test]
    fn duplex_input_frames_are_authenticated_sequenced_and_bounded() {
        let token = "t".repeat(48);
        let nonce = "nonce-aaaaaaaaaaaaaaaaaaaaaaaa";
        let data_b64 = BASE64_STANDARD.encode(b"request\n");
        let mut mac = Hmac::<Sha256>::new_from_slice(token.as_bytes()).unwrap();
        mac.update(STDIO_MAC_CONTEXT);
        mac.update(nonce.as_bytes());
        mac.update(b"\0");
        mac.update(b"0");
        mac.update(b"\0stdin\0");
        mac.update(data_b64.as_bytes());
        let encoded_mac = mac
            .finalize()
            .into_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let frame = || StdioInputFrame {
            version: PROTOCOL_VERSION,
            nonce: nonce.to_string(),
            seq: 0,
            frame_type: "stdin".to_string(),
            data_b64: data_b64.clone(),
            mac: encoded_mac.clone(),
        };

        let (message, retained) =
            validate_stdio_input_frame(frame(), &token, nonce, 0, 1024).unwrap();
        assert!(matches!(message, StdioInputMessage::Data(value) if value == b"request\n"));
        assert_eq!(retained, 8);
        assert!(validate_stdio_input_frame(frame(), &token, nonce, 1, 1024).is_err());
        assert!(validate_stdio_input_frame(frame(), &token, "other-nonce", 0, 1024).is_err());
        assert!(validate_stdio_input_frame(frame(), &token, nonce, 0, 1).is_err());

        let mut tampered = frame();
        tampered.data_b64 = BASE64_STANDARD.encode(b"tampered\n");
        assert!(validate_stdio_input_frame(tampered, &token, nonce, 0, 1024).is_err());
    }

    #[test]
    fn events_serialize_to_the_v3_ndjson_shape() {
        let started = RuntimeEvent::Started {
            version: PROTOCOL_VERSION,
            nonce: "nonce".to_string(),
            seq: 0,
            pid: Some(123),
            capabilities: capabilities(),
        };
        let stdout = RuntimeEvent::Stdout {
            version: PROTOCOL_VERSION,
            nonce: "nonce".to_string(),
            seq: 1,
            data_b64: BASE64_STANDARD.encode(b"output"),
        };
        let completed = RuntimeEvent::Completed {
            version: PROTOCOL_VERSION,
            nonce: "nonce".to_string(),
            seq: 2,
            exit_code: 0,
        };

        assert_eq!(
            serde_json::to_value(started).unwrap()["type"],
            serde_json::json!("started")
        );
        assert_eq!(
            serde_json::to_value(stdout).unwrap()["data_b64"],
            serde_json::json!("b3V0cHV0")
        );
        assert_eq!(
            serde_json::to_value(completed).unwrap()["exit_code"],
            serde_json::json!(0)
        );
    }
}
