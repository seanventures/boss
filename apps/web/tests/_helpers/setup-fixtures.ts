/**
 * Repo and models views, as GET /v1/setup returns them, for the board's render tests. The pills
 * are written the way the server's catalogue (engine/boss_engine/services/setup_pills.py) says
 * them, so a test that compares rendered text to these compares it to the server's words.
 */
import type { Pill, SetupView } from "../../lib/setup-types";

const now = new Date("2026-10-04T12:00:00Z").toISOString();

export const PILLS = {
  working: { state: "working", tone: "green", label: "Working",
             tip: "Can read, write a branch and open a pull request." },
  cantWrite: { state: "cant_write", tone: "red", label: "Can't write",
               tip: "Token needs Contents: Read and write.", action: "fix_link",
               fix_url: "https://github.com/settings/personal-access-tokens" },
  modelWorking: { state: "working", tone: "green", label: "Working",
                  tip: "Answered a test question 5m ago." },
  keyRejected: { state: "key_rejected", tone: "red", label: "Key rejected",
                 tip: "The provider refused this key.", action: "replace" },
  limited: { state: "rate_limited", tone: "amber", label: "Limited · 14:20",
             tip: "Plan limit reached. Auto uses another model until 14:20." },
  budgetOk: { state: "ok", tone: "green", label: "OK", tip: "$3.20 of $200. Metered work runs." },
  budgetSpent: { state: "spent", tone: "red", label: "Spent",
                 tip: "Cap reached. Metered models paused until 2026-11-01." },
} satisfies Record<string, Pill>;

const CATALOGUE: SetupView["catalogue"] = [
  { provider: "claude_code", label: "Claude Code", kind: "subscription", fields: ["token"],
    command: "claude setup-token" },
  { provider: "codex", label: "Codex", kind: "subscription", fields: ["file"],
    command: "~/.codex/auth.json" },
  { provider: "grok", label: "Grok Build", kind: "subscription", fields: [] },
  { provider: "cursor", label: "Cursor", kind: "subscription", fields: [] },
  { provider: "openrouter", label: "OpenRouter", kind: "api_key", fields: ["api_key", "model"],
    key_url: "https://openrouter.ai/keys", example_model: "anthropic/claude-sonnet-4.5" },
  { provider: "openai", label: "OpenAI", kind: "api_key", fields: ["api_key", "model"],
    key_url: "https://platform.openai.com/api-keys", example_model: "gpt-4o" },
  { provider: "anthropic", label: "Anthropic", kind: "api_key", fields: ["api_key", "model"] },
  { provider: "gemini", label: "Gemini", kind: "api_key", fields: ["api_key", "model"] },
  { provider: "xai", label: "xAI", kind: "api_key", fields: ["api_key", "model"] },
  { provider: "custom", label: "Other", kind: "api_key", fields: ["base_url", "api_key", "model"] },
  { provider: "ollama", label: "Ollama", kind: "local", fields: ["base_url", "model"] },
  { provider: "vllm", label: "Other URL", kind: "local", fields: ["base_url", "model"] },
];

const DETECTED: SetupView["detected"] = [
  { provider: "claude_code", label: "Claude Code", kind: "subscription", found: "needs_token" },
  { provider: "codex", label: "Codex", kind: "subscription", found: "needs_file" },
  { provider: "grok", label: "Grok Build", kind: "subscription", found: "not_on_server" },
  { provider: "cursor", label: "Cursor", kind: "subscription", found: "not_on_server" },
  { provider: "ollama", label: "Ollama", kind: "local", found: "found",
    base_url: "http://127.0.0.1:11434/v1" },
  { provider: "vllm", label: "Other URL", kind: "local", found: "not_on_server" },
];

const JOBS_EMPTY: SetupView["jobs"] = [
  { job: "build", label: "Build", more: false, choice: null, resolved: null, metered: false, options: [] },
  { job: "review", label: "Review", more: false, choice: null, resolved: null, metered: false, options: [] },
  { job: "chat", label: "Chat", more: false, choice: null, resolved: null, metered: false, options: [] },
  { job: "decisions", label: "Decisions", more: false, choice: null, resolved: null, metered: false,
    options: [{ engine: "typesafe", model: null, label: "TypeSafe Jev", metered: true, needs_key: true }] },
  { job: "hard", label: "Hard problems", more: true, choice: null, resolved: null, metered: false, options: [] },
  { job: "routine", label: "Routine work", more: true, choice: null, resolved: null, metered: false, options: [] },
];

