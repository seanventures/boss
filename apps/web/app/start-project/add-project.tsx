"use client";

/**
 * ADD A PROJECT — TWO DOORS (BOSSAPP-916; new-project-v2 wireframes a/b/c for the parts each door
 * shares). Operator, 2026-11-08: "I'd have to have a separate dialog for new and existing project
 * … I really wanted to build things locally before taking to git later … not miss any use cases
 * and also be very slick." So the sheet no longer guesses the kind from one input:
 *
 *   0 the doors        two cards — Start a new app · Bring existing code. `?new_project=idea`
 *                      opens the first, `import` the second, `1`/`sample` the choice (Try a
 *                      sample stays one press in the head of every screen).
 *   NEW APP            the idea (5 lines, grows to 12, ⤢ full screen) → Project brief (Helm's
 *                      rewrite; Looks right · Fix it · Rewrite) → Agent actions: six boxes, the
 *                      Code box being WHERE THE CODE LIVES — on this machine (local first; the
 *                      default with no GitHub account), a new GitHub repo BOSS makes when you press
 *                      Create (917), or a folder in a repo you have (a monorepo).
 *   EXISTING CODE      one search over GitHub repos (the same-repo rules: Open › · Add work ·
 *                      separate anyway), folders on this machine (the import directory) and
 *                      folders on servers; a folder with a GitHub remote offers Keep using GitHub
 *                      or Copy it here (local first, history kept) → Repo summary → onboarding.
 *   Create and start   ONE primary (⌘↵): POST /v1/add-project checks everything and commits it
 *                      whole, then starts onboarding (code you have) or the plan (an idea), and
 *                      lands on the project. A refusal opens the same checklist inline and the
 *                      button becomes Start anyway.
 *
 * Models and GitHub accounts are global: shown read-only, linked to Repo and models.
 */
import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { useAreaKeydown } from "../../lib/area-keys";
import { createPortal } from "react-dom";
import {
  ArrowLeft, ArrowRight, ArrowsIn, ArrowsOut, ArrowsVertical, CaretRight, ChatsCircle, CircleNotch, DotsThree, Flask,
  FolderOpen, FolderPlus, GitBranch, GithubLogo, HardDrives, Laptop, Lightbulb, Play, Plus, RocketLaunch, ShieldCheck,
  Sparkle, Trash, User, X, DotsSixVertical, Check, ClockCounterClockwise,
} from "@phosphor-icons/react";

import { AgentAvatar } from "../agent-avatar";
import { consoleHref } from "../console-href";
import { GatedButton, useGate } from "../../components/gated";
import { BoxPop } from "../../components/box-pop";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { notePress } from "../../lib/user-gesture";
import { IdentityError } from "../../lib/identity/api";
import { withConsent } from "../../lib/identity/consent";
import { openPlan } from "../../lib/converse-doors";
import { useCatalogue } from "../../components/v2/templates/catalogue";
import { adapterLanguage, useStackAdapters } from "../../components/v2/templates/stack-warning";
import { templatesApi } from "../../lib/templates";
import { ageShort, hostFolders, type FolderFacts, type FolderHost } from "../../lib/host-folder";
import { accountsOf, allPresets, createFresh, installWideRefusal, nameFromIdea, projectPageHref, type Preset } from "../../lib/start-project";
import {
  addProjectApi, bringChoices, defaultWhere, detect, doorOf, estimateTip, estimateWords, hostPort, lineCount, nameFromTitle, nextFreeName, repoSlug,
  type Brief, type BriefField, type Bring, type Door, type Kind, type LocalFolder, type Preview, type RepoCheck, type RepoRead, type Understanding,
} from "../../lib/add-project";
import { normalizeChannel } from "../workflows/compose-plan";
import { ReadinessPanel, asReadiness, unaccepted, type Readiness } from "../workflow-readiness-ui";
import { useRegistry } from "./import-repo";
import { InUseBlock, InUseTag, PlanBoxes, egressOf, ownFields, usedBy, type InUseActions, type PlanState } from "./add-project-plan";
import { BriefCard, Marked, type BriefRow } from "../projects/brief-card";
import { SAMPLE, knowledgePresetHref } from "./start-sheet";
import "./start-project.css";
import "./add-project.css";

const EMPTY_PICKS = { stack: null, connectors: [], workflow: null };
const MIN_WORDS = 3;
/** Helm reads the words once typing pauses this long (and on ↵, and on Rewrite). */
const PAUSE_MS = 1600;

function initialPlan(): PlanState {
  return { captain: "engineering_captain", crew: null, stack: "", connectors: [], secrets: {}, workflow: "", inputs: {},
           layout: "one", repoIds: [], staging: null, stagingTouched: false, name: "", named: false, tell: "",
           when: ["needs_you", "pr_ready"], audience: "", doneWhen: [], readFirst: [], talksTo: [], mapRules: false, egressOk: false,
           where: "local", newRepo: { owner: "", name: "", private: true } };
}

const DRAFT_KEY = "boss.add-project.draft";
function readDraft(): string {
  try { return window.sessionStorage.getItem(DRAFT_KEY) ?? ""; } catch { return ""; }
}
function writeDraft(text: string): void {
  try {
    if (text.trim()) window.sessionStorage.setItem(DRAFT_KEY, text);
    else window.sessionStorage.removeItem(DRAFT_KEY);
  } catch { /* no storage: the draft is not kept */ }
}
function dropDraft(): void { writeDraft(""); }

/** A refusal in words. `doing` says what was being done, so a closed "Confirm it's you" reads as
 *  what did NOT happen ("Not created …"), never as a create when the press was something else. */
