//! Local read-only context assembly: hierarchical `AGENTS.md`/`CLAUDE.md` walk,
//! `@file` mention resolution, and read-only `git diff` helper (`/diff`).

use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::LazyLock;

use metatron_protocol::FileContext;
use metatron_secrets::redact_secrets;
use regex::Regex;
use thiserror::Error;

pub const DEFAULT_AGENTS_MD_MAX_BYTES: usize = 32 * 1024;
pub const DEFAULT_AT_FILE_MAX_BYTES: usize = 64 * 1024;

static AT_MENTION_RE: LazyLock<Option<Regex>> =
    LazyLock::new(|| Regex::new(r"(?:^|\s)@([A-Za-z0-9_./~-]+)").ok());

#[derive(Debug, Error)]
pub enum ContextError {
    #[error("IO error reading context file '{path}': {detail}")]
    Io { path: String, detail: String },
    #[error("referenced path '{0}' escapes the allowed workspace root")]
    PathEscape(String),
}

/// Walk from the nearest `.git` repository root (or `start_dir` itself if not in a repo)
/// down to `start_dir`, concatenating every `AGENTS.md` and `CLAUDE.md` within `max_bytes`.
pub fn collect_agents_md(start_dir: &Path, max_bytes: usize) -> Option<String> {
    let canonical_start = fs::canonicalize(start_dir).unwrap_or_else(|_| start_dir.to_path_buf());
    let mut chain: Vec<PathBuf> = Vec::new();
    let mut cur = Some(canonical_start.as_path());
    let mut found_git = false;

    while let Some(dir) = cur {
        chain.push(dir.to_path_buf());
        if dir.join(".git").exists() {
            found_git = true;
            break;
        }
        cur = dir.parent();
    }
    if !found_git {
        chain.truncate(1);
    }
    chain.reverse(); // repo root -> cwd order

    let mut combined = String::new();
    for dir in chain {
        for filename in ["AGENTS.md", "CLAUDE.md"] {
            let candidate = dir.join(filename);
            if !candidate.is_file() {
                continue;
            }
            let Ok(raw) = fs::read_to_string(&candidate) else {
                continue;
            };
            let clean = redact_secrets(&raw);
            let header = format!("# {}\n", candidate.display());
            if combined.len() + header.len() >= max_bytes {
                break;
            }
            if !combined.is_empty() {
                combined.push_str("\n---\n\n");
            }
            combined.push_str(&header);
            let remaining = max_bytes.saturating_sub(combined.len());
            if clean.len() <= remaining {
                combined.push_str(&clean);
            } else {
                let mut cut = remaining;
                while cut > 0 && !clean.is_char_boundary(cut) {
                    cut -= 1;
                }
                combined.push_str(&clean[..cut]);
                combined.push_str("\n...[truncated]");
                break;
            }
        }
    }

    if combined.is_empty() {
        None
    } else {
        Some(combined)
    }
}

