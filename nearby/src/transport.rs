use crate::runtime::NearbyConfig;
use anyhow::Result;
use std::{future::Future, pin::Pin};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum TransportMode {
    #[default]
    Ble,
    Mock,
}

pub trait TransportBackend: Send + Sync {
    fn run<'a>(
        &'a self,
        config: NearbyConfig,
    ) -> Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>>;
}

struct BleTransport;

impl TransportBackend for BleTransport {
    fn run<'a>(
        &'a self,
        config: NearbyConfig,
    ) -> Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>> {
        Box::pin(crate::ipc::run_ble(config))
    }
}

struct MockTransport;

impl TransportBackend for MockTransport {
    fn run<'a>(
        &'a self,
        config: NearbyConfig,
    ) -> Pin<Box<dyn Future<Output = Result<()>> + Send + 'a>> {
        Box::pin(crate::mock::run(config))
    }
}

pub async fn run(config: NearbyConfig) -> Result<()> {
    match config.transport {
        TransportMode::Ble => BleTransport.run(config).await,
        TransportMode::Mock => MockTransport.run(config).await,
    }
}
