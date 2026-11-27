//! HTTP and SSE client for BOSS control-plane endpoints (`MT-2`, `MT-7`, `MT-3`).
//!
//! Security invariants (`ARCHITECTURE.md` §6.2, §7.1):
//! - Refuses any redirect to a different host/scheme/port so auth headers can never leak.
//! - Never logs `Authorization` or `X-Boss-Api-Key` headers.
//! - Attaches `X-Metatron-Client` and `X-Boss-Contract` headers to every request.
//! - Handles `426 client_too_old`, `Idempotency-Key`, and `Last-Event-ID` SSE reconnect.

use std::fmt;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use futures_util::{Stream, StreamExt};
use metatron_protocol::{
    ApprovalReviewRequest, ApprovalReviewResponse, AuditBatchRequest, AuditBatchResponse,
    BOSS_CONTRACT_VERSION, BossErrorEnvelope, CapabilitiesResponse, ClientInfo,
    CredentialLeaseRequest, CredentialLeaseResponse, DeviceCodeRequest, DeviceCodeResponse,
    DeviceTokenError, DeviceTokenRequest, GrantCreateRequest, GrantRecord, JwksDocument,
    RefreshTokenRequest, RegisteredHost, RevokeTokenRequest, SignedPolicyResponse, SseEvent,
    SseFrame, TokenResponse, ToolResultSubmission, TurnRequest,
};
use reqwest::header::{ACCEPT, AUTHORIZATION, HeaderMap, HeaderName, HeaderValue, IF_NONE_MATCH};
use reqwest::{Method, StatusCode};
use serde::de::DeserializeOwned;
use serde_json::Value;
use thiserror::Error;
use url::Url;

const HEADER_CLIENT: &str = "x-metatron-client";
const HEADER_CONTRACT: &str = "x-boss-contract";
const HEADER_API_KEY: &str = "x-boss-api-key";
const HEADER_IDEMPOTENCY: &str = "idempotency-key";
const HEADER_LAST_EVENT_ID: &str = "last-event-id";

#[derive(Clone, PartialEq, Eq)]
pub enum AuthMethod {
    None,
    Bearer(String),
    ApiKey(String),
}

