//! Keychain integration, secret redaction, history filtering, and ephemeral SSH key files.
//!
//! All OS keychain access in Metatron lives exclusively in this crate, wrapping the
//! vendored `codex-keyring-store` trait so tests can inject an in-memory fake.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::{LazyLock, Mutex, PoisonError};

pub mod cookies;

pub use cookies::{
    CHROME_LINUX_PBKDF2_ITERATIONS, CHROME_MACOS_PBKDF2_ITERATIONS, ChromeCookieRow,
    ChromeDomainReadResult, ChromeKeychainProvider, CookieError, CookieRecord,
    CookieSessionManager, CookieSourceKind, CookieValue, OsChromeKeychain,
    cookie_domain_matches_host, decrypt_chrome_cookie_v10, encrypt_chrome_cookie_v10,
    load_chrome_sqlite_for_domain, load_cookie_file_for_host, parse_json_cookies,
    parse_netscape_cookies, read_chrome_rows_for_domain, validate_exact_host,
};

use chrono::{DateTime, Utc};
use codex_keyring_store::{DefaultKeyringStore, KeyringStore};
use metatron_protocol::TokenPrincipal;
use regex::Regex;
use serde::{Deserialize, Serialize};
use tempfile::TempDir;
use thiserror::Error;
use zeroize::Zeroize;

/// OS keyring service name for Metatron credentials.
pub const KEYRING_SERVICE: &str = "boss.metatron";

#[derive(Debug, Error)]
pub enum SecretError {
    #[error("keyring error: {0}")]
    Keyring(String),
    #[error("serialization error: {0}")]
    Serde(#[from] serde_json::Error),
    #[error("filesystem error: {0}")]
    Io(#[from] std::io::Error),
}

/// Persisted per-profile authentication material.
///
/// `Debug` always masks tokens and API keys so they cannot leak into logs.
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StoredAuth {
    pub url: String,
    #[serde(default)]
    pub access_token: Option<String>,
    #[serde(default)]
    pub refresh_token: Option<String>,
    #[serde(default)]
    pub api_key: Option<String>,
    #[serde(default)]
    pub expires_at: Option<DateTime<Utc>>,
    #[serde(default)]
    pub principal: Option<TokenPrincipal>,
}

impl fmt::Debug for StoredAuth {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("StoredAuth")
            .field("url", &self.url)
            .field(
                "access_token",
                &self.access_token.as_ref().map(|_| "[REDACTED]"),
            )
            .field(
                "refresh_token",
                &self.refresh_token.as_ref().map(|_| "[REDACTED]"),
            )
            .field("api_key", &self.api_key.as_ref().map(|_| "[REDACTED]"))
            .field("expires_at", &self.expires_at)
            .field("principal", &self.principal)
            .finish()
    }
}

/// Abstract credential store for Metatron profiles.
pub trait SecretStore: Send + Sync {
    fn load_auth(&self, profile: &str) -> Result<Option<StoredAuth>, SecretError>;
    fn save_auth(&self, profile: &str, auth: &StoredAuth) -> Result<(), SecretError>;
    fn delete_auth(&self, profile: &str) -> Result<bool, SecretError>;
}

/// Production store backed by the OS keychain via vendored `codex-keyring-store`.
pub struct KeyringSecretStore<S: KeyringStore = DefaultKeyringStore> {
    store: S,
    service: String,
}

impl KeyringSecretStore<DefaultKeyringStore> {
    pub fn os_default() -> Self {
        Self {
            store: DefaultKeyringStore,
            service: KEYRING_SERVICE.to_string(),
        }
    }
}

impl<S: KeyringStore> KeyringSecretStore<S> {
    pub fn with_store(store: S) -> Self {
        Self {
            store,
            service: KEYRING_SERVICE.to_string(),
        }
    }

