//! Per-domain, per-session cookie vault, Netscape/JSON parsers, Chrome Safe Storage
//! reader (approved domain only), and planted-secret scrubbing (`MT-6`, `ARCHITECTURE.md` §7).
//!
//! Hard security invariants:
//! 1. Two cookie sources ONLY: (a) a user-named cookie file (Netscape `cookies.txt` or JSON export),
//!    (b) the local Chrome SQLite store read only after an explicit per-use ask, decrypting ONLY
//!    rows for the single approved exact host while leaving the OS keychain prompt visible.
//! 2. Cookie values stay strictly in process memory (wrapped in `ZeroizeOnDrop`), NEVER written
//!    to session JSONL, NEVER sent to BOSS audit, and NEVER shown to the model.
//! 3. Consent keys on exact hostnames only (no wildcards `*`) and expires with the session or TTL.
//! 4. Audit holds domain, source kind, cookie names, method, path, status, and bytes ONLY.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::fs;
use std::path::Path;
use std::process::Command;
use std::sync::{Mutex, PoisonError};

use aes::Aes128;
use base64::{Engine as _, engine::general_purpose::STANDARD as B64};
use cbc::cipher::{BlockDecryptMut, BlockEncryptMut, KeyIvInit, block_padding::Pkcs7};
use chrono::{DateTime, Duration, Utc};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha1::Sha1;
use tempfile::TempDir;
use thiserror::Error;
use zeroize::Zeroize;

use crate::{redact_secrets, register_runtime_secret};

type HmacSha1 = Hmac<Sha1>;
type Aes128CbcDec = cbc::Decryptor<Aes128>;
type Aes128CbcEnc = cbc::Encryptor<Aes128>;

/// Default Chrome macOS PBKDF2 iteration count.
pub const CHROME_MACOS_PBKDF2_ITERATIONS: u32 = 1003;
/// Default Chrome Linux PBKDF2 iteration count.
pub const CHROME_LINUX_PBKDF2_ITERATIONS: u32 = 1;
const CHROME_SALT: &[u8] = b"saltysalt";
const CHROME_IV: [u8; 16] = [b' '; 16];

#[derive(Debug, Error)]
pub enum CookieError {
    #[error("cookie mode is disabled by BOSS policy")]
    DisabledByPolicy,
    #[error("domain '{0}' is not permitted by BOSS cookie_mode policy")]
    DomainNotAllowedByPolicy(String),
    #[error("invalid exact host '{0}' (wildcards and paths are forbidden)")]
    InvalidHost(String),
    #[error("cookie IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("cookie JSON parse error: {0}")]
    Json(#[from] serde_json::Error),
    #[error("Chrome keychain error: {0}")]
    Keychain(String),
    #[error("Chrome cookie decryption error: {0}")]
    Decryption(String),
    #[error("Chrome SQLite query error: {0}")]
    Sqlite(String),
}

/// The ONLY two allowed cookie sources (`ARCHITECTURE.md` §7.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CookieSourceKind {
    NamedFile,
    ChromeLocalStore,
}

impl CookieSourceKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NamedFile => "named_file",
            Self::ChromeLocalStore => "chrome_local_store",
        }
    }
}

/// Sensitive cookie value held strictly in process memory, zeroized on drop,
/// and never printed by `Debug`.
#[derive(Clone, PartialEq, Eq)]
pub struct CookieValue(String);

impl CookieValue {
    pub fn new(value: String) -> Self {
        register_runtime_secret(&value);
        Self(value)
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl Drop for CookieValue {
    fn drop(&mut self) {
        self.0.zeroize();
    }
}

impl fmt::Debug for CookieValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("[REDACTED:COOKIE]")
    }
}

/// Parsed in-memory cookie record.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CookieRecord {
    pub name: String,
    pub value: CookieValue,
    pub domain: String,
    pub path: String,
    pub secure: bool,
    pub http_only: bool,
    pub expires_at: Option<i64>,
}