impl fmt::Debug for AuthMethod {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::None => write!(f, "AuthMethod::None"),
            Self::Bearer(_) => write!(f, "AuthMethod::Bearer([REDACTED])"),
            Self::ApiKey(_) => write!(f, "AuthMethod::ApiKey([REDACTED])"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct BossClientConfig {
    pub base_url: Url,
    pub auth: AuthMethod,
    pub timeout: Duration,
    pub stream_timeout: Duration,
    pub contract_version: u32,
}

impl BossClientConfig {
    pub fn new(base_url: &str, auth: AuthMethod) -> Result<Self, BossClientError> {
        let parsed =
            Url::parse(base_url).map_err(|e| BossClientError::InvalidUrl(e.to_string()))?;
        if parsed.scheme() != "http" && parsed.scheme() != "https" {
            return Err(BossClientError::InvalidUrl(format!(
                "unsupported scheme '{}'",
                parsed.scheme()
            )));
        }
        Ok(Self {
            base_url: parsed,
            auth,
            timeout: Duration::from_secs(30),
            stream_timeout: Duration::from_secs(900),
            contract_version: BOSS_CONTRACT_VERSION,
        })
    }
}

#[derive(Debug, Error)]
pub enum BossClientError {
    #[error("invalid BOSS URL: {0}")]
    InvalidUrl(String),
    #[error("cross-host redirect refused (origin {expected}, target {actual})")]
    CrossHostRedirect { expected: String, actual: String },
    #[error("client too old (minimum required: {min_client}): {message}")]
    ClientTooOld { min_client: String, message: String },
    #[error("authentication required (HTTP 401): {0}")]
    Unauthorized(String),
    #[error("permission or policy denied (HTTP 403): {0}")]
    Forbidden(String),
    #[error("budget exhausted or rate limited (HTTP 429): {0}")]
    BudgetExhausted(String),
    #[error("BOSS API HTTP {status} ({code}): {message}")]
    ApiError {
        status: u16,
        code: String,
        message: String,
        retryable: bool,
    },
    #[error("HTTP transport error: {0}")]
    Http(String),
    #[error("SSE stream parse error: {0}")]
    SseParse(String),
    #[error("invalid local API path: {0}")]
    InvalidLocalPath(String),
    #[error("JSON decode error: {0}")]
    Json(#[from] serde_json::Error),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DevicePollResult {
    Token(TokenResponse),
    Pending,
    SlowDown,
    Expired,
    AccessDenied,
}

#[derive(Debug, Clone, PartialEq)]
pub enum PolicyFetchResult {
    Updated {
        response: Box<SignedPolicyResponse>,
        etag: Option<String>,
    },
    NotModified,
}

/// Async stream of MT-10 SSE frames.
pub type SseEventStream = Pin<Box<dyn Stream<Item = Result<SseFrame, BossClientError>> + Send>>;

#[derive(Clone)]
pub struct BossClient {
    http: reqwest::Client,
    config: Arc<BossClientConfig>,
    client_header: String,
}

impl fmt::Debug for BossClient {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BossClient")
            .field("base_url", &self.config.base_url.as_str())
            .field("auth", &self.config.auth)
            .field("contract_version", &self.config.contract_version)
            .finish()
    }
}

impl BossClient {
    pub fn new(config: BossClientConfig) -> Result<Self, BossClientError> {
        let origin_for_policy = origin_key(&config.base_url);
        let redirect_policy = reqwest::redirect::Policy::custom(move |attempt| {
            let target_origin = origin_key(attempt.url());
            if target_origin != origin_for_policy {
                attempt.error(std::io::Error::new(
                    std::io::ErrorKind::PermissionDenied,
                    format!(
                        "cross-host redirect refused: expected {origin_for_policy}, got {target_origin}"
                    ),
                ))
            } else if attempt.previous().len() >= 3 {
                attempt.error(std::io::Error::other("too many redirects"))
            } else {
                attempt.follow()
            }
        });

        let http = reqwest::Client::builder()
            .redirect(redirect_policy)
            .connect_timeout(Duration::from_secs(10))
            .build()
            .map_err(|e| BossClientError::Http(e.to_string()))?;

        let client_header = ClientInfo::current().header_value();
        Ok(Self {
            http,
            config: Arc::new(config),
            client_header,
        })
    }

    pub fn base_url(&self) -> &Url {
        &self.config.base_url
    }

    pub fn with_auth(&self, auth: AuthMethod) -> Result<Self, BossClientError> {
        let mut next = (*self.config).clone();
        next.auth = auth;
        Self::new(next)
    }

    fn endpoint(&self, path: &str) -> Result<Url, BossClientError> {
        let base = self.config.base_url.as_str().trim_end_matches('/');
        let rel = if path.starts_with('/') {
            path.to_string()
        } else {
            format!("/{path}")
        };
        Url::parse(&format!("{base}{rel}")).map_err(|e| BossClientError::InvalidUrl(e.to_string()))
    }

    fn default_headers(&self) -> Result<HeaderMap, BossClientError> {
        let mut headers = HeaderMap::new();
        headers.insert(
            HeaderName::from_static(HEADER_CLIENT),
            HeaderValue::from_str(&self.client_header)
                .map_err(|e| BossClientError::Http(e.to_string()))?,
        );
        headers.insert(
            HeaderName::from_static(HEADER_CONTRACT),
            HeaderValue::from_str(&self.config.contract_version.to_string())
                .map_err(|e| BossClientError::Http(e.to_string()))?,
        );
        match &self.config.auth {
            AuthMethod::None => {}
            AuthMethod::Bearer(token) => {
                let value = HeaderValue::from_str(&format!("Bearer {token}")).map_err(|_| {
                    BossClientError::Http("invalid bearer token header".to_string())
                })?;
                headers.insert(AUTHORIZATION, value);
            }
            AuthMethod::ApiKey(key) => {
                let value = HeaderValue::from_str(key)
                    .map_err(|_| BossClientError::Http("invalid api key header".to_string()))?;
                headers.insert(HeaderName::from_static(HEADER_API_KEY), value);
            }
        }
        Ok(headers)
    }

    fn map_transport_err(&self, err: reqwest::Error) -> BossClientError {
        let mut cursor: Option<&(dyn std::error::Error + 'static)> = Some(&err);
        while let Some(src) = cursor {
            if src.to_string().contains("cross-host redirect refused") {
                let actual = err
                    .url()
                    .map(origin_key)
                    .unwrap_or_else(|| "unknown".to_string());
                return BossClientError::CrossHostRedirect {
                    expected: origin_key(&self.config.base_url),
                    actual,
                };
            }
            cursor = src.source();
        }
        BossClientError::Http(err.to_string())
    }

    async fn decode_error_response(status: StatusCode, body_text: &str) -> BossClientError {
        if let Ok(env) = serde_json::from_str::<BossErrorEnvelope>(body_text) {
            if status == StatusCode::UPGRADE_REQUIRED || env.error.code == "client_too_old" {
                return BossClientError::ClientTooOld {
                    min_client: env
                        .error
                        .min_client
                        .unwrap_or_else(|| "unknown".to_string()),
                    message: env.error.message,
                };
            }
            if status == StatusCode::UNAUTHORIZED {
                return BossClientError::Unauthorized(env.error.message);
            }
            if status == StatusCode::FORBIDDEN {
                return BossClientError::Forbidden(env.error.message);
            }
            if status == StatusCode::TOO_MANY_REQUESTS || env.error.code == "budget_exhausted" {
                return BossClientError::BudgetExhausted(env.error.message);
            }
            return BossClientError::ApiError {
                status: status.as_u16(),
                code: env.error.code,
                message: env.error.message,
                retryable: env.error.retryable,
            };
        }
        match status {
            StatusCode::UNAUTHORIZED => BossClientError::Unauthorized(body_text.to_string()),
            StatusCode::FORBIDDEN => BossClientError::Forbidden(body_text.to_string()),
            StatusCode::TOO_MANY_REQUESTS => {
                BossClientError::BudgetExhausted(body_text.to_string())
            }
            _ => BossClientError::ApiError {
                status: status.as_u16(),
                code: format!("http_{}", status.as_u16()),
                message: body_text.chars().take(400).collect(),
                retryable: status.is_server_error(),
            },
        }
    }

    async fn send_json<T: DeserializeOwned>(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
        extra_headers: Option<HeaderMap>,
    ) -> Result<T, BossClientError> {
        let url = self.endpoint(path)?;
        let mut headers = self.default_headers()?;
        if let Some(extra) = extra_headers {
            headers.extend(extra);
        }
        // Log method and path only — never headers or auth credentials.
        tracing::debug!(method = %method, path = %path, "boss_client request");
        let mut req = self
            .http
            .request(method, url)
            .headers(headers)
            .timeout(self.config.timeout);
        if let Some(payload) = body {
            req = req.json(payload);
        }
        let resp = req.send().await.map_err(|e| self.map_transport_err(e))?;
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| BossClientError::Http(e.to_string()))?;
        if !status.is_success() {
            return Err(Self::decode_error_response(status, &text).await);
        }
        serde_json::from_str(&text).map_err(BossClientError::from)
    }

    // --- Capabilities & Auth ---

    pub async fn capabilities(&self) -> Result<CapabilitiesResponse, BossClientError> {
        self.send_json(Method::GET, "/v1/metatron/capabilities", None, None)
            .await
    }

    pub async fn start_device_login(
        &self,
        scope: &str,
    ) -> Result<DeviceCodeResponse, BossClientError> {
        let payload = serde_json::to_value(DeviceCodeRequest {
            client_id: "metatron".to_string(),
            scope: scope.to_string(),
        })?;
        self.send_json(Method::POST, "/v1/auth/device/code", Some(&payload), None)
            .await
    }

    pub async fn poll_device_token(
        &self,
        device_code: &str,
    ) -> Result<DevicePollResult, BossClientError> {
        let url = self.endpoint("/v1/auth/device/token")?;
        let headers = self.default_headers()?;
        let body = DeviceTokenRequest {
            grant_type: "urn:ietf:params:oauth:grant-type:device_code".to_string(),
            device_code: device_code.to_string(),
        };
        let resp = self
            .http
            .post(url)
            .headers(headers)
            .timeout(self.config.timeout)
            .json(&body)
            .send()
            .await
            .map_err(|e| self.map_transport_err(e))?;
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| BossClientError::Http(e.to_string()))?;
        if status.is_success() {
            let tok: TokenResponse = serde_json::from_str(&text)?;
            return Ok(DevicePollResult::Token(tok));
        }
        if status == StatusCode::BAD_REQUEST
            && let Ok(err) = serde_json::from_str::<DeviceTokenError>(&text)
        {
            return match err.error.as_str() {
                "authorization_pending" => Ok(DevicePollResult::Pending),
                "slow_down" => Ok(DevicePollResult::SlowDown),
                "expired_token" => Ok(DevicePollResult::Expired),
                "access_denied" => Ok(DevicePollResult::AccessDenied),
                _ => Err(Self::decode_error_response(status, &text).await),
            };
        }
        Err(Self::decode_error_response(status, &text).await)
    }

    pub async fn refresh_token(
        &self,
        refresh_token: &str,
    ) -> Result<TokenResponse, BossClientError> {
        let payload = serde_json::to_value(RefreshTokenRequest {
            grant_type: "refresh_token".to_string(),
            refresh_token: refresh_token.to_string(),
        })?;
        self.send_json(Method::POST, "/v1/auth/token", Some(&payload), None)
            .await
    }

    pub async fn revoke_token(&self, token: &str) -> Result<(), BossClientError> {
        let url = self.endpoint("/v1/auth/revoke")?;
        let headers = self.default_headers()?;
        let body = RevokeTokenRequest {
            token: token.to_string(),
        };
        let resp = self
            .http
            .post(url)
            .headers(headers)
            .timeout(self.config.timeout)
            .json(&body)
            .send()
            .await
            .map_err(|e| self.map_transport_err(e))?;
        if resp.status().is_success() {
            Ok(())
        } else {
            let st = resp.status();
            let text = resp.text().await.unwrap_or_default();
            Err(Self::decode_error_response(st, &text).await)
        }
    }

    // --- Streaming Turns (SSE + Idempotency-Key + Last-Event-ID) ---

    pub async fn start_turn_stream(
        &self,
        req: &TurnRequest,
        idempotency_key: &str,
    ) -> Result<SseEventStream, BossClientError> {
        let url = self.endpoint("/v1/metatron/turns")?;
        let mut headers = self.default_headers()?;
        headers.insert(ACCEPT, HeaderValue::from_static("text/event-stream"));
        headers.insert(
            HeaderName::from_static(HEADER_IDEMPOTENCY),
            HeaderValue::from_str(idempotency_key)
                .map_err(|e| BossClientError::Http(e.to_string()))?,
        );
        tracing::debug!(path = "/v1/metatron/turns", "boss_client start_turn_stream");
        let resp = self
            .http
            .post(url)
            .headers(headers)
            .timeout(self.config.stream_timeout)
            .json(req)
            .send()
            .await
            .map_err(|e| self.map_transport_err(e))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(Self::decode_error_response(status, &text).await);
        }
        Ok(parse_sse_byte_stream(resp.bytes_stream()))
    }