function refusalText(e: unknown, doing: "create" | "read" = "read"): string {
  if (e instanceof IdentityError) {
    if (e.code === "step_up_cancelled") {
      return doing === "create" ? "Not created: “Confirm it’s you” was closed. Press Create and start to confirm and go on."
        : "Not read: “Confirm it’s you” was closed.";
    }
    return typeof e.body.message === "string" && e.body.message ? e.body.message : e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

/** "ledger-api" → "Ledger api": a folder's name as a project name. */
function nameFromFolder(path: string): string {
  const last = path.replace(/\/+$/, "").split("/").pop() ?? "";
  return last.replace(/[-_.]+/g, " ").trim().replace(/^./, (c) => c.toUpperCase());
}

type PickedFolder = { source: "host" | "local"; hostId?: string; host?: string; path: string };

export function AddProjectSheet({ projectId, homeId, startKind = "idea", initialText = "", onClose, onForm, go = (href) => window.location.assign(href) }: {
  /** The project open now: the org's registry, hosts, workflows and model route are read through it. */
  projectId: string; homeId: string;
  /** Which door opens: an idea (Start a new app), a repo or folder (Bring existing code), or the
   *  two doors ("choose"; "sample" also lights Try a sample). */
  startKind?: Kind | "sample" | "choose";
  /** A brief handed over by a door ("Build it"): written in, read by Helm, never started unseen. */
  initialText?: string;
  onClose: () => void;
  /** More ways › Blank or Knowledge-graph project: the name-and-description form. */
  onForm: () => void;
  go?: (href: string) => void;
}) {
  const titleId = useId();
  const guarded = useGuardedCall();
  const canCreate = useGate("project:create");
  const [door, setDoorState] = useState<Door>(() => doorOf(startKind, !!initialText));
  // ESC NEVER LOSES WORDS: the idea is kept for this tab until Create and start succeeds or the
  // person presses Clear; opening the sheet again brings it back, said in one line.
  const [restored] = useState(() => (initialText ? "" : readDraft()));
  const [ideaText, setIdeaText] = useState(initialText || restored);
  const [draftShown, setDraftShown] = useState(!!restored);
  useEffect(() => { writeDraft(ideaText); }, [ideaText]);
  // Existing code has its own words (a repo, a URL, a folder, and lines under it for Helm).
  const [codeText, setCodeText] = useState("");
  const text = door === "existing" ? codeText : ideaText;
  const setText = door === "existing" ? setCodeText : setIdeaText;
  // INSIDE "Bring existing code" the words say repo or folder (owner/repo, a URL, /path); a
  // picked row or a source chip forces it. "Start a new app" is always an idea: nothing guessed.
  const [forced, setForced] = useState<Kind | null>(null);
  const det = useMemo(() => detect(text), [text]);
  const kind: Kind = door === "existing" ? (forced ?? (det.kind === "folder" ? "folder" : "repo")) : "idea";
  const [full, setFull] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** Where a refusal is fixed ("Connect GitHub ›" for a repository the org cannot reach yet). */
  const [fixHref, setFixHref] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  const [startAnyway, setStartAnyway] = useState<string[]>([]);
  const kept = useRef<string | null>(null);
  const [plan, setPlanState] = useState<PlanState>(initialPlan);
  const touched = useRef<Set<keyof PlanState>>(new Set());
  const set = useCallback((patch: Partial<PlanState>) => {
    for (const k of Object.keys(patch) as (keyof PlanState)[]) touched.current.add(k);
    setPlanState((p) => ({ ...p, ...patch }));
  }, []);
  const auto = useCallback((patch: Partial<PlanState>) => {
    setPlanState((p) => {
      const next = { ...p };
      for (const [k, v] of Object.entries(patch) as [keyof PlanState, never][]) if (!touched.current.has(k)) next[k] = v;
      return next;
    });
  }, []);
  const box = useRef<HTMLTextAreaElement>(null);
  const setDoor = (d: Door) => {
    setDoorState(d); setError(null); setFixHref(null); setRefusal(null);
    window.setTimeout(() => box.current?.focus(), 0);
  };

  // ── the org's registry, hosts, catalogue, stacks, preview ───────────────────────────────
  const { registry, loadError } = useRegistry(projectId);
  const cards = useCatalogue();
  const adapters = useStackAdapters(projectId);
  const [hosts, setHosts] = useState<FolderHost[] | null>(null);
  useEffect(() => {
    let alive = true;
    // One machine is one row: a host registered twice (two projects) is listed once.
    hostFolders.hosts(projectId).then((h) => { if (alive) setHosts((h.hosts ?? []).filter((x, i, all) => all.findIndex((y) => (y.address || y.name) === (x.address || x.name)) === i)); })
      .catch(() => { if (alive) setHosts([]); });
    return () => { alive = false; };
  }, [projectId]);
  // This machine's import directory, read once the door for existing code opens.
  const [localFolders, setLocalFolders] = useState<{ configured: boolean; items: LocalFolder[]; words?: string } | null>(null);
  useEffect(() => {
    if (door !== "existing" || localFolders) return undefined;
    const ctl = new AbortController();
    addProjectApi.localFolders(projectId, "", ctl.signal).then(setLocalFolders)
      .catch(() => { if (!ctl.signal.aborted) setLocalFolders({ configured: false, items: [] }); });
    return () => ctl.abort();
  }, [door, projectId, localFolders]);
  const [preview, setPreview] = useState<Preview | null>(null);
  const mode: Kind = kind;
  useEffect(() => {
    const ctl = new AbortController();
    addProjectApi.preview(projectId, mode, plan.name, ctl.signal).then((p) => {
      setPreview(p);
      auto({ workflow: p.default_workflow ?? "", captain: p.captain.role_key ?? "engineering_captain" });
      const sug = p.staging.find((h) => h.suggested);
      if (sug) auto({ staging: { host_id: sug.host_id, port: sug.port } });
    }).catch(() => undefined);
    return () => ctl.abort();
    // The name only tunes the suggested port; re-read on the kind.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, mode, auto]);

  // ── the repository / folder results (Bring existing code) ──────────────────────────────
  const accounts = useMemo(() => accountsOf(registry), [registry]);
  const account = registry?.connection?.account_login ?? accounts[0] ?? "";
  const hasGitHub = accounts.length > 0;
  const allRepos = useMemo(() => (registry?.items ?? []).filter((r) => r.allowed && !r.archived && r.reachable !== false), [registry]);
  const [pickedRepo, setPickedRepo] = useState<string>("");
  const [pickedFolder, setPickedFolder] = useState<PickedFolder | null>(null);
  const pickedLine = useRef("");
  const [cursor, setCursor] = useState(0);
  const firstLine = text.split("\n")[0].trim();
  const q = (kind === "repo" ? (det.kind === "repo" ? det.query : firstLine) : "").toLowerCase();
  const repoRows = useMemo(() => {
    if (kind !== "repo") return [];
    const exact = det.ref ? allRepos.filter((r) => r.full_name.toLowerCase() === det.ref.toLowerCase()) : [];
    const rest = allRepos.filter((r) => !exact.includes(r) && (!q || r.full_name.toLowerCase().includes(q)))
      .sort((a, b) => String(b.pushed_at ?? "").localeCompare(String(a.pushed_at ?? "")));
    return [...exact, ...rest].slice(0, 6);
  }, [kind, allRepos, q, det.ref]);
  // This machine: the import directory's folders whose name (or path) has what is typed.
  const lq = (kind === "folder" ? (det.query || "") : q).toLowerCase();
  const localRows = useMemo(() => door !== "existing" || !localFolders?.configured ? []
    : localFolders.items.filter((f) => !lq || f.name.toLowerCase().includes(lq) || f.path.toLowerCase().includes(lq)).slice(0, 4),
  [door, localFolders, lq]);
  const folderPath = kind === "folder" ? (firstLine || "/") : "";
  // A repo search also offers /srv/apps/<name> on each server, only for a NAME (one token), never
  // for words with spaces in them.
  const slugQ = /^[\w.-]{2,}$/.test(q);
  const folderRows = useMemo(() => (kind === "folder" || (kind === "repo" && slugQ)) && hosts
    ? hosts.slice(0, 4).map((h) => ({ hostId: h.id, host: h.name, path: kind === "folder" ? folderPath : `/srv/apps/${q}` })) : [], [kind, hosts, folderPath, q, slugQ]);
  const rows = [...repoRows.map((r) => ({ type: "repo" as const, id: r.id })),
                ...localRows.map((f) => ({ type: "local" as const, id: f.path })),
                ...folderRows.map((f) => ({ type: "folder" as const, id: `${f.hostId}:${f.path}` }))];
  const showResults = door === "existing" && !pickedRepo && !pickedFolder;
  useEffect(() => { setCursor(0); }, [q, kind]);

  // A picked folder: how it comes in (Keep using GitHub · Copy it here · Read it in place).
  const [folder, setFolder] = useState<FolderFacts | null>(null);
  const [folderError, setFolderError] = useState<string | null>(null);
  const ghOfFolder = pickedFolder ? (folder?.github ?? null) : null;
  const ghRepoRow = ghOfFolder ? allRepos.find((r) => r.full_name.toLowerCase() === ghOfFolder.toLowerCase()) ?? null : null;
  const bringOpts = pickedFolder && folder?.exists ? bringChoices(pickedFolder.source, ghOfFolder, !!ghRepoRow) : null;
  const [bringPicked, setBring] = useState<Bring | null>(null);
  const bring: Bring | null = bringOpts ? (bringPicked && bringOpts.choices.includes(bringPicked) ? bringPicked : bringOpts.pick) : null;
  const [keepHistory, setKeepHistory] = useState(true);
  const viaGitHub = bring === "github" && !!ghRepoRow;
  const repoId = pickedRepo || (viaGitHub ? ghRepoRow!.id : "");
  const repo = allRepos.find((r) => r.id === repoId) ?? null;

  const [read, setRead] = useState<RepoRead | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  useEffect(() => {
    setRead(null); setReadError(null);
    if (!repoId) return undefined;
    const ctl = new AbortController();
    addProjectApi.repo(projectId, repoId, ctl.signal).then((r) => {
      setRead(r);
      auto({ name: r.facts.suggested_name });
    }).catch((e) => { if (!ctl.signal.aborted) setReadError(refusalText(e)); });
    return () => ctl.abort();
  }, [repoId, projectId, auto]);

  // ── a repo already in a project ────────────────────────────────────────────────────────
  const [shared, setShared] = useState<string[]>([]);
  const highlighted = rows[cursor];
  const highlightedInUse = highlighted?.type === "repo"
    && usedBy(allRepos.find((r) => r.id === highlighted.id) ?? {}).length > 0
    && !shared.includes(highlighted.id);
  const inUse: InUseActions = {
    open: (pid) => go(projectPageHref(pid, homeId)),
    addWork: (pid) => {
      // The person's words go along as the work: an idea's words, or the lines under a repo.
      const words = (kind === "idea" ? text : text.split("\n").slice(1).join("\n")).trim();
      const qs = new URLSearchParams({ tab: "work", compose: "1", compose_kind: "feature", ...(words ? { compose_text: words.slice(0, 1500) } : {}) });
      go(`${projectPageHref(pid, homeId)}&${qs.toString()}`);
    },
    shared,
    share: (id) => setShared((s) => (s.includes(id) ? s : [...s, id])),
  };
  // In the results list, saying "separate" also picks it.
  const inUseRows: InUseActions = { ...inUse, share: (id) => {
    inUse.share(id); takeRepo(id);
    // Back to the box, at its end: the lines under the repo are a note to Helm.
    window.setTimeout(() => { const el = box.current; if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }, 0);
  } };

  const takeRepo = (id: string) => {
    const r = allRepos.find((x) => x.id === id);
    if (!r) return;
    setPickedRepo(r.id); setPickedFolder(null); setFolder(null); setForced("repo");
    setCodeText((t) => [r.full_name, ...t.split("\n").slice(1)].join("\n"));
    pickedLine.current = r.full_name;
    auto({ name: r.full_name.split("/")[1].replace(/[-_.]+/g, " ").replace(/^./, (c) => c.toUpperCase()) });
  };
  const pickRow = async (i: number) => {
    const row = rows[i];
    if (!row) return;
    if (row.type === "repo") {
      const r = allRepos.find((x) => x.id === row.id);
      if (!r) return;
      // THE SAME REPO AGAIN opens the project it is in, and creates nothing.
      const used = usedBy(r);
      if (used.length && !shared.includes(r.id)) { inUse.open(used[0].project_id); return; }
      takeRepo(r.id);
      return;
    }
    setBring(null); setKeepHistory(true); setPickedRepo(""); setForced("folder");
    pickedLine.current = firstLine;
    if (row.type === "local") {
      const f = localRows.find((x) => x.path === row.id);
      if (!f) return;
      setPickedFolder({ source: "local", path: f.path });
      setFolder({ path: f.path, exists: true, git: f.git, github: f.github, branch: f.branch, files: f.files, top: f.top ?? [], stack: f.stack ?? [],
                  build: f.build ?? null, suggested_name: nameFromFolder(f.name) });
      setFolderError(null);
      auto({ name: nameFromFolder(f.name) });
      return;
    }
    const f = folderRows.find((x) => `${x.hostId}:${x.path}` === row.id);
    if (!f) return;
    setPickedFolder({ source: "host", hostId: f.hostId, host: f.host, path: f.path });
    setFolder(null); setFolderError(null);
    try {
      const facts = await guarded("Read the folder on the server.", () => hostFolders.probe(projectId, f.hostId, f.path));
      setFolder(facts);
      if (!facts.exists) setFolderError(`There is no folder ${f.path} on ${facts.host ?? f.host}.`);
      else if (facts.suggested_name) auto({ name: facts.suggested_name });
    } catch (e) { setFolderError(refusalText(e)); }
  };
  const unpick = () => { setPickedRepo(""); setPickedFolder(null); setFolder(null); setRead(null); setBring(null); };

  // ── Helm's understanding (an idea) ─────────────────────────────────────────────────────
  const [u, setU] = useState<Understanding | null>(null);
  const [reading, setReading] = useState(false);
  const [ok, setOk] = useState(false);
  const [fix, setFix] = useState<Brief | null>(null);
  const [pinned, setPinned] = useState<Partial<Record<BriefField, unknown>>>({});
  // ONE READ PER WORDS (walk 2026-11-07: ↵ showed nothing for 90 s). `asked` is the words the
  // last read was asked for, whatever started it — the pause, ↵, Rewrite or Try again. The pause
  // and ↵ ask only when the words changed since; Rewrite and Try again always ask. A rewrite's
  // answer is never overwritten by the pause timer re-asking without the hint.
  const asked = useRef<string>("");
  const seq = useRef(0);
  const understand = useCallback(async (opts: { hint?: string; force?: boolean } = {}) => {
    const words = ideaText.trim();
    if (door !== "new" || words.split(/\s+/).length < MIN_WORDS) return;
    if (!opts.force && words === asked.current) return;
    asked.current = words;
    const mine = ++seq.current;
    setReading(true); setOk(false);
    try {
      const got = await addProjectApi.understand(projectId, { words, hint: opts.hint || undefined, previous: u?.brief ?? null, pinned });
      if (mine !== seq.current) return;
      setU(got);
      if (got.brief.title) auto({ name: nameFromTitle(got.brief.title) });
      if (got.status === "ok") {
        if (got.picks.connectors.length) auto({ connectors: got.picks.connectors.map((c) => c.key) });
        if (got.picks.workflow) auto({ workflow: got.picks.workflow.key });
      }
    } catch (e) {
      if (mine !== seq.current) return;
      setU({ status: "no_model", brief: { title: words.split("\n")[0].slice(0, 70), problem: "", who_uses: "", must_haves: words.split("\n").slice(1).filter(Boolean),
             out_of_scope: [], done_when: "", open_questions: [], added: [], pinned: [], raw: true }, picks: EMPTY_PICKS,
             message: `Helm could not read it now (${refusalText(e)}). Your words are kept; you can continue.` });
    } finally {
      if (mine === seq.current) setReading(false);
    }
  }, [ideaText, door, projectId, u?.brief, pinned, auto]);
  const understandRef = useRef(understand);
  understandRef.current = understand;
  // The pause: only the words (and the door) restart it, never a new answer.
  useEffect(() => {
    if (door !== "new" || fix) return undefined;
    const t = window.setTimeout(() => { void understandRef.current(); }, PAUSE_MS);
    return () => window.clearTimeout(t);
  }, [ideaText, door, fix]);
  // Changing the words un-confirms the brief: the person confirms what is shown.
  useEffect(() => { setOk(false); }, [text]);
  const brief: Brief | null = u?.brief ?? null;

  const saveFix = async () => {
    if (!fix) return;
    const edited: Partial<Record<BriefField, unknown>> = { ...pinned };
    for (const k of ["title", "problem", "who_uses", "must_haves", "out_of_scope", "done_when", "open_questions"] as BriefField[]) {
      if (JSON.stringify(fix[k]) !== JSON.stringify(brief?.[k])) edited[k] = fix[k];
    }
    setPinned(edited);
    setU((cur) => cur ? { ...cur, brief: { ...fix, pinned: Object.keys(edited) } } : cur);
    setFix(null);
    if (fix.title) auto({ name: nameFromTitle(fix.title) });
    // The picks follow the brief as edited (the same turn, every line kept as written).
    try {
      const again = await addProjectApi.connectors(projectId, fix, ideaText);
      if (again.picks?.connectors.length && !touched.current.has("connectors")) setPlanState((p) => ({ ...p, connectors: again.picks!.connectors.map((c) => c.key) }));
      if (again.picks) setU((cur) => cur ? { ...cur, picks: again.picks! } : cur);
    } catch { /* the picks stay as they were */ }
  };

  const answer = (qi: number, a: string) => setU((cur) => cur ? { ...cur, brief: { ...cur.brief, open_questions: cur.brief.open_questions.map((q2, i) => i === qi ? { ...q2, answer: q2.answer === a ? null : a } : q2) } } : cur);

  // ── WHERE A NEW APP'S CODE LIVES (917/918): the new GitHub repo's owner and name, and whether
  //    BOSS can make it — asked while the person is still choosing, in the engine's words ───────
  useEffect(() => {
    if (!plan.newRepo.owner && account) setPlanState((p) => ({ ...p, newRepo: { ...p.newRepo, owner: account } }));
  }, [account, plan.newRepo.owner]);
  // The name BOSS proposes (from the project's name); taken on GitHub, it moves to name-2, name-3 …
  // on its own. A name the person typed is theirs: never changed.
  const [bump, setBump] = useState(0);
  const slug = repoSlug(plan.name.trim() || nameFromIdea(ideaText) || "app");
  useEffect(() => { setBump(0); }, [slug]);
  const repoName = plan.newRepo.name || nextFreeName(slug, bump);
  const [repoCheck, setRepoCheck] = useState<RepoCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const wantCheck = door === "new" && hasGitHub && !!plan.newRepo.owner && !!repoName
    && (plan.where === "github_new" || !touched.current.has("where"));
  useEffect(() => {
    if (!wantCheck) return undefined;
    const ctl = new AbortController();
    setChecking(true);
    // Asked once more on its own when the read fails (it is a read: no step-up); then nothing is
    // claimed — Create still asks GitHub itself and says its words.
    const ask = (left: number) => addProjectApi.checkRepo(projectId, plan.newRepo.owner, repoName, "create", ctl.signal)
      .then((c) => { setRepoCheck(c); setChecking(false); })
      .catch(() => {
        if (ctl.signal.aborted) return;
        if (left > 0) { window.setTimeout(() => { if (!ctl.signal.aborted) void ask(left - 1); }, 1200); return; }
        setRepoCheck(null); setChecking(false);
      });
    const t = window.setTimeout(() => { void ask(1); }, 450);
    return () => { window.clearTimeout(t); ctl.abort(); };
  }, [wantCheck, projectId, plan.newRepo.owner, repoName]);
  // The default: on this machine with no GitHub account (or when BOSS cannot make the repo),
  // else a new GitHub repo. Never over the person's own pick.
  useEffect(() => { auto({ where: defaultWhere(hasGitHub, repoCheck) }); }, [hasGitHub, repoCheck, auto]);
  useEffect(() => {
    if (repoCheck?.reason === "exists" && !plan.newRepo.name && repoCheck.full_name === `${plan.newRepo.owner}/${repoName}` && bump < 20) {
      setBump((b) => b + 1);
    }
  }, [repoCheck, plan.newRepo.name, plan.newRepo.owner, repoName, bump]);

  // ── the six boxes' derived values ──────────────────────────────────────────────────────
  const workflow = preview?.workflows.find((w) => w.key === plan.workflow) ?? preview?.workflows.find((w) => w.key === preview.default_workflow) ?? null;
  const repoLabel = useMemo(() => {
    if (kind === "folder" && !viaGitHub) return { v: pickedFolder?.path.split("/").pop() ?? "", s: (folder?.stack ?? []).join(" · ") || (folder?.files ? `${folder.files} files` : "read at onboarding") };
    if (!repo) return { v: "", s: "" };
    const f = read?.facts.facts;
    const v = f ? [f.framework, f.language].filter(Boolean).join(" + ") : "";
    return { v: v || repo.full_name, s: f?.build ? `${f.build}${f.tests ? " · tests" : ""}` : "at first build" };
  }, [kind, repo, read, pickedFolder, folder, viaGitHub]);
  const stackFor = adapters.find((a) => a.id === (plan.stack || u?.picks.stack?.key || "")) ?? null;
  const cost = estimateWords(workflow, kind);
  const createKind: Kind = door === "new" ? "idea" : repoId ? "repo" : pickedFolder ? "folder" : kind;
  /** The Code box for existing code: where it lives once added. */
  const codeWhere = door !== "existing" ? null
    : repo ? { name: repo.full_name, sub: viaGitHub ? "its GitHub repo · PRs" : "its branch · PRs", local: false }
    : pickedFolder ? (bring === "in_place"
      ? { name: pickedFolder.path, sub: `on ${pickedFolder.host ?? "the server"} · read-only`, local: false }
      : { name: "This machine", sub: `${pickedFolder.source === "host" ? `copied from ${pickedFolder.host ?? "the server"}` : "copied here"} · ${folder?.git ? (keepHistory ? "history kept" : "one commit") : "first commit"}`, local: true })
    : null;

  // ── Create and start ───────────────────────────────────────────────────────────────────
  // The name: the person's, else the brief's title, else the first words (never a blank start).
  const name = plan.name.trim() || (door === "new" ? nameFromIdea(ideaText) : "");
  const open = unaccepted(refusal, startAnyway);
  const pickedCards = door === "new" ? plan.connectors.map((k) => (cards ?? []).find((c) => c.key === k)).filter((c): c is NonNullable<typeof c> => !!c) : [];
  const egress = egressOf(pickedCards);
  const [askEgress, setAskEgress] = useState(false);
  // WHY CREATE CAN'T BE PRESSED, in words, on hover and under the button (walk 2026-11-07: it
  // "looked disabled with no reason given"). "" = it can.
  const words = ideaText.trim().split(/\s+/).filter(Boolean).length;
  const ghBlocked = door === "new" && plan.where === "github_new" && repoCheck && !repoCheck.ok && repoCheck.full_name === `${plan.newRepo.owner}/${repoName}`;
  const why = busy ? "" : door === "choose" ? "Pick a door first" : !canCreate.allowed ? canCreate.reason
    : door === "new" && words < MIN_WORDS ? "Type a few words about the app first"
    : door === "existing" && !repoId && !pickedFolder ? "Pick a repo or a folder first"
    : door === "existing" && pickedFolder && !folder?.exists && !viaGitHub ? (folder || folderError ? "Pick a folder that exists" : "Reading the folder…")
    : door === "existing" && bring === "github" && !ghRepoRow ? `Allow ${ghOfFolder} in Repo and models, or copy it here`
    : door === "existing" && viaGitHub && usedBy(ghRepoRow!).length && !shared.includes(ghRepoRow!.id) ? `${ghOfFolder} is already in ${usedBy(ghRepoRow!)[0].name}: open it, or copy the folder here`
    : ghBlocked ? `GitHub can’t make ${repoCheck!.full_name}: see Code`
    : door === "new" && plan.where === "github_new" && !plan.newRepo.owner ? "Pick the GitHub owner (Code)"
    : !name ? "Give it a name (the Name chip)"
    : refusal && open.length ? `Tick ${open.length === 1 ? "the item" : `the ${open.length} items`} above to start anyway`
    : askEgress && egress.length && !plan.egressOk ? "Tick “Allow these hosts” above first" : "";
  const egressBox = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!askEgress) return;
    egressBox.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    egressBox.current?.querySelector<HTMLInputElement>("input")?.focus({ preventScroll: true });
  }, [askEgress]);
  // One press, one project: a second press (a double click, ⌘↵ while it posts) is ignored.
  const pressing = useRef(false);
  const create = async () => {
    if (pressing.current || busy || why) return;
    // THE HOSTS THE APP TALKS TO are allowed out loud (the templates' own consent): asked here,
    // inline and in view, the first time; the tick is the answer.
    if (egress.length && !plan.egressOk) { setAskEgress(true); return; }
    pressing.current = true;
    setBusy("create"); setError(null); setFixHref(null);
    const idea = door === "new";
    const repoIds = createKind === "repo" ? [repoId] : idea && plan.where === "repo" ? plan.repoIds : [];
    const fields = ownFields(workflow);
    const inputs: Record<string, unknown> = {};
    for (const f of fields) if (plan.inputs[f.name]) inputs[f.name] = plan.inputs[f.name];
    if (plan.tell.trim()) inputs.notify_channel = normalizeChannel(plan.tell);
    const hostFolder = createKind === "folder" && pickedFolder?.source === "host" ? pickedFolder : null;
    const localFolder = createKind === "folder" && pickedFolder?.source === "local" ? pickedFolder : null;
    const body = {
      context_project_id: projectId, kind: createKind, name, words: idea ? ideaText.trim() : "",
      brief: idea && brief && !brief.raw ? brief : null,
      note: !idea ? det.note || null : null, repo_ids: repoIds,
      folder: hostFolder ? { host_id: hostFolder.hostId!, path: hostFolder.path, import_code: bring === "copy", keep_history: keepHistory } : null,
      local_folder: localFolder ? { path: localFolder.path, keep_history: keepHistory } : null,
      code_location: idea && plan.where === "local" ? "local" as const : idea && plan.where === "github_new" ? "github" as const : null,
      new_repo: idea && plan.where === "github_new" ? { owner: plan.newRepo.owner, repo: repoName, private: plan.newRepo.private } : null,
      captain: plan.captain, crew: plan.crew, stack: stackFor?.label ?? null,
      stack_language: stackFor ? adapterLanguage(stackFor) : null,
      connectors: idea ? plan.connectors : [], connector_secrets: plan.secrets,
      accept_egress: plan.egressOk ? egress : [],
      workflow_key: idea ? (workflow?.key ?? null) : null, inputs, notify_when: plan.when,
      audience: plan.audience || null, done_when: plan.doneWhen, read_first: plan.readFirst,
      talks_to: plan.talksTo.map((t) => ({ name: t })),
      staging: plan.staging, map_rules: !idea && plan.mapRules, start_anyway: startAnyway,
      // Only the person's "Make a separate project anyway" sends it (the server refuses otherwise).
      allow_shared_repo: repoIds.some((id) => shared.includes(id)),
    };
    try {
      const r = await guarded("Create the project and start it.", () => withConsent(() =>
        addProjectApi.create({ ...body, project_id: kept.current }, (id) => { kept.current = id; })));
      if (!r.ok) {
        pressing.current = false;
        if (r.refusal.project_id) kept.current = r.refusal.retry?.project_id ?? kept.current;
        const ready2 = asReadiness(r.refusal.readiness);
        if (ready2) { setRefusal(ready2); setBusy(null); return; }
        setError(r.refusal.message || "It did not start.");
        setFixHref(r.refusal.fix_href ?? null);
        setBusy(null);
        return;
      }
      setBusy("created");
      if (idea) dropDraft();
      const landing = projectPageHref(r.data.project_id, homeId, "project", idea ? null : "onboarding");
      // GitHub refused the new repo (no Administration permission, a personal account): the
      // project is local and building; it opens with Publish to GitHub showing the words.
      const pub = r.data.publish;
      go(pub && !pub.ok ? `${landing}&publish=${encodeURIComponent(`${pub.owner ?? plan.newRepo.owner}/${pub.repo ?? repoName}`)}` : landing);
    } catch (e) {
      pressing.current = false;
      setError(refusalText(e, "create"));
      setBusy(null);
    }
  };

  // ── the sample, the presets ────────────────────────────────────────────────────────────
  const [sampleProject, setSampleProject] = useState<string | null>(null);
  const runSample = async () => {
    if (!canCreate.allowed) { setError(canCreate.reason); return; }
    setBusy("sample"); setError(null);
    try {
      let pid = sampleProject;
      if (!pid) {
        const wide = asReadiness(await installWideRefusal(homeId, SAMPLE.workflow_key, undefined, startAnyway));
        if (wide) { setRefusal(wide); setBusy(null); return; }
        const made = await guarded("Create the Hello World project.", () => createFresh(SAMPLE.name));
        pid = made.id;
        setSampleProject(pid);
      }
      await guarded("Start the sample.", () => withConsent(() => templatesApi.apply(SAMPLE.template, {
        project_id: pid!, new_app: { name: SAMPLE.name }, stack: SAMPLE.stack, workflow_key: SAMPLE.workflow_key,
        dedupe_key: `sample:${pid}`, ...(startAnyway.length ? { start_anyway: startAnyway } : {}) } as never)));
      go(projectPageHref(pid, homeId));
    } catch (e) {
      const b = e instanceof IdentityError ? e.body : {};
      const detail = (b.detail && typeof b.detail === "object" ? b.detail : {}) as Record<string, unknown>;
      const r = asReadiness(b.readiness ?? detail.readiness);
      if (r) { setRefusal(r); setBusy(null); return; }
      setError(refusalText(e));
      setBusy(null);
    }
  };
  // A "Try a sample" door opens the sheet with the sample chip lit: one press starts it. A link
  // never creates anything by being opened.
  const pickPreset = async (p: Preset) => {
    if (!canCreate.allowed) { setError(canCreate.reason); return; }
    setBusy(p.key); setError(null);
    try {
      const made = await guarded(`Create the ${p.title} project.`, () => createFresh(p.title, { type: p.kind === "knowledge_graph" ? "knowledge_graph" : "app" }));
      if (p.kind === "knowledge_graph") { go(knowledgePresetHref(made.id, homeId)); return; }
      go(`${projectPageHref(made.id, homeId, "overview")}&start_preset=${encodeURIComponent(p.key)}`);
    } catch (e) { setError(refusalText(e)); setBusy(null); }
  };
  const [ways, setWays] = useState<"menu" | "presets" | null>(null);
  const [ideas, setIdeas] = useState(false);
  const [undo, setUndo] = useState<string | null>(null);

  // ── keys: Esc closes, ⌘↵ creates ───────────────────────────────────────────────────────
  const createRef = useRef(create);
  createRef.current = create;
  useAreaKeydown((e) => {
    // Esc belongs to whatever sits on top: "Confirm it's you", a popover (it stops the key), the
    // full-screen editor, then the sheet. The words stay (the draft).
    if (e.key === "Escape" && !busy && !e.defaultPrevented && !document.querySelector("[data-testid='step-up-sheet']")) {
      if (full) setFull(false); else onClose();
    }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); void createRef.current(); }
  }, { target: "document" });
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const line = parseFloat(getComputedStyle(el).lineHeight) || 20;
    el.style.height = "auto";
    const lines = Math.min(12, Math.max(kind === "idea" ? 5 : 1, Math.ceil(el.scrollHeight / line)));
    el.style.height = `${lines * line + 4}px`;
  }, [text, kind, door]);
  const onBoxKey = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== "Enter" || e.shiftKey || e.metaKey || e.ctrlKey) return;
    if (showResults && rows.length) { e.preventDefault(); void pickRow(cursor); return; }
    // ↵ before the list has loaded (a pasted URL, then ↵ at once) is kept and taken when it lands.
    if (showResults && (!registry || !hosts)) { e.preventDefault(); enterWaiting.current = true; return; }
    // ↵ reads the words now (the pause would in a moment); words already read are not asked again.
    if (door === "new") { e.preventDefault(); notePress(e.nativeEvent); void understand(); }
  };
  const enterWaiting = useRef(false);
  const pickRef = useRef(pickRow);
  pickRef.current = pickRow;
  useEffect(() => {
    if (!enterWaiting.current || !rows.length || !registry) return;
    enterWaiting.current = false;
    void pickRef.current(0);
  }, [rows.length, registry]);
  const onBoxNav = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (!showResults || !rows.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setCursor((c) => Math.min(rows.length - 1, c + 1)); }
    if (e.key === "ArrowUp") { e.preventDefault(); setCursor((c) => Math.max(0, c - 1)); }
  };

  const addAccount = consoleHref({ area: "setup", focus: "repository", new_project: undefined });
  const homeHref = (p: Record<string, string>) => consoleHref({ ...p, new_project: undefined });
  const lines = lineCount(text);
  const helmLive = reading || (!!repoId && !read && !readError);
  const KindIcon = door === "existing" ? (kind === "folder" ? FolderOpen : GithubLogo) : Lightbulb;
  // Words that are code you have, typed into Start a new app: one quiet way across, never a guess.
  const looksLikeCode = door === "new" && !!ideaText.trim() && det.kind !== "idea";
  const toExisting = () => { setCodeText(ideaText.trim()); setIdeaText(""); setForced(null); setDoor("existing"); };

  const head = (
    <div className="npx-head">
      {door === "choose" ? <span className="npx-hico" aria-hidden><FolderPlus size={17} weight="duotone" /></span>
        : <button type="button" className="npx-back" data-testid="ap-back" title="Back to the two doors" aria-label="Back to the two doors" disabled={!!busy} onClick={() => setDoor("choose")}><ArrowLeft size={15} weight="bold" aria-hidden /></button>}
      <h2 id={titleId}>Add a project</h2>
      {door !== "choose" ? (
        <span className="npx-crumb" data-testid="ap-door" data-door={door}>
          <CaretRight size={11} aria-hidden />{door === "new" ? <><Lightbulb size={13} weight="fill" aria-hidden />New app</> : <><GitBranch size={13} weight="bold" aria-hidden />Existing code</>}
        </span>
      ) : null}
      <span className="npx-spacer" />
      <GatedButton permission="project:create" className="npx-pchip" data-testid="ap-sample" disabled={!!busy} data-lit={startKind === "sample" || undefined}
                   title={`Starts now: ${SAMPLE.name} · ${SAMPLE.stack === "node" ? "Node" : SAMPLE.stack} · Small workflow · ≈ 8 min`}
                   onClick={() => void runSample()}>
        <Flask size={13} aria-hidden />{busy === "sample" ? "Starting…" : "Try a sample"} <Play size={11} weight="fill" aria-hidden />
      </GatedButton>
      <button type="button" className="npx-x" aria-label="Close" title="Close (Esc)" data-testid="ap-close" disabled={!!busy} onClick={onClose}><X size={16} weight="bold" aria-hidden /></button>
    </div>
  );

  const foot = (
    <div className="npx-foot">
      {door === "choose" ? <span className="npx-faint npx-small npx-doors-note" data-testid="ap-doors-note"><Laptop size={12} aria-hidden /><span>Code can stay on this machine · GitHub any time</span></span>
        : cost ? <span className="npx-tag npx-cost" data-tone="cost" data-testid="ap-cost" title={estimateTip(workflow)}>{cost}</span>
        : <span className="npx-tag npx-cost" data-tone="cost" title="No run of this workflow here yet">cost after its first run</span>}
      {door !== "choose" ? <span className="npx-faint npx-hide-s"><ShieldCheck size={12} aria-hidden /> Nothing reaches prod without you</span> : null}
      <span className="npx-spacer" />
      <button type="button" className="npx-flink npx-hide-s" data-testid="ap-plan-agent" title="Talk it through first: a PRD meeting, then the build"
              onClick={() => { onClose(); openPlan("pm", { from: "composer" }); }}><ChatsCircle size={13} aria-hidden /> Plan with an agent first</button>
      <span className="npx-node-wrap">
        <button type="button" className="npx-flink" data-testid="ap-more-ways" aria-expanded={!!ways} title="Blank project · Knowledge-graph project · Presets"
                onClick={() => setWays((w) => (w ? null : "menu"))}><DotsThree size={14} aria-hidden /> More ways</button>
        {ways ? (
          <BoxPop className="npx-pop npx-ways" role="menu" data-testid="ap-ways" onEscape={() => setWays(null)}>
            {ways === "menu" ? (
              <>
                <button type="button" role="menuitem" className="npx-opt" data-testid="ap-way-blank" onClick={onForm}><Plus size={13} aria-hidden /><b>Blank project</b><small>no build yet</small></button>
                <button type="button" role="menuitem" className="npx-opt" data-testid="ap-way-kg" onClick={onForm}><GitBranch size={13} aria-hidden /><b>Knowledge-graph project</b><small>type, description</small></button>
                <button type="button" role="menuitem" className="npx-opt" data-testid="ap-way-presets" onClick={() => setWays("presets")}><Sparkle size={13} aria-hidden /><b>Presets</b><small>samples · banking KG</small></button>
                <button type="button" role="menuitem" className="npx-opt npx-show-s" data-testid="ap-way-plan" onClick={() => { onClose(); openPlan("pm", { from: "composer" }); }}><ChatsCircle size={13} aria-hidden /><b>Plan with an agent first</b></button>
              </>
            ) : (
              <div className="npx-presets" data-testid="ap-presets">
                {allPresets().map((p) => (
                  <GatedButton key={p.key} permission="project:create" className="npx-opt" data-testid={`ap-preset-${p.key}`} disabled={!!busy}
                               title={p.kind === "knowledge_graph" ? "A knowledge-graph project, opened on its sample" : "A project with this brief, ready to Start"}
                               onClick={() => void pickPreset(p)}>
                    <b>{busy === p.key ? "Creating…" : p.title}</b><small>{p.label}</small>
                  </GatedButton>
                ))}
              </div>
            )}
          </BoxPop>
        ) : null}
      </span>
      {door !== "choose" ? (
        <>
          <GatedButton permission="project:create" className="npx-primary" data-testid="ap-create"
                       disabled={!!why || !!busy} aria-busy={!!busy || undefined} data-busy={busy === "create" || busy === "created" || undefined}
                       aria-describedby={why ? `${titleId}-why` : undefined}
                       title={why || (refusal ? "Starts it with the items above accepted (⌘↵)" : "Creates the project and starts it (⌘↵)")}
                       onClick={() => void create()}>
            {busy === "create" || busy === "created" ? <CircleNotch size={16} className="npx-spin" aria-hidden /> : <RocketLaunch size={16} aria-hidden />}
            {busy === "create" ? "Creating…" : busy === "created" ? "Created · opening…" : refusal ? "Start anyway" : "Create and start"}<span className="npx-kbd2" aria-hidden>⌘↵</span>
          </GatedButton>
          {why ? <span className="npx-why" id={`${titleId}-why`} data-testid="ap-create-why">{why}</span> : null}
        </>
      ) : null}
    </div>
  );

  const refusals = (
    <>
      {askEgress && egress.length ? (
        <div className="npx-refusal npx-egress" data-testid="ap-egress" role="alert" ref={egressBox}>
          <p><b>The app talks to {egress.length} outside host{egress.length === 1 ? "" : "s"}</b></p>
          <span className="npx-chips">{egress.map((h) => <span key={h} className="npx-chip npx-mono">{h}</span>)}</span>
          <label className="npx-egress-ok">
            <input type="checkbox" data-testid="ap-egress-allow" checked={plan.egressOk} onChange={(e) => set({ egressOk: e.target.checked })} /> Allow these hosts</label>
        </div>
      ) : null}
      {refusal ? (
        <div data-testid="ap-readiness">
          <ReadinessPanel readiness={refusal} accepted={startAnyway} onAccept={setStartAnyway} />
        </div>
      ) : null}
      {error ? <p className="bx-np-error" role="alert" data-testid="ap-error">{error}
        {fixHref ? <> <a className="npx-link" href={homeHref(Object.fromEntries(new URLSearchParams(fixHref.replace(/^\/?\?/, ""))))} data-testid="ap-error-fix">Connect GitHub ›</a></> : null}</p> : null}
    </>
  );

  const sheet = (
    <div className="bx-np-backdrop npx-backdrop" data-testid="add-project-backdrop"
         onPointerDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div className="npx-sheet" role="dialog" aria-modal="true" aria-labelledby={titleId} data-testid="add-project" data-kind={door === "choose" ? "" : kind} data-door={door}>
        {head}
        <div className="npx-body">
          {door === "choose" ? (
            <Doors onPick={(d) => setDoor(d)} hasGitHub={hasGitHub} draft={!!ideaText.trim()} />
          ) : (
            <>
          {/* 1 THE INPUT: the idea (new app) or the code you have (existing) */}
          <div className="npx-in" data-kind={kind} title={door === "new" ? "A few lines about the app" : "owner/repo, a GitHub URL, a folder here, or /path on a server"}>
            <span className="npx-kind" aria-hidden><KindIcon size={19} weight="fill" /></span>
            <textarea ref={box} className="npx-txt" data-testid="ap-input" aria-label={door === "new" ? "Your idea" : "A repo or a folder"} maxLength={8000} autoFocus
                      placeholder={door === "new" ? "A few lines about the app, e.g. “A helpdesk: customers email support@ and it becomes a ticket; AI drafts the reply; staff approve it.”"
                        : "Search your repos and folders, or paste owner/repo, a GitHub URL, or /srv/app"}
                      value={text} rows={door === "new" ? 5 : 1}
                      onChange={(e) => {
                        const v = e.target.value;
                        setText(v);
                        // A picked repo or folder lets go when its line changes; lines typed under it are a note.
                        if ((pickedRepo || pickedFolder) && v.split("\n")[0].trim() !== pickedLine.current) { unpick(); setForced(null); }
                      }}
                      onKeyDown={(e) => { onBoxNav(e); onBoxKey(e); }} />
            <div className="npx-tools">
              {door === "new" ? (
                <>
                  <button type="button" className="npx-link-chip" data-testid="ap-full" title="Full screen editor (Esc closes)" onClick={() => setFull(true)}><ArrowsOut size={13} aria-hidden /></button>
                  <span className="npx-node-wrap">
                    <button type="button" className="npx-link-chip" data-testid="ap-ideas" aria-expanded={ideas} title="Start from an idea: example briefs" onClick={() => setIdeas((v) => !v)}><Sparkle size={12} aria-hidden />Ideas</button>
                    {ideas ? (
                      <BoxPop className="npx-pop npx-ideas" role="listbox" aria-label="Example briefs" data-testid="ap-ideas-list" onEscape={() => setIdeas(false)}>
                        {allPresets().filter((p) => p.kind === "app").slice(0, 10).map((p) => (
                          <button key={p.key} type="button" role="option" aria-selected={false} className="npx-opt" data-testid={`ap-idea-${p.key}`}
                                  onClick={() => { setUndo(ideaText); setIdeaText(p.kind === "app" ? p.brief : ""); setIdeas(false); touched.current.delete("name"); }}>
                            <Sparkle size={12} aria-hidden /><b>{p.title}</b>
                          </button>
                        ))}
                      </BoxPop>
                    ) : null}
                  </span>
                  {undo !== null ? <button type="button" className="npx-link" data-testid="ap-undo" onClick={() => { setIdeaText(undo); setUndo(null); }}>Undo</button> : null}
                </>
              ) : (
                <>
                  {account ? <span className="npx-tag" title="The GitHub account is global — Repo and models"><User size={11} aria-hidden />{account}</span> : null}
                  <a className="npx-link-chip" href={addAccount} data-testid="ap-add-account" title="Connect another account — opens Repo and models › GitHub (global)">+ account</a>
                </>
              )}
            </div>
            <div className="npx-in-foot" data-testid="ap-input-foot">
              {door === "new" && draftShown && ideaText === restored ? (
                <span className="npx-draft" data-testid="ap-draft" role="status">Your words from before ·{" "}
                  <button type="button" className="npx-link" data-testid="ap-draft-clear"
                          onClick={() => { setIdeaText(""); setDraftShown(false); dropDraft(); box.current?.focus(); }}>Clear</button>
                  <span>·</span></span>
              ) : null}
              {door === "new" ? (
                <>
                  <span title="Starts at 5 lines and grows to 12, then scrolls; ⤢ opens the full-screen editor"><ArrowsVertical size={11} aria-hidden /> {text.trim() ? `${lines} line${lines === 1 ? "" : "s"} · grows` : "5 lines · grows"}</span>
                  <span>·</span><span>⇧↵ new line</span><span>·</span><span>↵ Helm reads it</span>
                  <span className="npx-spacer" />
                  {looksLikeCode ? (
                    <button type="button" className="npx-link" data-testid="ap-looks-like-code" onClick={toExisting}>Code you have? Bring existing code ›</button>
                  ) : brief ? <span className="npx-hide-s">Helm rewrote it below ↓</span> : null}
                </>
              ) : <span>⇧↵ adds lines under the {kind === "folder" ? "folder" : "repo"}: a note to Helm</span>}
            </div>
          </div>

          {/* inline results (existing code): GitHub, this machine, servers — one list */}
          {showResults ? (
            <div className="npx-drop" role="listbox" aria-label="Repositories and folders" data-testid="ap-results">
              {kind === "repo" ? <div className="npx-grp"><GithubLogo size={12} aria-hidden />GitHub{account ? ` · ${account}` : ""}</div> : null}
              {kind === "repo" && !registry && !loadError ? [0, 1].map((i) => <div key={i} className="npx-sk" aria-hidden />) : null}
              {loadError && kind === "repo" ? <p className="npx-faint">{loadError}</p> : null}
              {repoRows.map((r, i) => {
                const users = usedBy(r);
                const opens = users.length > 0 && !shared.includes(r.id);
                return (
                  <React.Fragment key={r.id}>
                  <button type="button" role="option" aria-selected={cursor === i} className="npx-pick" data-testid={`ap-repo-${r.id}`} data-in-use={users.length ? "" : undefined}
                          onMouseMove={() => { if (cursor !== i) setCursor(i); }} onClick={() => void pickRow(i)} title={opens ? `Already in ${users[0].name}: opens it` : r.description ?? r.full_name}>
                    <GitBranch size={13} aria-hidden /><span className="npx-mono"><span className="npx-faint">{r.full_name.split("/")[0]}/</span><b>{r.full_name.split("/")[1]}</b></span>
                    {r.language ? <span className="npx-faint npx-small">{r.language}</span> : null}
                    <span className="npx-spacer" />
                    {opens ? <InUseTag used={users} /> : null}
                    <span className="npx-faint npx-mono npx-small" title={r.pushed_at ? `Last push ${new Date(r.pushed_at).toLocaleString()}` : "Last push not known yet"}>{ageShort(r.pushed_at)}</span>
                    {cursor === i ? <span className="npx-kbd">↵</span> : null}
                  </button>
                  {users.length ? <InUseBlock repoId={r.id} used={users} act={inUseRows} testid={`ap-repo-${r.id}`} /> : null}
                  </React.Fragment>
                );
              })}
              {kind === "repo" && registry && !repoRows.length ? <p className="npx-faint npx-pad">No repo matches. <a className="npx-link" href={addAccount}>Allow more in Repo and models ›</a></p> : null}
              {localRows.length ? <div className="npx-grp"><Laptop size={12} aria-hidden />This machine</div> : null}
              {localRows.map((f, j) => {
                const i = repoRows.length + j;
                return (
                  <button key={f.path} type="button" role="option" aria-selected={cursor === i} className="npx-pick" data-testid={`ap-local-${f.path}`}
                          onMouseMove={() => { if (cursor !== i) setCursor(i); }} onClick={() => void pickRow(i)}>
                    <FolderOpen size={13} aria-hidden /><span className="npx-mono">{f.path}</span>
                    <span className="npx-faint npx-small">{f.files} files{f.git ? " · git" : ""}</span>
                    {f.github ? <span className="npx-tag" title={`Its GitHub remote: ${f.github}`}><GithubLogo size={10} aria-hidden />{f.github.split("/")[1]}</span> : null}
                    <span className="npx-spacer" />{cursor === i ? <span className="npx-kbd">↵</span> : null}
                  </button>
                );
              })}
              {kind === "folder" && localFolders && !localFolders.configured ? (
                <p className="npx-faint npx-pad" data-testid="ap-local-off"><Laptop size={12} aria-hidden /> This machine: {localFolders.words || "importing a folder here is off on this install."}</p>
              ) : null}
              {folderRows.length ? <div className="npx-grp"><HardDrives size={12} aria-hidden />Folders on servers</div> : null}
              {folderRows.map((f, j) => {
                const i = repoRows.length + localRows.length + j;
                return (
                  <button key={`${f.hostId}:${f.path}`} type="button" role="option" aria-selected={cursor === i} className="npx-pick" data-testid={`ap-folder-${f.hostId}`}
                          onMouseMove={() => { if (cursor !== i) setCursor(i); }} onClick={() => void pickRow(i)}>
                    <FolderOpen size={13} aria-hidden /><span className="npx-mono">{f.path}</span><span className="npx-faint npx-small">on {f.host}</span>
                    <span className="npx-spacer" />{cursor === i ? <span className="npx-kbd">↵</span> : null}
                  </button>
                );
              })}
              {kind === "folder" && hosts && !hosts.length && !localRows.length ? <p className="npx-faint npx-pad">No verified server yet · <a className="npx-link" href={homeHref({ area: "infra", add: "host" })}>Add one in Infra ›</a></p> : null}
            </div>
          ) : null}

          {/* how a picked folder comes in */}
          {door === "existing" && pickedFolder && bringOpts ? (
            <BringChoice source={pickedFolder} github={ghOfFolder} inRegistry={!!ghRepoRow} choices={bringOpts.choices} bring={bring!}
                         onBring={(b) => setBring(b)} git={!!folder?.git} keepHistory={keepHistory} onKeepHistory={setKeepHistory}
                         allowHref={addAccount}
                         inUse={ghRepoRow && usedBy(ghRepoRow).length && !shared.includes(ghRepoRow.id)
                           ? <InUseBlock repoId={ghRepoRow.id} used={usedBy(ghRepoRow)} act={inUse} testid="ap-bring-gh" /> : null} />
          ) : null}

          {/* 2 PROJECT BRIEF (new app) · REPO SUMMARY (existing code) */}
          {door === "new" ? (
            <IdeaCard text={ideaText} u={u} reading={reading} ok={ok} fix={fix}
                      onOk={() => setOk(true)} onFix={() => brief && setFix({ ...brief })} onFixChange={setFix}
                      onSave={() => void saveFix()} onAnswer={answer}
                      onRewrite={(h) => { void understand({ hint: h, force: true }); }}
                      onRetry={() => void understand({ force: true })} />
          ) : (
            <RepoCard kind={viaGitHub ? "repo" : kind} repo={repo?.full_name ?? pickedFolder?.path ?? ""} read={read} readError={readError}
                      folder={viaGitHub ? null : folder} folderError={folderError}
                      note={det.note} live={helmLive} staging={plan.staging ? preview?.staging.find((h) => h.host_id === plan.staging?.host_id) ?? null : null}
                      onUnpick={unpick} picked={!!repoId || !!pickedFolder} ok={ok} onOk={() => setOk(true)}
                      onFix={() => { setCodeText((t) => (t.includes("\n") ? t : `${t}\n`)); window.setTimeout(() => { const el = box.current; if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }, 0); }} />
          )}

          {/* 3 AGENT ACTIONS */}
          <section className="npx-sum" aria-labelledby={`${titleId}-sum`} data-testid="ap-sum">
            <div className="npx-sum-h">
              <AgentAvatar agent={plan.captain || "engineering_captain"} size={30} aiBadge={false} state={helmLive ? "working" : "idle"} />
              <div><b id={`${titleId}-sum`} data-testid="ap-sum-heading">Agent actions</b>
                <small>{door === "new" ? (brief && !brief.raw ? "Picked from the brief · click any box to change it" : "Picked for you · click any box to change it")
                  : repo ? <>Helm is reading <span className="npx-mono">{repo.full_name.split("/")[1]}</span>{read?.files_total ? ` · ${read.files_total} files` : ""}</>
                  : pickedFolder ? <>Helm reads <span className="npx-mono">{pickedFolder.path.split("/").pop()}</span> at onboarding</> : "Pick the code first"}</small></div>
              <span className="npx-spacer" />
              {helmLive && kind !== "idea" ? <span className="npx-tag" data-tone="blue"><i className="npx-pulse" aria-hidden />live</span> : null}
              <span className="npx-ready" data-testid="ap-ready" title="Readiness: what this install needs before it can start">
                {(preview?.readiness ?? []).map((d) => (
                  <a key={d.key} href={d.ok || !d.href ? undefined : homeHref(Object.fromEntries(new URLSearchParams(d.href.replace(/^\/?\?/, ""))))}
                     data-ok={d.ok || undefined} data-testid={`ap-dot-${d.key}`} title={`${d.label}: ${d.detail}`}><i aria-hidden />{d.label}</a>
                ))}
              </span>
            </div>
            <PlanBoxes kind={kind} preview={preview} plan={plan} set={set} cards={cards} adapters={adapters}
                       helmPicks={u?.picks ?? EMPTY_PICKS} workflow={workflow} registry={allRepos} repoLabel={repoLabel} codeName={repo?.full_name ?? pickedFolder?.path ?? ""}
                       live={helmLive} accountLogin={account} accounts={accounts} homeHref={homeHref} inUse={inUse}
                       newRepo={{ name: repoName, check: repoCheck, checking }} codeWhere={codeWhere} />
          </section>

          {refusals}
            </>
          )}
          {door === "choose" ? refusals : null}
        </div>
        {foot}
      </div>
      {full ? (
        <div className="npx-full" role="dialog" aria-label="Full screen editor" data-testid="ap-full-editor">
          <div className="npx-full-h"><b>Describe the app</b><span className="npx-spacer" />
            <button type="button" className="npx-btn" onClick={() => setFull(false)}><ArrowsIn size={13} aria-hidden /> Done</button></div>
          {/* The cursor lands where the words end, so typing goes on from there. */}
          <textarea className="npx-full-txt" autoFocus value={ideaText} maxLength={8000} aria-label="Describe the app" data-testid="ap-full-input"
                    onFocus={(e) => { const el = e.currentTarget; el.setSelectionRange(el.value.length, el.value.length); }}
                    onChange={(e) => setIdeaText(e.target.value)} />
        </div>
      ) : null}
    </div>
  );
  return typeof document === "undefined" ? sheet : createPortal(sheet, document.body);
}

