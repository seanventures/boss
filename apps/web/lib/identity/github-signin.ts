/**
 * "Sign in with GitHub" — OAuth device flow against one published BOSS App (engine
 * identity/github_device.py). No callback URL, so it works on localhost, a tailnet or a private host.
 *
 * The decisions are pure functions of the server's answers, so the sequence (start → pending →
 * slow_down → connected / expired / denied) is tested without a DOM (tests/github-signin.test.ts).
 */
import { idGet, idPost } from "./api";

export type InstallNeeded = { account: string; type: "User" | "Organization" | null; install_url: string | null };
export type SigninStatus = {
  configured: boolean;
  slug: string | null;
  install_url: string | null;
  verification_uri: string;
  me: { id: string; login: string | null; status: string; linked_at: string | null; via_signin: boolean } | null;
};
export type DeviceStart = { user_code: string; verification_uri: string; expires_in: number; interval: number;
                            handle: string };
export type PollAnswer =
  | { state: "pending"; interval: number; slow_down?: boolean }
  | { state: "connected"; login: string | null }
  | { state: "expired" | "denied" | "failed"; error: string };
export type Reach = { login: string; repos: { full_name: string; private: boolean; default_branch: string | null;
                                              owner_type: string }[];
                      read_only: number; install_needed: InstallNeeded[]; install_url: string | null };

/** Only a whole answer counts; an older API (or anything else) reads as null: "not offered here". */
export function signinOf(raw: unknown): SigninStatus | null {
  const s = raw as Partial<SigninStatus> | null;
  if (!s || typeof s !== "object" || typeof s.configured !== "boolean") return null;
  return { configured: s.configured, slug: s.slug ?? null, install_url: s.install_url ?? null,
           verification_uri: s.verification_uri || "https://github.com/login/device", me: s.me ?? null };
}

export type Phase =
  | { at: "idle" }
  | { at: "code"; code: string; uri: string; handle: string; interval: number; expiresAt: number }
  | { at: "done"; login: string | null }
  | { at: "stopped"; why: "expired" | "denied" | "failed"; error: string };

/** The next phase after one poll answer. slow_down keeps the code and waits longer (GitHub's rule). */
export function afterPoll(phase: Phase, answer: PollAnswer): Phase {
  if (phase.at !== "code") return phase;
  if (answer.state === "pending") return { ...phase, interval: Math.max(answer.interval, phase.interval) };
  if (answer.state === "connected") return { at: "done", login: answer.login };
  return { at: "stopped", why: answer.state, error: answer.error };
}

/** What each ending says, in a few words (the detail is in the tooltip). */
export const STOP_WORDS: Record<"expired" | "denied" | "failed", { label: string; tip: string }> = {
  expired: { label: "Code expired", tip: "The code lasts 15 minutes. Start again for a new one." },
  denied: { label: "Not approved", tip: "GitHub says the request was cancelled. Start again to retry." },
  failed: { label: "Sign-in failed", tip: "GitHub refused the sign-in. Start again, or use Advanced." },
};

export const INSTALL_TIP = "An owner installs it once. Otherwise GitHub asks an owner for you.";

/** "Install BOSS on <org>" — the exact words for the one-time install on an account. */
export function installLabel(n: InstallNeeded): string {
  return `Install BOSS on ${n.account}`;
}

export const fetchSignin = async () => signinOf(await idGet<unknown>("/v1/identity/github/signin"));
export const startDevice = () => idPost<DeviceStart>("/v1/identity/github/device/start", {});
export const pollDevice = (handle: string) => idPost<PollAnswer>("/v1/identity/github/device/poll", { handle });
export const fetchReach = () => idGet<Reach>("/v1/identity/github/reach");

/** Said when a sign-in finishes, so Pick repos (and anything else) re-reads the person's reach. */
export const SIGNIN_EVENT = "boss:github-signed-in";
