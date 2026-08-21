use crate::identity::{
    default_agent_name, default_display_name, load_nearby_settings, load_or_create_peer_id,
    resolve_state_dir, save_nearby_settings, NearbySettings,
};
#[cfg(not(target_os = "linux"))]
use crate::protocol::should_initiate;
use crate::protocol::{
    FileChunk, FrameCodec, Message, PeerInfo, Reassembler, ReplyReference, INCOMING_MESSAGE_UUID,
    OUTGOING_MESSAGE_UUID, PEER_INFO_UUID, PROTOCOL_VERSION, SERVICE_UUID,
};
use crate::runtime::NearbyConfig;
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
    api::{Central, CentralEvent, Manager as _, Peripheral as _, ScanFilter, WriteType},
    platform::{Manager, Peripheral},
};
use futures::StreamExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    env,
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{self, AsyncBufReadExt, AsyncWriteExt, BufReader, BufWriter},
    sync::{mpsc, Mutex},
};

const FILE_CHUNK_BASE64_BYTES: usize = 8 * 1024;
const MAX_NEARBY_FILE_BYTES: u64 = 4 * 1024 * 1024;

fn diagnostic_device_id(value: &str) -> String {
    let suffix = value.chars().rev().take(8).collect::<String>();
    format!("…{}", suffix.chars().rev().collect::<String>())
}