// ── the two doors ────────────────────────────────────────────────────────────────────────────

/** THE TWO DOORS: two cards, each with its own short flow drawn as three steps. Agents do the
 *  rest — Helm writes the brief or reads the code, onboarding sets the machine up and fixes it. */
function Doors({ onPick, hasGitHub, draft }: { onPick: (d: Door) => void; hasGitHub: boolean; draft: boolean }) {
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => { first.current?.focus(); }, []);
  const door = (d: "new" | "existing") => (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { (e.currentTarget.nextElementSibling as HTMLElement | null)?.focus(); e.preventDefault(); }
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") { (e.currentTarget.previousElementSibling as HTMLElement | null)?.focus(); e.preventDefault(); }
    void d;
  };
  return (
    <div className="npx-doors" role="group" aria-label="What are you adding?" data-testid="ap-doors">
      <button ref={first} type="button" className="npx-door" data-testid="ap-door-new" onClick={() => onPick("new")} onKeyDown={door("new")}>
        <span className="npx-door-ico" data-t="new" aria-hidden><Lightbulb size={22} weight="duotone" /></span>
        <b>Start a new app</b>
        <small>Describe it. Agents plan, build and test it.</small>
        <span className="npx-door-flow" aria-hidden>
          <i>Idea</i><ArrowRight size={10} /><i>Brief</i><ArrowRight size={10} /><i data-you>You approve</i><ArrowRight size={10} /><i>Build</i>
        </span>
        <span className="npx-door-where">
          <span className="npx-tag" data-tone={hasGitHub ? undefined : "blue"}><Laptop size={10} aria-hidden />This machine</span>
          <span className="npx-tag" data-tone={hasGitHub ? "blue" : undefined}><GithubLogo size={10} aria-hidden />GitHub</span>
          {draft ? <span className="npx-tag" data-tone="amber" data-testid="ap-door-draft"><ClockCounterClockwise size={10} aria-hidden />your words kept</span> : null}
        </span>
      </button>
      <button type="button" className="npx-door" data-testid="ap-door-existing" onClick={() => onPick("existing")} onKeyDown={door("existing")}>
        <span className="npx-door-ico" data-t="existing" aria-hidden><GitBranch size={22} weight="duotone" /></span>
        <b>Bring existing code</b>
        <small>A repo or a folder. Agents read it and set it up.</small>
        <span className="npx-door-flow" aria-hidden>
          <i>Code</i><ArrowRight size={10} /><i>Summary</i><ArrowRight size={10} /><i>Onboard</i><ArrowRight size={10} /><i data-you>You accept</i>
        </span>
        <span className="npx-door-where">
          <span className="npx-tag"><GithubLogo size={10} aria-hidden />GitHub</span>
          <span className="npx-tag"><Laptop size={10} aria-hidden />This machine</span>
          <span className="npx-tag"><HardDrives size={10} aria-hidden />A server</span>
        </span>
      </button>
    </div>
  );
}

