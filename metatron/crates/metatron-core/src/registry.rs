//! Ordered `ToolRegistry`, `Direct`/`Deferred` exposure, `tool_search`, MCP tool proxy,
//! and static/runtime zero-code-edit invariant scanner (`MT-9`, `MT-5`).

use std::collections::{BTreeMap, HashSet};
use std::sync::{Arc, Mutex, PoisonError};

use async_trait::async_trait;
use metatron_mcp::{ApprovedMcpTool, McpClient};
use metatron_policy::PolicyEvaluator;
use metatron_protocol::{ClientToolSpec, RiskClass, ToolExposure, ToolResultStatus};
use serde_json::{Value, json};

use crate::orchestrator::{Tool, ToolCtx, ToolOutcome};

/// Forbidden code-editing / patching tool name patterns (`MT-5`: Metatron NEVER edits code locally).
const FORBIDDEN_EDIT_PATTERNS: &[&str] = &[
    "apply_patch",
    "patch_file",
    "edit_file",
    "write_file",
    "replace_in_file",
    "modify_file",
    "delete_file",
    "create_source",
    "str_replace",
    "code_edit",
    "write_source",
];

/// Ordered registry of Metatron tools supporting `Direct` and `Deferred` exposure (`MT-9`).
#[derive(Default)]
pub struct ToolRegistry {
    tools: BTreeMap<String, Arc<dyn Tool>>,
    activated_deferred: Mutex<HashSet<String>>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&mut self, tool: Arc<dyn Tool>) {
        self.tools.insert(tool.name().to_string(), tool);
    }

    /// Register all tools discovered from an approved MCP server as `McpProxyTool`s.
    pub fn register_mcp_tools(
        &mut self,
        client: Arc<McpClient>,
        approved_tools: Vec<ApprovedMcpTool>,
    ) {
        for spec in approved_tools {
            let proxy: Arc<dyn Tool> = Arc::new(McpProxyTool {
                client: Arc::clone(&client),
                tool: spec,
            });
            self.register(proxy);
        }
    }

    pub fn get(&self, name: &str) -> Option<Arc<dyn Tool>> {
        self.tools.get(name).cloned()
    }

    pub fn all_specs(&self) -> Vec<ClientToolSpec> {
        self.tools.values().map(|t| t.spec()).collect()
    }

    pub fn all_names(&self) -> Vec<String> {
        self.tools.keys().cloned().collect()
    }

    /// Return the per-step tool list visible to the model:
    /// - All `Direct` tools permitted by `evaluator`.
    /// - Any `Deferred` tool previously activated via `tool_search` and permitted by `evaluator`.
    pub fn visible_specs(&self, evaluator: &PolicyEvaluator) -> Vec<ClientToolSpec> {
        let activated = self
            .activated_deferred
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone();
        let candidates: Vec<ClientToolSpec> = self
            .tools
            .values()
            .filter(|t| t.exposure() == ToolExposure::Direct || activated.contains(t.name()))
            .map(|t| t.spec())
            .collect();
        evaluator.filter_tools(&candidates)
    }

    /// Search deferred (and direct) tools by keyword query, activating matching deferred tools
    /// so they appear in subsequent steps (`MT-9`).
    pub fn search_and_activate(
        &self,
        query: &str,
        evaluator: &PolicyEvaluator,
        limit: usize,
    ) -> Vec<ClientToolSpec> {
        let q = query.trim().to_ascii_lowercase();
        let tokens: Vec<&str> = q.split_whitespace().collect();
        let mut matches: Vec<ClientToolSpec> = Vec::new();

        for tool in self.tools.values() {
            let spec = tool.spec();
            if !evaluator.is_tool_allowed(&spec.name) {
                continue;
            }
            let hay = format!(
                "{} {}",
                spec.name.to_ascii_lowercase(),
                spec.description.to_ascii_lowercase()
            );
            let is_match = if tokens.is_empty() {
                tool.exposure() == ToolExposure::Deferred
            } else {
                tokens.iter().any(|tok| hay.contains(tok))
            };
            if is_match {
                if tool.exposure() == ToolExposure::Deferred {
                    let mut guard = self
                        .activated_deferred
                        .lock()
                        .unwrap_or_else(PoisonError::into_inner);
                    guard.insert(spec.name.clone());
                }
                matches.push(spec);
                if matches.len() >= limit.max(1) {
                    break;
                }
            }
        }
        matches
    }

    /// Scan every registered tool name, description, and input schema for prohibited
    /// local code-editing / patching capabilities (`MT-5` hard invariant).
    /// Returns a list of violations (empty means invariant holds).
    pub fn scan_for_forbidden_code_edit_tools(&self) -> Vec<String> {
        let mut violations = Vec::new();
        for (name, tool) in &self.tools {
            let spec = tool.spec();
            let lower_name = name.to_ascii_lowercase();
            let lower_desc = spec.description.to_ascii_lowercase();
            for pat in FORBIDDEN_EDIT_PATTERNS {
                if lower_name.contains(pat) {
                    violations.push(format!(
                        "tool '{name}' matches forbidden edit pattern '{pat}'"
                    ));
                }
            }
            if lower_desc.contains("apply a patch")
                || lower_desc.contains("edit source file")
                || lower_desc.contains("modify local source")
                || lower_desc.contains("write file outside scratch")
            {
                violations.push(format!(
                    "tool '{name}' description advertises prohibited local file editing"
                ));
            }
            if let Some(props) = spec
                .input_schema
                .get("properties")
                .and_then(Value::as_object)
                && (props.contains_key("patch")
                    || props.contains_key("unified_diff")
                    || (props.contains_key("file_path") && props.contains_key("new_content")))
            {
                violations.push(format!(
                    "tool '{name}' schema exposes prohibited patch/file-edit parameters"
                ));
            }
        }
        violations
    }
}

