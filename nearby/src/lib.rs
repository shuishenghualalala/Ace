//! Ace Nearby cross-platform BLE peer discovery and chat.

pub mod identity;
pub mod ipc;
pub mod mock;
pub mod protocol;
pub mod runtime;
pub mod transport;

pub use identity::{
    default_agent_name, default_display_name, load_nearby_settings, load_or_create_peer_id,
    resolve_state_dir, save_nearby_settings, NearbySettings,
};
pub use protocol::{
    should_initiate, FileChunk, Frame, FrameCodec, FrameError, Message, PeerInfo, PublishedAgent,
    Reassembler, ReplyReference, UuidSet, INCOMING_MESSAGE_UUID, OUTGOING_MESSAGE_UUID,
    PEER_INFO_UUID, PROTOCOL_VERSION, SERVICE_UUID,
};
pub use runtime::{BleAdapter, NearbyConfig, PeerSession};
pub use transport::{adapter as link_adapter, LinkAdapter, TransportMode};
