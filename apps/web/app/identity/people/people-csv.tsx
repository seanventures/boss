"use client";

/**
 * PEOPLE AS CSV (identity-provider parity): export the directory as you see it, and invite many people from
 * a file. Import reads `email[,role][,groups]` (a header row is optional; groups are separated by
 * `;` or `|`), shows every row with what is wrong before anything is sent, then invites each
 * row through the same call as Invite people, one invitation per address, step-up once.
 */
import React, { useMemo, useState } from "react";
import { DownloadSimple, FileArrowUp, UploadSimple } from "@phosphor-icons/react";

import { showToast } from "../../client-forms";
import { useGuardedCall, useIdentity } from "../../../components/identity/identity-provider";
import { ErrorLine, Sheet } from "../../../components/identity/kit";
import { IdentityError, idPost } from "../../../lib/identity/api";
import type { AccessRole, Group, Person } from "../../../lib/identity/types";
import type { CsvInviteRow } from "../../../lib/identity/types-people";
import { grantReach } from "../../../lib/identity/grant-limit";

const hasMfa = (p: Person) => p.sign_in.passkeys > 0 || p.sign_in.totp || Boolean(p.sign_in.idp_mfa);

// ---- CSV text --------------------------------------------------------------------------------

/** One field, quoted when it must be; a leading =,+,-,@ is neutralised (spreadsheet injection). */
export function csvField(v: string): string {
  const safe = /^[=+\-@\t\r]/.test(v) ? `'${v}` : v;
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

export function peopleCsv(people: Person[]): string {
  const head = ["Name", "Email", "Status", "Access roles", "Groups", "MFA", "Last seen"];
  const rows = people.map((p) => [
    p.display_name, p.email, p.status === "pending" ? "invited" : p.status,
    p.bindings.filter((b) => !b.via_group).map((b) => (b.scope === "org" ? b.role_name : `${b.role_name} (${b.scope.replace(/^project:/, "")})`)).join("; "),
    p.groups.map((g) => g.name).join("; "),
    hasMfa(p) ? "on" : "off",
    p.last_seen_at ?? "never",
  ]);
  return [head, ...rows].map((r) => r.map(csvField).join(",")).join("\r\n") + "\r\n";
}

/** Split CSV text into rows of fields (quotes, doubled quotes, CRLF). */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i += 1; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i += 1;
      row.push(field); field = "";
      if (row.some((f) => f.trim())) rows.push(row);
      row = [];
    } else field += c;
  }
  row.push(field);
  if (row.some((f) => f.trim())) rows.push(row);
  return rows;
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/**
 * Rows to invite, each checked: a valid address, not already here, not twice in the file, a role
 * that exists and that you hold all of, groups that exist and are managed in BOSS.
 */
export function readInviteCsv(text: string, ctx: { roles: AccessRole[]; groups: Group[]; people: Person[]; held: ReadonlySet<string> | null; defaultRole: string }): CsvInviteRow[] {
  const rows = parseCsv(text.replace(/^﻿/, ""));
  if (!rows.length) return [];
  const first = rows[0].map((f) => f.trim().toLowerCase());
  const header = first.some((f) => f === "email" || f === "e-mail");
  const col = (names: string[], fallback: number) => {
    const i = header ? first.findIndex((f) => names.includes(f)) : -1;
    return header ? i : fallback;
  };
  const ei = col(["email", "e-mail"], 0);
  const ri = col(["role", "access role", "access roles"], 1);
  const gi = col(["group", "groups"], 2);
  const seen = new Set<string>();
  const known = new Set(ctx.people.map((p) => p.email.toLowerCase()));
  return rows.slice(header ? 1 : 0).map((r, n) => {
    const email = (r[ei] ?? "").trim();
    const roleText = (ri >= 0 ? r[ri] ?? "" : "").trim();
    const groupText = (gi >= 0 ? r[gi] ?? "" : "").trim();
    const role = roleText
      ? ctx.roles.find((x) => x.key.toLowerCase() === roleText.toLowerCase() || x.name.toLowerCase() === roleText.toLowerCase())
      : ctx.roles.find((x) => x.key === ctx.defaultRole);
    const groupNames = groupText ? groupText.split(/[;|]/).map((g) => g.trim()).filter(Boolean) : [];
    const groups = groupNames.map((g) => ctx.groups.find((x) => x.name.toLowerCase() === g.toLowerCase() || x.id === g));
    let problem: string | null = null;
    if (!EMAIL.test(email)) problem = "Not an email address";
    else if (known.has(email.toLowerCase())) problem = "Already here";
    else if (seen.has(email.toLowerCase())) problem = "Twice in the file";
    else if (!role || role.system) problem = `No role called “${roleText}”`;
    else if (role.key === "owner") problem = "Owners are not invited in bulk";
    else if (!grantReach(ctx.held, role).ok) problem = `You don't hold all of ${role.name}`;
    else {
      const bad = groupNames.find((_, i) => !groups[i]);
      if (bad) problem = `No group called “${bad}”`;
      else if (groups.some((g) => g && g.source !== "manual")) problem = "Synced groups are set by your identity provider";
    }
    seen.add(email.toLowerCase());
    return { line: n + (header ? 2 : 1), email, role: role?.key ?? roleText, groups: groups.flatMap((g) => (g ? [g.id] : [])), problem };
  });
}

// ---- the controls ----------------------------------------------------------------------------

