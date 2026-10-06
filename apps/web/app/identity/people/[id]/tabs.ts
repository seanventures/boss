/** The profile's sections (?tab=), shared by the server page and the client screen. */
export const PERSON_TABS = [
  { id: "overview", label: "Overview" },
  { id: "access", label: "Access" },
  { id: "sessions", label: "Sessions" },
  { id: "security", label: "Security" },
  { id: "delegations", label: "Delegations" },
  { id: "keys", label: "API keys" },
  { id: "audit", label: "Audit" },
] as const;
export type PersonTab = (typeof PERSON_TABS)[number]["id"];