/** HOW A PICKED FOLDER COMES IN: a folder with a GitHub remote keeps using GitHub or is copied
 *  here (local first); a server's folder can also be read in place. Keep its history when it has one. */
function BringChoice({ source, github, inRegistry, choices, bring, onBring, git, keepHistory, onKeepHistory, allowHref, inUse }: {
  source: PickedFolder; github: string | null; inRegistry: boolean; choices: Bring[]; bring: Bring; onBring: (b: Bring) => void;
  git: boolean; keepHistory: boolean; onKeepHistory: (v: boolean) => void; allowHref: string;
  /** Its GitHub repo is already in a project: the same rules as the repo list (open, add work, separate). */
  inUse?: React.ReactNode;
}) {
  const label: Record<Bring, { t: string; s: string; icon: React.ReactNode }> = {
    github: { t: "Keep using GitHub", s: github ? `builds on ${github} · PRs` : "", icon: <GithubLogo size={15} weight="fill" aria-hidden /> },
    copy: { t: "Copy it here", s: "local first · publish any time", icon: <Laptop size={15} weight="duotone" aria-hidden /> },
    in_place: { t: "Read it in place", s: `on ${source.host ?? "the server"} · read-only`, icon: <HardDrives size={15} aria-hidden /> },
  };
  return (
    <section className="npx-bring" data-testid="ap-bring" aria-label="How it comes in">
      <span className="npx-bring-from">
        <FolderOpen size={13} aria-hidden /><span className="npx-mono">{source.path}</span>
        <span className="npx-faint">{source.source === "local" ? "this machine" : `on ${source.host ?? "a server"}`}</span>
        {github ? <span className="npx-tag"><GithubLogo size={10} aria-hidden />{github}</span> : null}
      </span>
      <div className="npx-bring-opts" role="radiogroup" aria-label="How it comes in">
        {choices.map((b) => {
          const blocked = b === "github" && !inRegistry;
          return (
            <button key={b} type="button" role="radio" aria-checked={bring === b} className="npx-bring-opt" data-testid={`ap-bring-${b}`}
                    data-blocked={blocked || undefined} onClick={() => onBring(b)}
                    title={blocked ? `${github} is not allowed for BOSS yet: allow it in Repo and models` : label[b].s}>
              {label[b].icon}<span><b>{label[b].t}</b><small>{blocked ? "not allowed yet" : label[b].s}</small></span>
              {bring === b ? <Check size={13} weight="bold" className="npx-ok" aria-hidden /> : null}
            </button>
          );
        })}
      </div>
      {bring === "github" && !inRegistry ? (
        <p className="npx-faint npx-small" data-testid="ap-bring-allow">{github} isn’t allowed for BOSS yet · <a className="npx-link" href={allowHref}>Allow it in Repo and models ›</a></p>
      ) : null}
      {bring === "github" && inUse ? inUse : null}
      {bring === "copy" && git ? (
        <label className="npx-keep" data-testid="ap-keep-history-row">
          <input type="checkbox" data-testid="ap-keep-history" checked={keepHistory} onChange={(e) => onKeepHistory(e.target.checked)} />
          Keep its git history <span className="npx-faint">· every branch and tag</span>
        </label>
      ) : null}
    </section>
  );
}

