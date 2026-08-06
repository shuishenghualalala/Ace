//! Private Unix-socket bridge between a host proxy and a bwrap network namespace.

use std::io;
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

pub const INNER_PROXY_PORT: u16 = 43119;
pub const INNER_SOCKET_PATH: &str = "/run/ace-network/proxy.sock";

pub struct HostBridge {
    pub socket_dir: PathBuf,
    stopped: Arc<AtomicBool>,
}

impl HostBridge {
    pub fn start(proxy_address: SocketAddr) -> Result<Self, String> {
        let socket_dir = std::env::temp_dir().join(format!(
            "ace-network-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        std::fs::create_dir(&socket_dir)
            .map_err(|error| format!("cannot create proxy bridge directory: {error}"))?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&socket_dir, std::fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("cannot protect proxy bridge directory: {error}"))?;
        let listener = UnixListener::bind(socket_dir.join("proxy.sock"))
            .map_err(|error| format!("cannot bind proxy bridge socket: {error}"))?;
        listener
            .set_nonblocking(true)
            .map_err(|error| error.to_string())?;
        let stopped = Arc::new(AtomicBool::new(false));
        let stop = Arc::clone(&stopped);
        thread::spawn(move || {
            while !stop.load(Ordering::Acquire) {
                match listener.accept() {
                    Ok((inside, _)) => {
                        if let Ok(proxy) = TcpStream::connect(proxy_address) {
                            thread::spawn(move || {
                                let _ = tunnel_unix_tcp(inside, proxy);
                            });
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(Self {
            socket_dir,
            stopped,
        })
    }
}

impl Drop for HostBridge {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Release);
        let _ = std::fs::remove_dir_all(&self.socket_dir);
    }
}

/// Fork a tiny namespace-local loopback bridge before the command seccomp filter is installed.
pub fn start_inner_bridge(socket_path: &Path) -> Result<(), String> {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, INNER_PROXY_PORT))
        .map_err(|error| format!("cannot bind inner proxy bridge: {error}"))?;
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(format!(
            "cannot fork inner proxy bridge: {}",
            io::Error::last_os_error()
        ));
    }
    if pid == 0 {
        loop {
            let Ok((inside, _)) = listener.accept() else {
                std::process::exit(0);
            };
            let Ok(host) = UnixStream::connect(socket_path) else {
                continue;
            };
            thread::spawn(move || {
                let _ = tunnel_tcp_unix(inside, host);
            });
        }
    }
    drop(listener);
    Ok(())
}

pub fn proxy_url() -> String {
    format!("http://127.0.0.1:{INNER_PROXY_PORT}")
}

fn tunnel_unix_tcp(mut unix: UnixStream, mut tcp: TcpStream) -> io::Result<()> {
    let mut unix_read = unix.try_clone()?;
    let mut tcp_write = tcp.try_clone()?;
    let outbound = thread::spawn(move || io::copy(&mut unix_read, &mut tcp_write));
    io::copy(&mut tcp, &mut unix)?;
    outbound.join().unwrap_or(Ok(0))?;
    Ok(())
}

fn tunnel_tcp_unix(mut tcp: TcpStream, mut unix: UnixStream) -> io::Result<()> {
    let mut tcp_read = tcp.try_clone()?;
    let mut unix_write = unix.try_clone()?;
    let outbound = thread::spawn(move || io::copy(&mut tcp_read, &mut unix_write));
    io::copy(&mut unix, &mut tcp)?;
    outbound.join().unwrap_or(Ok(0))?;
    Ok(())
}
