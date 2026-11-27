//! Shared BOSS + local `SKILL.md` skills loader (`MT-9`, `ARCHITECTURE.md` §8).
//!
//! Security invariants:
//! - Only `name` and `description` are exposed to the prompt (`prompt_metadata_catalog`);
//!   the full skill body is loaded on demand when selected/mentioned.
//! - Declared tool dependencies are checked against `PolicyEvaluator`.
//! - A local skill can **NEVER widen permissions**: if a skill requests a forbidden or
//!   unapproved tool (or attempts to claim a looser mode), it is rejected/clamped.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use metatron_policy::PolicyEvaluator;
use metatron_protocol::BossSkillRecord;
use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum SkillError {
    #[error("skill IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("invalid SKILL.md frontmatter in {path}: {reason}")]
    InvalidFrontmatter { path: String, reason: String },
    #[error("skill '{name}' not found")]
    NotFound { name: String },
    #[error("skill '{name}' blocked by policy: requires disallowed tool '{tool}'")]
    PermissionEscalationDenied { name: String, tool: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SkillSource {
    BossGranted,
    LocalUser,
    LocalRepo,
}

/// Metadata-only view exposed to the model prompt (never includes body).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SkillMetadata {
    pub name: String,
    pub description: String,
    pub source: SkillSource,
    pub declared_tools: Vec<String>,
    pub declared_dependencies: Vec<String>,
}

#[derive(Debug, Clone)]
struct LoadedSkill {
    meta: SkillMetadata,
    body: Option<String>,
    path: Option<PathBuf>,
}

/// Registry of BOSS-granted and local `SKILL.md` skills with strict policy gating.
#[derive(Debug, Default)]
pub struct SkillRegistry {
    skills: BTreeMap<String, LoadedSkill>,
}

impl SkillRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Load BOSS-granted skills (`GET /v1/skills`). BOSS skills take precedence over local ones
    /// with the same name so a local file cannot shadow a governed BOSS skill.
    pub fn load_boss_skills(&mut self, records: Vec<BossSkillRecord>) {
        for rec in records {
            if !rec.enabled || rec.name.trim().is_empty() {
                continue;
            }
            let name = rec.name.trim().to_string();
            self.skills.insert(
                name.clone(),
                LoadedSkill {
                    meta: SkillMetadata {
                        name,
                        description: rec.description,
                        source: SkillSource::BossGranted,
                        declared_tools: rec.tools,
                        declared_dependencies: rec.dependencies,
                    },
                    body: rec.body,
                    path: None,
                },
            );
        }
    }

    /// Discover local `SKILL.md` files under `root` (`<root>/<skill-dir>/SKILL.md` or `<root>/SKILL.md`).
    /// Parses ONLY frontmatter metadata into memory; body is read lazily on demand.
    pub fn load_local_dir(
        &mut self,
        root: &Path,
        source: SkillSource,
    ) -> Result<usize, SkillError> {
        if !root.exists() {
            return Ok(0);
        }
        let mut loaded = 0usize;
        let direct = root.join("SKILL.md");
        if direct.is_file() && self.register_local_skill_file(&direct, source)? {
            loaded += 1;
        }
        if let Ok(entries) = fs::read_dir(root) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.is_dir() {
                    let skill_md = p.join("SKILL.md");
                    if skill_md.is_file() && self.register_local_skill_file(&skill_md, source)? {
                        loaded += 1;
                    }
                }
            }
        }
        Ok(loaded)
    }

    fn register_local_skill_file(
        &mut self,
        path: &Path,
        source: SkillSource,
    ) -> Result<bool, SkillError> {
        let raw = fs::read_to_string(path)?;
        let (meta, _body_offset) = parse_skill_md_frontmatter(&raw, path, source)?;
        // Never allow a local skill to overwrite a BOSS-granted skill.
        if let Some(existing) = self.skills.get(&meta.name)
            && existing.meta.source == SkillSource::BossGranted
        {
            return Ok(false);
        }
        self.skills.insert(
            meta.name.clone(),
            LoadedSkill {
                meta,
                body: None,
                path: Some(path.to_path_buf()),
            },
        );
        Ok(true)
    }

    /// Return metadata-only catalog (name + description) for skills whose declared
    /// tool dependencies are permitted by `evaluator`. Skills attempting to widen permissions
    /// are excluded from the prompt catalog!
    pub fn prompt_metadata_catalog(&self, evaluator: &PolicyEvaluator) -> Vec<SkillMetadata> {
        self.skills
            .values()
            .filter(|s| check_skill_dependencies(&s.meta, evaluator).is_ok())
            .map(|s| s.meta.clone())
            .collect()
    }

    /// Format metadata-only skill lines for system/turn context (`name: description` ONLY;
    /// zero body bytes leak into the default prompt!).
    pub fn format_prompt_index(&self, evaluator: &PolicyEvaluator) -> String {
        let catalog = self.prompt_metadata_catalog(evaluator);
        if catalog.is_empty() {
            return String::new();
        }
        let mut out = String::from("Available skills (metadata only; load body on demand):\n");
        for m in catalog {
            out.push_str(&format!("- ${}: {}\n", m.name, m.description));
        }
        out
    }

    /// Load a skill's full body on demand, enforcing that all declared tools/dependencies
    /// are permitted by `evaluator` (a skill can NEVER widen permissions).
    pub fn load_skill_body(
        &self,
        name: &str,
        evaluator: &PolicyEvaluator,
    ) -> Result<String, SkillError> {
        let entry = self.skills.get(name).ok_or_else(|| SkillError::NotFound {
            name: name.to_string(),
        })?;
        check_skill_dependencies(&entry.meta, evaluator)?;

        if let Some(ref body) = entry.body {
            return Ok(body.clone());
        }
        if let Some(ref path) = entry.path {
            let raw = fs::read_to_string(path)?;
            let (_meta, body_start) = parse_skill_md_frontmatter(&raw, path, entry.meta.source)?;
            return Ok(raw[body_start..].trim().to_string());
        }
        Ok(String::new())
    }
}

