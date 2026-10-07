import { ImageResponse } from "next/og";
import { NextResponse } from "next/server";

import type { Chain } from "../../../../lib/agent-world-types";
import { API_URL, authHeaders } from "../../../../lib/client";
import { svgDataUri } from "../../../../lib/svg-data-uri";
import { apiWrite } from "../../../../lib/api-write";
import { loadPath } from "../../../../lib/writing-get";
import { toChain } from "../../../agents/act-wire";
import { CARD_COLOURS as C, creditsCard, type CreditsCard } from "../../../agents/credits-card";

/**
 * Run Credits as a 1200×630 PNG: "Made by 5 agents", faces in chain order (spec §3.6, §11.1).
 *
 * Behind the console's session (middleware.ts), for download and copy, never for unfurling: a
 * private install's run is nobody else's business. It shows the agents, their role in the run,
 * the totals and the accountable person — no repository, no MCP server, no internal id.
 */
export const dynamic = "force-dynamic";

const ID = /^[A-Za-z0-9_.:-]{1,128}$/;
const W = 1200;
const H = 630;

function PersonGlyph({ size }: { size: number }) {
  // A person, drawn plainly: a head and shoulders. Never a face.
  return (
    <div style={{ width: size, height: size, borderRadius: size, background: C.surface, border: `2px solid ${C.border}`,
                  display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "flex-end", overflow: "hidden" }}>
      <div style={{ width: size * 0.34, height: size * 0.34, borderRadius: size, background: C.faint, marginBottom: size * 0.06 }} />
      <div style={{ width: size * 0.62, height: size * 0.3, borderRadius: `${size}px ${size}px 0 0`, background: C.faint }} />
    </div>
  );
}

function Card({ card }: { card: CreditsCard }) {
  const n = Math.max(1, card.faces.length);
  const gap = n > 6 ? 14 : 24;
  const col = Math.floor((W - 128 - gap * (n - 1)) / n);
  const size = Math.min(120, col - 16);
  return (
    <div style={{ width: W, height: H, display: "flex", flexDirection: "column", background: C.bg, color: C.ink,
                  padding: "56px 64px", fontFamily: "sans-serif" }}>
      <div style={{ display: "flex", flexDirection: "column" }}>
        <div style={{ fontSize: 22, letterSpacing: 4, color: C.accent, display: "flex" }}>
          {`MADE BY ${card.agents} ${card.agents === 1 ? "AGENT" : "AGENTS"}`}
        </div>
        <div style={{ fontSize: 48, marginTop: 8, display: "flex" }}>{card.title}</div>
      </div>
      <div style={{ display: "flex", flexDirection: "row", gap, marginTop: 40, flexGrow: 1 }}>
        {card.faces.map((f, i) => (
          <div key={`${f.roleKey ?? "p"}-${i}`} style={{ display: "flex", flexDirection: "column", alignItems: "center", width: col }}>
            {f.svg ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={svgDataUri(f.svg)} width={size} height={size} alt="" />
            ) : f.roleKey ? (
              <div style={{ width: size, height: size, borderRadius: size, background: f.tone, border: `2px solid ${C.border}`,
                            display: "flex", alignItems: "center", justifyContent: "center", fontSize: size / 2.8,
                            color: C.bg }}>{f.initials}</div>
            ) : <PersonGlyph size={size} />}
            <div style={{ fontSize: n > 6 ? 18 : 22, marginTop: 12, display: "flex", textAlign: "center", justifyContent: "center" }}>{f.name}</div>
            {f.role !== f.name ? <div style={{ fontSize: 14, color: C.muted, display: "flex", textAlign: "center" }}>{f.role}</div> : null}
            {f.line ? <div style={{ fontSize: 14, color: C.faint, marginTop: 4, display: "flex", textAlign: "center", justifyContent: "center" }}>{f.line.slice(0, 40)}</div> : null}
          </div>
        ))}
      </div>
      <div style={{ display: "flex", flexDirection: "row", justifyContent: "space-between", alignItems: "flex-end",
                    borderTop: `1px solid ${C.border}`, paddingTop: 22 }}>
        <div style={{ display: "flex", flexDirection: "column" }}>
          <div style={{ fontSize: 24, display: "flex" }}>{`${card.steps} steps · ${card.time} · ${card.cost}`}</div>
          <div style={{ fontSize: 20, color: C.muted, marginTop: 6, display: "flex" }}>{`Accountable: ${card.accountable}`}</div>
        </div>
        <div style={{ fontSize: 20, color: C.green, display: "flex" }}>Built with BOSS · open source</div>
      </div>
    </div>
  );
}

/** A build's credits are its own acts; a workflow run's, its chain. */
function chainPath(id: string): string {
  return id.startsWith("crun_") ? `/v1/code-tasks/${encodeURIComponent(id)}/chain`
    : loadPath(`/v1/workflow-runs/${encodeURIComponent(id)}/chain`);
}

export async function GET(req: Request, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  const params = new URL(req.url).searchParams;
  const projectId = (params.get("project_id") ?? "").trim();
  if (!ID.test(id)) return NextResponse.json({ error: "missing run id" }, { status: 400 });
  let chain: Chain;
  try {
    // A workflow run's chain is a writing GET: its POST twin (chainPath), through the write path.
    const url = `${API_URL}${chainPath(id)}?project_id=${encodeURIComponent(projectId)}`;
    const headers = authHeaders({ "Content-Type": "application/json" });
    const r = id.startsWith("crun_")
      ? await fetch(url, { headers, cache: "no-store" })
      : await apiWrite(url, { method: "POST", headers, cache: "no-store" });
    if (!r.ok) return NextResponse.json({ error: "no credits for this run" }, { status: r.status });
    chain = toChain(await r.json());
  } catch {
    return NextResponse.json({ error: "the run could not be read" }, { status: 502 });
  }
  const card = creditsCard(chain, params.get("title") ?? "");
  if (!card.agents) return NextResponse.json({ error: "no agents worked on this run" }, { status: 404 });
  return new ImageResponse(<Card card={card} />, {
    width: W, height: H,
    headers: { "Cache-Control": "private, no-store", "Content-Disposition": `inline; filename="boss-credits-${id}.png"` },
  });
}
