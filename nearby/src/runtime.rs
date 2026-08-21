use crate::identity::{
    default_agent_name, default_display_name, load_nearby_settings, load_or_create_peer_id,
    resolve_state_dir,
};
use crate::protocol::{
    should_initiate, FrameCodec, Message, PeerInfo, Reassembler, INCOMING_MESSAGE_UUID,
    OUTGOING_MESSAGE_UUID, PEER_INFO_UUID, PROTOCOL_VERSION, SERVICE_UUID,
};
use anyhow::{Context, Result};
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
    platform::{Adapter, Manager, Peripheral},
};
use futures::StreamExt;
use std::{
    collections::{HashMap, HashSet},
    env,
    path::PathBuf,
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{self, AsyncBufReadExt, BufReader},
    sync::{mpsc, Mutex},
};

#[derive(Debug, Clone)]
pub struct NearbyConfig {
    pub display_name: String,
    pub agent_name: String,
    pub capabilities: Vec<String>,
    pub peer_id: Option<String>,
    pub state_dir: Option<PathBuf>,
    /// `None` loads the persisted local preference; `Some` is a CLI override.
    pub discoverable: Option<bool>,
}

impl Default for NearbyConfig {
    fn default() -> Self {
        Self {
            display_name: default_display_name(),
            agent_name: default_agent_name(),
            capabilities: vec!["chat".to_owned()],
            peer_id: None,
            state_dir: None,
            discoverable: None,
        }
    }
}

#[derive(Clone)]
pub struct BleAdapter {
    adapter: Adapter,
    server: Arc<Mutex<ServerPeripheral>>,
}

pub struct PeerSession {
    peer: PeerInfo,
    outbound: mpsc::Sender<Message>,
}

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
    if characteristic
        .properties
        .contains(CharPropFlags::WRITE_WITHOUT_RESPONSE)
    {
        Ok(WriteType::WithoutResponse)
    } else if characteristic.properties.contains(CharPropFlags::WRITE) {
        Ok(WriteType::WithResponse)
    } else {
        anyhow::bail!("remote IncomingMessage characteristic is not writable")
    }
}

impl PeerSession {
    pub fn peer_info(&self) -> &PeerInfo {
        &self.peer
    }

    pub async fn send(&self, message: Message) -> Result<()> {
        self.outbound
            .send(message)
            .await
            .context("BLE peer session is no longer available")
    }
}

enum SessionEvent {
    Ready(PeerSession),
    Received(Message),
    Closed(String),
    Failed(String),
}

impl BleAdapter {
    pub async fn new(server: Arc<Mutex<ServerPeripheral>>) -> Result<Self> {
        let manager = Manager::new()
            .await
            .context("failed to initialize BLE manager")?;
        let adapters = manager
            .adapters()
            .await
            .context("failed to enumerate BLE adapters")?;
        eprintln!("[nearby][central] adapters_found={}", adapters.len());
        let adapter = adapters
            .into_iter()
            .next()
            .context("no BLE adapter was found")?;
        let adapter_info = adapter
            .adapter_info()
            .await
            .unwrap_or_else(|error| format!("unavailable: {error}"));
        eprintln!("[nearby][central] adapter_selected={adapter_info}");
        Ok(Self { adapter, server })
    }

