use anyhow::{Context, Result};
use async_trait::async_trait;
use bytes::BytesMut;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{
    fs,
    io::{AsyncReadExt, AsyncWriteExt},
    sync::{mpsc, Mutex},
};
use webrtc::{
    data_channel::{DataChannel, DataChannelEvent, RTCDataChannelInit},
    peer_connection::{
        PeerConnection, PeerConnectionBuilder, PeerConnectionEventHandler, RTCConfiguration,
        RTCConfigurationBuilder, RTCIceGatheringState, RTCIceServer, RTCSessionDescription,
    },
};

const DATA_CHANNEL_CHUNK_BYTES: usize = 12 * 1024;
const DATA_CHANNEL_SEND_BUFFER_BYTES: usize = 1024 * 1024;
const PROGRESS_REPORT_BYTES: u64 = 256 * 1024;
const ICE_GATHER_TIMEOUT: Duration = Duration::from_secs(15);
const TRANSFER_TIMEOUT: Duration = Duration::from_secs(120);
const TRANSFER_ACK: &[u8] = b"ace-file-complete-v1";
const TRANSFER_NACK: &[u8] = b"ace-file-invalid-v1";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IceServerConfig {
    pub urls: Vec<String>,
    pub username: String,
    pub credential: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileTransferMetadata {
    pub transfer_id: String,
    pub file_id: String,
    pub name: String,
    pub mime_type: String,
    pub size: u64,
    pub sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub room_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client_message_id: Option<String>,
}

#[derive(Debug)]
pub enum TransferEvent {
    OfferReady {
        peer_id: String,
        transfer_id: String,
        sdp: String,
    },
    AnswerReady {
        peer_id: String,
        transfer_id: String,
        sdp: String,
    },
    Progress {
        peer_id: String,
        transfer_id: String,
        sent: u64,
        total: u64,
        incoming: bool,
    },
    Sent {
        peer_id: String,
        metadata: FileTransferMetadata,
    },
    Received {
        peer_id: String,
        metadata: FileTransferMetadata,
        path: PathBuf,
    },
    Failed {
        peer_id: String,
        transfer_id: String,
        message: String,
    },
}

#[derive(Clone)]
pub struct WebRtcTransfers {
    receive_dir: PathBuf,
    max_file_bytes: u64,
    configuration: RTCConfiguration,
    connections: Arc<Mutex<HashMap<String, Arc<dyn PeerConnection>>>>,
    event_tx: mpsc::Sender<TransferEvent>,
}

impl WebRtcTransfers {
    pub fn new(
        receive_dir: PathBuf,
        max_file_bytes: u64,
        event_tx: mpsc::Sender<TransferEvent>,
    ) -> Self {
        Self {
            receive_dir,
            max_file_bytes,
            configuration: rtc_configuration(),
            connections: Arc::new(Mutex::new(HashMap::new())),
            event_tx,
        }
    }

    #[cfg(test)]
    fn new_with_configuration(
        receive_dir: PathBuf,
        max_file_bytes: u64,
        event_tx: mpsc::Sender<TransferEvent>,
        configuration: RTCConfiguration,
    ) -> Self {
        Self {
            receive_dir,
            max_file_bytes,
            configuration,
            connections: Arc::new(Mutex::new(HashMap::new())),
            event_tx,
        }
    }

    pub async fn start_sender(
        &self,
        peer_id: String,
        metadata: FileTransferMetadata,
        source_path: PathBuf,
    ) -> Result<()> {
        validate_metadata(&metadata, self.max_file_bytes)?;
        validate_source_file(&source_path, &metadata).await?;

        let (gather_tx, mut gather_rx) = mpsc::channel(1);
        let peer_connection = build_peer_connection(
            Arc::new(GatherHandler { gather_tx }),
            self.configuration.clone(),
        )
        .await?;
        let data_channel = peer_connection
            .create_data_channel(
                &format!("ace-file-{}", metadata.transfer_id),
                Some(RTCDataChannelInit {
                    ordered: true,
                    ..Default::default()
                }),
            )
            .await
            .context("failed to create WebRTC file data channel")?;

        self.connections
            .lock()
            .await
            .insert(metadata.transfer_id.clone(), Arc::clone(&peer_connection));

        spawn_sender(
            data_channel,
            source_path,
            peer_id.clone(),
            metadata.clone(),
            self.event_tx.clone(),
        );

        let offer = peer_connection
            .create_offer(None)
            .await
            .context("failed to create WebRTC offer")?;
        peer_connection
            .set_local_description(offer)
            .await
            .context("failed to set local WebRTC offer")?;
        wait_for_ice_gathering(&mut gather_rx).await?;
        let offer = peer_connection
            .local_description()
            .await
            .context("WebRTC offer disappeared after ICE gathering")?;
        self.event_tx
            .send(TransferEvent::OfferReady {
                peer_id,
                transfer_id: metadata.transfer_id,
                sdp: serde_json::to_string(&offer).context("failed to encode WebRTC offer")?,
            })
            .await
            .context("failed to publish WebRTC offer")?;
        Ok(())
    }

    pub async fn start_receiver(
        &self,
        peer_id: String,
        metadata: FileTransferMetadata,
        offer_sdp: String,
    ) -> Result<()> {
        validate_metadata(&metadata, self.max_file_bytes)?;
        fs::create_dir_all(&self.receive_dir)
            .await
            .with_context(|| {
                format!(
                    "failed to create Nearby receive directory {}",
                    self.receive_dir.display()
                )
            })?;
        let part_path = self
            .receive_dir
            .join(format!("{}.part", metadata.transfer_id));
        let final_path = self
            .receive_dir
            .join(format!("{}-{}", metadata.transfer_id, metadata.name));
        let (gather_tx, mut gather_rx) = mpsc::channel(1);
        let handler = ReceiverHandler {
            gather_tx,
            event_tx: self.event_tx.clone(),
            peer_id: peer_id.clone(),
            metadata: metadata.clone(),
            part_path,
            final_path,
        };
        let peer_connection =
            build_peer_connection(Arc::new(handler), self.configuration.clone()).await?;
        self.connections
            .lock()
            .await
            .insert(metadata.transfer_id.clone(), Arc::clone(&peer_connection));

        let offer = serde_json::from_str::<RTCSessionDescription>(&offer_sdp)
            .context("failed to decode remote WebRTC offer")?;
        peer_connection
            .set_remote_description(offer)
            .await
            .context("failed to apply remote WebRTC offer")?;
        let answer = peer_connection
            .create_answer(None)
            .await
            .context("failed to create WebRTC answer")?;
        peer_connection
            .set_local_description(answer)
            .await
            .context("failed to set local WebRTC answer")?;
        wait_for_ice_gathering(&mut gather_rx).await?;
        let answer = peer_connection
            .local_description()
            .await
            .context("WebRTC answer disappeared after ICE gathering")?;
        self.event_tx
            .send(TransferEvent::AnswerReady {
                peer_id,
                transfer_id: metadata.transfer_id,
                sdp: serde_json::to_string(&answer).context("failed to encode WebRTC answer")?,
            })
            .await
            .context("failed to publish WebRTC answer")?;
        Ok(())
    }

    pub async fn apply_answer(&self, transfer_id: &str, answer_sdp: &str) -> Result<()> {
        let peer_connection = self
            .connections
            .lock()
            .await
            .get(transfer_id)
            .cloned()
            .with_context(|| format!("unknown WebRTC transfer {transfer_id}"))?;
        let answer = serde_json::from_str::<RTCSessionDescription>(answer_sdp)
            .context("failed to decode remote WebRTC answer")?;
        peer_connection
            .set_remote_description(answer)
            .await
            .context("failed to apply remote WebRTC answer")
    }

    pub async fn finish(&self, transfer_id: &str) {
        self.connections.lock().await.remove(transfer_id);
    }
}

#[derive(Clone)]
struct GatherHandler {
    gather_tx: mpsc::Sender<()>,
}

#[async_trait]
impl PeerConnectionEventHandler for GatherHandler {
    async fn on_ice_gathering_state_change(&self, state: RTCIceGatheringState) {
        if state == RTCIceGatheringState::Complete {
            let _ = self.gather_tx.try_send(());
        }
    }
}

#[derive(Clone)]
struct ReceiverHandler {
    gather_tx: mpsc::Sender<()>,
    event_tx: mpsc::Sender<TransferEvent>,
    peer_id: String,
    metadata: FileTransferMetadata,
    part_path: PathBuf,
    final_path: PathBuf,
}

#[async_trait]
impl PeerConnectionEventHandler for ReceiverHandler {
    async fn on_ice_gathering_state_change(&self, state: RTCIceGatheringState) {
        if state == RTCIceGatheringState::Complete {
            let _ = self.gather_tx.try_send(());
        }
    }

    async fn on_data_channel(&self, data_channel: Arc<dyn DataChannel>) {
        let event_tx = self.event_tx.clone();
        let peer_id = self.peer_id.clone();
        let metadata = self.metadata.clone();
        let part_path = self.part_path.clone();
        let final_path = self.final_path.clone();
        tokio::spawn(async move {
            let result = tokio::time::timeout(
                TRANSFER_TIMEOUT,
                receive_file(
                    data_channel,
                    &peer_id,
                    &metadata,
                    &part_path,
                    &final_path,
                    &event_tx,
                ),
            )
            .await;
            let error = match result {
                Ok(Ok(())) => return,
                Ok(Err(error)) => format!("{error:#}"),
                Err(_) => "WebRTC file receive timed out".to_owned(),
            };
            {
                let _ = fs::remove_file(&part_path).await;
                let _ = event_tx
                    .send(TransferEvent::Failed {
                        peer_id,
                        transfer_id: metadata.transfer_id,
                        message: error,
                    })
                    .await;
            }
        });
    }
}

async fn build_peer_connection(
    handler: Arc<dyn PeerConnectionEventHandler>,
    configuration: RTCConfiguration,
) -> Result<Arc<dyn PeerConnection>> {
    let peer_connection = PeerConnectionBuilder::new()
        .with_configuration(configuration)
        .with_handler(handler)
        .with_udp_addrs(vec!["0.0.0.0:0".to_owned()])
        .with_data_channel_send_buffer_limit(DATA_CHANNEL_SEND_BUFFER_BYTES)
        .build()
        .await
        .context("failed to build WebRTC peer connection")?;
    Ok(Arc::new(peer_connection) as Arc<dyn PeerConnection>)
}

fn spawn_sender(
    data_channel: Arc<dyn DataChannel>,
    source_path: PathBuf,
    peer_id: String,
    metadata: FileTransferMetadata,
    event_tx: mpsc::Sender<TransferEvent>,
) {
    tokio::spawn(async move {
        let result = tokio::time::timeout(
            TRANSFER_TIMEOUT,
            send_file(data_channel, &source_path, &peer_id, &metadata, &event_tx),
        )
        .await;
        let error = match result {
            Ok(Ok(())) => return,
            Ok(Err(error)) => format!("{error:#}"),
            Err(_) => "WebRTC file send timed out".to_owned(),
        };
        {
            let _ = event_tx
                .send(TransferEvent::Failed {
                    peer_id,
                    transfer_id: metadata.transfer_id,
                    message: error,
                })
                .await;
        }
    });
}

async fn send_file(
    data_channel: Arc<dyn DataChannel>,
    source_path: &Path,
    peer_id: &str,
    metadata: &FileTransferMetadata,
    event_tx: &mpsc::Sender<TransferEvent>,
) -> Result<()> {
    loop {
        match data_channel.poll().await {
            Some(DataChannelEvent::OnOpen) => break,
            Some(DataChannelEvent::OnClose | DataChannelEvent::OnError) | None => {
                anyhow::bail!("WebRTC file channel closed before opening")
            }
            _ => {}
        }
    }

    let mut file = fs::File::open(source_path)
        .await
        .with_context(|| format!("failed to open file {}", source_path.display()))?;
    let mut buffer = vec![0_u8; DATA_CHANNEL_CHUNK_BYTES];
    let mut hasher = Sha256::new();
    let mut sent = 0_u64;
    let mut last_reported = 0_u64;
    loop {
        let read = file
            .read(&mut buffer)
            .await
            .with_context(|| format!("failed to read file {}", source_path.display()))?;
        if read == 0 {
            break;
        }
        data_channel
            .send(BytesMut::from(&buffer[..read]))
            .await
            .context("failed to send WebRTC file chunk")?;
        hasher.update(&buffer[..read]);
        sent += read as u64;
        if sent == metadata.size || sent.saturating_sub(last_reported) >= PROGRESS_REPORT_BYTES {
            let _ = event_tx
                .send(TransferEvent::Progress {
                    peer_id: peer_id.to_owned(),
                    transfer_id: metadata.transfer_id.clone(),
                    sent,
                    total: metadata.size,
                    incoming: false,
                })
                .await;
            last_reported = sent;
        }
    }
    anyhow::ensure!(
        sent == metadata.size,
        "file changed while it was being sent"
    );
    anyhow::ensure!(
        format!("{:x}", hasher.finalize()) == metadata.sha256.to_ascii_lowercase(),
        "file content changed while it was being sent"
    );

    loop {
        match data_channel.poll().await {
            Some(DataChannelEvent::OnMessage(message)) if message.data.as_ref() == TRANSFER_ACK => {
                event_tx
                    .send(TransferEvent::Sent {
                        peer_id: peer_id.to_owned(),
                        metadata: metadata.clone(),
                    })
                    .await
                    .context("failed to publish completed file transfer")?;
                return Ok(());
            }
            Some(DataChannelEvent::OnMessage(message))
                if message.data.as_ref() == TRANSFER_NACK =>
            {
                anyhow::bail!("receiver rejected the file verification")
            }
            Some(DataChannelEvent::OnClose | DataChannelEvent::OnError) | None => {
                anyhow::bail!("WebRTC file channel closed before receiver verification")
            }
            _ => {}
        }
    }
}

async fn receive_file(
    data_channel: Arc<dyn DataChannel>,
    peer_id: &str,
    metadata: &FileTransferMetadata,
    part_path: &Path,
    final_path: &Path,
    event_tx: &mpsc::Sender<TransferEvent>,
) -> Result<()> {
    let mut file = fs::File::create(part_path)
        .await
        .with_context(|| format!("failed to create temporary file {}", part_path.display()))?;
    let mut hasher = Sha256::new();
    let mut received = 0_u64;
    let mut last_reported = 0_u64;
    while let Some(event) = data_channel.poll().await {
        match event {
            DataChannelEvent::OnOpen if metadata.size == 0 => {
                return complete_received_file(
                    data_channel,
                    peer_id,
                    metadata,
                    part_path,
                    final_path,
                    file,
                    hasher,
                    event_tx,
                )
                .await;
            }
            DataChannelEvent::OnMessage(message) => {
                received += message.data.len() as u64;
                if received > metadata.size {
                    let _ = data_channel.send(BytesMut::from(TRANSFER_NACK)).await;
                    anyhow::bail!("received more bytes than declared");
                }
                file.write_all(&message.data)
                    .await
                    .context("failed to write received file chunk")?;
                hasher.update(&message.data);
                if received == metadata.size
                    || received.saturating_sub(last_reported) >= PROGRESS_REPORT_BYTES
                {
                    let _ = event_tx
                        .send(TransferEvent::Progress {
                            peer_id: peer_id.to_owned(),
                            transfer_id: metadata.transfer_id.clone(),
                            sent: received,
                            total: metadata.size,
                            incoming: true,
                        })
                        .await;
                    last_reported = received;
                }
                if received == metadata.size {
                    return complete_received_file(
                        data_channel,
                        peer_id,
                        metadata,
                        part_path,
                        final_path,
                        file,
                        hasher,
                        event_tx,
                    )
                    .await;
                }
            }
            DataChannelEvent::OnClose | DataChannelEvent::OnError => {
                anyhow::bail!("WebRTC file channel closed before transfer completed")
            }
            _ => {}
        }
    }
    anyhow::bail!("WebRTC file channel ended before transfer completed")
}

#[allow(clippy::too_many_arguments)]
async fn complete_received_file(
    data_channel: Arc<dyn DataChannel>,
    peer_id: &str,
    metadata: &FileTransferMetadata,
    part_path: &Path,
    final_path: &Path,
    mut file: fs::File,
    hasher: Sha256,
    event_tx: &mpsc::Sender<TransferEvent>,
) -> Result<()> {
    file.flush()
        .await
        .context("failed to flush received file")?;
    drop(file);
    let actual_sha256 = format!("{:x}", hasher.finalize());
    if actual_sha256 != metadata.sha256.to_ascii_lowercase() {
        let _ = data_channel.send(BytesMut::from(TRANSFER_NACK)).await;
        anyhow::bail!("received file SHA-256 does not match");
    }
    if final_path.exists() {
        fs::remove_file(final_path)
            .await
            .with_context(|| format!("failed to replace received file {}", final_path.display()))?;
    }
    fs::rename(part_path, final_path)
        .await
        .with_context(|| format!("failed to finalize received file {}", final_path.display()))?;
    data_channel
        .send(BytesMut::from(TRANSFER_ACK))
        .await
        .context("failed to acknowledge received file")?;
    event_tx
        .send(TransferEvent::Received {
            peer_id: peer_id.to_owned(),
            metadata: metadata.clone(),
            path: final_path.to_path_buf(),
        })
        .await
        .context("failed to publish received file")?;
    Ok(())
}

async fn wait_for_ice_gathering(receiver: &mut mpsc::Receiver<()>) -> Result<()> {
    tokio::time::timeout(ICE_GATHER_TIMEOUT, receiver.recv())
        .await
        .context("timed out while gathering WebRTC connection candidates")?
        .context("WebRTC candidate gathering stopped unexpectedly")?;
    Ok(())
}

async fn validate_source_file(path: &Path, metadata: &FileTransferMetadata) -> Result<()> {
    let stat = fs::metadata(path)
        .await
        .with_context(|| format!("failed to inspect file {}", path.display()))?;
    anyhow::ensure!(
        stat.is_file(),
        "selected transfer source is not a regular file"
    );
    anyhow::ensure!(stat.len() == metadata.size, "selected file size changed");
    let actual_sha256 = hash_file(path).await?;
    anyhow::ensure!(
        actual_sha256 == metadata.sha256.to_ascii_lowercase(),
        "selected file SHA-256 changed"
    );
    Ok(())
}

async fn hash_file(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path)
        .await
        .with_context(|| format!("failed to open file {}", path.display()))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .await
            .with_context(|| format!("failed to read file {}", path.display()))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

pub fn validate_metadata(metadata: &FileTransferMetadata, max_file_bytes: u64) -> Result<()> {
    anyhow::ensure!(
        !metadata.transfer_id.is_empty()
            && metadata.transfer_id.len() <= 128
            && metadata
                .transfer_id
                .chars()
                .all(|character| character.is_ascii_alphanumeric()
                    || matches!(character, '_' | '-' | '.' | ':')),
        "invalid file transfer id"
    );
    anyhow::ensure!(
        !metadata.file_id.is_empty()
            && metadata.file_id.len() <= 128
            && metadata
                .file_id
                .chars()
                .all(|character| character.is_ascii_alphanumeric()
                    || matches!(character, '_' | '-' | '.' | ':')),
        "invalid file id"
    );
    anyhow::ensure!(
        !metadata.name.is_empty()
            && metadata.name.len() <= 255
            && metadata.name != "."
            && metadata.name != ".."
            && !metadata.name.contains('/')
            && !metadata.name.contains('\\')
            && Path::new(&metadata.name)
                .file_name()
                .and_then(|value| value.to_str())
                == Some(metadata.name.as_str()),
        "invalid file name"
    );
    anyhow::ensure!(
        !metadata.mime_type.is_empty() && metadata.mime_type.len() <= 200,
        "invalid file MIME type"
    );
    anyhow::ensure!(metadata.size <= max_file_bytes, "file is too large");
    anyhow::ensure!(
        metadata.sha256.len() == 64
            && metadata
                .sha256
                .chars()
                .all(|character| character.is_ascii_hexdigit()),
        "invalid file SHA-256"
    );
    Ok(())
}

pub fn ice_servers_from_env() -> Vec<IceServerConfig> {
    let stun_urls = std::env::var("ACE_NEARBY_STUN_URLS")
        .unwrap_or_else(|_| "stun:stun.l.google.com:19302".to_owned())
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let turn_urls = std::env::var("ACE_NEARBY_TURN_URLS")
        .unwrap_or_default()
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
        .collect::<Vec<_>>();
    let mut servers = Vec::new();
    if !stun_urls.is_empty() {
        servers.push(IceServerConfig {
            urls: stun_urls,
            username: String::new(),
            credential: String::new(),
        });
    }
    if !turn_urls.is_empty() {
        servers.push(IceServerConfig {
            urls: turn_urls,
            username: std::env::var("ACE_NEARBY_TURN_USERNAME").unwrap_or_default(),
            credential: std::env::var("ACE_NEARBY_TURN_CREDENTIAL").unwrap_or_default(),
        });
    }
    servers
}

pub fn rtc_configuration() -> RTCConfiguration {
    let servers = ice_servers_from_env()
        .into_iter()
        .map(|server| RTCIceServer {
            urls: server.urls,
            username: server.username,
            credential: server.credential,
        })
        .collect();
    RTCConfigurationBuilder::default()
        .with_ice_servers(servers)
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn metadata(name: &str) -> FileTransferMetadata {
        FileTransferMetadata {
            transfer_id: "transfer-1".to_owned(),
            file_id: "file-1".to_owned(),
            name: name.to_owned(),
            mime_type: "application/octet-stream".to_owned(),
            size: 4,
            sha256: "a".repeat(64),
            room_id: None,
            client_message_id: None,
        }
    }

    #[test]
    fn metadata_rejects_path_components() {
        assert!(validate_metadata(&metadata("../secret.txt"), 1024).is_err());
        assert!(validate_metadata(&metadata("folder/file.txt"), 1024).is_err());
        assert!(validate_metadata(&metadata("folder\\file.txt"), 1024).is_err());
    }

    #[test]
    fn metadata_accepts_plain_file_name() {
        assert!(validate_metadata(&metadata("report.pdf"), 1024).is_ok());
    }

    #[test]
    fn metadata_rejects_invalid_mime_type_and_file_id() {
        let mut value = metadata("report.pdf");
        value.mime_type.clear();
        assert!(validate_metadata(&value, 1024).is_err());
        value.mime_type = "application/pdf".to_owned();
        value.file_id = "文件".to_owned();
        assert!(validate_metadata(&value, 1024).is_err());
    }

    #[test]
    fn default_ice_configuration_has_stun() {
        let servers = ice_servers_from_env();
        assert!(!servers.is_empty());
        assert!(servers.iter().all(|server| !server.urls.is_empty()));
    }

    async fn assert_local_data_channel_transfer(contents: &[u8]) {
        let test_root =
            std::env::temp_dir().join(format!("ace-webrtc-test-{}", uuid::Uuid::new_v4()));
        let receive_dir = test_root.join("received");
        fs::create_dir_all(&test_root).await.unwrap();
        let source_path = test_root.join("source.txt");
        fs::write(&source_path, contents).await.unwrap();
        let metadata = FileTransferMetadata {
            transfer_id: "transfer-local-1".to_owned(),
            file_id: "file-local-1".to_owned(),
            name: "source.txt".to_owned(),
            mime_type: "text/plain".to_owned(),
            size: contents.len() as u64,
            sha256: format!("{:x}", Sha256::digest(contents)),
            room_id: None,
            client_message_id: None,
        };
        let configuration = RTCConfigurationBuilder::default().build();
        let (sender_tx, mut sender_rx) = mpsc::channel(32);
        let (receiver_tx, mut receiver_rx) = mpsc::channel(32);
        let sender = WebRtcTransfers::new_with_configuration(
            test_root.join("unused"),
            1024,
            sender_tx,
            configuration.clone(),
        );
        let receiver =
            WebRtcTransfers::new_with_configuration(receive_dir, 1024, receiver_tx, configuration);

        sender
            .start_sender("receiver".to_owned(), metadata.clone(), source_path)
            .await
            .unwrap();
        let offer = match sender_rx.recv().await.unwrap() {
            TransferEvent::OfferReady { sdp, .. } => sdp,
            event => panic!("expected offer, got {event:?}"),
        };
        receiver
            .start_receiver("sender".to_owned(), metadata.clone(), offer)
            .await
            .unwrap();
        let answer = match receiver_rx.recv().await.unwrap() {
            TransferEvent::AnswerReady { sdp, .. } => sdp,
            event => panic!("expected answer, got {event:?}"),
        };
        sender
            .apply_answer(&metadata.transfer_id, &answer)
            .await
            .unwrap();

        let received_path = tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                match receiver_rx.recv().await.unwrap() {
                    TransferEvent::Received { path, .. } => break path,
                    TransferEvent::Failed { message, .. } => panic!("receive failed: {message}"),
                    _ => {}
                }
            }
        })
        .await
        .expect("local WebRTC receive timed out");
        tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                match sender_rx.recv().await.unwrap() {
                    TransferEvent::Sent { .. } => break,
                    TransferEvent::Failed { message, .. } => panic!("send failed: {message}"),
                    _ => {}
                }
            }
        })
        .await
        .expect("local WebRTC send timed out");

        assert_eq!(fs::read(received_path).await.unwrap(), contents);
        sender.finish(&metadata.transfer_id).await;
        receiver.finish(&metadata.transfer_id).await;
        fs::remove_dir_all(test_root).await.unwrap();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn transfers_a_file_over_a_local_data_channel() {
        assert_local_data_channel_transfer(b"nearby data channel transfer").await;
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn transfers_an_empty_file_over_a_local_data_channel() {
        assert_local_data_channel_transfer(b"").await;
    }
}
