use crate::runtime::NearbyConfig;
use anyhow::Result;
use std::{future::Future, pin::Pin};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum TransportMode {
    #[default]
    Ble,
    Mock,
}

/// Pluggable physical-link contract.
///
/// Companion protocol and conversation state live above this boundary.  A link
/// adapter only supplies discovery/connectivity and framed byte transport, so a
/// future LAN or relay adapter can be added without changing the domain model.
pub trait LinkAdapter: Send + Sync {
    fn id(&self) -> &'static str;

    fn run<'a>(
        &'a self,
        config: NearbyConfig,
    ) -> Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>>;
}

struct BleLinkAdapter;

impl LinkAdapter for BleLinkAdapter {
    fn id(&self) -> &'static str {
        "ble"
    }

    fn run<'a>(
        &'a self,
        config: NearbyConfig,
    ) -> Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>> {
        Box::pin(crate::ipc::run_ble(config))
    }
}

struct MockLinkAdapter;

impl LinkAdapter for MockLinkAdapter {
    fn id(&self) -> &'static str {
        "mock"
    }

    fn run<'a>(
        &'a self,
        config: NearbyConfig,
    ) -> Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>> {
        Box::pin(crate::mock::run(config))
    }
}

pub async fn run(config: NearbyConfig) -> Result<()> {
    adapter(config.transport).run(config).await
}

pub fn adapter(mode: TransportMode) -> Box<dyn LinkAdapter> {
    match mode {
        TransportMode::Ble => Box::new(BleLinkAdapter),
        TransportMode::Mock => Box::new(MockLinkAdapter),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolves_link_adapters_by_mode() {
        assert_eq!(adapter(TransportMode::Ble).id(), "ble");
        assert_eq!(adapter(TransportMode::Mock).id(), "mock");
    }
}