/// Validate an exact hostname for per-domain consent (forbids wildcards `*`, paths, ports, spaces).
pub fn validate_exact_host(host: &str) -> Result<String, CookieError> {
    let trimmed = host.trim().to_ascii_lowercase();
    if trimmed.is_empty()
        || trimmed.contains('*')
        || trimmed.contains('/')
        || trimmed.contains(':')
        || trimmed.contains('@')
        || trimmed.contains(' ')
        || trimmed.starts_with('.')
        || trimmed.ends_with('.')
        || !trimmed
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'.')
    {
        return Err(CookieError::InvalidHost(host.to_string()));
    }
    Ok(trimmed)
}

/// True if a cookie's domain attribute applies to `exact_host`.
pub fn cookie_domain_matches_host(cookie_domain: &str, exact_host: &str) -> bool {
    let host = exact_host.trim().to_ascii_lowercase();
    let cd = cookie_domain.trim().to_ascii_lowercase();
    if cd.is_empty() || host.is_empty() {
        return false;
    }
    if let Some(stripped) = cd.strip_prefix('.') {
        host == stripped || host.ends_with(&format!(".{stripped}"))
    } else {
        host == cd
    }
}

/// Parse a Netscape `cookies.txt` file (including `#HttpOnly_` prefixed lines).
/// Hardened against malformed/fuzzed input — never panics.
pub fn parse_netscape_cookies(content: &str) -> Vec<CookieRecord> {
    let mut out = Vec::new();
    for raw_line in content.lines() {
        let trimmed = raw_line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let (http_only, line) = if let Some(rest) = trimmed.strip_prefix("#HttpOnly_") {
            (true, rest)
        } else if trimmed.starts_with('#') {
            continue;
        } else {
            (false, trimmed)
        };
        let cols: Vec<&str> = line.split('\t').collect();
        if cols.len() < 7 {
            continue;
        }
        let domain = cols[0].trim().to_string();
        let path = if cols[2].trim().is_empty() {
            "/".to_string()
        } else {
            cols[2].trim().to_string()
        };
        let secure = cols[3].trim().eq_ignore_ascii_case("true");
        let expires_at = cols[4].trim().parse::<i64>().ok().filter(|&v| v > 0);
        let name = cols[5].trim().to_string();
        let value = cols[6].to_string();
        if domain.is_empty() || name.is_empty() {
            continue;
        }
        out.push(CookieRecord {
            name,
            value: CookieValue::new(value),
            domain,
            path,
            secure,
            http_only,
            expires_at,
        });
    }
    out
}

#[derive(Deserialize)]
struct JsonCookieDto {
    name: String,
    value: String,
    #[serde(default)]
    domain: String,
    #[serde(default = "default_root_path")]
    path: String,
    #[serde(default)]
    secure: bool,
    #[serde(default, alias = "httpOnly", alias = "http_only")]
    http_only: bool,
    #[serde(default, alias = "expirationDate", alias = "expires")]
    expires_at: Option<f64>,
}

fn default_root_path() -> String {
    "/".to_string()
}

/// Parse browser-exported JSON cookies (`[{name, value, domain, path, secure, httpOnly, expirationDate}]`
/// or `{"cookies": [...]}`).
pub fn parse_json_cookies(content: &str) -> Result<Vec<CookieRecord>, CookieError> {
    let val: serde_json::Value = serde_json::from_str(content)?;
    let arr = if let Some(a) = val.as_array() {
        a.clone()
    } else if let Some(a) = val.get("cookies").and_then(serde_json::Value::as_array) {
        a.clone()
    } else {
        return Ok(Vec::new());
    };
    let mut out = Vec::new();
    for item in arr {
        if let Ok(dto) = serde_json::from_value::<JsonCookieDto>(item) {
            if dto.name.trim().is_empty() || dto.domain.trim().is_empty() {
                continue;
            }
            out.push(CookieRecord {
                name: dto.name,
                value: CookieValue::new(dto.value),
                domain: dto.domain,
                path: dto.path,
                secure: dto.secure,
                http_only: dto.http_only,
                expires_at: dto.expires_at.map(|v| v as i64).filter(|&v| v > 0),
            });
        }
    }
    Ok(out)
}