fn check_skill_dependencies(
    meta: &SkillMetadata,
    evaluator: &PolicyEvaluator,
) -> Result<(), SkillError> {
    for tool in &meta.declared_tools {
        if !evaluator.is_tool_allowed(tool) {
            return Err(SkillError::PermissionEscalationDenied {
                name: meta.name.clone(),
                tool: tool.clone(),
            });
        }
    }
    Ok(())
}

/// Parse Codex / Claude `SKILL.md` YAML-like frontmatter (`---\n...\n---`).
/// Returns `(SkillMetadata, byte_offset_of_body)`.
pub fn parse_skill_md_frontmatter(
    raw: &str,
    path: &Path,
    source: SkillSource,
) -> Result<(SkillMetadata, usize), SkillError> {
    let trimmed_start = raw.trim_start_matches('\u{feff}');
    if !trimmed_start.starts_with("---") {
        return Err(SkillError::InvalidFrontmatter {
            path: path.display().to_string(),
            reason: "missing opening '---' frontmatter delimiter".to_string(),
        });
    }
    let after_open = &trimmed_start[3..];
    let after_open = after_open
        .strip_prefix("\r\n")
        .or_else(|| after_open.strip_prefix('\n'))
        .ok_or_else(|| SkillError::InvalidFrontmatter {
            path: path.display().to_string(),
            reason: "missing newline after opening '---'".to_string(),
        })?;

    let mut name = String::new();
    let mut description = String::new();
    let mut declared_tools: Vec<String> = Vec::new();
    let mut declared_dependencies: Vec<String> = Vec::new();
    let mut current_list: Option<&str> = None;

    let mut offset = raw.len() - after_open.len();
    let mut found_close = false;

    for line in after_open.split_inclusive('\n') {
        let stripped = line.trim();
        offset += line.len();
        if stripped == "---" {
            found_close = true;
            break;
        }
        if stripped.is_empty() || stripped.starts_with('#') {
            continue;
        }
        if let Some(item) = stripped.strip_prefix("- ") {
            let val = unquote(item.trim());
            if !val.is_empty() {
                match current_list {
                    Some("tools") => declared_tools.push(val),
                    Some("dependencies") => declared_dependencies.push(val),
                    _ => {}
                }
            }
            continue;
        }
        if let Some((k, v)) = stripped.split_once(':') {
            let key = k.trim().to_ascii_lowercase();
            let val = v.trim();
            current_list = None;
            match key.as_str() {
                "name" => name = unquote(val),
                "description" => description = unquote(val),
                "tools" => {
                    if val.is_empty() {
                        current_list = Some("tools");
                    } else {
                        declared_tools.extend(parse_inline_list(val));
                    }
                }
                "dependencies" => {
                    if val.is_empty() {
                        current_list = Some("dependencies");
                    } else {
                        declared_dependencies.extend(parse_inline_list(val));
                    }
                }
                _ => {}
            }
        }
    }

    if !found_close {
        return Err(SkillError::InvalidFrontmatter {
            path: path.display().to_string(),
            reason: "unterminated YAML frontmatter (missing closing '---')".to_string(),
        });
    }
    if name.trim().is_empty() {
        return Err(SkillError::InvalidFrontmatter {
            path: path.display().to_string(),
            reason: "missing required 'name' field".to_string(),
        });
    }

    Ok((
        SkillMetadata {
            name,
            description,
            source,
            declared_tools,
            declared_dependencies,
        },
        offset,
    ))
}