    pub async fn run(
        &self,
        peer: PeerInfo,
        mut server_event_rx: mpsc::Receiver<PeripheralEvent>,
    ) -> Result<()> {
        let mut central_events = self
            .adapter
            .events()
            .await
            .context("failed to subscribe to BLE adapter events")?;
        self.adapter
            .start_scan(ScanFilter {
                services: vec![SERVICE_UUID],
            })
            .await
            .context("failed to start BLE scanning")?;
        eprintln!("[nearby][central] scanning_started service_uuid={SERVICE_UUID}");
        println!("Scanning...");

        let (session_event_tx, mut session_event_rx) = mpsc::channel(32);
        let mut session: Option<PeerSession> = None;
        let mut connected_candidates = HashSet::new();
        let mut subscriptions = HashSet::new();
        let mut server_clients: HashMap<String, String> = HashMap::new();
        let mut incoming_reassemblers: HashMap<String, Reassembler> = HashMap::new();
        let mut transfer_id = 1_u32;
        let mut stdin = BufReader::new(io::stdin()).lines();

        loop {
            tokio::select! {
                line = stdin.next_line() => {
                    match line.context("failed to read terminal input")? {
                        Some(line) if !line.trim().is_empty() => {
                            let message = Message::chat(peer.peer_id.clone(), line.trim());
                            if let Some(current_session) = &session {
                                current_session.send(message).await?;
                            } else if !subscriptions.is_empty() {
                                notify_server(&self.server, &message, transfer_id).await?;
                                transfer_id = transfer_id.wrapping_add(1);
                            } else {
                                println!("No peer session yet; message not sent.");
                            }
                        }
                        Some(_) => {}
                        None => break,
                    }
                }
                Some(event) = central_events.next() => {
                    let id = match event {
                        CentralEvent::DeviceDiscovered(id)
                        | CentralEvent::DeviceUpdated(id)
                        | CentralEvent::ServicesAdvertisement { id, .. } => id,
                        _ => continue,
                    };
                    let key = format!("{id:?}");
                    if connected_candidates.insert(key.clone()) {
                        let device = diagnostic_device_id(&key);
                        let peripheral = self.adapter.peripheral(&id).await
                            .with_context(|| format!("failed to get BLE peripheral {key}"))?;
                        eprintln!("[nearby][scan] candidate device={device} action=connect_attempt");
                        println!("Found peer: {device}");
                        let local_peer = peer.clone();
                        let event_tx = session_event_tx.clone();
                        tokio::spawn(async move {
                            if let Err(error) = connect_to_peer(peripheral, local_peer, device.clone(), event_tx.clone()).await {
                                eprintln!("[nearby][session] device={device} result=failed error={error}");
                                let _ = event_tx.send(SessionEvent::Failed(error.to_string())).await;
                            }
                        });
                    }
                }
                Some(event) = server_event_rx.recv() => {
                    handle_peripheral_event(
                        event,
                        &peer,
                        &self.server,
                        &session_event_tx,
                        &mut subscriptions,
                        &mut server_clients,
                        &mut incoming_reassemblers,
                    ).await?;
                }
                Some(event) = session_event_rx.recv() => {
                    match event {
                        SessionEvent::Ready(new_session) => {
                            eprintln!(
                                "[nearby][session] peer_connected peer_id={}",
                                new_session.peer_info().peer_id
                            );
                            println!("Connected to: {}", new_session.peer_info().peer_id);
                            let hello = Message::hello(&peer);
                            new_session.send(hello).await?;
                            session = Some(new_session);
                        }
                        SessionEvent::Received(message) => print_peer_message(&message),
                        SessionEvent::Closed(peer_id) => {
                            eprintln!("[nearby][session] peer_disconnected peer_id={peer_id}");
                            println!("Disconnected from: {peer_id}");
                            session = None;
                        }
                        SessionEvent::Failed(error) => {
                            eprintln!("[nearby][session] failed error={error}");
                            eprintln!("BLE session failed: {error}");
                        }
                    }
                }
            }
        }

        self.adapter.stop_scan().await.ok();
        Ok(())
    }

    pub async fn configure_server(
        server: &Arc<Mutex<ServerPeripheral>>,
        peer: &PeerInfo,
    ) -> Result<()> {
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
        eprintln!("[nearby][peripheral] adding_service uuid={SERVICE_UUID}");
        server
            .add_service(&service)
            .await
            .context("failed to add Nearby GATT service")?;
        eprintln!("[nearby][peripheral] service_added characteristics=3");
        Ok(())
    }

    pub async fn set_discoverable(
        server: &Arc<Mutex<ServerPeripheral>>,
        name: &str,
        enabled: bool,
    ) -> Result<()> {
        let mut server = server.lock().await;
        eprintln!(
            "[nearby][peripheral] advertising_request enabled={} service_uuid={SERVICE_UUID}",
            enabled
        );
        if enabled {
            server
                .start_advertising(name, &[SERVICE_UUID])
                .await
                .context("failed to start BLE advertising")?;
            eprintln!("[nearby][peripheral] advertising_started");
            println!("Advertising...");
        } else {
            server
                .stop_advertising()
                .await
                .context("failed to stop BLE advertising")?;
            eprintln!("[nearby][peripheral] advertising_stopped");
            println!("Advertising stopped.");
        }
        Ok(())
    }

    pub async fn advertise(
        server: &Arc<Mutex<ServerPeripheral>>,
        name: &str,
        peer: &PeerInfo,
    ) -> Result<()> {
        Self::configure_server(server, peer).await?;
        Self::set_discoverable(server, name, true).await
    }
}