/// Load a user-named cookie file (auto-detecting JSON vs Netscape format) and return
/// ONLY cookies matching `approved_exact_host`.
pub fn load_cookie_file_for_host(
    path: &Path,
    approved_exact_host: &str,
) -> Result<Vec<CookieRecord>, CookieError> {
    let host = validate_exact_host(approved_exact_host)?;
    let raw = fs::read_to_string(path)?;
    let trimmed = raw.trim_start();
    let all = if trimmed.starts_with('[') || trimmed.starts_with('{') {
        parse_json_cookies(&raw)?
    } else {
        parse_netscape_cookies(&raw)
    };
    Ok(all
        .into_iter()
        .filter(|c| cookie_domain_matches_host(&c.domain, &host))
        .collect())
}

// --- Chrome Safe Storage PBKDF2-HMAC-SHA1 + AES-128-CBC reader ---

/// Keychain abstraction for Chrome Safe Storage.
/// Production `OsChromeKeychain` invokes the OS keychain with its native prompt left visible
/// (never auto-answered or bypassed); tests inject a fixture password.
pub trait ChromeKeychainProvider: Send + Sync {
    fn get_safe_storage_password(&self) -> Result<String, CookieError>;
}

/// Production OS keychain reader for `"Chrome Safe Storage"`.
pub struct OsChromeKeychain;

impl ChromeKeychainProvider for OsChromeKeychain {
    fn get_safe_storage_password(&self) -> Result<String, CookieError> {
        #[cfg(target_os = "macos")]
        {
            // Invoke macOS `security` directly so macOS displays the visible OS keychain prompt.
            let out = Command::new("/usr/bin/security")
                .args([
                    "find-generic-password",
                    "-w",
                    "-s",
                    "Chrome Safe Storage",
                    "-a",
                    "Chrome",
                ])
                .output()?;
            if !out.status.success() {
                return Err(CookieError::Keychain(
                    "Chrome Safe Storage keychain access was denied or unavailable".to_string(),
                ));
            }
            Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
        }
        #[cfg(target_os = "linux")]
        {
            if let Ok(out) = Command::new("secret-tool")
                .args(["lookup", "application", "chrome"])
                .output()
                && out.status.success()
            {
                let pw = String::from_utf8_lossy(&out.stdout).trim().to_string();
                if !pw.is_empty() {
                    return Ok(pw);
                }
            }
            Ok("peanuts".to_string())
        }
        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        {
            Err(CookieError::Keychain(
                "Chrome cookie store reading is supported on macOS and Linux only".to_string(),
            ))
        }
    }
}

/// Minimal RFC 2898 PBKDF2-HMAC-SHA1 deriving a 16-byte AES-128 key for Chrome `v10`/`v11` cookies.
pub fn pbkdf2_hmac_sha1_16(password: &[u8], salt: &[u8], iterations: u32) -> [u8; 16] {
    let mut salt_block = Vec::with_capacity(salt.len() + 4);
    salt_block.extend_from_slice(salt);
    salt_block.extend_from_slice(&1u32.to_be_bytes());

    let mut mac = <HmacSha1 as Mac>::new_from_slice(password)
        .unwrap_or_else(|_| unreachable!("HMAC accepts keys of any length"));
    mac.update(&salt_block);
    let mut u: [u8; 20] = mac.finalize().into_bytes().into();
    let mut out = u;

    for _ in 1..iterations.max(1) {
        let mut step = <HmacSha1 as Mac>::new_from_slice(password)
            .unwrap_or_else(|_| unreachable!("HMAC accepts keys of any length"));
        step.update(&u);
        u = step.finalize().into_bytes().into();
        for (acc, byte) in out.iter_mut().zip(u.iter()) {
            *acc ^= *byte;
        }
    }

    let mut key = [0u8; 16];
    key.copy_from_slice(&out[..16]);
    key
}

