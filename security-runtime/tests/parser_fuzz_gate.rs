#[path = "../src/network/policy.rs"]
mod network_policy;
#[allow(dead_code)]
#[path = "../src/protocol.rs"]
mod protocol;
#[path = "../src/shell.rs"]
mod shell;
#[cfg(windows)]
#[allow(dead_code)]
#[path = "../src/windows/path.rs"]
mod windows_path;

use std::collections::BTreeMap;
#[cfg(windows)]
use std::path::Path;

use network_policy::NetworkPolicy;
use protocol::{validate_process_inputs, NetworkRule, RequestEnvelope};
use serde_json::Value;
use shell::{classify, ShellVerdict};

const CORPUS: &str = include_str!("../../tests/security/test_012_parser_corpus.json");
const ABSOLUTE_MAX_CASES: usize = 2048;
const ABSOLUTE_MAX_GENERATED_INPUT_BYTES: usize = 4096;

struct DeterministicRng(u64);

impl DeterministicRng {
    fn new(seed: u64) -> Self {
        Self(seed.max(1))
    }

    fn next_u64(&mut self) -> u64 {
        let mut value = self.0;
        value ^= value << 13;
        value ^= value >> 7;
        value ^= value << 17;
        self.0 = value;
        value
    }

    fn index(&mut self, length: usize) -> usize {
        (self.next_u64() as usize) % length
    }

    fn token(&mut self, maximum: usize) -> String {
        const ALPHABET: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789-._~";
        let length = 1 + self.index(maximum.max(1));
        (0..length)
            .map(|_| ALPHABET[self.index(ALPHABET.len())] as char)
            .collect()
    }

