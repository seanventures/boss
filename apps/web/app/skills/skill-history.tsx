"use client";

/**
 * A skill's History and Tests tabs (BOSSAPP-842, BOSSAPP-844), inside the skill drawer.
 *
 * History: every saved version, newest first — who (a person, an agent through an approved
 * change, the system, a source sync), when, and why — a diff against the version before, and
 * Restore (a NEW version that copies the old one) and Publish (put the agents that hold the skill
 * on that version; refused while its latest graded test run failed).
 *
 * Tests: saved cases, Run all against a version, and each case's latest output beside the
 * previous version's, graded Pass or Fail.
 */
import React, { useCallback, useEffect, useState } from "react";

import { GatedButton } from "../../components/gated";
import { apiWrite } from "../../lib/api-write";
import { showToast } from "../client-forms";
import { shortError } from "./kit-copy";

type Version = {
  id: string; version_no: number; author_kind: string; author_id: string; source_note: string;
  created_at: string | null; current: boolean; restored_from: string | null;
};
type Holder = { role_key: string; pinned_version_no: number | null };
type Run = { id: string; status: string; output: Record<string, unknown>; grade: string | null; notes: string };
type Case = { id: string; name: string; input: Record<string, unknown>; current: Run | null; previous: Run | null };
type TestsView = { version_no: number | null; previous_no: number | null; cases: Case[];
                   gate: { allowed: boolean; reason: string } | null };

const WHO: Record<string, string> = { person: "Person", agent: "Agent (approved)", system: "System", sync: "Source sync" };

async function supplyRead<T>(params: Record<string, string>): Promise<{ ok: boolean; data: T; error: string }> {
  const r = await fetch(`/api/skill-supply?${new URLSearchParams(params)}`, { cache: "no-store" });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, data: data as T, error: String((data as { error?: string })?.error ?? `HTTP ${r.status}`) };
}

async function supplyWrite(body: Record<string, unknown>) {
  const r = await apiWrite("/api/skill-supply", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, data, error: String(data?.error ?? `HTTP ${r.status}`) };
}

function when(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "";
}

