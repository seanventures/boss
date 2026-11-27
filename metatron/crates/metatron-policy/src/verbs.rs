//! Safe command argv parsing and built-in MT-1 / MT-3 diagnostic & safety verb rule pack.
//!
//! Mirrors `engine/boss_engine/tools/host_diagnostics_rules.py` verbatim for
//! `sar`, `vmstat`, `iostat`, `mpstat`, and `top -b` flag and numeric-cap validation,
//! plus `tcpdump` (hard prompt), `osascript`/`shortcuts` (hard prompt), and
//! destructive verbs (`rm`, `dd`, `mkfs*`, etc. -> forbidden).

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use metatron_protocol::{PolicyDecision, RiskClass};
use regex::Regex;
use thiserror::Error;

const SHELL_META: &[char] = &['&', ';', '|', '<', '>', '`', '$', '\\'];
const MAX_INTERVAL_SECONDS: u64 = 10;
const MAX_SAMPLE_COUNT: u64 = 10;
const MAX_SAMPLE_DURATION_SECONDS: u64 = 30;
const TOP_MAX_COUNT: u64 = 5;
const TOP_MAX_DELAY_SECONDS: u64 = 5;
const TOP_MIN_WIDTH: u64 = 40;
const TOP_MAX_WIDTH: u64 = 512;

static MPSTAT_CPU_RE: LazyLock<Option<Regex>> =
    LazyLock::new(|| Regex::new(r"^(?:ALL|[0-9]+(?:[-,][0-9]+)*)$").ok());
static SYSTEMCTL_UNIT_RE: LazyLock<Option<Regex>> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_.@:-]+$").ok());
static SS_CLUSTER_RE: LazyLock<Option<Regex>> =
    LazyLock::new(|| Regex::new(r"^-[tulnpaHs46]+$").ok());

#[derive(Debug, Error, PartialEq, Eq)]
pub enum CommandParseError {
    #[error("empty command")]
    Empty,
    #[error("shell metacharacters or control characters are not permitted")]
    ShellMetacharacters,
    #[error("unbalanced shell quoting: {0}")]
    UnbalancedQuotes(String),
    #[error("nested shell wrapper '{0}' fails closed")]
    NestedWrapper(String),
}

/// Verdict returned by the built-in MT-1 / MT-3 verb classifier.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerbVerdict {
    pub rule_id: String,
    pub decision: PolicyDecision,
    pub risk_class: RiskClass,
    pub hard: bool,
    pub justification: String,
}

/// Parse a raw command string into argv tokens, failing closed on shell metacharacters,
/// control characters, unbalanced quoting, or nested shell wrappers (`sh -c`, `bash -c`, `eval`).
pub fn parse_command_argv(raw: &str) -> Result<Vec<String>, CommandParseError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(CommandParseError::Empty);
    }
    if trimmed
        .chars()
        .any(|c| (c as u32) < 32 || (c as u32) == 127 || SHELL_META.contains(&c))
    {
        return Err(CommandParseError::ShellMetacharacters);
    }
    let words = shell_words::split(trimmed)
        .map_err(|e| CommandParseError::UnbalancedQuotes(e.to_string()))?;
    if words.is_empty() {
        return Err(CommandParseError::Empty);
    }
    reject_nested_wrappers(&words)?;
    Ok(words)
}

fn reject_nested_wrappers(words: &[String]) -> Result<(), CommandParseError> {
    let verb = base_verb(&words[0]);
    if matches!(verb, "eval" | "exec") {
        return Err(CommandParseError::NestedWrapper(verb.to_string()));
    }
    if matches!(verb, "sh" | "bash" | "zsh" | "dash" | "ksh" | "fish")
        && words
            .iter()
            .skip(1)
            .any(|a| a == "-c" || a == "-lc" || a == "-ic")
    {
        return Err(CommandParseError::NestedWrapper(format!("{verb} -c")));
    }
    if matches!(verb, "env" | "xargs" | "sudo" | "su" | "nohup") {
        return Err(CommandParseError::NestedWrapper(verb.to_string()));
    }
    Ok(())
}