    pub async fn resume_turn_stream(
        &self,
        turn_id: &str,
        last_event_id: Option<&str>,
    ) -> Result<SseEventStream, BossClientError> {
        let path = format!("/v1/metatron/turns/{turn_id}/events");
        let url = self.endpoint(&path)?;
        let mut headers = self.default_headers()?;
        headers.insert(ACCEPT, HeaderValue::from_static("text/event-stream"));
        if let Some(eid) = last_event_id {
            headers.insert(
                HeaderName::from_static(HEADER_LAST_EVENT_ID),
                HeaderValue::from_str(eid).map_err(|e| BossClientError::Http(e.to_string()))?,
            );
        }
        tracing::debug!(path = %path, "boss_client resume_turn_stream");
        let resp = self
            .http
            .get(url)
            .headers(headers)
            .timeout(self.config.stream_timeout)
            .send()
            .await
            .map_err(|e| self.map_transport_err(e))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(Self::decode_error_response(status, &text).await);
        }
        Ok(parse_sse_byte_stream(resp.bytes_stream()))
    }

    /// Drive a turn SSE stream, automatically reconnecting via `GET /events` with
    /// `Last-Event-ID` if the connection closes before a terminal event arrives.
    pub async fn collect_or_drive_turn<F, Fut>(
        &self,
        req: &TurnRequest,
        idempotency_key: &str,
        max_reconnects: usize,
        mut on_frame: F,
    ) -> Result<(), BossClientError>
    where
        F: FnMut(SseFrame) -> Fut,
        Fut: std::future::Future<Output = Result<(), BossClientError>>,
    {
        let mut stream = self.start_turn_stream(req, idempotency_key).await?;
        let mut turn_id: Option<String> = None;
        let mut last_event_id: Option<String> = None;
        let mut saw_terminal = false;
        let mut reconnects = 0usize;

        loop {
            let mut stream_err: Option<BossClientError> = None;
            while let Some(item) = stream.next().await {
                match item {
                    Ok(frame) => {
                        if let Some(ref id) = frame.id {
                            last_event_id = Some(id.clone());
                        }
                        if let SseEvent::TurnStarted {
                            turn_id: ref tid, ..
                        } = frame.event
                        {
                            turn_id = Some(tid.clone());
                        }
                        if frame.event.is_terminal() {
                            saw_terminal = true;
                        }
                        on_frame(frame).await?;
                        if saw_terminal {
                            return Ok(());
                        }
                    }
                    Err(e) => {
                        stream_err = Some(e);
                        break;
                    }
                }
            }

            if saw_terminal {
                return Ok(());
            }
            if reconnects >= max_reconnects {
                return Err(stream_err.unwrap_or_else(|| {
                    BossClientError::SseParse("turn stream ended before terminal event".to_string())
                }));
            }
            let Some(ref tid) = turn_id else {
                return Err(stream_err.unwrap_or_else(|| {
                    BossClientError::SseParse("turn stream ended before turn.started".to_string())
                }));
            };
            reconnects += 1;
            tokio::time::sleep(Duration::from_millis(50 * reconnects as u64)).await;
            stream = self
                .resume_turn_stream(tid, last_event_id.as_deref())
                .await?;
        }
    }

    pub async fn submit_tool_result(
        &self,
        turn_id: &str,
        submission: &ToolResultSubmission,
    ) -> Result<(), BossClientError> {
        let path = format!("/v1/metatron/turns/{turn_id}/tool_results");
        let url = self.endpoint(&path)?;
        let mut headers = self.default_headers()?;
        let idem = format!("{turn_id}:{}", submission.call_id);
        headers.insert(
            HeaderName::from_static(HEADER_IDEMPOTENCY),
            HeaderValue::from_str(&idem).map_err(|e| BossClientError::Http(e.to_string()))?,
        );
        let resp = self
            .http
            .post(url)
            .headers(headers)
            .timeout(self.config.timeout)
            .json(submission)
            .send()
            .await
            .map_err(|e| self.map_transport_err(e))?;
        if resp.status().is_success() {
            Ok(())
        } else {
            let st = resp.status();
            let text = resp.text().await.unwrap_or_default();
            Err(Self::decode_error_response(st, &text).await)
        }
    }

    pub async fn cancel_turn(&self, turn_id: &str) -> Result<(), BossClientError> {
        let path = format!("/v1/metatron/turns/{turn_id}/cancel");
        let url = self.endpoint(&path)?;
        let headers = self.default_headers()?;
        let resp = self
            .http
            .post(url)
            .headers(headers)
            .timeout(self.config.timeout)
            .send()
            .await
            .map_err(|e| self.map_transport_err(e))?;
        if resp.status().is_success() {
            Ok(())
        } else {
            let st = resp.status();
            let text = resp.text().await.unwrap_or_default();
            Err(Self::decode_error_response(st, &text).await)
        }
    }

    // --- Policy, Grants, Reviewer ---

    pub async fn fetch_policy(
        &self,
        project_id: &str,
        if_none_match: Option<&str>,
    ) -> Result<PolicyFetchResult, BossClientError> {
        let path = format!("/v1/metatron/policy?project_id={}", url_encode(project_id));
        let url = self.endpoint(&path)?;
        let mut headers = self.default_headers()?;
        if let Some(etag) = if_none_match {
            headers.insert(
                IF_NONE_MATCH,
                HeaderValue::from_str(etag).map_err(|e| BossClientError::Http(e.to_string()))?,
            );
        }
        let resp = self
            .http
            .get(url)
            .headers(headers)
            .timeout(self.config.timeout)
            .send()
            .await
            .map_err(|e| self.map_transport_err(e))?;
        if resp.status() == StatusCode::NOT_MODIFIED {
            return Ok(PolicyFetchResult::NotModified);
        }
        let etag = resp
            .headers()
            .get("etag")
            .and_then(|v| v.to_str().ok())
            .map(ToString::to_string);
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| BossClientError::Http(e.to_string()))?;
        if !status.is_success() {
            return Err(Self::decode_error_response(status, &text).await);
        }
        let response: SignedPolicyResponse = serde_json::from_str(&text)?;
        Ok(PolicyFetchResult::Updated {
            response: Box::new(response),
            etag,
        })
    }

    pub async fn fetch_policy_keys(&self) -> Result<JwksDocument, BossClientError> {
        self.send_json(Method::GET, "/v1/metatron/policy/keys", None, None)
            .await
    }

    pub async fn create_grant(
        &self,
        req: &GrantCreateRequest,
    ) -> Result<GrantRecord, BossClientError> {
        let payload = serde_json::to_value(req)?;
        self.send_json(Method::POST, "/v1/metatron/grants", Some(&payload), None)
            .await
    }

    pub async fn revoke_grant(&self, grant_id: &str) -> Result<(), BossClientError> {
        let path = format!("/v1/metatron/grants/{grant_id}");
        let url = self.endpoint(&path)?;
        let headers = self.default_headers()?;
        let resp = self
            .http
            .delete(url)
            .headers(headers)
            .timeout(self.config.timeout)
            .send()
            .await
            .map_err(|e| self.map_transport_err(e))?;
        if resp.status().is_success() {
            Ok(())
        } else {
            let st = resp.status();
            let text = resp.text().await.unwrap_or_default();
            Err(Self::decode_error_response(st, &text).await)
        }
    }

    pub async fn review_approval(
        &self,
        req: &ApprovalReviewRequest,
    ) -> Result<ApprovalReviewResponse, BossClientError> {
        let payload = serde_json::to_value(req)?;
        self.send_json(
            Method::POST,
            "/v1/metatron/approvals/review",
            Some(&payload),
            None,
        )
        .await
    }

    // --- Audit & Hosts / Credential Leases ---

    pub async fn upload_audit_batch(
        &self,
        req: &AuditBatchRequest,
    ) -> Result<AuditBatchResponse, BossClientError> {
        let payload = serde_json::to_value(req)?;
        self.send_json(Method::POST, "/v1/metatron/audit", Some(&payload), None)
            .await
    }

    pub async fn list_hosts(
        &self,
        project_id: &str,
    ) -> Result<Vec<RegisteredHost>, BossClientError> {
        let path = format!("/v1/metatron/hosts?project_id={}", url_encode(project_id));
        let val: Value = self.send_json(Method::GET, &path, None, None).await?;
        if let Some(arr) = val.get("hosts") {
            Ok(serde_json::from_value(arr.clone())?)
        } else if val.is_array() {
            Ok(serde_json::from_value(val)?)
        } else {
            Ok(Vec::new())
        }
    }

    pub async fn lease_host_credential(
        &self,
        host_id: &str,
        req: &CredentialLeaseRequest,
    ) -> Result<CredentialLeaseResponse, BossClientError> {
        let path = format!("/v1/metatron/hosts/{host_id}/credential-lease");
        let payload = serde_json::to_value(req)?;
        self.send_json(Method::POST, &path, Some(&payload), None)
            .await
    }

    // --- Generic `/v1/` and OpenAPI passthrough for TUI parity (`/op`, `/api`, `/pick`) ---

    pub async fn raw_v1_request(
        &self,
        method: Method,
        path: &str,
        body: Option<&Value>,
    ) -> Result<Value, BossClientError> {
        validate_local_v1_path(path)?;
        self.send_json(method, path, body, None).await
    }

    pub async fn fetch_openapi(&self) -> Result<Value, BossClientError> {
        self.send_json(Method::GET, "/openapi.json", None, None)
            .await
    }
}

