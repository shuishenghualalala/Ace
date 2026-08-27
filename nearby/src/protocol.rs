use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use thiserror::Error;
use uuid::Uuid;

pub const PROTOCOL_VERSION: u8 = 2;
pub const DEFAULT_AGENT_MODE: &str = "mention";
pub const AGENT_MODES: [&str; 3] = ["mention", "auto", "quiet"];
pub const MAX_ROOM_NAME_CHARS: usize = 120;
pub const SERVICE_UUID: Uuid = uuid::uuid!("5957645b-4b06-49cf-bde2-366a593e73a7");
pub const PEER_INFO_UUID: Uuid = uuid::uuid!("5957645b-4b06-49cf-bde2-366a593e73a8");
pub const INCOMING_MESSAGE_UUID: Uuid = uuid::uuid!("5957645b-4b06-49cf-bde2-366a593e73a9");
pub const OUTGOING_MESSAGE_UUID: Uuid = uuid::uuid!("5957645b-4b06-49cf-bde2-366a593e73aa");

pub struct UuidSet;

pub fn is_valid_agent_mode(value: &str) -> bool {
    AGENT_MODES.contains(&value)
}

pub fn normalize_room_name(value: &str) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.chars().count() > MAX_ROOM_NAME_CHARS {
        return None;
    }
    Some(trimmed.to_owned())
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PeerInfo {
    pub protocol_version: u8,
    pub peer_id: String,
    pub peer_token: String,
    pub display_name: String,
    pub agent_name: String,
    pub capabilities: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReplyReference {
    pub message_id: String,
    pub sender: String,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileChunk {
    pub file_id: String,
    pub name: String,
    pub mime_type: String,
    pub size: u64,
    pub sha256: String,
    pub chunk_index: u32,
    pub chunk_total: u32,
    pub data_base64: String,
}

impl PeerInfo {
    pub fn encode(&self) -> Result<Vec<u8>, serde_json::Error> {
        serde_json::to_vec(self)
    }

    pub fn decode(bytes: &[u8]) -> Result<Self, serde_json::Error> {
        serde_json::from_slice(bytes)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Message {
    pub version: u8,
    #[serde(rename = "type")]
    pub message_type: String,
    pub message_id: String,
    pub sender: String,
    pub payload: Value,
}

impl Message {
    pub fn hello(peer: &PeerInfo) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            message_type: "peer.hello".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: peer.peer_id.clone(),
            payload: serde_json::json!({
                "protocol_version": peer.protocol_version,
                "peer_token": peer.peer_token,
                "display_name": peer.display_name,
                "agent_name": peer.agent_name,
                "capabilities": peer.capabilities.clone(),
            }),
        }
    }

    pub fn agent_request(sender: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            message_type: "agent.request".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload: serde_json::json!({ "text": text.into() }),
        }
    }

    pub fn agent_reply(
        sender: impl Into<String>,
        request_id: impl Into<String>,
        text: impl Into<String>,
        error: bool,
    ) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            message_type: if error {
                "agent.error".to_owned()
            } else {
                "agent.response".to_owned()
            },
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload: serde_json::json!({
                "request_id": request_id.into(),
                "text": text.into(),
            }),
        }
    }

    pub fn peer_message(
        sender: impl Into<String>,
        text: impl Into<String>,
        mentions: Vec<String>,
    ) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            message_type: "peer.message".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload: serde_json::json!({
                "text": text.into(),
                "mentions": mentions,
            }),
        }
    }

    pub fn peer_connect(sender: impl Into<String>) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            message_type: "peer.connect".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload: serde_json::json!({}),
        }
    }

    pub fn peer_disconnect(sender: impl Into<String>) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            message_type: "peer.disconnect".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload: serde_json::json!({}),
        }
    }

    pub fn room_invite(
        sender: impl Into<String>,
        room_id: impl Into<String>,
        room_name: impl Into<String>,
        participants: Vec<String>,
        agent_mode: Option<&str>,
        owner_peer_id: Option<&str>,
    ) -> Self {
        let mut payload = serde_json::json!({
            "room_id": room_id.into(),
            "room_name": room_name.into(),
            "participants": participants,
        });
        if let Some(agent_mode) = agent_mode {
            payload["agent_mode"] = serde_json::Value::String(agent_mode.to_owned());
        }
        if let Some(owner_peer_id) = owner_peer_id {
            payload["owner_peer_id"] = serde_json::Value::String(owner_peer_id.to_owned());
        }
        Self {
            version: PROTOCOL_VERSION,
            message_type: "room.invite".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload,
        }
    }

    pub fn room_message(
        sender: impl Into<String>,
        room_id: impl Into<String>,
        text: impl Into<String>,
    ) -> Self {
        Self::room_message_with_context(sender, room_id, text, Vec::new(), None)
    }

    pub fn room_message_with_context(
        sender: impl Into<String>,
        room_id: impl Into<String>,
        text: impl Into<String>,
        mentions: Vec<String>,
        reply_to: Option<ReplyReference>,
    ) -> Self {
        let mut payload = serde_json::json!({
            "room_id": room_id.into(),
            "text": text.into(),
            "mentions": mentions,
        });
        if let Some(reply_to) = reply_to {
            payload["reply_to"] =
                serde_json::to_value(reply_to).expect("ReplyReference is always JSON serializable");
        }
        Self {
            version: PROTOCOL_VERSION,
            message_type: "room.message".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload,
        }
    }

    pub fn room_file(
        sender: impl Into<String>,
        room_id: impl Into<String>,
        file: FileChunk,
        mentions: Vec<String>,
        reply_to: Option<ReplyReference>,
    ) -> Self {
        let mut payload = serde_json::json!({
            "room_id": room_id.into(),
            "file": file,
            "mentions": mentions,
        });
        if let Some(reply_to) = reply_to {
            payload["reply_to"] =
                serde_json::to_value(reply_to).expect("ReplyReference is always JSON serializable");
        }
        Self {
            version: PROTOCOL_VERSION,
            message_type: "room.file".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload,
        }
    }

    pub fn room_join(
        sender: impl Into<String>,
        room_id: impl Into<String>,
        agent_mode: Option<&str>,
        owner_peer_id: Option<&str>,
    ) -> Self {
        let mut payload = serde_json::json!({ "room_id": room_id.into() });
        if let Some(agent_mode) = agent_mode {
            payload["agent_mode"] = serde_json::Value::String(agent_mode.to_owned());
        }
        if let Some(owner_peer_id) = owner_peer_id {
            payload["owner_peer_id"] = serde_json::Value::String(owner_peer_id.to_owned());
        }
        Self {
            version: PROTOCOL_VERSION,
            message_type: "room.join".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload,
        }
    }

    pub fn room_settings(
        sender: impl Into<String>,
        room_id: impl Into<String>,
        agent_mode: Option<&str>,
        room_name: Option<&str>,
    ) -> Self {
        let mut payload = serde_json::json!({ "room_id": room_id.into() });
        if let Some(agent_mode) = agent_mode {
            payload["agent_mode"] = serde_json::Value::String(agent_mode.to_owned());
        }
        if let Some(room_name) = room_name {
            payload["room_name"] = serde_json::Value::String(room_name.to_owned());
        }
        Self {
            version: PROTOCOL_VERSION,
            message_type: "room.settings".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload,
        }
    }

    pub fn room_leave(sender: impl Into<String>, room_id: impl Into<String>) -> Self {
        Self {
            version: PROTOCOL_VERSION,
            message_type: "room.leave".to_owned(),
            message_id: Uuid::new_v4().to_string(),
            sender: sender.into(),
            payload: serde_json::json!({ "room_id": room_id.into() }),
        }
    }

    pub fn encode(&self) -> Result<Vec<u8>, serde_json::Error> {
        serde_json::to_vec(self)
    }

    pub fn decode(bytes: &[u8]) -> Result<Self, serde_json::Error> {
        serde_json::from_slice(bytes)
    }
}

