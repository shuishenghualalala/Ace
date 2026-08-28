use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    env,
    io::{self, BufRead, BufReader, Write},
    path::Path,
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    thread,
};
use uuid::Uuid;

use crew_nearby::runtime::NearbyConfig;
use crew_nearby::transport::TransportMode;

const MAX_NEARBY_FILE_BYTES: u64 = 4 * 1024 * 1024;

#[derive(Debug, Default)]
struct CliState {
    local_peer_id: Option<String>,
    discoverable: bool,
    peers: BTreeMap<String, PeerSummary>,
    rooms: BTreeMap<String, RoomSummary>,
    files: BTreeMap<String, CliFile>,
}

#[derive(Debug)]
struct PeerSummary {
    display_name: String,
    agent_name: String,
    connection: String,
}

#[derive(Debug)]
struct RoomSummary {
    room_name: String,
    peer_ids: Vec<String>,
    messages: Vec<Value>,
}

#[derive(Debug)]
struct CliFile {
    name: String,
    size: u64,
    sha256: String,
    chunks: Vec<Option<String>>,
    local_path: Option<String>,
}

#[derive(Debug)]
enum CliAction {
    Command(Value),
    Quit,
}

pub fn run(config: NearbyConfig) -> Result<()> {
    let executable = env::current_exe().context("failed to locate crew-nearby executable")?;
    let mut child_command = Command::new(&executable);
    child_command
        .arg("--ipc")
        .arg("--display-name")
        .arg(&config.display_name)
        .arg("--agent-name")
        .arg(&config.agent_name);
    for capability in &config.capabilities {
        child_command.arg("--capability").arg(capability);
    }
    if let Some(peer_id) = &config.peer_id {
        child_command.arg("--peer-id").arg(peer_id);
    }
    if let Some(state_dir) = &config.state_dir {
        child_command.arg("--state-dir").arg(state_dir);
    }
    if config.transport == TransportMode::Mock {
        child_command.arg("--transport").arg("mock");
        if let Some(endpoint) = &config.mock_endpoint {
            child_command.arg("--mock-endpoint").arg(endpoint);
        }
    }
    match config.discoverable {
        Some(true) => {
            child_command.arg("--discoverable");
        }
        Some(false) => {
            child_command.arg("--no-discoverable");
        }
        None => {}
    }

    let mut child = child_command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .spawn()
        .with_context(|| format!("failed to start IPC runtime {}", executable.display()))?;
    let child_stdout = child
        .stdout
        .take()
        .context("Nearby IPC stdout is unavailable")?;
    let mut child_stdin = child
        .stdin
        .take()
        .context("Nearby IPC stdin is unavailable")?;

    let state = Arc::new(Mutex::new(CliState::default()));
    let reader_state = Arc::clone(&state);
    let reader = thread::spawn(move || {
        let reader = BufReader::new(child_stdout);
        for line in reader.lines() {
            match line {
                Ok(line) if !line.trim().is_empty() => {
                    print_event(&line, &reader_state);
                }
                Ok(_) => {}
                Err(error) => {
                    eprintln!("[nearby-cli] failed to read IPC event: {error}");
                    break;
                }
            }
        }
        println!("[nearby-cli] IPC runtime ended");
    });

    println!("Crew Nearby interactive CLI");
    println!("Type help for commands. BLE diagnostics are printed from the child runtime.");

    let stdin = io::stdin();
    let mut input = String::new();
    loop {
        print!("nearby> ");
        io::stdout().flush().ok();
        input.clear();
        if stdin.read_line(&mut input)? == 0 {
            send_command(&mut child_stdin, json!({ "type": "shutdown" }))?;
            break;
        }
        let line = input.trim();
        if line.is_empty() {
            continue;
        }
        let action = match parse_command(line) {
            Ok(action) => action,
            Err(error) => {
                println!("ERROR: {error}");
                continue;
            }
        };
        match action {
            CliAction::Quit => {
                send_command(&mut child_stdin, json!({ "type": "shutdown" }))?;
                break;
            }
            CliAction::Command(command) => {
                if command["type"] == "local_help" {
                    print_help();
                } else if command["type"] == "local_peers" {
                    print_peers(&state);
                } else if command["type"] == "local_rooms" {
                    print_rooms(&state);
                } else if command["type"] == "local_room_history" {
                    print_room_history(&state, command["room_id"].as_str().unwrap_or_default());
                } else if command["type"] == "local_status" {
                    print_status(&state);
                } else if command["type"] == "local_save_file" {
                    save_received_file(
                        &state,
                        command["file_id"].as_str().unwrap_or_default(),
                        command["path"].as_str().unwrap_or_default(),
                    )?;
                } else {
                    send_command(&mut child_stdin, command)?;
                }
            }
        }
    }

    drop(child_stdin);
    let status = child
        .wait()
        .context("failed to wait for Nearby IPC runtime")?;
    reader.join().ok();
    if !status.success() {
        eprintln!("[nearby-cli] IPC runtime exited with status {status}");
    }
    Ok(())
}

