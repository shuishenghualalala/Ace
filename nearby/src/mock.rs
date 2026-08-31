use crate::{
    identity::{load_nearby_settings, resolve_state_dir, save_nearby_settings, NearbySettings},
    ipc::{
        add_room_members, broadcast_room_message, build_history_snapshot, create_peer,
        filter_room_agent_mentions, filter_room_mentions, handle_received_message, load_dms,
        load_rooms, peer_supports_webrtc_file, remember_dm_message, remember_room_message,
        save_dms, save_rooms, unix_timestamp_seconds, validate_file_transfer, EventSink,
        IpcCommand, IpcEvent, RoomState,
    },
    protocol::{
        is_valid_agent_mode, normalize_room_name, FileChunk, Message, PeerInfo, DEFAULT_AGENT_MODE,
    },
    runtime::NearbyConfig,
};
use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use serde::{Deserialize, Serialize};
use std::{
    collections::{HashMap, HashSet},
    net::SocketAddr,
    sync::Arc,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    sync::{mpsc, Mutex},
};

pub const DEFAULT_ENDPOINT: &str = "127.0.0.1:39201";
const FILE_CHUNK_BASE64_BYTES: usize = 8 * 1024;

#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum BusRequest {
    Register { peer: PeerInfo, discoverable: bool },
    Connect { peer_id: String },
    Send { to: String, message: Message },
    Unregister,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum BusEvent {
    PeerDiscovered { peer: PeerInfo },
    PeerConnected { peer: PeerInfo },
    PeerDisconnected { peer_id: String },
    Message { peer_id: String, message: Message },
}

#[derive(Clone)]
struct BusPeer {
    peer: PeerInfo,
    discoverable: bool,
    events: mpsc::Sender<BusEvent>,
}

type BusPeers = Arc<Mutex<HashMap<String, BusPeer>>>;

pub async fn run_bus(endpoint: Option<String>) -> Result<()> {
    let endpoint = endpoint.unwrap_or_else(|| DEFAULT_ENDPOINT.to_owned());
    let address: SocketAddr = endpoint
        .parse()
        .with_context(|| format!("invalid Mock Bus endpoint {endpoint}"))?;
    let listener = TcpListener::bind(address)
        .await
        .with_context(|| format!("failed to bind Mock Bus at {endpoint}"))?;
    eprintln!("[nearby][mock] bus_listening endpoint={endpoint}");
    let peers: BusPeers = Arc::new(Mutex::new(HashMap::new()));
    loop {
        let (stream, _) = listener.accept().await.context("Mock Bus accept failed")?;
        let peers = Arc::clone(&peers);
        tokio::spawn(async move {
            if let Err(error) = handle_bus_connection(stream, peers).await {
                eprintln!("[nearby][mock] bus_client_error error={error}");
            }
        });
    }
}