fn base_verb(token: &str) -> &str {
    token.rsplit('/').next().unwrap_or(token)
}

/// Classify a parsed argv for either local (`is_remote = false`) or remote (`is_remote = true`) execution.
pub fn classify_command_argv(argv: &[String], is_remote: bool) -> VerbVerdict {
    if argv.is_empty() {
        return VerbVerdict {
            rule_id: "builtin-empty".to_string(),
            decision: PolicyDecision::Forbidden,
            risk_class: if is_remote {
                RiskClass::RemoteWrite
            } else {
                RiskClass::WriteLocal
            },
            hard: true,
            justification: "empty command is forbidden".to_string(),
        };
    }

    let verb = base_verb(&argv[0]);

    // 1. Destructive verbs -> strictly FORBIDDEN in every mode.
    if is_destructive_verb(verb) {
        return VerbVerdict {
            rule_id: format!("builtin-forbid-{verb}"),
            decision: PolicyDecision::Forbidden,
            risk_class: if is_remote {
                RiskClass::RemoteWrite
            } else {
                RiskClass::WriteLocal
            },
            hard: true,
            justification: format!("destructive command '{verb}' is forbidden by BOSS policy"),
        };
    }

    // 2. Hard-prompt capture verbs (`tcpdump`, `tshark -i`).
    if verb == "tcpdump" {
        return VerbVerdict {
            rule_id: "builtin-hard-tcpdump".to_string(),
            decision: PolicyDecision::Prompt,
            risk_class: RiskClass::Capture,
            hard: true,
            justification: "packet capture requires explicit human approval".to_string(),
        };
    }

    // 3. Hard-prompt app-control verbs (`osascript`, `shortcuts`).
    if matches!(verb, "osascript" | "shortcuts") {
        return VerbVerdict {
            rule_id: format!("builtin-hard-{verb}"),
            decision: PolicyDecision::Prompt,
            risk_class: RiskClass::AppControl,
            hard: true,
            justification: format!(
                "app-control command '{verb}' runs unsandboxed and requires human approval"
            ),
        };
    }

    let read_class = if is_remote {
        RiskClass::RemoteRead
    } else {
        RiskClass::ReadLocal
    };

    // 4. MT-1 bounded diagnostic verbs: sar, vmstat, iostat, mpstat, top -b.
    if matches!(verb, "sar" | "vmstat" | "iostat" | "mpstat" | "top") {
        return match validate_diagnostic_verb(verb, &argv[1..]) {
            Ok(()) => VerbVerdict {
                rule_id: format!("builtin-diag-{verb}"),
                decision: PolicyDecision::Allow,
                risk_class: read_class,
                hard: false,
                justification: format!("bounded read-only diagnostic command '{verb}'"),
            },
            Err(reason) => VerbVerdict {
                rule_id: format!("builtin-diag-{verb}-unsafe"),
                decision: PolicyDecision::Prompt,
                risk_class: if is_remote {
                    RiskClass::RemoteWrite
                } else {
                    RiskClass::WriteLocal
                },
                hard: is_remote,
                justification: reason,
            },
        };
    }

    // 5. Additional MT-1 read-only inspection verbs.
    if is_safe_readonly_verb(verb, &argv[1..]) {
        return VerbVerdict {
            rule_id: format!("builtin-read-{verb}"),
            decision: PolicyDecision::Allow,
            risk_class: read_class,
            hard: false,
            justification: format!("read-only inspection verb '{verb}'"),
        };
    }

    // 6. Local read-only analysis helpers (`ffprobe`, `head`, `tail`, `wc`, `grep`, `rg`, `stat`, `file`, `echo`, `pwd`).
    if !is_remote
        && matches!(
            verb,
            "ffprobe"
                | "head"
                | "tail"
                | "wc"
                | "grep"
                | "rg"
                | "stat"
                | "file"
                | "echo"
                | "pwd"
                | "cat"
        )
    {
        return VerbVerdict {
            rule_id: format!("builtin-local-read-{verb}"),
            decision: PolicyDecision::Allow,
            risk_class: RiskClass::ReadLocal,
            hard: false,
            justification: format!("read-only local command '{verb}'"),
        };
    }

    // 7. Unknown/mutating commands default to Prompt (fail-closed; remote writes are hard-prompt).
    VerbVerdict {
        rule_id: "builtin-default-prompt".to_string(),
        decision: PolicyDecision::Prompt,
        risk_class: if is_remote {
            RiskClass::RemoteWrite
        } else {
            RiskClass::WriteLocal
        },
        hard: is_remote,
        justification: format!("command '{verb}' is not on the read-only allowlist"),
    }
}

