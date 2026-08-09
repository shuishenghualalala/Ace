use std::collections::BTreeMap;

use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: u16 = 2;
pub const MAX_REQUEST_FRAME_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_STDIN_BYTES: usize = 1024 * 1024;
pub const MAX_ENV_BYTES: usize = 256 * 1024;
pub const MAX_HOME_FILE_BYTES: usize = 1024 * 1024;
pub const MAX_HOME_TOTAL_BYTES: usize = 2 * 1024 * 1024;
pub const MAX_HOME_FILES: usize = 64;
pub const MAX_RESPONSE_FRAME_BYTES: usize = 128 * 1024;
pub const MAX_OUTPUT_CHUNK_BYTES: usize = 64 * 1024;
pub const DEFAULT_MAX_OUTPUT_BYTES: usize = 2 * 1024 * 1024;
pub const READY_CAPABILITIES: [&str; 3] =
    ["stdin_once", "stream_output", "stdin_bidirectional"];

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

#[derive(Debug, Deserialize)]
pub struct RequestEnvelope {
    pub version: u16,
    pub token: String,
    pub nonce: String,
    pub request: RuntimeRequest,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
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
        denied_roots: Vec<String>,
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
    InteractiveOpen {
        command: Vec<String>,
        cwd: String,
        #[serde(default)]
        writable_roots: Vec<String>,
        #[serde(default)]
        readable_roots: Vec<String>,
        #[serde(default)]
        denied_roots: Vec<String>,
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
    pub capabilities: [&'static str; 3],
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

fn default_escalatable() -> bool {
    true
}

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
    for (name, value) in env_overrides {
        if !valid_environment_name(name) || value.contains('\0') {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "invalid environment entry",
            });
        }
        let normalized = name.to_ascii_uppercase();
        if matches!(
            normalized.as_str(),
            "HTTP_PROXY" | "HTTPS_PROXY" | "ALL_PROXY" | "NO_PROXY"
        ) || normalized.starts_with("ACE_SECURITY_")
            || normalized.starts_with("ACE_BUNDLED_")
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
            || components.iter().any(|part| part.is_empty() || *part == "." || *part == "..")
        {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "projected HOME path must be relative",
            });
        }
        let decoded = BASE64_STANDARD.decode(encoded).map_err(|_| InputValidationError {
            code: "sandbox_denied",
            message: "invalid projected HOME file encoding",
        })?;
        if decoded.len() > MAX_HOME_FILE_BYTES {
            return Err(InputValidationError {
                code: "sandbox_denied",
                message: "projected HOME file exceeds the size limit",
            });
        }
        total_home_bytes = total_home_bytes
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

fn valid_environment_name(name: &str) -> bool {
    let mut bytes = name.bytes();
    matches!(bytes.next(), Some(b'A'..=b'Z' | b'a'..=b'z' | b'_'))
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
}

#[cfg(test)]
mod tests {
    use super::{
        validate_process_inputs, RuntimeCapabilities, RuntimeEvent, MAX_ENV_BYTES,
        MAX_OUTPUT_CHUNK_BYTES, MAX_REQUEST_FRAME_BYTES, MAX_RESPONSE_FRAME_BYTES, MAX_STDIN_BYTES,
        PROTOCOL_VERSION,
    };
    use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
    use base64::Engine;
    use std::collections::BTreeMap;

    fn capabilities() -> RuntimeCapabilities {
        RuntimeCapabilities {
            backend: "test",
            filesystem_sandbox: true,
            process_tree_cleanup: true,
            managed_network: false,
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
        assert_eq!(PROTOCOL_VERSION, 2);
        assert_eq!(MAX_REQUEST_FRAME_BYTES, 2 * 1024 * 1024);
        assert_eq!(MAX_STDIN_BYTES, 1024 * 1024);
        assert_eq!(MAX_ENV_BYTES, 256 * 1024);
        assert_eq!(MAX_RESPONSE_FRAME_BYTES, 128 * 1024);
        assert_eq!(MAX_OUTPUT_CHUNK_BYTES, 64 * 1024);
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
            "HTTP_PROXY",
            "ace_security_runtime_token",
            "ACE_BUNDLED_BWRAP",
        ] {
            let environment = BTreeMap::from([(name.to_string(), "value".to_string())]);
            assert!(validate_process_inputs(None, &environment).is_err());
        }

        let oversized_environment =
            BTreeMap::from([("LARGE".to_string(), "x".repeat(MAX_ENV_BYTES))]);
        assert!(validate_process_inputs(None, &oversized_environment).is_err());
    }

    #[test]
    fn events_serialize_to_the_v2_ndjson_shape() {
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
