"use client";

/** Identity parts shared by several screens: sign-in summary, binding chips, locked panels. */
import React from "react";
import { LockSimple, ShieldCheck, Warning } from "@phosphor-icons/react";

import type { Binding, Person, SignInSummary } from "../../lib/identity/types";
import type { Permission } from "../../lib/identity/permissions.gen";
import { Perm, RoleChip } from "./kit";

export function signInWords(s: SignInSummary): { text: string; ok: boolean } {
  if (s.passkeys > 0) return { text: `passkey${s.passkeys > 1 ? ` ×${s.passkeys}` : ""}${s.totp ? " +1" : ""}`, ok: true };
  if (s.totp) return { text: "authenticator", ok: true };
  if (s.idp_mfa) return { text: s.idp_mfa, ok: true };
  return { text: "no MFA", ok: false };
}

export function SignIn({ s }: { s: SignInSummary }) {
  const w = signInWords(s);
  return (
    <span className={w.ok ? "idn-mut" : "idn-warn"} title={w.ok ? "Second factor on" : "No second factor"}>
      {w.ok ? <ShieldCheck size={14} className="idn-ok" aria-hidden /> : <Warning size={14} aria-hidden />} {w.text}
    </span>
  );
}

/** A person's direct and project bindings as chips (group-inherited ones are shown separately). */
export function BindingChips({ bindings, max = 2 }: { bindings: Binding[]; max?: number }) {
  const direct = bindings.filter((b) => !b.via_group);
  const shown = direct.slice(0, max);
  return (
    <span className="idn-row">
      {shown.map((b) => <RoleChip key={b.id} roleKey={b.role_key} name={b.role_name} scope={b.scope === "org" ? null : b.scope} />)}
      {direct.length > max ? <span className="idn-dim">+{direct.length - max}</span> : null}
      {!direct.length ? <span className="idn-dim">none</span> : null}
    </span>
  );
}

/** A panel the viewer cannot read: says which permission, does not pretend to be empty. */
export function Locked({ permission, what }: { permission: Permission; what: string }) {
  return (
    <div className="idn-card" data-testid={`locked-${what.toLowerCase().replace(/\s+/g, "-")}`}>
      <div className="idn-card-h"><h3>{what}</h3></div>
      <div className="idn-row idn-dim"><LockSimple size={15} aria-hidden /> Needs <Perm p={permission} sm /></div>
    </div>
  );
}

export function firstName(p: Pick<Person, "display_name">): string {
  return p.display_name.split(" ")[0];
}

export function topRole(p: Person): { key: string; name: string } | null {
  const order = ["owner", "admin", "operator", "auditor", "viewer"];
  const org = p.bindings.filter((b) => b.scope === "org" && !b.via_group);
  const sorted = [...org].sort((a, b) => order.indexOf(a.role_key) - order.indexOf(b.role_key));
  const b = sorted[0] ?? p.bindings[0];
  return b ? { key: b.role_key, name: b.role_name } : null;
}