fn send_command(writer: &mut impl Write, command: Value) -> Result<()> {
    serde_json::to_writer(&mut *writer, &command).context("failed to encode CLI command")?;
    writer
        .write_all(b"\n")
        .context("failed to write CLI command")?;
    writer.flush().context("failed to flush CLI command")?;
    Ok(())
}

fn parse_command(line: &str) -> Result<CliAction> {
    let words = split_words(line)?;
    let Some(command) = words.first().map(String::as_str) else {
        return Ok(CliAction::Command(json!({ "type": "local_help" })));
    };
    match command {
        "help" | "h" | "?" => Ok(CliAction::Command(json!({ "type": "local_help" }))),
        "peers" => Ok(CliAction::Command(json!({ "type": "local_peers" }))),
        "rooms" => Ok(CliAction::Command(json!({ "type": "local_rooms" }))),
        "status" => Ok(CliAction::Command(json!({ "type": "local_status" }))),
        "save" => {
            let file_id = words.get(1).context("usage: save <file_id> <path>")?;
            let path = words.get(2).context("usage: save <file_id> <path>")?;
            Ok(CliAction::Command(json!({
                "type": "local_save_file",
                "file_id": file_id,
                "path": path,
            })))
        }
        "accept" | "reject" => {
            let transfer_id = words.get(1).context("usage: accept|reject <transfer_id>")?;
            Ok(CliAction::Command(json!({
                "type": "respond_file_transfer",
                "transfer_id": transfer_id,
                "accepted": command == "accept",
            })))
        }
        "quit" | "exit" => Ok(CliAction::Quit),
        "discover" => parse_toggle_command(&words, "start_discovery", "stop_discovery"),
        "advertise" => parse_toggle_command(&words, "set_discoverable", "set_discoverable"),
        "room" => parse_room_command(&words),
        "send" => parse_message_command(&words),
        "file" => parse_file_command(&words),
        unknown => bail!("unknown CLI command {unknown}; type help"),
    }
}

fn parse_toggle_command(
    words: &[String],
    enabled_command: &str,
    disabled_command: &str,
) -> Result<CliAction> {
    let mode = words.get(1).map(String::as_str).unwrap_or_default();
    match (enabled_command, disabled_command, mode) {
        ("set_discoverable", "set_discoverable", "on") => Ok(CliAction::Command(
            json!({ "type": "set_discoverable", "enabled": true }),
        )),
        ("set_discoverable", "set_discoverable", "off") => Ok(CliAction::Command(
            json!({ "type": "set_discoverable", "enabled": false }),
        )),
        ("start_discovery", "stop_discovery", "on") => {
            Ok(CliAction::Command(json!({ "type": "start_discovery" })))
        }
        ("start_discovery", "stop_discovery", "off") => {
            Ok(CliAction::Command(json!({ "type": "stop_discovery" })))
        }
        _ => bail!("usage: {} on|off", words[0]),
    }
}

