"use client";

/**
 * NEW PROJECT: one screen, two jobs — "✦ A new app" and "＋ Add to an existing app".
 *
 * The brief on the left, the live plan on the right (the path, its real steps and who does each,
 * the team, where it lives, whether it can run here), and one footer that says what it costs and
 * starts it. Approved mockups: boss-new-project-mockup (mockup.html, mockup-add.html).
 *
 * ADDING TO AN APP IS THE EVERYDAY JOB (operator, 2026-10-05), and it used to live behind
 * Factory › build › ⋯ › Change it. It is a mode of this screen now, reachable from the Workflows
 * page, the Factory (build header, Apps rows) and the Command Center (`?compose=add&app=<key>`).
 * Every path it offers builds ONTO the app: the quick change is the feedback proposal on the
 * app's own run; Small and Major start their workflow with `extends_run_id`, which the build
 * step honours (create_code_task → start_followup_task: the app's branch, its lineage, its tests);
 * Spec it first convenes the panel with `[boss:extend_run=…]` (decisions.py).
 *
 * DRIVEN BY THE SELECTED WORKFLOW (boss-1, 2026-10-03): a workflow that is not a product brief
 * (Trend to content, a video edit) opens on its own start form, with no mode switch, no product
 * questions, and "Lives in" only when a step needs a repository (the readiness check's answer).
 * Every Start in the console opens this, so a run is never started blind.
 *
 * WHERE THE CODE LIVES (multi-repo, mockup 03): One repo is the default and exactly today's
 * behaviour and request. Several repos — offered when the project has two or more repositories —
 * maps the app's components to repositories (suggested from names and languages, in build
 * order), PUTs them as the app's components on Start, then starts as today with
 * `repo_layout: {mode: "multi", app_name}`. A refused PUT says why and starts nothing.
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import React, { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { AgentAvatar } from "../agent-avatar";
import { AgentChip } from "../agent-chip";
import { HelpTip } from "../help-tip";
import { IDEAS, IdeaCover } from "../ask-boss-hero";
import { consoleHref } from "../url-intent";
import { apiWrite } from "../../lib/api-write";
import { callAction } from "../../lib/call-action";
import { GatedButton, useGate } from "../../components/gated";
import { openPlan } from "../../lib/converse-doors";
import { confirmCodeTaskFeedbackAction, conveneFeaturePanelAction, submitCodeTaskFeedbackAction,
         type FeedbackActionResult } from "../actions";
import { showToast } from "../client-forms";
import { notifyChanged } from "../changed";
import { nameOf } from "../agents/run-face";
import { CHANGE_CHIPS, specFirstDirective } from "../factory/change-it";
import { builtBuild, parseApps, type AppRow } from "../factory/apps-tab";
import { AppContext } from "./app-context";
import { createLinkAction } from "../factory/links-actions";
import { TalksTo, linkRequests, talksToProblem, type TalksToRow } from "./talks-to";
import { defaultInputs, invalidInputs, missingRequired, startsFromABrief, suppliedInputs,
         typedInputs, workflowInputFields } from "../../lib/workflow-inputs";
import { ReadinessPanel, WIZARD_COLLECTS, unaccepted, useWorkflowReadiness, type Readiness }
  from "../workflow-readiness-ui";
import { get, post } from "./delivery-api";
import { StackSelect, StackWarning, adapterLanguage, useStackAdapters, useStackCheck }
  from "../../components/v2/templates/stack-warning";
import {
  KEEPS_WORKING, QUICK, SPEC, appLine, appMatches, approvalsWord, checksWithKind, costKey,
  costRange, counts, defaultPath, featureInput, findApp, landsAs, matchIdea, monogram, normalizeChannel,
  pathCards, readyWord, workingWord, type Asks,
  quickChange, rememberMode, rememberedMode, routeTimeline, timelineOf, toggleKind,
  youApproveWord, type ComposeMode, type CostHistory, type TimelineStep,
} from "./compose-plan";
import type { CatalogueEntry, Installed } from "../delivery-workflows-ui";
import { ConnectorCards, useCatalogue, useIdeas, type IdeaItem } from "../../components/v2/templates/catalogue";
import { IdeasDialog } from "../../components/v2/templates/ideas-dialog";
import { TemplateSheet, type ApplyTarget } from "../../components/v2/templates/template-sheet";
import { stackLabel, templatesApi, type Applied, type TemplateCard } from "../../lib/templates";
import { useIdentityVersion } from "../../lib/use-agent-identities";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { MULTIREPO_PLACES, listWords, multirepo, refusalMessage, roleLabel, shortName,
         type ProjectRepo } from "../../lib/multirepo";
import { ComponentRows, rowKey, rowsToInput, type MapRow } from "../multirepo/parts";
import { GitPullRequest, LockKey, Plus } from "@phosphor-icons/react";

/** A repository this project can build in, from its registry. */
type Repo = { id: string; full_name: string; role?: string | null; default_branch?: string };

/** What the composer has collected for a new app. One object so switching path costs nothing. */
export type NewProjectDraft = {
  title: string;
  directive: string;
  audience: string;
  criteria: string[];
  workflowKey: string;
  repository: string;
  links: string[];
  /** Who will be on it. Empty means "whatever BOSS seeds" — see the Team fact. */
  agents: string[];
  /** What THIS workflow declared it needs — a video to fetch, a clip to trim, a channel to
   *  publish to. Keyed by field name; only filled-in values are sent. */
  inputs: Record<string, string>;
};

const EMPTY_DRAFT: NewProjectDraft = {
  title: "", directive: "", audience: "", criteria: [],
  workflowKey: "", repository: "", links: [], agents: [], inputs: {},
};

/**
 * Whether the selected workflow has a step that needs a repository.
 *
 * THE READINESS CHECK'S ANSWER, not a second rule: `workflow_readiness` derives `repository`
 * from the steps' capabilities (build, preview, deploy, pull request), and the composer asks it
 * as soon as the workflow is known. Until it answers, the catalogue entry's own needs say the
 * same thing; a workflow somebody made needs one if its start form asks for one. No workflow
 * chosen is "this project's usual path", which builds.
 */
export function needsARepository(o: {
  workflowKey: string; readiness: Readiness | null;
  entry?: Pick<CatalogueEntry, "needs" | "requires_project">; schema: unknown;
}): boolean {
  if (!o.workflowKey) return true;
  if (o.readiness?.checked?.length) return o.readiness.checked.some((n) => n.key === "repository");
  if (o.entry) {
    return (o.entry.needs ?? []).some((n) => n.key === "repository")
      || (o.entry.requires_project ?? []).includes("repository");
  }
  return workflowInputFields(o.schema).some((f) => f.name === "repository");
}

/** The brief, assembled the way the pipeline reads best.
 *
 * Not decoration. A one-line directive produces a panel arguing about one line: BOSS's own
 * sample briefs are written as who-it-is-for plus what-it-does plus criteria, because that is
 * what a spec review can actually review. The composer collects the parts and joins them here,
 * so the operator writes prose and the pipeline receives structure.
 */
export function briefFrom(draft: NewProjectDraft): string {
  const parts = [draft.directive.trim()];
  if (draft.audience.trim()) parts.push(`Who it is for: ${draft.audience.trim()}`);
  return parts.filter(Boolean).join("\n");
}

/** Criteria worth sending: blank rows are UI, not requirements. */
export function criteriaFrom(draft: NewProjectDraft): string[] {
  return draft.criteria.map((c) => c.trim()).filter(Boolean);
}

