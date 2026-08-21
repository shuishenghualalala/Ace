use anyhow::{Context, Result};
use directories::BaseDirs;
use serde::{Deserialize, Serialize};
use std::{
    env, fs,
    path::{Path, PathBuf},
};
use uuid::Uuid;

#[derive(Debug, Serialize, Deserialize)]
struct PeerIdentityFile {
    peer_id: String,
}

/// Local-only Nearby preferences.
///
/// This file deliberately contains no public Agent metadata. In particular,
/// filesystem paths and runtime secrets must never be sent through PeerInfo.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NearbySettings {
    #[serde(default = "default_discoverable")]
    pub discoverable: bool,
}

fn default_discoverable() -> bool {
    true
}

impl Default for NearbySettings {
    fn default() -> Self {
        Self {
            discoverable: default_discoverable(),
        }
    }
}

pub fn resolve_state_dir(explicit: Option<&Path>) -> PathBuf {
    if let Some(path) = explicit {
        return path.to_path_buf();
    }

    if let Some(crew_home) = env::var_os("CREW_HOME") {
        return PathBuf::from(crew_home).join("nearby");
    }

    BaseDirs::new()
        .map(|dirs| dirs.home_dir().join(".Crew").join("nearby"))
        .unwrap_or_else(|| PathBuf::from(".Crew").join("nearby"))
}

pub fn load_or_create_peer_id(state_dir: &Path, requested: Option<&str>) -> Result<String> {
    fs::create_dir_all(state_dir)
        .with_context(|| format!("failed to create state directory {}", state_dir.display()))?;

    let identity_path = state_dir.join("peer.json");
    let peer_id = match requested {
        Some(value) => validate_peer_id(value)?,
        None if identity_path.exists() => {
            let content = fs::read_to_string(&identity_path)
                .with_context(|| format!("failed to read {}", identity_path.display()))?;
            let identity: PeerIdentityFile = serde_json::from_str(&content).with_context(|| {
                format!("invalid peer identity file {}", identity_path.display())
            })?;
            validate_peer_id(&identity.peer_id)?
        }
        None => format!("ace_{}", Uuid::new_v4().simple()),
    };

    let identity = PeerIdentityFile {
        peer_id: peer_id.clone(),
    };
    let content =
        serde_json::to_string_pretty(&identity).context("failed to encode peer identity")?;
    fs::write(&identity_path, format!("{content}\n"))
        .with_context(|| format!("failed to write {}", identity_path.display()))?;
    Ok(peer_id)
}

pub fn load_nearby_settings(state_dir: &Path) -> Result<NearbySettings> {
    fs::create_dir_all(state_dir)
        .with_context(|| format!("failed to create state directory {}", state_dir.display()))?;

    let settings_path = state_dir.join("settings.json");
    if !settings_path.exists() {
        let settings = NearbySettings::default();
        save_nearby_settings(state_dir, &settings)?;
        return Ok(settings);
    }

    let content = fs::read_to_string(&settings_path)
        .with_context(|| format!("failed to read {}", settings_path.display()))?;
    serde_json::from_str(&content)
        .with_context(|| format!("invalid Nearby settings file {}", settings_path.display()))
}

pub fn save_nearby_settings(state_dir: &Path, settings: &NearbySettings) -> Result<()> {
    fs::create_dir_all(state_dir)
        .with_context(|| format!("failed to create state directory {}", state_dir.display()))?;
    let settings_path = state_dir.join("settings.json");
    let content =
        serde_json::to_string_pretty(settings).context("failed to encode Nearby settings")?;
    fs::write(&settings_path, format!("{content}\n"))
        .with_context(|| format!("failed to write {}", settings_path.display()))?;
    Ok(())
}

fn validate_peer_id(value: &str) -> Result<String> {
    let trimmed = value.trim();
    anyhow::ensure!(!trimmed.is_empty(), "peer id cannot be empty");
    anyhow::ensure!(trimmed.len() <= 128, "peer id cannot exceed 128 characters");
    anyhow::ensure!(
        trimmed.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '_' | '-' | '.')
        }),
        "peer id may contain only ASCII letters, numbers, '_', '-' and '.'"
    );
    Ok(trimmed.to_owned())
}

pub fn default_display_name() -> String {
    env::var("CREW_DISPLAY_NAME")
        .or_else(|_| env::var("USER"))
        .or_else(|_| env::var("USERNAME"))
        .unwrap_or_else(|_| "Ace User".to_owned())
}

pub fn default_agent_name() -> String {
    env::var("CREW_AGENT_NAME").unwrap_or_else(|_| "Ace Agent".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_dir() -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock should be after epoch")
            .as_nanos();
        env::temp_dir().join(format!("crew-nearby-identity-{suffix}"))
    }

    #[test]
    fn creates_and_loads_stable_peer_id() {
        let directory = temp_dir();
        let first = load_or_create_peer_id(&directory, None).expect("identity should be created");
        let second = load_or_create_peer_id(&directory, None).expect("identity should load");
        assert_eq!(first, second);

        let overridden = load_or_create_peer_id(&directory, Some("crew_override"))
            .expect("override should be accepted");
        assert_eq!(overridden, "crew_override");
        assert_eq!(
            load_or_create_peer_id(&directory, None).expect("override should persist"),
            "crew_override"
        );

        fs::remove_dir_all(directory).expect("test directory should be removable");
    }

    #[test]
    fn rejects_invalid_peer_ids() {
        let directory = temp_dir();
        assert!(load_or_create_peer_id(&directory, Some("bad id")).is_err());
        assert!(load_or_create_peer_id(&directory, Some("")).is_err());
        fs::remove_dir_all(directory).expect("test directory should be removable");
    }

    #[test]
    fn nearby_settings_default_to_discoverable_and_persist_changes() {
        let directory = temp_dir();
        let initial = load_nearby_settings(&directory).expect("settings should be created");
        assert!(initial.discoverable);

        let updated = NearbySettings {
            discoverable: false,
        };
        save_nearby_settings(&directory, &updated).expect("settings should be saved");
        assert_eq!(load_nearby_settings(&directory).unwrap(), updated);

        fs::remove_dir_all(directory).expect("test directory should be removable");
    }

    #[test]
    fn missing_discoverable_field_keeps_legacy_default() {
        let directory = temp_dir();
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("settings.json"), "{}\n").unwrap();
        assert!(load_nearby_settings(&directory).unwrap().discoverable);
        fs::remove_dir_all(directory).expect("test directory should be removable");
    }
}