fn is_destructive_verb(verb: &str) -> bool {
    matches!(
        verb,
        "rm" | "dd"
            | "mkfs"
            | "fdisk"
            | "parted"
            | "shred"
            | "wipefs"
            | "shutdown"
            | "reboot"
            | "poweroff"
            | "halt"
            | "init"
    ) || verb.starts_with("mkfs.")
}

fn parse_positive_u64(token: &str) -> Option<u64> {
    if token.is_empty() || !token.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let val = token.parse::<u64>().ok()?;
    (val >= 1).then_some(val)
}

fn validate_numeric_tail(verb: &str, positionals: &[String]) -> Result<(), String> {
    if positionals.is_empty() {
        return Ok(());
    }
    if positionals.len() == 1 {
        return Err(format!(
            "{verb} with a single interval streams indefinitely; provide both interval (1..{MAX_INTERVAL_SECONDS}) and count (1..{MAX_SAMPLE_COUNT})"
        ));
    }
    if positionals.len() > 2 {
        return Err(format!(
            "{verb} accepts at most interval and count arguments"
        ));
    }
    let interval = parse_positive_u64(&positionals[0]).ok_or_else(|| {
        format!("{verb} interval must be a positive integer (1..{MAX_INTERVAL_SECONDS})")
    })?;
    let count = parse_positive_u64(&positionals[1]).ok_or_else(|| {
        format!("{verb} count must be a positive integer (1..{MAX_SAMPLE_COUNT})")
    })?;
    if interval > MAX_INTERVAL_SECONDS {
        return Err(format!(
            "{verb} interval {interval} exceeds maximum {MAX_INTERVAL_SECONDS}s"
        ));
    }
    if count > MAX_SAMPLE_COUNT {
        return Err(format!(
            "{verb} count {count} exceeds maximum {MAX_SAMPLE_COUNT}"
        ));
    }
    if interval * count > MAX_SAMPLE_DURATION_SECONDS {
        return Err(format!(
            "{verb} total sample duration {}s exceeds maximum {MAX_SAMPLE_DURATION_SECONDS}s",
            interval * count
        ));
    }
    Ok(())
}

struct DiagnosticVerbSpec {
    bool_flags: &'static [&'static str],
    value_flags: &'static [&'static str],
    forbidden_flags: &'static [&'static str],
    required_flags: &'static [&'static str],
    allow_cluster: bool,
    has_tail: bool,
}

