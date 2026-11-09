/**
 * A MEETING'S PRD, AS EVERY APPROVAL SURFACE SHOWS IT (BOSSAPP-929; engine services/prd_record.py).
 *
 * The decision payload (Approvals, the project's Needs you, the work drawer's story) carries
 * `prd`: the document to read in place, the panel that argued it (agenda, each agent's position,
 * dissent) and where it lives in the repository. One parser, so no surface reads it differently.
 */

export type PrdPosition = { seat: string; agent: string; position: string; points: string[]; debate: string; debatePoints: string[] };
export type PrdMeeting = {
  runId: string; title: string; href: string;
  agenda: { brief: string; participants: string[] };
  positions: PrdPosition[];
  dissent: { role: string | null; position: string; reason: string | null }[];
};
export type PrdRepo = {
  path: string; minutesPath: string; repository: string | null; committed: boolean;
  branch: string | null; url: string | null; minutesUrl: string | null;
};
export type PrdRecord = { title: string; markdown: string; fullPage: string | null; meeting: PrdMeeting | null; repo: PrdRepo | null };

type Obj = Record<string, unknown>;
const o = (v: unknown): Obj => (v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : {});
const a = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
const s = (v: unknown, d = ""): string => (typeof v === "string" ? v : d);
const sn = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

export function parsePrdRepo(v: unknown): PrdRepo | null {
  const r = o(v);
  if (!s(r.path)) return null;
  return {
    path: s(r.path), minutesPath: s(r.minutes_path), repository: sn(r.repository), committed: r.committed === true,
    branch: sn(r.branch), url: sn(r.url), minutesUrl: sn(r.minutes_url),
  };
}

export function parsePrdRecord(v: unknown): PrdRecord | null {
  if (!v || typeof v !== "object") return null;
  const x = o(v);
  const m = x.meeting && typeof x.meeting === "object" ? o(x.meeting) : null;
  const meeting: PrdMeeting | null = m ? {
    runId: s(m.run_id), title: s(m.title), href: s(m.href),
    agenda: { brief: s(o(m.agenda).brief), participants: a(o(m.agenda).participants).map((p) => s(p)).filter(Boolean) },
    positions: a(m.positions).map((p) => {
      const y = o(p);
      return { seat: s(y.seat), agent: s(y.agent, s(y.seat)), position: s(y.position), points: a(y.points).map((q) => s(q)).filter(Boolean),
               debate: s(y.debate), debatePoints: a(y.debate_points).map((q) => s(q)).filter(Boolean) };
    }).filter((p) => p.position || p.debate),
    dissent: a(m.dissent).map((d) => ({ role: sn(o(d).role), position: s(o(d).position), reason: sn(o(d).reason) })).filter((d) => d.position),
  } : null;
  const markdown = s(x.markdown);
  const repo = parsePrdRepo(x.repo);
  if (!markdown && !meeting && !repo) return null;
  return { title: s(x.title, "PRD"), markdown, fullPage: sn(x.full_page) ?? (meeting?.href || null), meeting, repo };
}

/** The repo link's words: the file on its branch once a build committed it, else where it will go. */
export function prdRepoWords(repo: PrdRepo): { label: string; detail: string } {
  if (repo.committed) {
    return { label: "In the repo", detail: `${repo.path}${repo.branch ? ` on ${repo.branch}` : ""}` };
  }
  return { label: "Not in the repo yet", detail: `The build commits it to ${repo.path} with its pull request` };
}