const BUDGET = { period: "month" as const, spent_usd: 3.2, cap_usd: 200, state: "ok" as const,
                 resets_at: "2026-11-01T00:00:00+00:00", pill: PILLS.budgetOk, monthly_cap_usd: 200 };

export function firstRun(): SetupView {
  return {
    project_id: "p", ready: false, next: "repository",
    verdict: { label: "2 to go", tone: "grey", tip: "Connect a repository and a model to start." },
    problems: 0, secrets: { backend: "database", needs_consent: false },
    repository: { connection: null, repos: [] }, models: [], detected: DETECTED,
    catalogue: CATALOGUE, jobs: JOBS_EMPTY, budget: BUDGET,
    try: { workflow_key: "setup_first_pr", last_run: null, done: false },
  };
}

const REPO = {
  binding_id: "ghb1", full_name: "owner/app", default: true, private: true,
  default_branch: "main",
  checks: { read: { ok: true, state: "working", at: now }, write: { ok: true, state: "working", at: now },
            pull_requests: { ok: true, state: "working", at: now } },
  pill: PILLS.working, checked_at: now, stale: false,
};

const CONNECTION = { id: "ghc1", auth: "token" as const, account_login: "param",
                     expires_at: "2027-01-02T10:00:00+00:00", source: "connected" as const };

export function repoDone(): SetupView {
  return {
    ...firstRun(), next: "model",
    verdict: { label: "1 to go", tone: "grey", tip: "Connect a repository and a model to start." },
    repository: { connection: CONNECTION, repos: [REPO] },
  };
}

const MODELS: SetupView["models"] = [
  { source_id: "anthropic_claude_code", provider: "claude_code", label: "Claude Code",
    kind: "subscription", model: null, used_for: ["Build", "Chat", "Decisions"],
    pill: PILLS.modelWorking, checked_at: now, source: "connected", retry_at: null, stale: false },
  { source_id: "setup-openrouter", provider: "openrouter", label: "OpenRouter", kind: "metered",
    model: "openai/gpt-5", used_for: ["Review"], pill: PILLS.modelWorking, checked_at: now,
    source: "connected", retry_at: null, stale: false },
];

const JOBS: SetupView["jobs"] = JOBS_EMPTY.map((job) => ({
  ...job,
  resolved: job.job === "review"
    ? { engine: "openrouter", model: "openai/gpt-5", label: "OpenRouter · openai/gpt-5", why: "chosen for this project" }
    : { engine: "claude_code", model: null, label: "Auto · Claude Code", why: "auto" },
  choice: job.job === "review" ? { engine: "openrouter", model: "openai/gpt-5" } : null,
  metered: job.job === "review",
  options: job.job === "decisions" ? job.options : [
    { engine: "claude_code", model: "sonnet", label: "Claude Code · sonnet", metered: false },
    { engine: "openrouter", model: "openai/gpt-5", label: "OpenRouter · openai/gpt-5", metered: true },
  ],
}));

export function tryStep(): SetupView {
  return {
    ...repoDone(), ready: true, next: "try",
    verdict: { label: "Ready", tone: "green", tip: "Repository and model checked. Ready to build." },
    models: MODELS, jobs: JOBS,
  };
}

export function board(): SetupView {
  return {
    ...tryStep(), next: null,
    try: { workflow_key: "setup_first_pr", done: true, last_run: {
      run_id: "wfr1", status: "completed", pr_url: "https://github.com/owner/app/pull/12",
      pr_number: 12, pill: { state: "pr_open", tone: "green", label: "PR #12 ↗",
                             tip: "Your first PR is open." } } },
  };
}

export function everyRed(): SetupView {
  const view = board();
  return {
    ...view, ready: false, problems: 2,
    verdict: { label: "2 problems", tone: "red", tip: "Token needs Contents: Read and write." },
    repository: { connection: CONNECTION, repos: [{ ...REPO, pill: PILLS.cantWrite,
      checks: { ...REPO.checks, write: { ok: false, state: "cant_write", at: now } } }] },
    models: [{ ...MODELS[0], pill: PILLS.limited }, { ...MODELS[1], pill: PILLS.keyRejected }],
    budget: { ...BUDGET, state: "spent", spent_usd: 201, pill: PILLS.budgetSpent },
  };
}