    fn account_key(profile: &str) -> String {
        format!("profile:{profile}")
    }
}

impl<S: KeyringStore> SecretStore for KeyringSecretStore<S> {
    fn load_auth(&self, profile: &str) -> Result<Option<StoredAuth>, SecretError> {
        let raw = self
            .store
            .load(&self.service, &Self::account_key(profile))
            .map_err(|e| SecretError::Keyring(e.message()))?;
        match raw {
            Some(json) => Ok(Some(serde_json::from_str(&json)?)),
            None => Ok(None),
        }
    }

    fn save_auth(&self, profile: &str, auth: &StoredAuth) -> Result<(), SecretError> {
        let json = serde_json::to_string(auth)?;
        self.store
            .save(&self.service, &Self::account_key(profile), &json)
            .map_err(|e| SecretError::Keyring(e.message()))
    }

    fn delete_auth(&self, profile: &str) -> Result<bool, SecretError> {
        self.store
            .delete(&self.service, &Self::account_key(profile))
            .map_err(|e| SecretError::Keyring(e.message()))
    }
}

/// In-memory secret store for tests.
#[derive(Default)]
pub struct MemorySecretStore {
    entries: Mutex<HashMap<String, String>>,
}

impl SecretStore for MemorySecretStore {
    fn load_auth(&self, profile: &str) -> Result<Option<StoredAuth>, SecretError> {
        let guard = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
        match guard.get(profile) {
            Some(json) => Ok(Some(serde_json::from_str(json)?)),
            None => Ok(None),
        }
    }

    fn save_auth(&self, profile: &str, auth: &StoredAuth) -> Result<(), SecretError> {
        let json = serde_json::to_string(auth)?;
        let mut guard = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
        guard.insert(profile.to_string(), json);
        Ok(())
    }

    fn delete_auth(&self, profile: &str) -> Result<bool, SecretError> {
        let mut guard = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
        Ok(guard.remove(profile).is_some())
    }
}

/// Explicit opt-in 0600 file fallback store (only used with `--insecure-file-store`).
pub struct FileSecretStore {
    path: PathBuf,
}

impl FileSecretStore {
    pub fn new(path: PathBuf) -> Self {
        Self { path }
    }

    fn read_map(&self) -> Result<HashMap<String, StoredAuth>, SecretError> {
        if !self.path.exists() {
            return Ok(HashMap::new());
        }
        let text = fs::read_to_string(&self.path)?;
        if text.trim().is_empty() {
            return Ok(HashMap::new());
        }
        Ok(serde_json::from_str(&text)?)
    }

    fn write_map(&self, map: &HashMap<String, StoredAuth>) -> Result<(), SecretError> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let json = serde_json::to_string_pretty(map)?;
        let mut opts = OpenOptions::new();
        opts.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let mut file = opts.open(&self.path)?;
        file.write_all(json.as_bytes())?;
        file.sync_all()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&self.path, fs::Permissions::from_mode(0o600))?;
        }
        Ok(())
    }
}

impl SecretStore for FileSecretStore {
    fn load_auth(&self, profile: &str) -> Result<Option<StoredAuth>, SecretError> {
        Ok(self.read_map()?.remove(profile))
    }

    fn save_auth(&self, profile: &str, auth: &StoredAuth) -> Result<(), SecretError> {
        let mut map = self.read_map()?;
        map.insert(profile.to_string(), auth.clone());
        self.write_map(&map)
    }

    fn delete_auth(&self, profile: &str) -> Result<bool, SecretError> {
        let mut map = self.read_map()?;
        let removed = map.remove(profile).is_some();
        self.write_map(&map)?;
        Ok(removed)
    }
}

/// Vault-lent ephemeral SSH key materialized in a private 0700 dir with a 0600 file.
///
/// Overwrites and deletes the key file immediately when dropped.
pub struct EphemeralKeyFile {
    _dir: TempDir,
    key_path: PathBuf,
    known_hosts_path: Option<PathBuf>,
    key_len: usize,
}

