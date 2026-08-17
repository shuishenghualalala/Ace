use std::collections::HashSet;
use std::net::{IpAddr, SocketAddr, ToSocketAddrs};

use crate::protocol::{NetworkErrorCode, NetworkRule};

#[cfg(test)]
const METADATA_V4: &str = "169.254.169.254";
const MAX_DNS_ANSWERS: usize = 32;

/// Structured network-layer error carrying a stable code (spec §13) plus a
/// human-readable message. Replaces the free-form `String` errors previously
/// returned by `resolve_allowed` (N8): those strings were flattened by the
/// platform backends into `sandbox_unavailable`/`sandbox_denied`, losing the
/// real reason and forcing the host to fall back on trial-and-error.
#[derive(Clone, Debug)]
pub struct NetworkError {
    pub code: NetworkErrorCode,
    pub message: String,
}

impl NetworkError {
    pub fn new(code: NetworkErrorCode, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

impl std::fmt::Display for NetworkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code.as_str(), self.message)
    }
}

impl std::error::Error for NetworkError {}

#[derive(Clone, Debug)]
pub struct NetworkPolicy {
    rules: Vec<NetworkRule>,
}

impl NetworkPolicy {
    pub fn new(rules: Vec<NetworkRule>) -> Result<Self, NetworkError> {
        for rule in &rules {
            validate_rule(rule)
                .map_err(|message| NetworkError::new(NetworkErrorCode::PolicyDenied, message))?;
        }
        Ok(Self { rules })
    }

    /// Resolve once, evaluate every returned IP, and return only approved socket addresses.
    ///
    /// Errors carry a stable `NetworkErrorCode` so the host can distinguish a
    /// policy rejection (`policy_denied`) from an unreachable destination
    /// (`network_unavailable`) instead of receiving a flattened
    /// `sandbox_unavailable` (spec §13, N8).
    pub fn resolve_allowed(
        &self,
        host: &str,
        port: u16,
        protocol: &str,
    ) -> Result<Vec<SocketAddr>, NetworkError> {
        let host = normalize_host(host)
            .map_err(|message| NetworkError::new(NetworkErrorCode::PolicyDenied, message))?;
        if is_metadata_host(&host) {
            return Err(NetworkError::new(
                NetworkErrorCode::PolicyDenied,
                "cloud metadata endpoints are permanently denied",
            ));
        }
        let protocol = protocol.to_ascii_lowercase();
        let matching: Vec<&NetworkRule> = self
            .rules
            .iter()
            .filter(|rule| rule.host == host && rule.port == port && rule.protocol == protocol)
            .collect();
        if matching.iter().any(|rule| !rule.allow) {
            return Err(NetworkError::new(
                NetworkErrorCode::PolicyDenied,
                "network destination is explicitly denied",
            ));
        }
        let allow = matching.iter().find(|rule| rule.allow).ok_or_else(|| {
            NetworkError::new(
                NetworkErrorCode::PolicyDenied,
                "network destination is not approved",
            )
        })?;
        let addresses: Vec<SocketAddr> = (host.as_str(), port)
            .to_socket_addrs()
            .map_err(|error| {
                NetworkError::new(
                    NetworkErrorCode::NetworkUnavailable,
                    format!("cannot resolve approved destination: {error}"),
                )
            })?
            .collect();
        if addresses.is_empty() {
            return Err(NetworkError::new(
                NetworkErrorCode::NetworkUnavailable,
                "approved destination resolved to no address",
            ));
        }
        validate_resolved_addresses(
            addresses,
            allow.allow_private,
            host == "localhost" || host.ends_with(".localhost"),
        )
    }
}

fn validate_resolved_addresses(
    addresses: Vec<SocketAddr>,
    allow_private: bool,
    localhost_name: bool,
) -> Result<Vec<SocketAddr>, NetworkError> {
    if addresses.len() > MAX_DNS_ANSWERS {
        return Err(NetworkError::new(
            NetworkErrorCode::PolicyDenied,
            "DNS answer limit exceeded",
        ));
    }
    let mut seen = HashSet::new();
    let mut approved = Vec::new();
    for address in addresses {
        if localhost_name && !is_loopback(address.ip()) {
            return Err(NetworkError::new(
                NetworkErrorCode::PolicyDenied,
                "localhost name resolved to a non-loopback address",
            ));
        }
        if is_metadata(address.ip()) {
            return Err(NetworkError::new(
                NetworkErrorCode::PolicyDenied,
                "cloud metadata endpoints are permanently denied",
            ));
        }
        if is_local_or_private(address.ip()) && !allow_private {
            return Err(NetworkError::new(
                NetworkErrorCode::PolicyDenied,
                "destination resolved to a local or private address",
            ));
        }
        if seen.insert(address) {
            approved.push(address);
        }
    }
    Ok(approved)
}

