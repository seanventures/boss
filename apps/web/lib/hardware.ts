import type {
  HardwareAgentEnrollResponse,
  HardwarePlace,
  HardwarePlacesResponse,
} from "@boss/api-client";
import { apiWrite } from "./api-write";

/** Copy for the installer machine. It is a place even when nothing else is registered. */
export const DEFAULT_PLACE_TITLE = "This computer";
export const DEFAULT_PLACE_LINE = "Where BOSS was installed";
export const DEFAULT_PLACE_DETAIL =
  "Apps install on this computer. It is where the installer ran, and it stays selected.";
export const EMPTY_EXTRAS_COPY =
  "No additional hardware. This computer is already where apps install.";
export const AGENT_INSTALLED_COPY = "Hardware agent installed on this computer.";
export const AGENT_ABSENT_COPY = "Hardware agent not ready on this computer.";
export const SAME_AGENT_COPY =
  "A new machine gets the same hardware agent that runs on this computer.";

const PEM = /-----BEGIN [A-Z0-9 ]+-----/;
const AWS_ACCESS_KEY = /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/;
const LIVE_TOKEN = /(?:^|[\s"'=:])(?:sk-|sk-ant-|ghp_|github_pat_|dop_v1_|doo_v1_|xox[baprs]-|glpat-|ya29\.)/;
const ASSIGNED_SECRET =
  /(?:^|[\s,{])["']?(?:aws_)?(?:secret_access_key|access_key_id|password|api_key|token|secret)["']?\s*[:=]\s*["']?[A-Za-z0-9/+=_-]{8,}/i;
const RAW_BLOB = /^[A-Za-z0-9+/]{40,}={0,2}$/;
const REFERENCE = /^(?:[a-z][a-z0-9+.-]*:)?[A-Za-z0-9_./:@+-]{1,200}$/i;

/**
 * A secret reference is a name. Return an explanation when the value is a pasted credential
 * the client can recognize, otherwise null so a normal reference can be sent.
 */
export function explainRawSecret(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return "Enter the secret reference, not the secret itself.";
  }
  if (PEM.test(trimmed)) {
    return "That is a private key. Store it, then paste the secret reference — not the key.";
  }
  if (AWS_ACCESS_KEY.test(trimmed)) {
    return "That is an AWS access key. Paste the secret reference, not the key.";
  }
  if (LIVE_TOKEN.test(trimmed)) {
    return "That is a live token. Paste the secret reference, not the token.";
  }
  if (ASSIGNED_SECRET.test(trimmed) || trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return "That looks like a pasted credential. Paste the secret reference.";
  }
  if (/\s/.test(trimmed)) {
    return "A secret reference is a single name, not a pasted secret.";
  }
  if (RAW_BLOB.test(trimmed)) {
    return "That looks like a raw secret. Paste the secret reference.";
  }
  if (!REFERENCE.test(trimmed)) {
    return "Enter a secret reference such as env:BOSS_AWS_SECRET, not the secret itself.";
  }
  return null;
}

export function enrollCommand(body: HardwareAgentEnrollResponse | null | undefined): string {
  const command = body?.command ?? body?.enroll_command ?? "";
  return command.trim();
}

export function isLocalPlace(place: HardwarePlace): boolean {
  return place.kind === "local";
}

export function agentStateCopy(state: HardwarePlace["agent_state"] | undefined): {
  state: "installed" | "absent";
  label: string;
} {
  if (state === "installed") {
    return { state: "installed", label: AGENT_INSTALLED_COPY };
  }
  return { state: "absent", label: AGENT_ABSENT_COPY };
}

/** The installer machine wins, then the API's default id, then the first local place. */
export function selectedPlace(
  places: HardwarePlace[],
  defaultPlaceId: string | null | undefined,
): HardwarePlace | null {
  if (defaultPlaceId) {
    const named = places.find((place) => place.id === defaultPlaceId);
    if (named) return named;
  }
  return places.find(isLocalPlace) ?? null;
}

async function readBody(res: Response): Promise<Record<string, unknown>> {
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (!res.ok) {
    const detail = data.detail;
    const error = typeof data.error === "string"
      ? data.error
      : typeof detail === "string"
        ? detail
        : `The hardware request failed (${res.status}).`;
    throw new Error(error);
  }
  return data;
}

export async function fetchHardwarePlaces(projectId: string): Promise<HardwarePlacesResponse> {
  const res = await fetch(
    `/api/hardware/places?project_id=${encodeURIComponent(projectId)}`,
    { cache: "no-store" },
  );
  const data = await readBody(res);
  return {
    places: Array.isArray(data.places) ? data.places as HardwarePlace[] : [],
    default_place_id: typeof data.default_place_id === "string" ? data.default_place_id : null,
  };
}

export async function resolveHardwarePlace(projectId: string): Promise<HardwarePlace | null> {
  const res = await fetch(
    `/api/hardware/resolve?project_id=${encodeURIComponent(projectId)}`,
    { cache: "no-store" },
  );
  if (!res.ok) return null;
  const data = (await res.json().catch(() => null)) as HardwarePlace | { place?: HardwarePlace } | null;
  if (!data || typeof data !== "object") return null;
  if ("place" in data && data.place && typeof data.place.id === "string") return data.place;
  if ("id" in data && typeof data.id === "string") return data as HardwarePlace;
  return null;
}

export async function registerLocalPlace(body: {
  project_id: string;
  actor: string;
  hostname: string;
  arch: string;
  os: string;
}): Promise<HardwarePlace> {
  const res = await apiWrite("/api/hardware/places/local", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await readBody(res) as unknown as HardwarePlace;
}

export async function enrollHardwareAgent(body: {
  project_id: string;
  actor: string;
  name: string;
}): Promise<HardwareAgentEnrollResponse> {
  const res = await apiWrite("/api/hardware/places/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await readBody(res) as HardwareAgentEnrollResponse;
}

export async function registerHardwareProvider(body: {
  project_id: string;
  actor: string;
  name: string;
  provider: string;
  secret_ref: string;
}): Promise<HardwarePlace> {
  const res = await apiWrite("/api/hardware/places/providers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return await readBody(res) as unknown as HardwarePlace;
}