/** Links worth sending, and only ones an agent could actually open. */
export function linksFrom(draft: NewProjectDraft): string[] {
  return draft.links
    .map((l) => l.trim())
    .filter((l) => /^https?:\/\//i.test(l));
}

/**
 * What "Lives in" says about where the code goes.
 *
 * NO PLATFORM DEFAULT IS PROMISED. With nothing registered this said "the platform default is
 * used" — but the API pre-selects that default whenever one exists, so reaching this branch
 * means there is none. Community has none at all, and the build then deadlettered with "No
 * repository registered for project …" after the panel had already run. The form says what is
 * true: builds need a connected GitHub repository, and where to connect one.
 */
export function repositoryHint(
  suggested: { name: string; source: string } | null, repoCount: number,
): { text: string; needsConnection: boolean } {
  if (suggested) {
    return {
      text: ` Pre-selected: ${suggested.name}${
        suggested.source === "registry_primary" ? " — this project's primary." : "."}`,
      needsConnection: false,
    };
  }
  if (repoCount > 0) {
    return {
      text: " This project has more than one repository and none is primary, so choose one below.",
      needsConnection: false,
    };
  }
  return {
    text: " This project has no repository connected yet, and a build needs one: BOSS opens "
      + "its pull request in a GitHub repository you connect. The panel and PRD still run "
      + "without one; the build step stops until a repository is connected.",
    needsConnection: true,
  };
}

/** What stops the operator starting, said as the thing to do rather than as an error. */
export function blockingReason(
  step: number, draft: NewProjectDraft, declaresInputs = false,
): string {
  if (step === 0) {
    // A WORKFLOW THAT DECLARES ITS OWN INPUTS DOES NOT NEED A BRIEF.
    //
    // This demand exists because the catalogue's paths convene a PANEL that argues from the
    // text, and a thin brief produces a thin spec. A workflow that asks for a video link and
    // editing instructions convenes nothing: its steps read the fields it declared, and the
    // real instruction is one of them. Demanding twenty characters of prose as well is asking
    // the operator to write a brief nobody reads, to satisfy a check meant for a different
    // kind of work.
    if (declaresInputs) return "";
    if (draft.directive.trim().length < 20) {
      return "Describe what you want built, in a sentence or two.";
    }
  }
  return "";
}

/** What stops a feature being added: the app, and what it should gain. */
export function addBlockingReason(o: { app: AppRow | null; gain: string }): string {
  if (!o.app) return "Pick the app it goes into.";
  if (o.gain.trim().length < 10) return "Say what it should gain, in a sentence.";
  return "";
}

/** The start-form fields the composer draws for a workflow.
 *
 * Everything the workflow declared, less what the composer collects itself: for a product
 * brief, the directive, name, criteria, repository and links; for any other workflow, only the
 * repository, and only when "Lives in" is shown. A workflow somebody made keeps every other
 * field it declared — its `title` may mean a YouTube title, not the run's name. */
export function composerFields(schema: unknown, brief: boolean, repositoryElsewhere = false) {
  return workflowInputFields(schema).filter((f) =>
    brief ? !WIZARD_COLLECTS.has(f.name) : !(repositoryElsewhere && f.name === "repository"));
}

/** The run input Start sends. A product brief sends the brief's own keys (title, directive,
 *  criteria, links, repository) plus its other declared fields; any other workflow sends
 *  exactly its declared fields, by their declared names — what its steps' `from_input` read
 *  and what the example setup scripts document — and nothing it did not ask for. */
export function startInput(o: {
  brief: boolean; draft: NewProjectDraft; fields: ReturnType<typeof workflowInputFields>;
  supplied: Record<string, string>; needsRepository: boolean;
}): Record<string, unknown> {
  const { draft } = o;
  const typed = typedInputs(o.fields, o.supplied);
  const links = o.needsRepository ? linksFrom(draft) : [];
  const where = {
    // Sent only when the operator gave one. An empty string here would read as a deliberate
    // choice of "" further down and beat the project's own default.
    ...(o.needsRepository && draft.repository ? { repository: draft.repository } : {}),
    ...(links.length ? { reference_links: links } : {}),
  };
  if (!o.brief) return { ...typed, ...where };
  const text = briefFrom(draft).trim();
  const title = draft.title.trim() || (text.length > 72 ? `${text.slice(0, 69)}…` : text);
  const criteria = criteriaFrom(draft);
  return {
    title, directive: text,
    ...(criteria.length ? { acceptance_criteria: criteria } : {}),
    ...where,
    ...typed,
  };
}

/** The cost history of every workflow, for the footer's "Builds like this cost". */
function useCosts(projectId: string): Record<string, CostHistory> {
  const [costs, setCosts] = useState<Record<string, CostHistory>>({});
  useEffect(() => {
    let alive = true;
    void (async () => {
      const r = await get<{ costs?: Record<string, CostHistory> }>("costs", projectId);
      // No history, or an API without the aggregate: the line is hidden, not guessed.
      if (alive && r && !("error" in r)) setCosts(r.costs ?? {});
    })();
    return () => { alive = false; };
  }, [projectId]);
  return costs;
}

/** Every time the operator will be asked, per path (GET /v1/delivery-workflows/asks): the one
 *  source the cards, the timeline and the footer count from. Null on an API without it; the
 *  plan then reads the catalogue's gates and still lists each build's merge question. */
function useAsks(projectId: string): Asks | null {
  const [asks, setAsks] = useState<Asks | null>(null);
  useEffect(() => {
    let alive = true;
    void (async () => {
      const r = await get<Asks>("asks", projectId);
      if (alive && r && !("error" in r)) setAsks(r);
    })();
    return () => { alive = false; };
  }, [projectId]);
  return asks;
}

/** The apps this project has built (GET /v1/factory/apps), for "App". */
function useApps(projectId: string, wanted: boolean): { apps: AppRow[]; loaded: boolean } {
  const [apps, setApps] = useState<AppRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!wanted) return;
    let alive = true;
    void (async () => {
      try {
        const r = await fetch(`/v1/factory/apps?project_id=${encodeURIComponent(projectId)}`,
                              { cache: "no-store" });
        if (r.ok && alive) setApps(parseApps(await r.json()));
      } catch { /* the picker says there are none */ }
      if (alive) setLoaded(true);
    })();
    return () => { alive = false; };
  }, [projectId, wanted]);
  return { apps, loaded };
}

/** An app's cover: the template it was built from, else its monogram. */
function AppArt({ app, scope }: { app: string; scope: string }) {
  const idea = matchIdea(app, IDEAS);
  if (idea) return <IdeaCover idea={idea} scope={scope} />;
  const m = monogram(app);
  return (
    <span className="np-mono" style={{ "--np-hue": m.hue } as React.CSSProperties} aria-hidden>
      {m.initials}
    </span>
  );
}

/** The big box: user-resizable (a visible grip), and a full-screen editor that keeps the text. */
function BigBox({ id, value, onChange, placeholder, label, onFull, testid, onSubmit }: {
  id: string; value: string; onChange: (v: string) => void; placeholder: string; label: string;
  onFull: () => void; testid: string; onSubmit: () => void;
}) {
  return (
    <div className="np-big-wrap">
      <textarea id={id} className="dw-new-input np-big" value={value} data-testid={testid}
                aria-label={label} placeholder={placeholder}
                onChange={(e) => onChange(e.target.value)}
                onKeyDown={(e) => {
                  // ⌘/Ctrl+Enter starts, as every other composer does.
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                    e.preventDefault();
                    e.stopPropagation();
                    onSubmit();
                  }
                }} />
      <span className="np-grip" aria-hidden>drag to resize <i /></span>
      <button type="button" className="np-full" data-testid={`${testid}-full`} onClick={onFull}
              title="Open a large editor. Esc closes it.">⤢ Full screen</button>
    </div>
  );
}

/** The full-screen editor: same text, more room. Esc closes it first, before anything else. */
function FullEditor({ label, value, onChange, onClose }: {
  label: string; value: string; onChange: (v: string) => void; onClose: () => void;
}) {
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = box.current;
    if (el) {
      el.focus();
      // The caret goes where the operator stopped typing: at the end, not before the first word.
      el.setSelectionRange(el.value.length, el.value.length);
      el.scrollTop = el.scrollHeight;
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); onClose(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [onClose]);
  const sheet = (
    <div className="np-fs" role="dialog" aria-modal="true" aria-label={label} data-testid="np-fullscreen"
         onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="np-fs-card">
        <div className="np-fs-head">
          <span className="np-lbl">{label}</span>
          <span className="np-spacer" />
          <button type="button" className="np-btn" data-testid="np-fullscreen-done" onClick={onClose}
                  title="Esc">Done</button>
        </div>
        <textarea ref={box} className="np-fs-text" value={value} aria-label={label}
                  data-testid="np-fullscreen-text" onChange={(e) => onChange(e.target.value)} />
      </div>
    </div>
  );
  return typeof document === "undefined" ? sheet : createPortal(sheet, document.body);
}

/** "Done when": green rows, each editable, ✕ to drop, "+ Add a check". */
function Checks({ checks, onChange, testid, placeholder }: {
  checks: string[]; onChange: (next: string[]) => void; testid: string; placeholder: string;
}) {
  const last = useRef<HTMLTextAreaElement | null>(null);
  const [focusNew, setFocusNew] = useState(false);
  useEffect(() => { if (focusNew) { last.current?.focus(); setFocusNew(false); } }, [focusNew]);
  return (
    <>
      <div className="np-checks">
        {checks.map((c, i) => (
          <div className="np-check" key={i}>
            <span className="np-tick" aria-hidden>✓</span>
            <textarea className="np-check-text" value={c} data-testid={`${testid}-${i}`} rows={1}
                      ref={i === checks.length - 1 ? last : undefined}
                      aria-label={`Check ${i + 1}`} placeholder={placeholder}
                      onChange={(e) => {
                        const next = [...checks];
                        next[i] = e.target.value.replace(/\n/g, " ");
                        onChange(next);
                      }} />
            <button type="button" className="np-rm" aria-label="Remove this check"
                    data-testid={`${testid}-rm-${i}`}
                    onClick={() => onChange(checks.filter((_, j) => j !== i))}>✕</button>
          </div>
        ))}
      </div>
      <button type="button" className="np-add" data-testid={`${testid}-add`}
              onClick={() => { onChange([...checks, ""]); setFocusNew(true); }}>+ Add a check</button>
    </>
  );
}