fn parse_room_command(words: &[String]) -> Result<CliAction> {
    match words.get(1).map(String::as_str) {
        Some("create") => {
            let room_id = words
                .get(2)
                .context("usage: room create <room_id> [--name <name>] <peer_id>...")?;
            let mut room_name = room_id.clone();
            let mut peer_ids = Vec::new();
            let mut index = 3;
            while index < words.len() {
                if words[index] == "--name" {
                    room_name = words
                        .get(index + 1)
                        .context("usage: room create <room_id> [--name <name>] <peer_id>...")?
                        .clone();
                    index += 2;
                } else {
                    peer_ids.push(words[index].clone());
                    index += 1;
                }
            }
            if peer_ids.is_empty() {
                bail!("usage: room create <room_id> [--name <name>] <peer_id>...");
            }
            Ok(CliAction::Command(json!({
                "type": "create_room",
                "room_id": room_id,
                "room_name": room_name,
                "peer_ids": peer_ids,
            })))
        }
        Some("leave") => {
            let room_id = words.get(2).context("usage: room leave <room_id>")?;
            Ok(CliAction::Command(json!({
                "type": "leave_room",
                "room_id": room_id,
            })))
        }
        Some("history") => {
            let room_id = words.get(2).context("usage: room history <room_id>")?;
            Ok(CliAction::Command(json!({
                "type": "local_room_history",
                "room_id": room_id,
            })))
        }
        _ => bail!("usage: room create|leave|history ..."),
    }
}

fn parse_message_command(words: &[String]) -> Result<CliAction> {
    let room_id = words
        .get(1)
        .context("usage: send <room_id> [options] -- <message>")?;
    let (options, message) = split_options_and_message(&words[2..])?;
    if message.is_empty() {
        bail!("usage: send <room_id> [options] -- <message>");
    }
    let (mentions, reply_to) = parse_message_options(options)?;
    let mut command = json!({
        "type": "send_room_message",
        "room_id": room_id,
        "text": message.join(" "),
        "mentions": mentions,
    });
    if let Some(reply_to) = reply_to {
        command["reply_to"] = reply_to;
    }
    Ok(CliAction::Command(command))
}

fn parse_file_command(words: &[String]) -> Result<CliAction> {
    let room_id = words
        .get(1)
        .context("usage: file <room_id> <path> [options]")?;
    let file_path = words
        .get(2)
        .context("usage: file <room_id> <path> [options]")?;
    let (mentions, reply_to) = parse_message_options(&words[3..])?;
    let path = Path::new(file_path);
    let bytes =
        std::fs::read(path).with_context(|| format!("failed to read file {}", path.display()))?;
    let size = u64::try_from(bytes.len()).context("file size does not fit u64")?;
    if size > MAX_NEARBY_FILE_BYTES {
        bail!(
            "file is too large; maximum is {} bytes",
            MAX_NEARBY_FILE_BYTES
        );
    }
    let name = path
        .file_name()
        .and_then(|value| value.to_str())
        .context("file name is not valid UTF-8")?;
    let sha256 = format!("{:x}", Sha256::digest(&bytes));
    let canonical_path = std::fs::canonicalize(path)
        .with_context(|| format!("failed to resolve file {}", path.display()))?;
    let mut command = json!({
        "type": "send_room_file",
        "room_id": room_id,
        "file_id": Uuid::new_v4().to_string(),
        "name": name,
        "mime_type": "application/octet-stream",
        "size": size,
        "sha256": sha256,
        "file_path": canonical_path,
        "mentions": mentions,
    });
    if let Some(reply_to) = reply_to {
        command["reply_to"] = reply_to;
    }
    Ok(CliAction::Command(command))
}

fn split_options_and_message(words: &[String]) -> Result<(&[String], &[String])> {
    let Some(separator) = words.iter().position(|word| word == "--") else {
        bail!("use -- before the message text");
    };
    Ok((&words[..separator], &words[separator + 1..]))
}