// ── the cards: the ONE brief card (app/projects/brief-card.tsx: "Project brief" · "Repo summary"), the
//    same one New work draws, with Add a project's own rows ──────────────────────────────────

const HELM = { name: "Helm", role_key: "engineering_captain", avatar: "" };

function IdeaCard({ text, u, reading, ok, fix, onOk, onFix, onFixChange, onSave, onAnswer, onRewrite, onRetry }: {
  text: string; u: Understanding | null; reading: boolean; ok: boolean; fix: Brief | null;
  onOk: () => void; onFix: () => void; onFixChange: (b: Brief | null) => void; onSave: () => void;
  onAnswer: (i: number, a: string) => void; onRewrite: (hint: string) => void; onRetry: () => void;
}) {
  const b = u?.brief ?? null;
  // Reading with nothing good on screen yet (the first read, or Try again after no model or a
  // timeout): the card shows Helm reading, never the old "couldn't read it" while it asks again.
  const fresh = reading && (!b || (u?.status ?? "ok") !== "ok");
  const words = text.trim().split(/\s+/).filter(Boolean).length;
  const n = lineCount(text);
  if (!b && !reading) {
    return (
      <section className="px-ub" data-state="empty" data-testid="ap-understood" style={{ borderStyle: "dashed" }}>
        <div className="px-ub-h"><AgentAvatar agent="engineering_captain" size={30} aiBadge={false} />
          <div><b data-testid="ap-ub-heading">Project brief</b><small>{words < MIN_WORDS ? "Type a few lines: the brief fills here for you to confirm." : "Fills when you pause, or press ↵."}</small></div></div>
      </section>
    );
  }
  const added = b?.added ?? [];
  const f = fix;
  const edit = (patch: Partial<Brief>) => f && onFixChange({ ...f, ...patch });
  const open = (b?.open_questions ?? []).filter((q) => !q.answer).length;
  const rows: BriefRow[] = [
    { key: "title", label: "Title", col: 1, skeleton: [80],
      value: <span className="px-ub-ttl"><Marked text={b?.title ?? ""} added={added} /></span>,
      edit: f ? <input className="npx-input" data-testid="ap-fix-title" value={f.title} onChange={(e) => edit({ title: e.target.value })} /> : undefined },
    { key: "problem", label: "Problem", col: 1, skeleton: [90, 60],
      value: <Marked text={b?.problem ?? ""} added={added} />,
      edit: f ? <textarea className="npx-input" rows={2} value={f.problem} onChange={(e) => edit({ problem: e.target.value })} /> : undefined },
    { key: "who", label: "Who uses it", col: 1, skeleton: [70],
      value: <Marked text={b?.who_uses ?? ""} added={added} />,
      edit: f ? <input className="npx-input" value={f.who_uses} onChange={(e) => edit({ who_uses: e.target.value })} /> : undefined },
    { key: "scope", label: "Out of scope", col: 1, skeleton: [55],
      value: <>{(b?.out_of_scope ?? []).map((x, i) => <React.Fragment key={i}>{i ? " · " : ""}<Marked text={x} added={added} /></React.Fragment>)}</>,
      edit: f ? <input className="npx-input" value={f.out_of_scope.join(" · ")} onChange={(e) => edit({ out_of_scope: e.target.value.split(/\s*·\s*|;\s*/).filter(Boolean) })} /> : undefined },
    { key: "musts", label: "Must-haves", col: 2, skeleton: [85, 75, 80, 60],
      value: <ul>{(b?.must_haves ?? []).map((m, i) => <li key={i}><Marked text={m} added={added} /></li>)}</ul>,
      edit: f ? (
        <ul className="npx-fix-list" data-testid="ap-fix-musts">
          {f.must_haves.map((m, i) => (
            <li key={i}><DotsSixVertical size={12} className="npx-faint" aria-hidden />
              <input className="npx-input" value={m} onChange={(e) => edit({ must_haves: f.must_haves.map((x, j) => j === i ? e.target.value : x) })} />
              <button type="button" aria-label="Move up" disabled={!i} onClick={() => { const a = [...f.must_haves]; [a[i - 1], a[i]] = [a[i], a[i - 1]]; edit({ must_haves: a }); }}>↑</button>
              <button type="button" aria-label="Delete" onClick={() => edit({ must_haves: f.must_haves.filter((_, j) => j !== i) })}><Trash size={11} aria-hidden /></button></li>
          ))}
          <li><button type="button" className="npx-link" data-testid="ap-fix-add" onClick={() => edit({ must_haves: [...f.must_haves, ""] })}><Plus size={11} aria-hidden /> add a must-have</button></li>
        </ul>) : undefined },
    { key: "done", label: "Done when", col: 2, skeleton: [90, 70],
      value: <Marked text={b?.done_when ?? ""} added={added} />,
      edit: f ? <textarea className="npx-input" rows={2} value={f.done_when} onChange={(e) => edit({ done_when: e.target.value })} /> : undefined },
  ].filter((r) => fresh ? true : f || r.key === "title" || r.key === "musts" || (r.key === "problem" && b?.problem) || (r.key === "who" && b?.who_uses)
    || (r.key === "scope" && b?.out_of_scope.length) || (r.key === "done" && b?.done_when)) as BriefRow[];
  if (b?.open_questions.length) {
    rows.push({ key: "open", label: `Open · ${open}`, col: 2, question: true, value: (
      <div className="px-ub-q">
        {b.open_questions.map((q, i) => (
          <React.Fragment key={i}><span>{q.question}</span>
            <div className="px-ub-sugs">{[...q.options, "Leave to PRD"].map((o) => (
              <button key={o} type="button" className="px-ub-sug" aria-pressed={q.answer === o} data-testid={`ap-q${i}-${o.replace(/\W+/g, "-").toLowerCase()}`}
                      title={o === "Leave to PRD" ? "Helm decides during the PRD" : undefined} onClick={() => onAnswer(i, o)}>{o}</button>))}</div>
          </React.Fragment>
        ))}
      </div>) });
  }
  const state = fresh ? "reading" : u && u.status !== "ok" ? "fallback" : ok && !f ? "confirmed" : "ready";
  return (
    <div data-testid="ap-understood" data-state={fresh ? "reading" : u?.status ?? "reading"} data-ok={ok || undefined}>
      <BriefCard state={state} agent={HELM} heading="Project brief" rows={rows} columns={2} editing={!!f} confirmed={ok}
                 title={b?.title ?? ""} testid="ap-ub"
                 chips={<>{b?.must_haves.length ? <span className="px-tag">{b.must_haves.length} must-haves</span> : null}
                   {b?.done_when ? <span className="px-tag" data-tone="green">done when set</span> : null}
                   {open ? <span className="px-tag" data-tone="amber">{open} open · to the PRD</span> : null}</>}
                 reading={<>Helm is reading your {n} line{n === 1 ? "" : "s"}…</>}
                 sub={state === "fallback" ? `${u?.message ?? "No model answered."}` : ok ? "Brief confirmed · it becomes the PRD’s first draft"
                   : f ? "Editing · change any line, or give a hint and Rewrite" : `From your ${n} line${n === 1 ? "" : "s"} · edit any line`}
                 live={reading && b ? <span className="px-tag" data-tone="blue"><span className="px-dot" data-s="now" aria-hidden /> rewriting</span> : undefined}
                 onConfirm={onOk} onFix={() => (f ? onSave() : onFix())} onRewrite={onRewrite} onRetry={onRetry} onOpen={() => undefined}
                 hintPlaceholder="Hint for a rewrite, e.g. “shorter”, “split into phases”"
                 saveLabel="Save brief" onCancel={f ? () => onFixChange(null) : undefined} />
    </div>
  );
}