impl EphemeralKeyFile {
    pub fn write(material: &str, known_hosts: Option<&str>) -> Result<Self, SecretError> {
        let dir = TempDir::new()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700))?;
        }
        let key_path = dir.path().join("id_ephemeral");
        let mut key_bytes = material.as_bytes().to_vec();
        if !key_bytes.ends_with(b"\n") {
            key_bytes.push(b'\n');
        }
        let key_len = key_bytes.len();

        let mut opts = OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        let write_res = (|| -> Result<(), SecretError> {
            let mut f = opts.open(&key_path)?;
            f.write_all(&key_bytes)?;
            f.sync_all()?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(&key_path, fs::Permissions::from_mode(0o600))?;
            }
            Ok(())
        })();
        key_bytes.zeroize();
        write_res?;

        let known_hosts_path = if let Some(kh) = known_hosts.filter(|s| !s.trim().is_empty()) {
            let p = dir.path().join("known_hosts");
            let mut f = OpenOptions::new().write(true).create_new(true).open(&p)?;
            f.write_all(kh.as_bytes())?;
            if !kh.ends_with('\n') {
                f.write_all(b"\n")?;
            }
            f.sync_all()?;
            Some(p)
        } else {
            None
        };

        Ok(Self {
            _dir: dir,
            key_path,
            known_hosts_path,
            key_len,
        })
    }

    pub fn key_path(&self) -> &Path {
        &self.key_path
    }

    pub fn known_hosts_path(&self) -> Option<&Path> {
        self.known_hosts_path.as_deref()
    }
}

impl Drop for EphemeralKeyFile {
    fn drop(&mut self) {
        if self.key_path.exists() {
            if let Ok(mut f) = OpenOptions::new().write(true).open(&self.key_path) {
                let zeros = vec![0u8; self.key_len.max(64)];
                let _ = f.write_all(&zeros);
                let _ = f.sync_all();
            }
            let _ = fs::remove_file(&self.key_path);
        }
        if let Some(ref kh) = self.known_hosts_path {
            let _ = fs::remove_file(kh);
        }
    }
}

// --- Redaction & credential-line filtering ---

struct RedactionRule {
    regex: Regex,
    replacement: &'static str,
}

fn build_rules() -> Vec<RedactionRule> {
    let specs: &[(&str, &str)] = &[
        // PEM private key blocks
        (
            r"(?s)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            "[REDACTED:PRIVATE_KEY]",
        ),
        // BOSS token prefixes (matching MT-10 server redaction)
        (
            r"\b(?:boss_pat|boss_sak|boss_scim|boss_run)_[A-Za-z0-9._-]{8,}\b",
            "[REDACTED:BOSS_TOKEN]",
        ),
        // Common provider / VCS / cloud keys
        (
            r"\b(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{12,}\b",
            "[REDACTED:API_KEY]",
        ),
        (r"\bsk-[A-Za-z0-9_-]{16,}\b", "[REDACTED:API_KEY]"),
        (
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b",
            "[REDACTED:GITHUB_TOKEN]",
        ),
        (
            r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
            "[REDACTED:SLACK_TOKEN]",
        ),
        (r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED:AWS_KEY]"),
        // Compact JWT / JWS tokens
        (
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",
            "[REDACTED:JWT]",
        ),
        // Authorization / API key headers
        (
            r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s\r\n]+",
            "${1}[REDACTED]",
        ),
        (r"(?i)(X-Boss-Api-Key\s*:\s*)[^\s\r\n]+", "${1}[REDACTED]"),
        (r"(?i)(Set-Cookie\s*:\s*)[^\r\n]+", "${1}[REDACTED]"),
        // URL userinfo credentials https://user:pass@host
        (
            r"(?i)(https?://[^/\s:@]+:)([^/\s@]+)(@)",
            "${1}[REDACTED]${3}",
        ),
        // CLI / env assignments containing secret key names
        (
            r"(?i)\b((?:BOSS_API_KEY|BOSS_HOST_API_KEY|API_KEY|AUTH_TOKEN|ACCESS_TOKEN|REFRESH_TOKEN|PASSWORD|PASSWD|SECRET|TOKEN|PRIVATE_KEY|COOKIE)\s*[=:]\s*)([^\s'&;]+)",
            "${1}[REDACTED]",
        ),
    ];

    let mut compiled = Vec::with_capacity(specs.len());
    for (pat, repl) in specs {
        if let Ok(re) = Regex::new(pat) {
            compiled.push(RedactionRule {
                regex: re,
                replacement: repl,
            });
        }
    }
    compiled
}