fn validate_diagnostic_verb(verb: &str, args: &[String]) -> Result<(), String> {
    let spec = match verb {
        "sar" => DiagnosticVerbSpec {
            bool_flags: &["-u", "-r", "-q", "-b", "-d", "-h", "-p"],
            value_flags: &["-n"],
            forbidden_flags: &["-o", "-f"],
            required_flags: &[],
            allow_cluster: true,
            has_tail: true,
        },
        "vmstat" => DiagnosticVerbSpec {
            bool_flags: &["-a", "-d", "-D", "-s", "-t", "-w"],
            value_flags: &["-S"],
            forbidden_flags: &["-p", "-m"],
            required_flags: &[],
            allow_cluster: true,
            has_tail: true,
        },
        "iostat" => DiagnosticVerbSpec {
            bool_flags: &["-c", "-d", "-h", "-k", "-m", "-t", "-x", "-y", "-z"],
            value_flags: &[],
            forbidden_flags: &["-o"],
            required_flags: &[],
            allow_cluster: true,
            has_tail: true,
        },
        "mpstat" => DiagnosticVerbSpec {
            bool_flags: &["-A", "-u"],
            value_flags: &["-P"],
            forbidden_flags: &[],
            required_flags: &[],
            allow_cluster: true,
            has_tail: true,
        },
        "top" => DiagnosticVerbSpec {
            bool_flags: &["-b", "-H", "-c", "-i", "-1"],
            value_flags: &["-n", "-d", "-w", "-o"],
            forbidden_flags: &["-u", "-U"],
            required_flags: &["-b", "-n"],
            allow_cluster: false,
            has_tail: false,
        },
        _ => return Err(format!("unsupported diagnostic verb {verb}")),
    };
    let DiagnosticVerbSpec {
        bool_flags,
        value_flags,
        forbidden_flags,
        required_flags,
        allow_cluster,
        has_tail,
    } = spec;

    let mut seen: HashSet<String> = HashSet::new();
    let mut values: HashMap<String, String> = HashMap::new();
    let mut positionals: Vec<String> = Vec::new();
    let mut sar_u_all_consumed = false;
    let mut i = 0usize;

    while i < args.len() {
        let tok = &args[i];
        if !positionals.is_empty() {
            if tok.starts_with('-') {
                return Err(format!("{verb} flags must precede interval and count"));
            }
            positionals.push(tok.clone());
            i += 1;
            continue;
        }
        if verb == "sar" && tok == "ALL" && seen.contains("-u") && !sar_u_all_consumed {
            sar_u_all_consumed = true;
            i += 1;
            continue;
        }
        if forbidden_flags
            .iter()
            .any(|f| tok == f || (tok.starts_with(f) && tok.len() > f.len()))
        {
            return Err(format!(
                "{verb} option '{tok}' is not permitted in read-only inspection"
            ));
        }

        // Handle attached short value flags on `top`, e.g. `-n1`, `-d2`, `-w160`.
        let attached = if verb == "top" && tok.len() > 2 {
            let prefix = &tok[..2];
            if value_flags.contains(&prefix) {
                Some((prefix, &tok[2..]))
            } else {
                None
            }
        } else {
            None
        };

        if let Some((flag, val)) = attached {
            validate_value_flag(verb, flag, val)?;
            seen.insert(flag.to_string());
            values.insert(flag.to_string(), val.to_string());
            i += 1;
            continue;
        }
        if value_flags.contains(&tok.as_str()) {
            let val = args
                .get(i + 1)
                .ok_or_else(|| format!("{verb} {tok} requires a value"))?;
            validate_value_flag(verb, tok, val)?;
            seen.insert(tok.clone());
            values.insert(tok.clone(), val.clone());
            i += 2;
            continue;
        }
        if bool_flags.contains(&tok.as_str()) {
            seen.insert(tok.clone());
            i += 1;
            continue;
        }
        if allow_cluster && tok.len() > 2 && tok.starts_with('-') && !tok.starts_with("--") {
            let expanded: Vec<String> = tok[1..].chars().map(|c| format!("-{c}")).collect();
            let all_ok = expanded.iter().all(|f| {
                bool_flags.contains(&f.as_str())
                    && !forbidden_flags.contains(&f.as_str())
                    && !value_flags.contains(&f.as_str())
            });
            if all_ok {
                for f in expanded {
                    seen.insert(f);
                }
                i += 1;
                continue;
            }
        }
        if tok.starts_with('-') {
            return Err(format!(
                "{verb} option '{tok}' is not permitted in read-only inspection"
            ));
        }
        if !has_tail {
            return Err(format!("{verb} unexpected argument '{tok}'"));
        }
        positionals.push(tok.clone());
        i += 1;
    }

    for req in required_flags {
        if !seen.contains(*req) {
            return Err(format!("{verb} requires {req} in read-only batch mode"));
        }
    }
    if verb == "top" {
        let n = values
            .get("-n")
            .and_then(|v| parse_positive_u64(v))
            .unwrap_or(1);
        let d = values
            .get("-d")
            .and_then(|v| parse_positive_u64(v))
            .unwrap_or(1);
        if n * d > MAX_SAMPLE_DURATION_SECONDS {
            return Err(format!(
                "top total duration {}s exceeds maximum {MAX_SAMPLE_DURATION_SECONDS}s",
                n * d
            ));
        }
    }
    if has_tail {
        validate_numeric_tail(verb, &positionals)?;
    }
    Ok(())
}

