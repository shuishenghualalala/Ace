use crate::file_transfer::{
    validate_metadata, FileTransferMetadata, TransferEvent, WebRtcTransfers,
};
use crate::identity::{
    default_agent_name, default_display_name, load_nearby_settings, load_or_create_peer_id,
    resolve_state_dir, save_nearby_settings, NearbySettings,
};
#[cfg(not(target_os = "linux"))]
use crate::protocol::should_initiate;
use crate::protocol::{
    is_valid_agent_mode, normalize_room_name, FrameCodec, Message, PeerInfo, PublishedAgent,
    Reassembler, ReplyReference, TransferredFile, DEFAULT_AGENT_MODE, FILE_WEBRTC_CAPABILITY,
    INCOMING_MESSAGE_UUID, OUTGOING_MESSAGE_UUID, PEER_INFO_UUID, PROTOCOL_VERSION, SERVICE_UUID,
};
use crate::runtime::{subscribe_outgoing_message, NearbyConfig};
use anyhow::{Context, Result};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use ble_peripheral_rust::{
    gatt::{
        characteristic::Characteristic as ServerCharacteristic,
        peripheral_event::{
            PeripheralEvent, ReadRequestResponse, RequestResponse, WriteRequestResponse,
        },
        properties::{AttributePermission, CharacteristicProperty},
        service::Service,
    },
    Peripheral as ServerPeripheral, PeripheralImpl,
};
use btleplug::{
    api::{
        Central, CentralEvent, CharPropFlags, Manager as _, Peripheral as _, ScanFilter, WriteType,
    },
    platform::{Manager, Peripheral},
};
use futures::StreamExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    env, fs,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{self, AsyncBufReadExt, AsyncWriteExt, BufReader, BufWriter},
    sync::{mpsc, Mutex},
};
use uuid::Uuid;

const MAX_NEARBY_FILE_BYTES: u64 = 4 * 1024 * 1024;
const ROOM_HISTORY_LIMIT: usize = 200;
const DM_HISTORY_LIMIT: usize = 200;
const ROOMS_FILE_NAME: &str = "rooms.json";
const DMS_FILE_NAME: &str = "dms.json";

fn diagnostic_device_id(value: &str) -> String {
    let suffix = value.chars().rev().take(8).collect::<String>();
    format!("…{}", suffix.chars().rev().collect::<String>())
}

fn diagnostic_characteristics(peripheral: &Peripheral) -> String {
    let mut uuids = peripheral
        .characteristics()
        .into_iter()
        .map(|characteristic| format!("{}({:?})", characteristic.uuid, characteristic.properties))
        .collect::<Vec<_>>();
    uuids.sort_unstable();
    uuids.join(",")
}

