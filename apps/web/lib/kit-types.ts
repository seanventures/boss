/**
 * The Kit's shapes (skills-mcp spec §6.3), shared by the console and the API proxy.
 *
 * An ABILITY is what the operator sees: one outcome ("Open pull requests"), backed by at most one
 * connection, some skills and some MCP tools. `GET /v1/kit` computes every ability's single
 * status server-side, so no screen re-derives it.
 */
/** `untested`: set up and held, but no passing test yet — not Ready until one passes (K8). */
export type AbilityStatus = "ready" | "untested" | "unheld" | "setup" | "needs_repo" | "testing" | "failing" | "review" | "off";
export type ToolPolicy = "allow" | "ask" | "off";
export type AbilityGroup = "code" | "research" | "coordinate" | "ship" | "media";

export type KitTool = {
  ref: string;
  label: string;
  tier: 0 | 1 | 2 | 3;
  policy: ToolPolicy;
  /** The lowest policy the runtime allows: "ask" for tier >= 2, else null. */
  floor: ToolPolicy | null;
  kind: "skill" | "mcp" | "builtin";
};

export type ProviderField = {
  name: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
  copy?: boolean;
  optional?: boolean;
  /** A choice rather than text (Email's TLS: None · STARTTLS · SSL): a segmented control. */
  options?: { value: string; label: string }[];
  /** The option chosen when nothing is stored. */
  default?: string;
};

export type Provider = {
  key: string;
  label: string;
  fields: ProviderField[];
  reuses?: string;
  token_url?: string;
  scopes_tip?: string;
  keyless?: boolean;
};

export type TestResult = {
  ok: boolean;
  at: string;
  latency_ms: number;
  fact: string | null;
  error: string | null;
  field?: string | null;
};

export type Ability = {
  key: string;
  group: AbilityGroup;
  label: string;
  tip: string;
  icon: string;
  provenance: "built_in" | "official" | "custom";
  essential: boolean;
  status: AbilityStatus;
  status_line: string | null;
  status_tip: string;
  connection: null | {
    providers: Provider[];
    provider: string | null;
    /** Stored values: text as typed, a secret only as "…a1f3" (never the value). */
    set_fields: Record<string, string>;
    acts_as: string | null;
    /** "channels" for YouTube: the drawer runs the existing Google client + channel flow. */
    flow?: string | null;
    /** Whether something is stored (or works without anything, like keyless Exa). */
    connected?: boolean;
  };
  tools: KitTool[];
  holders: string[];
  recommended: string[];
  /** `via` names what the step uses ("skill:<id>", "mcp:<server>", "capability:<x>"), so a
   *  Library row counts its own steps rather than the whole ability's. */
  used_in: { workflow_key: string; workflow_name: string; step_id: string; step_title: string; via?: string }[];
  last_test: null | TestResult;
  try: null | { field: ProviderField };
  depends_on: string[];
  /** Where an ability whose connection lives in another area is set up (Deploy → Infra). */
  connect_href?: string | null;
  /** The token scopes a provider needs, for the `Get a token ↗` tooltip. */
  scopes_tip?: string | null;
  mcp: { server_id: string; name: string; trusted: boolean; reachable: boolean | null; url?: string }[];
  /** The MCP servers the kit file names, registered or not. */
  mcp_names?: string[];
};

export type Kit = {
  project_id: string;
  ready: number;
  total: number;
  essentials: string[];
  groups: string[];
  abilities: Ability[];
};

export type GrantResult = {
  granted: { role_key: string; ability: string }[];
  skipped: { role_key: string; ability: string; reason: string }[];
};

export type CrewRow = { ability: string; ok: boolean; holders: string[]; detail: string };
export type CrewTest = { rows: CrewRow[]; live?: { role_key: string; ok: boolean; detail: string; receipt_id?: string | null }[] };

export type McpCatalogueEntry = {
  key: string;
  name: string;
  label: string;
  url: string;
  auth: "none" | "bearer" | "header";
  header_name?: string | null;
  token_url?: string | null;
  scopes_tip?: string | null;
  risk_default: number;
  provenance: "official" | "built_in" | "custom";
  description: string;
  added?: boolean;
};

export type McpTestResult = {
  ok: boolean;
  tools: { name: string; description: string }[];
  latency_ms: number;
  error?: string | null;
};