fn unquote(s: &str) -> String {
    let s = s.trim();
    if (s.starts_with('"') && s.ends_with('"') && s.len() >= 2)
        || (s.starts_with('\'') && s.ends_with('\'') && s.len() >= 2)
    {
        s[1..s.len() - 1].to_string()
    } else {
        s.to_string()
    }
}

fn parse_inline_list(s: &str) -> Vec<String> {
    let inner = s.trim().trim_start_matches('[').trim_end_matches(']');
    inner
        .split(',')
        .map(unquote)
        .filter(|v| !v.is_empty())
        .collect()
}

#[cfg(test)]
#[allow(clippy::expect_used, clippy::unwrap_used)]
mod tests {
    use super::*;
    use chrono::{Duration, Utc};
    use ed25519_dalek::SigningKey;
    use metatron_policy::{
        GrantStore, LocalPolicyConfig, sign_policy_bundle_ed25519, verify_signed_policy_bundle,
    };
    use metatron_protocol::{ApprovalMode, CookieModePolicy, PolicyBundle};
    use rand::rngs::OsRng;
    use tempfile::TempDir;

    fn make_evaluator(tools_allowed: Vec<&str>) -> PolicyEvaluator {
        let now = Utc::now();
        let sk = SigningKey::generate(&mut OsRng);
        let bundle = PolicyBundle {
            version: "v-skills".to_string(),
            project_id: Some("boss-dogfood".to_string()),
            issued_at: Some(now),
            expires_at: now + Duration::hours(1),
            mode_floor: ApprovalMode::RiskyOnly,
            tools_allowed: tools_allowed.into_iter().map(str::to_string).collect(),
            rules: Vec::new(),
            cookie_mode: CookieModePolicy::default(),
            host_diagnostics: None,
            mcp: None,
        };
        let (signed, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-sk", &sk).expect("sign");
        let verified = verify_signed_policy_bundle(&signed, &jwks, now).expect("verify");
        PolicyEvaluator::with_verified_bundle(
            verified,
            ApprovalMode::RiskyOnly,
            LocalPolicyConfig::default(),
            GrantStore::default(),
        )
    }

    #[test]
    fn loads_boss_and_local_skills_metadata_only_and_refuses_permission_widening() {
        let tmp = TempDir::new().expect("tmp");
        let safe_dir = tmp.path().join("video-edit");
        let bad_dir = tmp.path().join("escalate-skill");
        fs::create_dir_all(&safe_dir).expect("mkdir");
        fs::create_dir_all(&bad_dir).expect("mkdir");

        fs::write(
            safe_dir.join("SKILL.md"),
            "---\nname: video-edit\ndescription: Trim and inspect videos with ffmpeg\ntools: [shell_local, view_image]\n---\n# Video Edit Body\nUNIQUE_SECRET_BODY_MARKER\n",
        )
        .expect("write safe");

        fs::write(
            bad_dir.join("SKILL.md"),
            "---\nname: escalate-skill\ndescription: Demands forbidden tool\ntools:\n  - apply_patch\n---\nNever allowed.\n",
        )
        .expect("write bad");

        let mut reg = SkillRegistry::new();
        reg.load_boss_skills(vec![BossSkillRecord {
            id: "sk_1".to_string(),
            name: "sre-triage".to_string(),
            description: "Investigate CPU and latency spikes".to_string(),
            body: Some("BOSS_TRIAGE_BODY_MARKER".to_string()),
            tools: vec!["ssh_run".to_string()],
            dependencies: vec!["sysstat".to_string()],
            enabled: true,
        }]);
        let count = reg
            .load_local_dir(tmp.path(), SkillSource::LocalRepo)
            .expect("load local");
        assert_eq!(count, 2);

        let eval = make_evaluator(vec!["shell_local", "ssh_run", "view_image"]);

        // Prompt index contains ONLY name + description, NEVER body markers, and EXCLUDES escalating skill!
        let prompt_idx = reg.format_prompt_index(&eval);
        assert!(prompt_idx.contains("$sre-triage: Investigate CPU and latency spikes"));
        assert!(prompt_idx.contains("$video-edit: Trim and inspect videos with ffmpeg"));
        assert!(!prompt_idx.contains("UNIQUE_SECRET_BODY_MARKER"));
        assert!(!prompt_idx.contains("BOSS_TRIAGE_BODY_MARKER"));
        assert!(!prompt_idx.contains("escalate-skill"));

        // On-demand body loading succeeds for allowed skills.
        let body = reg.load_skill_body("video-edit", &eval).expect("load body");
        assert!(body.contains("UNIQUE_SECRET_BODY_MARKER"));

        // Local skill declaring disallowed tool `apply_patch` FAILS CLOSED (cannot widen permissions).
        let err = reg
            .load_skill_body("escalate-skill", &eval)
            .expect_err("must reject permission widening");
        assert!(matches!(
            err,
            SkillError::PermissionEscalationDenied { ref tool, .. } if tool == "apply_patch"
        ));
    }
}
