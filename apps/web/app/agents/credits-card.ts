/**
 * What the Run Credits image shows, from a chain (spec §3.6, §11.1). Pure and server-safe, so
 * the image route and a test read the same card.
 */
import { agentIdentity, avatarSvg, type Chain } from "../../lib/agent-world-types";
import { costLabel, minutesLabel, roleWords } from "./chain-model";

/** Base colours (globals.css :root), spelled out: an image has no stylesheet. */
export const CARD_COLOURS = { bg: "#090D17", surface: "#101A30", border: "#26365C", ink: "#EEF3FB", muted: "#B4C4E0",
  faint: "#8092B3", accent: "#2EC5C5", green: "#34D399" };
const C = CARD_COLOURS;
const TONES = ["#2F6FE0", "#2EC5C5", "#34D399", "#A78BFA", "#F5B544", "#F2647A", "#8092B3"];

export type CreditFace = { roleKey: string | null; name: string; role: string; line: string; svg: string | null; initials: string; tone: string };
export type CreditsCard = { title: string; faces: CreditFace[]; agents: number; steps: number; time: string; cost: string; accountable: string };

function tone(key: string): string {
  // cyrb-style string hash: stable, and no two anagrams collide the way a char sum does.
  let h = 2166136261;
  for (let i = 0; i < key.length; i += 1) h = Math.imul(h ^ key.charCodeAt(i), 16777619);
  return TONES[Math.abs(h) % TONES.length];
}

function face(roleKey: string | null, line: string): CreditFace {
  if (!roleKey) return { roleKey, name: "A person", role: "A person", line, svg: null, initials: "", tone: C.faint };
  let name = roleWords(roleKey);
  let role = name;
  let svg: string | null = null;
  try {
    const id = agentIdentity(roleKey);
    name = id.callsign || id.name;
    role = id.name;
    svg = avatarSvg(id.avatar, { size: 96, state: "done" });
  } catch { /* the kit is not mounted: initials */ }
  const words = role.split(/\s+/);
  const initials = (words.length > 1 ? words[0][0] + words[1][0] : role.slice(0, 2)).toUpperCase();
  return { roleKey, name, role, line, svg, initials, tone: tone(roleKey) };
}

/** What the card shows, from a chain. Pure, so a test can hold it. */
export function creditsCard(chain: Chain, title: string): CreditsCard {
  const credits = chain.credits.length ? chain.credits
    : [...new Set(chain.acts.map((a) => a.roleKey).filter((k): k is string => !!k))].map((roleKey) => ({ roleKey, line: "" }));
  const faces = credits.map((c) => face(c.roleKey, c.line));
  return {
    title: title.slice(0, 80) || "This run",
    faces: faces.slice(0, 8),
    agents: faces.filter((f) => f.roleKey).length,
    steps: chain.totals.steps,
    time: minutesLabel(chain.totals.minutes),
    cost: costLabel(chain.totals.cost),
    accountable: chain.accountable || "—",
  };
}