fn parse_message_options(options: &[String]) -> Result<(Vec<String>, Option<Value>)> {
    let mut mentions = Vec::new();
    let mut reply_to = None;
    let mut index = 0;
    while index < options.len() {
        match options[index].as_str() {
            "--mention" => {
                mentions.push(
                    options
                        .get(index + 1)
                        .context("usage: --mention <peer_id>")?
                        .clone(),
                );
                index += 2;
            }
            "--reply" => {
                let message_id = options
                    .get(index + 1)
                    .context("usage: --reply <message_id> <sender_peer_id> <quoted_text>")?;
                let sender = options
                    .get(index + 2)
                    .context("usage: --reply <message_id> <sender_peer_id> <quoted_text>")?;
                let text = options
                    .get(index + 3)
                    .context("usage: --reply <message_id> <sender_peer_id> <quoted_text>")?;
                reply_to = Some(json!({
                    "message_id": message_id,
                    "sender": sender,
                    "text": text,
                }));
                index += 4;
            }
            unknown => bail!("unknown message option {unknown}"),
        }
    }
    Ok((mentions, reply_to))
}

fn split_words(line: &str) -> Result<Vec<String>> {
    let mut words = Vec::new();
    let mut current = String::new();
    let mut quote = None;
    let mut escaped = false;
    for character in line.chars() {
        if escaped {
            current.push(character);
            escaped = false;
            continue;
        }
        if character == '\\' {
            escaped = true;
            continue;
        }
        if let Some(active_quote) = quote {
            if character == active_quote {
                quote = None;
            } else {
                current.push(character);
            }
        } else if character == '\'' || character == '"' {
            quote = Some(character);
        } else if character.is_whitespace() {
            if !current.is_empty() {
                words.push(std::mem::take(&mut current));
            }
        } else {
            current.push(character);
        }
    }
    if escaped {
        current.push('\\');
    }
    if quote.is_some() {
        bail!("unterminated quote");
    }
    if !current.is_empty() {
        words.push(current);
    }
    Ok(words)
}

