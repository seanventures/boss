"use client";

/**
 * INFRA › ADD A SERVER (docs/product/wireframes/agentic-factory/3-add-a-server.html): a server
 * onboards itself, like a repo does.
 *
 *   1 one input        an address, user@host, an ssh line, or words. Kind pills: SSH server,
 *                      It dials in (the old Enroll), Cloud account.
 *   2 the probe        the network, before any credential: port open, host key.
 *   3 the gate         the ONE human step (a credential is on the list in Where people decide),
 *                      amber and set apart: run BOSS's one line, or use a key you have (write-only
 *                      to the vault, or a stored key). No "I ran it": the agent sees the key land.
 *   4 Agent actions    the machine, what was seen so far, the role, the steps.
 *   5 six boxes        Access · Name · Set up · Role · Watch · Owner, each with a popover; every old
 *                      Host-sheet field is there, prefilled.
 *   6 one primary      Add and set up (host:manage, step-up), then the server page's diagram.
 *
 * APP-INDEPENDENT: a server gets a ROLE and never an app; each app picks servers on its own
 * Project › Deployments page. "Fill it in myself" keeps the old sheets (Host, Enroll, Hardware
 * account).
 */
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  CheckCircle, Cloud, Eye, GearSix, HardDrives, Key, PencilSimple, PlugsConnected, ShieldCheck, Tag, Terminal,
  UserCircle, Wrench,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { GatedButton } from "../../components/gated";
import { enrollCommand, enrollHardwareAgent, explainRawSecret, registerHardwareProvider } from "../../lib/hardware";
import {
  createServer, KIND_WORDS, readServer, ROLE_LINE, ROLE_WORDS, STORES,
  type AccessMode, type Role, type ServerKind, type ServerRead,
} from "../../lib/add-server";
import { afterWrite } from "./infra-actions";
import { pasteKey } from "./cloud-actions";
import { CopyButton, ErrorLine } from "./infra-util";
import { Sheet } from "./kit";
import "./add-server.css";

type Box = "access" | "name" | "setup" | "role" | "watch" | "owner";
const PAUSE_MS = 450;

export type AddServerProps = {
  projectId: string; actor: string; initialText?: string;
  onClose: () => void;
  /** The server is recorded: land on its page (the host drawer, setting itself up). */
  onAdded: (hostId: string) => void;
  /** "Fill it in myself": the old sheet for this kind. */
  onManual: (kind: ServerKind) => void;
  /** A cloud account was connected: open it. */
  onCloud?: () => void;
};

/** The words for a refusal, never JSON. */
const words = (e: string) => e.replace(/^Not added: /, "Not added: ");