fn diagnostic_characteristics(peripheral: &Peripheral) -> String {
    let mut uuids = peripheral
        .characteristics()
        .into_iter()
        .map(|characteristic| characteristic.uuid.to_string())
        .collect::<Vec<_>>();
    uuids.sort_unstable();
    uuids.join(",")
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum IpcCommand {
    StartDiscovery,
    StopDiscovery,
    SetDiscoverable {
        enabled: bool,
    },
    CreateRoom {
        room_id: String,
        room_name: String,
        peer_ids: Vec<String>,
    },
    SendRoomMessage {
        room_id: String,
        text: String,
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
        data_base64: String,
        #[serde(default)]
        mentions: Vec<String>,
        #[serde(default)]
        reply_to: Option<ReplyReference>,
    },
    LeaveRoom {
        room_id: String,
    },
    Shutdown,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum IpcEvent {
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
    RoomCreated {
        room_id: String,
        room_name: String,
        peer_ids: Vec<String>,
    },
    RoomJoined {
        room_id: String,
        room_name: String,
        peer_ids: Vec<String>,
    },
    RoomLeft {
        room_id: String,
    },
    Message {
        peer_id: String,
        message: Message,
    },
    Error {
        message: String,
    },
}

#[derive(Debug)]
struct RoomState {
    peer_ids: HashSet<String>,
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
        outbound: mpsc::Sender<Message>,
    },
    Received {
        peer_id: String,
        message: Message,
    },
    Closed(String),
    Failed {
        peer_id: String,
        error: String,
    },
}

#[derive(Clone)]
struct EventSink(Arc<Mutex<BufWriter<io::Stdout>>>);

impl EventSink {
    async fn send(&self, event: IpcEvent) -> Result<()> {
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

pub async fn run(config: NearbyConfig) -> Result<()> {
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
    configure_server(&server).await?;
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
    let mut discovered: HashMap<String, PeerInfo> = HashMap::new();
    let mut rooms: HashMap<String, RoomState> = HashMap::new();
    let mut connection_candidates = HashSet::new();
    let mut server_clients: HashMap<String, String> = HashMap::new();
    let mut server_reassemblers: HashMap<String, Reassembler> = HashMap::new();
    let mut seen_messages = HashSet::new();

    sink.send(IpcEvent::Ready {
        peer: peer.clone(),
        discoverable,
    })
    .await?;
    sink.send(IpcEvent::DiscoveryStarted).await?;

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
                    IpcCommand::CreateRoom { room_id, room_name, peer_ids } => {
                        let selected: Vec<String> = peer_ids.into_iter()
                            .filter(|peer_id| sessions.contains_key(peer_id))
                            .collect();
                        if selected.is_empty() {
                            sink.send(IpcEvent::Error { message: "没有可用的同伴连接".to_owned() }).await?;
                            continue;
                        }
                        let mut participants = selected.iter().cloned().collect::<HashSet<_>>();
                        participants.insert(peer.peer_id.clone());
                        rooms.insert(room_id.clone(), RoomState { peer_ids: participants.clone() });
                        let invite = Message::room_invite(
                            peer.peer_id.clone(),
                            room_id.clone(),
                            room_name.clone(),
                            participants.iter().cloned().collect(),
                        );
                        send_to_peers(&sessions, &selected, &invite).await;
                        sink.send(IpcEvent::RoomCreated {
                            room_id,
                            room_name,
                            peer_ids: participants.into_iter().collect(),
                        }).await?;
                    }
                    IpcCommand::SendRoomMessage { room_id, text, mentions, reply_to } => {
                        if let Some(room) = rooms.get(&room_id) {
                            let mentions = filter_room_mentions(mentions, room);
                            let message = Message::room_message_with_context(
                                peer.peer_id.clone(),
                                room_id.clone(),
                                text,
                                mentions,
                                reply_to,
                            );
                            seen_messages.insert(message.message_id.clone());
                            broadcast_room_message(&sessions, room, &message).await;
                            sink.send(IpcEvent::Message { peer_id: peer.peer_id.clone(), message }).await?;
                        } else {
                            sink.send(IpcEvent::Error { message: "群聊不存在或已退出".to_owned() }).await?;
                        }
                    }
                    IpcCommand::SendRoomFile { room_id, file_id, name, mime_type, size, sha256, data_base64, mentions, reply_to } => {
                        if let Some(room) = rooms.get(&room_id) {
                            if let Err(error) = validate_file_transfer(&file_id, &name, &mime_type, size, &sha256, &data_base64) {
                                sink.send(IpcEvent::Error { message: error.to_string() }).await?;
                                continue;
                            }
                            let mentions = filter_room_mentions(mentions, room);
                            let chunks = if data_base64.is_empty() {
                                vec![&[][..]]
                            } else {
                                data_base64.as_bytes().chunks(FILE_CHUNK_BASE64_BYTES).collect::<Vec<_>>()
                            };
                            let chunk_total = u32::try_from(chunks.len().max(1)).context("file has too many chunks")?;
                            for (chunk_index, data) in chunks.into_iter().enumerate() {
                                let file = FileChunk {
                                    file_id: file_id.clone(),
                                    name: name.clone(),
                                    mime_type: mime_type.clone(),
                                    size,
                                    sha256: sha256.clone(),
                                    chunk_index: u32::try_from(chunk_index).context("file chunk index overflow")?,
                                    chunk_total,
                                    data_base64: String::from_utf8(data.to_vec()).context("file data is not valid base64 text")?,
                                };
                                let message = Message::room_file(
                                    peer.peer_id.clone(),
                                    room_id.clone(),
                                    file,
                                    mentions.clone(),
                                    reply_to.clone(),
                                );
                                seen_messages.insert(message.message_id.clone());
                                broadcast_room_message(&sessions, room, &message).await;
                                sink.send(IpcEvent::Message { peer_id: peer.peer_id.clone(), message }).await?;
                            }
                        } else {
                            sink.send(IpcEvent::Error { message: "群聊不存在或已退出".to_owned() }).await?;
                        }
                    }
                    IpcCommand::LeaveRoom { room_id } => {
                        if let Some(room) = rooms.remove(&room_id) {
                            let leave = Message::room_leave(peer.peer_id.clone(), room_id.clone());
                            send_to_peers(&sessions, &room.peer_ids.iter().filter(|id| *id != &peer.peer_id).cloned().collect::<Vec<_>>(), &leave).await;
                            sink.send(IpcEvent::RoomLeft { room_id }).await?;
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
                    tokio::spawn(async move {
                        if let Err(error) = connect_to_peer(peripheral, local_peer, device.clone(), event_tx.clone()).await {
                            eprintln!("[nearby][session] device={device} result=failed error={error}");
                            let _ = event_tx.send(SessionEvent::Failed { peer_id: key, error: error.to_string() }).await;
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
                    SessionEvent::Ready { peer: remote, outbound } => {
                        eprintln!("[nearby][session] peer_connected peer_id={}", remote.peer_id);
                        discovered.insert(remote.peer_id.clone(), remote.clone());
                        sessions.insert(remote.peer_id.clone(), outbound);
                        sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                    }
                    SessionEvent::Received { peer_id, message } => {
                        handle_received_message(
                            &peer.peer_id,
                            &sessions,
                            &mut rooms,
                            &mut seen_messages,
                            &sink,
                            peer_id,
                            message,
                        ).await?;
                    }
                    SessionEvent::Closed(peer_id) => {
                        eprintln!("[nearby][session] peer_disconnected peer_id={peer_id}");
                        sessions.remove(&peer_id);
                        server_clients.retain(|_, connected_peer_id| connected_peer_id != &peer_id);
                        sink.send(IpcEvent::PeerDisconnected { peer_id }).await?;
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
        IpcCommand::CreateRoom { .. } => "create_room",
        IpcCommand::SendRoomMessage { .. } => "send_room_message",
        IpcCommand::SendRoomFile { .. } => "send_room_file",
        IpcCommand::LeaveRoom { .. } => "leave_room",
        IpcCommand::Shutdown => "shutdown",
    }
}

fn create_peer(config: &NearbyConfig) -> Result<PeerInfo> {
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
        capabilities: config.capabilities.clone(),
    })
}

async fn configure_server(server: &Arc<Mutex<ServerPeripheral>>) -> Result<()> {
    let service = nearby_service();
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

fn nearby_service() -> Service {
    Service {
        uuid: SERVICE_UUID,
        primary: true,
        characteristics: vec![
            ServerCharacteristic {
                uuid: PEER_INFO_UUID,
                properties: vec![CharacteristicProperty::Read],
                permissions: vec![AttributePermission::Readable],
                value: None,
                descriptors: vec![],
            },
            ServerCharacteristic {
                uuid: INCOMING_MESSAGE_UUID,
                properties: vec![CharacteristicProperty::Write],
                permissions: vec![AttributePermission::Writeable],
                value: None,
                descriptors: vec![],
            },
            ServerCharacteristic {
                uuid: OUTGOING_MESSAGE_UUID,
                properties: vec![CharacteristicProperty::Notify],
                permissions: vec![],
                value: None,
                descriptors: vec![],
            },
        ],
    }
}

async fn connect_to_peer(
    peripheral: Peripheral,
    local_peer: PeerInfo,
    device: String,
    event_tx: mpsc::Sender<SessionEvent>,
) -> Result<()> {
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
    let remote = PeerInfo::decode(
        &peripheral
            .read(&peer_info_characteristic)
            .await
            .context("failed to read remote PeerInfo")?,
    )
    .context("remote PeerInfo is not valid JSON")?;
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
        eprintln!("[nearby][session] device={device} stage=duplicate_connection_close");
        peripheral.disconnect().await.ok();
        return Ok(());
    }
    peripheral
        .subscribe(&outgoing_characteristic)
        .await
        .context("failed to subscribe to remote OutgoingMessage")?;
    eprintln!("[nearby][session] device={device} stage=notifications_subscribed");
    let mut notifications = peripheral
        .notifications()
        .await
        .context("failed to open BLE notification stream")?;
    let (outbound, mut outbound_rx) = mpsc::channel(64);
    let max_payload = FrameCodec::frame_payload_capacity(peripheral.mtu());
    let hello = Message::hello(&local_peer);
    let hello_frames = FrameCodec::fragment(&hello.encode()?, max_payload, 1)?;
    eprintln!(
        "[nearby][session] device={device} stage=hello_write mtu={} frame_payload={} frame_count={}",
        peripheral.mtu(),
        max_payload,
        hello_frames.len()
    );
    for frame in hello_frames {
        peripheral
            .write(&incoming_characteristic, &frame, WriteType::WithResponse)
            .await
            .context("failed to send BLE peer hello")?;
    }
    event_tx
        .send(SessionEvent::Ready {
            peer: remote.clone(),
            outbound,
        })
        .await
        .context("failed to publish ready BLE session")?;
    eprintln!("[nearby][session] device={device} stage=ready");
    let mut transfer_id = 2_u32;
    let mut reassembler = Reassembler::default();
    loop {
        tokio::select! {
            Some(message) = outbound_rx.recv() => {
                let frames = FrameCodec::fragment(&message.encode()?, max_payload, transfer_id)?;
                eprintln!("[nearby][session] device={device} stage=message_write message_type={} frame_count={}", message.message_type, frames.len());
                transfer_id = transfer_id.wrapping_add(1);
                for frame in frames { peripheral.write(&incoming_characteristic, &frame, WriteType::WithResponse).await.context("failed to write BLE message frame")?; }
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
        .send(SessionEvent::Closed(remote.peer_id))
        .await
        .ok();
    Ok(())
}

async fn handle_server_event(
    event: PeripheralEvent,
    peer: &PeerInfo,
    server: &Arc<Mutex<ServerPeripheral>>,
    event_tx: &mpsc::Sender<SessionEvent>,
    server_clients: &mut HashMap<String, String>,
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
                        server_clients.insert(client, remote.peer_id.clone());
                        spawn_server_writer(Arc::clone(server), outbound_rx);
                        event_tx
                            .send(SessionEvent::Ready {
                                peer: remote,
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
                if let Some(peer_id) = server_clients.remove(&request.client) {
                    reassemblers.remove(&request.client);
                    event_tx.send(SessionEvent::Closed(peer_id)).await.ok();
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

async fn broadcast_room_message(
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
async fn handle_received_message(
    local_peer_id: &str,
    sessions: &HashMap<String, mpsc::Sender<Message>>,
    rooms: &mut HashMap<String, RoomState>,
    seen_messages: &mut HashSet<String>,
    sink: &EventSink,
    peer_id: String,
    message: Message,
) -> Result<()> {
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
                rooms.insert(
                    room_id.clone(),
                    RoomState {
                        peer_ids: peer_ids.iter().cloned().collect(),
                    },
                );
                let join = Message::room_join(local_peer_id.to_owned(), room_id.clone());
                send_to_peers(sessions, &[peer_id], &join).await;
                sink.send(IpcEvent::RoomJoined {
                    room_id,
                    room_name,
                    peer_ids,
                })
                .await?;
            }
        }
        "room.join" => {
            if let Some(room_id) = message.payload.get("room_id").and_then(|v| v.as_str()) {
                if let Some(room) = rooms.get_mut(room_id) {
                    room.peer_ids.insert(peer_id);
                }
            }
        }
        "room.leave" => {
            if let Some(room_id) = message.payload.get("room_id").and_then(|v| v.as_str()) {
                if let Some(room) = rooms.get_mut(room_id) {
                    room.peer_ids.remove(&peer_id);
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
            if let Some(room) = rooms.get(&room_id) {
                if !room.peer_ids.contains(local_peer_id) {
                    return Ok(());
                }
                sink.send(IpcEvent::Message {
                    peer_id: peer_id.clone(),
                    message: message.clone(),
                })
                .await?;
                broadcast_room_message(sessions, room, &message).await;
            }
        }
        _ => {
            sink.send(IpcEvent::Message { peer_id, message }).await?;
        }
    }
    Ok(())
}

fn is_room_member(peer_ids: &[String], local_peer_id: &str) -> bool {
    peer_ids.iter().any(|peer_id| peer_id == local_peer_id)
}

fn filter_room_mentions(mentions: Vec<String>, room: &RoomState) -> Vec<String> {
    mentions
        .into_iter()
        .filter(|peer_id| room.peer_ids.contains(peer_id))
        .collect()
}

fn validate_file_transfer(
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
        let event = serde_json::to_value(IpcEvent::DiscoveryStarted).unwrap();
        assert_eq!(event["type"], "discovery_started");
        let event = serde_json::to_value(IpcEvent::DiscoverabilityChanged {
            discoverable: false,
        })
        .unwrap();
        assert_eq!(event["type"], "discoverability_changed");
        assert_eq!(event["discoverable"], false);
    }

    #[test]
    fn room_messages_keep_room_id_and_text() {
        let message = Message::room_message("crew_a", "room_1", "hello");
        assert_eq!(message.message_type, "room.message");
        assert_eq!(message.payload["room_id"], "room_1");
        assert_eq!(message.payload["text"], "hello");

        let join = Message::room_join("crew_a", "room_1");
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
            peer_ids: ["crew_host".to_owned(), "crew_agent".to_owned()]
                .into_iter()
                .collect(),
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
        };
        let hello = Message::hello(&peer);
        assert_eq!(peer_info_from_hello(&hello), Some(peer));
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
            peer_ids: HashSet::from(["crew_alice".to_owned(), "crew_bob".to_owned()]),
        };
        let message = Message::room_message("crew_alice", "room_1", "hello");

        broadcast_room_message(&sessions, &room, &message).await;

        assert_eq!(alice_rx.recv().await, Some(message.clone()));
        assert_eq!(bob_rx.recv().await, Some(message));
        assert!(carol_rx.try_recv().is_err());
    }
}