const HEADER_LEN: usize = 12;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub transfer_id: u32,
    pub sequence: u16,
    pub total: u16,
    pub payload: Vec<u8>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum FrameError {
    #[error("frame is shorter than the {HEADER_LEN}-byte header")]
    TooShort,
    #[error("unsupported frame protocol version {0}")]
    UnsupportedVersion(u8),
    #[error("frame payload length does not match its header")]
    InvalidPayloadLength,
    #[error("frame has an invalid sequence {sequence} for {total} fragments")]
    InvalidSequence { sequence: u16, total: u16 },
    #[error("frame declares zero fragments")]
    EmptyTransfer,
    #[error("frame payload capacity must be greater than zero")]
    InvalidPayloadCapacity,
}

pub struct FrameCodec;

impl FrameCodec {
    pub fn fragment(
        message: &[u8],
        max_payload: usize,
        transfer_id: u32,
    ) -> Result<Vec<Vec<u8>>, FrameError> {
        if max_payload == 0 {
            return Err(FrameError::InvalidPayloadCapacity);
        }

        let fragment_count = message.len().max(1).div_ceil(max_payload);
        let total = u16::try_from(fragment_count).map_err(|_| FrameError::InvalidSequence {
            sequence: 0,
            total: u16::MAX,
        })?;
        let mut frames = Vec::with_capacity(fragment_count);

        for sequence in 0..fragment_count {
            let start = sequence * max_payload;
            let end = (start + max_payload).min(message.len());
            let payload = if start < end {
                &message[start..end]
            } else {
                &[]
            };
            frames.push(Self::encode_frame(Frame {
                transfer_id,
                sequence: sequence as u16,
                total,
                payload: payload.to_vec(),
            }));
        }

        Ok(frames)
    }