async fn handle_bus_connection(stream: TcpStream, peers: BusPeers) -> Result<()> {
    let (reader, mut writer) = stream.into_split();
    let (event_tx, mut event_rx) = mpsc::channel::<BusEvent>(128);
    let writer_task = tokio::spawn(async move {
        while let Some(event) = event_rx.recv().await {
            let line = serde_json::to_string(&event).context("failed to encode Mock Bus event")?;
            writer.write_all(line.as_bytes()).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;
        }
        Ok::<(), anyhow::Error>(())
    });
    let mut lines = BufReader::new(reader).lines();
    let mut local_peer_id: Option<String> = None;

    while let Some(line) = lines
        .next_line()
        .await
        .context("failed to read Mock Bus request")?
    {
        let request: BusRequest =
            serde_json::from_str(&line).context("invalid Mock Bus request")?;
        match request {
            BusRequest::Register { peer, discoverable } => {
                let previous = {
                    let mut peers = peers.lock().await;
                    let existing = peers
                        .values()
                        .filter(|candidate| candidate.discoverable)
                        .map(|candidate| candidate.peer.clone())
                        .collect::<Vec<_>>();
                    peers.insert(
                        peer.peer_id.clone(),
                        BusPeer {
                            peer: peer.clone(),
                            discoverable,
                            events: event_tx.clone(),
                        },
                    );
                    existing
                };
                local_peer_id = Some(peer.peer_id.clone());
                for candidate in previous {
                    event_tx
                        .send(BusEvent::PeerDiscovered { peer: candidate })
                        .await
                        .ok();
                }
                if discoverable {
                    let targets = peers
                        .lock()
                        .await
                        .values()
                        .filter(|candidate| {
                            candidate.peer.peer_id != peer.peer_id && candidate.discoverable
                        })
                        .map(|candidate| candidate.events.clone())
                        .collect::<Vec<_>>();
                    for target in targets {
                        target
                            .send(BusEvent::PeerDiscovered { peer: peer.clone() })
                            .await
                            .ok();
                    }
                }
            }
            BusRequest::Connect { peer_id } => {
                let Some(local_id) = local_peer_id.as_deref() else {
                    bail!("Mock Bus client must register before connecting");
                };
                let (local_events, remote) = {
                    let peers = peers.lock().await;
                    (
                        peers.get(local_id).map(|peer| peer.events.clone()),
                        peers.get(&peer_id).cloned(),
                    )
                };
                if let Some(remote) = remote {
                    if let Some(local_events) = local_events {
                        local_events
                            .send(BusEvent::PeerConnected {
                                peer: remote.peer.clone(),
                            })
                            .await
                            .ok();
                    }
                    remote
                        .events
                        .send(BusEvent::PeerConnected {
                            peer: peers
                                .lock()
                                .await
                                .get(local_id)
                                .map(|peer| peer.peer.clone())
                                .context("Mock Bus local peer disappeared")?,
                        })
                        .await
                        .ok();
                }
            }
            BusRequest::Send { to, message } => {
                let Some(local_id) = local_peer_id.as_deref() else {
                    bail!("Mock Bus client must register before sending");
                };
                let target = peers.lock().await.get(&to).map(|peer| peer.events.clone());
                if let Some(target) = target {
                    target
                        .send(BusEvent::Message {
                            peer_id: local_id.to_owned(),
                            message,
                        })
                        .await
                        .ok();
                }
            }
            BusRequest::Unregister => break,
        }
    }

    if let Some(peer_id) = local_peer_id {
        let targets = {
            let mut peers = peers.lock().await;
            peers.remove(&peer_id);
            peers
                .values()
                .map(|peer| peer.events.clone())
                .collect::<Vec<_>>()
        };
        for target in targets {
            target
                .send(BusEvent::PeerDisconnected {
                    peer_id: peer_id.clone(),
                })
                .await
                .ok();
        }
    }
    writer_task.abort();
    Ok(())
}

struct MockClient {
    requests: mpsc::Sender<BusRequest>,
    events: mpsc::Receiver<BusEvent>,
}

impl MockClient {
    async fn connect(endpoint: &str, peer: &PeerInfo, discoverable: bool) -> Result<Self> {
        let stream = TcpStream::connect(endpoint)
            .await
            .with_context(|| format!("failed to connect to Mock Bus {endpoint}"))?;
        let (reader, mut writer) = stream.into_split();
        let (request_tx, mut request_rx) = mpsc::channel::<BusRequest>(128);
        let (event_tx, event_rx) = mpsc::channel::<BusEvent>(128);
        tokio::spawn(async move {
            while let Some(request) = request_rx.recv().await {
                let line = match serde_json::to_string(&request) {
                    Ok(line) => line,
                    Err(_) => break,
                };
                if writer.write_all(line.as_bytes()).await.is_err()
                    || writer.write_all(b"\n").await.is_err()
                    || writer.flush().await.is_err()
                {
                    break;
                }
            }
        });
        tokio::spawn(async move {
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                let Ok(event) = serde_json::from_str::<BusEvent>(&line) else {
                    continue;
                };
                if event_tx.send(event).await.is_err() {
                    break;
                }
            }
        });
        request_tx
            .send(BusRequest::Register {
                peer: peer.clone(),
                discoverable,
            })
            .await
            .context("failed to register with Mock Bus")?;
        Ok(Self {
            requests: request_tx,
            events: event_rx,
        })
    }
}

