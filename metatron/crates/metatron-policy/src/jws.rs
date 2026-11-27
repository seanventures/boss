//! Ed25519 compact JWS verification for BOSS signed policy bundles (`MT-7` / `MT-10`).
//!
//! Mirrors `engine/boss_engine/identity/jwks.py` (RFC 8725 BCP):
//! - `alg` must be strictly `"EdDSA"` (`none`, `HS*`, `RS*`, `ES*` refused).
//! - `typ` must be strictly `"boss-policy+jws"`.
//! - `crit` header is refused.
//! - `kid` is resolved strictly against the local JWKS (embedded `jwk`/`jku`/`x5u`/`x5c` ignored).
//! - Expired bundles are refused; every rule's `match` and `not_match` examples are executed
//!   as self-tests at load time and fail the load if any example disagrees.

use std::collections::BTreeMap;

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use chrono::{DateTime, Utc};
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use metatron_protocol::{
    JwkKey, JwksDocument, POLICY_JWS_ALG, POLICY_JWS_TYP, PolicyBundle, PolicyRule,
    SignedPolicyResponse,
};
use serde_json::Value;
use thiserror::Error;

use crate::evaluator::rule_matches_example;

const MAX_JWS_BYTES: usize = 256 * 1024;

#[derive(Debug, Error)]
pub enum JwsError {
    #[error("malformed policy JWS: {0}")]
    Malformed(String),
    #[error("unsupported JWS algorithm '{0}' (expected EdDSA)")]
    BadAlg(String),
    #[error("unexpected JWS typ '{0}' (expected {POLICY_JWS_TYP})")]
    BadTyp(String),
    #[error("critical JWS header extensions ('crit') are not supported")]
    BadCrit,
    #[error("missing or invalid JWS kid")]
    BadKid,
    #[error("unknown signing key id '{0}'")]
    UnknownKid(String),
    #[error("invalid Ed25519 JWK: {0}")]
    BadJwk(String),
    #[error("invalid Ed25519 signature")]
    BadSignature,
    #[error("policy bundle expired at {0}")]
    Expired(DateTime<Utc>),
    #[error("signed bundle payload does not match response bundle")]
    BundleMismatch,
    #[error("policy rule '{rule_id}' example failed: {detail}")]
    RuleExampleFailed { rule_id: String, detail: String },
}

/// A cryptographically verified, non-expired, example-tested BOSS policy bundle.
#[derive(Debug, Clone)]
pub struct VerifiedPolicyBundle {
    pub bundle: PolicyBundle,
    pub kid: String,
    pub verified_at: DateTime<Utc>,
}

impl VerifiedPolicyBundle {
    pub fn is_expired(&self, now: DateTime<Utc>) -> bool {
        now >= self.bundle.expires_at
    }
}

fn decode_b64u(segment: &str) -> Result<Vec<u8>, JwsError> {
    if segment.is_empty()
        || segment
            .bytes()
            .any(|b| matches!(b, b'=' | b'+' | b'/' | b' ' | b'\n' | b'\r'))
    {
        return Err(JwsError::Malformed("invalid base64url segment".to_string()));
    }
    URL_SAFE_NO_PAD
        .decode(segment)
        .map_err(|e| JwsError::Malformed(e.to_string()))
}

fn verifying_key_from_jwk(jwk: &JwkKey) -> Result<VerifyingKey, JwsError> {
    if jwk.kty != "OKP" || jwk.crv != "Ed25519" {
        return Err(JwsError::BadJwk(format!(
            "expected OKP/Ed25519, got {}/{}",
            jwk.kty, jwk.crv
        )));
    }
    if jwk.d.is_some() {
        return Err(JwsError::BadJwk(
            "public JWK must not contain private key material 'd'".to_string(),
        ));
    }
    let raw = decode_b64u(&jwk.x)?;
    let bytes: [u8; 32] = raw
        .try_into()
        .map_err(|_| JwsError::BadJwk("Ed25519 key must be 32 bytes".to_string()))?;
    VerifyingKey::from_bytes(&bytes).map_err(|e| JwsError::BadJwk(e.to_string()))
}