fn validate_value_flag(verb: &str, flag: &str, val: &str) -> Result<(), String> {
    if val.is_empty() || (val.starts_with('-') && !(verb == "top" && flag == "-o")) {
        return Err(format!("{verb} {flag} requires a valid value"));
    }
    match (verb, flag) {
        ("sar", "-n") => {
            if matches!(val, "DEV" | "EDEV" | "SOCK" | "TCP" | "UDP") {
                Ok(())
            } else {
                Err(format!("sar -n keyword '{val}' is not allowed"))
            }
        }
        ("vmstat", "-S") => {
            if matches!(val, "k" | "K" | "m" | "M") {
                Ok(())
            } else {
                Err("vmstat -S unit must be one of: k, K, m, M".to_string())
            }
        }
        ("mpstat", "-P") => {
            if MPSTAT_CPU_RE.as_ref().is_some_and(|re| re.is_match(val)) {
                Ok(())
            } else {
                Err("mpstat -P must be ALL or numeric CPU list".to_string())
            }
        }
        ("top", "-n") => match parse_positive_u64(val) {
            Some(v) if v <= TOP_MAX_COUNT => Ok(()),
            _ => Err(format!("top -n must be between 1 and {TOP_MAX_COUNT}")),
        },
        ("top", "-d") => match parse_positive_u64(val) {
            Some(v) if v <= TOP_MAX_DELAY_SECONDS => Ok(()),
            _ => Err(format!(
                "top -d must be between 1 and {TOP_MAX_DELAY_SECONDS}"
            )),
        },
        ("top", "-w") => match parse_positive_u64(val) {
            Some(v) if (TOP_MIN_WIDTH..=TOP_MAX_WIDTH).contains(&v) => Ok(()),
            _ => Err(format!(
                "top -w must be between {TOP_MIN_WIDTH} and {TOP_MAX_WIDTH}"
            )),
        },
        ("top", "-o") => {
            if matches!(
                val,
                "%CPU" | "%MEM" | "RES" | "VIRT" | "SHR" | "TIME+" | "PID"
            ) {
                Ok(())
            } else {
                Err(format!("top -o sort field '{val}' is not allowed"))
            }
        }
        _ => Err(format!("{verb} option '{flag}' is not supported")),
    }
}

fn is_safe_readonly_verb(verb: &str, args: &[String]) -> bool {
    match verb {
        "df" | "free" | "uptime" | "uname" | "ls" | "netstat" | "whoami" => true,
        "ps" => args.len() == 1 && args[0] == "aux",
        "cat" => args.len() == 1 && args[0] == "/etc/os-release",
        "docker" => {
            matches!(
                args.first().map(String::as_str),
                Some("ps" | "logs" | "inspect" | "images")
            ) || (args.len() >= 2 && args[0] == "stats" && args[1] == "--no-stream")
        }
        "systemctl" => {
            args.first().map(String::as_str) == Some("status")
                && args[1..]
                    .iter()
                    .all(|u| SYSTEMCTL_UNIT_RE.as_ref().is_some_and(|re| re.is_match(u)))
        }
        "ss" => {
            !args.is_empty()
                && args.iter().all(|opt| {
                    SS_CLUSTER_RE.as_ref().is_some_and(|re| re.is_match(opt))
                        || matches!(
                            opt.as_str(),
                            "--tcp"
                                | "--udp"
                                | "--listening"
                                | "--numeric"
                                | "--processes"
                                | "--all"
                                | "--summary"
                                | "--no-header"
                        )
                })
        }
        "journalctl" => validate_journalctl(args),
        _ => false,
    }
}