    pub fn parse(bytes: &[u8]) -> Result<Frame, FrameError> {
        if bytes.len() < HEADER_LEN {
            return Err(FrameError::TooShort);
        }
        if bytes[0] != PROTOCOL_VERSION {
            return Err(FrameError::UnsupportedVersion(bytes[0]));
        }

        let transfer_id =
            u32::from_be_bytes(bytes[2..6].try_into().expect("validated header length"));
        let sequence = u16::from_be_bytes(bytes[6..8].try_into().expect("validated header length"));
        let total = u16::from_be_bytes(bytes[8..10].try_into().expect("validated header length"));
        let payload_len = usize::from(u16::from_be_bytes(
            bytes[10..12].try_into().expect("validated header length"),
        ));

        if total == 0 {
            return Err(FrameError::EmptyTransfer);
        }
        if sequence >= total {
            return Err(FrameError::InvalidSequence { sequence, total });
        }
        if bytes.len() != HEADER_LEN + payload_len {
            return Err(FrameError::InvalidPayloadLength);
        }

        Ok(Frame {
            transfer_id,
            sequence,
            total,
            payload: bytes[HEADER_LEN..].to_vec(),
        })
    }

    pub fn frame_payload_capacity(mtu: u16) -> usize {
        usize::from(mtu.max(23))
            .saturating_sub(3 + HEADER_LEN)
            .max(1)
    }

