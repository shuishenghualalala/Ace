//! Compile and exercise the Linux bwrap plan builder on every development host.
//!
//! Native namespace behavior still requires Linux, but this catches Rust/type
//! regressions in Linux-only code during the normal Windows test suite.

mod linux {
    use std::collections::BTreeMap;
    use std::path::PathBuf;

    #[allow(dead_code)]
    pub struct LinuxRunRequest {
        pub command: Vec<String>,
        pub cwd: PathBuf,
        pub writable_roots: Vec<PathBuf>,
        pub readable_roots: Vec<PathBuf>,
        pub denied_roots: Vec<PathBuf>,
        pub network_enabled: bool,
        pub network_rules: Vec<()>,
        pub allow_local_binding: bool,
        pub proxy_socket_dir: Option<PathBuf>,
        pub max_output_bytes: usize,
        pub stdin: Option<Vec<u8>>,
        pub env_overrides: BTreeMap<String, String>,
        pub home_files: BTreeMap<String, Vec<u8>>,
    }

    pub mod proxy_routing {
        pub const INNER_SOCKET_PATH: &str = "/run/ace-network/proxy.sock";
    }

    pub mod bwrap {
        include!(concat!(env!("CARGO_MANIFEST_DIR"), "/src/linux/bwrap.rs"));
    }
}