/// Verify a `SignedPolicyResponse` against a local `JwksDocument` at timestamp `now`.
pub fn verify_signed_policy_bundle(
    response: &SignedPolicyResponse,
    jwks: &JwksDocument,
    now: DateTime<Utc>,
) -> Result<VerifiedPolicyBundle, JwsError> {
    let token = response.jws.trim();
    if token.is_empty() || token.len() > MAX_JWS_BYTES {
        return Err(JwsError::Malformed("empty or oversized JWS".to_string()));
    }
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 || parts[0].is_empty() || parts[1].is_empty() || parts[2].is_empty() {
        return Err(JwsError::Malformed(
            "expected 3 dot-separated JWS parts".to_string(),
        ));
    }

    let header_bytes = decode_b64u(parts[0])?;
    let payload_bytes = decode_b64u(parts[1])?;
    let sig_bytes = decode_b64u(parts[2])?;

    let header: Value = serde_json::from_slice(&header_bytes)
        .map_err(|e| JwsError::Malformed(format!("invalid header JSON: {e}")))?;
    let Some(header_obj) = header.as_object() else {
        return Err(JwsError::Malformed(
            "header must be a JSON object".to_string(),
        ));
    };

    // RFC 8725 checks before touching any key or payload.
    let alg = header_obj
        .get("alg")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if alg != POLICY_JWS_ALG {
        return Err(JwsError::BadAlg(alg.to_string()));
    }
    let typ = header_obj
        .get("typ")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if typ != POLICY_JWS_TYP {
        return Err(JwsError::BadTyp(typ.to_string()));
    }
    if header_obj.contains_key("crit") {
        return Err(JwsError::BadCrit);
    }
    let kid = header_obj
        .get("kid")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .ok_or(JwsError::BadKid)?;

    let jwk = jwks
        .keys
        .iter()
        .find(|k| k.kid.as_deref() == Some(kid))
        .ok_or_else(|| JwsError::UnknownKid(kid.to_string()))?;
    let verifying_key = verifying_key_from_jwk(jwk)?;

    let sig_arr: [u8; 64] = sig_bytes.try_into().map_err(|_| JwsError::BadSignature)?;
    let signature = Signature::from_bytes(&sig_arr);
    let signing_input = format!("{}.{}", parts[0], parts[1]);
    verifying_key
        .verify_strict(signing_input.as_bytes(), &signature)
        .map_err(|_| JwsError::BadSignature)?;

    let signed_bundle: PolicyBundle = serde_json::from_slice(&payload_bytes)
        .map_err(|e| JwsError::Malformed(format!("invalid bundle payload JSON: {e}")))?;

    if signed_bundle.version != response.bundle.version
        || signed_bundle.expires_at != response.bundle.expires_at
        || signed_bundle.rules.len() != response.bundle.rules.len()
    {
        return Err(JwsError::BundleMismatch);
    }

    if now >= signed_bundle.expires_at {
        return Err(JwsError::Expired(signed_bundle.expires_at));
    }

    // Run every rule's match / not_match examples as unit tests on bundle load.
    validate_rule_examples(&signed_bundle.rules)?;

    Ok(VerifiedPolicyBundle {
        bundle: signed_bundle,
        kid: kid.to_string(),
        verified_at: now,
    })
}

/// Validate every rule's `examples.match` and `examples.not_match` lists.
pub fn validate_rule_examples(rules: &[PolicyRule]) -> Result<(), JwsError> {
    for rule in rules {
        for sample in &rule.examples.should_match {
            if !rule_matches_example(rule, sample) {
                return Err(JwsError::RuleExampleFailed {
                    rule_id: rule.id.clone(),
                    detail: format!("match example '{sample}' did not match rule"),
                });
            }
        }
        for sample in &rule.examples.not_match {
            if rule_matches_example(rule, sample) {
                return Err(JwsError::RuleExampleFailed {
                    rule_id: rule.id.clone(),
                    detail: format!("not_match example '{sample}' unexpectedly matched rule"),
                });
            }
        }
    }
    Ok(())
}