    fn encode_frame(frame: Frame) -> Vec<u8> {
        let payload_len =
            u16::try_from(frame.payload.len()).expect("fragment payload must fit u16");
        let mut encoded = Vec::with_capacity(HEADER_LEN + frame.payload.len());
        encoded.push(PROTOCOL_VERSION);
        encoded.push(0);
        encoded.extend_from_slice(&frame.transfer_id.to_be_bytes());
        encoded.extend_from_slice(&frame.sequence.to_be_bytes());
        encoded.extend_from_slice(&frame.total.to_be_bytes());
        encoded.extend_from_slice(&payload_len.to_be_bytes());
        encoded.extend_from_slice(&frame.payload);
        encoded
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReassemblyResult {
    Incomplete,
    Complete(Vec<u8>),
    Duplicate,
}

#[derive(Debug)]
struct Transfer {
    total: u16,
    fragments: Vec<Option<Vec<u8>>>,
}

#[derive(Debug)]
pub struct Reassembler {
    transfers: HashMap<u32, Transfer>,
    completed: HashSet<u32>,
    max_transfers: usize,
}

impl Default for Reassembler {
    fn default() -> Self {
        Self::new(32)
    }
}

impl Reassembler {
    pub fn new(max_transfers: usize) -> Self {
        Self {
            transfers: HashMap::new(),
            completed: HashSet::new(),
            max_transfers: max_transfers.max(1),
        }
    }

    pub fn accept(&mut self, frame: Frame) -> ReassemblyResult {
        if self.completed.contains(&frame.transfer_id) {
            return ReassemblyResult::Duplicate;
        }

        if !self.transfers.contains_key(&frame.transfer_id)
            && self.transfers.len() >= self.max_transfers
        {
            if let Some(oldest_id) = self.transfers.keys().next().copied() {
                self.transfers.remove(&oldest_id);
            }
        }

        let transfer = self
            .transfers
            .entry(frame.transfer_id)
            .or_insert_with(|| Transfer {
                total: frame.total,
                fragments: vec![None; usize::from(frame.total)],
            });

        if transfer.total != frame.total {
            return ReassemblyResult::Duplicate;
        }
        let slot = &mut transfer.fragments[usize::from(frame.sequence)];
        if slot.is_some() {
            return ReassemblyResult::Duplicate;
        }
        *slot = Some(frame.payload);

        if transfer.fragments.iter().any(Option::is_none) {
            return ReassemblyResult::Incomplete;
        }

        let completed = transfer
            .fragments
            .iter_mut()
            .filter_map(Option::take)
            .flatten()
            .collect();
        self.transfers.remove(&frame.transfer_id);
        self.completed.insert(frame.transfer_id);
        ReassemblyResult::Complete(completed)
    }

    pub fn clear_expired(&mut self) {
        self.transfers.clear();
    }
}

pub fn should_initiate(local_peer_id: &str, remote_peer_id: &str) -> bool {
    local_peer_id < remote_peer_id
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_peer() -> PeerInfo {
        PeerInfo {
            protocol_version: PROTOCOL_VERSION,
            peer_id: "crew_local".to_owned(),
            peer_token: "token".to_owned(),
            display_name: "Local".to_owned(),
            agent_name: "Agent".to_owned(),
            capabilities: vec!["chat".to_owned(), "tools".to_owned()],
        }
    }

    #[test]
    fn peer_info_and_messages_round_trip_as_json() {
        let peer = test_peer();
        assert_eq!(PeerInfo::decode(&peer.encode().unwrap()).unwrap(), peer);

        let hello = Message::hello(&peer);
        assert_eq!(Message::decode(&hello.encode().unwrap()).unwrap(), hello);
        let request = Message::agent_request("crew_local", "hello");
        assert_eq!(request.message_type, "agent.request");
        assert_eq!(request.payload["text"], "hello");
        let wire_request = serde_json::to_value(&request).unwrap();
        assert_eq!(wire_request["type"], "agent.request");
        assert!(wire_request.get("message_type").is_none());

        let response = Message::agent_reply("crew_local", &request.message_id, "hi", false);
        assert_eq!(response.message_type, "agent.response");
        assert_eq!(response.payload["request_id"], request.message_id);
        assert_eq!(response.payload["text"], "hi");

        let error = Message::agent_reply("crew_local", "request-id", "failed", true);
        assert_eq!(error.message_type, "agent.error");
    }

    #[test]
    fn direct_session_control_messages_use_stable_types() {
        let connect = Message::peer_connect("ace_local");
        assert_eq!(connect.message_type, "peer.connect");
        assert_eq!(connect.sender, "ace_local");
        assert_eq!(connect.payload, serde_json::json!({}));

        let disconnect = Message::peer_disconnect("ace_local");
        assert_eq!(disconnect.message_type, "peer.disconnect");
        assert_eq!(disconnect.sender, "ace_local");
        assert_eq!(disconnect.payload, serde_json::json!({}));
    }

    #[test]
    fn room_message_preserves_mentions_and_reply_context() {
        let message = Message::room_message_with_context(
            "crew_local",
            "room_1",
            "@Agent please check this",
            vec!["crew_agent".to_owned()],
            Some(ReplyReference {
                message_id: "m_parent".to_owned(),
                sender: "crew_agent".to_owned(),
                text: "previous message".to_owned(),
            }),
        );
        let decoded = Message::decode(&message.encode().unwrap()).unwrap();
        assert_eq!(decoded.payload["mentions"][0], "crew_agent");
        assert_eq!(decoded.payload["reply_to"]["message_id"], "m_parent");
        assert_eq!(decoded.payload["reply_to"]["text"], "previous message");
    }

    #[test]
    fn peer_message_carries_text_and_mentions() {
        let message = Message::peer_message("crew_local", "你好", vec!["crew_agent".to_owned()]);
        assert_eq!(message.message_type, "peer.message");
        let decoded = Message::decode(&message.encode().unwrap()).unwrap();
        assert_eq!(decoded, message);
        assert_eq!(decoded.payload["text"], "你好");
        assert_eq!(decoded.payload["mentions"][0], "crew_agent");
        let wire = serde_json::to_value(&message).unwrap();
        assert_eq!(wire["type"], "peer.message");
    }

    #[test]
    fn room_invite_and_join_carry_optional_metadata() {
        let invite = Message::room_invite(
            "crew_local",
            "room_1",
            "项目群",
            vec!["crew_peer".to_owned()],
            Some("auto"),
            Some("crew_local"),
        );
        assert_eq!(invite.payload["agent_mode"], "auto");
        assert_eq!(invite.payload["owner_peer_id"], "crew_local");
        let decoded = Message::decode(&invite.encode().unwrap()).unwrap();
        assert_eq!(decoded.payload["agent_mode"], "auto");
        assert_eq!(decoded.payload["owner_peer_id"], "crew_local");

        let legacy_invite = Message::room_invite("crew_local", "room_1", "项目群", vec![], None, None);
        assert!(legacy_invite.payload.get("agent_mode").is_none());
        assert!(legacy_invite.payload.get("owner_peer_id").is_none());

        let join = Message::room_join("crew_peer", "room_1", Some("quiet"), Some("crew_local"));
        assert_eq!(join.payload["agent_mode"], "quiet");
        assert_eq!(join.payload["owner_peer_id"], "crew_local");
        let legacy_join = Message::room_join("crew_peer", "room_1", None, None);
        assert!(legacy_join.payload.get("agent_mode").is_none());
        assert!(legacy_join.payload.get("owner_peer_id").is_none());
    }

    #[test]
    fn room_settings_round_trips_and_validates_fields() {
        let message = Message::room_settings("crew_local", "room_1", Some("quiet"), Some("新群名"));
        assert_eq!(message.message_type, "room.settings");
        let decoded = Message::decode(&message.encode().unwrap()).unwrap();
        assert_eq!(decoded.payload["room_id"], "room_1");
        assert_eq!(decoded.payload["agent_mode"], "quiet");
        assert_eq!(decoded.payload["room_name"], "新群名");

        let rename_only = Message::room_settings("crew_local", "room_1", None, Some("改名"));
        assert!(rename_only.payload.get("agent_mode").is_none());
        assert_eq!(rename_only.payload["room_name"], "改名");

        assert!(is_valid_agent_mode("mention"));
        assert!(is_valid_agent_mode("auto"));
        assert!(is_valid_agent_mode("quiet"));
        assert!(!is_valid_agent_mode("loud"));
        assert_eq!(DEFAULT_AGENT_MODE, "mention");

        assert_eq!(normalize_room_name("  项目群  "), Some("项目群".to_owned()));
        assert_eq!(normalize_room_name("   "), None);
        assert_eq!(normalize_room_name(&"长".repeat(MAX_ROOM_NAME_CHARS + 1)), None);
    }

    #[test]
    fn room_file_preserves_chunk_metadata() {
        let message = Message::room_file(
            "crew_local",
            "room_1",
            FileChunk {
                file_id: "file_1".to_owned(),
                name: "notes.txt".to_owned(),
                mime_type: "text/plain".to_owned(),
                size: 5,
                sha256: "a".repeat(64),
                chunk_index: 1,
                chunk_total: 3,
                data_base64: "aGVsbG8=".to_owned(),
            },
            vec!["crew_agent".to_owned()],
            None,
        );
        let decoded = Message::decode(&message.encode().unwrap()).unwrap();
        assert_eq!(decoded.message_type, "room.file");
        assert_eq!(decoded.payload["file"]["chunk_index"], 1);
        assert_eq!(decoded.payload["file"]["chunk_total"], 3);
        assert_eq!(decoded.payload["file"]["data_base64"], "aGVsbG8=");
    }

    #[test]
    fn fragments_reassemble_in_any_order_and_ignore_duplicates() {
        let message = b"a deliberately long message for BLE fragmentation";
        let encoded = FrameCodec::fragment(message, 7, 41).unwrap();
        let mut reassembler = Reassembler::default();
        let mut order: Vec<_> = (0..encoded.len()).collect();
        order.reverse();

        for index in order {
            let frame = FrameCodec::parse(&encoded[index]).unwrap();
            assert!(matches!(
                reassembler.accept(frame),
                ReassemblyResult::Incomplete | ReassemblyResult::Complete(_)
            ));
        }
        assert_eq!(
            reassembler.accept(FrameCodec::parse(&encoded[0]).unwrap()),
            ReassemblyResult::Duplicate
        );
    }

    #[test]
    fn incomplete_transfer_can_be_cleared() {
        let encoded = FrameCodec::fragment(b"abcdef", 2, 7).unwrap();
        let mut reassembler = Reassembler::default();
        assert_eq!(
            reassembler.accept(FrameCodec::parse(&encoded[0]).unwrap()),
            ReassemblyResult::Incomplete
        );
        reassembler.clear_expired();
        assert_eq!(
            reassembler.accept(FrameCodec::parse(&encoded[0]).unwrap()),
            ReassemblyResult::Incomplete
        );
    }

    #[test]
    fn invalid_frames_are_rejected() {
        assert_eq!(
            FrameCodec::parse(&[1, 0]).unwrap_err(),
            FrameError::TooShort
        );
        let mut frame = FrameCodec::fragment(b"hello", 10, 1).unwrap().remove(0);
        frame[0] = 99;
        assert_eq!(
            FrameCodec::parse(&frame).unwrap_err(),
            FrameError::UnsupportedVersion(99)
        );
        frame[0] = PROTOCOL_VERSION;
        frame[10] = 0;
        frame[11] = 4;
        assert_eq!(
            FrameCodec::parse(&frame).unwrap_err(),
            FrameError::InvalidPayloadLength
        );
    }

    #[test]
    fn smaller_peer_id_initiates() {
        assert!(should_initiate("crew_a", "crew_b"));
        assert!(!should_initiate("crew_b", "crew_a"));
        assert!(!should_initiate("crew_a", "crew_a"));
    }
}