fn incoming_write_type(characteristic: &btleplug::api::Characteristic) -> Result<WriteType> {
    if characteristic.properties.contains(CharPropFlags::WRITE) {
        Ok(WriteType::WithResponse)
    } else if characteristic
        .properties
        .contains(CharPropFlags::WRITE_WITHOUT_RESPONSE)
    {
        Ok(WriteType::WithoutResponse)
    } else {
        anyhow::bail!("remote IncomingMessage characteristic is not writable")
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(crate) enum IpcCommand {
    StartDiscovery,
    StopDiscovery,
    SetDiscoverable {
        enabled: bool,
    },
    ConnectPeer {
        peer_id: String,
    },
    DisconnectPeer {
        peer_id: String,
    },
    SendAgentRequest {
        peer_id: String,
        text: String,
    },
    SendAgentReply {
        peer_id: String,
        request_id: String,
        text: String,
        #[serde(default)]
        error: bool,
    },
    SendPeerMessage {
        peer_id: String,
        text: String,
        #[serde(default)]
        client_message_id: Option<String>,
        #[serde(default)]
        mentions: Vec<String>,
    },
    SendPeerFile {
        peer_id: String,
        file_id: String,
        name: String,
        mime_type: String,
        size: u64,
        sha256: String,
        file_path: String,
        #[serde(default)]
        client_message_id: Option<String>,
    },
    CreateRoom {
        room_id: String,
        room_name: String,
        peer_ids: Vec<String>,
        #[serde(default)]
        agent_mode: Option<String>,
    },
    InviteToRoom {
        room_id: String,
        peer_ids: Vec<String>,
    },
    SendRoomMessage {
        room_id: String,
        text: String,
        #[serde(default)]
        client_message_id: Option<String>,
        #[serde(default)]
        mentions: Vec<String>,
        #[serde(default)]
        reply_to: Option<ReplyReference>,
    },
    SendRoomFile {
        room_id: String,
        file_id: String,
        name: String,
        mime_type: String,
        size: u64,
        sha256: String,
        file_path: String,
        #[serde(default)]
        client_message_id: Option<String>,
        #[serde(default)]
        mentions: Vec<String>,
        #[serde(default)]
        reply_to: Option<ReplyReference>,
    },
    RespondFileTransfer {
        transfer_id: String,
        accepted: bool,
    },
    LeaveRoom {
        room_id: String,
    },
    SetRoomAgentMode {
        room_id: String,
        #[serde(default)]
        agent_mode: Option<String>,
        #[serde(default)]
        room_name: Option<String>,
    },
    Shutdown,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(crate) enum IpcEvent {
    Ready {
        peer: PeerInfo,
        discoverable: bool,
    },
    DiscoveryStarted,
    DiscoveryStopped,
    DiscoverabilityChanged {
        discoverable: bool,
    },
    PeerDiscovered {
        peer: PeerInfo,
    },
    PeerConnected {
        peer: PeerInfo,
    },
    PeerDisconnected {
        peer_id: String,
    },
    PeerUnavailable {
        peer_id: String,
    },
    PeerConnectionFailed {
        peer_id: String,
        message: String,
    },
    PeerMessageReceived {
        peer_id: String,
        display_name: String,
        text: String,
        mentions: Vec<String>,
        message_id: String,
        timestamp: u64,
    },
    MessageDelivered {
        peer_id: String,
        message_id: String,
    },
    RoomCreated {
        room_id: String,
        room_name: String,
        peer_ids: Vec<String>,
        agent_mode: String,
        owner_peer_id: Option<String>,
    },
    RoomJoined {
        room_id: String,
        room_name: String,
        peer_ids: Vec<String>,
        agent_mode: String,
        owner_peer_id: Option<String>,
    },
    RoomRestored {
        room_id: String,
        room_name: String,
        peer_ids: Vec<String>,
        agent_mode: String,
        owner_peer_id: Option<String>,
        messages: Vec<Message>,
    },
    RoomSettingsUpdated {
        room_id: String,
        agent_mode: String,
        room_name: String,
    },
    RoomMemberJoined {
        room_id: String,
        peer_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        display_name: Option<String>,
    },
    RoomMemberLeft {
        room_id: String,
        peer_id: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        display_name: Option<String>,
    },
    RoomLeft {
        room_id: String,
    },
    HistorySnapshot {
        rooms: Vec<RoomSnapshot>,
        dms: Vec<DmSnapshot>,
    },
    Message {
        peer_id: String,
        message: Message,
    },
    FileTransferRequested {
        peer_id: String,
        transfer: FileTransferMetadata,
    },
    FileTransferProgress {
        peer_id: String,
        transfer_id: String,
        sent: u64,
        total: u64,
        incoming: bool,
    },
    FileTransferFailed {
        peer_id: String,
        transfer_id: String,
        message: String,
    },
    Error {
        message: String,
    },
}

#[derive(Debug, Clone)]
struct PendingOutgoingTransfer {
    peer_id: String,
    metadata: FileTransferMetadata,
    source_path: PathBuf,
}

#[derive(Debug, Clone)]
struct PendingIncomingTransfer {
    peer_id: String,
    metadata: FileTransferMetadata,
    accepted: bool,
}

fn default_agent_mode() -> String {
    DEFAULT_AGENT_MODE.to_owned()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct RoomState {
    pub(crate) room_name: String,
    pub(crate) peer_ids: HashSet<String>,
    #[serde(default = "default_agent_mode")]
    pub(crate) agent_mode: String,
    #[serde(default)]
    pub(crate) owner_peer_id: Option<String>,
    #[serde(default)]
    pub(crate) messages: Vec<Message>,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct RoomSnapshot {
    pub(crate) room_id: String,
    pub(crate) room_name: String,
    pub(crate) agent_mode: String,
    pub(crate) owner_peer_id: Option<String>,
    pub(crate) peer_ids: Vec<String>,
    pub(crate) messages: Vec<Message>,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct DmSnapshot {
    pub(crate) peer_id: String,
    pub(crate) messages: Vec<Message>,
}

#[cfg(target_os = "linux")]
const USE_PASSIVE_SESSIONS: bool = false;
#[cfg(not(target_os = "linux"))]
const USE_PASSIVE_SESSIONS: bool = true;

#[derive(Debug)]
enum SessionEvent {
    Discovered(PeerInfo),
    Ready {
        peer: PeerInfo,
        session_id: String,
        outbound: mpsc::Sender<Message>,
    },
    Received {
        peer_id: String,
        message: Message,
    },
    Closed {
        peer_id: String,
        session_id: String,
    },
    Failed {
        peer_id: String,
        error: String,
    },
}

#[derive(Clone)]
pub(crate) struct EventSink(Arc<Mutex<BufWriter<io::Stdout>>>);

impl EventSink {
    pub(crate) fn stdout() -> Self {
        Self(Arc::new(Mutex::new(BufWriter::new(io::stdout()))))
    }

    pub(crate) async fn send(&self, event: IpcEvent) -> Result<()> {
        let line = serde_json::to_string(&event).context("failed to encode Nearby IPC event")?;
        let mut output = self.0.lock().await;
        output
            .write_all(line.as_bytes())
            .await
            .context("failed to write Nearby IPC event")?;
        output
            .write_all(b"\n")
            .await
            .context("failed to terminate Nearby IPC event")?;
        output
            .flush()
            .await
            .context("failed to flush Nearby IPC event")?;
        Ok(())
    }
}

fn rooms_path(state_dir: &Path) -> std::path::PathBuf {
    state_dir.join(ROOMS_FILE_NAME)
}

fn dms_path(state_dir: &Path) -> std::path::PathBuf {
    state_dir.join(DMS_FILE_NAME)
}

pub(crate) fn load_rooms(state_dir: &Path) -> Result<HashMap<String, RoomState>> {
    let path = rooms_path(state_dir);
    if !path.exists() {
        return Ok(HashMap::new());
    }
    let bytes = fs::read(&path)
        .with_context(|| format!("failed to read Nearby room history {}", path.display()))?;
    serde_json::from_slice(&bytes)
        .with_context(|| format!("failed to decode Nearby room history {}", path.display()))
}

pub(crate) fn load_dms(state_dir: &Path) -> Result<HashMap<String, Vec<Message>>> {
    let path = dms_path(state_dir);
    if !path.exists() {
        return Ok(HashMap::new());
    }
    let bytes = fs::read(&path)
        .with_context(|| format!("failed to read Nearby DM history {}", path.display()))?;
    serde_json::from_slice(&bytes)
        .with_context(|| format!("failed to decode Nearby DM history {}", path.display()))
}

fn save_state_file<T: Serialize>(state_dir: &Path, file_name: &str, value: &T) -> Result<()> {
    fs::create_dir_all(state_dir).with_context(|| {
        format!(
            "failed to create Nearby state directory {}",
            state_dir.display()
        )
    })?;
    let path = state_dir.join(file_name);
    let temporary_path = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(value).context("failed to encode Nearby state")?;
    fs::write(&temporary_path, bytes).with_context(|| {
        format!(
            "failed to write temporary Nearby state {}",
            temporary_path.display()
        )
    })?;
    if let Err(error) = fs::rename(&temporary_path, &path) {
        if path.exists() {
            fs::remove_file(&path)
                .with_context(|| format!("failed to replace Nearby state {}", path.display()))?;
            fs::rename(&temporary_path, &path)
                .with_context(|| format!("failed to finalize Nearby state {}", path.display()))?;
        } else {
            return Err(error)
                .with_context(|| format!("failed to finalize Nearby state {}", path.display()));
        }
    }
    Ok(())
}

pub(crate) fn save_rooms(state_dir: &Path, rooms: &HashMap<String, RoomState>) -> Result<()> {
    save_state_file(state_dir, ROOMS_FILE_NAME, rooms)
}

pub(crate) fn save_dms(state_dir: &Path, dms: &HashMap<String, Vec<Message>>) -> Result<()> {
    save_state_file(state_dir, DMS_FILE_NAME, dms)
}

pub(crate) fn remember_room_message(room: &mut RoomState, message: &Message) {
    room.messages.push(message.clone());
    if room.messages.len() > ROOM_HISTORY_LIMIT {
        let excess = room.messages.len() - ROOM_HISTORY_LIMIT;
        room.messages.drain(0..excess);
    }
}

pub(crate) fn remember_dm_message(
    dms: &mut HashMap<String, Vec<Message>>,
    peer_id: &str,
    message: &Message,
) {
    let history = dms.entry(peer_id.to_owned()).or_default();
    history.push(message.clone());
    if history.len() > DM_HISTORY_LIMIT {
        let excess = history.len() - DM_HISTORY_LIMIT;
        history.drain(0..excess);
    }
}

pub(crate) fn unix_timestamp_seconds() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

pub(crate) fn build_history_snapshot(
    rooms: &HashMap<String, RoomState>,
    dms: &HashMap<String, Vec<Message>>,
) -> IpcEvent {
    IpcEvent::HistorySnapshot {
        rooms: rooms
            .iter()
            .map(|(room_id, room)| RoomSnapshot {
                room_id: room_id.clone(),
                room_name: room.room_name.clone(),
                agent_mode: room.agent_mode.clone(),
                owner_peer_id: room.owner_peer_id.clone(),
                peer_ids: room.peer_ids.iter().cloned().collect(),
                messages: room.messages.clone(),
            })
            .collect(),
        dms: dms
            .iter()
            .map(|(peer_id, messages)| DmSnapshot {
                peer_id: peer_id.clone(),
                messages: messages.clone(),
            })
            .collect(),
    }
}

pub async fn run(config: NearbyConfig) -> Result<()> {
    crate::transport::run(config).await
}

pub(crate) async fn run_ble(config: NearbyConfig) -> Result<()> {
    let state_dir = resolve_state_dir(config.state_dir.as_deref());
    let mut discoverable = config
        .discoverable
        .unwrap_or(load_nearby_settings(&state_dir)?.discoverable);
    let peer = create_peer(&config)?;
    eprintln!(
        "[nearby][startup] mode=ipc os={} arch={} peer_id={} discoverable={} service_uuid={}",
        env::consts::OS,
        env::consts::ARCH,
        peer.peer_id,
        discoverable,
        SERVICE_UUID
    );
    let sink = EventSink(Arc::new(Mutex::new(BufWriter::new(io::stdout()))));
    let (server_event_tx, mut server_event_rx) = mpsc::channel(256);
    eprintln!("[nearby][peripheral] initializing GATT server");
    let server = ServerPeripheral::new(server_event_tx)
        .await
        .context("failed to initialize BLE peripheral")?;
    let server = Arc::new(Mutex::new(server));
    configure_server(&server, &peer).await?;
    set_discoverable(&server, &peer.display_name, discoverable).await?;

    let manager = Manager::new()
        .await
        .context("failed to initialize BLE manager")?;
    let adapters = manager
        .adapters()
        .await
        .context("failed to enumerate BLE adapters")?;
    eprintln!(
        "[nearby][central] adapters_found={} requested_service={}",
        adapters.len(),
        SERVICE_UUID
    );
    let adapter = adapters
        .into_iter()
        .next()
        .context("no BLE adapter was found")?;
    let adapter_info = adapter
        .adapter_info()
        .await
        .unwrap_or_else(|error| format!("unavailable: {error}"));
    eprintln!("[nearby][central] adapter_selected={adapter_info}");
    let mut central_events = adapter
        .events()
        .await
        .context("failed to subscribe to BLE adapter events")?;
    adapter
        .start_scan(ScanFilter {
            services: vec![SERVICE_UUID],
        })
        .await
        .context("failed to start BLE scanning")?;
    eprintln!("[nearby][central] scanning_started");

    let (session_event_tx, mut session_event_rx) = mpsc::channel(128);
    let mut stdin = BufReader::new(io::stdin()).lines();
    let mut sessions: HashMap<String, mpsc::Sender<Message>> = HashMap::new();
    let mut session_ids: HashMap<String, String> = HashMap::new();
    let mut discovered: HashMap<String, PeerInfo> = HashMap::new();
    let mut active_peers = HashSet::new();
    let mut pending_peer_connections = HashSet::new();
    let mut rooms = load_rooms(&state_dir)?;
    let mut dms = load_dms(&state_dir)?;
    let mut connection_candidates = HashSet::new();
    let scan_lock = Arc::new(Mutex::new(()));
    let mut server_clients: HashMap<String, (String, String)> = HashMap::new();
    let mut server_reassemblers: HashMap<String, Reassembler> = HashMap::new();
    let mut seen_messages = HashSet::new();
    let (transfer_event_tx, mut transfer_event_rx) = mpsc::channel(256);
    let transfers = WebRtcTransfers::new(
        state_dir.join("received"),
        MAX_NEARBY_FILE_BYTES,
        transfer_event_tx.clone(),
    );
    let mut outgoing_transfers: HashMap<String, PendingOutgoingTransfer> = HashMap::new();
    let mut incoming_transfers: HashMap<String, PendingIncomingTransfer> = HashMap::new();

    sink.send(IpcEvent::Ready {
        peer: peer.clone(),
        discoverable,
    })
    .await?;
    sink.send(IpcEvent::DiscoveryStarted).await?;
    sink.send(build_history_snapshot(&rooms, &dms)).await?;
    let restored_rooms = rooms
        .iter()
        .map(|(room_id, room)| IpcEvent::RoomRestored {
            room_id: room_id.clone(),
            room_name: room.room_name.clone(),
            peer_ids: room.peer_ids.iter().cloned().collect(),
            agent_mode: room.agent_mode.clone(),
            owner_peer_id: room.owner_peer_id.clone(),
            messages: room.messages.clone(),
        })
        .collect::<Vec<_>>();
    for event in restored_rooms {
        sink.send(event).await?;
    }

    loop {
        tokio::select! {
            line = stdin.next_line() => {
                let line = line.context("failed to read Nearby IPC command")?;
                let Some(line) = line else { break; };
                if line.trim().is_empty() { continue; }
                let command = match serde_json::from_str::<IpcCommand>(&line) {
                    Ok(command) => command,
                    Err(error) => {
                        eprintln!("[nearby][ipc] invalid_command error={error}");
                        sink.send(IpcEvent::Error { message: format!("invalid IPC command: {error}") }).await?;
                        continue;
                    }
                };
                eprintln!("[nearby][ipc] command={}", command_name(&command));
                match command {
                    IpcCommand::StartDiscovery => {
                        if let Err(error) = adapter.start_scan(ScanFilter { services: vec![SERVICE_UUID] }).await {
                            eprintln!("[nearby][central] scanning_restart_failed error={error}");
                            sink.send(IpcEvent::Error { message: format!("failed to start discovery: {error}") }).await?;
                        } else {
                            eprintln!("[nearby][central] scanning_started");
                            sink.send(IpcEvent::DiscoveryStarted).await?;
                        }
                    }
                    IpcCommand::StopDiscovery => {
                        if let Err(error) = adapter.stop_scan().await {
                            eprintln!("[nearby][central] scanning_stop_failed error={error}");
                            sink.send(IpcEvent::Error { message: format!("failed to stop discovery: {error}") }).await?;
                        } else {
                            eprintln!("[nearby][central] scanning_stopped");
                            sink.send(IpcEvent::DiscoveryStopped).await?;
                        }
                    }
                    IpcCommand::SetDiscoverable { enabled } => {
                        let previous = discoverable;
                        if let Err(error) = set_discoverable(&server, &peer.display_name, enabled).await {
                            eprintln!("[nearby][peripheral] discoverability_change_failed enabled={enabled} error={error}");
                            sink.send(IpcEvent::Error { message: format!("failed to update discoverability: {error}") }).await?;
                        } else if let Err(error) = save_nearby_settings(&state_dir, &NearbySettings { discoverable: enabled }) {
                            eprintln!("[nearby][peripheral] discoverability_persist_failed enabled={enabled} error={error}");
                            let rollback = set_discoverable(&server, &peer.display_name, previous).await;
                            let message = match rollback {
                                Ok(()) => format!("failed to persist discoverability: {error}"),
                                Err(rollback_error) => format!("failed to persist discoverability: {error}; rollback failed: {rollback_error}"),
                            };
                            sink.send(IpcEvent::Error { message }).await?;
                        } else {
                            discoverable = enabled;
                            sink.send(IpcEvent::DiscoverabilityChanged { discoverable: enabled }).await?;
                        }
                    }
                    IpcCommand::ConnectPeer { peer_id } => {
                        let Some(remote) = discovered.get(&peer_id).cloned() else {
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "附近已找不到这台 Ace".to_owned(),
                            }).await?;
                            continue;
                        };
                        if active_peers.contains(&peer_id) {
                            sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                            continue;
                        }
                        pending_peer_connections.insert(peer_id.clone());
                        if let Some(outbound) = sessions.get(&peer_id).cloned() {
                            if outbound.send(Message::peer_connect(peer.peer_id.clone())).await.is_err() {
                                pending_peer_connections.remove(&peer_id);
                                sink.send(IpcEvent::PeerConnectionFailed {
                                    peer_id,
                                    message: "BLE 会话已经断开，请重新查找".to_owned(),
                                }).await?;
                                continue;
                            }
                            pending_peer_connections.remove(&peer_id);
                            active_peers.insert(peer_id);
                            sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                        }
                    }
                    IpcCommand::DisconnectPeer { peer_id } => {
                        pending_peer_connections.remove(&peer_id);
                        if active_peers.remove(&peer_id) {
                            if let Some(outbound) = sessions.get(&peer_id).cloned() {
                                outbound.send(Message::peer_disconnect(peer.peer_id.clone())).await.ok();
                            }
                            sink.send(IpcEvent::PeerDisconnected { peer_id }).await?;
                        }
                    }
                    IpcCommand::SendAgentRequest { peer_id, text } => {
                        let text = text.trim();
                        if text.is_empty() || text.chars().count() > 8_000 {
                            sink.send(IpcEvent::Error { message: "消息不能为空且不能超过 8000 个字符".to_owned() }).await?;
                            continue;
                        }
                        if !active_peers.contains(&peer_id) {
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "请先连接这台 Ace".to_owned(),
                            }).await?;
                            continue;
                        }
                        let Some(outbound) = sessions.get(&peer_id).cloned() else {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "BLE 会话已经断开，请重新连接".to_owned(),
                            }).await?;
                            continue;
                        };
                        let message = Message::agent_request(peer.peer_id.clone(), text);
                        if outbound.send(message.clone()).await.is_err() {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "消息发送失败，BLE 会话已经断开".to_owned(),
                            }).await?;
                            continue;
                        }
                        remember_dm_message(&mut dms, &peer_id, &message);
                        save_dms(&state_dir, &dms)?;
                        sink.send(IpcEvent::Message {
                            peer_id,
                            message,
                        }).await?;
                    }
                    IpcCommand::SendAgentReply { peer_id, request_id, text, error } => {
                        let text = text.trim();
                        if request_id.trim().is_empty() || text.is_empty() || text.chars().count() > 8_000 {
                            sink.send(IpcEvent::Error { message: "Agent 回复无效或超过 8000 个字符".to_owned() }).await?;
                            continue;
                        }
                        if !active_peers.contains(&peer_id) {
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "无法回复：对方已经断开".to_owned(),
                            }).await?;
                            continue;
                        }
                        let Some(outbound) = sessions.get(&peer_id).cloned() else {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "无法回复：BLE 会话已经断开".to_owned(),
                            }).await?;
                            continue;
                        };
                        let message = Message::agent_reply(peer.peer_id.clone(), request_id, text, error);
                        if outbound.send(message.clone()).await.is_err() {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "回复发送失败，BLE 会话已经断开".to_owned(),
                            }).await?;
                            continue;
                        }
                        remember_dm_message(&mut dms, &peer_id, &message);
                        save_dms(&state_dir, &dms)?;
                        sink.send(IpcEvent::Message { peer_id, message }).await?;
                    }
                    IpcCommand::SendPeerMessage { peer_id, text, client_message_id, mentions } => {
                        let text = text.trim();
                        if text.is_empty() || text.chars().count() > 8_000 {
                            sink.send(IpcEvent::Error { message: "消息不能为空且不能超过 8000 个字符".to_owned() }).await?;
                            continue;
                        }
                        if !active_peers.contains(&peer_id) {
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "请先连接这台 Ace".to_owned(),
                            }).await?;
                            continue;
                        }
                        let Some(outbound) = sessions.get(&peer_id).cloned() else {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "BLE 会话已经断开，请重新连接".to_owned(),
                            }).await?;
                            continue;
                        };
                        let message = Message::peer_message(peer.peer_id.clone(), text, mentions)
                            .with_client_message_id(client_message_id);
                        if outbound.send(message.clone()).await.is_err() {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "消息发送失败，BLE 会话已经断开".to_owned(),
                            }).await?;
                            continue;
                        }
                        remember_dm_message(&mut dms, &peer_id, &message);
                        save_dms(&state_dir, &dms)?;
                        sink.send(IpcEvent::Message { peer_id, message }).await?;
                    }
                    IpcCommand::SendPeerFile { peer_id, file_id, name, mime_type, size, sha256, file_path, client_message_id } => {
                        if !active_peers.contains(&peer_id) {
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "请先连接这台 Ace".to_owned(),
                            }).await?;
                            continue;
                        }
                        if !discovered
                            .get(&peer_id)
                            .is_some_and(peer_supports_webrtc_file)
                        {
                            sink.send(IpcEvent::Error {
                                message: "对方版本不支持快速文件传输，请更新对方的 Ace".to_owned(),
                            }).await?;
                            continue;
                        }
                        let Some(outbound) = sessions.get(&peer_id).cloned() else {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "BLE 会话已经断开，请重新连接".to_owned(),
                            }).await?;
                            continue;
                        };
                        let source_path = PathBuf::from(file_path);
                        if !source_path.is_absolute() {
                            sink.send(IpcEvent::Error { message: "文件路径必须是绝对路径".to_owned() }).await?;
                            continue;
                        }
                        let metadata = FileTransferMetadata {
                            transfer_id: Uuid::new_v4().to_string(),
                            file_id,
                            name,
                            mime_type,
                            size,
                            sha256: sha256.to_ascii_lowercase(),
                            room_id: None,
                            client_message_id,
                        };
                        if let Err(error) = validate_metadata(&metadata, MAX_NEARBY_FILE_BYTES) {
                            sink.send(IpcEvent::Error { message: error.to_string() }).await?;
                            continue;
                        }
                        let offer = Message::file_offer(peer.peer_id.clone(), metadata.clone());
                        outgoing_transfers.insert(metadata.transfer_id.clone(), PendingOutgoingTransfer {
                            peer_id: peer_id.clone(),
                            metadata: metadata.clone(),
                            source_path,
                        });
                        if outbound.send(offer).await.is_err() {
                            outgoing_transfers.remove(&metadata.transfer_id);
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed {
                                peer_id,
                                message: "文件请求发送失败，BLE 会话已经断开".to_owned(),
                            }).await?;
                        } else {
                            sink.send(IpcEvent::FileTransferProgress {
                                peer_id,
                                transfer_id: metadata.transfer_id,
                                sent: 0,
                                total: metadata.size,
                                incoming: false,
                            }).await?;
                        }
                    }
                    IpcCommand::CreateRoom { room_id, room_name, peer_ids, agent_mode } => {
                        let agent_mode = agent_mode.unwrap_or_else(|| DEFAULT_AGENT_MODE.to_owned());
                        if !is_valid_agent_mode(&agent_mode) {
                            sink.send(IpcEvent::Error { message: "无效的 Agent 触发模式".to_owned() }).await?;
                            continue;
                        }
                        // Re-creating an existing room must not wipe its history; merge instead.
                        let existed = rooms.contains_key(&room_id);
                        if !existed {
                            let mut participants = HashSet::new();
                            participants.insert(peer.peer_id.clone());
                            rooms.insert(room_id.clone(), RoomState {
                                room_name: room_name.clone(),
                                peer_ids: participants,
                                agent_mode: agent_mode.clone(),
                                owner_peer_id: Some(peer.peer_id.clone()),
                                messages: Vec::new(),
                            });
                        }
                        let added = add_room_members(&sessions, &mut rooms, &peer.peer_id, &room_id, peer_ids).await;
                        if added.is_empty() && !existed {
                            rooms.remove(&room_id);
                            sink.send(IpcEvent::Error { message: "没有可用的同伴连接".to_owned() }).await?;
                            continue;
                        }
                        save_rooms(&state_dir, &rooms)?;
                        let room = rooms.get(&room_id).expect("room was just inserted");
                        sink.send(IpcEvent::RoomCreated {
                            room_id,
                            room_name: room.room_name.clone(),
                            peer_ids: room.peer_ids.iter().cloned().collect(),
                            agent_mode: room.agent_mode.clone(),
                            owner_peer_id: room.owner_peer_id.clone(),
                        }).await?;
                    }
                    IpcCommand::InviteToRoom { room_id, peer_ids } => {
                        let Some(room) = rooms.get(&room_id) else {
                            sink.send(IpcEvent::Error { message: "群聊不存在或已退出".to_owned() }).await?;
                            continue;
                        };
                        if room.owner_peer_id.as_deref() != Some(peer.peer_id.as_str()) {
                            sink.send(IpcEvent::Error { message: "只有群主可以邀请新成员".to_owned() }).await?;
                            continue;
                        }
                        let added = add_room_members(&sessions, &mut rooms, &peer.peer_id, &room_id, peer_ids).await;
                        if added.is_empty() {
                            sink.send(IpcEvent::Error { message: "没有可邀请的新成员".to_owned() }).await?;
                            continue;
                        }
                        save_rooms(&state_dir, &rooms)?;
                        let room = rooms.get(&room_id).expect("room exists");
                        sink.send(IpcEvent::RoomCreated {
                            room_id,
                            room_name: room.room_name.clone(),
                            peer_ids: room.peer_ids.iter().cloned().collect(),
                            agent_mode: room.agent_mode.clone(),
                            owner_peer_id: room.owner_peer_id.clone(),
                        }).await?;
                    }
                    IpcCommand::SendRoomMessage { room_id, text, client_message_id, mentions, reply_to } => {
                        if rooms.contains_key(&room_id) {
                            let mentions = rooms
                                .get(&room_id)
                                .map(|room| filter_room_mentions(mentions, room))
                                .unwrap_or_default();
                            let message = Message::room_message_with_context(
                                peer.peer_id.clone(), room_id.clone(), text, mentions, reply_to,
                            ).with_client_message_id(client_message_id);
                            if let Some(room) = rooms.get_mut(&room_id) {
                                remember_room_message(room, &message);
                            }
                            save_rooms(&state_dir, &rooms)?;
                            if let Some(room) = rooms.get(&room_id) {
                                seen_messages.insert(message.message_id.clone());
                                broadcast_room_message(&sessions, room, &message).await;
                            }
                            sink.send(IpcEvent::Message { peer_id: peer.peer_id.clone(), message }).await?;
                        } else {
                            sink.send(IpcEvent::Error { message: "群聊不存在或已退出".to_owned() }).await?;
                        }
                    }
                    IpcCommand::SendRoomFile { room_id, file_id, name, mime_type, size, sha256, file_path, client_message_id, mentions: _, reply_to: _ } => {
                        if rooms.contains_key(&room_id) {
                            let source_path = PathBuf::from(file_path);
                            if !source_path.is_absolute() {
                                sink.send(IpcEvent::Error { message: "文件路径必须是绝对路径".to_owned() }).await?;
                                continue;
                            }
                            let recipients = rooms
                                .get(&room_id)
                                .map(|room| room.peer_ids.iter()
                                    .filter(|peer_id| *peer_id != &peer.peer_id && sessions.contains_key(*peer_id))
                                    .cloned()
                                    .collect::<Vec<_>>())
                                .unwrap_or_default();
                            if recipients.is_empty() {
                                sink.send(IpcEvent::Error { message: "群内暂无可接收文件的在线同伴".to_owned() }).await?;
                                continue;
                            }
                            let unsupported = recipients
                                .iter()
                                .filter(|peer_id| {
                                    !discovered
                                        .get(*peer_id)
                                        .is_some_and(peer_supports_webrtc_file)
                                })
                                .cloned()
                                .collect::<Vec<_>>();
                            if !unsupported.is_empty() {
                                sink.send(IpcEvent::Error {
                                    message: format!(
                                        "群内有同伴不支持快速文件传输，请先更新：{}",
                                        unsupported.join(", ")
                                    ),
                                }).await?;
                                continue;
                            }
                            for recipient in recipients {
                                let metadata = FileTransferMetadata {
                                    transfer_id: Uuid::new_v4().to_string(),
                                    file_id: file_id.clone(),
                                    name: name.clone(),
                                    mime_type: mime_type.clone(),
                                    size,
                                    sha256: sha256.to_ascii_lowercase(),
                                    room_id: Some(room_id.clone()),
                                    client_message_id: client_message_id.clone(),
                                };
                                if let Err(error) = validate_metadata(&metadata, MAX_NEARBY_FILE_BYTES) {
                                    sink.send(IpcEvent::Error { message: error.to_string() }).await?;
                                    break;
                                }
                                let Some(outbound) = sessions.get(&recipient).cloned() else { continue; };
                                outgoing_transfers.insert(metadata.transfer_id.clone(), PendingOutgoingTransfer {
                                    peer_id: recipient.clone(),
                                    metadata: metadata.clone(),
                                    source_path: source_path.clone(),
                                });
                                if outbound.send(Message::file_offer(peer.peer_id.clone(), metadata.clone())).await.is_err() {
                                    outgoing_transfers.remove(&metadata.transfer_id);
                                    sink.send(IpcEvent::FileTransferFailed {
                                        peer_id: recipient,
                                        transfer_id: metadata.transfer_id,
                                        message: "文件请求发送失败".to_owned(),
                                    }).await?;
                                }
                            }
                        } else {
                            sink.send(IpcEvent::Error { message: "群聊不存在或已退出".to_owned() }).await?;
                        }
                    }
                    IpcCommand::RespondFileTransfer { transfer_id, accepted } => {
                        let Some(pending) = incoming_transfers.get_mut(&transfer_id) else {
                            sink.send(IpcEvent::Error { message: "文件请求已失效".to_owned() }).await?;
                            continue;
                        };
                        let peer_id = pending.peer_id.clone();
                        let Some(outbound) = sessions.get(&peer_id).cloned() else {
                            incoming_transfers.remove(&transfer_id);
                            sink.send(IpcEvent::FileTransferFailed {
                                peer_id,
                                transfer_id,
                                message: "对方已经离线".to_owned(),
                            }).await?;
                            continue;
                        };
                        pending.accepted = accepted;
                        if outbound.send(Message::file_decision(
                            peer.peer_id.clone(),
                            transfer_id.clone(),
                            accepted,
                        )).await.is_err() {
                            incoming_transfers.remove(&transfer_id);
                            sink.send(IpcEvent::FileTransferFailed {
                                peer_id,
                                transfer_id,
                                message: "无法回复文件请求".to_owned(),
                            }).await?;
                        } else if !accepted {
                            incoming_transfers.remove(&transfer_id);
                        }
                    }
                    IpcCommand::LeaveRoom { room_id } => {
                        if let Some(room) = rooms.remove(&room_id) {
                            let leave = Message::room_leave(peer.peer_id.clone(), room_id.clone());
                            send_to_peers(&sessions, &room.peer_ids.iter().filter(|id| *id != &peer.peer_id).cloned().collect::<Vec<_>>(), &leave).await;
                            save_rooms(&state_dir, &rooms)?;
                            sink.send(IpcEvent::RoomLeft { room_id }).await?;
                        }
                    }
                    IpcCommand::SetRoomAgentMode { room_id, agent_mode, room_name } => {
                        if agent_mode.is_none() && room_name.is_none() {
                            sink.send(IpcEvent::Error { message: "请至少提供 agent_mode 或 room_name".to_owned() }).await?;
                            continue;
                        }
                        if let Some(mode) = agent_mode.as_deref() {
                            if !is_valid_agent_mode(mode) {
                                sink.send(IpcEvent::Error { message: "无效的 Agent 触发模式".to_owned() }).await?;
                                continue;
                            }
                        }
                        let room_name = match room_name.as_deref() {
                            Some(name) => match normalize_room_name(name) {
                                Some(name) => Some(name),
                                None => {
                                    sink.send(IpcEvent::Error { message: "群名无效".to_owned() }).await?;
                                    continue;
                                }
                            },
                            None => None,
                        };
                        let Some(room) = rooms.get_mut(&room_id) else {
                            sink.send(IpcEvent::Error { message: "群聊不存在或已退出".to_owned() }).await?;
                            continue;
                        };
                        if let Some(mode) = agent_mode.as_deref() {
                            room.agent_mode = mode.to_owned();
                        }
                        if let Some(name) = room_name.as_deref() {
                            room.room_name = name.to_owned();
                        }
                        save_rooms(&state_dir, &rooms)?;
                        let settings = Message::room_settings(
                            peer.peer_id.clone(),
                            room_id.clone(),
                            agent_mode.as_deref(),
                            room_name.as_deref(),
                        );
                        seen_messages.insert(settings.message_id.clone());
                        if let Some(room) = rooms.get(&room_id) {
                            broadcast_room_message(&sessions, room, &settings).await;
                            sink.send(IpcEvent::RoomSettingsUpdated {
                                room_id,
                                agent_mode: room.agent_mode.clone(),
                                room_name: room.room_name.clone(),
                            }).await?;
                        }
                    }
                    IpcCommand::Shutdown => break,
                }
            }
            Some(event) = central_events.next() => {
                let id = match event {
                    CentralEvent::DeviceDiscovered(id) => {
                        eprintln!("[nearby][scan] event=device_discovered");
                        id
                    }
                    CentralEvent::DeviceUpdated(id) => id,
                    CentralEvent::ServicesAdvertisement { id, .. } => {
                        eprintln!("[nearby][scan] event=services_advertisement");
                        id
                    }
                    _ => continue,
                };
                let key = format!("{id:?}");
                if connection_candidates.insert(key.clone()) {
                    let device = diagnostic_device_id(&key);
                    eprintln!("[nearby][scan] candidate device={device} action=connect_attempt");
                    let peripheral = adapter.peripheral(&id).await
                        .with_context(|| format!("failed to get BLE peripheral {key}"))?;
                    let local_peer = peer.clone();
                    let event_tx = session_event_tx.clone();
                    let connection_adapter = adapter.clone();
                    let connection_scan_lock = Arc::clone(&scan_lock);
                    tokio::spawn(async move {
                        let result = connect_to_peer(
                            peripheral,
                            local_peer,
                            device.clone(),
                            connection_adapter.clone(),
                            connection_scan_lock,
                            event_tx.clone(),
                        ).await;
                        if let Err(error) = result {
                            let detail = format!("{error:#}");
                            connection_adapter
                                .start_scan(ScanFilter { services: vec![SERVICE_UUID] })
                                .await
                                .ok();
                            eprintln!("[nearby][session] device={device} result=failed error={detail}");
                            let _ = event_tx.send(SessionEvent::Failed { peer_id: key, error: detail }).await;
                        }
                    });
                }
            }
            Some(event) = server_event_rx.recv() => {
                handle_server_event(
                    event,
                    &peer,
                    &server,
                    &session_event_tx,
                    &mut server_clients,
                    &mut server_reassemblers,
                ).await?;
            }
            Some(event) = session_event_rx.recv() => {
                match event {
                    SessionEvent::Discovered(remote) => {
                        eprintln!(
                            "[nearby][session] peer_discovered peer_id={} display_name={}",
                            remote.peer_id, remote.display_name
                        );
                        discovered.insert(remote.peer_id.clone(), remote.clone());
                        sink.send(IpcEvent::PeerDiscovered { peer: remote }).await?;
                    }
                    SessionEvent::Ready { peer: remote, session_id, outbound } => {
                        eprintln!("[nearby][session] peer_ready peer_id={}", remote.peer_id);
                        discovered.insert(remote.peer_id.clone(), remote.clone());
                        session_ids.insert(remote.peer_id.clone(), session_id);
                        sessions.insert(remote.peer_id.clone(), outbound.clone());
                        if pending_peer_connections.remove(&remote.peer_id) {
                            if outbound.send(Message::peer_connect(peer.peer_id.clone())).await.is_ok() {
                                active_peers.insert(remote.peer_id.clone());
                                sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                            } else {
                                sink.send(IpcEvent::PeerConnectionFailed {
                                    peer_id: remote.peer_id,
                                    message: "BLE 会话在连接时断开".to_owned(),
                                }).await?;
                            }
                        }
                    }
                    SessionEvent::Received { peer_id, message } => {
                        // room.* 消息允许中继转发（sender 是原作者，与会话对端不同）；
                        // 1:1 消息仍要求 sender 与会话对端一致，防止伪造。
                        if message.version != PROTOCOL_VERSION {
                            continue;
                        }
                        if message.sender != peer_id && !message.message_type.starts_with("room.") {
                            continue;
                        }
                        match message.message_type.as_str() {
                            "peer.connect" => {
                                if !seen_messages.insert(message.message_id) {
                                    continue;
                                }
                                active_peers.insert(peer_id.clone());
                                if let Some(remote) = discovered.get(&peer_id).cloned() {
                                    sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                                }
                            }
                            "peer.disconnect" => {
                                if !seen_messages.insert(message.message_id) {
                                    continue;
                                }
                                if active_peers.remove(&peer_id) {
                                    sink.send(IpcEvent::PeerDisconnected { peer_id }).await?;
                                }
                            }
                            "agent.request" | "agent.response" | "agent.error" => {
                                if !active_peers.contains(&peer_id)
                                    || !seen_messages.insert(message.message_id.clone())
                                {
                                    continue;
                                }
                                remember_dm_message(&mut dms, &peer_id, &message);
                                save_dms(&state_dir, &dms)?;
                                sink.send(IpcEvent::Message { peer_id, message }).await?;
                            }
                            "message.ack" => {
                                if !seen_messages.insert(message.message_id.clone()) {
                                    continue;
                                }
                                emit_delivery_ack(&sink, peer_id, &message).await?;
                            }
                            "file.offer" => {
                                if !active_peers.contains(&peer_id)
                                    || !seen_messages.insert(message.message_id.clone())
                                {
                                    continue;
                                }
                                if !discovered
                                    .get(&peer_id)
                                    .is_some_and(peer_supports_webrtc_file)
                                {
                                    continue;
                                }
                                let transfer = message
                                    .payload
                                    .get("transfer")
                                    .cloned()
                                    .and_then(|value| serde_json::from_value::<FileTransferMetadata>(value).ok());
                                let Some(transfer) = transfer else { continue; };
                                let room_allowed = transfer.room_id.as_ref().is_none_or(|room_id| {
                                    rooms
                                        .get(room_id)
                                        .is_some_and(|room| room.peer_ids.contains(&peer_id))
                                });
                                if !room_allowed
                                    || validate_metadata(&transfer, MAX_NEARBY_FILE_BYTES).is_err()
                                    || incoming_transfers.contains_key(&transfer.transfer_id)
                                {
                                    continue;
                                }
                                incoming_transfers.insert(transfer.transfer_id.clone(), PendingIncomingTransfer {
                                    peer_id: peer_id.clone(),
                                    metadata: transfer.clone(),
                                    accepted: false,
                                });
                                sink.send(IpcEvent::FileTransferRequested {
                                    peer_id,
                                    transfer,
                                }).await?;
                            }
                            "file.decision" => {
                                if !seen_messages.insert(message.message_id.clone()) {
                                    continue;
                                }
                                let transfer_id = message.payload.get("transfer_id")
                                    .and_then(|value| value.as_str())
                                    .unwrap_or_default()
                                    .to_owned();
                                let accepted = message.payload.get("accepted")
                                    .and_then(|value| value.as_bool())
                                    .unwrap_or(false);
                                let Some(pending) = outgoing_transfers.get(&transfer_id).cloned() else { continue; };
                                if pending.peer_id != peer_id {
                                    continue;
                                }
                                if !accepted {
                                    outgoing_transfers.remove(&transfer_id);
                                    sink.send(IpcEvent::FileTransferFailed {
                                        peer_id,
                                        transfer_id,
                                        message: "对方拒绝了文件".to_owned(),
                                    }).await?;
                                    continue;
                                }
                                let manager = transfers.clone();
                                let event_tx = transfer_event_tx.clone();
                                tokio::spawn(async move {
                                    if let Err(error) = manager.start_sender(
                                        pending.peer_id.clone(),
                                        pending.metadata.clone(),
                                        pending.source_path,
                                    ).await {
                                        let _ = event_tx.send(TransferEvent::Failed {
                                            peer_id: pending.peer_id,
                                            transfer_id: pending.metadata.transfer_id,
                                            message: format!("{error:#}"),
                                        }).await;
                                    }
                                });
                            }
                            "file.webrtc_offer" => {
                                if !seen_messages.insert(message.message_id.clone()) {
                                    continue;
                                }
                                let transfer_id = message.payload.get("transfer_id")
                                    .and_then(|value| value.as_str())
                                    .unwrap_or_default()
                                    .to_owned();
                                let sdp = message.payload.get("sdp")
                                    .and_then(|value| value.as_str())
                                    .unwrap_or_default()
                                    .to_owned();
                                let Some(pending) = incoming_transfers.get(&transfer_id).cloned() else { continue; };
                                if pending.peer_id != peer_id || !pending.accepted || sdp.is_empty() {
                                    continue;
                                }
                                let manager = transfers.clone();
                                let event_tx = transfer_event_tx.clone();
                                tokio::spawn(async move {
                                    if let Err(error) = manager.start_receiver(
                                        pending.peer_id.clone(),
                                        pending.metadata.clone(),
                                        sdp,
                                    ).await {
                                        let _ = event_tx.send(TransferEvent::Failed {
                                            peer_id: pending.peer_id,
                                            transfer_id: pending.metadata.transfer_id,
                                            message: format!("{error:#}"),
                                        }).await;
                                    }
                                });
                            }
                            "file.webrtc_answer" => {
                                if !seen_messages.insert(message.message_id.clone()) {
                                    continue;
                                }
                                let transfer_id = message.payload.get("transfer_id")
                                    .and_then(|value| value.as_str())
                                    .unwrap_or_default()
                                    .to_owned();
                                let sdp = message.payload.get("sdp")
                                    .and_then(|value| value.as_str())
                                    .unwrap_or_default()
                                    .to_owned();
                                let Some(pending) = outgoing_transfers.get(&transfer_id) else { continue; };
                                if pending.peer_id != peer_id || sdp.is_empty() {
                                    continue;
                                }
                                let manager = transfers.clone();
                                let event_tx = transfer_event_tx.clone();
                                let failed_peer_id = peer_id.clone();
                                tokio::spawn(async move {
                                    if let Err(error) = manager.apply_answer(&transfer_id, &sdp).await {
                                        let _ = event_tx.send(TransferEvent::Failed {
                                            peer_id: failed_peer_id,
                                            transfer_id,
                                            message: format!("{error:#}"),
                                        }).await;
                                    }
                                });
                            }
                            "peer.message" => {
                                if !active_peers.contains(&peer_id)
                                    || !seen_messages.insert(message.message_id.clone())
                                {
                                    continue;
                                }
                                let text = message
                                    .payload
                                    .get("text")
                                    .and_then(|value| value.as_str())
                                    .unwrap_or_default()
                                    .to_owned();
                                let mentions = message
                                    .payload
                                    .get("mentions")
                                    .and_then(|value| value.as_array())
                                    .map(|items| {
                                        items
                                            .iter()
                                            .filter_map(|value| value.as_str().map(str::to_owned))
                                            .collect::<Vec<_>>()
                                    })
                                    .unwrap_or_default();
                                let display_name = discovered
                                    .get(&peer_id)
                                    .map(|remote| remote.display_name.clone())
                                    .unwrap_or_else(|| peer_id.clone());
                                remember_dm_message(&mut dms, &peer_id, &message);
                                save_dms(&state_dir, &dms)?;
                                acknowledge_received_message(
                                    &peer.peer_id,
                                    &sessions,
                                    &peer_id,
                                    &message,
                                )
                                .await;
                                sink.send(IpcEvent::PeerMessageReceived {
                                    peer_id,
                                    display_name,
                                    text,
                                    mentions,
                                    message_id: message.message_id.clone(),
                                    timestamp: unix_timestamp_seconds(),
                                }).await?;
                            }
                            "peer.file" => {
                                if !active_peers.contains(&peer_id)
                                    || !seen_messages.insert(message.message_id.clone())
                                {
                                    continue;
                                }
                                remember_dm_message(&mut dms, &peer_id, &message);
                                save_dms(&state_dir, &dms)?;
                                acknowledge_received_message(
                                    &peer.peer_id,
                                    &sessions,
                                    &peer_id,
                                    &message,
                                )
                                .await;
                                sink.send(IpcEvent::Message { peer_id, message }).await?;
                            }
                            _ => {
                                handle_received_message(
                                    &peer.peer_id,
                                    &sessions,
                                    &mut rooms,
                                    &mut seen_messages,
                                    &discovered,
                                    &sink,
                                    peer_id,
                                    message,
                                ).await?;
                                save_rooms(&state_dir, &rooms)?;
                            }
                        }
                    }
                    SessionEvent::Closed { peer_id, session_id } => {
                        eprintln!("[nearby][session] peer_disconnected peer_id={peer_id}");
                        server_clients.retain(|_, (_, active_session_id)| active_session_id != &session_id);
                        if !close_current_session(&mut session_ids, &peer_id, &session_id) {
                            eprintln!("[nearby][session] stale_close_ignored peer_id={peer_id}");
                            continue;
                        }
                        sessions.remove(&peer_id);
                        pending_peer_connections.remove(&peer_id);
                        let interrupted = outgoing_transfers
                            .iter()
                            .filter(|(_, transfer)| transfer.peer_id == peer_id)
                            .map(|(transfer_id, _)| transfer_id.clone())
                            .chain(
                                incoming_transfers
                                    .iter()
                                    .filter(|(_, transfer)| transfer.peer_id == peer_id)
                                    .map(|(transfer_id, _)| transfer_id.clone()),
                            )
                            .collect::<HashSet<_>>();
                        outgoing_transfers.retain(|_, transfer| transfer.peer_id != peer_id);
                        incoming_transfers.retain(|_, transfer| transfer.peer_id != peer_id);
                        for transfer_id in interrupted {
                            transfers.finish(&transfer_id).await;
                            sink.send(IpcEvent::FileTransferFailed {
                                peer_id: peer_id.clone(),
                                transfer_id,
                                message: "附近连接已断开，文件传输已停止".to_owned(),
                            }).await?;
                        }
                        if active_peers.remove(&peer_id) {
                            sink.send(IpcEvent::PeerDisconnected { peer_id }).await?;
                        } else if discovered.contains_key(&peer_id) {
                            sink.send(IpcEvent::PeerUnavailable { peer_id }).await?;
                        }
                    }
                    SessionEvent::Failed { peer_id, error } => {
                        eprintln!(
                            "[nearby][session] failed candidate={} error={error}",
                            diagnostic_device_id(&peer_id)
                        );
                        connection_candidates.remove(&peer_id);
                        sink.send(IpcEvent::Error { message: format!("BLE session {peer_id} failed: {error}") }).await?;
                    }
                }
            }
            Some(event) = transfer_event_rx.recv() => {
                match event {
                    TransferEvent::OfferReady { peer_id, transfer_id, sdp } => {
                        let sent = if let Some(outbound) = sessions.get(&peer_id) {
                            outbound.send(Message::file_webrtc_signal(
                                peer.peer_id.clone(),
                                transfer_id.clone(),
                                sdp,
                                false,
                            )).await.is_ok()
                        } else {
                            false
                        };
                        if !sent {
                            outgoing_transfers.remove(&transfer_id);
                            transfers.finish(&transfer_id).await;
                            sink.send(IpcEvent::FileTransferFailed {
                                peer_id,
                                transfer_id,
                                message: "WebRTC 连接信息发送失败".to_owned(),
                            }).await?;
                        }
                    }
                    TransferEvent::AnswerReady { peer_id, transfer_id, sdp } => {
                        let sent = if let Some(outbound) = sessions.get(&peer_id) {
                            outbound.send(Message::file_webrtc_signal(
                                peer.peer_id.clone(),
                                transfer_id.clone(),
                                sdp,
                                true,
                            )).await.is_ok()
                        } else {
                            false
                        };
                        if !sent {
                            incoming_transfers.remove(&transfer_id);
                            transfers.finish(&transfer_id).await;
                            sink.send(IpcEvent::FileTransferFailed {
                                peer_id,
                                transfer_id,
                                message: "WebRTC 连接信息回复失败".to_owned(),
                            }).await?;
                        }
                    }
                    TransferEvent::Progress { peer_id, transfer_id, sent, total, incoming } => {
                        sink.send(IpcEvent::FileTransferProgress {
                            peer_id,
                            transfer_id,
                            sent,
                            total,
                            incoming,
                        }).await?;
                    }
                    TransferEvent::Sent { peer_id, metadata } => {
                        outgoing_transfers.remove(&metadata.transfer_id);
                        transfers.finish(&metadata.transfer_id).await;
                        if let Some(message_id) = metadata.client_message_id {
                            sink.send(IpcEvent::MessageDelivered { peer_id, message_id }).await?;
                        }
                    }
                    TransferEvent::Received { peer_id, metadata, path } => {
                        incoming_transfers.remove(&metadata.transfer_id);
                        transfers.finish(&metadata.transfer_id).await;
                        let file = TransferredFile {
                            file_id: metadata.file_id,
                            name: metadata.name,
                            mime_type: metadata.mime_type,
                            size: metadata.size,
                            sha256: metadata.sha256,
                            local_path: path.to_string_lossy().into_owned(),
                            complete: true,
                        };
                        if let Some(room_id) = metadata.room_id {
                            let message = Message::room_transferred_file(peer_id.clone(), room_id.clone(), file);
                            if let Some(room) = rooms.get_mut(&room_id) {
                                remember_room_message(room, &message);
                                save_rooms(&state_dir, &rooms)?;
                                sink.send(IpcEvent::Message { peer_id, message }).await?;
                            }
                        } else {
                            let message = Message::peer_transferred_file(peer_id.clone(), file);
                            remember_dm_message(&mut dms, &peer_id, &message);
                            save_dms(&state_dir, &dms)?;
                            sink.send(IpcEvent::Message { peer_id, message }).await?;
                        }
                    }
                    TransferEvent::Failed { peer_id, transfer_id, message } => {
                        outgoing_transfers.remove(&transfer_id);
                        incoming_transfers.remove(&transfer_id);
                        transfers.finish(&transfer_id).await;
                        sink.send(IpcEvent::FileTransferFailed {
                            peer_id,
                            transfer_id,
                            message,
                        }).await?;
                    }
                }
            }
        }
    }

    adapter.stop_scan().await.ok();
    Ok(())
}