pub async fn run(config: NearbyConfig) -> Result<()> {
    let state_dir = resolve_state_dir(config.state_dir.as_deref());
    let discoverable = config
        .discoverable
        .unwrap_or(load_nearby_settings(&state_dir)?.discoverable);
    let peer_id = load_or_create_peer_id(&state_dir, config.peer_id.as_deref())?;
    let peer = PeerInfo {
        protocol_version: PROTOCOL_VERSION,
        peer_id,
        peer_token: uuid::Uuid::new_v4().to_string(),
        display_name: config.display_name,
        agent_name: config.agent_name,
        capabilities: config.capabilities,
    };
    eprintln!(
        "[nearby][startup] mode=cli os={} arch={} peer_id={} discoverable={} service_uuid={}",
        env::consts::OS,
        env::consts::ARCH,
        peer.peer_id,
        discoverable,
        SERVICE_UUID
    );
    let (server_event_tx, server_event_rx) = mpsc::channel(256);
    eprintln!("[nearby][peripheral] initializing GATT server");
    let server = ServerPeripheral::new(server_event_tx)
        .await
        .context("failed to initialize BLE peripheral")?;
    let server = Arc::new(Mutex::new(server));
    BleAdapter::configure_server(&server, &peer).await?;
    BleAdapter::set_discoverable(&server, &peer.display_name, discoverable).await?;
    let adapter = BleAdapter::new(server.clone()).await?;

    println!("Crew BLE started");
    println!("Peer ID: {}", peer.peer_id);

    adapter.run(peer, server_event_rx).await
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
                properties: vec![CharacteristicProperty::Notify],
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
    println!("Connected to BLE peripheral; discovering services...");
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
    let remote_peer =
        PeerInfo::decode(&peer_info_bytes).context("remote PeerInfo is not valid JSON")?;
    eprintln!(
        "[nearby][session] device={device} stage=peer_info_read remote_peer_id={} remote_display_name={}",
        remote_peer.peer_id, remote_peer.display_name
    );
    let should_initiate = should_initiate(&local_peer.peer_id, &remote_peer.peer_id);
    eprintln!(
        "[nearby][session] device={device} stage=connection_policy should_initiate={} local_peer_id={} remote_peer_id={}",
        should_initiate, local_peer.peer_id, remote_peer.peer_id
    );
    if !should_initiate {
        eprintln!("[nearby][session] device={device} stage=duplicate_connection_close");
        println!(
            "Peer {} has connection priority; closing duplicate",
            remote_peer.peer_id
        );
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
    let (outbound, mut outbound_rx) = mpsc::channel(32);
    event_tx
        .send(SessionEvent::Ready(PeerSession {
            peer: remote_peer.clone(),
            outbound,
        }))
        .await
        .context("failed to publish ready BLE session")?;
    eprintln!("[nearby][session] device={device} stage=ready");

    let mtu = peripheral.mtu();
    let max_payload = FrameCodec::frame_payload_capacity(mtu);
    let mut transfer_id = 1_u32;
    let mut reassembler = Reassembler::default();

    loop {
        tokio::select! {
            Some(message) = outbound_rx.recv() => {
                let encoded = message.encode().context("failed to encode BLE message")?;
                let frames = FrameCodec::fragment(&encoded, max_payload, transfer_id)?;
                eprintln!("[nearby][session] device={device} stage=message_write message_type={} frame_count={}", message.message_type, frames.len());
                transfer_id = transfer_id.wrapping_add(1);
                for frame in frames {
                    peripheral.write(&incoming_characteristic, &frame, write_type)
                        .await
                        .context("failed to write BLE message frame")?;
                }
            }
            Some(notification) = notifications.next() => {
                if notification.uuid != OUTGOING_MESSAGE_UUID {
                    continue;
                }
                let frame = match FrameCodec::parse(&notification.value) {
                    Ok(frame) => frame,
                    Err(error) => {
                        eprintln!("Ignored invalid BLE frame: {error}");
                        continue;
                    }
                };
                if let crate::protocol::ReassemblyResult::Complete(bytes) = reassembler.accept(frame) {
                    let message = Message::decode(&bytes).context("received invalid BLE message JSON")?;
                    eprintln!("[nearby][session] device={device} stage=message_received message_type={} sender={}", message.message_type, message.sender);
                    event_tx.send(SessionEvent::Received(message)).await.ok();
                }
            }
            else => break,
        }
    }

    peripheral.disconnect().await.ok();
    eprintln!(
        "[nearby][session] device={device} stage=disconnected remote_peer_id={}",
        remote_peer.peer_id
    );
    event_tx
        .send(SessionEvent::Closed(remote_peer.peer_id))
        .await
        .ok();
    Ok(())
}

async fn handle_peripheral_event(
    event: PeripheralEvent,
    peer: &PeerInfo,
    server: &Arc<Mutex<ServerPeripheral>>,
    event_tx: &mpsc::Sender<SessionEvent>,
    subscriptions: &mut HashSet<String>,
    server_clients: &mut HashMap<String, String>,
    reassemblers: &mut HashMap<String, Reassembler>,
) -> Result<()> {
    match event {
        PeripheralEvent::StateUpdate { is_powered } => {
            eprintln!("[nearby][peripheral] state_update powered={is_powered}");
            if !is_powered {
                eprintln!("BLE peripheral is powered off");
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
            if request.characteristic == OUTGOING_MESSAGE_UUID {
                if subscribed {
                    subscriptions.insert(request.client);
                } else {
                    subscriptions.remove(&request.client);
                    if let Some(peer_id) = server_clients.remove(&request.client) {
                        reassemblers.remove(&request.client);
                        event_tx.send(SessionEvent::Closed(peer_id)).await.ok();
                    }
                }
            }
        }
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
                let bytes = peer
                    .encode()
                    .context("failed to encode PeerInfo for read")?;
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
            let response = if accepted {
                RequestResponse::Success
            } else {
                RequestResponse::RequestNotSupported
            };
            responder.send(WriteRequestResponse { response }).ok();
            if !accepted {
                return Ok(());
            }

            let client = request.client.clone();
            let reassembler = reassemblers.entry(client.clone()).or_default();
            let frame = match FrameCodec::parse(&value) {
                Ok(frame) => frame,
                Err(error) => {
                    eprintln!("Ignored invalid incoming BLE frame: {error}");
                    return Ok(());
                }
            };
            if let crate::protocol::ReassemblyResult::Complete(bytes) = reassembler.accept(frame) {
                let message =
                    Message::decode(&bytes).context("received invalid BLE message JSON")?;
                eprintln!(
                    "[nearby][peripheral] message_received sender={} message_type={}",
                    message.sender, message.message_type
                );
                if message.message_type == "peer.hello" {
                    if let Some(remote) = peer_info_from_hello(&message) {
                        eprintln!(
                            "[nearby][peripheral] passive_session_ready peer_id={}",
                            remote.peer_id
                        );
                        let (outbound, outbound_rx) = mpsc::channel(32);
                        server_clients.insert(client, remote.peer_id.clone());
                        spawn_server_writer(Arc::clone(server), outbound_rx);
                        event_tx
                            .send(SessionEvent::Ready(PeerSession {
                                peer: remote,
                                outbound,
                            }))
                            .await
                            .ok();
                    } else {
                        eprintln!("[nearby][peripheral] peer_hello_rejected reason=invalid_protocol_or_identity");
                    }
                } else {
                    event_tx.send(SessionEvent::Received(message)).await.ok();
                }
            }
        }
    }
    Ok(())
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

async fn notify_server(
    server: &Arc<Mutex<ServerPeripheral>>,
    message: &Message,
    transfer_id: u32,
) -> Result<()> {
    let bytes = message.encode().context("failed to encode BLE message")?;
    for frame in FrameCodec::fragment(&bytes, FrameCodec::frame_payload_capacity(23), transfer_id)?
    {
        server
            .lock()
            .await
            .update_characteristic(OUTGOING_MESSAGE_UUID, frame)
            .await
            .context("failed to notify BLE message frame")?;
    }
    Ok(())
}

fn print_peer_message(message: &Message) {
    if message.message_type == "chat.message" {
        if let Some(text) = message.payload.get("text").and_then(|value| value.as_str()) {
            println!("Peer: {text}");
            return;
        }
    }
    println!(
        "Peer message [{}]: {}",
        message.message_type, message.payload
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_service_contains_the_three_poc_characteristics() {
        let peer = PeerInfo {
            protocol_version: PROTOCOL_VERSION,
            peer_id: "crew_local".to_owned(),
            peer_token: "token_local".to_owned(),
            display_name: "Local".to_owned(),
            agent_name: "Crew Agent".to_owned(),
            capabilities: vec!["chat".to_owned()],
        };
        let service = nearby_service(&peer).unwrap();
        assert_eq!(service.uuid, SERVICE_UUID);
        assert_eq!(service.characteristics.len(), 3);
        let peer_info = service
            .characteristics
            .iter()
            .find(|characteristic| characteristic.uuid == PEER_INFO_UUID)
            .expect("PeerInfo characteristic should exist");
        assert_eq!(peer_info.value, Some(peer.encode().unwrap()));
        let incoming = service
            .characteristics
            .iter()
            .find(|characteristic| characteristic.uuid == INCOMING_MESSAGE_UUID)
            .expect("IncomingMessage characteristic should exist");
        assert!(incoming.properties.contains(&CharacteristicProperty::Write));
        assert!(incoming
            .properties
            .contains(&CharacteristicProperty::WriteWithoutResponse));
        assert!(service
            .characteristics
            .iter()
            .any(|characteristic| characteristic.uuid == OUTGOING_MESSAGE_UUID));
    }

    #[test]
    fn passive_hello_rehydrates_a_bidirectional_cli_session() {
        let peer = PeerInfo {
            protocol_version: PROTOCOL_VERSION,
            peer_id: "crew_remote".to_owned(),
            peer_token: "token_remote".to_owned(),
            display_name: "Remote".to_owned(),
            agent_name: "Crew Agent".to_owned(),
            capabilities: vec!["chat".to_owned()],
        };
        let hello = Message::hello(&peer);
        assert_eq!(peer_info_from_hello(&hello), Some(peer));
    }
}
