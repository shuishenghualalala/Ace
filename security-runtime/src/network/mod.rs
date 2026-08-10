use std::collections::BTreeMap;

pub mod connector;
pub mod policy;
pub mod proxy;

pub use policy::NetworkPolicy;

/// Runtime-owned proxy variables applied after host-provided environment
/// overrides. Node 24 requires NODE_USE_ENV_PROXY to honor the standard proxy
/// variables; other runtimes safely ignore the extra key.
pub fn managed_proxy_environment(proxy_url: &str) -> BTreeMap<String, String> {
    BTreeMap::from([
        ("HTTP_PROXY".to_string(), proxy_url.to_string()),
        ("HTTPS_PROXY".to_string(), proxy_url.to_string()),
        ("ALL_PROXY".to_string(), proxy_url.to_string()),
        ("NO_PROXY".to_string(), String::new()),
        ("NODE_USE_ENV_PROXY".to_string(), "1".to_string()),
    ])
}

#[cfg(test)]
mod tests {
    use super::managed_proxy_environment;

    #[test]
    fn managed_proxy_environment_enables_node_without_bypassing_the_proxy() {
        let environment = managed_proxy_environment("http://127.0.0.1:43119");
        assert_eq!(environment["HTTP_PROXY"], "http://127.0.0.1:43119");
        assert_eq!(environment["HTTPS_PROXY"], "http://127.0.0.1:43119");
        assert_eq!(environment["ALL_PROXY"], "http://127.0.0.1:43119");
        assert_eq!(environment["NO_PROXY"], "");
        assert_eq!(environment["NODE_USE_ENV_PROXY"], "1");
    }
}