/// Validates that a `/api` route targets a clean local `/v1/` path without traversal.
pub fn validate_local_v1_path(path: &str) -> Result<(), BossClientError> {
    let path_only = path.split('?').next().unwrap_or(path);
    if !path_only.starts_with("/v1/") {
        return Err(BossClientError::InvalidLocalPath(
            "path must start with /v1/".to_string(),
        ));
    }
    if path_only.contains("//") || path_only.split('/').any(|seg| seg == "..") {
        return Err(BossClientError::InvalidLocalPath(
            "path traversal is not allowed".to_string(),
        ));
    }
    Ok(())
}

fn origin_key(url: &Url) -> String {
    format!(
        "{}://{}:{}",
        url.scheme(),
        url.host_str().unwrap_or(""),
        url.port_or_known_default().unwrap_or(0)
    )
}

fn url_encode(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for b in input.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Incremental SSE parser for MT-10 frames (`id:`, `event:`, `data:`, `: comment`).
pub fn parse_sse_frames(buffer: &mut String) -> Vec<Result<SseFrame, BossClientError>> {
    let mut out = Vec::new();
    let normalized = buffer.replace("\r\n", "\n");
    *buffer = normalized;

    while let Some(idx) = buffer.find("\n\n") {
        let block = buffer[..idx].to_string();
        buffer.drain(..idx + 2);
        if let Some(parsed) = parse_single_sse_block(&block) {
            out.push(parsed);
        }
    }
    out
}

fn parse_single_sse_block(block: &str) -> Option<Result<SseFrame, BossClientError>> {
    let mut id: Option<String> = None;
    let mut event_name: Option<String> = None;
    let mut data_lines: Vec<String> = Vec::new();

    for line in block.lines() {
        if line.is_empty() || line.starts_with(':') {
            // Keepalive comments (`: ping`) and empty lines are ignored.
            continue;
        }
        if let Some(rest) = line.strip_prefix("id:") {
            id = Some(rest.trim().to_string());
        } else if let Some(rest) = line.strip_prefix("event:") {
            event_name = Some(rest.trim().to_string());
        } else if let Some(rest) = line.strip_prefix("data:") {
            data_lines.push(rest.trim_start().to_string());
        }
    }

    if data_lines.is_empty() && event_name.is_none() {
        return None;
    }
    let name = event_name.unwrap_or_else(|| "message".to_string());
    let data = data_lines.join("\n");
    match SseEvent::from_sse(&name, &data) {
        Ok(event) => Some(Ok(SseFrame { id, event })),
        Err(e) => Some(Err(BossClientError::SseParse(format!(
            "event '{name}': {e}"
        )))),
    }
}

fn parse_sse_byte_stream<S>(byte_stream: S) -> SseEventStream
where
    S: Stream<Item = Result<bytes::Bytes, reqwest::Error>> + Send + Unpin + 'static,
{
    let stream = futures_util::stream::unfold(
        (
            byte_stream,
            String::new(),
            std::collections::VecDeque::new(),
            false,
        ),
        |(mut bs, mut buf, mut pending, mut done)| async move {
            loop {
                if let Some(item) = pending.pop_front() {
                    return Some((item, (bs, buf, pending, done)));
                }
                if done {
                    return None;
                }
                match bs.next().await {
                    Some(Ok(chunk)) => {
                        buf.push_str(&String::from_utf8_lossy(&chunk));
                        for frame in parse_sse_frames(&mut buf) {
                            pending.push_back(frame);
                        }
                    }
                    Some(Err(e)) => {
                        done = true;
                        return Some((
                            Err(BossClientError::Http(e.to_string())),
                            (bs, buf, pending, done),
                        ));
                    }
                    None => {
                        done = true;
                        if !buf.trim().is_empty() {
                            buf.push_str("\n\n");
                            for frame in parse_sse_frames(&mut buf) {
                                pending.push_back(frame);
                            }
                            if let Some(item) = pending.pop_front() {
                                return Some((item, (bs, buf, pending, done)));
                            }
                        }
                        return None;
                    }
                }
            }
        },
    );
    Box::pin(stream)
}

#[cfg(test)]
mod tests {
    use super::*;
    use metatron_protocol::{ApprovalMode, TurnContext};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    async fn spawn_fake_server<F, Fut>(handler: F) -> String
    where
        F: Fn(String) -> Fut + Send + Sync + 'static,
        Fut: std::future::Future<Output = String> + Send,
    {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let addr = listener.local_addr().expect("addr");
        let handler = Arc::new(handler);
        tokio::spawn(async move {
            loop {
                let Ok((mut sock, _)) = listener.accept().await else {
                    break;
                };
                let h = Arc::clone(&handler);
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 16384];
                    let n = sock.read(&mut buf).await.unwrap_or(0);
                    let req = String::from_utf8_lossy(&buf[..n]).to_string();
                    let resp = h(req).await;
                    let _ = sock.write_all(resp.as_bytes()).await;
                    let _ = sock.shutdown().await;
                });
            }
        });
        format!("http://{addr}")
    }

    #[tokio::test]
    async fn refuses_cross_host_redirect_so_auth_cannot_leak() {
        let base = spawn_fake_server(|_req| async {
            "HTTP/1.1 302 Found\r\nLocation: http://evil.example.invalid/steal\r\nContent-Length: 0\r\n\r\n"
                .to_string()
        })
        .await;

        let client = BossClient::new(
            BossClientConfig::new(&base, AuthMethod::Bearer("boss_pat_secret_abc".to_string()))
                .expect("cfg"),
        )
        .expect("client");

        let err = client
            .capabilities()
            .await
            .expect_err("must refuse redirect");
        assert!(
            matches!(err, BossClientError::CrossHostRedirect { .. }),
            "expected CrossHostRedirect, got {err:?}"
        );
    }

    #[tokio::test]
    async fn handles_426_client_too_old_and_sends_contract_headers() {
        let base = spawn_fake_server(|req| async move {
            assert!(req.to_ascii_lowercase().contains("x-metatron-client: metatron/"));
            assert!(req.to_ascii_lowercase().contains("x-boss-contract: 1"));
            let body = r#"{"error":{"code":"client_too_old","message":"upgrade required","retryable":false,"min_client":"0.9.0"}}"#;
            format!(
                "HTTP/1.1 426 Upgrade Required\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                body.len(),
                body
            )
        })
        .await;

        let client = BossClient::new(
            BossClientConfig::new(&base, AuthMethod::ApiKey("boss_pat_test_12345".to_string()))
                .expect("cfg"),
        )
        .expect("client");

        let err = client.capabilities().await.expect_err("expected 426");
        assert!(matches!(
            err,
            BossClientError::ClientTooOld { min_client, .. } if min_client == "0.9.0"
        ));
    }

    #[tokio::test]
    async fn sse_reconnects_with_last_event_id_on_midstream_drop() {
        let hits = Arc::new(AtomicUsize::new(0));
        let hits_clone = Arc::clone(&hits);

        let base = spawn_fake_server(move |req| {
            let h = Arc::clone(&hits_clone);
            async move {
                let n = h.fetch_add(1, Ordering::SeqCst);
                if n == 0 {
                    assert!(req.starts_with("POST /v1/metatron/turns "));
                    assert!(req.to_ascii_lowercase().contains("idempotency-key: idem-1"));
                    // Drop stream right after text.delta #2 (before turn.completed!)
                    let body = concat!(
                        "id: mturn_r1:1\nevent: turn.started\ndata: {\"turn_id\":\"mturn_r1\",\"thread_id\":\"athr_1\",\"model\":\"gpt-4o\",\"mode\":\"ask\",\"tools\":[],\"dropped\":[]}\n\n",
                        ": ping\n\n",
                        "id: mturn_r1:2\nevent: text.delta\ndata: {\"text\":\"Hello \"}\n\n"
                    );
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    )
                } else {
                    assert!(req.starts_with("GET /v1/metatron/turns/mturn_r1/events "));
                    assert!(
                        req.to_ascii_lowercase()
                            .contains("last-event-id: mturn_r1:2"),
                        "missing Last-Event-ID in reconnect request: {req}"
                    );
                    let body = concat!(
                        "id: mturn_r1:3\nevent: text.delta\ndata: {\"text\":\"BOSS!\"}\n\n",
                        "id: mturn_r1:4\nevent: turn.completed\ndata: {\"turn_id\":\"mturn_r1\",\"stop_reason\":\"end_turn\"}\n\n"
                    );
                    format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\r\n{}",
                        body.len(),
                        body
                    )
                }
            }
        })
        .await;

        let client = BossClient::new(
            BossClientConfig::new(&base, AuthMethod::Bearer("tok".to_string())).expect("cfg"),
        )
        .expect("client");

        let req = TurnRequest {
            project_id: "boss-dogfood".to_string(),
            thread_id: None,
            agent: None,
            message: "hello".to_string(),
            mode: ApprovalMode::Ask,
            client: ClientInfo::current(),
            context: TurnContext::default(),
            client_tools: Vec::new(),
        };

        let mut text = String::new();
        let mut completed = false;
        client
            .collect_or_drive_turn(&req, "idem-1", 2, |frame| {
                if let SseEvent::TextDelta { text: delta } = frame.event {
                    text.push_str(&delta);
                } else if matches!(frame.event, SseEvent::TurnCompleted { .. }) {
                    completed = true;
                }
                async { Ok(()) }
            })
            .await
            .expect("drive turn");

        assert_eq!(text, "Hello BOSS!");
        assert!(completed);
        assert_eq!(hits.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn debug_never_prints_auth_headers_or_tokens() {
        let client = BossClient::new(
            BossClientConfig::new(
                "https://boss.local",
                AuthMethod::Bearer("boss_pat_super_secret_token".to_string()),
            )
            .expect("cfg"),
        )
        .expect("client");
        let dbg = format!("{client:?}");
        assert!(!dbg.contains("boss_pat_super_secret_token"));
        assert!(dbg.contains("[REDACTED]"));
    }

    #[test]
    fn validates_local_v1_paths() {
        assert!(validate_local_v1_path("/v1/tasks?project_id=p1").is_ok());
        assert!(validate_local_v1_path("/v2/tasks").is_err());
        assert!(validate_local_v1_path("/v1/../admin").is_err());
        assert!(validate_local_v1_path("/v1//tasks").is_err());
    }
}