fn print_event(line: &str, state: &Arc<Mutex<CliState>>) {
    let Ok(event) = serde_json::from_str::<Value>(line) else {
        println!("[nearby-cli] runtime output: {line}");
        return;
    };
    let event_type = event["type"].as_str().unwrap_or("unknown");
    match event_type {
        "ready" => {
            if let Ok(mut state) = state.lock() {
                state.local_peer_id = event["peer"]["peer_id"].as_str().map(str::to_owned);
                state.discoverable = event["discoverable"].as_bool().unwrap_or(true);
            }
            println!(
                "Local peer: {} (discoverable={})",
                event["peer"]["peer_id"].as_str().unwrap_or("unknown"),
                event["discoverable"].as_bool().unwrap_or(true)
            );
        }
        "discovery_started" => println!("Scanning..."),
        "discovery_stopped" => println!("Scanning stopped."),
        "discoverability_changed" => {
            let enabled = event["discoverable"].as_bool().unwrap_or(false);
            if let Ok(mut state) = state.lock() {
                state.discoverable = enabled;
            }
            println!("Advertising: {}", if enabled { "on" } else { "off" });
        }
        "peer_discovered" | "peer_connected" => {
            let peer_id = event["peer"]["peer_id"].as_str().unwrap_or("unknown");
            let display_name = event["peer"]["display_name"].as_str().unwrap_or("");
            let agent_name = event["peer"]["agent_name"].as_str().unwrap_or("");
            let connection = if event_type == "peer_connected" {
                "connected"
            } else {
                "discovered"
            };
            if let Ok(mut state) = state.lock() {
                state.peers.insert(
                    peer_id.to_owned(),
                    PeerSummary {
                        display_name: display_name.to_owned(),
                        agent_name: agent_name.to_owned(),
                        connection: connection.to_owned(),
                    },
                );
            }
            println!("{event_type}: {peer_id} {display_name} ({agent_name})");
        }
        "peer_disconnected" => {
            let peer_id = event["peer_id"].as_str().unwrap_or("unknown");
            if let Ok(mut state) = state.lock() {
                if let Some(peer) = state.peers.get_mut(peer_id) {
                    peer.connection = "disconnected".to_owned();
                }
            }
            println!("peer_disconnected: {peer_id}");
        }
        "room_created" | "room_joined" | "room_restored" => {
            let room_id = event["room_id"].as_str().unwrap_or("unknown");
            let room_name = event["room_name"].as_str().unwrap_or(room_id);
            let peer_ids = event["peer_ids"]
                .as_array()
                .map(|values| {
                    values
                        .iter()
                        .filter_map(|value| value.as_str().map(str::to_owned))
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            let messages = event["messages"].as_array().cloned().unwrap_or_default();
            if let Ok(mut state) = state.lock() {
                state.rooms.insert(
                    room_id.to_owned(),
                    RoomSummary {
                        room_name: room_name.to_owned(),
                        peer_ids: peer_ids.clone(),
                        messages,
                    },
                );
            }
            println!("{event_type}: {room_id} {room_name} members={peer_ids:?}");
        }
        "room_left" => {
            let room_id = event["room_id"].as_str().unwrap_or("unknown");
            if let Ok(mut state) = state.lock() {
                state.rooms.remove(room_id);
            }
            println!("room_left: {room_id}");
        }
        "message" => print_message_event(&event, state),
        "file_transfer_requested" => println!(
            "file_request: peer={} transfer={} name={} size={} (use: accept {} | reject {})",
            event["peer_id"].as_str().unwrap_or("unknown"),
            event["transfer"]["transfer_id"]
                .as_str()
                .unwrap_or("unknown"),
            event["transfer"]["name"].as_str().unwrap_or("unknown"),
            event["transfer"]["size"].as_u64().unwrap_or(0),
            event["transfer"]["transfer_id"]
                .as_str()
                .unwrap_or("unknown"),
            event["transfer"]["transfer_id"]
                .as_str()
                .unwrap_or("unknown"),
        ),
        "file_transfer_progress" => println!(
            "file_progress: transfer={} {}/{} direction={}",
            event["transfer_id"].as_str().unwrap_or("unknown"),
            event["sent"].as_u64().unwrap_or(0),
            event["total"].as_u64().unwrap_or(0),
            if event["incoming"].as_bool().unwrap_or(false) {
                "receive"
            } else {
                "send"
            },
        ),
        "file_transfer_failed" => println!(
            "file_failed: transfer={} error={}",
            event["transfer_id"].as_str().unwrap_or("unknown"),
            event["message"].as_str().unwrap_or("unknown"),
        ),
        "error" => println!(
            "ERROR: {}",
            event["message"].as_str().unwrap_or("Nearby error")
        ),
        _ => println!("event: {event_type}"),
    }
}

fn print_message_event(event: &Value, state: &Arc<Mutex<CliState>>) {
    let message = &event["message"];
    let message_type = message["type"].as_str().unwrap_or("unknown");
    let message_id = message["message_id"].as_str().unwrap_or("unknown");
    let sender = message["sender"].as_str().unwrap_or("unknown");
    if message_type == "room.message" {
        if let Some(room_id) = message["payload"]["room_id"].as_str() {
            if let Ok(mut state) = state.lock() {
                if let Some(room) = state.rooms.get_mut(room_id) {
                    room.messages.push(message.clone());
                }
            }
        }
        println!(
            "message id={} room={} sender={} text={}",
            message_id,
            message["payload"]["room_id"].as_str().unwrap_or("unknown"),
            sender,
            message["payload"]["text"].as_str().unwrap_or("")
        );
    } else if message_type == "room.file" || message_type == "peer.file" {
        let file = &message["payload"]["file"];
        let file_id = file["file_id"].as_str().unwrap_or("unknown");
        let local_path = file["local_path"].as_str().map(str::to_owned);
        let chunk_index = file["chunk_index"].as_u64().unwrap_or(0) as usize;
        let chunk_total = file["chunk_total"].as_u64().unwrap_or(0) as usize;
        let name = file["name"].as_str().unwrap_or("unknown");
        let size = file["size"].as_u64().unwrap_or(0);
        let sha256 = file["sha256"].as_str().unwrap_or_default();
        let data_base64 = file["data_base64"].as_str().unwrap_or_default();
        if let Ok(mut state) = state.lock() {
            let transfer = state
                .files
                .entry(file_id.to_owned())
                .or_insert_with(|| CliFile {
                    name: name.to_owned(),
                    size,
                    sha256: sha256.to_owned(),
                    chunks: vec![None; chunk_total.max(1)],
                    local_path,
                });
            if transfer.local_path.is_some() {
                println!(
                    "file_received message_id={} sender={} id={} name={} size={} (use: save {} <path>)",
                    message_id, sender, file_id, transfer.name, transfer.size, file_id
                );
                return;
            }
            if chunk_index < transfer.chunks.len() {
                transfer.chunks[chunk_index] = Some(data_base64.to_owned());
            }
            if transfer.chunks.iter().all(Option::is_some) {
                println!(
                    "file_received message_id={} room={} sender={} id={} name={} size={} (use: save {} <path>)",
                    message_id,
                    message["payload"]["room_id"].as_str().unwrap_or("unknown"),
                    sender,
                    file_id,
                    transfer.name,
                    transfer.size,
                    file_id
                );
            }
        }
    } else {
        println!("message type={message_type} sender={sender}");
    }
}

fn print_help() {
    println!(
        "Commands:\n\
  peers                                      List discovered peers\n\
  rooms                                      List active rooms\n\
  room history <id>                          Show persisted room messages\n\
  status                                     Show local peer and current state\n\
  discover on|off                            Start or stop scanning\n\
  advertise on|off                           Start or stop BLE advertising\n\
  room create <id> [--name <name>] <peer>... Create and invite a room\n\
  room leave <id>                            Leave a room\n\
  send <room> [options] -- <message>         Send a room message\n\
    --mention <peer>                         Add a peer mention (repeatable)\n\
    --reply <id> <sender> <quoted>            Add a reply reference\n\
  file <room> <path> [options]               Send a file up to 4 MiB\n\
  accept <transfer_id>                       Accept an incoming file\n\
  reject <transfer_id>                       Reject an incoming file\n\
  save <file_id> <path>                      Save a received file\n\
  quit                                       Stop the BLE runtime and exit\n\
\nExamples:\n\
  room create test --name \"Test room\" crew_win\n\
  send test --mention crew_win -- \"hello from Mac\"\n\
  file test ./hello.txt\n\
  send test --reply msg-id crew_win \"previous text\" -- \"reply text\""
    );
}

fn print_peers(state: &Arc<Mutex<CliState>>) {
    let Ok(state) = state.lock() else {
        println!("peer state unavailable");
        return;
    };
    if state.peers.is_empty() {
        println!("No peers discovered.");
        return;
    }
    for (peer_id, peer) in &state.peers {
        println!(
            "{peer_id}: {} / {} [{}]",
            peer.display_name, peer.agent_name, peer.connection
        );
    }
}

fn print_rooms(state: &Arc<Mutex<CliState>>) {
    let Ok(state) = state.lock() else {
        println!("room state unavailable");
        return;
    };
    if state.rooms.is_empty() {
        println!("No active rooms.");
        return;
    }
    for (room_id, room) in &state.rooms {
        println!("{room_id}: {} members={:?}", room.room_name, room.peer_ids);
    }
}

fn print_room_history(state: &Arc<Mutex<CliState>>, room_id: &str) {
    let Ok(state) = state.lock() else {
        println!("room state unavailable");
        return;
    };
    let Some(room) = state.rooms.get(room_id) else {
        println!("No room found: {room_id}");
        return;
    };
    if room.messages.is_empty() {
        println!("No messages: {room_id}");
        return;
    }
    for message in &room.messages {
        println!(
            "{} sender={} text={}",
            message["message_id"].as_str().unwrap_or("unknown"),
            message["sender"].as_str().unwrap_or("unknown"),
            message["payload"]["text"]
                .as_str()
                .unwrap_or("[file or system message]")
        );
    }
}

fn print_status(state: &Arc<Mutex<CliState>>) {
    let Ok(state) = state.lock() else {
        println!("state unavailable");
        return;
    };
    println!(
        "Local peer: {} discoverable={} peers={} rooms={}",
        state.local_peer_id.as_deref().unwrap_or("unknown"),
        state.discoverable,
        state.peers.len(),
        state.rooms.len()
    );
}

fn save_received_file(state: &Arc<Mutex<CliState>>, file_id: &str, path: &str) -> Result<()> {
    let state = state
        .lock()
        .map_err(|_| anyhow::anyhow!("file state unavailable"))?;
    let file = state
        .files
        .get(file_id)
        .context("received file is not complete or does not exist")?;
    if let Some(source) = file.local_path.as_deref() {
        let bytes = std::fs::read(source)
            .with_context(|| format!("failed to read received file {source}"))?;
        if u64::try_from(bytes.len()).ok() != Some(file.size)
            || format!("{:x}", Sha256::digest(&bytes)) != file.sha256
        {
            bail!("received file verification failed");
        }
        std::fs::write(path, bytes).with_context(|| format!("failed to save file to {path}"))?;
        println!("file_saved: {path}");
        return Ok(());
    }
    if file.chunks.iter().any(Option::is_none) {
        bail!("received file is still incomplete");
    }
    let data_base64 = file
        .chunks
        .iter()
        .filter_map(|chunk| chunk.as_deref())
        .collect::<String>();
    let bytes = BASE64
        .decode(data_base64)
        .context("received file has invalid Base64 data")?;
    if u64::try_from(bytes.len()).ok() != Some(file.size) {
        bail!("received file size does not match");
    }
    let actual_sha256 = format!("{:x}", Sha256::digest(&bytes));
    if actual_sha256 != file.sha256 {
        bail!("received file SHA-256 does not match");
    }
    std::fs::write(path, bytes).with_context(|| format!("failed to save file to {path}"))?;
    println!("file_saved: {path}");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_room_and_message_commands() {
        let room = parse_command("room create test --name \"Test room\" crew_win").unwrap();
        let CliAction::Command(room) = room else {
            panic!("expected room command");
        };
        assert_eq!(room["type"], "create_room");
        assert_eq!(room["room_name"], "Test room");

        let message = parse_command(
            "send test --mention crew_win --reply msg crew_win quoted -- \"hello world\"",
        )
        .unwrap();
        let CliAction::Command(message) = message else {
            panic!("expected message command");
        };
        assert_eq!(message["type"], "send_room_message");
        assert_eq!(message["text"], "hello world");
        assert_eq!(message["mentions"][0], "crew_win");
        assert_eq!(message["reply_to"]["message_id"], "msg");
    }

    #[test]
    fn parses_toggles_and_quit() {
        assert_eq!(
            parse_command("discover on").unwrap().command_type(),
            Some("start_discovery")
        );
        assert_eq!(
            parse_command("advertise off").unwrap().command_type(),
            Some("set_discoverable")
        );
        assert!(matches!(parse_command("quit").unwrap(), CliAction::Quit));
    }

    #[test]
    fn parses_room_history_command() {
        let history = parse_command("room history saved_room").unwrap();
        let CliAction::Command(history) = history else {
            panic!("expected local room history command");
        };
        assert_eq!(history["type"], "local_room_history");
        assert_eq!(history["room_id"], "saved_room");
    }

    #[test]
    fn parses_file_transfer_decision() {
        let accept = parse_command("accept transfer-1").unwrap();
        let CliAction::Command(accept) = accept else {
            panic!("expected file transfer command");
        };
        assert_eq!(accept["type"], "respond_file_transfer");
        assert_eq!(accept["transfer_id"], "transfer-1");
        assert_eq!(accept["accepted"], true);
    }

    #[test]
    fn shell_split_preserves_quoted_text() {
        assert_eq!(
            split_words(r#"send test -- "hello nearby world""#).unwrap(),
            vec!["send", "test", "--", "hello nearby world"]
        );
    }

    trait CommandType {
        fn command_type(&self) -> Option<&str>;
    }

    impl CommandType for CliAction {
        fn command_type(&self) -> Option<&str> {
            match self {
                CliAction::Command(value) => value["type"].as_str(),
                CliAction::Quit => None,
            }
        }
    }
}
