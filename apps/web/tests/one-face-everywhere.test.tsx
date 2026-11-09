/**
 * One face everywhere (spec §2.1, §12.4 test 11).
 *
 * The same agent, reached by any of its names, draws the identical SVG on every surface that
 * shows agents today: the chip (Agents, composer, panels), the workflow step node, the
 * inspector's agent picker and a meeting seat. The Passport, roster card and dock head are
 * mounted by S-agents / S-runs on <AgentAvatar>, which is what this renders.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { AgentAvatar } from "../app/agent-avatar";
import { AgentChip } from "../app/agent-chip";
import { ROLE_ORDER, SeatAvatar } from "../app/meeting-room-ui";
import { Avatar as InspectorAvatar } from "../app/workflows/editor/inspector/agent-combobox";
import { StepNode } from "../app/workflows/flow/step-node";
import { avatarSvg, specForRole } from "../lib/agent-avatar";
import { codeOnly } from "./_helpers/source";

(globalThis as Record<string, unknown>).React = React;

/** The kit face in a piece of markup (a node also draws its phosphor type icon). */
const svgOf = (html: string) => /<svg[^>]*class="ag-av"[\s\S]*?<\/svg>/.exec(html)?.[0] ?? "";

const BADGES = { approval: false, forced: false, inputs: 0, unwired: [], skills: 0, mcp: 0, lacks: [] };
const node = (agent: Parameters<typeof StepNode>[0]["view"]["agent"], mode: "design" | "run" = "design",
              phase?: "moving" | "needs_you" | "done" | "failed") => renderToStaticMarkup(
  <StepNode mode={mode} run={phase ? { phase, meta: "" } : undefined}
            view={{ id: "s", title: "Test it", type: "agent", icon: "agent", agent, badges: BADGES }} />);