/// Parse `@path/to/file` mentions from a prompt and read each referenced file read-only,
/// ensuring it stays inside `base_dir` and redacting any embedded secrets.
pub fn resolve_at_file_mentions(
    prompt: &str,
    base_dir: &Path,
    max_file_bytes: usize,
) -> Result<Vec<FileContext>, ContextError> {
    let Some(ref re) = *AT_MENTION_RE else {
        return Ok(Vec::new());
    };
    let canonical_root = fs::canonicalize(base_dir).unwrap_or_else(|_| base_dir.to_path_buf());
    let mut seen = HashSet::new();
    let mut out = Vec::new();

    for cap in re.captures_iter(prompt) {
        let Some(m) = cap.get(1) else {
            continue;
        };
        let raw_rel = m
            .as_str()
            .trim_end_matches(&['.', ',', ';', ':', ')', ']'][..]);
        if raw_rel.is_empty() || !seen.insert(raw_rel.to_string()) {
            continue;
        }
        let candidate = if Path::new(raw_rel).is_absolute() {
            PathBuf::from(raw_rel)
        } else {
            canonical_root.join(raw_rel)
        };
        if !candidate.exists() || !candidate.is_file() {
            // Skip non-file @mentions (like @alice or emails).
            continue;
        }
        let canonical_file = fs::canonicalize(&candidate).map_err(|e| ContextError::Io {
            path: raw_rel.to_string(),
            detail: e.to_string(),
        })?;
        if !canonical_file.starts_with(&canonical_root) {
            return Err(ContextError::PathEscape(raw_rel.to_string()));
        }
        let bytes = fs::read(&canonical_file).map_err(|e| ContextError::Io {
            path: raw_rel.to_string(),
            detail: e.to_string(),
        })?;
        // Skip binary files containing NUL bytes.
        if bytes.iter().take(1024).any(|&b| b == 0) {
            continue;
        }
        let truncated = bytes.len() > max_file_bytes;
        let slice = if truncated {
            &bytes[..max_file_bytes]
        } else {
            &bytes[..]
        };
        let content = redact_secrets(&String::from_utf8_lossy(slice));
        out.push(FileContext {
            path: raw_rel.to_string(),
            content,
            truncated,
        });
    }

    Ok(out)
}

/// Capture read-only `git diff` output in `cwd` for the `/diff` command.
pub fn collect_git_diff(cwd: &Path) -> Result<String, ContextError> {
    let output = Command::new("git")
        .args(["diff", "--no-ext-diff"])
        .current_dir(cwd)
        .output()
        .map_err(|e| ContextError::Io {
            path: "git".to_string(),
            detail: e.to_string(),
        })?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        return Err(ContextError::Io {
            path: "git diff".to_string(),
            detail: stderr.trim().to_string(),
        });
    }
    if stdout.trim().is_empty() {
        Ok("(no unstaged local diff)".to_string())
    } else {
        Ok(redact_secrets(&stdout))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn walks_agents_md_and_claude_md_from_git_root_to_cwd() {
        let tmp = TempDir::new().expect("tmp");
        fs::create_dir_all(tmp.path().join(".git")).expect("git");
        let sub = tmp.path().join("services/api");
        fs::create_dir_all(&sub).expect("sub");

        fs::write(
            tmp.path().join("AGENTS.md"),
            "Root rule: never leak boss_pat_1234567890abcdef",
        )
        .expect("root agents");
        fs::write(sub.join("CLAUDE.md"), "Subdir note: check metrics first").expect("sub claude");

        let collected = collect_agents_md(&sub, DEFAULT_AGENTS_MD_MAX_BYTES).expect("collected");
        assert!(collected.contains("Root rule:"));
        assert!(collected.contains("Subdir note:"));
        assert!(!collected.contains("boss_pat_1234567890abcdef"));
    }

    #[test]
    fn resolves_at_file_mentions_and_blocks_escape() {
        let tmp = TempDir::new().expect("tmp");
        let work = tmp.path().join("work");
        fs::create_dir_all(&work).expect("work");
        fs::write(
            work.join("notes.txt"),
            "Host web-1 token=boss_pat_9999999999abcdef",
        )
        .expect("notes");
        fs::write(tmp.path().join("outside.txt"), "outside").expect("outside");

        let files = resolve_at_file_mentions(
            "Explain @notes.txt and ping @alice",
            &work,
            DEFAULT_AT_FILE_MAX_BYTES,
        )
        .expect("resolve");
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].path, "notes.txt");
        assert!(files[0].content.contains("Host web-1"));
        assert!(!files[0].content.contains("boss_pat_9999999999abcdef"));

        let escape =
            resolve_at_file_mentions("Read @../outside.txt", &work, DEFAULT_AT_FILE_MAX_BYTES);
        assert!(matches!(escape, Err(ContextError::PathEscape(_))));
    }
}
