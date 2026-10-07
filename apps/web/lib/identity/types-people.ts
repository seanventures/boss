/**
 * Shapes for the People and Groups extras (FX-C): access requests (A13, FX-B's approve/deny),
 * a project choice for Add access, and the CSV import's parsed rows.
 */
import type { ReadFailure } from "../../components/identity/states";

/** GET /v1/identity/access-requests → requests[] (FX-B adds candidate_roles). */
export interface AccessRequestRow {
  id: string;
  requester_principal_id: string;
  requester_name: string;
  permission_key: string;
  project_id: string | null;
  reason: string | null;
  status: "open" | "granted" | "declined" | string;
  created_at: string;
  candidate_roles?: { id: string; key: string; name: string }[];
}

/** A project the viewer can pick (GET /v1/projects). */
export interface ProjectChoice {
  id: string;
  name: string;
}

/** One line of an imported CSV, after checking. */
export interface CsvInviteRow {
  line: number;
  email: string;
  role: string;
  groups: string[];
  /** Why the row will not be sent; null when it is fine. */
  problem: string | null;
}

/** The parts of the directory a page read, and why a part is missing (forbidden vs failed). */
export type DirectoryFailures = Partial<Record<"users" | "roles" | "groups" | "bindings", ReadFailure>>;