export function ExportCsvButton({ people }: { people: Person[] }) {
  return (
    <button type="button" className="idn-btn sm" title="Download everyone shown as a spreadsheet file" data-testid="people-export"
      disabled={!people.length}
      onClick={() => {
        const blob = new Blob([peopleCsv(people)], { type: "text/csv;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `people-${new Date().toISOString().slice(0, 10)}.csv`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }}>
      <DownloadSimple size={14} aria-hidden /> Export
    </button>
  );
}

type Result = { email: string; ok: boolean; note: string };

export function ImportSheet({ roles, groups, people, onClose, onDone }: {
  roles: AccessRole[]; groups: Group[]; people: Person[]; onClose: () => void; onDone?: () => void;
}) {
  const { org } = useIdentity();
  const guarded = useGuardedCall();
  const [text, setText] = useState("");
  const [file, setFile] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<Result[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rows = useMemo(() => readInviteCsv(text, { roles, groups, people, held: org?.permissions ?? null, defaultRole: "operator" }),
    [text, roles, groups, people, org]);
  const good = rows.filter((r) => !r.problem);

  const send = async () => {
    setBusy(true);
    setError(null);
    const out: Result[] = [];
    try {
      await guarded("Inviting people grants them access.", async () => {
        for (const r of good) {
          if (out.some((x) => x.email === r.email)) continue;
          try {
            const res = await idPost<{ emailed?: boolean; link?: string | null }>("/v1/identity/users", {
              email: r.email, bindings: [{ role_key: r.role, scope_type: "org" }], group_ids: r.groups,
            });
            out.push({ email: r.email, ok: true, note: res.emailed ? "Emailed" : "Link made: copy it from People" });
          } catch (e) {
            // A step-up refusal stops the run (the sheet asks once, then this resumes after it).
            if (e instanceof IdentityError && e.stepUp) throw e;
            out.push({ email: r.email, ok: false, note: e instanceof IdentityError ? e.message : "Not sent" });
          }
        }
      });
    } catch (e) {
      setError(e instanceof IdentityError ? e.message : "Not sent");
    } finally {
      setBusy(false);
      if (out.length) {
        setResults(out);
        onDone?.();
        const n = out.filter((x) => x.ok).length;
        showToast(n === out.length ? "success" : "error", `${n} of ${out.length} invited`);
      }
    }
  };

  if (results) {
    return (
      <Sheet title="Import finished" icon={<FileArrowUp size={18} aria-hidden />} onClose={onClose} wide testId="import-results">
        <div className="idn-scroll">
          <table className="idn-t" data-testid="import-results-table">
            <thead><tr><th>Email</th><th>Result</th></tr></thead>
            <tbody>
              {results.map((r) => (
                <tr key={r.email}><td data-copy="data">{r.email}</td><td className={r.ok ? "idn-ok" : "idn-bad"}>{r.note}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="idn-mf"><span className="idn-spacer" /><button type="button" className="idn-btn primary" onClick={onClose}>Done</button></div>
      </Sheet>
    );
  }

  return (
    <Sheet title="Invite from a file" icon={<FileArrowUp size={18} aria-hidden />} onClose={onClose} wide testId="import-sheet">
      <p className="idn-lede2" data-copy="prose">A CSV: one email per line, then an optional role and groups (split with ;). Nothing is sent until you check.</p>
      <div className="idn-field">
        <label htmlFor="import-file">File</label>
        <input id="import-file" type="file" accept=".csv,text/csv,text/plain" className="idn-input" data-testid="import-file"
          onChange={async (e) => {
            const f = e.target.files?.[0];
            if (!f) return;
            if (f.size > 1_000_000) { setError("Files over 1 MB are refused"); return; }
            setFile(f.name);
            setText(await f.text());
          }} />
      </div>
      <div className="idn-field">
        <label htmlFor="import-text">Or paste</label>
        <textarea id="import-text" className="idn-input" rows={4} placeholder={"email,role,groups\nana@acme.dev,operator,Release"} value={text}
          onChange={(e) => { setFile(null); setText(e.target.value); }} data-testid="import-text" />
      </div>
      {rows.length ? (
        <div className="idn-scroll idn-import-preview">
          <table className="idn-t" data-testid="import-preview">
            <thead><tr><th>Line</th><th>Email</th><th>Role</th><th>Check</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.line} data-ok={!r.problem}>
                  <td className="idn-dim">{r.line}</td>
                  <td data-copy="data">{r.email || "—"}</td>
                  <td data-copy="data">{roles.find((x) => x.key === r.role)?.name ?? r.role}{r.groups.length ? ` + ${r.groups.length} ${r.groups.length === 1 ? "group" : "groups"}` : ""}</td>
                  <td className={r.problem ? "idn-bad" : "idn-ok"}>{r.problem ?? "Ready"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <ErrorLine error={error} />
      <div className="idn-mf">
        <span className="idn-dim">{rows.length ? `${good.length} of ${rows.length} ready` : file ? "No rows found" : "Choose a file"}</span>
        <span className="idn-spacer" />
        <button type="button" className="idn-btn ghost" onClick={onClose}>Cancel</button>
        <button type="button" className="idn-btn primary" disabled={!good.length || busy} onClick={send} data-testid="import-send">
          <UploadSimple size={14} aria-hidden /> {busy ? "Inviting…" : `Invite ${good.length || ""}`.trim()}
        </button>
      </div>
    </Sheet>
  );
}