static REDACTION_RULES: LazyLock<Vec<RedactionRule>> = LazyLock::new(build_rules);
static RUNTIME_SECRETS: LazyLock<Mutex<HashSet<String>>> =
    LazyLock::new(|| Mutex::new(HashSet::new()));

/// Register an ephemeral in-memory secret (such as a session cookie value) so `redact_secrets()`
/// guarantees it can never appear in logs, session JSONL, audit records, or model prompts (`MT-6`).
pub fn register_runtime_secret(secret: &str) {
    if secret.len() < 4 {
        return;
    }
    let mut guard = RUNTIME_SECRETS
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    guard.insert(secret.to_string());
}

/// Clear all registered runtime secrets (e.g., on session end).
pub fn clear_runtime_secrets() {
    let mut guard = RUNTIME_SECRETS
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    guard.clear();
}

/// Redact tokens, API keys, passwords, PEM blocks, URL credentials, and active session cookies.
pub fn redact_secrets(input: &str) -> String {
    let mut current = input.to_string();
    for rule in REDACTION_RULES.iter() {
        let replaced = rule.regex.replace_all(&current, rule.replacement);
        if let std::borrow::Cow::Owned(next) = replaced {
            current = next;
        }
    }
    let runtime_vals: Vec<String> = {
        let guard = RUNTIME_SECRETS
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        guard.iter().cloned().collect()
    };
    for secret in runtime_vals {
        if current.contains(&secret) {
            current = current.replace(&secret, "[REDACTED:COOKIE]");
        }
    }
    current
}

/// Redact secrets across a command's argv tokens (including `--token VALUE` pairs).
pub fn redact_argv(argv: &[String]) -> Vec<String> {
    let mut out = Vec::with_capacity(argv.len());
    let mut redact_next = false;
    for arg in argv {
        if redact_next {
            out.push("[REDACTED]".to_string());
            redact_next = false;
            continue;
        }
        let lower = arg.to_ascii_lowercase();
        if matches!(
            lower.as_str(),
            "--token"
                | "--api-key"
                | "--apikey"
                | "--password"
                | "--passwd"
                | "--secret"
                | "-p"
                | "--header"
                | "-h"
        ) && (lower != "-h" && lower != "-p")
        {
            out.push(arg.clone());
            redact_next = true;
            continue;
        }
        if let Some((flag, _val)) = arg.split_once('=') {
            let fl = flag.to_ascii_lowercase();
            if fl.contains("token")
                || fl.contains("password")
                || fl.contains("passwd")
                || fl.contains("secret")
                || fl.contains("api-key")
                || fl.contains("apikey")
                || fl.contains("cookie")
            {
                out.push(format!("{flag}=[REDACTED]"));
                continue;
            }
        }
        out.push(redact_secrets(arg));
    }
    out
}

/// Returns true if `text` contains any unredacted secret pattern.
pub fn contains_forbidden_secret(text: &str) -> bool {
    redact_secrets(text) != text
}