fn validate_journalctl(args: &[String]) -> bool {
    const FLAGS: &[&str] = &[
        "-b",
        "--boot",
        "-k",
        "--dmesg",
        "-q",
        "--quiet",
        "-r",
        "--reverse",
        "-x",
        "--catalog",
        "--no-pager",
        "--utc",
    ];
    const VALUE_FLAGS: &[&str] = &[
        "-u",
        "--unit",
        "-n",
        "--lines",
        "-p",
        "--priority",
        "-o",
        "--output",
        "--since",
        "--until",
    ];
    let mut i = 0;
    while i < args.len() {
        let opt = &args[i];
        if FLAGS.contains(&opt.as_str()) {
            i += 1;
        } else if VALUE_FLAGS.contains(&opt.as_str()) {
            let Some(val) = args.get(i + 1) else {
                return false;
            };
            if val.is_empty() || val.starts_with('-') {
                return false;
            }
            i += 2;
        } else if let Some((k, v)) = opt.split_once('=') {
            if k.starts_with("--")
                && VALUE_FLAGS.contains(&k)
                && !v.is_empty()
                && !v.starts_with('-')
            {
                i += 1;
            } else {
                return false;
            }
        } else {
            return false;
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_clean_commands_and_rejects_shell_meta_and_wrappers() {
        assert_eq!(
            parse_command_argv("sar -u 1 5").expect("parse"),
            vec!["sar", "-u", "1", "5"]
        );
        assert!(matches!(
            parse_command_argv("sar -u 1 5; rm -rf /"),
            Err(CommandParseError::ShellMetacharacters)
        ));
        assert!(matches!(
            parse_command_argv("bash -c 'sar -u 1 5'"),
            Err(CommandParseError::NestedWrapper(_))
        ));
        assert!(matches!(
            parse_command_argv("sudo sar -u 1 5"),
            Err(CommandParseError::NestedWrapper(_))
        ));
    }

    #[test]
    fn diagnostic_rule_pack_allows_bounded_sar_and_blocks_unsafe_flags_and_destructive_verbs() {
        let ok_sar = classify_command_argv(&["sar", "-u", "1", "5"].map(str::to_string), true);
        assert_eq!(ok_sar.decision, PolicyDecision::Allow);
        assert_eq!(ok_sar.risk_class, RiskClass::RemoteRead);
        assert!(!ok_sar.hard);

        // sar -o writes a file -> must NOT auto-allow
        let bad_sar = classify_command_argv(
            &["sar", "-o", "/tmp/out", "1", "5"].map(str::to_string),
            true,
        );
        assert_eq!(bad_sar.decision, PolicyDecision::Prompt);

        // sar with single interval streams indefinitely -> must NOT auto-allow
        let inf_sar = classify_command_argv(&["sar", "-u", "1"].map(str::to_string), true);
        assert_eq!(inf_sar.decision, PolicyDecision::Prompt);

        // top -b -n 1 allowed; top without -b rejected
        let ok_top = classify_command_argv(&["top", "-b", "-n", "1"].map(str::to_string), true);
        assert_eq!(ok_top.decision, PolicyDecision::Allow);
        let bad_top = classify_command_argv(&["top", "-n", "1"].map(str::to_string), true);
        assert_eq!(bad_top.decision, PolicyDecision::Prompt);

        // tcpdump -> hard prompt Capture
        let tcpdump = classify_command_argv(
            &["tcpdump", "-i", "eth0", "-c", "10"].map(str::to_string),
            true,
        );
        assert_eq!(tcpdump.decision, PolicyDecision::Prompt);
        assert_eq!(tcpdump.risk_class, RiskClass::Capture);
        assert!(tcpdump.hard);

        // rm / dd / mkfs -> Forbidden
        for verb in ["rm", "dd", "mkfs.ext4"] {
            let v = classify_command_argv(&[verb.to_string(), "/tmp/x".to_string()], true);
            assert_eq!(v.decision, PolicyDecision::Forbidden);
            assert!(v.hard);
        }
    }
}
