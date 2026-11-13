import type { CSSProperties } from "react";
import { redirect } from "next/navigation";

import { API_URL } from "../../../lib/client";

export const dynamic = "force-dynamic";

/**
 * Link a chat account to you (BOSSAPP-834). A person who pressed Approve/Reject in Slack or
 * Telegram before BOSS knew them was sent here with a one-time code (valid 30 minutes). Signed in
 * (the middleware sends a visitor without a session to sign in and back), they see WHICH chat
 * account the code names and confirm it is theirs. Only then do their presses in that channel
 * answer decisions — as them, and only within what they may decide here.
 */
type Peek = { kind: string; endpoint_name: string; external_user_id: string; external_name?: string | null };
type Props = { searchParams: Promise<{ code?: string; linked?: string; error?: string }> };

async function post<T>(path: string, body: unknown): Promise<{ ok: boolean; data: T | null; message: string }> {
  try {
    const r = await fetch(`${API_URL}${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store",
    });
    const data = (await r.json().catch(() => null)) as (T & { message?: string }) | null;
    return { ok: r.ok, data: r.ok ? data : null, message: (data && typeof data.message === "string") ? data.message : `BOSS answered ${r.status}` };
  } catch (e) {
    return { ok: false, data: null, message: `BOSS could not be reached: ${String((e as Error)?.message ?? e)}` };
  }
}

async function confirm(form: FormData) {
  "use server";
  const code = String(form.get("code") ?? "").trim();
  const done = await post<{ principal_id: string }>("/v1/chat/links/confirm", { code });
  redirect(done.ok ? "/chat/link?linked=1" : `/chat/link?error=${encodeURIComponent(done.message)}`);
}

const box: CSSProperties = {
  maxWidth: 520, margin: "48px auto", padding: "24px 16px", display: "grid", gap: 16,
  color: "var(--ink)", background: "var(--bg)",
};

export default async function ChatLinkPage({ searchParams }: Props) {
  const { code = "", linked, error } = await searchParams;
  if (linked === "1") {
    return (
      <main style={box}>
        <h1 style={{ fontSize: "var(--fs-page)" }}>Chat account linked</h1>
        <p>Decisions you answer from that chat are now recorded as you. You can close this tab.</p>
      </main>
    );
  }
  if (error) {
    return (
      <main style={box}>
        <h1 style={{ fontSize: "var(--fs-page)" }}>Could not link</h1>
        <p role="alert">{error}</p>
        <p>Press a button in chat again for a new link.</p>
      </main>
    );
  }
  const peek = code ? await post<Peek>("/v1/chat/links/peek", { code }) : null;
  if (!peek || !peek.ok || !peek.data) {
    return (
      <main style={box}>
        <h1 style={{ fontSize: "var(--fs-page)" }}>Link a chat account</h1>
        <p role="alert">{peek ? peek.message : "This page needs the link BOSS sent you in chat."}</p>
      </main>
    );
  }
  const who = peek.data.external_name ? `@${peek.data.external_name}` : peek.data.external_user_id;
  const channel = peek.data.kind === "telegram" ? "Telegram" : peek.data.kind === "slack" ? "Slack" : peek.data.kind;
  return (
    <main style={box}>
      <h1 style={{ fontSize: "var(--fs-page)" }}>Link {channel} to you</h1>
      <p>
        <strong>{who}</strong> in {channel} ({peek.data.endpoint_name}) asked to answer BOSS decisions.
        Confirm only if that is you.
      </p>
      <form action={confirm}>
        <input type="hidden" name="code" value={code} />
        <button type="submit" className="btn primary">This is me — link it</button>
      </form>
    </main>
  );
}