/// Returns true if a TUI history line looks like it contains credentials and must
/// NOT be persisted to disk (`BOSSAPP-1369` credential-line history filtering).
pub fn is_credential_history_line(line: &str) -> bool {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return false;
    }
    if contains_forbidden_secret(trimmed) {
        return true;
    }
    let lower = trimmed.to_ascii_lowercase();
    for marker in [
        "boss_api_key",
        "x-boss-api-key",
        "authorization:",
        "bearer ",
        "--api-key",
        "--token",
        "--password",
        "private_key",
        "cookie:",
    ] {
        if lower.contains(marker) {
            return true;
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use codex_keyring_store::tests::MockKeyringStore;

    #[test]
    fn keyring_secret_store_round_trips_with_mock_keyring() {
        let mock = MockKeyringStore::default();
        let store = KeyringSecretStore::with_store(mock);
        let auth = StoredAuth {
            url: "https://boss.example.com".to_string(),
            access_token: Some("boss_pat_secret_1234567890".to_string()),
            refresh_token: Some("refresh_tok_1234".to_string()),
            api_key: None,
            expires_at: None,
            principal: Some(TokenPrincipal {
                id: "usr_1".to_string(),
                actor: "alice".to_string(),
                roles: vec!["operator".to_string()],
            }),
        };

        assert!(store.load_auth("default").expect("load").is_none());
        store.save_auth("default", &auth).expect("save");
        let loaded = store.load_auth("default").expect("load").expect("present");
        assert_eq!(loaded, auth);

        let dbg = format!("{loaded:?}");
        assert!(!dbg.contains("boss_pat_secret_1234567890"));
        assert!(!dbg.contains("refresh_tok_1234"));
        assert!(dbg.contains("[REDACTED]"));

        assert!(store.delete_auth("default").expect("delete"));
        assert!(store.load_auth("default").expect("load").is_none());
    }

    #[test]
    fn file_secret_store_enforces_0600_permissions() {
        let tmp = TempDir::new().expect("tempdir");
        let path = tmp.path().join("creds.json");
        let store = FileSecretStore::new(path.clone());
        let auth = StoredAuth {
            url: "http://127.0.0.1:8000".to_string(),
            access_token: None,
            refresh_token: None,
            api_key: Some("boss_pat_local_test_token".to_string()),
            expires_at: None,
            principal: None,
        };
        store.save_auth("dev", &auth).expect("save");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = fs::metadata(&path).expect("meta").permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
        assert_eq!(store.load_auth("dev").expect("load"), Some(auth));
    }

    #[test]
    fn ephemeral_key_file_sets_0600_and_removes_on_drop() {
        let key_path;
        {
            let eph = EphemeralKeyFile::write(
                "-----BEGIN OPENSSH PRIVATE KEY-----\nSECRET\n-----END OPENSSH PRIVATE KEY-----",
                Some("web-1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAtest"),
            )
            .expect("write key");
            key_path = eph.key_path().to_path_buf();
            assert!(key_path.exists());
            assert!(eph.known_hosts_path().is_some());
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = fs::metadata(&key_path).expect("meta").permissions().mode() & 0o777;
                assert_eq!(mode, 0o600);
            }
        }
        assert!(!key_path.exists(), "ephemeral key must be deleted on drop");
    }

    #[test]
    fn redacts_tokens_headers_urls_and_argv() {
        let raw = "curl -H 'Authorization: Bearer boss_pat_1234567890abcdef' https://alice:superpass@api.example.com/v1";
        let clean = redact_secrets(raw);
        assert!(!clean.contains("boss_pat_1234567890abcdef"));
        assert!(!clean.contains("superpass"));

        let argv = vec![
            "curl".to_string(),
            "--token".to_string(),
            "my-secret-value".to_string(),
            "--password=hunter2".to_string(),
        ];
        let redacted = redact_argv(&argv);
        assert_eq!(redacted[2], "[REDACTED]");
        assert_eq!(redacted[3], "--password=[REDACTED]");
    }

    #[test]
    fn filters_credential_history_lines() {
        assert!(is_credential_history_line(
            "BOSS_API_KEY=boss_pat_abcdef12345678"
        ));
        assert!(is_credential_history_line("/login --api-key foo"));
        assert!(!is_credential_history_line("sar -u 1 5"));
        assert!(!is_credential_history_line("/tasks"));
    }
}