/// Decrypt a Chrome `v10`/`v11` AES-128-CBC encrypted cookie blob.
pub fn decrypt_chrome_cookie_v10(
    encrypted: &[u8],
    password: &str,
    iterations: u32,
) -> Result<String, CookieError> {
    if encrypted.len() < 3 {
        return Err(CookieError::Decryption(
            "encrypted value too short".to_string(),
        ));
    }
    let prefix = &encrypted[..3];
    if prefix != b"v10" && prefix != b"v11" {
        return Err(CookieError::Decryption(
            "unsupported Chrome cookie version prefix".to_string(),
        ));
    }
    let mut buf = encrypted[3..].to_vec();
    if buf.is_empty() || !buf.len().is_multiple_of(16) {
        return Err(CookieError::Decryption(
            "invalid AES-128-CBC ciphertext length".to_string(),
        ));
    }
    let mut key = pbkdf2_hmac_sha1_16(password.as_bytes(), CHROME_SALT, iterations);
    let dec = Aes128CbcDec::new(&key.into(), &CHROME_IV.into());
    let plain = dec
        .decrypt_padded_mut::<Pkcs7>(&mut buf)
        .map_err(|_| CookieError::Decryption("AES-128-CBC PKCS7 decrypt failed".to_string()))?;
    key.zeroize();
    let s = String::from_utf8(plain.to_vec())
        .map_err(|e| CookieError::Decryption(format!("non-UTF8 cookie value: {e}")))?;
    buf.zeroize();
    Ok(s)
}

/// Encrypt a plaintext cookie value into Chrome `v10` format (for fixture tests).
pub fn encrypt_chrome_cookie_v10(plaintext: &str, password: &str, iterations: u32) -> Vec<u8> {
    let mut key = pbkdf2_hmac_sha1_16(password.as_bytes(), CHROME_SALT, iterations);
    let enc = Aes128CbcEnc::new(&key.into(), &CHROME_IV.into());
    let msg = plaintext.as_bytes();
    let mut buf = vec![0u8; msg.len() + 16];
    buf[..msg.len()].copy_from_slice(msg);
    let ct_len = enc
        .encrypt_padded_mut::<Pkcs7>(&mut buf, msg.len())
        .map(|s| s.len())
        .unwrap_or(0);
    key.zeroize();
    let mut out = Vec::with_capacity(3 + ct_len);
    out.extend_from_slice(b"v10");
    out.extend_from_slice(&buf[..ct_len]);
    out
}

/// Raw row from a Chrome `Cookies` SQLite database (or fixture).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChromeCookieRow {
    pub host_key: String,
    pub name: String,
    pub path: String,
    pub encrypted_value: Vec<u8>,
    pub plain_value: String,
    pub is_secure: bool,
    pub is_httponly: bool,
    pub expires_utc: Option<i64>,
}

/// Result of reading Chrome cookies for ONE approved exact host.
#[derive(Debug)]
pub struct ChromeDomainReadResult {
    pub cookies: Vec<CookieRecord>,
    pub decrypted_rows: usize,
    pub skipped_other_domain_rows: usize,
}

/// Decrypt ONLY the Chrome cookie rows matching `approved_exact_host`.
/// Rows belonging to any other domain are skipped BEFORE decryption (and before keychain use
/// if no rows match!).
pub fn read_chrome_rows_for_domain(
    rows: &[ChromeCookieRow],
    approved_exact_host: &str,
    keychain: &dyn ChromeKeychainProvider,
    iterations: u32,
) -> Result<ChromeDomainReadResult, CookieError> {
    let host = validate_exact_host(approved_exact_host)?;
    let mut matching = Vec::new();
    let mut skipped = 0usize;
    for row in rows {
        if cookie_domain_matches_host(&row.host_key, &host) {
            matching.push(row);
        } else {
            skipped += 1;
        }
    }
    if matching.is_empty() {
        return Ok(ChromeDomainReadResult {
            cookies: Vec::new(),
            decrypted_rows: 0,
            skipped_other_domain_rows: skipped,
        });
    }

    let needs_decrypt = matching
        .iter()
        .any(|r| !r.encrypted_value.is_empty() && r.plain_value.is_empty());
    let mut password = if needs_decrypt {
        Some(keychain.get_safe_storage_password()?)
    } else {
        None
    };

    let mut cookies = Vec::new();
    let mut decrypted_rows = 0usize;
    for row in matching {
        let value_str = if !row.plain_value.is_empty() {
            row.plain_value.clone()
        } else if let Some(ref pw) = password {
            let dec = decrypt_chrome_cookie_v10(&row.encrypted_value, pw, iterations)?;
            decrypted_rows += 1;
            dec
        } else {
            continue;
        };
        cookies.push(CookieRecord {
            name: row.name.clone(),
            value: CookieValue::new(value_str),
            domain: row.host_key.clone(),
            path: if row.path.is_empty() {
                "/".to_string()
            } else {
                row.path.clone()
            },
            secure: row.is_secure,
            http_only: row.is_httponly,
            expires_at: row.expires_utc,
        });
    }
    if let Some(ref mut pw) = password {
        pw.zeroize();
    }

    Ok(ChromeDomainReadResult {
        cookies,
        decrypted_rows,
        skipped_other_domain_rows: skipped,
    })
}

