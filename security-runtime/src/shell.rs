use serde::Serialize;
use sha2::{Digest, Sha256};
use std::path::Path;
#[cfg(windows)]
use std::process::{Command, Stdio};
use tree_sitter::{Node, Parser, Tree};
use tree_sitter_bash::LANGUAGE as BASH;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ShellVerdict {
    AllowReadOnly,
    Ask,
}

#[derive(Debug, Serialize)]
pub struct ShellClassification {
    pub shell_kind: &'static str,
    pub raw_command: String,
    pub parsed_commands: Vec<Vec<String>>,
    pub canonical_digest: String,
    pub verdict: ShellVerdict,
    pub reason: &'static str,
}

pub fn classify(shell_kind: &str, executable: &str, raw_command: &str) -> ShellClassification {
    let normalized_kind = match shell_kind.to_ascii_lowercase().as_str() {
        "bash" | "sh" | "zsh" => "bash",
        "powershell" | "pwsh" => "powershell",
        _ => "unknown",
    };
    let parsed_commands = match normalized_kind {
        "bash" => parse_bash(raw_command),
        "powershell" => parse_powershell(executable, raw_command),
        _ => None,
    };
    let (commands, verdict, reason) = match parsed_commands {
        Some(commands)
            if !commands.is_empty() && commands.iter().all(|command| is_read_only(command)) =>
        {
            (
                commands,
                ShellVerdict::AllowReadOnly,
                "all_commands_proven_read_only",
            )
        }
        Some(commands) => (
            commands,
            ShellVerdict::Ask,
            "command_not_in_read_only_policy",
        ),
        None => (
            Vec::new(),
            ShellVerdict::Ask,
            "shell_parse_unsupported_or_failed",
        ),
    };
    ShellClassification {
        shell_kind: normalized_kind,
        raw_command: raw_command.to_string(),
        canonical_digest: canonical_digest(normalized_kind, raw_command, &commands),
        parsed_commands: commands,
        verdict,
        reason,
    }
}

fn canonical_digest(shell_kind: &str, raw: &str, commands: &[Vec<String>]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(shell_kind.as_bytes());
    hasher.update([0]);
    hasher.update(raw.as_bytes());
    hasher.update([0]);
    if let Ok(encoded) = serde_json::to_vec(commands) {
        hasher.update(encoded);
    }
    format!("{:x}", hasher.finalize())
}

fn parse_bash(script: &str) -> Option<Vec<Vec<String>>> {
    let mut parser = Parser::new();
    parser.set_language(&BASH.into()).ok()?;
    let tree = parser.parse(script, None)?;
    parse_word_only_commands(&tree, script)
}

fn parse_word_only_commands(tree: &Tree, source: &str) -> Option<Vec<Vec<String>>> {
    if tree.root_node().has_error() {
        return None;
    }
    const ALLOWED_NAMED: &[&str] = &[
        "program",
        "list",
        "pipeline",
        "command",
        "command_name",
        "word",
        "string",
        "string_content",
        "raw_string",
        "number",
        "concatenation",
    ];
    const ALLOWED_TOKENS: &[&str] = &["&&", "||", ";", "|", "\"", "'"];
    let root = tree.root_node();
    let mut stack = vec![root];
    let mut command_nodes = Vec::new();
    while let Some(node) = stack.pop() {
        let kind = node.kind();
        if node.is_named() {
            if !ALLOWED_NAMED.contains(&kind) {
                return None;
            }
            if kind == "command" {
                command_nodes.push(node);
            }
        } else if !(ALLOWED_TOKENS.contains(&kind) || kind.trim().is_empty()) {
            return None;
        }
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            stack.push(child);
        }
    }
    command_nodes.sort_by_key(Node::start_byte);
    let mut commands = Vec::new();
    for node in command_nodes {
        commands.push(parse_bash_command(node, source)?);
    }
    (!commands.is_empty()).then_some(commands)
}

fn parse_bash_command(node: Node<'_>, source: &str) -> Option<Vec<String>> {
    let mut words = Vec::new();
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        match child.kind() {
            "command_name" | "word" | "number" | "string" | "raw_string" | "concatenation" => {
                let raw = child.utf8_text(source.as_bytes()).ok()?;
                words.push(unquote_static_word(raw)?);
            }
            _ => {}
        }
    }
    (!words.is_empty()).then_some(words)
}

fn unquote_static_word(raw: &str) -> Option<String> {
    if raw.contains(['$', '`', '\n', '\r', '\0']) {
        return None;
    }
    if raw.len() >= 2
        && ((raw.starts_with('"') && raw.ends_with('"'))
            || (raw.starts_with('\'') && raw.ends_with('\'')))
    {
        return Some(raw[1..raw.len() - 1].to_string());
    }
    Some(raw.to_string())
}

#[cfg(windows)]
fn parse_powershell(executable: &str, script: &str) -> Option<Vec<Vec<String>>> {
    // ponytail: one parser process per classification keeps the TCB small. Cache a
    // long-lived parser only if profiling shows approval latency is material.
    let parser = include_str!("powershell_parser.ps1");
    let encoded = base64_utf16(script);
    let invocation = format!("& {{ {parser} }} -Payload '{encoded}'");
    let output = Command::new(executable)
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            &invocation,
        ])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    serde_json::from_slice::<Vec<Vec<String>>>(&output.stdout).ok()
}

#[cfg(not(windows))]
fn parse_powershell(_executable: &str, _script: &str) -> Option<Vec<Vec<String>>> {
    None
}