fn command_name(command: &IpcCommand) -> &'static str {
    match command {
        IpcCommand::StartDiscovery => "start_discovery",
        IpcCommand::StopDiscovery => "stop_discovery",
        IpcCommand::SetDiscoverable { .. } => "set_discoverable",
        IpcCommand::ConnectPeer { .. } => "connect_peer",
        IpcCommand::DisconnectPeer { .. } => "disconnect_peer",
        IpcCommand::SendAgentRequest { .. } => "send_agent_request",
        IpcCommand::SendAgentReply { .. } => "send_agent_reply",
        IpcCommand::SendPeerMessage { .. } => "send_peer_message",
        IpcCommand::SendPeerFile { .. } => "send_peer_file",
        IpcCommand::CreateRoom { .. } => "create_room",
        IpcCommand::SendRoomMessage { .. } => "send_room_message",
        IpcCommand::SendRoomFile { .. } => "send_room_file",
        IpcCommand::RespondFileTransfer { .. } => "respond_file_transfer",
        IpcCommand::LeaveRoom { .. } => "leave_room",
        IpcCommand::SetRoomAgentMode { .. } => "set_room_agent_mode",
        IpcCommand::InviteToRoom { .. } => "invite_to_room",
        IpcCommand::Shutdown => "shutdown",
    }
}

