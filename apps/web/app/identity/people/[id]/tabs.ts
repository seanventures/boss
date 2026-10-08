/** The profile's sections (?tab=), shared by the server page and the client screen. */
export const PERSON_TABS = [
  // Access first: it is the page's default and its main job (giving or taking away access).
  { id: "access", label: "Access" },
  { id: "overview", label: "Overview" },
  { id: "sessions", label: "Sessions" },
  { id: "security", label: "Security" },
  { id: "delegations", label: "Delegations" },
  { id: "keys", label: "API keys" },
  { id: "audit", label: "Audit" },
] as const;
export type PersonTab = (typeof PERSON_TABS)[number]["id"];