pub async fn run(config: NearbyConfig) -> Result<()> {
    let state_dir = resolve_state_dir(config.state_dir.as_deref());
    let discoverable = config
        .discoverable
        .unwrap_or(load_nearby_settings(&state_dir)?.discoverable);
    let peer = create_peer(&config)?;
    let endpoint = config.mock_endpoint.as_deref().unwrap_or(DEFAULT_ENDPOINT);
    let mut client = MockClient::connect(endpoint, &peer, discoverable).await?;
    let sink = EventSink::stdout();
    sink.send(IpcEvent::Ready {
        peer: peer.clone(),
        discoverable,
    })
    .await?;
    sink.send(IpcEvent::DiscoveryStarted).await?;

    let mut rooms = load_rooms(&state_dir)?;
    let mut dms = load_dms(&state_dir)?;
    sink.send(build_history_snapshot(&rooms, &dms)).await?;
    for (room_id, room) in rooms.clone() {
        sink.send(IpcEvent::RoomRestored {
            room_id,
            room_name: room.room_name,
            peer_ids: room.peer_ids.into_iter().collect(),
            agent_mode: room.agent_mode,
            owner_peer_id: room.owner_peer_id,
            messages: room.messages,
        })
        .await?;
    }
    let mut stdin = BufReader::new(tokio::io::stdin()).lines();
    let mut sessions: HashMap<String, mpsc::Sender<Message>> = HashMap::new();
    let mut discovered = HashMap::new();
    let mut requested_connections = HashSet::new();
    let mut active_peers = HashSet::new();
    let mut pending_peer_connections = HashSet::new();
    let mut seen_messages = HashSet::new();

    loop {
        tokio::select! {
            line = stdin.next_line() => {
                let Some(line) = line.context("failed to read Mock Nearby command")? else { break; };
                if line.trim().is_empty() { continue; }
                let command = serde_json::from_str::<IpcCommand>(&line).context("invalid Nearby IPC command")?;
                match command {
                    IpcCommand::StartDiscovery => sink.send(IpcEvent::DiscoveryStarted).await?,
                    IpcCommand::StopDiscovery => sink.send(IpcEvent::DiscoveryStopped).await?,
                    IpcCommand::SetDiscoverable { enabled } => {
                        save_nearby_settings(&state_dir, &NearbySettings { discoverable: enabled })?;
                        sink.send(IpcEvent::DiscoverabilityChanged { discoverable: enabled }).await?;
                    }
                    IpcCommand::ConnectPeer { peer_id } => {
                        let Some(remote) = discovered.get(&peer_id).cloned() else {
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "附近已找不到这台 Ace".to_owned() }).await?;
                            continue;
                        };
                        if active_peers.contains(&peer_id) {
                            sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                            continue;
                        }
                        pending_peer_connections.insert(peer_id.clone());
                        if let Some(session) = sessions.get(&peer_id).cloned() {
                            session.send(Message::peer_connect(peer.peer_id.clone())).await.ok();
                            pending_peer_connections.remove(&peer_id);
                            active_peers.insert(peer_id);
                            sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                        }
                    }
                    IpcCommand::DisconnectPeer { peer_id } => {
                        pending_peer_connections.remove(&peer_id);
                        if active_peers.remove(&peer_id) {
                            if let Some(session) = sessions.get(&peer_id).cloned() {
                                session.send(Message::peer_disconnect(peer.peer_id.clone())).await.ok();
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
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "请先连接这台 Ace".to_owned() }).await?;
                            continue;
                        }
                        let Some(session) = sessions.get(&peer_id).cloned() else {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "连接已经断开".to_owned() }).await?;
                            continue;
                        };
                        let message = Message::agent_request(peer.peer_id.clone(), text);
                        session.send(message.clone()).await.ok();
                        remember_dm_message(&mut dms, &peer_id, &message);
                        save_dms(&state_dir, &dms)?;
                        sink.send(IpcEvent::Message { peer_id, message }).await?;
                    }
                    IpcCommand::SendAgentReply { peer_id, request_id, text, error } => {
                        let text = text.trim();
                        if request_id.trim().is_empty() || text.is_empty() || text.chars().count() > 8_000 {
                            sink.send(IpcEvent::Error { message: "Agent 回复无效或超过 8000 个字符".to_owned() }).await?;
                            continue;
                        }
                        if !active_peers.contains(&peer_id) {
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "无法回复：对方已经断开".to_owned() }).await?;
                            continue;
                        }
                        let Some(session) = sessions.get(&peer_id).cloned() else {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "无法回复：BLE 会话已经断开".to_owned() }).await?;
                            continue;
                        };
                        let message = Message::agent_reply(peer.peer_id.clone(), request_id, text, error);
                        session.send(message.clone()).await.ok();
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
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "请先连接这台 Ace".to_owned() }).await?;
                            continue;
                        }
                        let Some(session) = sessions.get(&peer_id).cloned() else {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "连接已经断开".to_owned() }).await?;
                            continue;
                        };
                        let message = Message::peer_message(peer.peer_id.clone(), text, mentions)
                            .with_client_message_id(client_message_id);
                        session.send(message.clone()).await.ok();
                        remember_dm_message(&mut dms, &peer_id, &message);
                        save_dms(&state_dir, &dms)?;
                        sink.send(IpcEvent::Message { peer_id, message }).await?;
                    }
                    IpcCommand::SendPeerFile { peer_id, file_id, name, mime_type, size, sha256, file_path, client_message_id } => {
                        if !active_peers.contains(&peer_id) {
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "请先连接这台 Ace".to_owned() }).await?;
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
                        let Some(session) = sessions.get(&peer_id).cloned() else {
                            active_peers.remove(&peer_id);
                            sink.send(IpcEvent::PeerConnectionFailed { peer_id, message: "连接已经断开".to_owned() }).await?;
                            continue;
                        };
                        let data_base64 = match tokio::fs::read(&file_path).await {
                            Ok(data) => BASE64.encode(data),
                            Err(error) => {
                                sink.send(IpcEvent::Error { message: format!("无法读取文件：{error}") }).await?;
                                continue;
                            }
                        };
                        if let Err(error) = validate_file_transfer(&file_id, &name, &mime_type, size, &sha256, &data_base64) {
                            sink.send(IpcEvent::Error { message: error.to_string() }).await?;
                            continue;
                        }
                        let chunks = if data_base64.is_empty() { vec![&[][..]] } else { data_base64.as_bytes().chunks(FILE_CHUNK_BASE64_BYTES).collect::<Vec<_>>() };
                        let chunk_total = u32::try_from(chunks.len().max(1)).context("file has too many chunks")?;
                        for (chunk_index, data) in chunks.into_iter().enumerate() {
                            let file = FileChunk { file_id: file_id.clone(), name: name.clone(), mime_type: mime_type.clone(), size, sha256: sha256.clone(), chunk_index: u32::try_from(chunk_index)?, chunk_total, data_base64: String::from_utf8(data.to_vec())? };
                            let message = Message::peer_file(peer.peer_id.clone(), file)
                                .with_client_message_id(client_message_id.clone());
                            session.send(message.clone()).await.ok();
                            remember_dm_message(&mut dms, &peer_id, &message);
                            sink.send(IpcEvent::Message { peer_id: peer_id.clone(), message }).await?;
                        }
                        save_dms(&state_dir, &dms)?;
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
                            rooms.insert(room_id.clone(), RoomState { room_name: room_name.clone(), peer_ids: participants, agent_mode: agent_mode.clone(), owner_peer_id: Some(peer.peer_id.clone()), messages: Vec::new() });
                        }
                        let added = add_room_members(&sessions, &mut rooms, &peer.peer_id, &room_id, peer_ids).await;
                        if added.is_empty() && !existed {
                            rooms.remove(&room_id);
                            sink.send(IpcEvent::Error { message: "没有可用的同伴连接".to_owned() }).await?;
                            continue;
                        }
                        save_rooms(&state_dir, &rooms)?;
                        let room = rooms.get(&room_id).expect("room was just inserted");
                        sink.send(IpcEvent::RoomCreated { room_id, room_name: room.room_name.clone(), peer_ids: room.peer_ids.iter().cloned().collect(), agent_mode: room.agent_mode.clone(), owner_peer_id: room.owner_peer_id.clone() }).await?;
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
                        sink.send(IpcEvent::RoomCreated { room_id, room_name: room.room_name.clone(), peer_ids: room.peer_ids.iter().cloned().collect(), agent_mode: room.agent_mode.clone(), owner_peer_id: room.owner_peer_id.clone() }).await?;
                    }
                    IpcCommand::SendRoomMessage { room_id, text, client_message_id, mentions, agent_mentions, agent_sender, reply_to } => {
                        if !rooms.contains_key(&room_id) {
                            sink.send(IpcEvent::Error { message: "群聊不存在或已退出".to_owned() }).await?;
                            continue;
                        }
                        let mentions = filter_room_mentions(mentions, rooms.get(&room_id).unwrap());
                        let agent_mentions = filter_room_agent_mentions(agent_mentions, rooms.get(&room_id).unwrap());
                        let message = Message::room_message_with_context(peer.peer_id.clone(), room_id.clone(), text, mentions, reply_to)
                            .with_agent_mentions(agent_mentions)
                            .with_agent_sender(agent_sender)
                            .with_client_message_id(client_message_id);
                        remember_room_message(rooms.get_mut(&room_id).unwrap(), &message);
                        save_rooms(&state_dir, &rooms)?;
                        if let Some(room) = rooms.get(&room_id) { broadcast_room_message(&sessions, room, &message).await; }
                        seen_messages.insert(message.message_id.clone());
                        sink.send(IpcEvent::Message { peer_id: peer.peer_id.clone(), message }).await?;
                    }
                    IpcCommand::SendRoomFile { room_id, file_id, name, mime_type, size, sha256, file_path, client_message_id, mentions, reply_to } => {
                        if !rooms.contains_key(&room_id) {
                            sink.send(IpcEvent::Error { message: "群聊不存在或已退出".to_owned() }).await?;
                            continue;
                        }
                        let recipients = rooms
                            .get(&room_id)
                            .map(|room| {
                                room.peer_ids
                                    .iter()
                                    .filter(|candidate| {
                                        *candidate != &peer.peer_id && sessions.contains_key(*candidate)
                                    })
                                    .cloned()
                                    .collect::<Vec<_>>()
                            })
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
                        let data_base64 = match tokio::fs::read(&file_path).await {
                            Ok(data) => BASE64.encode(data),
                            Err(error) => {
                                sink.send(IpcEvent::Error { message: format!("无法读取文件：{error}") }).await?;
                                continue;
                            }
                        };
                        if let Err(error) = validate_file_transfer(&file_id, &name, &mime_type, size, &sha256, &data_base64) {
                            sink.send(IpcEvent::Error { message: error.to_string() }).await?;
                            continue;
                        }
                        let mentions = filter_room_mentions(mentions, rooms.get(&room_id).unwrap());
                        let chunks = if data_base64.is_empty() { vec![&[][..]] } else { data_base64.as_bytes().chunks(FILE_CHUNK_BASE64_BYTES).collect::<Vec<_>>() };
                        let chunk_total = u32::try_from(chunks.len().max(1)).context("file has too many chunks")?;
                        for (chunk_index, data) in chunks.into_iter().enumerate() {
                            let file = FileChunk { file_id: file_id.clone(), name: name.clone(), mime_type: mime_type.clone(), size, sha256: sha256.clone(), chunk_index: u32::try_from(chunk_index)?, chunk_total, data_base64: String::from_utf8(data.to_vec())? };
                            let message = Message::room_file(peer.peer_id.clone(), room_id.clone(), file, mentions.clone(), reply_to.clone())
                                .with_client_message_id(client_message_id.clone());
                            remember_room_message(rooms.get_mut(&room_id).unwrap(), &message);
                            if let Some(room) = rooms.get(&room_id) { broadcast_room_message(&sessions, room, &message).await; }
                            seen_messages.insert(message.message_id.clone());
                            sink.send(IpcEvent::Message { peer_id: peer.peer_id.clone(), message }).await?;
                        }
                        save_rooms(&state_dir, &rooms)?;
                    }
                    IpcCommand::RespondFileTransfer { .. } => {
                        sink.send(IpcEvent::Error { message: "Mock 传输不支持文件接收确认".to_owned() }).await?;
                    }
                    IpcCommand::LeaveRoom { room_id } => {
                        if let Some(room) = rooms.remove(&room_id) {
                            let leave = Message::room_leave(peer.peer_id.clone(), room_id.clone());
                            send_messages(&sessions, &room.peer_ids.iter().filter(|id| *id != &peer.peer_id).cloned().collect::<Vec<_>>(), leave).await;
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
                        let settings = Message::room_settings(peer.peer_id.clone(), room_id.clone(), agent_mode.as_deref(), room_name.as_deref());
                        seen_messages.insert(settings.message_id.clone());
                        if let Some(room) = rooms.get(&room_id) { broadcast_room_message(&sessions, room, &settings).await; }
                        if let Some(room) = rooms.get(&room_id) {
                            sink.send(IpcEvent::RoomSettingsUpdated { room_id, agent_mode: room.agent_mode.clone(), room_name: room.room_name.clone() }).await?;
                        }
                    }
                    IpcCommand::Shutdown => break,
                }
            }
            Some(event) = client.events.recv() => match event {
                BusEvent::PeerDiscovered { peer: remote } => {
                    discovered.insert(remote.peer_id.clone(), remote.clone());
                    sink.send(IpcEvent::PeerDiscovered { peer: remote.clone() }).await?;
                    if remote.peer_id != peer.peer_id && requested_connections.insert(remote.peer_id.clone()) {
                        client.requests.send(BusRequest::Connect { peer_id: remote.peer_id }).await.ok();
                    }
                }
                BusEvent::PeerConnected { peer: remote } => {
                    if remote.peer_id == peer.peer_id { continue; }
                    if !sessions.contains_key(&remote.peer_id) {
                        let (outbound, mut outbound_rx) = mpsc::channel(64);
                        let requests = client.requests.clone();
                        let remote_id = remote.peer_id.clone();
                        tokio::spawn(async move {
                            while let Some(message) = outbound_rx.recv().await {
                                if requests.send(BusRequest::Send { to: remote_id.clone(), message }).await.is_err() { break; }
                            }
                        });
                        sessions.insert(remote.peer_id.clone(), outbound);
                    }
                    if pending_peer_connections.remove(&remote.peer_id) {
                        if let Some(session) = sessions.get(&remote.peer_id).cloned() {
                            session.send(Message::peer_connect(peer.peer_id.clone())).await.ok();
                        }
                        active_peers.insert(remote.peer_id.clone());
                        sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                    }
                }
                BusEvent::PeerDisconnected { peer_id } => {
                    sessions.remove(&peer_id);
                    pending_peer_connections.remove(&peer_id);
                    requested_connections.remove(&peer_id);
                    if active_peers.remove(&peer_id) {
                        sink.send(IpcEvent::PeerDisconnected { peer_id }).await?;
                    } else if discovered.contains_key(&peer_id) {
                        sink.send(IpcEvent::PeerUnavailable { peer_id }).await?;
                    }
                }
                BusEvent::Message { peer_id, message } => {
                    // room.* 消息允许中继转发（sender 是原作者，与会话对端不同）；
                    // 1:1 消息仍要求 sender 与会话对端一致，防止伪造。
                    if message.version != crate::protocol::PROTOCOL_VERSION {
                        continue;
                    }
                    if message.sender != peer_id && !message.message_type.starts_with("room.") {
                        continue;
                    }
                    match message.message_type.as_str() {
                        "peer.connect" => {
                            if !seen_messages.insert(message.message_id) { continue; }
                            active_peers.insert(peer_id.clone());
                            if let Some(remote) = discovered.get(&peer_id).cloned() {
                                sink.send(IpcEvent::PeerConnected { peer: remote }).await?;
                            }
                        }
                        "peer.disconnect" => {
                            if !seen_messages.insert(message.message_id) { continue; }
                            if active_peers.remove(&peer_id) {
                                sink.send(IpcEvent::PeerDisconnected { peer_id }).await?;
                            }
                        }
                        "agent.request" | "agent.response" | "agent.error" => {
                            if !active_peers.contains(&peer_id) || !seen_messages.insert(message.message_id.clone()) { continue; }
                            remember_dm_message(&mut dms, &peer_id, &message);
                            save_dms(&state_dir, &dms)?;
                            sink.send(IpcEvent::Message { peer_id, message }).await?;
                        }
                        "message.ack" => {
                            handle_received_message(&peer.peer_id, &sessions, &mut rooms, &mut seen_messages, &discovered, &sink, peer_id, message).await?;
                        }
                        "peer.message" => {
                            if !active_peers.contains(&peer_id) || !seen_messages.insert(message.message_id.clone()) { continue; }
                            let text = message.payload.get("text").and_then(|value| value.as_str()).unwrap_or_default().to_owned();
                            let mentions = message.payload.get("mentions").and_then(|value| value.as_array())
                                .map(|items| items.iter().filter_map(|value| value.as_str().map(str::to_owned)).collect::<Vec<_>>())
                                .unwrap_or_default();
                            let display_name = discovered.get(&peer_id).map(|remote| remote.display_name.clone()).unwrap_or_else(|| peer_id.clone());
                            remember_dm_message(&mut dms, &peer_id, &message);
                            save_dms(&state_dir, &dms)?;
                            super::ipc::acknowledge_received_message(
                                &peer.peer_id,
                                &sessions,
                                &peer_id,
                                &message,
                            ).await;
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
                            if !active_peers.contains(&peer_id) || !seen_messages.insert(message.message_id.clone()) { continue; }
                            remember_dm_message(&mut dms, &peer_id, &message);
                            save_dms(&state_dir, &dms)?;
                            super::ipc::acknowledge_received_message(
                                &peer.peer_id,
                                &sessions,
                                &peer_id,
                                &message,
                            ).await;
                            sink.send(IpcEvent::Message { peer_id, message }).await?;
                        }
                        _ => {
                            handle_received_message(&peer.peer_id, &sessions, &mut rooms, &mut seen_messages, &discovered, &sink, peer_id, message).await?;
                            save_rooms(&state_dir, &rooms)?;
                        }
                    }
                }
            },
            else => break,
        }
    }
    client.requests.send(BusRequest::Unregister).await.ok();
    Ok(())
}

async fn send_messages(
    sessions: &HashMap<String, mpsc::Sender<Message>>,
    peer_ids: &[String],
    message: Message,
) {
    for peer_id in peer_ids {
        if let Some(session) = sessions.get(peer_id) {
            session.send(message.clone()).await.ok();
        }
    }
}