fn is_loopback(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(value) => value.is_loopback(),
        IpAddr::V6(value) => value
            .to_ipv4()
            .map(|mapped| mapped.is_loopback())
            .unwrap_or_else(|| value.is_loopback()),
    }
}

fn validate_rule(rule: &NetworkRule) -> Result<(), String> {
    if normalize_host(&rule.host)? != rule.host {
        return Err("network host is not canonical".to_string());
    }
    if rule.port == 0 {
        return Err("network port must be between 1 and 65535".to_string());
    }
    if rule.protocol != rule.protocol.to_ascii_lowercase() {
        return Err("network protocol is not canonical".to_string());
    }
    if !matches!(rule.protocol.as_str(), "http" | "https" | "tcp") {
        return Err("managed proxy supports only http, https, or tcp".to_string());
    }
    if !rule.allow && !rule.escalatable && rule.allow_private {
        return Err("immutable deny cannot carry allow_private".to_string());
    }
    Ok(())
}

fn normalize_host(raw: &str) -> Result<String, String> {
    if raw.is_empty()
        || raw != raw.trim()
        || !raw.is_ascii()
        || raw.bytes().any(|byte| byte <= 0x20 || byte == 0x7f)
    {
        return Err("network host must be one exact hostname or IP".to_string());
    }
    let unbracketed = if let Some(value) = raw.strip_prefix('[') {
        value
            .strip_suffix(']')
            .ok_or_else(|| "network host must be one exact hostname or IP".to_string())?
    } else {
        if raw.contains(['[', ']']) {
            return Err("network host must be one exact hostname or IP".to_string());
        }
        raw
    };
    if let Ok(address) = unbracketed.parse::<IpAddr>() {
        return Ok(address.to_string().to_ascii_lowercase());
    }
    if unbracketed.contains(':') {
        return Err("network host must be one exact hostname or IP".to_string());
    }
    let host = unbracketed
        .strip_suffix('.')
        .unwrap_or(unbracketed)
        .to_ascii_lowercase();
    if host.is_empty()
        || host.ends_with('.')
        || host.len() > 253
        || host
            .chars()
            .all(|value| value.is_ascii_digit() || value == '.')
    {
        return Err("network host must be one exact hostname or IP".to_string());
    }
    for label in host.split('.') {
        let bytes = label.as_bytes();
        if bytes.is_empty()
            || bytes.len() > 63
            || !bytes[0].is_ascii_alphanumeric()
            || !bytes[bytes.len() - 1].is_ascii_alphanumeric()
            || !bytes
                .iter()
                .all(|value| value.is_ascii_alphanumeric() || *value == b'-')
        {
            return Err("network host must be one exact hostname or IP".to_string());
        }
    }
    Ok(host)
}

fn is_metadata(ip: IpAddr) -> bool {
    // IPv4-mapped IPv6（::ffff:a.b.c.d）必须先归约到 IPv4 再判定，否则 v4 元数据
    // 端点能用 mapped 形式绕过（经典 SSRF）。Codex policy.rs:83-91 同样用 to_ipv4() 短路。
    let ip = match ip {
        IpAddr::V6(v6) => match v6.to_ipv4() {
            Some(v4) => IpAddr::V4(v4),
            None => IpAddr::V6(v6),
        },
        other => other,
    };
    match ip {
        IpAddr::V4(value) => {
            value.octets() == [169, 254, 169, 254]
                || value.octets() == [169, 254, 170, 2]
                || value.octets() == [168, 63, 129, 16]
                || value.octets() == [100, 100, 100, 200]
                || value.octets() == [192, 0, 0, 192]
        }
        // AWS IPv6 元数据端点 fd00:ec2::254 属 ULA（fc00::/7），is_local_or_private 会判为私网；
        // 这里显式拒绝，保证"元数据不可升级 deny"在任何 allow_private 放行下仍成立。
        IpAddr::V6(value) => value.segments()[0] == 0xfd00 && value.segments()[1] == 0x0ec2,
    }
}

fn is_metadata_host(host: &str) -> bool {
    matches!(
        host,
        "instance-data"
            | "metadata"
            | "metadata.aws.internal"
            | "metadata.azure.internal"
            | "metadata.google.internal"
    ) || host.ends_with(".metadata.google.internal")
        || host.ends_with(".metadata.azure.internal")
        || host.ends_with(".metadata.aws.internal")
}