#[cfg(windows)]
fn base64_utf16(value: &str) -> String {
    use base64::engine::general_purpose::STANDARD;
    use base64::Engine;
    let mut bytes = Vec::with_capacity(value.len() * 2);
    for unit in value.encode_utf16() {
        bytes.extend_from_slice(&unit.to_le_bytes());
    }
    STANDARD.encode(bytes)
}

fn command_name(command: &[String]) -> Option<String> {
    Path::new(command.first()?)
        .file_name()
        .and_then(|name| name.to_str())
        .map(|name| {
            let lower = name.to_ascii_lowercase();
            for suffix in [".exe", ".cmd", ".bat", ".com"] {
                if let Some(stem) = lower.strip_suffix(suffix) {
                    return stem.to_string();
                }
            }
            lower
        })
}

fn is_read_only(command: &[String]) -> bool {
    let Some(name) = command_name(command) else {
        return false;
    };
    match name.as_str() {
        "cat" | "cd" | "cut" | "echo" | "expr" | "false" | "grep" | "head" | "id" | "ls" | "nl"
        | "paste" | "pwd" | "rev" | "seq" | "stat" | "tail" | "tr" | "true" | "uname" | "uniq"
        | "wc" | "which" | "whoami" | "get-childitem" | "gci" | "dir" | "get-content" | "gc"
        | "type" | "write-output" | "measure-object" | "measure" | "get-location" | "gl"
        | "test-path" | "tp" | "resolve-path" | "rvpa" | "select-object" | "select"
        | "get-item" => true,
        "rg" => !has_option(command, &["--pre", "--hostname-bin", "--search-zip", "-z"]),
        "find" => !has_option(
            command,
            &[
                "-exec", "-execdir", "-ok", "-okdir", "-delete", "-fls", "-fprint", "-fprint0",
                "-fprintf",
            ],
        ),
        "base64" => !has_option(command, &["-o", "--output"]),
        "git" => is_read_only_git(command),
        _ => false,
    }
}

fn has_option(command: &[String], unsafe_options: &[&str]) -> bool {
    command.iter().skip(1).any(|arg| {
        unsafe_options
            .iter()
            .any(|option| arg == option || arg.starts_with(&format!("{option}=")))
    })
}

fn is_read_only_git(command: &[String]) -> bool {
    let index = 1;
    let Some(subcommand) = command.get(index) else {
        return false;
    };
    if !["status", "log", "diff", "show", "branch"].contains(&subcommand.as_str()) {
        // Any token before the read-only subcommand changes Git's execution
        // context (pager/config/work-tree/etc.). Conservatively ASK.
        return false;
    }
    let args = &command[index + 1..];
    if has_option_slice(args, &["--output", "--ext-diff", "--textconv", "--exec"]) {
        return false;
    }
    if subcommand == "branch" {
        return args.is_empty()
            || args.iter().all(|arg| {
                matches!(
                    arg.as_str(),
                    "--list"
                        | "-l"
                        | "--show-current"
                        | "-a"
                        | "--all"
                        | "-r"
                        | "--remotes"
                        | "-v"
                        | "-vv"
                        | "--verbose"
                ) || arg.starts_with("--format=")
            });
    }
    true
}

fn has_option_slice(args: &[String], options: &[&str]) -> bool {
    args.iter().any(|arg| {
        options
            .iter()
            .any(|option| arg == option || arg.starts_with(&format!("{option}=")))
    })
}

#[cfg(test)]
mod tests {
    use super::{classify, ShellVerdict};
    #[cfg(windows)]
    use std::process::Command;

    #[test]
    fn bash_only_allows_static_read_only_commands() {
        assert_eq!(
            classify("bash", "/bin/bash", "git status | head -5").verdict,
            ShellVerdict::AllowReadOnly
        );
        for script in [
            "git status; rm -rf /tmp/x",
            "cat file > out",
            "x=cat; $x file",
            "cat $(echo file)",
            "python -c 'print(1)'",
            "git --output=result status",
            "git -c core.pager=cat status",
        ] {
            assert_eq!(
                classify("bash", "/bin/bash", script).verdict,
                ShellVerdict::Ask,
                "{script}"
            );
        }
    }

    #[cfg(windows)]
    #[test]
    fn powershell_ast_allows_only_static_read_only_commands() {
        let executable = std::env::var("COMSPEC")
            .ok()
            .and_then(|_| {
                ["pwsh.exe", "powershell.exe"]
                    .into_iter()
                    .find(|candidate| {
                        Command::new(candidate)
                            .args(["-NoProfile", "-Command", "exit 0"])
                            .status()
                            .is_ok_and(|status| status.success())
                    })
            })
            .expect("Windows test requires PowerShell");
        assert_eq!(
            classify("powershell", executable, "Get-ChildItem | Measure-Object").verdict,
            ShellVerdict::AllowReadOnly
        );
        for script in [
            "Remove-Item -Recurse C:\\Temp",
            "$cmd='Get-ChildItem'; & $cmd",
            "Get-Content file | Set-Content out",
            "Get-ChildItem > out.txt",
        ] {
            assert_eq!(
                classify("powershell", executable, script).verdict,
                ShellVerdict::Ask,
                "{script}"
            );
        }
    }

    #[test]
    fn unknown_shell_is_never_auto_allowed() {
        assert_eq!(classify("cmd", "cmd.exe", "dir").verdict, ShellVerdict::Ask);
    }
}
