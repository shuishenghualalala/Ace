use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

use super::policy::{NetworkError, NetworkPolicy};
use crate::protocol::NetworkErrorCode;

/// Connect to the first reachable approved address for `host:port`.
///
/// Returns a structured `NetworkError` so the caller can surface a stable
/// code (spec §13, N8): `policy_denied` when no rule approves the
/// destination, `network_unavailable` when every approved address fails to
/// connect.
pub fn connect(
    policy: &NetworkPolicy,
    host: &str,
    port: u16,
    protocol: &str,
    timeout: Duration,
) -> Result<TcpStream, NetworkError> {
    let addresses = policy.resolve_allowed(host, port, protocol)?;
    let mut last_error = None;
    for address in addresses {
        match connect_pinned(address, timeout) {
            Ok(stream) => return Ok(stream),
            Err(error) => last_error = Some(error),
        }
    }
    Err(NetworkError::new(
        NetworkErrorCode::NetworkUnavailable,
        format!(
            "approved destination could not be reached: {}",
            last_error.unwrap_or_else(|| "no address".to_string())
        ),
    ))
}

fn connect_pinned(address: SocketAddr, timeout: Duration) -> Result<TcpStream, String> {
    let stream = TcpStream::connect_timeout(&address, timeout)
        .map_err(|error| format!("connect {address} failed: {error}"))?;
    stream
        .set_read_timeout(Some(timeout))
        .and_then(|_| stream.set_write_timeout(Some(timeout)))
        .map_err(|error| format!("cannot set connector timeout: {error}"))?;
    Ok(stream)
}