fn is_local_or_private(ip: IpAddr) -> bool {
    // 与 is_metadata 同步：mapped IPv6 归约到 IPv4 后再分类，避免 ::ffff:10.0.0.1 被当公网。
    let ip = match ip {
        IpAddr::V6(v6) => match v6.to_ipv4() {
            Some(v4) => IpAddr::V4(v4),
            None => IpAddr::V6(v6),
        },
        other => other,
    };
    match ip {
        IpAddr::V4(value) => {
            value.is_loopback()
                || value.is_private()
                || value.is_link_local()
                || value.is_unspecified()
                || value.is_multicast()
                || value.is_broadcast()
                || ipv4_in_cidr(value, [0, 0, 0, 0], 8) // "this network" (RFC 1122)
                || ipv4_in_cidr(value, [100, 64, 0, 0], 10) // CGNAT (RFC 6598)
                || ipv4_in_cidr(value, [192, 0, 0, 0], 24) // IETF Protocol Assignments (RFC 6890)
                || ipv4_in_cidr(value, [192, 0, 2, 0], 24) // TEST-NET-1 (RFC 5737)
                || ipv4_in_cidr(value, [198, 18, 0, 0], 15) // Benchmarking (RFC 2544)
                || ipv4_in_cidr(value, [198, 51, 100, 0], 24) // TEST-NET-2 (RFC 5737)
                || ipv4_in_cidr(value, [203, 0, 113, 0], 24) // TEST-NET-3 (RFC 5737)
                || ipv4_in_cidr(value, [240, 0, 0, 0], 4) // Reserved (RFC 6890)
        }
        IpAddr::V6(value) => {
            value.is_loopback()
                || value.is_unspecified()
                || value.is_multicast()
                || value.is_unique_local()
                || value.is_unicast_link_local()
        }
    }
}