/// Read ONLY rows matching `approved_exact_host` from a Chrome SQLite `Cookies` DB file
/// using a temporary read-only copy (immediately deleted on exit).
pub fn load_chrome_sqlite_for_domain(
    db_path: &Path,
    approved_exact_host: &str,
    keychain: &dyn ChromeKeychainProvider,
    iterations: u32,
) -> Result<ChromeDomainReadResult, CookieError> {
    let host = validate_exact_host(approved_exact_host)?;
    let tmp = TempDir::new()?;
    let copy_path = tmp.path().join("cookies_ro.db");
    fs::copy(db_path, &copy_path)?;

    // `host` has been strictly validated to `[a-z0-9.-]` only, so SQL interpolation is safe.
    let sql = format!(
        "SELECT host_key, name, path, hex(encrypted_value), value, is_secure, is_httponly, expires_utc \
         FROM cookies WHERE lower(host_key) = '{host}' OR lower(host_key) = '.{host}';"
    );
    let out = Command::new("sqlite3")
        .args([
            "-readonly",
            "-separator",
            "\t",
            &copy_path.to_string_lossy(),
            &sql,
        ])
        .output()
        .map_err(|e| CookieError::Sqlite(format!("failed to invoke sqlite3: {e}")))?;
    if !out.status.success() {
        return Err(CookieError::Sqlite(
            String::from_utf8_lossy(&out.stderr).trim().to_string(),
        ));
    }

    let stdout = String::from_utf8_lossy(&out.stdout);
    let mut rows = Vec::new();
    for line in stdout.lines() {
        let cols: Vec<&str> = line.split('\t').collect();
        if cols.len() < 8 {
            continue;
        }
        let enc_bytes = hex::decode(cols[3].trim()).unwrap_or_default();
        rows.push(ChromeCookieRow {
            host_key: cols[0].to_string(),
            name: cols[1].to_string(),
            path: cols[2].to_string(),
            encrypted_value: enc_bytes,
            plain_value: cols[4].to_string(),
            is_secure: cols[5].trim() == "1",
            is_httponly: cols[6].trim() == "1",
            expires_utc: cols[7].trim().parse::<i64>().ok().filter(|&v| v > 0),
        });
    }
    read_chrome_rows_for_domain(&rows, &host, keychain, iterations)
}

// --- In-memory Per-Domain Session Consent & Jar ---

#[derive(Debug)]
struct ApprovedDomainEntry {
    source: CookieSourceKind,
    cookies: Vec<CookieRecord>,
    expires_at: DateTime<Utc>,
}

/// In-memory-only per-domain, per-session cookie manager (`ARCHITECTURE.md` §7.3).
/// Never writes cookie values to disk.
#[derive(Debug, Default)]
pub struct CookieSessionManager {
    domains: Mutex<HashMap<String, ApprovedDomainEntry>>,
}

impl CookieSessionManager {
    pub fn new() -> Self {
        Self::default()
    }

    /// Store approved cookies for `exact_host` in memory with a session TTL.
    /// Rejects wildcards or malformed hosts.
    pub fn approve_domain(
        &self,
        exact_host: &str,
        source: CookieSourceKind,
        cookies: Vec<CookieRecord>,
        ttl_s: i64,
        now: DateTime<Utc>,
    ) -> Result<Vec<String>, CookieError> {
        let host = validate_exact_host(exact_host)?;
        let filtered: Vec<CookieRecord> = cookies
            .into_iter()
            .filter(|c| cookie_domain_matches_host(&c.domain, &host))
            .collect();
        let names: Vec<String> = filtered.iter().map(|c| c.name.clone()).collect();
        let entry = ApprovedDomainEntry {
            source,
            cookies: filtered,
            expires_at: now + Duration::seconds(ttl_s.clamp(60, 14_400)),
        };
        let mut guard = self.domains.lock().unwrap_or_else(PoisonError::into_inner);
        guard.insert(host, entry);
        Ok(names)
    }