/** "Read first": links as chips, "+ Add a link" opens a field; Enter adds it. */
function Links({ links, onChange, testid }: { links: string[]; onChange: (next: string[]) => void; testid: string }) {
  const [adding, setAdding] = useState(false);
  const [text, setText] = useState("");
  const commit = () => {
    const v = text.trim();
    if (v) onChange([...links, v]);
    setText("");
    setAdding(false);
  };
  return (
    <div className="np-links">
      {links.map((l, i) => (
        <span className="np-link-chip" key={`${l}-${i}`} data-testid={`${testid}-${i}`} title={l} data-user-text>
          {l.replace(/^https?:\/\//i, "")}
          <button type="button" className="np-rm" aria-label={`Remove ${l}`}
                  onClick={() => onChange(links.filter((_, j) => j !== i))}>✕</button>
        </span>
      ))}
      {adding ? (
        <input className="input np-link-input" autoFocus value={text} data-testid={`${testid}-input`}
               placeholder="https://docs.example.com/api" aria-label="Link to read first"
               onChange={(e) => setText(e.target.value)} onBlur={commit}
               onKeyDown={(e) => {
                 if (e.key === "Enter") { e.preventDefault(); commit(); }
                 if (e.key === "Escape") { e.stopPropagation(); setText(""); setAdding(false); }
               }} />
      ) : (
        <button type="button" className="np-add" data-testid={`${testid}-add`}
                onClick={() => setAdding(true)}>+ Add a link</button>
      )}
    </div>
  );
}

/** Who does a step, drawn: the captain's face and callsign, BOSS, or an amber "You approve". */
function WhoCell({ step }: { step: TimelineStep }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  if (step.who.kind === "you") {
    return <span className="np-who np-who-you"><span className="np-you">You</span>approve</span>;
  }
  if (step.who.kind === "boss") {
    return <span className="np-who np-who-boss"><AgentAvatar variant="boss" size={18} />BOSS</span>;
  }
  const n = nameOf(step.who.role);
  return (
    <span className="np-who" title={n.role}>
      <AgentAvatar agent={step.who.role} size={24} />{n.name}
    </span>
  );
}

function Timeline({ steps }: { steps: TimelineStep[] }) {
  let n = 0;
  return (
    <ol className="np-timeline" data-testid="np-timeline">
      {steps.map((s) => {
        if (!s.keep && !s.merge && !s.ask) n += 1;
        const gate = s.who.kind === "you";
        return (
          <li key={s.id} className={[gate ? "np-gate" : "", s.keep ? "np-keep" : "", s.merge ? "np-merge" : ""]
                .filter(Boolean).join(" ") || undefined}
              data-testid={`np-step-${s.id}`} data-who={s.who.kind}>
            <span className="np-dot" aria-hidden>{s.keep ? "✓" : s.merge ? "PR" : s.ask ? "✎" : n}</span>
            <span className="np-st"><b>{s.name}</b>{s.line ? <span>{s.line}</span> : null}</span>
            <WhoCell step={s} />
          </li>
        );
      })}
    </ol>
  );
}

/**
 * STARTING WORK, WHERE THE WORK IS WATCHED.
 */
export function NewProjectComposer({ projectId, actor, workflows, catalogue, onStarted, startWith,
                                     example, mode: askedMode, appKey, previewId, onCancel }: {
  projectId: string; actor: string;
  workflows: Installed[];
  catalogue: CatalogueEntry[];
  onStarted: (runId: string | null) => void;
  /** A workflow the operator asked to start, chosen already. */
  startWith?: string;
  /** "Try an example": the template's own example brief, filled in for the operator to read
   *  and change — never started unseen. */
  example?: { title?: string; directive?: string } | null;
  /** The mode a link asked for (`?compose=`); otherwise the last one used. */
  mode?: ComposeMode | null;
  /** The app a link chose (`?compose=add&app=`): a factory app key or any of its builds. */
  appKey?: string | null;
  /** A running preview of that app: a quick change is anchored to it. */
  previewId?: string | null;
  onCancel?: () => void;
}) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const schemaOf = (key: string) =>
    workflows.find((w) => w.workflow_key === key)?.input_schema
      ?? catalogue.find((c) => c.key === key)?.input_schema;
  /** Each field's declared default, for a workflow just chosen. */
  const defaultsFor = (key: string) => defaultInputs(workflowInputFields(schemaOf(key)));
  const installedKeys = useMemo(() => workflows.map((w) => w.workflow_key), [workflows]);

  // WHICH JOB. A link's mode wins; a Start on one workflow is "new"; otherwise the last used,
  // read after mount so the server render and the first client render agree.
  const [mode, setModeState] = useState<ComposeMode>(askedMode ?? "new");
  useEffect(() => {
    if (!askedMode && !startWith) setModeState(rememberedMode());
  }, [askedMode, startWith]);

  const firstPath = startWith ?? defaultPath("new", installedKeys);
  const [draft, setDraft] = useState<NewProjectDraft>(
    { ...EMPTY_DRAFT, workflowKey: firstPath, inputs: firstPath ? defaultsFor(firstPath) : {},
      title: example?.title ?? "", directive: example?.directive ?? "" });
  /** The path chosen in add mode: a workflow key, or QUICK / SPEC. */
  const [addPath, setAddPath] = useState<string>(() => defaultPath("add", installedKeys));
  const setMode = (m: ComposeMode) => {
    setModeState(m);
    rememberMode(m);
    setError(null);
  };

  // ── Add mode's own state ────────────────────────────────────────────────────────────────
  // Read in both modes: add mode picks the app; "Talks to" (either mode) picks what it calls.
  const { apps, loaded: appsLoaded } = useApps(projectId, true);
  const [appId, setAppId] = useState<string | null>(appKey ?? null);
  const app = findApp(apps, appId);
  // The app's stack, from the templates it already has (none known: the gallery shows all, and
  // the API refuses a stack the app's files do not show).
  useEffect(() => {
    setAppStack(null);
    setAppTemplates([]);
    if (!app?.appKey) return undefined;
    let alive = true;
    templatesApi.applied(projectId, app.appKey)
      .then((a) => {
        if (!alive) return;
        setAppStack(a.find((x) => x.stack)?.stack ?? null);
        setAppTemplates(a.filter((x) => x.status !== "abandoned"));
      })
      .catch(() => undefined);
    return () => { alive = false; };
  }, [projectId, app?.appKey]);
  const [appQuery, setAppQuery] = useState("");
  const [allApps, setAllApps] = useState(false);
  const [gain, setGain] = useState("");
  const [kinds, setKinds] = useState<string[]>([]);
  const [addChecks, setAddChecks] = useState<string[]>([KEEPS_WORKING]);
  const [addLinks, setAddLinks] = useState<string[]>([]);
  const [proposal, setProposal] = useState<FeedbackActionResult | null>(null);
  /** "Talks to": the project's other apps this one should call (§5.5). */
  const [talks, setTalks] = useState<TalksToRow[]>([]);
  const linkGate = useGate("app_link:manage");
  // ── Connectors and ideas (operator, 2026-10-13). `connector`: the one chosen in "Add a
  //    connector" (either mode); Start then opens its sheet (stack, test secrets, what it talks
  //    to, the security note) with the name and brief already written. `tplCard`: that sheet, or
  //    an idea's "Build from its reference".
  const connectorCards = useCatalogue();
  const ideas = useIdeas();
  const [connector, setConnector] = useState<TemplateCard | null>(null);
  const [ideaCard, setIdeaCard] = useState<TemplateCard | null>(null);
  const [tplCard, setTplCard] = useState<{ card: TemplateCard; target: ApplyTarget;
                                           name?: string; note?: string; stack?: string } | null>(null);
  const [appStack, setAppStack] = useState<string | null>(null);
  /** What the app already carries (connectors, its starter): "Already in the app" on their cards. */
  const [appTemplates, setAppTemplates] = useState<Applied[]>([]);
  /** Add a feature: the app picker, reopened by "Change app" once an app is chosen. */
  const [pickApp, setPickApp] = useState(false);
  /** "Start from an idea ▸": the ideas dialog. */
  const [ideasOpen, setIdeasOpen] = useState(false);
  /** "More detail": who it is for, done when, read first, talks to. */
  const [moreOpen, setMoreOpen] = useState(false);
  /** A new app's stack: an adapter id, or "" for BOSS to pick from the brief. */
  const adapters = useStackAdapters(projectId);
  const [stackId, setStackId] = useState("");
  const stackAdapter = adapters.find((a) => a.id === stackId) ?? null;
  // A deep link to an app that is not on the first page of cards still shows it first.
  const shownApps = useMemo(() => {
    const matched = apps.filter((a) => appMatches(a, appQuery));
    if (allApps || appQuery.trim()) return matched;
    const first = matched.slice(0, 6);
    if (app && !first.includes(app)) return [app, ...first.slice(0, 5)];
    return first;
  }, [apps, appQuery, allApps, app]);

  /** The missing needs the operator chose to start without — sent as `start_anyway`. */
  const [startAnyway, setStartAnyway] = useState<string[]>([]);
  /** The checklist from a refused start. */
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  // WHO WILL BE ON IT. Read from the API rather than guessed here: start_run seeds from
  // STAGE_ROLES plus the chosen workflow's approver roles, and a second copy of that rule in
  // the console would drift from the one that actually runs.
  const [defaultAgents, setDefaultAgents] = useState<string[]>([]);
  /** The workflow whose seeded team the defaults are: the new app's path, or the change's. */
  const teamKey = mode === "add" ? (addPath.startsWith("@") ? "" : addPath) : draft.workflowKey;
  const [allRoles, setAllRoles] = useState<Array<{ role_key: string;
                                                   display_name?: string | null }>>([]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(
          `/api/project-agents?project_id=${encodeURIComponent(projectId)}&defaults=1`
          + (teamKey ? `&workflow_key=${encodeURIComponent(teamKey)}` : ""),
          { cache: "no-store" });
        if (r.ok && alive) setDefaultAgents(((await r.json())?.default_roles ?? []) as string[]);
      } catch { /* the run still starts; BOSS seeds them either way */ }
    })();
    return () => { alive = false; };
  }, [projectId, teamKey]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(`/api/agent?view=roles&project_id=${encodeURIComponent(projectId)}`,
                              { cache: "no-store" });
        if (r.ok && alive) setAllRoles(((await r.json())?.items ?? []) as typeof allRoles);
      } catch { /* the picker is empty; the defaults still show */ }
    })();
    return () => { alive = false; };
  }, [projectId]);
  const [repos, setRepos] = useState<Repo[]>([]);
  // ── Where the code lives: One repo (today) or Several repos (the app's components). ──────────
  const [layout, setLayout] = useState<"one" | "several">("one");
  /** The project's repositories (GET /v1/projects/{id}/repos); null until read or on an older API. */
  const [projectRepos, setProjectRepos] = useState<ProjectRepo[] | null>(null);
  const [mapRows, setMapRows] = useState<MapRow[]>([]);
  const [suggesting, setSuggesting] = useState(false);
  const manageGate = useGate("repository:manage");
  const guarded = useGuardedCall();
  const [suggested, setSuggested] = useState<{ name: string; source: string } | null>(null);
  const repoHint = repositoryHint(suggested, repos.length);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [allPaths, setAllPaths] = useState(false);
  const [editTeam, setEditTeam] = useState(false);
  const [editRepo, setEditRepo] = useState(false);
  const [planOpen, setPlanOpen] = useState(false);
  const [full, setFull] = useState<"brief" | "gain" | null>(null);
  const costs = useCosts(projectId);
  const asks = useAsks(projectId);
  const set = <K extends keyof NewProjectDraft>(key: K, value: NewProjectDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const setInput = (name: string, value: string) =>
    setDraft((d) => ({ ...d, inputs: { ...d.inputs, [name]: value } }));
  const choosePath = (key: string) => setDraft((d) => ({
    ...d, workflowKey: key, inputs: { ...defaultsFor(key), ...d.inputs } }));

  // WHERE THE CODE GOES, ANSWERED BEFORE IT IS ASKED. The project's registry knows its
  // repositories and which one is primary, so "Lives in" arrives filled in.
  useEffect(() => {
    let alive = true;
    void (async () => {
      const r = await get<{ repositories: Repo[]; suggested: string; suggested_source: string }>(
        "repositories", projectId);
      // A project with no registry is a normal state, not an error to surface: "Lives in"
      // says a build needs one and where to connect it (see repositoryHint).
      if (!alive || !r || "error" in r) return;
      setRepos(r.repositories ?? []);
      if (r.suggested) setSuggested({ name: r.suggested, source: r.suggested_source });
    })();
    return () => { alive = false; };
  }, [projectId]);

  // THE PROJECT'S REPOSITORIES, for Several repos. An API without them leaves One repo alone.
  useEffect(() => {
    let alive = true;
    multirepo.projectRepos(projectId)
      .then((v) => { if (alive) setProjectRepos(v.items ?? []); })
      .catch(() => { if (alive) setProjectRepos(null); });
    return () => { alive = false; };
  }, [projectId]);

  /** Several repos: the suggested mapping, asked once; a row per repository when none comes. */
  const chooseLayout = async (next: "one" | "several") => {
    setLayout(next);
    setError(null);
    if (next !== "several" || mapRows.length || !projectRepos?.length) return;
    setSuggesting(true);
    try {
      const out = await multirepo.suggest(projectId, projectRepos.map((r) => r.binding_id));
      const rows = (out.components ?? []).map((c): MapRow => ({
        key: rowKey(), name: c.name, binding_id: c.binding_id, path: c.path ?? "/", kind: c.kind ?? "code",
        depends_on: c.depends_on ?? [], reason: c.reason ?? null }));
      if (rows.length) { setMapRows(rows); return; }
    } catch { /* no suggestions: one row per repository, named by its role */ }
    finally { setSuggesting(false); }
    setMapRows(projectRepos.map((r) => ({
      key: rowKey(), name: roleLabel(r.role_hint) || shortName(r.full_name), binding_id: r.binding_id, path: "/",
      kind: r.role_hint === "deploy_config" ? "deploy_config" : "code", depends_on: [] })));
  };

  // WHAT THE CHOSEN WORKFLOW IS. Read from the installed definition rather than hardcoded: a
  // custom workflow declares its own start form, and the composer has to draw whatever it is.
  const adding = mode === "add";
  const flowKey = adding ? (addPath.startsWith("@") ? "" : addPath) : draft.workflowKey;
  const chosen = workflows.find((w) => w.workflow_key === flowKey);
  const entry = catalogue.find((c) => c.key === flowKey);
  const schema = schemaOf(flowKey);
  const brief = startsFromABrief(schema, !!draft.workflowKey) || adding;
  // A WORKFLOW THAT IS NOT A PRODUCT BRIEF is started on its own form: no mode switch.
  const formOnly = !adding && !!draft.workflowKey && !startsFromABrief(schema, true);
  /** Opened by a Start on that very workflow: its own name and description, no mode switch. */
  const lockedForm = formOnly && startWith === draft.workflowKey;
  const allFields = workflowInputFields(schema);

  // CAN IT RUN HERE. Asked as soon as the path is known, so what is missing is on screen
  // before Start rather than in a run that fails at its last step.
  // A BARE ADDRESS IS AN EMAIL CHANNEL. "Who to tell" said team@example.com and the readiness
  // check read it as no channel at all; normalised once, read by the check and by Start alike.
  const inputsNow = draft.inputs.notify_channel
    ? { ...draft.inputs, notify_channel: normalizeChannel(draft.inputs.notify_channel) } : draft.inputs;
  const asked = useWorkflowReadiness(projectId, flowKey, suppliedInputs(allFields, inputsNow));
  const readiness = asked ?? refusal;
  const notReady = unaccepted(readiness, startAnyway);

  const needsRepository = !adding && needsARepository({ workflowKey: draft.workflowKey, readiness,
                                                        entry, schema });
  /** Several repos is offered only where it can mean something: two or more repositories. */
  const severalOffered = (projectRepos?.length ?? 0) >= 2;
  const several = needsRepository && severalOffered && layout === "several";
  const mapInput = rowsToInput(mapRows);
  const mapReady = mapInput.some((c) => c.kind === "code") && mapInput.length === mapRows.length;
  const mapRepoNames = (kind?: "code" | "deploy_config") => [...new Set(mapInput
    .filter((c) => !kind || c.kind === kind)
    .map((c) => projectRepos?.find((r) => r.binding_id === c.binding_id)?.full_name ?? "")
    .filter(Boolean))];
  const declaredInputs = composerFields(schema, brief, needsRepository);
  const inputsToSend = suppliedInputs(declaredInputs, inputsNow);
  const stillBlank = missingRequired(declaredInputs, inputsToSend);
  const malformed = invalidInputs(declaredInputs, inputsToSend);

  // THE PLAN: the chosen path's real steps, who does each, and where you approve.
  const appName = app?.app ?? "";
  const timeline: TimelineStep[] = adding && addPath.startsWith("@")
    ? routeTimeline(addPath, appName, asks?.routes?.[addPath] ?? null)
    : timelineOf({ steps: chosen?.steps ?? [], gates: entry?.gates ?? null,
                   automatic: entry?.automatic_approvals ?? null, mode, app: appName,
                   asks: asks?.workflows?.[flowKey] ?? null });
  const total = counts(timeline);
  const deploysOnlyWithYou = !timeline.some((s) => s.id === "deploy" && s.who.kind !== "you");
  const cost = costRange(costs[costKey(adding ? addPath : draft.workflowKey)]);

  // `??` WAS WRONG HERE AND IT COST A RUN: "" is "nothing blocking", and `??` never looks past
  // it. A required input left blank must stop the start at the form, not at the runtime.
  // CAN BOSS BUILD WHAT IT ASKS FOR ("build it in Go")? Asked while typing; said before Start.
  // THE STACK CHOSEN travels in the brief the builder reads ("Stack: …"), and the check asks about it.
  const stackLine = stackAdapter ? `Stack: ${stackAdapter.label}.` : "";
  const directiveOut = [draft.directive.trim(), stackLine].filter(Boolean).join("\n\n");
  const stackCheck = useStackCheck(projectId, adding ? gain : `${draft.title}\n${draft.directive}`,
                                   adding || !stackAdapter ? undefined : adapterLanguage(stackAdapter));
  const blocked = (adding ? addBlockingReason({ app, gain }) : blockingReason(0, draft, !brief))
    || (stackCheck.blocks ? "BOSS cannot build the stack this asks for: see the note above." : "")
    || (stillBlank.length ? `Needs ${stillBlank.join(", ")}` : "")
    || (malformed.length ? `Check ${malformed.join(", ")}` : "")
    || talksToProblem(talks)
    || (several && !mapReady ? "Map each component to a repository." : "")
    // So does something the project lacks, unless the operator chose to start without it.
    || (notReady.length
        ? `Not ready here yet: ${notReady.map((m) => m.label).join("; ")} — see the plan.`
        : "");

  const description = lockedForm
    ? String(chosen?.description || entry?.description || "").split("\n")[0].trim()
    : "";
  const heading = lockedForm
    ? (chosen?.name ?? entry?.name ?? draft.workflowKey)
    : "New project";

  /** WHO IS ON IT, recorded as a CHOICE — but only when it differs from what start_run already
   *  seeded. Writing an identical list back as `operator` would claim a decision nobody made. */
  const assignTeam = async (title: string) => {
    // Recording the team is POST /v1/projects/{id}/agents (work_item:manage). A viewer who
    // cannot keeps the seeded team rather than send a write that is refused.
    if (!teamGate.allowed) return;
    const chosenAgents = draft.agents;
    const sameAsDefault = chosenAgents.length === defaultAgents.length
      && chosenAgents.every((a) => defaultAgents.includes(a));
    if (chosenAgents.length && !sameAsDefault) {
      try {
        await apiWrite("/api/project-agents", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_id: projectId, app: title, actor,
                                 role_keys: chosenAgents }),
        });
        // Anyone the operator took OFF the default list has to come off for real, or the
        // composer would show a choice the run does not honour.
        for (const gone of defaultAgents.filter((d) => !chosenAgents.includes(d))) {
          await apiWrite("/api/project-agents", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: projectId, app: title, actor,
                                   action: "unassign", role_key: gone }),
          });
        }
      } catch { /* the run is started and seeded; the assignment can be fixed on the run */ }
    }
  };

  /** Start a workflow run: a new app's, or one built onto an app (`extends_run_id`). */
  const startRun = async (workflowKey: string, input: Record<string, unknown>) => {
    const r = await post("start", {
      project_id: projectId, actor,
      ...(workflowKey ? { workflow_key: workflowKey } : { work_class: "project.new" }),
      input,
      ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
      ...(stackCheck.refused && stackCheck.allowed ? { allow_unsupported_stack: true } : {}),
    });
    if (!r.ok) {
      setError(r.error ?? "Could not start it.");
      // Refused for what the project lacks: draw the list where the operator is looking.
      if ("readiness" in r && r.readiness) setRefusal(r.readiness);
      return { ok: false as const, id: null };
    }
    const run = (r.data as { run?: { id?: string }; id?: string } | undefined);
    await assignTeam(String(input.title || heading));
    return { ok: true as const, id: String(run?.run?.id ?? run?.id ?? "") || null };
  };

  const startNew = async () => {
    const input = startInput({ brief, draft: brief ? { ...draft, directive: directiveOut } : draft,
                               fields: declaredInputs, supplied: inputsToSend,
                               needsRepository });
    // A NEW APP'S LINKS travel with its start: the app exists only once it is built, so the start
    // checks them (app_link:manage, the apps, the aliases) and the first build makes them.
    if (brief && talks.length) input.app_links = linkRequests(talks);
    if (several) {
      // THE APP'S COMPONENTS FIRST, under the name the run files the app by; refused, nothing starts.
      const appName = String(input.title || heading).trim();
      try {
        await guarded(`Map ${appName}'s components to repositories.`,
                      () => multirepo.putComponents(projectId, appName, mapInput, appName));
      } catch (e) {
        if ((e as { code?: string }).code !== "step_up_cancelled") setError(refusalMessage(e));
        return;
      }
      delete input.repository;
      input.repo_layout = { mode: "multi", app_name: appName };
    }
    const started = await startRun(draft.workflowKey, input);
    if (!started.ok) return;
    setDraft(EMPTY_DRAFT);
    setTalks([]);
    setLayout("one");
    setMapRows([]);
    onStarted(started.id);
  };

  /** The run to build onto: the app's newest build that BUILT something. */
  const baseRun = app ? (builtBuild(app.builds)?.runId ?? app.key) : "";

  /** The quick change: the agent scopes it; the proposal shows here; nothing runs until Start. */
  const propose = async () => {
    const qc = quickChange({ gain, kinds, checks: addChecks, links: addLinks });
    const form = new FormData();
    form.set("run_id", baseRun);
    form.set("message", qc.message);
    form.set("expected", qc.expected);
    form.set("workflow_version_id", "");
    form.set("actor", actor);
    if (previewId) form.set("anchor", `/v1/previews/${previewId}/web/`);
    const r = await callAction(submitCodeTaskFeedbackAction, undefined, "code_task:steer")(null, form);
    if (r?.error) setError(r.error); else { setProposal(r); setPlanOpen(true); }
  };

  const confirmChange = async () => {
    if (!proposal?.thread_id) return;
    const form = new FormData();
    form.set("thread_id", proposal.thread_id);
    form.set("actor", actor);
    const r = await callAction(confirmCodeTaskFeedbackAction, undefined, "code_task:steer")(null, form);
    if (r?.error) { setError(r.error); return; }
    notifyChanged("code-tasks");
    showToast("success", "Change started");
    if (r?.workflow_run_id) { onStarted(r.workflow_run_id); return; }
    // The change is a build of this app: watch it on its build page.
    if (r?.run_id && typeof window !== "undefined") {
      window.location.assign(consoleHref(window.location.search, { area: "code_tasks", build: r.run_id }));
    }
    onStarted(null);
  };

  /** Spec it first: the panel specs THIS feature; the approved PRD builds onto the app. */
  const convene = async () => {
    const what = [gain.trim(), addChecks.filter((c) => c.trim()).length
      ? `Done when: ${addChecks.filter((c) => c.trim()).join("; ")}` : "",
                  addLinks.length ? `Read first: ${addLinks.join(" ")}` : ""].filter(Boolean).join("\n");
    const form = new FormData();
    form.set("project_id", projectId);
    form.set("actor", actor);
    form.set("directive", specFirstDirective("feature", appName, app?.repo ?? "", baseRun, what));
    // STRUCTURED, NOT A MARKER: the app travels as constraints.extends_run_id, which the PRD's
    // approval reads to build onto the app (the text marker stays for old panels only).
    form.set("extends_run_id", baseRun);
    const r = await callAction(conveneFeaturePanelAction, undefined, "meeting:convene")(form);
    if (!r || r.ok === false) { setError(r?.error ?? "The panel could not be convened."); return; }
    notifyChanged("meetings");
    showToast("success", "Panel convened");
    // Land on the panel just convened: that is where the spec is written and approved.
    if (typeof window !== "undefined") {
      window.location.assign(consoleHref(window.location.search, r.meeting_run_id
        ? { area: "meetings", meeting: r.meeting_run_id } : { area: "meetings" }));
    }
    onStarted(null);
  };

  /** AN EXISTING APP'S LINKS are made BEFORE the change starts, so its build already gets
   *  BOSS_LINK_<ALIAS>_*. A refused link stops here, says which and why, and starts nothing;
   *  the ones made before it stay made (they are listed under the app's Links) and leave the form. */
  const createLinks = async (): Promise<boolean> => {
    if (!app || !talks.length) return true;
    const made: string[] = [];
    for (const row of talks) {
      const [body] = linkRequests([row]);
      const form = new FormData();
      form.set("project_id", projectId);
      form.set("app_key", app.key);
      form.set("provider_app_key", body.provider_app_key);
      form.set("alias", body.alias);
      form.set("env", body.env);
      if (body.credentials === "test_users") form.set("test_users", "on");
      const r = await callAction(createLinkAction, undefined, "app_link:manage")(form);
      if (!r?.ok) {
        setTalks((rows) => rows.filter((x) => !made.includes(x.alias)));
        setError(`${body.alias} was not linked: ${r?.error || "refused"}. Nothing was started`
          + `${made.length ? `; ${made.join(", ")} ${made.length === 1 ? "is" : "are"} linked` : ""}.`);
        return false;
      }
      made.push(body.alias);
    }
    setTalks([]);
    showToast("success", `Linked ${made.join(", ")}`);
    return true;
  };

  const startAdd = async () => {
    if (!app) return;
    // The quick change's first press only asks for a proposal: nothing is linked until Start.
    if (!(addPath === QUICK && !proposal?.proposal) && !(await createLinks())) return;
    if (addPath === QUICK) { await (proposal?.proposal ? confirmChange() : propose()); return; }
    if (addPath === SPEC) { await convene(); return; }
    const input = { ...featureInput({ app: appName, runId: baseRun, gain, kinds, checks: addChecks,
                                      links: addLinks }),
                    // The preview the operator was looking at, as the quick change anchors to it.
                    ...(previewId ? { preview_id: previewId } : {}),
                    ...typedInputs(declaredInputs, inputsToSend) };
    const started = await startRun(addPath, input);
    if (started.ok) onStarted(started.id);
  };

  // Pre-greyed (P1-18) by what the primary press will do: steer a quick change, convene a
  // spec-first panel, or start a workflow run.
  const steerGate = useGate("code_task:steer");
  const conveneGate = useGate("meeting:convene");
  const runGate = useGate("workflow_run:start");
  const teamGate = useGate("work_item:manage");
  const primaryGate = adding && addPath === QUICK ? steerGate : adding && addPath === SPEC ? conveneGate : runGate;

  const primary = async () => {
    if (busy || (blocked && !(adding && proposal?.proposal))) return;
    // A CONNECTOR CHOSEN: Start opens its sheet (stack, test secrets, what it talks to, the
    // security note), with the app's name and the brief already in it. Its own Start applies it.
    if (connector && !formOnly) {
      if (adding && app) {
        setTplCard({ card: connector, note: gain,
                     target: { kind: "add", runId: baseRun, appKey: app.appKey ?? null, name: app.app, stack: appStack } });
      } else if (!adding) {
        setTplCard({ card: connector, target: { kind: "new" }, name: draft.title,
                     stack: stackAdapter ? adapterLanguage(stackAdapter) : undefined,
                     note: [directiveOut, draft.audience ? `Who it is for: ${draft.audience}` : ""].filter(Boolean).join("\n\n") });
      }
      return;
    }
    if (!primaryGate.allowed) { showToast("error", primaryGate.reason); return; }
    setBusy(true);
    setError(null);
    try {
      await (adding ? startAdd() : startNew());
    } finally {
      setBusy(false);
    }
  };
  const onKey = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); void primary(); }
  };

  // THE TEAM: in add mode, the crew that built the app until somebody changes it. Editing it
  // starts from that crew AND the workflow's seeded team, so taking one face off never quietly
  // unassigns an approver the crew list did not show.
  const crew = adding && app?.builders.length ? app.builders : [];
  const baseTeam = crew.length ? [...new Set([...crew, ...defaultAgents])] : defaultAgents;
  const agentsNow = draft.agents.length ? draft.agents : baseTeam;
  const crewNamed = crew.length > 0 && !draft.agents.length;
  const faces = crewNamed ? crew : agentsNow;
  const teamNames = faces.slice(0, 2).map((k) => nameOf(k).name).join(", ");
  const teamMore = faces.length > 2 ? `+${faces.length - 2}` : "";
  const agentPicker = (
    <div className="aot-list np-team-edit" data-testid="dw-wiz-agents">
      {agentsNow.map((key) => (
        <AgentChip key={key} roleKey={key}
                   label={allRoles.find((r) => r.role_key === key)?.display_name
                          || key.replace(/_/g, " ")}>
          <button type="button" className="aot-x" aria-label={`Remove ${key}`}
                  onClick={() => set("agents", agentsNow.filter((k) => k !== key))}>✕</button>
        </AgentChip>
      ))}
      <select className="mr-select aot-pick" value="" data-testid="dw-wiz-agent-add" aria-label="Add an agent"
              onChange={(e) => {
                if (!e.target.value) return;
                set("agents", [...agentsNow, e.target.value]);
              }}>
        <option value="">+ Add an agent…</option>
        {allRoles
          .filter((r) => !agentsNow.includes(r.role_key))
          .map((r) => (
            <option key={r.role_key} value={r.role_key}>
              {r.display_name || r.role_key}
            </option>
          ))}
      </select>
      {draft.agents.length ? (
        <span className="dw-field-hint">
          Changed.{" "}
          <button type="button" className="dw-linkish" data-testid="np-team-reset"
                  onClick={() => set("agents", [])}>Put it back</button>
        </span>
      ) : null}
    </div>
  );

  const pathName = (key: string) => key === "" ? "This project's usual path"
    : workflows.find((w) => w.workflow_key === key)?.name ?? key;
  const cards = pathCards(mode, installedKeys);
  const current = adding ? addPath : draft.workflowKey;
  const onCard = cards.some((c) => c.key === current);
  const cardFacts = (key: string): { sub: string; gates: string } => {
    // Counted from the same timeline the plan draws, from the same asks: one source.
    if (key === QUICK || key === SPEC) {
      return { sub: key === QUICK ? "Agent scopes it" : "Panel → build",
               gates: approvalsWord(counts(routeTimeline(key, "", asks?.routes?.[key] ?? null)).approvals) };
    }
    const w = workflows.find((x) => x.workflow_key === key);
    const e = catalogue.find((c) => c.key === key);
    const steps = timelineOf({ steps: w?.steps ?? [], gates: e?.gates ?? null,
                               automatic: e?.automatic_approvals ?? null, mode,
                               asks: asks?.workflows?.[key] ?? null });
    const n = w?.step_count ?? e?.step_count ?? counts(steps).steps;
    return { sub: `${n} steps`, gates: approvalsWord(counts(steps).approvals) };
  };

  const summary = `${adding ? (cards.find((c) => c.key === addPath)?.short ?? "") : (
    cards.find((c) => c.key === draft.workflowKey)?.short ?? pathName(draft.workflowKey))}`
    + ` · ${total.steps} steps · ${approvalsWord(total.approvals)}`;

  const readyDots = readiness?.checked?.length ? (
    <div className="np-ready" data-testid="np-ready">
      {readiness.checked.map((c) => {
        const missing = (readiness.missing ?? []).some((m) => m.key === c.key);
        return (
          <span key={c.key} data-missing={missing || undefined} title={c.label}>
            <i aria-hidden />{readyWord(c.key, c.label, missing)}
          </span>
        );
      })}
      {/* THE BUDGET: green says nothing in the checklist, so the dot says it; yellow and orange
          come as notes and are said as such (the note's sentence is the tooltip). */}
      {(readiness.missing ?? []).some((m) => m.key === "budget") ? null : (() => {
        const note = (readiness.notes ?? []).find((n) => n.key === "budget");
        return (
          <span data-missing={note ? true : undefined} title={note?.text ?? "Within this project's budget"}>
            <i aria-hidden />{note ? `Budget ${note.level}` : "Budget OK"}
          </span>
        );
      })()}
    </div>
  ) : null;

  const declaredForm = declaredInputs.length ? (
    <div className="dw-wiz-inputs np-inputs" data-testid="dw-workflow-inputs">
      {/* THE WORKFLOW'S OWN START FORM. Rendered FROM its declaration (input_schema, either
          shape): label, required mark, the author's help as a tooltip, the example as the
          placeholder, the default as the starting value, and the control its type asks for. */}
      {declaredInputs.map((field) => {
        const id = `dw-workflow-input-${field.name}`;
        const value = draft.inputs[field.name] ?? "";
        const common = {
          id, "data-testid": id,
          "aria-required": field.required || undefined,
          "aria-invalid": malformed.includes(field.label) || undefined,
        };
        return (
          <div className="dw-wiz-input dw-field" key={field.name} data-kind={field.kind}>
            {field.kind === "boolean" ? (
              <label className="dw-field-label dw-check" htmlFor={id}>
                <input type="checkbox" {...common} checked={value === "true"}
                       onChange={(e) => setInput(field.name, e.target.checked ? "true" : "false")} />
                {" "}{field.label}
                {field.help ? <> <HelpTip label={field.label} text={field.help} /></> : null}
              </label>
            ) : (
              <>
                <span className="dw-field-label">
                  <label htmlFor={id}>{field.label}</label>
                  {field.required ? <em className="dw-req" title="Required"> required</em> : null}
                  {field.help ? <> <HelpTip label={field.label} text={field.help} /></> : null}
                </span>
                {/* A paragraph gets a box big enough to read back. */}
                {field.kind === "multiline" ? (
                  <textarea {...common} className="dw-new-input" rows={4} value={value}
                            placeholder={field.example}
                            onChange={(e) => setInput(field.name, e.target.value)} />
                ) : (
                  <input {...common} className="input" value={value}
                         type={field.kind === "number" ? "number"
                           : field.kind === "email" ? "email"
                           : field.kind === "url" ? "url" : "text"}
                         inputMode={field.kind === "number" ? "numeric" : undefined}
                         placeholder={field.example}
                         onChange={(e) => setInput(field.name, e.target.value)} />
                )}
              </>
            )}
          </div>
        );
      })}
    </div>
  ) : null;

  const proposalView = adding && proposal?.proposal ? (
    <div className="np-proposal" data-testid="np-proposal">
      <div className="np-lbl">The proposal <em>nothing runs until you start it</em></div>
      <span className="np-proposal-kind" data-testid="np-proposal-kind"
            title={proposal.proposal.classification === "full_rerun"
              ? "Re-runs the whole build on the same branch" : "A scoped change on the same branch and PR"}>
        {proposal.proposal.classification === "full_rerun" ? "Full rerun" : "Small change"}
      </span>
      <pre className="np-proposal-note" data-user-text>{proposal.proposal.fix_note}</pre>
      <button type="button" className="np-add" data-testid="np-proposal-edit"
              onClick={() => setProposal(null)}>Edit</button>
    </div>
  ) : adding && proposal?.question ? (
    <p className="np-ask" data-testid="np-proposal-question" data-user-text>{proposal.question}</p>
  ) : null;

  /** Picking an idea fills the brief and the name; a starter also offers its reference. */
  const pickIdea = (idea: IdeaItem) => {
    setDraft((d) => ({ ...d, directive: idea.brief, title: idea.title }));
    setIdeaCard(idea.card ?? null);
    setIdeasOpen(false);
  };

  /** "More detail": who it is for, done when, read first, talks to. Open while any is filled. */
  const moreCount = (draft.audience.trim() ? 1 : 0) + draft.criteria.filter((c) => c.trim()).length
    + draft.links.length + talks.length;
  const more = (fields: React.ReactNode, tip: string, count = moreCount) => (
    <details className="np-more-detail" data-testid="np-more" open={moreOpen || count > 0}
             onToggle={(e) => setMoreOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary data-testid="np-more-toggle" title={tip}>
        More detail <em>{count ? `${count} added` : "optional"}</em>
      </summary>
      <div className="np-more-body">{fields}</div>
    </details>
  );

  const newBrief = (
    <>
      <div>
        <div className="np-lbl np-lbl-row">
          <label htmlFor="dw-new-directive">What should it do?</label>
          <HelpTip label="Project description"
                   text="The panel and builder use your words. Say what it does and what a person can do with it." />
          <span className="np-spacer" />
          <button type="button" className="np-link np-ideas-open" data-testid="np-ideas-open"
                  title="An example app to start from: it fills in the brief, which you then change"
                  onClick={() => setIdeasOpen(true)}>
            Start from an idea ▸
          </button>
        </div>
        <BigBox id="dw-new-directive" testid="dw-new-directive" label="What should it do?"
                value={draft.directive} onChange={(v) => set("directive", v)}
                placeholder={"A shift-swap board where staff can offer a shift they cannot work "
                  + "and colleagues can claim it, with the manager approving the swap."}
                onFull={() => setFull("brief")} onSubmit={() => void primary()} />
        {ideaCard ? (
          <div className="np-idea-chosen" data-testid="np-idea-chosen">
            <span className="np-dim">From the idea <b data-user-text>{ideaCard.name}</b></span>
            <button type="button" className="np-link" data-testid="np-idea-reference"
                    title="Build it from the starter's tested reference implementation, with its own tests as a gate"
                    onClick={() => setTplCard({ card: ideaCard, target: { kind: "new" }, name: draft.title,
                                                note: directiveOut,
                                                stack: stackAdapter ? adapterLanguage(stackAdapter) : undefined })}>
              Build from its reference ↗
            </button>
          </div>
        ) : null}
      </div>
      <div className="np-two">
        <div>
          <div className="np-lbl"><label htmlFor="dw-new-title">Name</label> <em>optional</em></div>
          <input id="dw-new-title" className="input np-line" value={draft.title} data-testid="dw-new-title"
                 placeholder="Shift swap board" onChange={(e) => set("title", e.target.value)} />
        </div>
        <div>
          <div className="np-lbl"><label htmlFor="np-stack">Stack</label></div>
          <StackSelect id="np-stack" adapters={adapters} value={stackId} onChange={setStackId} />
        </div>
      </div>
      <ConnectorCards cards={connectorCards} selected={connector?.key ?? null} onSelect={setConnector}
                      sub="Built into your app's own style" />
      {more(
        <>
          <div>
            <div className="np-lbl"><label htmlFor="dw-new-audience">Who is it for</label></div>
            <input id="dw-new-audience" className="input np-line" value={draft.audience} data-testid="dw-new-audience"
                   placeholder="A duty manager on a phone, mid-shift"
                   title="The people and setting. A phone mid-shift needs a different screen from an office."
                   onChange={(e) => set("audience", e.target.value)} />
          </div>
          <div>
            <div className="np-lbl">Done when
              <HelpTip label="Acceptance criteria"
                       text="These become acceptance criteria: checks for the build. Leave them out and the panel proposes some for your approval." /></div>
            <Checks checks={draft.criteria} onChange={(c) => set("criteria", c)} testid="dw-new-criterion"
                    placeholder="A member of staff can offer a shift in under 30 seconds" />
          </div>
          {needsRepository ? (
            <div>
              <div className="np-lbl">Read first
                <HelpTip label="Links to read first"
                         text="Links the agents open before designing or writing: an API doc, a repo to follow, a design. Fetched and read, so they must open without a login." /></div>
              <Links links={draft.links} onChange={(l) => set("links", l)} testid="dw-new-link" />
            </div>
          ) : null}
          <TalksTo apps={apps.map((a) => ({ key: a.key, app: a.app }))} rows={talks} onChange={setTalks}
                   gate={linkGate} />
        </>,
        "Who it's for, done-when checks, links to read first, and the apps it talks to",
      )}
      {declaredForm}
    </>
  );

  const appCard = (a: AppRow) => {
    const on = app?.key === a.key;
    return (
      <button key={a.key} type="button" role="radio" aria-checked={on} className="np-app"
              data-on={on || undefined} data-testid={`np-app-${a.key}`} title={a.brief || a.app}
              onClick={() => { setAppId(a.key); setPickApp(false); setProposal(null); setError(null); }}>
        <span className="np-art"><AppArt app={a.app} scope={`np-${a.key}`} /></span>
        <span className="np-app-t"><b data-user-text>{a.app}</b><span>{appLine(a)}</span></span>
      </button>
    );
  };

  const appPicker = (
    <div>
      <div className="np-lbl">App <em>{apps.length} built by your team</em></div>
      <div className="np-find">
        <input className="input np-line" value={appQuery} placeholder="Find an app…" aria-label="Find an app"
               data-testid="np-app-find" onChange={(e) => setAppQuery(e.target.value)} />
        {apps.length > 6 ? (
          <button type="button" className="np-add" data-testid="np-app-all" aria-expanded={allApps}
                  onClick={() => setAllApps((v) => !v)}>
            {allApps ? "Show fewer" : `Show all ${apps.length}`}
          </button>
        ) : null}
        {app ? (
          <button type="button" className="np-add" data-testid="np-app-keep" onClick={() => setPickApp(false)}>
            Keep {app.app}
          </button>
        ) : null}
      </div>
      <div className="np-apps" role="radiogroup" aria-label="Which app" data-all={allApps || undefined}
           data-testid="np-apps">
        {shownApps.map(appCard)}
        {appsLoaded && !apps.length ? (
          // NO DEAD END: a connected repository BOSS has not built in yet is changed through
          // Try it (one change, one PR), not "start a new one" over the existing code.
          <span className="np-empty" data-testid="np-no-apps">
            Nothing built here yet. To change a connected repository, use{" "}
            <a href="?area=setup&focus=try" data-testid="np-no-apps-try">Try it in Repo and models</a>;
            or start a new app.
          </span>
        ) : null}
        {appsLoaded && apps.length && !shownApps.length ? (
          <span className="np-empty">No app matches.</span>
        ) : null}
      </div>
    </div>
  );

  /** THE APP, AS A CARD: name, stack, repo and branch, whether it works today; then its tiles. */
  const appContextCard = app ? (
    <section className="np-ctx-card" aria-labelledby="np-ctx-h">
      <div className="np-ctx-head">
        <span className="np-art"><AppArt app={app.app} scope={`np-base-${app.key}`} /></span>
        <span className="np-ctx-name" data-testid="np-builds-on">
          <h3 id="np-ctx-h" data-user-text>{app.app}</h3>
          <span data-user-text>{[appStack ? stackLabel(appStack) : null, app.repo,
                                  app.prNumber ? `PR #${app.prNumber} branch` : null,
                                  `${app.builds.length} build${app.builds.length === 1 ? "" : "s"} so far`]
            .filter(Boolean).join(" · ")}</span>
          {(() => {
            // "Working today" only when a live preview or a healthy deploy shows it.
            const w = workingWord(app);
            return w.working ? <span className="np-keep" title={w.text}>● {w.text}</span>
              : <span className="np-dim" title={w.text}>{w.text}</span>;
          })()}
          {previewId ? (
            <span className="np-anchor" data-testid="np-preview-anchor"
                  title="The change is anchored to the preview you were looking at">
              ◉ Anchored to its running preview
            </span>
          ) : null}
        </span>
        <button type="button" className="np-link" data-testid="np-app-change" aria-expanded={pickApp}
                onClick={() => setPickApp((v) => !v)}>Change app</button>
      </div>
      <AppContext projectId={projectId} app={app} />
    </section>
  ) : null;

  const addMoreCount = kinds.length + addChecks.filter((c) => c.trim() && c !== KEEPS_WORKING).length
    + addLinks.length + talks.length;

  const addBrief = (
    <>
      {!app || pickApp ? appPicker : null}
      {appContextCard}
      <div>
        <div className="np-lbl"><label htmlFor="np-gain">What should change?</label></div>
        <BigBox id="np-gain" testid="np-gain" label="What should change?" value={gain}
                onChange={(v) => { setGain(v); setProposal(null); }}
                placeholder="Let a sales rep snooze a lead until a date."
                onFull={() => setFull("gain")} onSubmit={() => void primary()} />
      </div>
      {proposalView}
      {/* ADD A CONNECTOR to this app: only the ones that fit its stack, built in its own look. */}
      {app ? (
        <ConnectorCards cards={connectorCards} stack={appStack} selected={connector?.key ?? null}
                        inApp={appTemplates.map((t) => t.key)} appName={app.app}
                        heading="Add connectors" sub="Built into its screens and style"
                        onSelect={setConnector} testId="np-add-connectors" />
      ) : null}
      {more(
        <>
          <div className="np-kinds" role="group" aria-label="What kind of change">
            {CHANGE_CHIPS.map((c) => {
              const on = kinds.includes(c.label);
              return (
                <button key={c.key} type="button" className="np-pill np-kind" aria-pressed={on} title={c.tip}
                        data-testid={`np-kind-${c.key}`}
                        onClick={() => {
                          setKinds((k) => toggleKind(k, c.label));
                          setAddChecks((checks) => checksWithKind(checks, c.expected, !on));
                          setProposal(null);
                        }}>
                  {c.label}
                </button>
              );
            })}
          </div>
          <div>
            <div className="np-lbl">Done when</div>
            <Checks checks={addChecks} onChange={(c) => { setAddChecks(c); setProposal(null); }}
                    testid="np-check" placeholder="A snoozed lead comes back on its date" />
          </div>
          <div>
            <div className="np-lbl">Read first</div>
            <Links links={addLinks} onChange={setAddLinks} testid="np-link" />
          </div>
          <TalksTo apps={apps.filter((a) => a.key !== app?.key).map((a) => ({ key: a.key, app: a.app }))}
                   rows={talks} onChange={setTalks} gate={linkGate} />
        </>,
        "What kind of change, done-when checks, links to read first, and the apps it talks to",
        addMoreCount,
      )}
      {addPath.startsWith("@") ? null : declaredForm}
    </>
  );

  const team = (
    <div className="np-fact" data-testid="np-team">
      <small>Team<button type="button" className="np-fact-act" aria-expanded={editTeam}
                         data-testid="np-team-edit" onClick={() => setEditTeam((v) => !v)}>
        {editTeam ? "Done" : "Edit"}</button></small>
      <div className="np-team-row">
        <span className="np-faces" aria-label={faces.map((k) => nameOf(k).name).join(", ")}>
          {faces.slice(0, 7).map((k) => (
            <span key={k} className="np-face" title={nameOf(k).name}><AgentAvatar agent={k} size={26} /></span>
          ))}
        </span>
        <b className="np-team-names">{crewNamed ? "The crew that built it" : teamNames || "Chosen when it starts"}</b>
        {!crewNamed && teamMore ? <span className="np-team-more">{teamMore}</span> : null}
      </div>
    </div>
  );

  /** One repo / Several repos — or, with fewer than two repositories, where to add more. */
  const layoutSwitch = !needsRepository || projectRepos === null ? null : severalOffered ? (
    <span className="mr-ui-seg" role="radiogroup" aria-label="Where the code lives" data-testid="np-layout">
      <button type="button" role="radio" aria-checked={layout === "one"} data-testid="np-layout-one"
              title="Everything in one repository. The simple default." onClick={() => void chooseLayout("one")}>One repo</button>
      <button type="button" role="radio" aria-checked={layout === "several"} data-testid="np-layout-several"
              title="Each part of the app in its own repository."
              {...manageGate.props}
              onClick={manageGate.guard(() => void chooseLayout("several"))}>Several repos{manageGate.sr}</button>
    </span>
  ) : (
    <span className="mr-map-hint" data-testid="np-layout-hint">
      Several repos needs two or more in this project ·{" "}
      <a className="mr-ui-link" href={consoleHref(typeof window !== "undefined" ? window.location.search : "",
                                                  MULTIREPO_PLACES.projectRepos)}>Add repositories</a>
    </span>
  );

  const severalEditor = several ? (() => {
    const all = mapRepoNames();
    const readOnly = mapRepoNames("deploy_config").filter((r) => !mapRepoNames("code").includes(r));
    return (
      <div className="mr-map np-where" data-testid="np-several">
        {suggesting ? <p className="np-dim">Suggesting a mapping…</p> : (
          <ComponentRows rows={mapRows} repos={projectRepos ?? []} onChange={setMapRows} disabled={busy} testPrefix="np-map" />
        )}
        <div className="mr-map-head">
          <button type="button" className="mr-ui-link" data-testid="np-map-add" disabled={busy}
                  onClick={() => setMapRows((rows) => [...rows, { key: rowKey(), name: "", binding_id: "", path: "/", kind: "code", depends_on: [] }])}>
            <Plus size={12} aria-hidden /> Add a component
          </button>
          <span className="mr-ui-spacer" />
          <span className="np-dim" style={{ fontSize: 12 }}>Suggestions come from repository names and languages.</span>
        </div>
        <div className="mr-ui-note" data-testid="np-map-lock">
          <LockKey size={15} aria-hidden />
          <div>
            Agents in this project may touch <b>{all.length === 1 ? "this repository only" : `these ${all.length} repositories only`}</b>
            {readOnly.length ? ` (${listWords(readOnly)} read only)` : ""}. You confirm this before the first build.
          </div>
        </div>
        <div className="mr-ui-note" data-testid="np-map-linked">
          <GitPullRequest size={15} aria-hidden />
          <div>With several repositories one run opens <b>linked pull requests</b>, one per repository, reviewed and approved <b>as a set</b>, and deploys them together.</div>
        </div>
      </div>
    );
  })() : null;

  const livesIn = adding ? (
    <div className="np-fact" data-testid="np-lands">
      <small>Result</small>
      <b title="From the app's PR: an open one is updated on its branch; a merged one means a new PR">{landsAs(app)}</b>
    </div>
  ) : needsRepository && several ? (
    <div className="np-fact" data-testid="np-lives" data-layout="several">
      <small>Where the code lives</small>
      <b>{mapRepoNames().length} repositories</b>
      {layoutSwitch}
    </div>
  ) : needsRepository ? (
    <div className="np-fact" data-testid="np-lives">
      <small>Repository<button type="button" className="np-fact-act" aria-expanded={editRepo}
                             data-testid="np-repo-change" onClick={() => setEditRepo((v) => !v)}>
        {editRepo ? "Done" : "Change"}</button></small>
      <b data-user-text>{draft.repository || suggested?.name
          || (repoHint.needsConnection ? "No repository yet" : "The project default")}
        <span className="np-dim"> · {(repos.find((r) => r.full_name === (draft.repository || suggested?.name))
          ?.default_branch) || "main"}</span></b>
      {repoHint.needsConnection ? (
        <span className="dw-field-hint" data-testid="dw-new-repo-hint">
          None connected.{" "}
          <a data-testid="dw-new-repo-connect"
             title="A fine-grained token with Contents and Pull requests: read and write"
             href={consoleHref(
               typeof window !== "undefined" ? window.location.search : "",
               { area: "setup", focus: "repository" })}>
            Connect one
          </a>
        </span>
      ) : null}
      {layoutSwitch}
    </div>
  ) : null;

  const repoChoices = editRepo && needsRepository ? (
    <div className="np-choices" role="radiogroup" aria-label="Repository" data-testid="np-repos">
      <button type="button" role="radio" className="np-choice"
              aria-checked={draft.repository === ""}
              data-testid="dw-new-repo-default"
              title={`Where the branch and the pull request go.${repoHint.text}`}
              onClick={() => set("repository", "")}>
        {suggested ? suggested.name
          : repoHint.needsConnection ? "No repository yet" : "The project default"}
      </button>
      {repos
        .filter((r) => r.full_name !== suggested?.name)
        .map((r) => (
          <button type="button" role="radio" className="np-choice" key={r.id}
                  aria-checked={draft.repository === r.full_name}
                  data-testid={`dw-new-repo-${r.full_name}`}
                  title={`${r.role ? `${r.role} · ` : ""}branch ${r.default_branch || "main"}`}
                  onClick={() => set("repository", r.full_name)}>
            {r.full_name}
          </button>
        ))}
    </div>
  ) : null;

  const currentName = (adding ? cards.find((c) => c.key === addPath)?.short
    : cards.find((c) => c.key === draft.workflowKey)?.short) ?? pathName(current);
  const currentFacts = cardFacts(current);

  const startLabel = busy ? "Starting…"
    : adding ? (addPath === QUICK ? (proposal?.proposal ? "Start the change" : "Propose the change")
      : addPath === SPEC ? "Convene the panel" : "Add the feature")
    : formOnly ? "Start" : "Start the project";

  /** "Change workflow": the recommended paths as cards, then every installed workflow. */
  const chooser = allPaths ? (
    <div className="np-chooser" data-testid="np-chooser">
      {formOnly ? null : (
        <div className="np-paths" role="radiogroup" aria-label="Path" data-n={cards.length}>
          {cards.map((c) => {
            const f = cardFacts(c.key);
            const on = current === c.key;
            return (
              <button key={c.key} type="button" role="radio" aria-checked={on} className="np-path"
                      data-on={on || undefined} title={c.tip} data-testid={`np-path-${c.key.replace("@", "")}`}
                      onClick={() => {
                        if (adding) { setAddPath(c.key); setProposal(null); } else choosePath(c.key);
                      }}>
                {c.recommended ? <span className="np-rec">Recommended</span> : null}
                <b>{c.short}</b><span>{f.sub}</span><span className="np-g">{f.gates}</span>
              </button>
            );
          })}
        </div>
      )}
      {/* ANY INSTALLED WORKFLOW CAN RUN — onto the app too (Security Hardening, UX Certification,
          Fast Fix), with the same extends_run_id the cards send. */}
      <div className="np-choices" role="radiogroup" aria-label="Workflow" data-testid="np-all-list">
        {adding ? null : (
          <button type="button" role="radio" className="np-choice"
                  aria-checked={draft.workflowKey === ""}
                  data-testid="dw-new-workflow-default"
                  title="Whatever this project maps for new work"
                  onClick={() => choosePath("")}>
            This project&apos;s usual path
          </button>
        )}
        {workflows.map((w) => (
          <button type="button" role="radio" className="np-choice" key={w.id}
                  aria-checked={(adding ? addPath : draft.workflowKey) === w.workflow_key}
                  data-testid={adding ? `np-add-workflow-${w.workflow_key}` : `dw-new-workflow-${w.workflow_key}`}
                  title={String(catalogue.find((c) => c.key === w.workflow_key)?.description
                    || w.description || "")}
                  onClick={() => {
                    if (adding) { setAddPath(w.workflow_key); setProposal(null); } else choosePath(w.workflow_key);
                  }}>
            {w.name}
          </button>
        ))}
      </div>
    </div>
  ) : null;

  /** HOW IT'S BUILT: the workflow, its steps (yours in amber), where it lands, and Start. */
  const plan = (
    <aside className="np-plan" aria-label="How it's built" data-open={planOpen || undefined}>
      <button type="button" className="np-plan-toggle" aria-expanded={planOpen}
              data-testid="np-plan-toggle" onClick={() => setPlanOpen((v) => !v)}>
        <span>{summary}</span><span aria-hidden>{planOpen ? "⌃" : "⌄"}</span>
      </button>
      <div className="np-plan-body">
        <div className="np-plan-head">
          <h3>How it&apos;s built</h3>
          <button type="button" className="np-link" aria-expanded={allPaths} data-testid="np-all-paths"
                  onClick={() => setAllPaths((v) => !v)}>
            {allPaths ? "Done" : "Change workflow"}
          </button>
        </div>
        <div className="np-flow" data-testid="np-flow" title={String(entry?.description || chosen?.description || "")}>
          <b data-user-text>{currentName}</b>
          <span>{adding && app ? `Builds on ${app.app}'s branch · ` : ""}{currentFacts.sub} · {currentFacts.gates}</span>
        </div>
        {chooser}
        {timeline.length ? <Timeline steps={timeline} /> : (
          <p className="np-dim" data-testid="np-no-steps">The project&apos;s mapping picks the steps.</p>
        )}
        <div className="np-facts">{team}{livesIn}</div>
        {editTeam ? agentPicker : null}
        {several ? severalEditor : repoChoices}
        {readyDots}
        {readiness?.missing?.length ? (
          <ReadinessPanel readiness={readiness} accepted={startAnyway} projectId={projectId}
                          onAccept={setStartAnyway} />
        ) : null}
      </div>
      <div className="np-foot">
        <span className="np-cost" data-testid="np-cost">
          {cost ? <span className="np-chip">{adding ? "Changes" : "Builds"} like this cost <b>{cost}</b></span> : null}
          {several ? (() => {
            const code = mapRepoNames("code").length;
            return <span className="np-chip" data-testid="np-cost-several">{code} {code === 1 ? "repository" : "repositories"} · {code} linked {code === 1 ? "PR" : "PRs"} · you approve once</span>;
          })() : <span className="np-chip">{total.steps} steps · {youApproveWord(total.approvals)}</span>}
          <HelpTip label="Cost" text="Starting spends model budget. It stops and asks before it deploys anything." />
        </span>
        <StackWarning state={stackCheck} testId="np-stack-warning" />
        {blocked && !(adding && proposal?.proposal) ? (
          <span className="np-blocked" data-testid="np-blocked" role="status">{blocked}</span>
        ) : null}
        <button type="button" className="np-btn np-primary np-start" data-testid="dw-new-start"
                disabled={busy || (!!blocked && !(adding && proposal?.proposal))} {...primaryGate.props}
                onClick={primaryGate.guard(() => void primary())}>
          {startLabel}<span className="np-kbd" aria-hidden>⌘↵</span>{primaryGate.sr}
        </button>
        {deploysOnlyWithYou && !formOnly ? (
          <span className="np-deploy-note" data-testid="np-deploy-note">Nothing deploys without your approval.</span>
        ) : null}
        <span className="np-actions">
          {/* NOT THE DEFAULT, STILL HERE: talk it through with the planner first (a PRD, then the
              build). Every "New build" door opens this composer; this is its agent route. */}
          {!formOnly ? (
            <GatedButton permission="meeting:convene" type="button" className="np-link np-plan-agent" data-testid="np-plan-agent"
                         title="Talk it through first: a PRD, then the build" onClick={() => openPlan("pm", { from: "composer" })}>
              Plan it with an agent
            </GatedButton>
          ) : null}
          {onCancel ? (
            <button type="button" className="np-link" data-testid="np-cancel" onClick={onCancel}>Cancel</button>
          ) : null}
        </span>
      </div>
    </aside>
  );

  return (
    <section className="dw-new np" data-testid="dw-new-project" data-mode={formOnly ? "form" : mode}
             data-workflow={flowKey || undefined} onKeyDown={onKey}>
      <header className="np-head">
        <h2 data-testid="dw-new-heading" data-user-text={lockedForm || undefined}>{heading}</h2>
        {lockedForm ? (
          description ? (
            <p className="np-sub" data-testid="dw-new-subtitle" title={description} data-user-text>{description}</p>
          ) : null
        ) : (
          <div className="np-mode" role="radiogroup" aria-label="What are you making?" data-testid="np-mode">
            <button type="button" role="radio" aria-checked={mode === "new"} data-testid="np-mode-new"
                    onClick={() => setMode("new")}>New app</button>
            <button type="button" role="radio" aria-checked={adding} data-testid="np-mode-add"
                    onClick={() => setMode("add")}>Add a feature</button>
          </div>
        )}
        <span className="np-spacer" />
        {onCancel ? (
          <button type="button" className="np-x" aria-label="Close" data-testid="np-close" onClick={onCancel}>✕</button>
        ) : null}
      </header>
      <div className="np-body">
        <div className="np-brief" data-testid="np-brief">
          {formOnly ? declaredForm ?? <p className="np-dim">Nothing to fill in.</p> : adding ? addBrief : newBrief}
        </div>
        {plan}
      </div>
      {error ? <p className="dw-new-error" role="alert" data-testid="dw-new-error" data-user-text>{error}</p> : null}
      {ideasOpen ? <IdeasDialog ideas={ideas} onPick={pickIdea} onClose={() => setIdeasOpen(false)} /> : null}
      {tplCard ? (
        <TemplateSheet projectId={projectId} card={tplCard.card} target={tplCard.target}
                       initialName={tplCard.name} initialNote={tplCard.note} initialWorkflow={flowKey}
                       initialStack={tplCard.stack}
                       onClose={() => setTplCard(null)}
                       onStarted={(runId) => {
                         setTplCard(null);
                         notifyChanged("code-tasks");
                         showToast("success", `${tplCard.card.name} started`);
                         onStarted(runId);
                       }} />
      ) : null}
      {full ? (
        <FullEditor label={full === "brief" ? "What should it do?" : "What should change?"}
                    value={full === "brief" ? draft.directive : gain}
                    onChange={(v) => (full === "brief" ? set("directive", v) : setGain(v))}
                    onClose={() => setFull(null)} />
      ) : null}
    </section>
  );
}