export function AddServerSheet({ projectId, actor, initialText = "", onClose, onAdded, onManual, onCloud }: AddServerProps) {
  const boxId = useId();
  const [text, setText] = useState(initialText);
  const [forced, setForced] = useState<ServerKind | null>(null);
  const [read, setRead] = useState<ServerRead | null>(null);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  // the person's edits win over what the read suggests
  const [edits, setEdits] = useState<Partial<{ user: string; port: string; name: string; arch: string; identity: string;
    role: Role; portMin: string; portMax: string; maxApps: string; notes: string }>>({});
  const [backend, setBackend] = useState("database");
  const [mode, setMode] = useState<AccessMode>("boss_key");
  const [privateKey, setPrivateKey] = useState("");
  const [storedId, setStoredId] = useState("");
  const [setup, setSetup] = useState({ docker: true, exporter: true, caddy: true });
  const [watch, setWatch] = useState({ host_health: true, disk_pct: 80, prune_cache: true });
  const [open, setOpen] = useState<Box | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [enroll, setEnroll] = useState<string | null>(null);
  const [cloudSecret, setCloudSecret] = useState("");
  const [cloudRef, setCloudRef] = useState("");
  const kind: ServerKind = forced ?? read?.kind ?? "ssh";
  // THE INPUT HAS THE FOCUS (the dialog focuses its first control; the one input wins).
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  useEffect(() => {
    const t = window.setTimeout(() => inputRef.current?.focus(), 30);
    return () => window.clearTimeout(t);
  }, []);

  // ── the read: words + the network probe, once typing pauses ────────────────────────────
  const seq = useRef(0);
  useEffect(() => {
    const t = text.trim();
    if (!t) { setRead(null); setReadError(null); setReading(false); return undefined; }
    const ctl = new AbortController();
    const mine = ++seq.current;
    setReading(true);
    const timer = window.setTimeout(() => {
      void readServer(projectId, t, forced, ctl.signal).then((r) => {
        if (mine !== seq.current) return;
        setReading(false);
        if (r.ok) { setRead(r.data); setReadError(null); }
        else if (r.error) { setReadError(r.error); setRead(null); }
      });
    }, PAUSE_MS);
    return () => { window.clearTimeout(timer); ctl.abort(); };
  }, [projectId, text, forced]);

  const user = edits.user ?? read?.user ?? "root";
  const port = edits.port ?? String(read?.port ?? 22);
  const name = edits.name ?? read?.name ?? "";
  const role: Role = edits.role ?? read?.role ?? "staging";
  const address = read?.address ?? null;
  const probe = read?.probe ?? null;
  const identity = edits.identity ?? read?.identityFile ?? "";
  const plan = useMemo(() => {
    const lines = ["Read OS, CPU, memory, disk, arch"];
    const want = [setup.docker && "Docker", setup.exporter && "node-exporter", setup.caddy && "Caddy for TLS"].filter(Boolean) as string[];
    if (want.length) lines.push(`Install ${want.length > 1 ? `${want.slice(0, -1).join(", ")} and ${want[want.length - 1]}` : want[0]} (if missing)`);
    lines.push("Verify it can build and run a container");
    lines.push(role === "staging" ? "Add it to the staging pool, with its free ports"
      : role === "prod" ? "Offer it for production (each prod deploy still asks you)" : "Add it to the build pool");
    if (watch.host_health) lines.push("Start watching host health (disk, Docker, certs)");
    return lines;
  }, [setup, watch.host_health, role]);

  // ── what stops Add and set up, in words ───────────────────────────────────────────────
  const blocker = useMemo((): string | null => {
    if (kind === "ssh") {
      if (!text.trim()) return "Name the machine first: an address, user@host or an ssh line.";
      if (reading && !read) return "Reading it…";
      if (!address) return "No address in that yet: try user@host or an IP.";
      if (read && !read.outwardSsh) return "BOSS is set not to reach out over SSH on this install.";
      if (mode === "my_key" && !/PRIVATE KEY/.test(privateKey)) return "Paste the private key, or pick another way in.";
      if (mode === "stored_key" && !storedId) return "Pick a stored key.";
      if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535) return "The SSH port is a number from 1 to 65535.";
      return null;
    }
    if (kind === "dials_in") return name.trim() ? null : "Name the machine.";
    if (!(read?.provider || "").trim() && !text.trim()) return "Say which cloud: AWS, DigitalOcean or another.";
    if (!cloudSecret.trim() && !cloudRef.trim()) return "Hand over the account's key, or a stored secret reference.";
    return null;
  }, [kind, text, reading, read, address, mode, privateKey, storedId, port, name, cloudSecret, cloudRef]);

  const submit = useCallback(async () => {
    if (busy) return;
    if (blocker) { setError(blocker); return; }
    setError(null);
    setBusy(true);
    try {
      if (kind === "ssh" && address) {
        const r = await createServer({
          project_id: projectId, actor, address, ssh_user: user.trim() || "root", ssh_port: Number(port) || 22, name: name.trim(),
          arch: edits.arch?.trim() || undefined, admin_identity_file: identity.trim() || undefined, role,
          port_min: edits.portMin ? Number(edits.portMin) : undefined, port_max: edits.portMax ? Number(edits.portMax) : undefined,
          max_apps: edits.maxApps ? Number(edits.maxApps) : undefined,
          access: { mode, backend, private_key: mode === "my_key" ? privateKey : undefined, secret_ref_id: mode === "stored_key" ? storedId : undefined },
          setup, watch, probe: probe ? { address: probe.address, port: probe.port, port_open: probe.portOpen, host_key: probe.hostKey } : undefined,
        });
        setPrivateKey(""); // sent: never kept in the browser
        if (!r.ok) { setError(words(r.error)); return; }
        afterWrite();
        onAdded(String(r.data.host_id ?? ""));
        return;
      }
      if (kind === "dials_in") {
        const cmd = enrollCommand(await enrollHardwareAgent({ project_id: projectId, actor, name: name.trim() }));
        if (!cmd) { setError("No enroll command came back. Nothing ran."); return; }
        setEnroll(cmd);
        afterWrite();
        return;
      }
      const provider = read?.provider || "generic";
      if (cloudSecret.trim()) {
        const r = await pasteKey(projectId, actor, { provider, account_label: name.trim() || provider, secret_value: cloudSecret });
        const secret = cloudSecret;
        setCloudSecret("");
        if (!r.ok) { setError(r.error.split(secret).join("[redacted]")); return; }
      } else {
        const raw = explainRawSecret(cloudRef);
        if (raw) { setError(raw); return; }
        await registerHardwareProvider({ project_id: projectId, actor, name: name.trim() || provider, provider, secret_ref: cloudRef.trim() });
      }
      afterWrite();
      onCloud?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [busy, blocker, kind, address, projectId, actor, user, port, name, edits, identity, role, mode, backend, privateKey, storedId,
      setup, watch, probe, onAdded, read, cloudSecret, cloudRef, onCloud]);

  const keyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void submit(); }
  };
  const seen = probe ? [
    { k: "port", ok: probe.portOpen, label: probe.portOpen ? `SSH ${probe.port}` : `SSH ${probe.port} closed` },
    ...(probe.portOpen ? [{ k: "web", ok: true, label: Object.entries(probe.web).filter(([, v]) => v === "free").map(([p]) => p).join(" · ")
      ? `${Object.entries(probe.web).filter(([, v]) => v === "free").map(([p]) => p).join(" · ")} free` : "80 · 443 in use" }] : []),
  ] : [];
  const primaryLabel = kind === "ssh" ? "Add and set up" : kind === "dials_in" ? "Add and get the line" : "Connect the account";

  return (
    <Sheet title="Add a server" testId="as-sheet" onClose={onClose} wide>
      <div className="as" onKeyDown={keyDown}>
        {/* 1 · the one input */}
        <label className="as-input" htmlFor={boxId} data-testid="as-input-box">
          <span className="as-input-ico" aria-hidden><HardDrives size={20} /></span>
          <textarea id={boxId} ref={inputRef} data-autofocus className="as-text" rows={3} value={text} spellCheck={false} autoComplete="off"
                    placeholder={"ubuntu@203.0.113.40\nA staging box."} data-testid="as-input"
                    aria-label="An address, user@host, an ssh line, or words"
                    onChange={(e) => { setText(e.target.value); setConfirmed(false); setError(null); }} />
          <span className="as-input-foot">
            <span>an address, <code>user@host</code>, an ssh line, or “a cloud box for staging”</span>
            <span className="as-faint">Never paste a key here: you hand it over below</span>
          </span>
        </label>
        <div className="as-kinds-row">
          <div className="as-pills" role="radiogroup" aria-label="Kind" data-testid="as-kinds">
            {(["ssh", "dials_in", "cloud"] as ServerKind[]).map((k) => (
              <button key={k} type="button" role="radio" aria-checked={kind === k} className="as-pill" data-testid={`as-kind-${k}`}
                      title={k === "ssh" ? "BOSS reaches it over SSH" : k === "dials_in" ? "The hardware agent on it dials out to BOSS (Enroll)" : "A provider account BOSS may place machines in"}
                      onClick={() => { setForced(k); setError(null); }}>
                {k === "ssh" ? <Terminal size={13} aria-hidden /> : k === "dials_in" ? <PlugsConnected size={13} aria-hidden /> : <Cloud size={13} aria-hidden />}
                {KIND_WORDS[k]}
              </button>
            ))}
          </div>
          <span className="as-probe" data-testid="as-probe" aria-live="polite">
            {reading ? <span className="as-faint">{kind === "ssh" ? "Probing…" : "Reading…"}</span>
              : kind !== "ssh" ? <span className="as-faint">{kind === "dials_in" ? "No inbound port needed: it dials out" : "The key goes through the gate below"}</span>
              : readError ? <span className="inf-error" data-testid="as-read-error">{readError}</span>
              : probe ? (
                <span>Auto-detected: user <b>{user}</b>, port <b>{probe.port}</b> {probe.portOpen ? "open" : "not answering"}
                  {probe.hostKey ? <>, host key <code data-testid="as-hostkey" title={probe.hostKey}>{shortKey(probe.hostKey)}</code></> : null}</span>
              ) : text.trim() ? <span className="as-faint">No address in that yet</span> : <span className="as-faint">Waiting for an address</span>}
          </span>
        </div>

        {/* 3 · the gate: the one thing only you can do */}
        <section className="as-gate" aria-labelledby={`${boxId}-gate`} data-testid="as-gate">
          <header className="as-gate-head">
            <span className="as-hand" aria-hidden>✋</span>
            <span>
              <h3 id={`${boxId}-gate`}>Let BOSS in · the one thing only you can do</h3>
              <span className="as-faint">A credential is on the list (Secrets and credentials). Pick one way; the agent does the rest.</span>
            </span>
          </header>
          {kind === "ssh" ? (
            <div className="as-gate-opts" role="radiogroup" aria-label="How BOSS gets in">
              <button type="button" role="radio" aria-checked={mode === "boss_key"} className="as-opt" data-testid="as-gate-line"
                      onClick={() => setMode("boss_key")}>
                <span className="as-opt-h"><Terminal size={14} aria-hidden /> Run one line on the server</span>
                <code className="as-code" data-testid="as-gate-line-preview">echo &quot;ssh-ed25519 … boss@{name || "server"}&quot; &gt;&gt; ~/.ssh/authorized_keys</code>
                <span className="as-faint">Adds only BOSS&apos;s public key for <b>{user}</b>; nothing runs. BOSS makes the key when you add, the line waits on the next screen, and the agent sees it land. No “I ran it”.</span>
              </button>
              <div role="radio" aria-checked={mode !== "boss_key"} tabIndex={0} className="as-opt" data-testid="as-gate-key"
                   onClick={() => { if (mode === "boss_key") setMode(storedId ? "stored_key" : "my_key"); }}
                   onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && e.target === e.currentTarget) { e.preventDefault(); setMode("my_key"); } }}>
                <span className="as-opt-h"><Key size={14} aria-hidden /> Use a key I already have</span>
                <textarea className="as-secret" rows={2} value={privateKey} spellCheck={false} autoComplete="off"
                          placeholder="paste a private key · write-only · stored in the vault" data-testid="as-private-key"
                          aria-label="Private key, write-only"
                          onFocus={() => setMode("my_key")}
                          onChange={(e) => { setPrivateKey(e.target.value); setMode("my_key"); setStoredId(""); }} />
                <span className="as-stored">
                  <span className="as-faint">Or pick a stored one:</span>
                  {read?.storedKeys.length ? read.storedKeys.map((k) => (
                    <button key={k.id} type="button" className="as-chip" aria-pressed={mode === "stored_key" && storedId === k.id}
                            data-testid={`as-stored-${k.id}`} title={k.fingerprint ?? k.name}
                            onClick={(e) => { e.stopPropagation(); setStoredId(k.id); setMode("stored_key"); setPrivateKey(""); }}>{k.name}</button>
                  )) : <span className="as-faint" data-testid="as-stored-none">none stored yet</span>}
                </span>
              </div>
            </div>
          ) : kind === "dials_in" ? (
            <div className="as-gate-opts as-one">
              <div className="as-opt" aria-checked data-testid="as-gate-enroll">
                <span className="as-opt-h"><Terminal size={14} aria-hidden /> Run one line on the machine</span>
                {enroll ? (
                  <>
                    <code className="as-code" data-copy="data" data-testid="as-enroll-command">{enroll}</code>
                    <span className="as-row"><CopyButton text={enroll} label="Copy the line" testId="as-enroll-copy" />
                      <span className="as-faint">It installs the hardware agent, which dials out to BOSS; the machine shows up in Fleet.</span></span>
                  </>
                ) : <span className="as-faint">BOSS makes a one-time enroll line when you add. The machine dials out; no port is opened.</span>}
              </div>
            </div>
          ) : (
            <div className="as-gate-opts">
              <div className="as-opt" aria-checked={!!cloudSecret} data-testid="as-gate-cloud-key">
                <span className="as-opt-h"><Key size={14} aria-hidden /> Paste the account&apos;s API key</span>
                <input type="password" className="as-secret-in" autoComplete="new-password" value={cloudSecret} data-testid="as-cloud-secret"
                       placeholder="write-only · stored in the vault" aria-label="Account API key, write-only"
                       onChange={(e) => { setCloudSecret(e.target.value); setCloudRef(""); }} />
              </div>
              <div className="as-opt" aria-checked={!!cloudRef} data-testid="as-gate-cloud-ref">
                <span className="as-opt-h"><ShieldCheck size={14} aria-hidden /> Use a stored secret</span>
                <input className="as-secret-in" autoComplete="off" spellCheck={false} value={cloudRef} data-testid="as-cloud-ref"
                       placeholder="env:BOSS_AWS_SECRET (a reference, never the secret)" aria-label="Secret reference"
                       onChange={(e) => { setCloudRef(e.target.value); setCloudSecret(""); }} />
              </div>
            </div>
          )}
        </section>

        {/* 4 · Agent actions */}
        <section className="as-card" data-confirmed={confirmed || undefined} aria-labelledby={`${boxId}-aa`} data-testid="as-agent-actions">
          <header className="as-card-head">
            <AgentAvatar agent="infra" size={28} aiBadge={false} tip="Infra agent" />
            <span className="as-card-title">
              <h3 id={`${boxId}-aa`}>Agent actions</h3>
              <span className="as-faint">Infra agent · from the network now; the rest fills the moment the key lands</span>
            </span>
            <span className="as-tag" data-tone={kind === "ssh" ? "amber" : "accent"} data-testid="as-aa-state">
              {kind === "ssh" ? "waiting for the key" : kind === "dials_in" ? "waiting for it to dial in" : "waiting for the key"}</span>
          </header>
          <div className="as-card-body">
            <dl className="as-facts">
              <dt>The machine</dt>
              <dd data-testid="as-aa-machine">{kind === "cloud" ? <b>{read?.provider ? read.provider.toUpperCase() : "A cloud account"}</b>
                : <><b>{address ?? "—"}</b>{name ? <> · name <b className="as-u">{name}</b></> : null}</>}</dd>
              <dt>Seen so far</dt>
              <dd className="as-chips" data-testid="as-aa-seen">
                {seen.map((c) => <span key={c.k} className="as-chip" data-tone={c.ok ? "green" : "red"}>{c.label}</span>)}
                <span className="as-chip">OS, CPU, disk: after the key</span>
              </dd>
              <dt>What it&apos;s for</dt>
              <dd data-testid="as-aa-role"><b>{ROLE_WORDS[role]}</b> · <span className="as-u">{ROLE_LINE[role]}</span></dd>
            </dl>
            <div className="as-then">
              <span className="as-label">Then the agent will</span>
              <ol className="as-steps" data-testid="as-aa-steps">{plan.map((l) => <li key={l}>{l}</li>)}</ol>
            </div>
          </div>
          <footer className="as-card-foot">
            <button type="button" className="as-btn" data-kind="green" aria-pressed={confirmed} data-testid="as-looks-right"
                    onClick={() => setConfirmed(true)}><CheckCircle size={14} aria-hidden /> {confirmed ? "Looks right ✓" : "Looks right"}</button>
            <button type="button" className="as-btn" data-testid="as-fix-it" onClick={() => { setConfirmed(false); setOpen("name"); }}>
              <PencilSimple size={14} aria-hidden /> Fix it</button>
            <span className="as-faint">Nothing is installed until the key lands. Prod targets stay a person&apos;s choice.</span>
          </footer>
        </section>

        {/* 5 · six boxes */}
        <section className="as-plan" aria-label="What BOSS will do" data-testid="as-boxes">
          <header className="as-plan-head">
            <h3>What BOSS will do</h3>
            <span className="as-faint">Picked from the address and your words · click any box to change it</span>
          </header>
          <div className="as-boxes">
            <BoxBtn k="access" open={open} setOpen={setOpen} icon={<Key size={13} aria-hidden />} label="Access"
                    value={mode === "boss_key" ? `${user} · BOSS key` : mode === "my_key" ? `${user} · your key` : `${user} · stored key`}
                    sub="you hand it over" amber />
            <BoxBtn k="name" open={open} setOpen={setOpen} icon={<Tag size={13} aria-hidden />} label="Name" auto={!edits.name}
                    value={name || "—"} sub={`arch: ${edits.arch || "detected"}`} />
            <BoxBtn k="setup" open={open} setOpen={setOpen} icon={<Wrench size={13} aria-hidden />} label="Set up" auto
                    value={[setup.docker && "Docker", setup.exporter && "exporter", setup.caddy && "Caddy"].filter(Boolean).join(" · ") || "nothing"}
                    sub="only what's missing" />
            <BoxBtn k="role" open={open} setOpen={setOpen} icon={<HardDrives size={13} aria-hidden />} label="Role"
                    value={ROLE_WORDS[role]} sub="staging · prod · builds" />
            <BoxBtn k="watch" open={open} setOpen={setOpen} icon={<Eye size={13} aria-hidden />} label="Watch"
                    value={watch.host_health ? "host health" : "not watched"} sub="Infra + SRE" />
            <BoxBtn k="owner" open={open} setOpen={setOpen} icon={<UserCircle size={13} aria-hidden />} label="Owner"
                    value="Infra agent" sub="fixes disk, Docker, certs" />
          </div>
          {open ? (
            <div className="as-pop" role="dialog" aria-label={open} data-testid={`as-pop-${open}`} data-box={open}>
              <header className="as-pop-h"><GearSix size={13} aria-hidden /> <b>{BOX_TITLE[open]}</b>
                <span className="ag-spacer" />
                <button type="button" className="inf-link" data-testid="as-pop-done" onClick={() => setOpen(null)}>Done</button></header>
              {open === "access" ? (
                <div className="as-pop-b">
                  <div className="inf-form-row">
                    <label className="inf-field"><span>SSH user</span>
                      <input value={user} onChange={(e) => setEdits({ ...edits, user: e.target.value })} autoComplete="off" spellCheck={false} data-testid="as-f-user" /></label>
                    <label className="inf-field"><span>SSH port</span>
                      <input value={port} inputMode="numeric" onChange={(e) => setEdits({ ...edits, port: e.target.value })} data-testid="as-f-port" /></label>
                  </div>
                  <label className="inf-field" title="The key your own ssh uses to reach it, for the ssh-from-here line; blank if plain ssh works">
                    <span>Your SSH key (for the line from your laptop)</span>
                    <input value={identity} placeholder="~/.ssh/id_ed25519" onChange={(e) => setEdits({ ...edits, identity: e.target.value })}
                           autoComplete="off" spellCheck={false} data-testid="as-f-identity" /></label>
                  <label className="inf-field" title="Where BOSS keeps the private half of this machine's key"><span>Store in</span>
                    <select value={backend} onChange={(e) => setBackend(e.target.value)} data-testid="as-f-store">
                      {STORES.map((s) => <option key={s.key} value={s.key} title={s.tip}>{s.label}</option>)}
                    </select></label>
                  <span className="as-faint">BOSS makes one key per server. To regenerate it later, use the server page; a new key stops any line already copied.
                    {" "}<a className="inf-link" href={`?area=infra&add=secret`} data-testid="as-secrets-link">Secrets ›</a></span>
                </div>
              ) : open === "name" ? (
                <div className="as-pop-b">
                  <div className="inf-form-row">
                    <label className="inf-field"><span>Name</span>
                      <input value={name} onChange={(e) => setEdits({ ...edits, name: e.target.value })} autoComplete="off" data-testid="as-f-name" /></label>
                    <label className="inf-field" title="Read from the machine when it is verified"><span>Arch</span>
                      <input value={edits.arch ?? ""} placeholder="Detected" onChange={(e) => setEdits({ ...edits, arch: e.target.value })} data-testid="as-f-arch" /></label>
                  </div>
                  <div className="inf-form-row">
                    <label className="inf-field"><span>App ports from</span>
                      <input value={edits.portMin ?? ""} placeholder="8000" inputMode="numeric" onChange={(e) => setEdits({ ...edits, portMin: e.target.value })} data-testid="as-f-portmin" /></label>
                    <label className="inf-field"><span>to</span>
                      <input value={edits.portMax ?? ""} placeholder="8999" inputMode="numeric" onChange={(e) => setEdits({ ...edits, portMax: e.target.value })} data-testid="as-f-portmax" /></label>
                    <label className="inf-field"><span>Max apps</span>
                      <input value={edits.maxApps ?? ""} placeholder="No limit" inputMode="numeric" onChange={(e) => setEdits({ ...edits, maxApps: e.target.value })} data-testid="as-f-maxapps" /></label>
                  </div>
                </div>
              ) : open === "setup" ? (
                <div className="as-pop-b">
                  {([["docker", "Docker", "BOSS ships apps as containers"], ["exporter", "node-exporter", "host metrics for Observability"],
                     ["caddy", "Caddy for TLS", "the official Caddy plugin, per app (Infra › Plugins)"]] as const).map(([k, l, tip]) => (
                    <label key={k} className="inf-check" title={tip}>
                      <input type="checkbox" checked={setup[k]} onChange={(e) => setSetup({ ...setup, [k]: e.target.checked })} data-testid={`as-f-setup-${k}`} /> {l}
                      <span className="as-faint"> · {tip}</span>
                    </label>
                  ))}
                  <span className="as-faint" data-testid="as-writes-words">{read?.hostWrites === false && read.hostWritesWords
                    ? read.hostWritesWords : "Only what is missing is installed; nothing is removed."}</span>
                  <a className="inf-link" href="?area=infra&view=plugins" data-testid="as-plugins-link">Plugins ›</a>
                </div>
              ) : open === "role" ? (
                <div className="as-pop-b" role="radiogroup" aria-label="Role">
                  {(["staging", "prod", "builds"] as Role[]).map((r) => (
                    <button key={r} type="button" role="radio" aria-checked={role === r} className="as-role" data-testid={`as-f-role-${r}`}
                            onClick={() => setEdits({ ...edits, role: r })}>
                      <b>{ROLE_WORDS[r]}</b><span className="as-faint">{ROLE_LINE[r]}</span>
                    </button>
                  ))}
                  <span className="as-faint">A server never moves an app: each app picks it on Project › Deployments. Prod is never picked for you.</span>
                </div>
              ) : open === "watch" ? (
                <div className="as-pop-b">
                  <label className="inf-check"><input type="checkbox" checked={watch.host_health} data-testid="as-f-watch"
                    onChange={(e) => setWatch({ ...watch, host_health: e.target.checked })} /> Watch host health (disk, Docker, certs)</label>
                  <label className="inf-field" title="The Infra agent says so when the disk passes this"><span>Disk alert at %</span>
                    <input value={String(watch.disk_pct)} inputMode="numeric" data-testid="as-f-disk"
                           onChange={(e) => setWatch({ ...watch, disk_pct: Math.max(0, Math.min(99, Number(e.target.value.replace(/\D/g, "")) || 0)) })} /></label>
                  <label className="inf-check"><input type="checkbox" checked={watch.prune_cache} data-testid="as-f-prune"
                    onChange={(e) => setWatch({ ...watch, prune_cache: e.target.checked })} /> Prune Docker build cache alone (cache, not data)</label>
                </div>
              ) : (
                <div className="as-pop-b">
                  <span className="as-owner"><AgentAvatar agent="infra" size={22} aiBadge={false} tip="Infra agent" /> <b>Infra agent</b> owns it once it is ready.</span>
                  <span className="as-faint">Alone: prunes build cache, re-reads health, re-verifies. Waits for you: retiring it, deploying prod, any outside DNS record.</span>
                  <a className="inf-link" href="?area=budget&tab=autonomy" data-testid="as-owner-link">Where people decide ›</a>
                </div>
              )}
            </div>
          ) : null}
        </section>

        {/* 6 · the one primary */}
        <footer className="as-foot">
          <span className="as-chip as-mono" title="The setup runs no model">≈ 4 min to ready · no model cost</span>
          <span className="as-faint"><ShieldCheck size={12} aria-hidden /> Retiring it or deploying prod still asks you</span>
          <span className="ag-spacer" />
          <button type="button" className="inf-link" data-testid="as-manual" onClick={() => onManual(kind)}>
            <PencilSimple size={12} aria-hidden /> Fill it in myself</button>
          {kind === "cloud" ? (
            <GatedButton permission="cloud_account:manage" className="as-primary" data-testid="as-submit"
                         disabled={busy} data-blocked={blocker ? "1" : undefined} title={blocker ?? "Connects the account; the key goes to the vault"}
                         onClick={() => void submit()}>
              {busy ? "Connecting…" : primaryLabel} <kbd className="as-kbd" aria-hidden>⌘↵</kbd>
            </GatedButton>
          ) : (
            <GatedButton permission="host:manage" className="as-primary" data-testid="as-submit"
                         disabled={busy} data-blocked={blocker ? "1" : undefined} title={blocker ?? "Records it, hands over access, and the agent sets it up"}
                         onClick={() => void submit()}>
              {busy ? "Adding…" : primaryLabel} <kbd className="as-kbd" aria-hidden>⌘↵</kbd>
            </GatedButton>
          )}
        </footer>
        <ErrorLine error={error} testId="as-refusal" />
      </div>
    </Sheet>
  );
}

const BOX_TITLE: Record<Box, string> = { access: "Access", name: "Name", setup: "Set up", role: "Role", watch: "Watch", owner: "Owner" };

function BoxBtn({ k, open, setOpen, icon, label, value, sub, auto, amber }: {
  k: Box; open: Box | null; setOpen: (b: Box | null) => void; icon: React.ReactNode; label: string; value: string; sub: string;
  auto?: boolean; amber?: boolean;
}) {
  return (
    <button type="button" className="as-box" aria-expanded={open === k} data-amber={amber || undefined} data-testid={`as-box-${k}`}
            onClick={() => setOpen(open === k ? null : k)} title={`${label}: ${value}`}>
      <span className="as-box-h">{icon}<span className="as-label">{label}</span>{auto ? <span className="as-auto">AUTO</span> : null}</span>
      <b className="as-box-v">{value}</b>
      <span className="as-box-s">{sub}</span>
    </button>
  );
}

function shortKey(fp: string): string {
  const [algo, rest] = fp.split(":");
  return rest && rest.length > 6 ? `${algo}:${rest.slice(0, 2)}…${rest.slice(-2)}` : fp;
}