    pub fn has_active_consent(&self, exact_host: &str, now: DateTime<Utc>) -> bool {
        let Ok(host) = validate_exact_host(exact_host) else {
            return false;
        };
        let guard = self.domains.lock().unwrap_or_else(PoisonError::into_inner);
        guard
            .get(&host)
            .is_some_and(|e| now < e.expires_at && !e.cookies.is_empty())
    }

    /// Return `(Cookie header value, cookie_names, source)` ONLY for `exact_host` while unexpired.
    /// Cross-host requests get `None`.
    pub fn header_for_host(
        &self,
        exact_host: &str,
        req_path: &str,
        is_https: bool,
        now: DateTime<Utc>,
    ) -> Option<(String, Vec<String>, CookieSourceKind)> {
        let host = validate_exact_host(exact_host).ok()?;
        let guard = self.domains.lock().unwrap_or_else(PoisonError::into_inner);
        let entry = guard.get(&host)?;
        if now >= entry.expires_at {
            return None;
        }
        let mut pairs = Vec::new();
        let mut names = Vec::new();
        for c in &entry.cookies {
            if c.secure && !is_https {
                continue;
            }
            if !req_path.starts_with(&c.path) {
                continue;
            }
            pairs.push(format!("{}={}", c.name, c.value.expose()));
            names.push(c.name.clone());
        }
        if pairs.is_empty() {
            return None;
        }
        Some((pairs.join("; "), names, entry.source))
    }

    /// Scrub every loaded cookie value (raw, URL-encoded, base64) plus standard secret patterns.
    pub fn redact_text(&self, text: &str) -> String {
        let mut current = redact_secrets(text);
        let values: HashSet<String> = {
            let guard = self.domains.lock().unwrap_or_else(PoisonError::into_inner);
            guard
                .values()
                .flat_map(|e| e.cookies.iter().map(|c| c.value.expose().to_string()))
                .filter(|v| v.len() >= 4)
                .collect()
        };
        for val in values {
            if current.contains(&val) {
                current = current.replace(&val, "[REDACTED:COOKIE]");
            }
            let b64 = B64.encode(val.as_bytes());
            if b64.len() >= 6 && current.contains(&b64) {
                current = current.replace(&b64, "[REDACTED:COOKIE]");
            }
        }
        current
    }

    /// Drop all in-memory cookies for a host (or all hosts).
    pub fn clear(&self) {
        let mut guard = self.domains.lock().unwrap_or_else(PoisonError::into_inner);
        guard.clear();
    }
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;

    struct StaticKeychain(String);
    impl ChromeKeychainProvider for StaticKeychain {
        fn get_safe_storage_password(&self) -> Result<String, CookieError> {
            Ok(self.0.clone())
        }
    }