describe("one agent, one face, on every surface", () => {
  // Every surface below draws the 20-24px tier; compare like with like.
  const expected = avatarSvg(specForRole("qa"), { size: 24 });

  it("draws Vera identically by key, seat title, display name and callsign", () => {
    for (const name of ["qa", "QA / Test", "QA Lead", "Vera", "qa lead"]) {
      expect(svgOf(renderToStaticMarkup(<AgentAvatar agent={name} size={24} />)), name).toBe(expected);
    }
  });

  it("the chip, the step node, the inspector option and a meeting seat agree", () => {
    const chip = svgOf(renderToStaticMarkup(<AgentChip roleKey="qa" />));
    const chipByTitle = svgOf(renderToStaticMarkup(<AgentChip roleKey="QA / Test" />));
    const step = svgOf(node({ kind: "named", role_key: "qa", label: "QA Lead", tip: "" }));
    const inspector = svgOf(renderToStaticMarkup(<InspectorAvatar value="qa" label="QA Lead" size={24} />));
    const seat = svgOf(renderToStaticMarkup(<AgentAvatar agent="QA / Test" size={24} className="mr-avatar ag-face" />));
    for (const [where, svg] of Object.entries({ chip, chipByTitle, step: step.replace(/width="20" height="20"/, 'width="24" height="24"'), inspector, seat })) {
      expect(svg, where).toBe(expected);
    }
  });

  it("the meeting room has no face table of its own any more", () => {
    const ROOM = codeOnly("app/meeting-room-ui.tsx");
    expect(ROOM).not.toContain("ROLE_META");
    expect(ROOM).not.toContain("initials");
    expect(ROOM).toContain("<AgentAvatar agent={role}");
    expect(ROOM).toContain('variant="boss"'); // the facilitator is BOSS itself
  });

  it("the library card's footer draws faces, not initials", () => {
    const LIB = codeOnly("app/workflows/library.tsx");
    expect(LIB).toContain("<AgentAvatar key={a} agent={a}");
    expect(LIB).not.toContain("{m.initials}");
  });

  it("Auto, You and A person are the kit's system glyphs on the node and in the picker", () => {
    for (const [kind, variant, label] of [["auto", "auto", "Auto"], ["you", "you", "You"], ["person", "person", "A person"]] as const) {
      expect(node({ kind, label, tip: "" })).toContain(`data-variant="${variant}"`);
      expect(renderToStaticMarkup(<InspectorAvatar value={null} label={label} />)).toContain(`data-variant="${variant}"`);
    }
  });

  it("a running step's face carries the step's real phase", () => {
    const named = { kind: "named" as const, role_key: "qa", label: "QA Lead", tip: "" };
    expect(node(named)).toContain('data-state="idle"');
    expect(node(named, "run", "moving")).toContain('data-state="working"');
    expect(node(named, "run", "needs_you")).toContain('data-state="needs_you"');
    expect(node(named, "run", "failed")).toContain('data-state="failed"');
    expect(node(named, "run", "done")).toContain('data-state="done"');
  });

  it("a face beside a name is hidden from screen readers; a standalone face says who, that it is an AI, and what", () => {
    expect(renderToStaticMarkup(<AgentAvatar agent="qa" />)).toContain('aria-hidden="true"');
    const alone = renderToStaticMarkup(<AgentAvatar agent="qa" standalone state="needs_you" size={48} />);
    expect(alone).toContain('role="img"');
    expect(alone).toContain('aria-label="QA Lead, AI agent, Needs you"');
    // The system faces are not agents and do not claim to be.
    expect(renderToStaticMarkup(<AgentAvatar variant="you" standalone />)).toContain('aria-label="You, Idle"');
  });

  it("a chip tells a screen reader the agent is an AI, at every size", () => {
    for (const size of [16, 24, 48] as const) {
      const html = renderToStaticMarkup(<AgentChip roleKey="qa" size={size} />);
      // A sibling of the name, outside the aria-hidden face: the badge and tooltip are not read aloud.
      expect(html, String(size)).toContain('<span class="agent-chip-name">QA Lead</span><span class="ag-sr">, AI agent</span>');
    }
  });

  it("shows the AI badge from 32px, and says AI agent in the chip's tooltip below that", () => {
    expect(renderToStaticMarkup(<AgentAvatar agent="qa" size={32} />)).toContain(">AI</span>");
    expect(renderToStaticMarkup(<AgentAvatar agent="qa" size={24} />)).not.toContain(">AI</span>");
    expect(renderToStaticMarkup(<AgentAvatar variant="auto" size={48} />)).not.toContain(">AI</span>");
    expect(renderToStaticMarkup(<AgentChip roleKey="qa" />)).toContain('data-tip="QA Lead, Vera, AI agent"');
  });

  it("a paused agent shows paused wherever it appears, whatever it was last doing", async () => {
    const { primeIdentities, resetIdentities } = await import("../lib/agent-identity");
    primeIdentities([{ role_key: "qa", lifecycle: "paused" }]);
    try {
      const chip = renderToStaticMarkup(<AgentChip roleKey="qa" state="working" />);
      // The face, the chip and the word agree: one state, never "paused" beside "Working".
      expect(chip).toContain('class="agent-chip" data-testid="agent-chip-qa" data-state="paused"');
      expect(chip).toContain('<span class="ag-state-word" data-state="paused">Paused</span>');
      expect(chip).not.toContain("Working");
      expect(chip).not.toContain('data-state="working"');
      // Paused is said even when the caller passed no state at all.
      expect(renderToStaticMarkup(<AgentChip roleKey="qa" />)).toContain(">Paused</span>");
    } finally {
      resetIdentities();
    }
  });

  it("seats every default panel role as its own face, while each keeps its agent's hat and palette", async () => {
    const { agentIdentity } = await import("../lib/agent-identity");
    expect(ROLE_ORDER).toHaveLength(10);
    for (const size of [24, 26, 48]) {
      const faces = ROLE_ORDER.map((seat) => svgOf(renderToStaticMarkup(<SeatAvatar role={seat} size={size} />)));
      expect(new Set(faces).size, `${size}px`).toBe(ROLE_ORDER.length);
    }
    // Growth is still the PM's crew: Juno's hat and palette, its own emblem and visor.
    const growth = renderToStaticMarkup(<SeatAvatar role="Growth / Marketing" />);
    expect(growth).toContain('data-role-key="pm"');
    const pm = agentIdentity("pm").avatar;
    const { seatAvatar } = await import("../lib/agent-identity");
    expect(seatAvatar("Growth / Marketing")).toMatchObject({ hat: pm.hat, palette: pm.palette });
    expect(seatAvatar("growth")).toEqual(seatAvatar("Growth / Marketing"));
    // A seat with an agent of its own is exactly that agent's face.
    expect(svgOf(renderToStaticMarkup(<SeatAvatar role="QA / Test" size={24} />))).toBe(expected);
  });
});