export function SkillHistory({ projectId, actor, skillKey, onChanged }: {
  projectId: string; actor: string; skillKey: string; onChanged: () => void;
}) {
  const [versions, setVersions] = useState<Version[] | null>(null);
  const [holders, setHolders] = useState<Holder[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [diff, setDiff] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const r = await supplyRead<{ versions: Version[]; holders: Holder[] }>(
      { view: "versions", project_id: projectId, skill_key: skillKey });
    if (!r.ok) { showToast("error", shortError(r.error)); setVersions([]); return; }
    setVersions(r.data.versions ?? []); setHolders(r.data.holders ?? []);
  }, [projectId, skillKey]);
  useEffect(() => { void load(); }, [load]);

  const showDiff = async (v: Version) => {
    if (open === v.version_no) { setOpen(null); return; }
    setOpen(v.version_no); setDiff("");
    if (v.version_no <= 1) { setDiff("(the first version)"); return; }
    const r = await supplyRead<{ diff: string }>({ view: "diff", project_id: projectId, skill_key: skillKey,
                                                   from: String(v.version_no - 1), to: String(v.version_no) });
    setDiff(r.ok ? (r.data.diff || "(no change in content)") : shortError(r.error));
  };

  const act = async (action: "restore" | "publish", v: Version) => {
    setBusy(true);
    const r = await supplyWrite({ action, project_id: projectId, actor, skill_key: skillKey, version: v.version_no });
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    showToast("success", action === "restore" ? `Restored v${v.version_no} as a new version` : `Agents now run v${v.version_no}`);
    await load(); onChanged();
  };

  if (versions === null) return <div className="kit-faint">Loading…</div>;
  if (!versions.length) return <div className="kit-faint" data-testid="skill-history-empty">No versions yet. Save it once.</div>;
  return (
    <div data-testid="skill-history">
      {holders.some((h) => h.pinned_version_no) ? (
        <div className="kit-faint" data-testid="skill-history-pins">
          Pinned: {holders.filter((h) => h.pinned_version_no).map((h) => `${h.role_key} on v${h.pinned_version_no}`).join(", ")}
        </div>
      ) : null}
      <ul className="kit-used">
        {versions.map((v) => (
          <li key={v.id} data-testid={`skill-version-${v.version_no}`}>
            <div className="kit-row">
              <strong>v{v.version_no}</strong>
              {v.current ? <span className="kit-chip" data-tone="accent">Current</span> : null}
              <span className="kit-faint" data-tip={v.author_id}>{WHO[v.author_kind] ?? v.author_kind} · {when(v.created_at)}</span>
              <span className="kit-spacer" />
              <button type="button" className="kit-btn kit-btn-sm" data-testid={`skill-version-diff-${v.version_no}`}
                      onClick={() => void showDiff(v)}>{open === v.version_no ? "Hide" : "Diff"}</button>
              {!v.current ? (
                <GatedButton permission="skill:manage" type="button" className="kit-btn kit-btn-sm" disabled={busy}
                             data-testid={`skill-version-restore-${v.version_no}`} onClick={() => void act("restore", v)}>Restore</GatedButton>
              ) : null}
              <GatedButton permission="skill:manage" type="button" className="kit-btn kit-btn-sm" disabled={busy}
                           data-tip="Put every agent that holds it on this version"
                           data-testid={`skill-version-publish-${v.version_no}`} onClick={() => void act("publish", v)}>Publish</GatedButton>
            </div>
            {v.source_note ? <div className="kit-faint" data-user-text>{v.source_note}</div> : null}
            {open === v.version_no ? (
              <pre className="kit-diff" data-testid="skill-version-diff-text" data-user-text>{diff || "Loading…"}</pre>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function outputText(run: Run | null): string {
  if (!run) return "—";
  const out = run.output ?? {};
  const shown = (out.text ?? out.reason ?? out.error ?? out) as unknown;
  return typeof shown === "string" ? shown : JSON.stringify(shown, null, 1);
}

export function SkillTests({ projectId, actor, skillKey }: { projectId: string; actor: string; skillKey: string }) {
  const [view, setView] = useState<TestsView | null>(null);
  const [name, setName] = useState("");
  const [input, setInput] = useState("{}");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const r = await supplyRead<TestsView>({ view: "tests", project_id: projectId, skill_key: skillKey });
    if (!r.ok) { showToast("error", shortError(r.error)); return; }
    setView(r.data);
  }, [projectId, skillKey]);
  useEffect(() => { void load(); }, [load]);

  const write = async (body: Record<string, unknown>, done: string) => {
    setBusy(true);
    const r = await supplyWrite({ project_id: projectId, actor, skill_key: skillKey, ...body });
    setBusy(false);
    if (!r.ok) { showToast("error", shortError(r.error)); return false; }
    showToast("success", done); await load(); return true;
  };

  const addCase = async () => {
    let parsed: unknown;
    try { parsed = JSON.parse(input || "{}"); } catch { showToast("error", "Input must be JSON"); return; }
    if (await write({ action: "add_case", name: name.trim(), input: parsed }, "Case saved")) { setName(""); setInput("{}"); }
  };

  if (!view) return <div className="kit-faint">Loading…</div>;
  return (
    <div data-testid="skill-tests">
      <div className="kit-row">
        <span className="kit-faint">
          v{view.version_no ?? "?"}{view.previous_no ? ` beside v${view.previous_no}` : ""}
        </span>
        {view.gate && !view.gate.allowed ? (
          <span className="kit-chip" data-tone="danger" data-tip={view.gate.reason} data-testid="skill-tests-gate">Publish held</span>
        ) : null}
        <span className="kit-spacer" />
        <GatedButton permission="skill:invoke" type="button" className="kit-btn kit-btn-primary kit-btn-sm"
                     disabled={busy || !view.cases.length} data-testid="skill-tests-run"
                     onClick={() => void write({ action: "run_tests" }, "Ran every case")}>Run all</GatedButton>
      </div>
      {view.cases.length ? (
        <ul className="kit-used">
          {view.cases.map((c) => (
            <li key={c.id} data-testid={`skill-case-${c.name}`}>
              <div className="kit-row">
                <strong data-user-text>{c.name}</strong>
                {c.current?.grade ? <span className="kit-chip" data-tone={c.current.grade === "pass" ? "ok" : "danger"}>{c.current.grade === "pass" ? "Pass" : "Fail"}</span> : null}
                <span className="kit-spacer" />
                {c.current ? (
                  <>
                    <GatedButton permission="skill:manage" type="button" className="kit-btn kit-btn-sm" disabled={busy}
                                 data-testid={`skill-case-pass-${c.name}`}
                                 onClick={() => void write({ action: "grade", run_id: c.current?.id, grade: "pass" }, "Graded pass")}>Pass</GatedButton>
                    <GatedButton permission="skill:manage" type="button" className="kit-btn kit-btn-sm" disabled={busy}
                                 data-testid={`skill-case-fail-${c.name}`}
                                 onClick={() => void write({ action: "grade", run_id: c.current?.id, grade: "fail" }, "Graded fail")}>Fail</GatedButton>
                  </>
                ) : null}
                <GatedButton permission="skill:manage" type="button" className="kit-btn kit-btn-sm" disabled={busy}
                             aria-label={`Delete ${c.name}`}
                             onClick={() => void write({ action: "delete_case", case_id: c.id }, "Case deleted")}>×</GatedButton>
              </div>
              <div className="kit-side-by-side">
                <pre className="kit-diff" data-tip="This version" data-testid={`skill-case-now-${c.name}`} data-user-text>{outputText(c.current)}</pre>
                <pre className="kit-diff" data-tip="The version before" data-user-text>{outputText(c.previous)}</pre>
              </div>
            </li>
          ))}
        </ul>
      ) : <div className="kit-faint">No cases yet</div>}
      <div className="kit-row">
        <input type="text" value={name} placeholder="Case name" aria-label="Case name" data-testid="skill-case-name"
               onChange={(e) => setName(e.target.value)} />
        <input type="text" value={input} spellCheck={false} aria-label="Input (JSON)" data-testid="skill-case-input"
               onChange={(e) => setInput(e.target.value)} />
        <GatedButton permission="skill:manage" type="button" className="kit-btn kit-btn-sm" disabled={busy || !name.trim()}
                     data-testid="skill-case-add" onClick={() => void addCase()}>Add</GatedButton>
      </div>
    </div>
  );
}
