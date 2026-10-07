"use client";

/**
 * The `+ Add ▾` sheets (spec §4.3): Host (the old Add-a-VM form, one press: record the machine
 * and mint its key, so the next thing on screen is the key to authorise), Enroll machine (the
 * hardware agent's enroll command), Hardware account (hardware "Connect a provider": a secret
 * REFERENCE, never the secret). App target and Secret live in target-sheet.tsx and
 * secrets-sheet.tsx.
 */
import { GatedButton } from "../../components/gated";
import React, { useState } from "react";

import { enrollCommand, enrollHardwareAgent, explainRawSecret, registerHardwareProvider } from "../../lib/hardware";
import { showToast } from "../client-forms";
import { afterWrite, generateHostKey, registerHost } from "./infra-actions";
import { CopyButton, ErrorLine } from "./infra-util";
import { Sheet } from "./kit";

const val = (f: FormData, k: string) => String(f.get(k) ?? "").trim();

/** `Host`: Address, SSH user, SSH port, Name, Arch, Your SSH key → `Add host`. A blank name is
 *  the address; the key is stored durably (`database`) — `file` is lost on BOSS's next deploy. */
export function HostSheet({ projectId, actor, onClose, onAdded }: {
  projectId: string; actor: string; onClose: () => void; onAdded: (hostId: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title="Add host" testId="inf-host-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={async (e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        const address = val(f, "address");
        if (!address) { setError("Address"); return; }
        setBusy(true); setError(null);
        const reg = await registerHost(projectId, actor, {
          address, ssh_user: val(f, "ssh_user") || "root", ssh_port: Number(val(f, "ssh_port")) || 22,
          name: val(f, "name") || address, arch: val(f, "arch") || undefined,
          admin_identity_file: val(f, "admin_identity_file") || undefined,
        });
        const id = reg.ok ? String(reg.data.id ?? "") : "";
        if (!reg.ok || !id) { setBusy(false); setError(reg.ok ? "The API answered without a host" : reg.error); return; }
        const key = await generateHostKey(id, actor, "database");
        setBusy(false);
        afterWrite();
        if (!key.ok) {
          setError(`Added, but its key was not made: ${key.error}`);
          onAdded(id);
          return;
        }
        showToast("success", "Host added · set it up");
        onAdded(id);
      }}>
        <div className="inf-form-row">
          <label className="inf-field"><span>Address</span>
            <input name="address" required placeholder="192.0.2.202" autoComplete="off" spellCheck={false} data-testid="inf-host-address" /></label>
          <label className="inf-field"><span>SSH user</span>
            <input name="ssh_user" defaultValue="root" autoComplete="off" spellCheck={false} data-testid="inf-host-user" /></label>
          <label className="inf-field"><span>SSH port</span>
            <input name="ssh_port" type="number" min={1} max={65535} defaultValue="22" data-testid="inf-host-port" /></label>
        </div>
        <div className="inf-form-row">
          <label className="inf-field" title="Defaults to the address"><span>Name</span>
            <input name="name" placeholder="Optional" autoComplete="off" data-testid="inf-host-name" /></label>
          <label className="inf-field" title="Read from the machine when it is verified"><span>Arch</span>
            <input name="arch" placeholder="Detected" autoComplete="off" data-testid="inf-host-arch" /></label>
          <label className="inf-field" title="The key your own ssh uses to reach it; blank if plain ssh works">
            <span>Your SSH key</span>
            <input name="admin_identity_file" placeholder="~/.ssh/id_ed25519" autoComplete="off" spellCheck={false} data-testid="inf-host-identity" /></label>
        </div>
        <div className="inf-actions">
          <GatedButton permission="host:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-host-submit"
                  title="Records the machine and makes its key (Add VM and generate its key)">
            {busy ? "Adding…" : "Add host"}
          </GatedButton>
          <ErrorLine error={error} testId="inf-host-sheet-error" />
        </div>
      </form>
    </Sheet>
  );
}

/** `Enroll machine`: name a machine you already have → the one command to run on it. */
export function EnrollSheet({ projectId, actor, onClose }: { projectId: string; actor: string; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [command, setCommand] = useState<string | null>(null);
  return (
    <Sheet title="Enroll machine" testId="inf-enroll-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={async (e) => {
        e.preventDefault();
        const name = val(new FormData(e.currentTarget), "name");
        if (!name) { setError("Name the machine"); return; }
        setBusy(true); setError(null); setCommand(null);
        try {
          const cmd = enrollCommand(await enrollHardwareAgent({ project_id: projectId, actor, name }));
          if (!cmd) setError("No enroll command came back. Nothing ran.");
          else { setCommand(cmd); afterWrite(); }
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
        } finally { setBusy(false); }
      }}>
        <label className="inf-field" title="A new machine gets the same hardware agent as this computer">
          <span>Machine name</span>
          <input name="name" required placeholder="build-vm" autoComplete="off" data-testid="inf-enroll-name" /></label>
        <div className="inf-actions">
          <GatedButton permission="host:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-enroll-submit">
            {busy ? "Preparing…" : "Show enroll command"}
          </GatedButton>
          <ErrorLine error={error} />
        </div>
      </form>
      {command ? (
        <div className="inf-keyblock" data-testid="inf-enroll-command">
          <code className="inf-mono inf-code" data-copy="data" title="Run it on the machine">{command}</code>
          <CopyButton text={command} label="Copy command" />
        </div>
      ) : null}
    </Sheet>
  );
}

const PROVIDERS = [{ value: "aws", label: "AWS" }, { value: "digitalocean", label: "DigitalOcean" }, { value: "generic", label: "Other" }];

/** `Hardware account`: a provider account BOSS may place machines in, by secret reference. */
export function HwAccountSheet({ projectId, actor, onClose }: { projectId: string; actor: string; onClose: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <Sheet title="Hardware account" testId="inf-hwaccount-sheet" onClose={onClose}>
      <form className="inf-form" onSubmit={async (e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        const name = val(f, "name");
        const provider = val(f, "provider");
        const secretRef = val(f, "secret_ref");
        if (!name || !provider) { setError("Name and provider"); return; }
        const raw = explainRawSecret(secretRef);
        if (raw) { setError(raw); return; }
        setBusy(true); setError(null);
        try {
          await registerHardwareProvider({ project_id: projectId, actor, name, provider, secret_ref: secretRef });
          showToast("success", "Account registered");
          afterWrite();
          onClose();
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
        } finally { setBusy(false); }
      }}>
        <div className="inf-form-row">
          <label className="inf-field"><span>Name</span>
            <input name="name" required placeholder="Production" autoComplete="off" data-testid="inf-hwaccount-name" /></label>
          <label className="inf-field"><span>Provider</span>
            <select name="provider" defaultValue="aws" data-testid="inf-hwaccount-provider">
              {PROVIDERS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select></label>
        </div>
        <label className="inf-field" title="The name of a stored secret, such as env:BOSS_AWS_SECRET — never the secret">
          <span>Secret</span>
          <input name="secret_ref" required placeholder="env:BOSS_AWS_SECRET" autoComplete="off" spellCheck={false} data-testid="inf-hwaccount-secret" /></label>
        <div className="inf-actions">
          <GatedButton permission="host:manage" type="submit" className="ag-btn ag-btn-primary" disabled={busy} data-testid="inf-hwaccount-submit"
                  title="Nothing is created or changed on the provider">{busy ? "Registering…" : "Register account"}</GatedButton>
          <ErrorLine error={error} testId="inf-hwaccount-error" />
        </div>
      </form>
    </Sheet>
  );
}
