mod cli;

use anyhow::{bail, Result};
use crew_nearby::ipc::run as run_ipc;
use crew_nearby::mock::run_bus;
use crew_nearby::runtime::{run, NearbyConfig};
use crew_nearby::transport::TransportMode;
use std::env;

#[tokio::main]
async fn main() -> Result<()> {
    let arguments = Arguments::parse(env::args().skip(1))?;
    if arguments.help_requested {
        print_help();
        return Ok(());
    }

    let config = NearbyConfig {
        display_name: arguments
            .display_name
            .unwrap_or_else(crew_nearby::default_display_name),
        agent_name: arguments
            .agent_name
            .unwrap_or_else(crew_nearby::default_agent_name),
        capabilities: if arguments.capabilities.is_empty() {
            vec!["chat".to_owned()]
        } else {
            arguments.capabilities
        },
        peer_id: arguments.peer_id,
        state_dir: arguments.state_dir,
        discoverable: arguments.discoverable,
        transport: arguments.transport,
        mock_endpoint: arguments.mock_endpoint.clone(),
    };
    if arguments.mock_bus {
        if arguments.transport != TransportMode::Mock {
            bail!("--mock-bus requires --transport mock");
        }
        return run_bus(arguments.mock_endpoint).await;
    }
    if arguments.cli {
        if arguments.ipc {
            bail!("--cli and --ipc cannot be used together");
        }
        cli::run(config)
    } else if arguments.ipc || arguments.transport == TransportMode::Mock {
        run_ipc(config).await
    } else {
        run(config).await
    }
}

#[derive(Default)]
struct Arguments {
    display_name: Option<String>,
    agent_name: Option<String>,
    capabilities: Vec<String>,
    peer_id: Option<String>,
    state_dir: Option<std::path::PathBuf>,
    discoverable: Option<bool>,
    transport: TransportMode,
    mock_endpoint: Option<String>,
    mock_bus: bool,
    help_requested: bool,
    ipc: bool,
    cli: bool,
}

impl Arguments {
    fn parse(values: impl Iterator<Item = String>) -> Result<Self> {
        let mut arguments = Self::default();
        let mut values = values.peekable();
        while let Some(argument) = values.next() {
            match argument.as_str() {
                "--display-name" => {
                    arguments.display_name = Some(next_value(&mut values, &argument)?)
                }
                "--agent-name" => arguments.agent_name = Some(next_value(&mut values, &argument)?),
                "--capability" => arguments
                    .capabilities
                    .push(next_value(&mut values, &argument)?),
                "--peer-id" => arguments.peer_id = Some(next_value(&mut values, &argument)?),
                "--state-dir" => {
                    arguments.state_dir = Some(next_value(&mut values, &argument)?.into())
                }
                "--discoverable" => arguments.discoverable = Some(true),
                "--no-discoverable" => arguments.discoverable = Some(false),
                "--transport" => {
                    let value = next_value(&mut values, &argument)?;
                    arguments.transport = match value.as_str() {
                        "ble" => TransportMode::Ble,
                        "mock" => TransportMode::Mock,
                        _ => bail!("unsupported transport: {value}; expected ble or mock"),
                    };
                }
                "--mock-endpoint" => {
                    arguments.mock_endpoint = Some(next_value(&mut values, &argument)?);
                }
                "--mock-bus" => arguments.mock_bus = true,
                "--help" | "-h" => arguments.help_requested = true,
                "--ipc" => arguments.ipc = true,
                "--cli" => arguments.cli = true,
                unknown => bail!("unknown argument: {unknown}"),
            }
        }
        Ok(arguments)
    }
}

fn next_value(values: &mut impl Iterator<Item = String>, argument: &str) -> Result<String> {
    values
        .next()
        .ok_or_else(|| anyhow::anyhow!("missing value for {argument}"))
}

fn print_help() {
    println!(
        "Ace Nearby BLE\n\nUsage:\n  cargo run --manifest-path nearby/Cargo.toml -- [options]\n\nOptions:\n  --display-name <name>      Local display name\n  --agent-name <name>        Local agent name\n  --capability <name>        Add a capability; may be repeated\n  --peer-id <id>             Override the persisted peer ID\n  --state-dir <path>         Override the nearby state directory\n  --discoverable             Enable BLE advertising\n  --no-discoverable          Disable BLE advertising\n  --transport <ble|mock>     Select the transport backend\n  --mock-endpoint <addr>     Mock Bus TCP endpoint\n  --mock-bus                 Run the Mock Bus coordinator\n  --ipc                      Use JSONL IPC mode for the desktop client\n  --cli                      Use the interactive Nearby CLI\n  -h, --help                 Show this help"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_repeated_capabilities() {
        let arguments = Arguments::parse(
            [
                "--display-name",
                "Mac One",
                "--agent-name",
                "Agent One",
                "--capability",
                "chat",
                "--capability",
                "tools",
                "--peer-id",
                "crew_a",
            ]
            .into_iter()
            .map(str::to_owned),
        )
        .unwrap();
        assert_eq!(arguments.display_name.as_deref(), Some("Mac One"));
        assert_eq!(arguments.capabilities, vec!["chat", "tools"]);
        assert_eq!(arguments.peer_id.as_deref(), Some("crew_a"));
    }

    #[test]
    fn parses_discoverability_overrides() {
        let enabled = Arguments::parse(["--discoverable"].into_iter().map(str::to_owned)).unwrap();
        assert_eq!(enabled.discoverable, Some(true));

        let disabled =
            Arguments::parse(["--no-discoverable"].into_iter().map(str::to_owned)).unwrap();
        assert_eq!(disabled.discoverable, Some(false));
    }

    #[test]
    fn parses_cli_mode() {
        let arguments = Arguments::parse(["--cli"].into_iter().map(str::to_owned)).unwrap();
        assert!(arguments.cli);
        assert!(!arguments.ipc);
    }

    #[test]
    fn parses_mock_transport_options() {
        let arguments = Arguments::parse(
            [
                "--transport",
                "mock",
                "--mock-endpoint",
                "127.0.0.1:39202",
                "--mock-bus",
            ]
            .into_iter()
            .map(str::to_owned),
        )
        .unwrap();
        assert_eq!(arguments.transport, TransportMode::Mock);
        assert_eq!(arguments.mock_endpoint.as_deref(), Some("127.0.0.1:39202"));
        assert!(arguments.mock_bus);
    }

    #[test]
    fn defaults_to_ble() {
        let default = Arguments::parse(std::iter::empty()).unwrap();
        assert_eq!(default.transport, TransportMode::Ble);
    }
}