pub(crate) fn create_peer(config: &NearbyConfig) -> Result<PeerInfo> {
    let state_dir = resolve_state_dir(config.state_dir.as_deref());
    Ok(PeerInfo {
        protocol_version: PROTOCOL_VERSION,
        peer_id: load_or_create_peer_id(&state_dir, config.peer_id.as_deref())?,
        peer_token: uuid::Uuid::new_v4().to_string(),
        display_name: if config.display_name.is_empty() {
            default_display_name()
        } else {
            config.display_name.clone()
        },
        agent_name: if config.agent_name.is_empty() {
            default_agent_name()
        } else {
            config.agent_name.clone()
        },
        capabilities: {
            let mut capabilities = config.capabilities.clone();
            if !capabilities
                .iter()
                .any(|value| value == FILE_WEBRTC_CAPABILITY)
            {
                capabilities.push(FILE_WEBRTC_CAPABILITY.to_owned());
            }
            capabilities
        },
        published_agents: config.published_agents.clone(),
    })
}

pub(crate) fn peer_supports_webrtc_file(peer: &PeerInfo) -> bool {
    peer.capabilities
        .iter()
        .any(|capability| capability == FILE_WEBRTC_CAPABILITY)
}

async fn configure_server(server: &Arc<Mutex<ServerPeripheral>>, peer: &PeerInfo) -> Result<()> {
    let service = nearby_service(peer)?;
    let mut server = server.lock().await;
    let mut powered = false;
    for attempt in 1..=50 {
        powered = server
            .is_powered()
            .await
            .context("failed to query BLE power state")?;
        if powered {
            eprintln!("[nearby][peripheral] powered=true poll_attempt={attempt}");
            break;
        }
        if attempt == 1 || attempt == 50 {
            eprintln!("[nearby][peripheral] powered=false poll_attempt={attempt}");
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    anyhow::ensure!(powered, "BLE adapter is powered off or unavailable");
    eprintln!("[nearby][peripheral] adding_service uuid={}", SERVICE_UUID);
    server
        .add_service(&service)
        .await
        .context("failed to add Nearby GATT service")?;
    eprintln!("[nearby][peripheral] service_added characteristics=3");
    Ok(())
}

async fn set_discoverable(
    server: &Arc<Mutex<ServerPeripheral>>,
    name: &str,
    enabled: bool,
) -> Result<()> {
    let mut server = server.lock().await;
    eprintln!(
        "[nearby][peripheral] advertising_request enabled={} service_uuid={}",
        enabled, SERVICE_UUID
    );
    if enabled {
        server
            .start_advertising(name, &[SERVICE_UUID])
            .await
            .context("failed to start BLE advertising")?;
        eprintln!("[nearby][peripheral] advertising_started");
    } else {
        server
            .stop_advertising()
            .await
            .context("failed to stop BLE advertising")?;
        eprintln!("[nearby][peripheral] advertising_stopped");
    }
    Ok(())
}

fn nearby_service(peer: &PeerInfo) -> Result<Service> {
    let peer_info = peer
        .encode()
        .context("failed to encode static Nearby PeerInfo")?;
    Ok(Service {
        uuid: SERVICE_UUID,
        primary: true,
        characteristics: vec![
            ServerCharacteristic {
                uuid: PEER_INFO_UUID,
                properties: vec![CharacteristicProperty::Read],
                permissions: vec![AttributePermission::Readable],
                value: Some(peer_info),
                descriptors: vec![],
            },
            ServerCharacteristic {
                uuid: INCOMING_MESSAGE_UUID,
                properties: vec![
                    CharacteristicProperty::Write,
                    CharacteristicProperty::WriteWithoutResponse,
                ],
                permissions: vec![AttributePermission::Writeable],
                value: None,
                descriptors: vec![],
            },
            ServerCharacteristic {
                uuid: OUTGOING_MESSAGE_UUID,
                properties: vec![
                    CharacteristicProperty::Notify,
                    CharacteristicProperty::Indicate,
                ],
                permissions: vec![],
                value: None,
                descriptors: vec![],
            },
        ],
    })
}

async fn connect_to_peer(
    peripheral: Peripheral,
    local_peer: PeerInfo,
    device: String,
    adapter: btleplug::platform::Adapter,
    scan_lock: Arc<Mutex<()>>,
    event_tx: mpsc::Sender<SessionEvent>,
) -> Result<()> {
    let session_id = uuid::Uuid::new_v4().to_string();
    let _scan_lock = scan_lock.lock().await;
    adapter
        .stop_scan()
        .await
        .context("failed to pause BLE scanning before connection")?;
    eprintln!("[nearby][central] scanning_paused_for_connection device={device}");
    eprintln!(
        "[nearby][session] device={device} stage=connect_started local_peer_id={}",
        local_peer.peer_id
    );
    peripheral
        .connect_with_timeout(Duration::from_secs(15))
        .await
        .context("failed to connect")?;
    eprintln!("[nearby][session] device={device} stage=connect_succeeded");
    peripheral
        .discover_services()
        .await
        .context("failed to discover GATT services")?;
    eprintln!(
        "[nearby][session] device={device} stage=services_discovered characteristics={}",
        diagnostic_characteristics(&peripheral)
    );
    let characteristics = peripheral.characteristics();
    let peer_info_characteristic = characteristics
        .iter()
        .find(|characteristic| characteristic.uuid == PEER_INFO_UUID)
        .context("remote PeerInfo characteristic was not found")?
        .clone();
    let incoming_characteristic = characteristics
        .iter()
        .find(|characteristic| characteristic.uuid == INCOMING_MESSAGE_UUID)
        .context("remote IncomingMessage characteristic was not found")?
        .clone();
    let outgoing_characteristic = characteristics
        .iter()
        .find(|characteristic| characteristic.uuid == OUTGOING_MESSAGE_UUID)
        .context("remote OutgoingMessage characteristic was not found")?
        .clone();
    let write_type = incoming_write_type(&incoming_characteristic)?;
    eprintln!("[nearby][session] device={device} incoming_write_type={write_type:?}");
    eprintln!("[nearby][session] device={device} stage=peer_info_read_started");
    let peer_info_bytes = tokio::time::timeout(
        Duration::from_secs(8),
        peripheral.read(&peer_info_characteristic),
    )
    .await
    .context("timed out reading remote PeerInfo")?
    .context("failed to read remote PeerInfo")?;
    let remote = PeerInfo::decode(&peer_info_bytes).context("remote PeerInfo is not valid JSON")?;
    if remote.protocol_version != PROTOCOL_VERSION {
        eprintln!(
            "[nearby][session] device={device} stage=peer_info_rejected reason=protocol_version remote={} local={}",
            remote.protocol_version, PROTOCOL_VERSION
        );
    }
    anyhow::ensure!(
        remote.protocol_version == PROTOCOL_VERSION,
        "remote protocol version {} is incompatible with local version {}",
        remote.protocol_version,
        PROTOCOL_VERSION
    );
    anyhow::ensure!(
        remote.peer_id != local_peer.peer_id,
        "remote PeerInfo unexpectedly contains the local peer id"
    );
    eprintln!(
        "[nearby][session] device={device} stage=peer_info_read remote_peer_id={} remote_display_name={}",
        remote.peer_id, remote.display_name
    );
    event_tx
        .send(SessionEvent::Discovered(remote.clone()))
        .await
        .ok();
    let should_initiate = should_start_central_session(&local_peer.peer_id, &remote.peer_id);
    eprintln!(
        "[nearby][session] device={device} stage=connection_policy should_initiate={} local_peer_id={} remote_peer_id={}",
        should_initiate, local_peer.peer_id, remote.peer_id
    );
    if !should_initiate {
        eprintln!(
            "[nearby][session] device={device} stage=duplicate_connection_close_before_subscribe"
        );
        peripheral.disconnect().await.ok();
        adapter
            .start_scan(ScanFilter {
                services: vec![SERVICE_UUID],
            })
            .await
            .context("failed to resume BLE scanning after duplicate connection")?;
        eprintln!("[nearby][central] scanning_resumed_after_connection device={device}");
        drop(_scan_lock);
        return Ok(());
    }

    let max_payload = FrameCodec::frame_payload_capacity(peripheral.mtu());
    if let Err(error) =
        subscribe_outgoing_message(&peripheral, &outgoing_characteristic, &device).await
    {
        peripheral.disconnect().await.ok();
        return Err(error);
    }
    let mut notifications = peripheral
        .notifications()
        .await
        .context("failed to open BLE notification stream")?;
    let mut reassembler = Reassembler::default();
    let hello = Message::hello(&local_peer);
    let hello_frames = FrameCodec::fragment(&hello.encode()?, max_payload, 1)?;
    eprintln!(
        "[nearby][session] device={device} stage=peer_hello_write mtu={} frame_payload={} frame_count={}",
        peripheral.mtu(),
        max_payload,
        hello_frames.len()
    );
    for frame in hello_frames {
        peripheral
            .write(&incoming_characteristic, &frame, write_type)
            .await
            .context("failed to send BLE peer hello")?;
    }
    if USE_PASSIVE_SESSIONS {
        eprintln!("[nearby][session] device={device} stage=peer_hello_read_started");
        loop {
            let notification = tokio::time::timeout(Duration::from_secs(8), notifications.next())
                .await
                .context("timed out waiting for remote PeerInfo handshake")?
                .context("BLE notification stream ended during PeerInfo handshake")?;
            if notification.uuid != OUTGOING_MESSAGE_UUID {
                continue;
            }
            let Ok(frame) = FrameCodec::parse(&notification.value) else {
                continue;
            };
            if let crate::protocol::ReassemblyResult::Complete(bytes) = reassembler.accept(frame) {
                let message = Message::decode(&bytes)
                    .context("received invalid BLE PeerInfo handshake JSON")?;
                if message.message_type != "peer.hello" {
                    continue;
                }
                let hello_peer = peer_info_from_hello(&message)
                    .context("remote PeerInfo handshake has an incompatible protocol")?;
                anyhow::ensure!(
                    hello_peer.peer_id == remote.peer_id
                        && hello_peer.peer_token == remote.peer_token,
                    "remote PeerInfo changed during the BLE handshake"
                );
                eprintln!(
                    "[nearby][session] device={device} stage=peer_hello_received remote_peer_id={} remote_display_name={}",
                    hello_peer.peer_id, hello_peer.display_name
                );
                break;
            }
        }
    }
    let (outbound, mut outbound_rx) = mpsc::channel(64);
    event_tx
        .send(SessionEvent::Ready {
            peer: remote.clone(),
            session_id: session_id.clone(),
            outbound,
        })
        .await
        .context("failed to publish ready BLE session")?;
    adapter
        .start_scan(ScanFilter {
            services: vec![SERVICE_UUID],
        })
        .await
        .context("failed to resume BLE scanning after connection")?;
    eprintln!("[nearby][central] scanning_resumed_after_connection device={device}");
    drop(_scan_lock);
    eprintln!("[nearby][session] device={device} stage=ready");
    let mut transfer_id = 2_u32;
    loop {
        tokio::select! {
            Some(message) = outbound_rx.recv() => {
                let frames = FrameCodec::fragment(&message.encode()?, max_payload, transfer_id)?;
                eprintln!("[nearby][session] device={device} stage=message_write message_type={} frame_count={}", message.message_type, frames.len());
                transfer_id = transfer_id.wrapping_add(1);
                for frame in frames { peripheral.write(&incoming_characteristic, &frame, write_type).await.context("failed to write BLE message frame")?; }
            }
            Some(notification) = notifications.next() => {
                if notification.uuid != OUTGOING_MESSAGE_UUID { continue; }
                let frame = match FrameCodec::parse(&notification.value) { Ok(frame) => frame, Err(_) => continue };
                if let crate::protocol::ReassemblyResult::Complete(bytes) = reassembler.accept(frame) {
                    let message = Message::decode(&bytes).context("received invalid BLE message JSON")?;
                    eprintln!("[nearby][session] device={device} stage=message_received message_type={} sender={}", message.message_type, message.sender);
                    event_tx.send(SessionEvent::Received { peer_id: remote.peer_id.clone(), message }).await.ok();
                }
            }
            else => break,
        }
    }
    peripheral.disconnect().await.ok();
    eprintln!(
        "[nearby][session] device={device} stage=disconnected remote_peer_id={}",
        remote.peer_id
    );
    event_tx
        .send(SessionEvent::Closed {
            peer_id: remote.peer_id,
            session_id,
        })
        .await
        .ok();
    Ok(())
}

async fn handle_server_event(
    event: PeripheralEvent,
    peer: &PeerInfo,
    server: &Arc<Mutex<ServerPeripheral>>,
    event_tx: &mpsc::Sender<SessionEvent>,
    server_clients: &mut HashMap<String, (String, String)>,
    reassemblers: &mut HashMap<String, Reassembler>,
) -> Result<()> {
    match event {
        PeripheralEvent::ReadRequest {
            request,
            offset,
            responder,
        } => {
            eprintln!(
                "[nearby][peripheral] read_request client={} characteristic={} offset={offset}",
                diagnostic_device_id(&request.client),
                request.characteristic
            );
            let response = if request.characteristic == PEER_INFO_UUID {
                let bytes = peer.encode()?;
                let offset = usize::try_from(offset).unwrap_or(usize::MAX);
                if offset <= bytes.len() {
                    ReadRequestResponse {
                        value: bytes[offset..].to_vec(),
                        response: RequestResponse::Success,
                    }
                } else {
                    ReadRequestResponse {
                        value: vec![],
                        response: RequestResponse::InvalidOffset,
                    }
                }
            } else {
                ReadRequestResponse {
                    value: vec![],
                    response: RequestResponse::RequestNotSupported,
                }
            };
            responder.send(response).ok();
        }
        PeripheralEvent::WriteRequest {
            request,
            value,
            offset,
            responder,
        } => {
            let accepted = request.characteristic == INCOMING_MESSAGE_UUID && offset == 0;
            eprintln!(
                "[nearby][peripheral] write_request client={} characteristic={} offset={offset} bytes={} accepted={accepted}",
                diagnostic_device_id(&request.client),
                request.characteristic,
                value.len()
            );
            responder
                .send(WriteRequestResponse {
                    response: if accepted {
                        RequestResponse::Success
                    } else {
                        RequestResponse::RequestNotSupported
                    },
                })
                .ok();
            if !accepted {
                return Ok(());
            }
            let client = request.client.clone();
            let reassembler = reassemblers.entry(client.clone()).or_default();
            let Ok(frame) = FrameCodec::parse(&value) else {
                return Ok(());
            };
            if let crate::protocol::ReassemblyResult::Complete(bytes) = reassembler.accept(frame) {
                let message =
                    Message::decode(&bytes).context("received invalid BLE message JSON")?;
                eprintln!(
                    "[nearby][peripheral] message_received sender={} message_type={}",
                    message.sender, message.message_type
                );
                if message.message_type == "peer.hello" && USE_PASSIVE_SESSIONS {
                    if let Some(remote) = peer_info_from_hello(&message) {
                        eprintln!(
                            "[nearby][peripheral] passive_session_ready peer_id={}",
                            remote.peer_id
                        );
                        let (outbound, outbound_rx) = mpsc::channel(64);
                        let session_id = uuid::Uuid::new_v4().to_string();
                        server_clients.insert(client, (remote.peer_id.clone(), session_id.clone()));
                        spawn_server_writer(Arc::clone(server), outbound_rx);
                        outbound.send(Message::hello(peer)).await.ok();
                        event_tx
                            .send(SessionEvent::Ready {
                                peer: remote,
                                session_id,
                                outbound,
                            })
                            .await
                            .ok();
                    } else {
                        eprintln!("[nearby][peripheral] peer_hello_rejected reason=invalid_protocol_or_identity");
                    }
                } else {
                    event_tx
                        .send(SessionEvent::Received {
                            peer_id: message.sender.clone(),
                            message,
                        })
                        .await
                        .ok();
                }
            }
        }
        PeripheralEvent::CharacteristicSubscriptionUpdate {
            request,
            subscribed,
        } => {
            eprintln!(
                "[nearby][peripheral] subscription_update client={} characteristic={} subscribed={subscribed}",
                diagnostic_device_id(&request.client),
                request.characteristic
            );
            if request.characteristic == OUTGOING_MESSAGE_UUID && !subscribed {
                if let Some((peer_id, session_id)) = server_clients.remove(&request.client) {
                    reassemblers.remove(&request.client);
                    event_tx
                        .send(SessionEvent::Closed {
                            peer_id,
                            session_id,
                        })
                        .await
                        .ok();
                }
            }
        }
        PeripheralEvent::StateUpdate { is_powered } => {
            eprintln!("[nearby][peripheral] state_update powered={is_powered}");
        }
    }
    Ok(())
}

fn should_start_central_session(local_peer_id: &str, remote_peer_id: &str) -> bool {
    #[cfg(target_os = "linux")]
    {
        let _ = (local_peer_id, remote_peer_id);
        true
    }
    #[cfg(not(target_os = "linux"))]
    {
        should_initiate(local_peer_id, remote_peer_id)
    }
}

fn close_current_session(
    session_ids: &mut HashMap<String, String>,
    peer_id: &str,
    session_id: &str,
) -> bool {
    if session_ids.get(peer_id).map(String::as_str) != Some(session_id) {
        return false;
    }
    session_ids.remove(peer_id);
    true
}

fn peer_info_from_hello(message: &Message) -> Option<PeerInfo> {
    let protocol_version = message
        .payload
        .get("protocol_version")
        .and_then(|value| value.as_u64())
        .and_then(|value| u8::try_from(value).ok())
        .unwrap_or(message.version);
    if message.version != PROTOCOL_VERSION || protocol_version != PROTOCOL_VERSION {
        return None;
    }
    Some(PeerInfo {
        protocol_version,
        peer_id: message.sender.clone(),
        peer_token: message
            .payload
            .get("peer_token")
            .and_then(|value| value.as_str())
            .unwrap_or_default()
            .to_owned(),
        display_name: message
            .payload
            .get("display_name")
            .and_then(|value| value.as_str())
            .unwrap_or(&message.sender)
            .to_owned(),
        agent_name: message
            .payload
            .get("agent_name")
            .and_then(|value| value.as_str())
            .unwrap_or("Crew Agent")
            .to_owned(),
        capabilities: message
            .payload
            .get("capabilities")
            .and_then(|value| value.as_array())
            .map(|values| {
                values
                    .iter()
                    .filter_map(|value| value.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default(),
        published_agents: message
            .payload
            .get("published_agents")
            .cloned()
            .and_then(|value| serde_json::from_value::<Vec<PublishedAgent>>(value).ok())
            .unwrap_or_default(),
    })
}

fn spawn_server_writer(
    server: Arc<Mutex<ServerPeripheral>>,
    mut outbound_rx: mpsc::Receiver<Message>,
) {
    tokio::spawn(async move {
        let mut transfer_id = 1_u32;
        while let Some(message) = outbound_rx.recv().await {
            let Ok(bytes) = message.encode() else {
                continue;
            };
            let Ok(frames) =
                FrameCodec::fragment(&bytes, FrameCodec::frame_payload_capacity(23), transfer_id)
            else {
                continue;
            };
            transfer_id = transfer_id.wrapping_add(1);
            for frame in frames {
                if server
                    .lock()
                    .await
                    .update_characteristic(OUTGOING_MESSAGE_UUID, frame)
                    .await
                    .is_err()
                {
                    return;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }
    });
}

async fn send_to_peers(
    sessions: &HashMap<String, mpsc::Sender<Message>>,
    peer_ids: &[String],
    message: &Message,
) {
    for peer_id in peer_ids {
        if let Some(sender) = sessions.get(peer_id) {
            let _ = sender.send(message.clone()).await;
        }
    }
}

/// Merges peers into an existing room and invites the newly added, currently
/// connected members. Returns the peers that were actually added.
pub(crate) async fn add_room_members(
    sessions: &HashMap<String, mpsc::Sender<Message>>,
    rooms: &mut HashMap<String, RoomState>,
    local_peer_id: &str,
    room_id: &str,
    peer_ids: Vec<String>,
) -> Vec<String> {
    let Some(room) = rooms.get_mut(room_id) else {
        return Vec::new();
    };
    let selected: Vec<String> = peer_ids
        .into_iter()
        .filter(|peer_id| sessions.contains_key(peer_id) && !room.peer_ids.contains(peer_id))
        .collect();
    if selected.is_empty() {
        return selected;
    }
    room.peer_ids.extend(selected.iter().cloned());
    let invite = Message::room_invite(
        local_peer_id.to_owned(),
        room_id.to_owned(),
        room.room_name.clone(),
        room.peer_ids.iter().cloned().collect(),
        Some(&room.agent_mode),
        room.owner_peer_id.as_deref(),
    );
    send_to_peers(sessions, &selected, &invite).await;
    selected
}

pub(crate) async fn broadcast_room_message(
    sessions: &HashMap<String, mpsc::Sender<Message>>,
    room: &RoomState,
    message: &Message,
) {
    send_to_peers(
        sessions,
        &room.peer_ids.iter().cloned().collect::<Vec<_>>(),
        message,
    )
    .await;
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn handle_received_message(
    local_peer_id: &str,
    sessions: &HashMap<String, mpsc::Sender<Message>>,
    rooms: &mut HashMap<String, RoomState>,
    seen_messages: &mut HashSet<String>,
    discovered: &HashMap<String, PeerInfo>,
    sink: &EventSink,
    peer_id: String,
    message: Message,
) -> Result<()> {
    if message.message_type == "message.ack" {
        if seen_messages.insert(message.message_id.clone()) {
            emit_delivery_ack(sink, peer_id, &message).await?;
        }
        return Ok(());
    }
    if !seen_messages.insert(message.message_id.clone()) {
        return Ok(());
    }
    match message.message_type.as_str() {
        "room.invite" => {
            let room_id = message
                .payload
                .get("room_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_owned();
            let room_name = message
                .payload
                .get("room_name")
                .and_then(|v| v.as_str())
                .unwrap_or("同伴群聊")
                .to_owned();
            let agent_mode = message
                .payload
                .get("agent_mode")
                .and_then(|v| v.as_str())
                .filter(|value| is_valid_agent_mode(value))
                .unwrap_or(DEFAULT_AGENT_MODE)
                .to_owned();
            let owner_peer_id = message
                .payload
                .get("owner_peer_id")
                .and_then(|v| v.as_str())
                .map(str::to_owned)
                .or_else(|| Some(peer_id.clone()));
            let peer_ids = message
                .payload
                .get("participants")
                .and_then(|v| v.as_array())
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|v| v.as_str().map(str::to_owned))
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            if !room_id.is_empty() && is_room_member(&peer_ids, local_peer_id) {
                let room = rooms.entry(room_id.clone()).or_insert_with(|| RoomState {
                    room_name: room_name.clone(),
                    peer_ids: HashSet::new(),
                    agent_mode: agent_mode.clone(),
                    owner_peer_id: owner_peer_id.clone(),
                    messages: Vec::new(),
                });
                room.room_name = room_name.clone();
                room.agent_mode = agent_mode.clone();
                if let Some(owner) = &owner_peer_id {
                    room.owner_peer_id = Some(owner.clone());
                }
                room.peer_ids.extend(peer_ids.iter().cloned());
                let join = Message::room_join(
                    local_peer_id.to_owned(),
                    room_id.clone(),
                    Some(&agent_mode),
                    room.owner_peer_id.as_deref(),
                );
                send_to_peers(sessions, &[peer_id], &join).await;
                sink.send(IpcEvent::RoomJoined {
                    room_id,
                    room_name,
                    peer_ids,
                    agent_mode,
                    owner_peer_id,
                })
                .await?;
            }
        }
        "room.join" => {
            // 成员变更以消息原作者为准（中继转发时 peer_id 是转发者）。
            let member_id = message.sender.clone();
            if let Some(room_id) = message.payload.get("room_id").and_then(|v| v.as_str()) {
                let mut joined = false;
                if let Some(room) = rooms.get_mut(room_id) {
                    joined = room.peer_ids.insert(member_id.clone());
                }
                if joined {
                    let display_name = discovered
                        .get(&member_id)
                        .map(|remote| remote.display_name.clone());
                    sink.send(IpcEvent::RoomMemberJoined {
                        room_id: room_id.to_owned(),
                        peer_id: member_id.clone(),
                        display_name,
                    })
                    .await?;
                }
                // 向其他成员转发加入消息，保证全员的成员列表一致。
                if let Some(room) = rooms.get(room_id) {
                    let targets: Vec<String> = room
                        .peer_ids
                        .iter()
                        .filter(|id| *id != local_peer_id && *id != &member_id)
                        .cloned()
                        .collect();
                    send_to_peers(sessions, &targets, &message).await;
                }
            }
        }
        "room.leave" => {
            let member_id = message.sender.clone();
            if let Some(room_id) = message.payload.get("room_id").and_then(|v| v.as_str()) {
                let mut left = false;
                if let Some(room) = rooms.get_mut(room_id) {
                    left = room.peer_ids.remove(&member_id);
                }
                if left {
                    let display_name = discovered
                        .get(&member_id)
                        .map(|remote| remote.display_name.clone());
                    sink.send(IpcEvent::RoomMemberLeft {
                        room_id: room_id.to_owned(),
                        peer_id: member_id.clone(),
                        display_name,
                    })
                    .await?;
                }
                // 向其他成员转发退出消息，保证全员的成员列表一致。
                if let Some(room) = rooms.get(room_id) {
                    let targets: Vec<String> = room
                        .peer_ids
                        .iter()
                        .filter(|id| *id != local_peer_id && *id != &member_id)
                        .cloned()
                        .collect();
                    send_to_peers(sessions, &targets, &message).await;
                }
            }
        }
        "room.settings" => {
            let room_id = message
                .payload
                .get("room_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_owned();
            let agent_mode = message
                .payload
                .get("agent_mode")
                .and_then(|v| v.as_str())
                .map(str::to_owned);
            let room_name = message
                .payload
                .get("room_name")
                .and_then(|v| v.as_str())
                .map(str::to_owned);
            if apply_room_settings(
                rooms,
                local_peer_id,
                &room_id,
                agent_mode.as_deref(),
                room_name.as_deref(),
            ) {
                if let Some(room) = rooms.get(&room_id) {
                    sink.send(IpcEvent::RoomSettingsUpdated {
                        room_id: room_id.clone(),
                        agent_mode: room.agent_mode.clone(),
                        room_name: room.room_name.clone(),
                    })
                    .await?;
                    let room_snapshot = room.clone();
                    broadcast_room_message(sessions, &room_snapshot, &message).await;
                }
            }
        }
        "room.message" | "room.file" => {
            let room_id = message
                .payload
                .get("room_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_owned();
            if let Some(room) = rooms.get_mut(&room_id) {
                if !room.peer_ids.contains(local_peer_id) {
                    return Ok(());
                }
                remember_room_message(room, &message);
                acknowledge_received_message(local_peer_id, sessions, &peer_id, &message).await;
                sink.send(IpcEvent::Message {
                    peer_id: peer_id.clone(),
                    message: message.clone(),
                })
                .await?;
                let room_snapshot = room.clone();
                broadcast_room_message(sessions, &room_snapshot, &message).await;
            }
        }
        _ => {
            sink.send(IpcEvent::Message { peer_id, message }).await?;
        }
    }
    Ok(())
}

fn client_message_id(message: &Message) -> Option<&str> {
    message
        .payload
        .get("client_message_id")
        .and_then(|value| value.as_str())
        .filter(|value| !value.is_empty())
}

fn is_final_delivery_unit(message: &Message) -> bool {
    if !matches!(message.message_type.as_str(), "peer.file" | "room.file") {
        return true;
    }
    let Some(file) = message.payload.get("file") else {
        return false;
    };
    let index = file.get("chunk_index").and_then(|value| value.as_u64());
    let total = file.get("chunk_total").and_then(|value| value.as_u64());
    matches!((index, total), (Some(index), Some(total)) if total > 0 && index + 1 == total)
}

pub(crate) async fn acknowledge_received_message(
    local_peer_id: &str,
    sessions: &HashMap<String, mpsc::Sender<Message>>,
    transport_peer_id: &str,
    message: &Message,
) {
    let Some(client_id) = client_message_id(message) else {
        return;
    };
    if !is_final_delivery_unit(message) {
        return;
    }
    let Some(session) = sessions.get(transport_peer_id) else {
        return;
    };
    let ack = Message::delivery_ack(
        local_peer_id.to_owned(),
        client_id.to_owned(),
        message.message_id.clone(),
    );
    session.send(ack).await.ok();
}

async fn emit_delivery_ack(sink: &EventSink, peer_id: String, message: &Message) -> Result<()> {
    if let Some(message_id) = client_message_id(message) {
        sink.send(IpcEvent::MessageDelivered {
            peer_id,
            message_id: message_id.to_owned(),
        })
        .await?;
    }
    Ok(())
}

fn is_room_member(peer_ids: &[String], local_peer_id: &str) -> bool {
    peer_ids.iter().any(|peer_id| peer_id == local_peer_id)
}

pub(crate) fn apply_room_settings(
    rooms: &mut HashMap<String, RoomState>,
    local_peer_id: &str,
    room_id: &str,
    agent_mode: Option<&str>,
    room_name: Option<&str>,
) -> bool {
    if agent_mode.is_none() && room_name.is_none() {
        return false;
    }
    if let Some(mode) = agent_mode {
        if !is_valid_agent_mode(mode) {
            return false;
        }
    }
    let normalized_name = match room_name {
        Some(name) => match normalize_room_name(name) {
            Some(name) => Some(name),
            None => return false,
        },
        None => None,
    };
    let Some(room) = rooms.get_mut(room_id) else {
        return false;
    };
    if !room.peer_ids.contains(local_peer_id) {
        return false;
    }
    if let Some(mode) = agent_mode {
        room.agent_mode = mode.to_owned();
    }
    if let Some(name) = normalized_name {
        room.room_name = name;
    }
    true
}

pub(crate) fn filter_room_mentions(mentions: Vec<String>, room: &RoomState) -> Vec<String> {
    mentions
        .into_iter()
        .filter(|peer_id| room.peer_ids.contains(peer_id))
        .collect()
}

pub(crate) fn validate_file_transfer(
    file_id: &str,
    name: &str,
    mime_type: &str,
    size: u64,
    sha256: &str,
    data_base64: &str,
) -> Result<()> {
    anyhow::ensure!(!file_id.is_empty() && file_id.len() <= 128, "文件 ID 无效");
    anyhow::ensure!(!name.is_empty() && name.len() <= 255, "文件名无效");
    anyhow::ensure!(
        !mime_type.is_empty() && mime_type.len() <= 200,
        "文件类型无效"
    );
    anyhow::ensure!(
        size <= MAX_NEARBY_FILE_BYTES,
        "文件超过 Nearby 限制（最大 4 MiB）"
    );
    anyhow::ensure!(
        sha256.len() == 64 && sha256.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "文件 SHA-256 无效"
    );
    anyhow::ensure!(
        data_base64.len() <= (MAX_NEARBY_FILE_BYTES as usize * 2),
        "文件数据过大"
    );
    let bytes = BASE64
        .decode(data_base64)
        .context("文件数据不是有效的 Base64")?;
    anyhow::ensure!(bytes.len() as u64 == size, "文件大小与内容不一致");
    let actual_sha256 = format!("{:x}", Sha256::digest(&bytes));
    anyhow::ensure!(
        actual_sha256.eq_ignore_ascii_case(sha256),
        "文件 SHA-256 校验失败"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ipc_command_uses_stable_json_tags() {
        let command: IpcCommand = serde_json::from_str(r#"{"type":"start_discovery"}"#).unwrap();
        assert!(matches!(command, IpcCommand::StartDiscovery));
        let command: IpcCommand =
            serde_json::from_str(r#"{"type":"set_discoverable","enabled":false}"#).unwrap();
        assert!(matches!(
            command,
            IpcCommand::SetDiscoverable { enabled: false }
        ));
        let command: IpcCommand =
            serde_json::from_str(r#"{"type":"connect_peer","peer_id":"ace_peer"}"#).unwrap();
        assert!(matches!(
            command,
            IpcCommand::ConnectPeer { peer_id } if peer_id == "ace_peer"
        ));
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"send_agent_request","peer_id":"ace_peer","text":"hello"}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::SendAgentRequest { peer_id, text }
                if peer_id == "ace_peer" && text == "hello"
        ));
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"send_agent_reply","peer_id":"ace_peer","request_id":"request","text":"hi","error":false}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::SendAgentReply { peer_id, request_id, text, error: false }
                if peer_id == "ace_peer" && request_id == "request" && text == "hi"
        ));
        let event = serde_json::to_value(IpcEvent::DiscoveryStarted).unwrap();
        assert_eq!(event["type"], "discovery_started");
        let event = serde_json::to_value(IpcEvent::DiscoverabilityChanged {
            discoverable: false,
        })
        .unwrap();
        assert_eq!(event["type"], "discoverability_changed");
        assert_eq!(event["discoverable"], false);
        let event = serde_json::to_value(IpcEvent::PeerConnectionFailed {
            peer_id: "ace_peer".to_owned(),
            message: "timeout".to_owned(),
        })
        .unwrap();
        assert_eq!(event["type"], "peer_connection_failed");
    }

    #[test]
    fn room_messages_keep_room_id_and_text() {
        let message = Message::room_message("crew_a", "room_1", "hello");
        assert_eq!(message.message_type, "room.message");
        assert_eq!(message.payload["room_id"], "room_1");
        assert_eq!(message.payload["text"], "hello");

        let join = Message::room_join("crew_a", "room_1", None, None);
        assert_eq!(join.message_type, "room.join");
        assert_eq!(join.payload["room_id"], "room_1");
    }

    #[test]
    fn file_transfer_validation_accepts_small_file_and_rejects_bad_hash() {
        assert!(validate_file_transfer(
            "file_1",
            "notes.txt",
            "text/plain",
            5,
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            "aGVsbG8=",
        )
        .is_ok());
        assert!(validate_file_transfer(
            "file_1",
            "notes.txt",
            "text/plain",
            4,
            &"a".repeat(64),
            "aGVsbG8=",
        )
        .is_err());
        assert!(validate_file_transfer(
            "file_1",
            "notes.txt",
            "text/plain",
            5,
            "not-a-hash",
            "aGVsbG8=",
        )
        .is_err());
    }

    #[test]
    fn room_mentions_are_limited_to_members() {
        let room = RoomState {
            room_name: "测试群".to_owned(),
            peer_ids: ["crew_host".to_owned(), "crew_agent".to_owned()]
                .into_iter()
                .collect(),
            agent_mode: DEFAULT_AGENT_MODE.to_owned(),
            owner_peer_id: Some("crew_host".to_owned()),
            messages: Vec::new(),
        };
        assert_eq!(
            filter_room_mentions(
                vec!["crew_agent".to_owned(), "crew_outsider".to_owned()],
                &room,
            ),
            vec!["crew_agent"]
        );
    }

    #[test]
    fn peer_hello_rehydrates_passive_session_identity() {
        let peer = PeerInfo {
            protocol_version: PROTOCOL_VERSION,
            peer_id: "crew_a".to_owned(),
            peer_token: "token_a".to_owned(),
            display_name: "Agent A".to_owned(),
            agent_name: "Crew Agent".to_owned(),
            capabilities: vec!["chat".to_owned()],
            published_agents: Vec::new(),
        };
        let hello = Message::hello(&peer);
        assert_eq!(peer_info_from_hello(&hello), Some(peer));
    }

    #[test]
    fn legacy_v3_peer_stays_compatible_without_webrtc_file_capability() {
        let peer = PeerInfo {
            protocol_version: 3,
            peer_id: "crew_legacy".to_owned(),
            peer_token: "legacy-token".to_owned(),
            display_name: "Legacy".to_owned(),
            agent_name: "Legacy Agent".to_owned(),
            capabilities: vec!["chat".to_owned()],
            published_agents: Vec::new(),
        };
        assert_eq!(PROTOCOL_VERSION, 3);
        assert!(!peer_supports_webrtc_file(&peer));
        assert_eq!(peer_info_from_hello(&Message::hello(&peer)), Some(peer));
    }

    #[test]
    fn new_peer_advertises_webrtc_file_capability_once() {
        let state_dir = std::env::temp_dir().join(format!(
            "crew-nearby-capability-test-{}",
            uuid::Uuid::new_v4()
        ));
        let config = NearbyConfig {
            state_dir: Some(state_dir.clone()),
            peer_id: Some("crew_modern".to_owned()),
            capabilities: vec!["chat".to_owned(), FILE_WEBRTC_CAPABILITY.to_owned()],
            ..NearbyConfig::default()
        };
        let peer = create_peer(&config).unwrap();
        assert!(peer_supports_webrtc_file(&peer));
        assert_eq!(
            peer.capabilities
                .iter()
                .filter(|value| value.as_str() == FILE_WEBRTC_CAPABILITY)
                .count(),
            1
        );
        fs::remove_dir_all(state_dir).unwrap();
    }

    #[test]
    fn room_notifications_are_filtered_to_selected_members() {
        let participants = vec!["crew_host".to_owned(), "crew_selected".to_owned()];
        assert!(is_room_member(&participants, "crew_selected"));
        assert!(!is_room_member(&participants, "crew_unselected"));
    }

    #[test]
    fn platform_connection_policy_preserves_logical_session_rules() {
        #[cfg(target_os = "linux")]
        assert!(should_start_central_session("crew_z", "crew_a"));
        #[cfg(not(target_os = "linux"))]
        {
            assert!(should_start_central_session("crew_a", "crew_z"));
            assert!(!should_start_central_session("crew_z", "crew_a"));
        }
    }

    #[test]
    fn stale_physical_session_close_does_not_remove_current_session() {
        let mut sessions = HashMap::from([("ace_peer".to_owned(), "session_new".to_owned())]);
        assert!(!close_current_session(
            &mut sessions,
            "ace_peer",
            "session_old"
        ));
        assert_eq!(
            sessions.get("ace_peer").map(String::as_str),
            Some("session_new")
        );
        assert!(close_current_session(
            &mut sessions,
            "ace_peer",
            "session_new"
        ));
        assert!(!sessions.contains_key("ace_peer"));
    }

    #[tokio::test]
    async fn host_broadcasts_only_to_room_members() {
        let (alice_tx, mut alice_rx) = mpsc::channel(1);
        let (bob_tx, mut bob_rx) = mpsc::channel(1);
        let (carol_tx, mut carol_rx) = mpsc::channel(1);
        let sessions = HashMap::from([
            ("crew_alice".to_owned(), alice_tx),
            ("crew_bob".to_owned(), bob_tx),
            ("crew_carol".to_owned(), carol_tx),
        ]);
        let room = RoomState {
            room_name: "测试群".to_owned(),
            peer_ids: HashSet::from(["crew_alice".to_owned(), "crew_bob".to_owned()]),
            agent_mode: DEFAULT_AGENT_MODE.to_owned(),
            owner_peer_id: Some("crew_alice".to_owned()),
            messages: Vec::new(),
        };
        let message = Message::room_message("crew_alice", "room_1", "hello");

        broadcast_room_message(&sessions, &room, &message).await;

        assert_eq!(alice_rx.recv().await, Some(message.clone()));
        assert_eq!(bob_rx.recv().await, Some(message));
        assert!(carol_rx.try_recv().is_err());
    }

    #[test]
    fn room_history_is_bounded_and_persisted() {
        let state_dir =
            std::env::temp_dir().join(format!("crew-nearby-room-test-{}", uuid::Uuid::new_v4()));
        let mut rooms = HashMap::from([(
            "room_1".to_owned(),
            RoomState {
                room_name: "测试群".to_owned(),
                peer_ids: HashSet::from(["crew_local".to_owned()]),
                agent_mode: DEFAULT_AGENT_MODE.to_owned(),
                owner_peer_id: Some("crew_local".to_owned()),
                messages: Vec::new(),
            },
        )]);
        for index in 0..(ROOM_HISTORY_LIMIT + 3) {
            let message = Message::room_message("crew_local", "room_1", index.to_string());
            remember_room_message(rooms.get_mut("room_1").unwrap(), &message);
        }

        save_rooms(&state_dir, &rooms).unwrap();
        let restored = load_rooms(&state_dir).unwrap();
        let room = restored.get("room_1").unwrap();
        assert_eq!(room.room_name, "测试群");
        assert_eq!(room.messages.len(), ROOM_HISTORY_LIMIT);
        assert_eq!(room.messages.first().unwrap().payload["text"], "3");

        fs::remove_dir_all(state_dir).unwrap();
    }

    #[test]
    fn legacy_rooms_json_without_agent_mode_defaults_to_mention() {
        let state_dir =
            std::env::temp_dir().join(format!("crew-nearby-legacy-room-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&state_dir).unwrap();
        fs::write(
            state_dir.join(ROOMS_FILE_NAME),
            r#"{"room_1":{"room_name":"旧群","peer_ids":["crew_local"],"messages":[]}}"#,
        )
        .unwrap();

        let rooms = load_rooms(&state_dir).unwrap();
        let room = rooms.get("room_1").unwrap();
        assert_eq!(room.agent_mode, DEFAULT_AGENT_MODE);
        assert_eq!(room.owner_peer_id, None);

        fs::remove_dir_all(state_dir).unwrap();
    }

    #[test]
    fn dm_history_is_bounded_and_persisted() {
        let state_dir =
            std::env::temp_dir().join(format!("crew-nearby-dm-test-{}", uuid::Uuid::new_v4()));
        let mut dms: HashMap<String, Vec<Message>> = HashMap::new();
        for index in 0..(DM_HISTORY_LIMIT + 2) {
            let message = Message::peer_message("crew_peer", index.to_string(), Vec::new());
            remember_dm_message(&mut dms, "crew_peer", &message);
        }
        let request = Message::agent_request("crew_peer", "ping");
        remember_dm_message(&mut dms, "crew_peer", &request);

        assert_eq!(dms.get("crew_peer").unwrap().len(), DM_HISTORY_LIMIT);
        save_dms(&state_dir, &dms).unwrap();
        let restored = load_dms(&state_dir).unwrap();
        let history = restored.get("crew_peer").unwrap();
        assert_eq!(history.len(), DM_HISTORY_LIMIT);
        assert_eq!(history.first().unwrap().payload["text"], "3");
        assert_eq!(history.last().unwrap().message_type, "agent.request");

        fs::remove_dir_all(state_dir).unwrap();
    }

    #[test]
    fn new_ipc_commands_use_stable_json_tags() {
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"send_peer_message","peer_id":"ace_peer","text":"hi"}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::SendPeerMessage { peer_id, text, mentions, .. }
                if peer_id == "ace_peer" && text == "hi" && mentions.is_empty()
        ));
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"send_peer_message","peer_id":"ace_peer","text":"hi","mentions":["ace_agent"]}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::SendPeerMessage { mentions, .. } if mentions == ["ace_agent"]
        ));
        let command: IpcCommand = serde_json::from_str(
            &format!(
                r#"{{"type":"send_peer_file","peer_id":"ace_peer","file_id":"file_1","name":"note.txt","mime_type":"text/plain","size":5,"sha256":"{}","file_path":"/tmp/note.txt"}}"#,
                "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
            ),
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::SendPeerFile { peer_id, file_id, .. }
                if peer_id == "ace_peer" && file_id == "file_1"
        ));
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"set_room_agent_mode","room_id":"room_1","agent_mode":"quiet"}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::SetRoomAgentMode { room_id, agent_mode, room_name }
                if room_id == "room_1" && agent_mode.as_deref() == Some("quiet") && room_name.is_none()
        ));
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"set_room_agent_mode","room_id":"room_1","room_name":"新群名"}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::SetRoomAgentMode { agent_mode, room_name, .. }
                if agent_mode.is_none() && room_name.as_deref() == Some("新群名")
        ));
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"invite_to_room","room_id":"room_1","peer_ids":["a","b"]}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::InviteToRoom { room_id, peer_ids }
                if room_id == "room_1" && peer_ids == ["a", "b"]
        ));
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"create_room","room_id":"room_1","room_name":"群","peer_ids":["a"],"agent_mode":"auto"}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::CreateRoom { agent_mode: Some(mode), .. } if mode == "auto"
        ));
        let command: IpcCommand = serde_json::from_str(
            r#"{"type":"create_room","room_id":"room_1","room_name":"群","peer_ids":["a"]}"#,
        )
        .unwrap();
        assert!(matches!(
            command,
            IpcCommand::CreateRoom {
                agent_mode: None,
                ..
            }
        ));
    }

    #[test]
    fn room_settings_apply_only_for_members_with_valid_mode() {
        let mut rooms = HashMap::from([(
            "room_1".to_owned(),
            RoomState {
                room_name: "测试群".to_owned(),
                peer_ids: HashSet::from(["crew_local".to_owned()]),
                agent_mode: DEFAULT_AGENT_MODE.to_owned(),
                owner_peer_id: Some("crew_local".to_owned()),
                messages: Vec::new(),
            },
        )]);

        assert!(apply_room_settings(
            &mut rooms,
            "crew_local",
            "room_1",
            Some("quiet"),
            None
        ));
        assert_eq!(rooms.get("room_1").unwrap().agent_mode, "quiet");
        assert!(!apply_room_settings(
            &mut rooms,
            "crew_outsider",
            "room_1",
            Some("auto"),
            None
        ));
        assert!(!apply_room_settings(
            &mut rooms,
            "crew_local",
            "room_1",
            Some("loud"),
            None
        ));
        assert!(!apply_room_settings(
            &mut rooms,
            "crew_local",
            "room_missing",
            Some("auto"),
            None
        ));
        assert!(!apply_room_settings(
            &mut rooms,
            "crew_local",
            "room_1",
            None,
            None
        ));
        assert_eq!(rooms.get("room_1").unwrap().agent_mode, "quiet");
    }

    #[test]
    fn room_settings_can_rename_without_touching_agent_mode() {
        let mut rooms = HashMap::from([(
            "room_1".to_owned(),
            RoomState {
                room_name: "测试群".to_owned(),
                peer_ids: HashSet::from(["crew_local".to_owned()]),
                agent_mode: "auto".to_owned(),
                owner_peer_id: Some("crew_local".to_owned()),
                messages: Vec::new(),
            },
        )]);

        assert!(apply_room_settings(
            &mut rooms,
            "crew_local",
            "room_1",
            None,
            Some("  新群名  ")
        ));
        let room = rooms.get("room_1").unwrap();
        assert_eq!(room.room_name, "新群名");
        assert_eq!(room.agent_mode, "auto");
        assert!(!apply_room_settings(
            &mut rooms,
            "crew_local",
            "room_1",
            None,
            Some("   ")
        ));
        assert!(!apply_room_settings(
            &mut rooms,
            "crew_local",
            "room_1",
            None,
            Some(&"长".repeat(crate::protocol::MAX_ROOM_NAME_CHARS + 1)),
        ));
        assert_eq!(rooms.get("room_1").unwrap().room_name, "新群名");
    }

    #[test]
    fn history_snapshot_serializes_rooms_and_dms() {
        let mut room = RoomState {
            room_name: "测试群".to_owned(),
            peer_ids: HashSet::from(["crew_local".to_owned()]),
            agent_mode: "auto".to_owned(),
            owner_peer_id: Some("crew_local".to_owned()),
            messages: Vec::new(),
        };
        let room_message = Message::room_message("crew_local", "room_1", "hello");
        remember_room_message(&mut room, &room_message);
        let rooms = HashMap::from([("room_1".to_owned(), room)]);
        let mut dms: HashMap<String, Vec<Message>> = HashMap::new();
        let dm = Message::peer_message("crew_peer", "hi", Vec::new());
        remember_dm_message(&mut dms, "crew_peer", &dm);

        let snapshot = serde_json::to_value(build_history_snapshot(&rooms, &dms)).unwrap();
        assert_eq!(snapshot["type"], "history_snapshot");
        assert_eq!(snapshot["rooms"][0]["room_id"], "room_1");
        assert_eq!(snapshot["rooms"][0]["agent_mode"], "auto");
        assert_eq!(snapshot["rooms"][0]["owner_peer_id"], "crew_local");
        assert_eq!(
            snapshot["rooms"][0]["messages"][0]["payload"]["text"],
            "hello"
        );
        assert_eq!(snapshot["dms"][0]["peer_id"], "crew_peer");
        assert_eq!(snapshot["dms"][0]["messages"][0]["type"], "peer.message");

        let event = serde_json::to_value(IpcEvent::PeerMessageReceived {
            peer_id: "crew_peer".to_owned(),
            display_name: "Peer".to_owned(),
            text: "hi".to_owned(),
            mentions: Vec::new(),
            message_id: "m_1".to_owned(),
            timestamp: 1_700_000_000,
        })
        .unwrap();
        assert_eq!(event["type"], "peer_message_received");
        assert_eq!(event["timestamp"], 1_700_000_000);
        let event = serde_json::to_value(IpcEvent::RoomSettingsUpdated {
            room_id: "room_1".to_owned(),
            agent_mode: "quiet".to_owned(),
            room_name: "新群名".to_owned(),
        })
        .unwrap();
        assert_eq!(event["type"], "room_settings_updated");
        assert_eq!(event["agent_mode"], "quiet");
        assert_eq!(event["room_name"], "新群名");
    }

    #[tokio::test]
    async fn add_room_members_merges_without_wiping_history() {
        let (bob_tx, mut bob_rx) = mpsc::channel(4);
        let (carol_tx, mut carol_rx) = mpsc::channel(4);
        let sessions = HashMap::from([
            ("crew_bob".to_owned(), bob_tx),
            ("crew_carol".to_owned(), carol_tx),
        ]);
        let mut room = RoomState {
            room_name: "测试群".to_owned(),
            peer_ids: HashSet::from(["crew_alice".to_owned(), "crew_bob".to_owned()]),
            agent_mode: "auto".to_owned(),
            owner_peer_id: Some("crew_alice".to_owned()),
            messages: Vec::new(),
        };
        let history = Message::room_message("crew_alice", "room_1", "之前的消息");
        remember_room_message(&mut room, &history);
        let mut rooms = HashMap::from([("room_1".to_owned(), room)]);

        // 既有成员不会重复收到邀请；新成员收到带完整成员列表的邀请。
        let added = add_room_members(
            &sessions,
            &mut rooms,
            "crew_alice",
            "room_1",
            vec!["crew_bob".to_owned(), "crew_carol".to_owned()],
        )
        .await;
        assert_eq!(added, vec!["crew_carol".to_owned()]);
        let room = rooms.get("room_1").unwrap();
        assert!(room.peer_ids.contains("crew_carol"));
        assert_eq!(room.messages.len(), 1);
        assert_eq!(room.messages[0].payload["text"], "之前的消息");
        assert!(bob_rx.try_recv().is_err());
        let invite = carol_rx.recv().await.unwrap();
        assert_eq!(invite.message_type, "room.invite");
        assert_eq!(invite.payload["agent_mode"], "auto");
        assert_eq!(invite.payload["owner_peer_id"], "crew_alice");
        assert_eq!(invite.payload["participants"].as_array().unwrap().len(), 3);
    }

    #[tokio::test]
    async fn room_join_and_leave_update_membership_once() {
        let mut rooms = HashMap::from([(
            "room_1".to_owned(),
            RoomState {
                room_name: "测试群".to_owned(),
                peer_ids: HashSet::from(["crew_local".to_owned()]),
                agent_mode: DEFAULT_AGENT_MODE.to_owned(),
                owner_peer_id: Some("crew_local".to_owned()),
                messages: Vec::new(),
            },
        )]);
        let sessions = HashMap::new();
        let mut seen_messages = HashSet::new();
        let discovered = HashMap::new();
        let sink = EventSink::stdout();

        let join = Message::room_join("crew_bob", "room_1", None, None);
        handle_received_message(
            "crew_local",
            &sessions,
            &mut rooms,
            &mut seen_messages,
            &discovered,
            &sink,
            "crew_bob".to_owned(),
            join.clone(),
        )
        .await
        .unwrap();
        assert!(rooms.get("room_1").unwrap().peer_ids.contains("crew_bob"));
        // 重复的 join 事件按 message_id 去重，成员状态保持幂等。
        handle_received_message(
            "crew_local",
            &sessions,
            &mut rooms,
            &mut seen_messages,
            &discovered,
            &sink,
            "crew_bob".to_owned(),
            join,
        )
        .await
        .unwrap();
        assert_eq!(rooms.get("room_1").unwrap().peer_ids.len(), 2);

        let leave = Message::room_leave("crew_bob", "room_1");
        handle_received_message(
            "crew_local",
            &sessions,
            &mut rooms,
            &mut seen_messages,
            &discovered,
            &sink,
            "crew_bob".to_owned(),
            leave,
        )
        .await
        .unwrap();
        assert!(!rooms.get("room_1").unwrap().peer_ids.contains("crew_bob"));
    }
}