/// Sign a `PolicyBundle` with an Ed25519 `SigningKey` using the exact BOSS compact JWS format.
pub fn sign_policy_bundle_ed25519(
    bundle: &PolicyBundle,
    kid: &str,
    signing_key: &SigningKey,
) -> Result<(SignedPolicyResponse, JwksDocument), JwsError> {
    let mut header = BTreeMap::new();
    header.insert("alg", POLICY_JWS_ALG);
    header.insert("kid", kid);
    header.insert("typ", POLICY_JWS_TYP);
    let header_json =
        serde_json::to_vec(&header).map_err(|e| JwsError::Malformed(e.to_string()))?;
    let payload_json =
        serde_json::to_vec(bundle).map_err(|e| JwsError::Malformed(e.to_string()))?;

    let signing_input = format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(&header_json),
        URL_SAFE_NO_PAD.encode(&payload_json)
    );
    let sig = signing_key.sign(signing_input.as_bytes());
    let jws = format!("{signing_input}.{}", URL_SAFE_NO_PAD.encode(sig.to_bytes()));

    let pub_key = signing_key.verifying_key();
    let jwks = JwksDocument {
        keys: vec![JwkKey {
            kty: "OKP".to_string(),
            crv: "Ed25519".to_string(),
            x: URL_SAFE_NO_PAD.encode(pub_key.to_bytes()),
            kid: Some(kid.to_string()),
            alg: Some(POLICY_JWS_ALG.to_string()),
            key_use: Some("sig".to_string()),
            d: None,
        }],
    };
    let resp = SignedPolicyResponse {
        bundle: bundle.clone(),
        jws,
        kid: kid.to_string(),
        alg: POLICY_JWS_ALG.to_string(),
        typ: POLICY_JWS_TYP.to_string(),
    };
    Ok((resp, jwks))
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;
    use metatron_protocol::{
        ApprovalMode, CookieModePolicy, PolicyDecision, PolicyRuleExamples, PolicyRuleMatch,
    };
    use rand::rngs::OsRng;

    fn sample_bundle(expires_at: DateTime<Utc>) -> PolicyBundle {
        PolicyBundle {
            version: "v1-test".to_string(),
            project_id: Some("boss-dogfood".to_string()),
            issued_at: Some(Utc::now()),
            expires_at,
            mode_floor: ApprovalMode::Ask,
            tools_allowed: vec!["shell_local".to_string(), "ssh_run".to_string()],
            rules: vec![PolicyRule {
                id: "r-sar".to_string(),
                scope: Some("project".to_string()),
                priority: 10,
                matcher: PolicyRuleMatch {
                    tool: Some("ssh_run".to_string()),
                    argv_prefix: Some(vec!["sar".to_string()]),
                    ..Default::default()
                },
                decision: PolicyDecision::Allow,
                hard: false,
                justification: "read-only sysstat".to_string(),
                examples: PolicyRuleExamples {
                    should_match: vec!["sar -u 1 5".to_string()],
                    not_match: vec!["rm -rf /tmp/x".to_string()],
                },
            }],
            cookie_mode: CookieModePolicy::default(),
            host_diagnostics: None,
            mcp: None,
        }
    }

    #[test]
    fn valid_ed25519_signed_bundle_verifies() {
        let sk = SigningKey::generate(&mut OsRng);
        let now = Utc::now();
        let bundle = sample_bundle(now + Duration::hours(1));
        let (resp, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-1", &sk).expect("sign");

        let verified = verify_signed_policy_bundle(&resp, &jwks, now).expect("verify");
        assert_eq!(verified.kid, "kid-1");
        assert_eq!(verified.bundle.version, "v1-test");
    }

    #[test]
    fn rejects_expired_tampered_unknown_kid_and_bad_alg_bundles() {
        let sk = SigningKey::generate(&mut OsRng);
        let now = Utc::now();

        // Expired
        let expired_bundle = sample_bundle(now - Duration::seconds(1));
        let (resp, jwks) = sign_policy_bundle_ed25519(&expired_bundle, "kid-1", &sk).expect("sign");
        assert!(matches!(
            verify_signed_policy_bundle(&resp, &jwks, now),
            Err(JwsError::Expired(_))
        ));

        // Tampered signature (flip a non-trailing character so it can never be a no-op)
        let fresh = sample_bundle(now + Duration::hours(1));
        let (mut resp, jwks) = sign_policy_bundle_ed25519(&fresh, "kid-1", &sk).expect("sign");
        let mut chars: Vec<char> = resp.jws.chars().collect();
        let flip_idx = chars.len() - 5;
        chars[flip_idx] = if chars[flip_idx] == 'A' { 'B' } else { 'A' };
        resp.jws = chars.into_iter().collect();
        assert!(matches!(
            verify_signed_policy_bundle(&resp, &jwks, now),
            Err(JwsError::BadSignature | JwsError::Malformed(_))
        ));

        // Unknown kid
        let (resp_ok, mut jwks_wrong) =
            sign_policy_bundle_ed25519(&fresh, "kid-1", &sk).expect("sign");
        jwks_wrong.keys[0].kid = Some("kid-other".to_string());
        assert!(matches!(
            verify_signed_policy_bundle(&resp_ok, &jwks_wrong, now),
            Err(JwsError::UnknownKid(_))
        ));

        // Algorithm confusion (`alg: none`)
        let fake_header =
            URL_SAFE_NO_PAD.encode(br#"{"alg":"none","kid":"kid-1","typ":"boss-policy+jws"}"#);
        let payload_part = resp_ok.jws.split('.').nth(1).expect("payload");
        let sig_part = resp_ok.jws.split('.').nth(2).expect("sig");
        let mut bad_alg = resp_ok.clone();
        bad_alg.jws = format!("{fake_header}.{payload_part}.{sig_part}");
        assert!(matches!(
            verify_signed_policy_bundle(&bad_alg, &jwks, now),
            Err(JwsError::BadAlg(_))
        ));
    }

    #[test]
    fn fails_bundle_load_when_rule_examples_are_wrong() {
        let sk = SigningKey::generate(&mut OsRng);
        let now = Utc::now();
        let mut bundle = sample_bundle(now + Duration::hours(1));
        // Wrong `not_match` example actually matches `sar`!
        bundle.rules[0]
            .examples
            .not_match
            .push("sar -u 1 2".to_string());
        let (resp, jwks) = sign_policy_bundle_ed25519(&bundle, "kid-1", &sk).expect("sign");

        let err = verify_signed_policy_bundle(&resp, &jwks, now).expect_err("must fail examples");
        assert!(matches!(err, JwsError::RuleExampleFailed { .. }));
    }
}