    #[test]
    fn parses_netscape_and_json_cookie_exports_and_rejects_fuzzed_garbage() {
        let netscape = "\
# Netscape HTTP Cookie File
.grafana.example.com\tTRUE\t/\tTRUE\t1999999999\tgrafana_session\tsess_abc123456
#HttpOnly_grafana.example.com\tFALSE\t/api\tFALSE\t0\tgrafana_csrf\tcsrf_xyz987
malformed_line_without_tabs
other.example.com\tFALSE\t/\tTRUE\t0\tother_tok\tshould_not_match
";
        let parsed = parse_netscape_cookies(netscape);
        assert_eq!(parsed.len(), 3);
        assert_eq!(parsed[0].name, "grafana_session");
        assert!(parsed[1].http_only);
        assert_eq!(format!("{:?}", parsed[0].value), "[REDACTED:COOKIE]");

        // Fuzz / garbage input must never panic
        for junk in ["", "\0\0\t\t", "###", "a\tb\tc", "\t\t\t\t\t\t\t"] {
            let _ = parse_netscape_cookies(junk);
        }

        let json_raw = r#"[
            {"name":"grafana_session","value":"json_secret_999","domain":"grafana.example.com","path":"/","secure":true,"httpOnly":true,"expirationDate":1999999999},
            {"name":"unrelated","value":"other","domain":"evil.example.com","path":"/"}
        ]"#;
        let json_cookies = parse_json_cookies(json_raw).expect("parse json");
        assert_eq!(json_cookies.len(), 2);
        assert_eq!(json_cookies[0].name, "grafana_session");
    }

    #[test]
    fn chrome_reader_decrypts_only_approved_domain_and_leaves_other_domains_untouched() {
        let pw = "fixture-chrome-safe-storage-pw";
        let grafana_ct = encrypt_chrome_cookie_v10(
            "planted_chrome_cookie_777",
            pw,
            CHROME_MACOS_PBKDF2_ITERATIONS,
        );
        // Put invalid ciphertext on another domain — proves we NEVER even attempt to decrypt it!
        let rows = vec![
            ChromeCookieRow {
                host_key: "grafana.example.com".to_string(),
                name: "grafana_session".to_string(),
                path: "/".to_string(),
                encrypted_value: grafana_ct,
                plain_value: String::new(),
                is_secure: true,
                is_httponly: true,
                expires_utc: None,
            },
            ChromeCookieRow {
                host_key: "bank.example.com".to_string(),
                name: "bank_auth".to_string(),
                path: "/".to_string(),
                encrypted_value: b"v10CORRUPT_MUST_NOT_DECRYPT".to_vec(),
                plain_value: String::new(),
                is_secure: true,
                is_httponly: true,
                expires_utc: None,
            },
        ];

        let kc = StaticKeychain(pw.to_string());
        let res = read_chrome_rows_for_domain(
            &rows,
            "grafana.example.com",
            &kc,
            CHROME_MACOS_PBKDF2_ITERATIONS,
        )
        .expect("read chrome rows");
        assert_eq!(res.decrypted_rows, 1);
        assert_eq!(res.skipped_other_domain_rows, 1);
        assert_eq!(res.cookies.len(), 1);
        assert_eq!(res.cookies[0].name, "grafana_session");
        assert_eq!(res.cookies[0].value.expose(), "planted_chrome_cookie_777");
    }

    #[test]
    fn cookie_session_manager_enforces_exact_host_rejects_wildcards_and_redacts_values() {
        let mgr = CookieSessionManager::new();
        let now = Utc::now();

        // Wildcard consent is FORBIDDEN!
        assert!(matches!(
            mgr.approve_domain(
                "*.example.com",
                CookieSourceKind::NamedFile,
                Vec::new(),
                600,
                now
            ),
            Err(CookieError::InvalidHost(_))
        ));

        let cookies = parse_netscape_cookies(
            "grafana.example.com\tFALSE\t/\tFALSE\t0\tgrafana_session\tplanted_secret_val_42\n",
        );
        let names = mgr
            .approve_domain(
                "grafana.example.com",
                CookieSourceKind::NamedFile,
                cookies,
                600,
                now,
            )
            .expect("approve");
        assert_eq!(names, vec!["grafana_session".to_string()]);

        // Exact approved host receives the cookie header; other host gets None!
        let (hdr, hdr_names, _) = mgr
            .header_for_host("grafana.example.com", "/api/ds/query", false, now)
            .expect("header");
        assert!(hdr.contains("grafana_session=planted_secret_val_42"));
        assert_eq!(hdr_names, vec!["grafana_session".to_string()]);
        assert!(
            mgr.header_for_host("evil.example.com", "/", false, now)
                .is_none()
        );

        // Redaction scrubs the planted value both via CookieSessionManager and global redact_secrets!
        let leaked = "debug response echoed planted_secret_val_42 in body";
        let scrubbed = mgr.redact_text(leaked);
        assert!(!scrubbed.contains("planted_secret_val_42"));
        assert!(!redact_secrets(leaked).contains("planted_secret_val_42"));
    }
}