    fn parser_text(&mut self, maximum: usize) -> String {
        const ALPHABET: &[u8] =
            b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789{}[],:;|&$`'\"/\\ \t\r\n";
        let length = self.index(maximum.saturating_add(1));
        (0..length)
            .map(|_| ALPHABET[self.index(ALPHABET.len())] as char)
            .collect()
    }
}

fn corpus() -> Value {
    serde_json::from_str(CORPUS).expect("TEST-012 corpus must be valid JSON")
}

fn strings<'a>(corpus: &'a Value, section: &str, field: &str) -> Vec<&'a str> {
    corpus[section][field]
        .as_array()
        .unwrap_or_else(|| panic!("{section}.{field} must be an array"))
        .iter()
        .map(|value| {
            value
                .as_str()
                .unwrap_or_else(|| panic!("{section}.{field} entries must be strings"))
        })
        .collect()
}

fn campaign_number(corpus: &Value, field: &str) -> usize {
    corpus["campaign"][field]
        .as_u64()
        .unwrap_or_else(|| panic!("campaign.{field} must be an unsigned integer")) as usize
}

fn campaign_seed(corpus: &Value) -> u64 {
    let default = corpus["campaign"]["seed"]
        .as_u64()
        .expect("campaign.seed must be an unsigned integer");
    std::env::var("ACE_TEST012_SEED")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn campaign_cases(corpus: &Value) -> usize {
    let minimum = campaign_number(corpus, "ci_cases");
    let maximum = campaign_number(corpus, "max_cases");
    assert!(minimum > 0 && minimum <= maximum && maximum <= ABSOLUTE_MAX_CASES);
    std::env::var("ACE_TEST012_CASES")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(minimum)
        .clamp(minimum, maximum)
}

fn max_generated_input_bytes(corpus: &Value) -> usize {
    let maximum = campaign_number(corpus, "max_generated_input_bytes");
    assert!((1..=ABSOLUTE_MAX_GENERATED_INPUT_BYTES).contains(&maximum));
    maximum
}

fn network_rule(host: &str) -> NetworkRule {
    NetworkRule {
        host: host.to_string(),
        port: 443,
        protocol: "https".to_string(),
        allow: true,
        allow_private: false,
        escalatable: true,
    }
}

#[test]
fn deterministic_runtime_frame_and_json_properties() {
    let corpus = corpus();
    let maximum = max_generated_input_bytes(&corpus);
    let mut rng = DeterministicRng::new(campaign_seed(&corpus) ^ 0x0046_5241_4d45);

    for frame in strings(&corpus, "frame", "runtime_valid") {
        assert!(
            serde_json::from_str::<RequestEnvelope>(frame).is_ok(),
            "valid runtime seed was rejected: {frame}"
        );
    }
    for frame in strings(&corpus, "frame", "runtime_invalid") {
        assert!(
            serde_json::from_str::<RequestEnvelope>(frame).is_err(),
            "invalid runtime seed was accepted: {frame}"
        );
    }

    let valid = strings(&corpus, "frame", "runtime_valid")[0];
    for case_index in 0..campaign_cases(&corpus) {
        let mut value: Value = serde_json::from_str(valid).unwrap();
        match rng.index(6) {
            0 => {
                value.as_object_mut().unwrap().remove("version");
            }
            1 => value["token"] = Value::Bool(true),
            2 => value["nonce"] = serde_json::json!({"nested": true}),
            3 => value["request"] = Value::Null,
            4 => value["request"]["op"] = Value::String("unknown".to_string()),
            _ => value["request"]["raw_command"] = Value::Array(Vec::new()),
        }
        let encoded = serde_json::to_string(&value).unwrap();
        assert!(encoded.len() <= maximum);
        assert!(
            serde_json::from_str::<RequestEnvelope>(&encoded).is_err(),
            "structured invalid frame {case_index} was accepted: {encoded}"
        );

        let arbitrary = rng.parser_text(maximum);
        assert!(arbitrary.len() <= maximum);
        let _ = serde_json::from_str::<RequestEnvelope>(&arbitrary);
    }
}

#[test]
fn deterministic_network_host_properties() {
    let corpus = corpus();
    let mut rng = DeterministicRng::new(campaign_seed(&corpus) ^ 0x0055_524c);

    for host in strings(&corpus, "url", "rust_host_valid") {
        assert!(
            NetworkPolicy::new(vec![network_rule(host)]).is_ok(),
            "valid host seed was rejected: {host}"
        );
    }
    for host in strings(&corpus, "url", "rust_host_invalid") {
        assert!(
            NetworkPolicy::new(vec![network_rule(host)]).is_err(),
            "ambiguous host seed was accepted: {host:?}"
        );
    }

    for case_index in 0..campaign_cases(&corpus) {
        let token = rng.token(32);
        let hostile = match rng.index(7) {
            0 => format!("{token}@example.com"),
            1 => format!("{token}/path"),
            2 => format!(" {token}.example"),
            3 => format!("{token}..example"),
            4 => format!("*.{token}.example"),
            5 => format!("{token}_.example"),
            _ => format!("{token}.example\0suffix"),
        };
        assert!(
            NetworkPolicy::new(vec![network_rule(&hostile)]).is_err(),
            "generated ambiguous host {case_index} was accepted: {hostile:?}"
        );
    }
}

#[test]
fn deterministic_process_input_properties() {
    let corpus = corpus();
    let maximum = max_generated_input_bytes(&corpus);
    let mut rng = DeterministicRng::new(campaign_seed(&corpus) ^ 0x0045_4e56);

    for case_index in 0..campaign_cases(&corpus) {
        let name = match rng.index(6) {
            0 => format!("{}-BAD", rng.token(24)),
            1 => format!("{}=BAD", rng.token(24)),
            2 => "HTTP_PROXY".to_string(),
            3 => format!("ACE_SECURITY_{}", rng.token(24).to_ascii_uppercase()),
            4 => format!("ACE_BUNDLED_{}", rng.token(24).to_ascii_uppercase()),
            _ => format!("9{}", rng.token(24)),
        };
        let value = rng.parser_text(maximum);
        let environment = BTreeMap::from([(name.clone(), value)]);
        assert!(
            validate_process_inputs(None, &environment).is_err(),
            "invalid environment case {case_index} was accepted: {name:?}"
        );
    }
}

#[test]
fn deterministic_bash_command_properties_and_no_host_execution() {
    let corpus = corpus();
    let mut rng = DeterministicRng::new(campaign_seed(&corpus) ^ 0x4241_5348);
    let readonly = strings(&corpus, "command", "bash_read_only");

    for command in &readonly {
        assert_eq!(
            classify("bash", "/bin/bash", command).verdict,
            ShellVerdict::AllowReadOnly,
            "read-only seed was not proven safe: {command}"
        );
    }
    for command in strings(&corpus, "command", "bash_must_ask") {
        assert_eq!(
            classify("bash", "/bin/bash", command).verdict,
            ShellVerdict::Ask,
            "unsafe shell seed was auto-allowed: {command}"
        );
    }

    for case_index in 0..campaign_cases(&corpus) {
        let base = readonly[rng.index(readonly.len())];
        let suffix = match rng.index(6) {
            0 => "; rm -rf /tmp/ace-test-012",
            1 => " > ace-test-012-output",
            2 => " $(id)",
            3 => "\nrm -rf /tmp/ace-test-012",
            4 => " `id`",
            _ => " | sh",
        };
        let command = format!("{base}{suffix}");
        assert_eq!(
            classify("bash", "/bin/bash", &command).verdict,
            ShellVerdict::Ask,
            "generated command injection {case_index} was auto-allowed: {command}"
        );
    }

    let temporary = tempfile::tempdir().unwrap();
    let marker = temporary.path().join("parser-must-not-execute");
    let template = corpus["command"]["host_execution_templates"]["bash"]
        .as_str()
        .unwrap();
    let payload = template.replace("{marker}", &marker.display().to_string());
    assert_eq!(
        classify("bash", "/bin/bash", &payload).verdict,
        ShellVerdict::Ask
    );
    assert!(
        !marker.exists(),
        "classifying a Bash payload executed it on the host"
    );
}

#[cfg(not(windows))]
#[test]
fn powershell_fails_closed_when_the_platform_parser_is_unavailable() {
    let corpus = corpus();
    for command in strings(&corpus, "command", "powershell_read_only")
        .into_iter()
        .chain(strings(&corpus, "command", "powershell_must_ask"))
    {
        assert_eq!(
            classify("powershell", "powershell", command).verdict,
            ShellVerdict::Ask,
            "PowerShell must not auto-allow without the platform parser: {command}"
        );
    }
}

#[cfg(windows)]
#[test]
fn deterministic_powershell_properties_and_no_host_execution() {
    let corpus = corpus();
    let executable = std::env::var_os("SystemRoot")
        .map(std::path::PathBuf::from)
        .map(|root| {
            root.join("System32")
                .join("WindowsPowerShell")
                .join("v1.0")
                .join("powershell.exe")
        })
        .filter(|path| path.is_file())
        .unwrap_or_else(|| std::path::PathBuf::from("powershell.exe"));
    let executable = executable.to_string_lossy();

    for command in strings(&corpus, "command", "powershell_read_only") {
        assert_eq!(
            classify("powershell", &executable, command).verdict,
            ShellVerdict::AllowReadOnly,
            "read-only PowerShell seed was not proven safe: {command}"
        );
    }
    let unsafe_commands = strings(&corpus, "command", "powershell_must_ask");
    for command in &unsafe_commands {
        assert_eq!(
            classify("powershell", &executable, command).verdict,
            ShellVerdict::Ask,
            "unsafe PowerShell seed was auto-allowed: {command}"
        );
    }

    let generated_cases = campaign_cases(&corpus).min(16);
    for case_index in 0..generated_cases {
        let command = format!(
            "Get-ChildItem; {}",
            unsafe_commands[case_index % unsafe_commands.len()]
        );
        assert_eq!(
            classify("powershell", &executable, &command).verdict,
            ShellVerdict::Ask,
            "generated PowerShell injection {case_index} was auto-allowed: {command}"
        );
    }

    let temporary = tempfile::tempdir().unwrap();
    let marker = temporary.path().join("parser-must-not-execute");
    let escaped_marker = marker.display().to_string().replace('\'', "''");
    let template = corpus["command"]["host_execution_templates"]["powershell"]
        .as_str()
        .unwrap();
    let payload = template.replace("{marker}", &escaped_marker);
    assert_eq!(
        classify("powershell", &executable, &payload).verdict,
        ShellVerdict::Ask
    );
    assert!(
        !marker.exists(),
        "classifying a PowerShell payload executed it on the host"
    );
}

#[cfg(windows)]
#[test]
fn deterministic_windows_path_properties() {
    let corpus = corpus();
    let mut rng = DeterministicRng::new(campaign_seed(&corpus) ^ 0x5041_5448);

    for path in strings(&corpus, "path", "windows_valid") {
        assert!(
            windows_path::validate_local_absolute(Path::new(path)).is_ok(),
            "valid Windows path seed was rejected: {path:?}"
        );
    }
    for path in strings(&corpus, "path", "windows_invalid") {
        assert!(
            windows_path::validate_local_absolute(Path::new(path)).is_err(),
            "unsafe Windows path seed was accepted: {path:?}"
        );
    }

    for case_index in 0..campaign_cases(&corpus) {
        let token = rng.token(24);
        let path = match rng.index(5) {
            0 => format!(r"C:{token}"),
            1 => format!(r"\\server\share\{token}"),
            2 => format!(r"\\.\PIPE\{token}"),
            3 => format!(r"\\?\C:\{token}"),
            _ => format!("relative\\{token}\0suffix"),
        };
        assert!(
            windows_path::validate_local_absolute(Path::new(&path)).is_err(),
            "generated unsafe Windows path {case_index} was accepted: {path:?}"
        );
    }
}
