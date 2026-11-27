//! Command and tool approval policy engine, signed JWS bundle verification,
//! MT-1 diagnostic verb rule pack, and four-mode evaluator (`MT-7`, `MT-3`).

pub mod evaluator;
pub mod jws;
pub mod verbs;

pub use evaluator::{
    EvaluationDecision, GrantStore, LocalPolicyConfig, PolicyEvaluator, ToolCallContext,
    mcp_policy_key,
};
pub use jws::{
    JwsError, VerifiedPolicyBundle, sign_policy_bundle_ed25519, verify_signed_policy_bundle,
};
pub use verbs::{CommandParseError, VerbVerdict, classify_command_argv, parse_command_argv};
