/**
 * The agent share card, 1200×630 (spec §3.12): the picture S-agents' `app/api/agent-card/[role]`
 * route hands to next/og, and the same JSX the Share card dialog previews in the page.
 *
 * Written for satori (next/og): every element with more than one child is `display: flex`,
 * styles are inline, colours are hex (no stylesheet exists there), and the face is an <img> of
 * the kit's own SVG, so the card's face is byte-for-byte the console's.
 *
 * Never shown: repos, MCP servers, costs (unless `stats` is passed), any id but the agt_ one.
 */
import React from "react";

import { avatarDataUri } from "../../lib/agent-avatar";
import type { AgentIdentity } from "../../lib/agent-world-types";
import { PALETTE_BY_ID } from "../../lib/avatar-kit/palettes";

export const SHARE_CARD = { width: 1200, height: 630 } as const;

export type ShareCardData = {
  identity: AgentIdentity;
  /** About me, first person. Cut to two lines. */
  aboutMe?: string;
  /** "3 Oct 2026". */
  born?: string;
  /** Up to three skill names, shown as stamps. */
  skills?: string[];
  /** "v4". */
  version?: string;
  /** Only when "Include stats" is ticked. */
  stats?: { trust: number; runs: number; cleanRate: number } | null;
};

const INK = "#eef3fb", MUTED = "#9fb0ce", FAINT = "#64769a", VOID = "#090d17", LINE = "#26365c";

/** The decorative machine-readable footer: agt id · kit · version, padded with chevrons. */
export function mrzLine(agentId: string | null, version = "v1"): string {
  const id = (agentId ?? "agt_local").toUpperCase().replace(/[^A-Z0-9]/g, "<");
  const line = `BOSS<<${id}<<KIT<BOSS<V1<<${version.toUpperCase()}`;
  return (line + "<".repeat(44)).slice(0, 44);
}

function clip(s: string | undefined, n: number): string {
  if (!s) return "";
  const t = s.replace(/\s+/g, " ").trim();
  return t.length > n ? `${t.slice(0, n - 1).trimEnd()}…` : t;
}

export function ShareCardVisual({ data }: { data: ShareCardData }) {
  const { identity: who } = data;
  const tint = PALETTE_BY_ID[who.avatar.palette ?? ""]?.body ?? "#2ec5c5";
  const title = who.callsign ? who.callsign : who.name;
  return (
    <div style={{
      width: SHARE_CARD.width, height: SHARE_CARD.height, display: "flex", position: "relative",
      background: VOID, color: INK, fontFamily: "Sora, Manrope, sans-serif", overflow: "hidden",
      backgroundImage: `radial-gradient(circle at 24% 46%, ${tint}33 0%, ${VOID} 46%)`,
    }}>
      <div style={{ width: 470, display: "flex", alignItems: "center", justifyContent: "center" }}>
        {/* eslint-disable-next-line @next/next/no-img-element -- satori draws <img>, not next/image */}
        <img src={avatarDataUri(who.avatar, { size: 320 })} width={340} height={340} alt="" />
      </div>
      <div style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center", padding: "0 64px 40px 0" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <span style={{ fontSize: 68, fontWeight: 700, letterSpacing: -1 }}>{title}</span>
          <span style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 14px", borderRadius: 999,
                         border: `2px solid ${LINE}`, fontSize: 22, fontWeight: 700, color: INK }}>
            <span style={{ width: 10, height: 10, borderRadius: 999, background: "#2ec5c5" }} />AI agent
          </span>
        </div>
        {who.callsign ? <div style={{ display: "flex", fontSize: 30, color: MUTED, marginTop: 4 }}>{who.name}</div> : null}
        {who.tagline ? <div style={{ display: "flex", fontSize: 32, fontStyle: "italic", color: tint, marginTop: 22 }}>{`“${clip(who.tagline, 60)}”`}</div> : null}
        {data.aboutMe ? <div style={{ display: "flex", fontSize: 24, lineHeight: 1.4, color: "#c3d0e8", marginTop: 18, maxWidth: 640 }}>{clip(data.aboutMe, 130)}</div> : null}
        <div style={{ display: "flex", gap: 12, marginTop: 28, flexWrap: "wrap" }}>
          {(data.skills ?? []).slice(0, 3).map((s) => (
            <span key={s} style={{ display: "flex", padding: "8px 16px", borderRadius: 8, border: `2px dashed ${tint}`, color: INK, fontSize: 20, fontWeight: 600 }}>{clip(s, 24)}</span>
          ))}
        </div>
        <div style={{ display: "flex", gap: 28, marginTop: 26, fontSize: 22, color: MUTED }}>
          {data.born ? <span style={{ display: "flex" }}>{`Born ${data.born}`}</span> : null}
          {data.stats ? <span style={{ display: "flex" }}>{`Trust ${data.stats.trust} · ${data.stats.runs} runs · ${Math.round(data.stats.cleanRate * 100)}% clean`}</span> : null}
        </div>
      </div>
      <div style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: 64, display: "flex", alignItems: "center",
                    justifyContent: "space-between", padding: "0 40px", borderTop: `2px solid ${LINE}`, background: "#0b101e" }}>
        <span style={{ display: "flex", fontFamily: "monospace", fontSize: 22, letterSpacing: 3, color: FAINT }}>{mrzLine(who.agentId, data.version)}</span>
        <span style={{ display: "flex", fontSize: 20, fontWeight: 700, color: MUTED }}>Built with BOSS · open source</span>
      </div>
    </div>
  );
}