fn ipv4_in_cidr(ip: std::net::Ipv4Addr, base: [u8; 4], prefix: u8) -> bool {
    let ip = u32::from(ip);
    let base = u32::from(std::net::Ipv4Addr::from(base));
    let mask = if prefix == 0 {
        0
    } else {
        u32::MAX << (32 - prefix)
    };
    (ip & mask) == (base & mask)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule(host: &str, allow: bool, allow_private: bool) -> NetworkRule {
        NetworkRule {
            host: host.to_string(),
            port: 8080,
            protocol: "http".to_string(),
            allow,
            allow_private,
            escalatable: true,
        }
    }

    #[test]
    fn no_allow_is_denied() {
        let policy = NetworkPolicy::new(Vec::new()).unwrap();
        assert!(policy.resolve_allowed("127.0.0.1", 8080, "http").is_err());
    }

    #[test]
    fn deny_wins_and_metadata_is_immutable() {
        let policy = NetworkPolicy::new(vec![
            rule("127.0.0.1", true, true),
            rule("127.0.0.1", false, false),
        ])
        .unwrap();
        assert!(policy.resolve_allowed("127.0.0.1", 8080, "http").is_err());
        let metadata = NetworkPolicy::new(vec![rule(METADATA_V4, true, true)]).unwrap();
        assert!(metadata.resolve_allowed(METADATA_V4, 8080, "http").is_err());
    }

    #[test]
    fn ipv4_mapped_ipv6_metadata_is_denied() {
        // ::ffff:169.254.169.254 必须被识别为元数据，不能因 v6 分支漏判而放行。
        let policy = NetworkPolicy::new(vec![rule("::ffff:169.254.169.254", true, true)]).unwrap();
        assert!(policy
            .resolve_allowed("::ffff:169.254.169.254", 8080, "http")
            .is_err());
    }

    #[test]
    fn ipv4_mapped_ipv6_private_is_denied_without_allow_private() {
        // ::ffff:10.0.0.1 必须被识别为私网，不能因 v6 分支漏判而当公网放行。
        let policy = NetworkPolicy::new(vec![rule("::ffff:10.0.0.1", true, false)]).unwrap();
        assert!(policy
            .resolve_allowed("::ffff:10.0.0.1", 8080, "http")
            .is_err());
    }

    #[test]
    fn aws_ipv6_metadata_endpoint_is_denied() {
        // fd00:ec2::254 是 AWS IPv6 元数据；即使 allow_private=true 也不可放行。
        let policy = NetworkPolicy::new(vec![rule("fd00:ec2::254", true, true)]).unwrap();
        assert!(policy
            .resolve_allowed("fd00:ec2::254", 8080, "http")
            .is_err());
    }

    #[test]
    fn cgnat_range_is_treated_as_private() {
        // 100.64.0.1 属 CGNAT（RFC 6598），非公网；无 allow_private 时必须拒绝。
        let policy = NetworkPolicy::new(vec![rule("100.64.0.1", true, false)]).unwrap();
        assert!(policy.resolve_allowed("100.64.0.1", 8080, "http").is_err());
    }

    // N8 regression: resolve_allowed must surface stable NetworkErrorCode
    // values (spec §13), not free-form strings flattened by the platform
    // backends into sandbox_unavailable/sandbox_denied.

    #[test]
    fn missing_rule_surfaces_policy_denied_code() {
        let policy = NetworkPolicy::new(Vec::new()).unwrap();
        let err = policy
            .resolve_allowed("127.0.0.1", 8080, "http")
            .unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
    }

    #[test]
    fn explicit_deny_surfaces_policy_denied_code() {
        let policy = NetworkPolicy::new(vec![
            rule("127.0.0.1", true, true),
            rule("127.0.0.1", false, false),
        ])
        .unwrap();
        let err = policy
            .resolve_allowed("127.0.0.1", 8080, "http")
            .unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
    }

    #[test]
    fn metadata_surfaces_policy_denied_code() {
        let metadata = NetworkPolicy::new(vec![rule(METADATA_V4, true, true)]).unwrap();
        let err = metadata
            .resolve_allowed(METADATA_V4, 8080, "http")
            .unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
    }

    #[test]
    fn metadata_hostname_is_denied_before_dns_even_with_private_access() {
        let metadata =
            NetworkPolicy::new(vec![rule("metadata.google.internal", true, true)]).unwrap();
        let err = metadata
            .resolve_allowed("metadata.google.internal", 8080, "http")
            .unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
    }

    #[test]
    fn private_without_allow_private_surfaces_policy_denied_code() {
        let policy = NetworkPolicy::new(vec![rule("100.64.0.1", true, false)]).unwrap();
        let err = policy
            .resolve_allowed("100.64.0.1", 8080, "http")
            .unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
    }

    #[test]
    fn network_error_displays_with_code_prefix() {
        let err = NetworkError::new(NetworkErrorCode::NetworkUnavailable, "boom");
        assert_eq!(format!("{err}"), "network_unavailable: boom");
    }

    #[test]
    fn invalid_rule_surfaces_policy_denied_code() {
        let mut invalid = rule("example.com", true, false);
        invalid.protocol = "udp".to_string();
        let err = NetworkPolicy::new(vec![invalid]).unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
    }

    #[test]
    fn cloud_metadata_aliases_are_denied_before_dns() {
        for host in [
            "metadata.aws.internal",
            "node.metadata.aws.internal",
            "168.63.129.16",
            "192.0.0.192",
        ] {
            let metadata = NetworkPolicy::new(vec![rule(host, true, true)]).unwrap();
            let err = metadata.resolve_allowed(host, 8080, "http").unwrap_err();
            assert_eq!(err.code, NetworkErrorCode::PolicyDenied, "{host}");
        }
    }

    #[test]
    fn zero_port_rule_is_rejected_before_resolution() {
        let mut invalid = rule("example.com", true, false);
        invalid.port = 0;
        let err = NetworkPolicy::new(vec![invalid]).unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
    }

    #[test]
    fn protocol_rule_must_be_canonical_lowercase() {
        let mut invalid = rule("example.com", true, false);
        invalid.protocol = "HTTP".to_string();
        let err = NetworkPolicy::new(vec![invalid]).unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
    }

    #[test]
    fn host_normalization_rejects_ambiguous_or_non_ascii_forms() {
        assert_eq!(normalize_host("EXAMPLE.COM.").unwrap(), "example.com");
        assert_eq!(
            normalize_host("[::ffff:127.0.0.1]").unwrap(),
            "::ffff:127.0.0.1"
        );
        for invalid in [
            "127.1",
            "example..com",
            "-example.com",
            "example_.com",
            "user@example.com",
            "ｅxample.com",
            " example.com",
        ] {
            assert!(
                normalize_host(invalid).is_err(),
                "{invalid} must be rejected"
            );
        }
    }

    #[test]
    fn dns_answer_count_is_bounded_before_connect() {
        let addresses = (1..=33)
            .map(|last| SocketAddr::from(([93, 184, 216, last], 443)))
            .collect();
        let err = validate_resolved_addresses(addresses, false, false).unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
        assert!(err.message.contains("answer limit"));
    }

    #[test]
    fn localhost_name_never_matches_a_non_loopback_answer() {
        let addresses = vec![SocketAddr::from(([93, 184, 216, 34], 443))];
        let err = validate_resolved_addresses(addresses, true, true).unwrap_err();
        assert_eq!(err.code, NetworkErrorCode::PolicyDenied);
        assert!(err.message.contains("localhost"));
    }
}
