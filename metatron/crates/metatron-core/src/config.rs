//! Layered `~/.config/metatron/config.toml` profiles, `-c key=value` overrides,
//! and BOSS environment variable fallbacks (`MT-2`).

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use metatron_protocol::ApprovalMode;
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::hooks::HookConfig;

pub const DEFAULT_BOSS_URL: &str = "http://127.0.0.1:8000";
pub const DEFAULT_PROFILE_NAME: &str = "default";

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("config IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("invalid config.toml: {0}")]
    TomlRead(#[from] toml::de::Error),
    #[error("failed to serialize config.toml: {0}")]
    TomlWrite(#[from] toml::ser::Error),
    #[error("invalid -c override '{0}' (expected key=value)")]
    InvalidOverride(String),
    #[error("profile '{0}' not found in config.toml")]
    ProfileNotFound(String),
    #[error("invalid approval mode: {0}")]
    InvalidMode(String),
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProfileConfig {
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub project: Option<String>,
    #[serde(default)]
    pub agent: Option<String>,
    #[serde(default)]
    pub mode: Option<ApprovalMode>,
    #[serde(default)]
    pub actor: Option<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct MetatronConfig {
    #[serde(default)]
    pub default_profile: Option<String>,
    #[serde(default)]
    pub url: Option<String>,
    #[serde(default)]
    pub project: Option<String>,
    #[serde(default)]
    pub agent: Option<String>,
    #[serde(default)]
    pub mode: Option<ApprovalMode>,
    #[serde(default)]
    pub actor: Option<String>,
    #[serde(default)]
    pub profiles: BTreeMap<String, ProfileConfig>,
    #[serde(default)]
    pub hooks: Vec<HookConfig>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedProfile {
    pub profile_name: String,
    pub url: String,
    pub project: String,
    pub agent: Option<String>,
    pub mode: ApprovalMode,
    pub actor: Option<String>,
}

impl MetatronConfig {
    pub fn default_config_path() -> PathBuf {
        if let Ok(explicit) = std::env::var("METATRON_CONFIG")
            && !explicit.trim().is_empty()
        {
            return PathBuf::from(explicit);
        }
        dirs::config_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("metatron")
            .join("config.toml")
    }

    pub fn default_data_dir() -> PathBuf {
        if let Ok(explicit) = std::env::var("METATRON_DATA_DIR")
            && !explicit.trim().is_empty()
        {
            return PathBuf::from(explicit);
        }
        dirs::data_local_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join("metatron")
    }

    pub fn load_from(path: &Path) -> Result<Self, ConfigError> {
        if !path.exists() {
            return Ok(Self::default());
        }
        let text = fs::read_to_string(path)?;
        if text.trim().is_empty() {
            return Ok(Self::default());
        }
        Ok(toml::from_str(&text)?)
    }

    pub fn save_to(&self, path: &Path) -> Result<(), ConfigError> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let text = toml::to_string_pretty(self)?;
        fs::write(path, text)?;
        Ok(())
    }

    pub fn upsert_profile_url(&mut self, profile: &str, url: &str) {
        let entry = self.profiles.entry(profile.to_string()).or_default();
        entry.url = Some(url.trim_end_matches('/').to_string());
        if self.default_profile.is_none() {
            self.default_profile = Some(profile.to_string());
        }
    }

    /// Resolve effective profile combining config file, profile section, environment
    /// variables (`BOSS_API_URL`, `BOSS_PROJECT_ID`, `BOSS_ACTOR`), and `-c key=value` overrides.
    pub fn resolve(
        &self,
        profile_flag: Option<&str>,
        overrides: &[String],
    ) -> Result<ResolvedProfile, ConfigError> {
        let mut override_map: BTreeMap<String, String> = BTreeMap::new();
        for item in overrides {
            let (k, v) = item
                .split_once('=')
                .ok_or_else(|| ConfigError::InvalidOverride(item.clone()))?;
            override_map.insert(k.trim().to_string(), v.trim().to_string());
        }

        let requested_profile = profile_flag
            .map(ToString::to_string)
            .or_else(|| override_map.get("profile").cloned())
            .or_else(|| self.default_profile.clone())
            .unwrap_or_else(|| DEFAULT_PROFILE_NAME.to_string());

        let profile_section = if let Some(sec) = self.profiles.get(&requested_profile) {
            sec.clone()
        } else if profile_flag.is_some() || override_map.contains_key("profile") {
            return Err(ConfigError::ProfileNotFound(requested_profile));
        } else {
            ProfileConfig::default()
        };

        let env_url = std::env::var("BOSS_API_URL")
            .ok()
            .filter(|s| !s.trim().is_empty());
        let env_project = std::env::var("BOSS_PROJECT_ID")
            .ok()
            .filter(|s| !s.trim().is_empty());
        let env_actor = std::env::var("BOSS_ACTOR")
            .ok()
            .filter(|s| !s.trim().is_empty());

        let url = override_map
            .get("url")
            .cloned()
            .or(profile_section.url)
            .or_else(|| self.url.clone())
            .or(env_url)
            .unwrap_or_else(|| DEFAULT_BOSS_URL.to_string())
            .trim_end_matches('/')
            .to_string();

        let project = override_map
            .get("project")
            .cloned()
            .or(profile_section.project)
            .or_else(|| self.project.clone())
            .or(env_project)
            .unwrap_or_else(|| "boss-dogfood".to_string());

        let agent = override_map
            .get("agent")
            .cloned()
            .or(profile_section.agent)
            .or_else(|| self.agent.clone());

        let mode = if let Some(m) = override_map.get("mode") {
            m.parse::<ApprovalMode>()
                .map_err(ConfigError::InvalidMode)?
        } else {
            profile_section
                .mode
                .or(self.mode)
                .unwrap_or(ApprovalMode::Ask)
        };

        let actor = override_map
            .get("actor")
            .cloned()
            .or(profile_section.actor)
            .or_else(|| self.actor.clone())
            .or(env_actor);

        Ok(ResolvedProfile {
            profile_name: requested_profile,
            url,
            project,
            agent,
            mode,
            actor,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn loads_profiles_and_applies_cli_overrides() {
        let tmp = TempDir::new().expect("tmp");
        let path = tmp.path().join("config.toml");
        fs::write(
            &path,
            r#"
default_profile = "dev"
mode = "ask"

[profiles.dev]
url = "http://127.0.0.1:8000"
project = "boss-dogfood"

[profiles.prod]
url = "https://boss.prod.example.com"
project = "prod-ops"
agent = "sre-captain"
mode = "risky-only"
"#,
        )
        .expect("write");

        let cfg = MetatronConfig::load_from(&path).expect("load");
        let dev = cfg.resolve(None, &[]).expect("resolve dev");
        assert_eq!(dev.url, "http://127.0.0.1:8000");
        assert_eq!(dev.project, "boss-dogfood");
        assert_eq!(dev.mode, ApprovalMode::Ask);

        let prod = cfg
            .resolve(
                Some("prod"),
                &[
                    "project=incident-99".to_string(),
                    "mode=read-only".to_string(),
                ],
            )
            .expect("resolve prod");
        assert_eq!(prod.url, "https://boss.prod.example.com");
        assert_eq!(prod.project, "incident-99");
        assert_eq!(prod.agent.as_deref(), Some("sre-captain"));
        assert_eq!(prod.mode, ApprovalMode::ReadOnly);
    }

    #[test]
    fn errors_on_unknown_explicit_profile() {
        let cfg = MetatronConfig::default();
        assert!(matches!(
            cfg.resolve(Some("missing"), &[]),
            Err(ConfigError::ProfileNotFound(_))
        ));
    }
}