function RepoCard({ kind, repo, read, readError, folder, folderError, note, live, staging, onUnpick, picked, ok, onOk, onFix }: {
  kind: Kind; repo: string; read: RepoRead | null; readError: string | null; folder: FolderFacts | null; folderError: string | null;
  note: string; live: boolean; staging: { host: string; port: number | null } | null; onUnpick: () => void; picked: boolean;
  ok: boolean; onOk: () => void; onFix: () => void;
}) {
  if (!picked) {
    return (
      <section className="px-ub" data-state="empty" data-testid="ap-found" style={{ borderStyle: "dashed" }}>
        <div className="px-ub-h"><AgentAvatar agent="engineering_captain" size={30} aiBadge={false} />
          <div><b data-testid="ap-repo-ub-heading">Repo summary</b><small>Pick a repo or a folder above: Helm reads it live.</small></div></div>
      </section>
    );
  }
  const will = (read?.onboarding_will ?? ["Check this machine and fix what it can", "Map the code into the knowledge graph", "Run its build and tests; explain any failures", "List the business rules it finds", "Propose a first backlog for you to accept", "Set up staging"])
    .map((w) => (w === "Set up staging" && staging ? `Set up staging on ${hostPort(staging.host, staging.port)}` : w));
  const health = read?.health ?? (folder ? [{ label: folder.git ? `git · ${folder.branch ?? "branch"}` : "no git", tone: "grey" }, { label: "no staging yet", tone: "grey" }] : []);
  const rows: BriefRow[] = [
    { key: "what", label: "What it is", col: 1, skeleton: [80],
      value: <span className="px-ub-ttl">{kind === "folder" ? ([folder?.stack?.join(" · "), folder?.build].filter(Boolean).join(" · ") || "A folder of code") : (read?.what_it_is || read?.facts.full_name)}</span> },
    { key: "parts", label: "Main parts", col: 1, skeleton: [60, 70, 50],
      value: <div className="px-ub-files">{kind === "folder" ? (folder?.top ?? []).slice(0, 5).map((t) => <span key={t}><FolderOpen size={12} aria-hidden />{t}</span>)
        : (read?.parts ?? []).map((p) => <span key={p.path}><FolderOpen size={12} aria-hidden /><span className="npx-mono">{p.path}</span><em>{p.files} files</em></span>)}
        {(read && !read.parts.length) || (kind === "folder" && folder && !(folder.top ?? []).length) ? <span className="npx-faint">read at onboarding</span> : null}</div> },
    { key: "health", label: "Health", col: 1, skeleton: [40],
      value: <span className="npx-tags">{health.map((h) => <span key={h.label} className="px-tag" data-tone={h.tone === "grey" ? undefined : h.tone}>{h.label}</span>)}</span> },
    { key: "will", label: "Onboarding will", col: 2, skeleton: [80, 75, 70, 85, 60],
      value: <ol className="npx-steps" data-testid="ap-onboarding-will">{will.map((w) => <li key={w}><span>{w}</span></li>)}</ol> },
    { key: "note", label: "Your note", col: 2,
      value: note ? <span data-testid="ap-note">{note}</span> : <span className="npx-faint">Optional: add lines under the {kind === "folder" ? "folder" : "repo"}, e.g. “start with the SLA bugs”</span> },
  ];
  const loaded = kind === "folder" ? !!folder : !!read;
  return (
    <div data-testid="ap-found" data-ok={ok || undefined}>
      {readError || folderError ? <p className="npx-faint" role="status">Couldn’t read it now: {readError ?? folderError}</p> : null}
      <BriefCard state={!loaded && live ? "reading" : "ready"} agent={HELM} heading="Repo summary" rows={rows} columns={2} editing={false} confirmed={ok}
                 title={repo} testid="ap-repo-ub"
                 reading={<>Reading <span className="npx-mono">{repo.split("/").pop()}</span>…</>}
                 sub={<>From <span className="npx-mono">{repo.split("/").pop()}</span>{read?.files_total ? ` · ${read.files_total} files` : folder?.files ? ` · ${folder.files} files` : ""} · {live ? "fills as Helm reads" : ok ? "confirmed" : "read"}</>}
                 live={live ? <span className="px-tag" data-tone="blue"><span className="px-dot" data-s="now" aria-hidden /> reading</span> : undefined}
                 onConfirm={onOk} onFix={onFix}
                 actions={<button type="button" className="px-btn" data-kind="ghost" data-size="s" data-testid="ap-change-repo" onClick={onUnpick}>Change</button>}
                 foot="Nothing is changed in the code until you approve a change." />
    </div>
  );
}
