/**
 * An agent's share card: a 1200×630 PNG (spec §3.12), made by next/og — no new dependency.
 *
 * Behind the console's auth (middleware covers /api/*): it is for Download and Copy image, not
 * for unfurling, because a private install has no public URL to unfurl. It shows only what the
 * agent is — face, name, role, tagline, two lines about itself, when it was born, three skills
 * and a decorative machine-readable strip — and NEVER repos, MCP servers, costs (unless stats are
 * asked for) or any internal id beyond its agt_ id.
 */
// React by name: the suite renders this route with the classic JSX runtime.
import React from "react";
import { ImageResponse } from "next/og";

import { API_URL, authHeaders } from "../../../../lib/client";
import { avatarSvg } from "../../../../lib/agent-avatar";
import { svgDataUri } from "../../../../lib/svg-data-uri";
import type { AvatarSpec } from "../../../../lib/agent-world-types";
import { bornDate, humanise, mrz, resolveAvatar, toPassport } from "../../../../lib/agent-world";

export const dynamic = "force-dynamic";

const C = {
  bg: "#090D17", surface: "#101A30", border: "#26365C", ink: "#EEF3FB", muted: "#B4C4E0",
  faint: "#8092B3", teal: "#2EC5C5", accent: "#2F6FE0", green: "#34D399",
};

async function getJson(path: string): Promise<unknown> {
  const r = await fetch(`${API_URL}${path}`, { headers: authHeaders({}), cache: "no-store" });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function faceDataUri(spec: AvatarSpec): string {
  const svg = avatarSvg(spec, { size: 320, state: "idle" });
  return svgDataUri(svg);
}

export async function GET(req: Request, ctx: { params: Promise<{ role: string }> }) {
  const { role } = await ctx.params;
  const url = new URL(req.url);
  const projectId = url.searchParams.get("project_id")?.trim();
  const stats = url.searchParams.get("stats") === "1";
  if (!role || !projectId) return Response.json({ error: "missing role or project_id" }, { status: 400 });
  if (!API_URL) return Response.json({ error: "BOSS_API_URL is not set" }, { status: 503 });

  let passport;
  let skills: string[] = [];
  try {
    const q = `?project_id=${encodeURIComponent(projectId)}`;
    passport = toPassport(await getJson(`/v1/agents/${encodeURIComponent(role)}/passport${q}`));
    const card = await getJson(`/v1/agents/${encodeURIComponent(role)}/card${q}`).catch(() => ({}));
    skills = ((card as { skills?: Array<{ name?: string; id?: string }> }).skills ?? [])
      .map((s) => s.name || s.id || "").filter(Boolean).slice(0, 3);
  } catch (e) {
    return Response.json({ error: String((e as Error).message ?? e) }, { status: 502 });
  }

  const persona = passport.persona;
  const display = passport.displayName || humanise(role);
  const name = persona.callsign || display;
  // The same face as every chip: resolved, never the stored avatar drawn raw.
  const face = faceDataUri(resolveAvatar(passport.roleKey || role, passport.agentId, persona.avatar));
  const about = (persona.about_me ?? "").slice(0, 170);
  const [m1, m2] = mrz(passport.agentId, passport.version, name);
  const v = passport.vitals;

  return new ImageResponse(
    (
      <div style={{ width: 1200, height: 630, display: "flex", flexDirection: "column", background: C.bg,
                    color: C.ink, fontFamily: "sans-serif", padding: 56, border: `2px solid ${C.border}` }}>
        <div style={{ display: "flex", flexDirection: "row", flex: 1 }}>
          <img src={face} width={300} height={300} style={{ borderRadius: 40, marginRight: 56 }} alt="" />
          <div style={{ display: "flex", flexDirection: "column", flex: 1 }}>
            <div style={{ display: "flex", alignItems: "center", fontSize: 22, color: C.teal, letterSpacing: 4 }}>
              AGENT PASSPORT
              <span style={{ marginLeft: 16, padding: "2px 12px", border: `2px solid ${C.teal}`, borderRadius: 999,
                             fontSize: 18, letterSpacing: 1 }}>AI agent</span>
            </div>
            <div style={{ display: "flex", fontSize: 68, fontWeight: 700, marginTop: 8 }}>{name}</div>
            <div style={{ display: "flex", fontSize: 30, color: C.muted }}>{persona.callsign ? display : humanise(role)}</div>
            {persona.tagline ? (
              <div style={{ display: "flex", fontSize: 28, marginTop: 12, color: C.ink }}>“{persona.tagline}”</div>
            ) : null}
            {about ? (
              <div style={{ display: "flex", fontSize: 22, marginTop: 14, color: C.muted, lineHeight: 1.4 }}>{about}</div>
            ) : null}
            <div style={{ display: "flex", marginTop: 14, fontSize: 20, color: C.faint }}>
              Born {bornDate(passport.born.at)}
            </div>
            <div style={{ display: "flex", marginTop: 10 }}>
              {skills.map((s) => (
                <span key={s} style={{ display: "flex", marginRight: 12, padding: "6px 14px", borderRadius: 8,
                                       border: `2px solid ${C.accent}`, color: C.ink, fontSize: 18 }}>{s}</span>
              ))}
            </div>
            {stats ? (
              <div style={{ display: "flex", marginTop: 10, fontSize: 20, color: C.green }}>
                {`Trust ${v.trust} · ${v.runs} runs · ${Math.round(v.cleanRate * (v.cleanRate <= 1 ? 100 : 1))}% clean`}
              </div>
            ) : null}
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", fontFamily: "monospace", fontSize: 22,
                      color: C.faint, letterSpacing: 3, borderTop: `2px solid ${C.border}`, paddingTop: 16 }}>
          <span style={{ display: "flex" }}>{m1}</span>
          <span style={{ display: "flex" }}>{m2}</span>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: 18, color: C.muted, marginTop: 10 }}>
          <span style={{ display: "flex" }}>AI agent · not a real person</span>
          <span style={{ display: "flex" }}>Built with BOSS · open source</span>
        </div>
      </div>
    ),
    { width: 1200, height: 630, headers: { "Cache-Control": "private, no-store" } },
  );
}