/// Bridges an admin-approved BOSS MCP server tool into Metatron's `Tool` trait (`MT-9`).
pub struct McpProxyTool {
    client: Arc<McpClient>,
    tool: ApprovedMcpTool,
}

#[async_trait]
impl Tool for McpProxyTool {
    fn name(&self) -> &str {
        &self.tool.namespaced_name
    }

    fn spec(&self) -> ClientToolSpec {
        self.tool.to_client_spec()
    }

    fn exposure(&self) -> ToolExposure {
        self.tool.exposure
    }

    fn risk(&self, _args: &Value) -> RiskClass {
        self.tool.risk_class
    }

    fn parallel_ok(&self) -> bool {
        matches!(
            self.tool.risk_class,
            RiskClass::ReadLocal | RiskClass::Network
        )
    }

    async fn run(&self, _ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let started = std::time::Instant::now();
        match self
            .client
            .call_tool(&self.tool.server_name, &self.tool.tool_name, args)
            .await
        {
            Ok(out) => {
                let bytes = out.text.len();
                ToolOutcome {
                    status: if out.is_error {
                        ToolResultStatus::Error
                    } else {
                        ToolResultStatus::Ok
                    },
                    inline_text: out.text.clone(),
                    full_text: out.text,
                    truncated: false,
                    exit_code: Some(if out.is_error { 1 } else { 0 }),
                    bytes,
                    duration_ms: started.elapsed().as_millis() as u64,
                    host: None,
                    host_origin: Some(format!("mcp:{}", self.tool.server_name)),
                    sandbox_denied: false,
                    images: Vec::new(),
                    http_audit: None,
                }
            }
            Err(e) => {
                let msg = e.to_string();
                ToolOutcome {
                    status: ToolResultStatus::Error,
                    inline_text: msg.clone(),
                    full_text: msg,
                    truncated: false,
                    exit_code: Some(1),
                    bytes: 0,
                    duration_ms: started.elapsed().as_millis() as u64,
                    host: None,
                    host_origin: Some(format!("mcp:{}", self.tool.server_name)),
                    sandbox_denied: false,
                    images: Vec::new(),
                    http_audit: None,
                }
            }
        }
    }
}

/// Built-in `tool_search` tool for lazy discovery of `Deferred` tools (`MT-9`).
pub struct ToolSearchTool {
    registry: Arc<ToolRegistry>,
    evaluator: Arc<PolicyEvaluator>,
}

impl ToolSearchTool {
    pub fn new(registry: Arc<ToolRegistry>, evaluator: Arc<PolicyEvaluator>) -> Self {
        Self {
            registry,
            evaluator,
        }
    }
}

#[async_trait]
impl Tool for ToolSearchTool {
    fn name(&self) -> &str {
        "tool_search"
    }

    fn spec(&self) -> ClientToolSpec {
        ClientToolSpec {
            name: "tool_search".to_string(),
            description: "Search and activate deferred tools (e.g. large MCP servers) by keyword."
                .to_string(),
            input_schema: json!({
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": { "type": "string", "description": "Keyword(s) matching tool name or description" },
                    "limit": { "type": "integer", "minimum": 1, "maximum": 25 }
                }
            }),
            risk_class: "read-local".to_string(),
            exposure: ToolExposure::Direct,
        }
    }

    fn risk(&self, _args: &Value) -> RiskClass {
        RiskClass::ReadLocal
    }

    fn parallel_ok(&self) -> bool {
        true
    }

    async fn run(&self, _ctx: &ToolCtx, args: Value) -> ToolOutcome {
        let query = args.get("query").and_then(Value::as_str).unwrap_or("");
        let limit = args
            .get("limit")
            .and_then(Value::as_u64)
            .unwrap_or(10)
            .clamp(1, 25) as usize;
        let found = self
            .registry
            .search_and_activate(query, &self.evaluator, limit);
        let text = serde_json::to_string_pretty(&json!({
            "query": query,
            "activated": found.iter().map(|s| json!({
                "name": s.name,
                "description": s.description,
                "risk_class": s.risk_class,
                "exposure": s.exposure
            })).collect::<Vec<_>>()
        }))
        .unwrap_or_else(|_| "{}".to_string());
        let bytes = text.len();
        ToolOutcome {
            status: ToolResultStatus::Ok,
            inline_text: text.clone(),
            full_text: text,
            truncated: false,
            exit_code: Some(0),
            bytes,
            duration_ms: 0,
            host: None,
            host_origin: None,
            sandbox_denied: false,
            images: Vec::new(),
            http_audit: None,
        }
    }
}
