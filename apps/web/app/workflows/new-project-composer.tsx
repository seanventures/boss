"use client";

/**
 * The Runs composer: fill in the chosen workflow's start form, confirm the path and who works
 * it, say where it lives when it builds, and start it, with the readiness checklist for what
 * the project still lacks.
 *
 * DRIVEN BY THE SELECTED WORKFLOW (boss-1, 2026-10-03). It used to be a software wizard for
 * every workflow: "Trend to content" was asked "What should it do?" and for a repository, its
 * own Trend and Reviewer email were never drawn, and Start said "Needs Reviewer email, Trend"
 * under a form with nowhere to type them. Now the first step IS the workflow's start form; the
 * product questions appear only when that form is the product brief (it declares `directive`),
 * and "Where it lives" only when a step needs a repository (the readiness check's own answer).
 *
 * Every Start in the console opens this (Library cards, the editor, a run's "Run again" when
 * it was refused), so a run is never started blind. Moved out of delivery-workflows-ui.tsx
 * unchanged when the Workflow area became Runs and Library; re-exported from there.
 */
// React by name: the suite renders with renderToStaticMarkup (classic runtime).
import React, { useEffect, useState } from "react";

import { AgentChip } from "../agent-chip";
import { HelpTip } from "../help-tip";
import { consoleHref } from "../url-intent";
import { apiWrite } from "../../lib/api-write";
import { defaultInputs, invalidInputs, missingRequired, startsFromABrief, suppliedInputs,
         typedInputs, workflowInputFields } from "../../lib/workflow-inputs";
import { ReadinessPanel, WIZARD_COLLECTS, unaccepted, useWorkflowReadiness, type Readiness }
  from "../workflow-readiness-ui";
import { get, post } from "./delivery-api";
import type { CatalogueEntry, Installed } from "../delivery-workflows-ui";

/** A repository this project can build in, from its registry. */
type Repo = { id: string; full_name: string; role?: string | null; default_branch?: string };

/** What the wizard has collected so far. One object so a Back button costs nothing. */
export type NewProjectDraft = {
  title: string;
  directive: string;
  audience: string;
  criteria: string[];
  workflowKey: string;
  repository: string;
  links: string[];
  /** Who will be on it. Empty means "whatever BOSS seeds" — see the wizard's agents step. */
  agents: string[];
  /** What THIS workflow declared it needs — a video to fetch, a clip to trim, a channel to
   *  publish to. Keyed by field name; only filled-in values are sent. */
  inputs: Record<string, string>;
};

const EMPTY_DRAFT: NewProjectDraft = {
  title: "", directive: "", audience: "", criteria: [""],
  workflowKey: "", repository: "", links: [], agents: [], inputs: {},
};

/** The composer's steps. Which are shown, and what they are called, depends on the workflow:
 *  see `composerSteps`. */
export type StepId = "brief" | "path" | "where" | "review";

/**
 * The steps for the selected workflow, in order.
 *
 * "brief" is the workflow's start form, shown when it has one (or is the product brief);
 * "where" (repository and links) only when a step needs a repository. A step that does not
 * apply is not drawn, so the rail renumbers rather than offering a question with no answer.
 */
export function composerSteps(o: { brief: boolean; fields: number; needsRepository: boolean })
  : { id: StepId; label: string }[] {
  const out: { id: StepId; label: string }[] = [];
  if (o.brief || o.fields > 0) {
    out.push({ id: "brief", label: o.brief ? "What you are building" : "What it needs" });
  }
  out.push({ id: "path", label: o.brief ? "How it gets built" : "How it runs" });
  if (o.needsRepository) out.push({ id: "where", label: "Where it lives" });
  out.push({ id: "review", label: "Review" });
  return out;
}

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
 * what a spec review can actually review. The wizard collects the parts and joins them here,
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
 * What the repository step says about where the code goes.
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

/** What stops the operator moving on, said as the thing to do rather than as an error. */
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


/** The start-form fields the composer draws for a workflow on its first step.
 *
 * Everything the workflow declared, less what another step of the composer collects: for a
 * product brief, the directive, name, criteria, repository and links (the product questions
 * and "Where it lives"); for any other workflow, only the repository, and only when "Where it
 * lives" is shown. A workflow somebody made keeps every other field it declared — its `title`
 * may mean a YouTube title, not the run's name. */
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

/**
 * STARTING WORK, WHERE THE WORK IS WATCHED.
 *
 * A wizard, not a box, and shaped by the workflow it starts: its start form first, then the
 * path and who works it, then — only for a workflow that builds — the repository, then a
 * review of what the press does. Start is offered from the first step and is enabled as soon
 * as every required field is filled in and valid.
 */
export function NewProjectComposer({ projectId, actor, workflows, catalogue, onStarted, startWith,
                             example }: {
  projectId: string; actor: string;
  workflows: Installed[];
  catalogue: CatalogueEntry[];
  onStarted: (runId: string | null) => void;
  /** A workflow the operator asked to start, chosen already. Opens on its start form. */
  startWith?: string;
  /** "Try an example": the template's own example brief, filled in for the operator to read
   *  and change — never started unseen. */
  example?: { title?: string; directive?: string } | null;
}) {
  const schemaOf = (key: string) =>
    workflows.find((w) => w.workflow_key === key)?.input_schema
      ?? catalogue.find((c) => c.key === key)?.input_schema;
  /** Each field's declared default, for a workflow just chosen. */
  const defaultsFor = (key: string) => defaultInputs(workflowInputFields(schemaOf(key)));

  const [stepId, setStepId] = useState<StepId>("brief");
  const [draft, setDraft] = useState<NewProjectDraft>(
    startWith ? { ...EMPTY_DRAFT, workflowKey: startWith, inputs: defaultsFor(startWith),
                  title: example?.title ?? "", directive: example?.directive ?? "" }
              : EMPTY_DRAFT);
  /** The missing needs the operator chose to start without — sent as `start_anyway`. */
  const [startAnyway, setStartAnyway] = useState<string[]>([]);
  /** The checklist from a refused start, when the path was "this project's usual path" and
   *  could not be asked about beforehand. */
  const [refusal, setRefusal] = useState<Readiness | null>(null);
  // WHO WILL BE ON IT. Read from the API rather than guessed here: start_run seeds from
  // STAGE_ROLES plus the chosen workflow's approver roles, and a second copy of that rule in
  // the console would drift from the one that actually runs.
  const [defaultAgents, setDefaultAgents] = useState<string[]>([]);
  const [allRoles, setAllRoles] = useState<Array<{ role_key: string;
                                                   display_name?: string | null }>>([]);
  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await fetch(
          `/api/project-agents?project_id=${encodeURIComponent(projectId)}&defaults=1`
          + (draft.workflowKey ? `&workflow_key=${encodeURIComponent(draft.workflowKey)}` : ""),
          { cache: "no-store" });
        if (r.ok && alive) setDefaultAgents(((await r.json())?.default_roles ?? []) as string[]);
      } catch { /* the wizard still starts the run; BOSS seeds them either way */ }
    })();
    return () => { alive = false; };
  }, [projectId, draft.workflowKey]);
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
  const [suggested, setSuggested] = useState<{ name: string; source: string } | null>(null);
  const repoHint = repositoryHint(suggested, repos.length);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof NewProjectDraft>(key: K, value: NewProjectDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const setInput = (name: string, value: string) =>
    setDraft((d) => ({ ...d, inputs: { ...d.inputs, [name]: value } }));

  // WHERE THE CODE GOES, ANSWERED BEFORE IT IS ASKED. The project's registry knows its
  // repositories and which one is primary, so the field arrives filled in.
  useEffect(() => {
    let alive = true;
    void (async () => {
      const r = await get<{ repositories: Repo[]; suggested: string; suggested_source: string }>(
        "repositories", projectId);
      // A project with no registry is a normal state, not an error to surface: the repository
      // step says a build needs one and where to connect it (see repositoryHint).
      if (!alive || !r || "error" in r) return;
      setRepos(r.repositories ?? []);
      if (r.suggested) setSuggested({ name: r.suggested, source: r.suggested_source });
    })();
    return () => { alive = false; };
  }, [projectId]);

  /** The catalogue entry behind an installed workflow, for its description and gates. */
  const describe = (key: string): CatalogueEntry | undefined =>
    catalogue.find((c) => c.key === key);

  // WHAT THE CHOSEN WORKFLOW IS. Read from the installed definition rather than hardcoded: a
  // custom workflow declares its own start form, and the composer has to draw whatever it is.
  const chosen = workflows.find((w) => w.workflow_key === draft.workflowKey);
  const entry = describe(draft.workflowKey);
  const schema = schemaOf(draft.workflowKey);
  const brief = startsFromABrief(schema, !!draft.workflowKey);
  const allFields = workflowInputFields(schema);

  // CAN IT RUN HERE. Asked as soon as the path is known, so what is missing is on screen
  // before Start rather than in a run that fails at its last step.
  const asked = useWorkflowReadiness(projectId, draft.workflowKey,
                                     suppliedInputs(allFields, draft.inputs));
  const readiness = asked ?? refusal;
  const notReady = unaccepted(readiness, startAnyway);

  const needsRepository = needsARepository({ workflowKey: draft.workflowKey, readiness, entry,
                                             schema });
  const declaredInputs = composerFields(schema, brief, needsRepository);
  const steps = composerSteps({ brief, fields: declaredInputs.length, needsRepository });
  const at = Math.max(0, steps.findIndex((s) => s.id === stepId));
  const current = steps[at].id;
  const go = (index: number) => setStepId(steps[Math.min(Math.max(index, 0), steps.length - 1)].id);

  // WHAT WILL ACTUALLY BE SENT, worked out once and used for the guard, the review and the
  // start.
  const inputsToSend = suppliedInputs(declaredInputs, draft.inputs);
  const stillBlank = missingRequired(declaredInputs, inputsToSend);
  const malformed = invalidInputs(declaredInputs, inputsToSend);

  // WHAT THE FIRST STEP STILL NEEDS: a product brief's description, and every required field
  // filled in and well-formed. Only this makes the step ✓, and only this disables its Next.
  const briefReason = blockingReason(0, draft, !brief)
    || (stillBlank.length ? `Needs ${stillBlank.join(", ")}` : "")
    || (malformed.length ? `Check ${malformed.join(", ")}` : "");
  const complete = (id: StepId) => (id === "brief" ? !briefReason : true);
  const stepBlocked = current === "brief" ? briefReason : "";

  // `??` WAS WRONG HERE AND IT COST A RUN: "" is "nothing blocking", and `??` never looks past
  // it. A required input left blank must stop the start at the form, not at the runtime.
  const blocked = blockingReason(0, draft, !brief)
    || (stillBlank.length ? `Needs ${stillBlank.join(", ")}` : "")
    || (malformed.length ? `Check ${malformed.join(", ")}` : "")
    // So does something the project lacks, unless the operator chose to start without it.
    || (notReady.length
        ? `Not ready here yet: ${notReady.map((m) => m.label).join("; ")} — see below.`
        : "");

  const description = draft.workflowKey
    ? String(chosen?.description || entry?.description || "").split("\n")[0].trim()
    : "";
  const heading = draft.workflowKey
    ? (chosen?.name ?? entry?.name ?? draft.workflowKey)
    : "New project";

  const start = async () => {
    if (blocked || busy) return;
    setBusy(true);
    setError(null);
    try {
      const input = startInput({ brief, draft, fields: declaredInputs, supplied: inputsToSend,
                                 needsRepository });
      const r = await post("start", {
        project_id: projectId, actor,
        ...(draft.workflowKey
            ? { workflow_key: draft.workflowKey }
            : { work_class: "project.new" }),
        input,
        ...(startAnyway.length ? { start_anyway: startAnyway } : {}),
      });
      if (!r.ok) {
        setError(r.error ?? "Could not start it.");
        // Refused for what the project lacks: draw the list where the operator is looking.
        if ("readiness" in r && r.readiness) setRefusal(r.readiness);
        return;
      }
      const run = (r.data as { run?: { id?: string }; id?: string } | undefined);
      // WHO IS ON IT, recorded as a CHOICE — but only when it differs from what start_run
      // already seeded. Writing an identical list back as `operator` would claim a decision
      // nobody made, and the panel distinguishes the two on purpose.
      const app = String(input.title || heading);
      const chosen = draft.agents;
      const sameAsDefault = chosen.length === defaultAgents.length
        && chosen.every((a) => defaultAgents.includes(a));
      if (chosen.length && !sameAsDefault) {
        try {
          await apiWrite("/api/project-agents", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: projectId, app, actor,
                                   role_keys: chosen }),
          });
          // Anyone the operator took OFF the default list has to come off for real, or the
          // wizard would show a choice the run does not honour.
          for (const gone of defaultAgents.filter((d) => !chosen.includes(d))) {
            await apiWrite("/api/project-agents", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ project_id: projectId, app, actor,
                                     action: "unassign", role_key: gone }),
            });
          }
        } catch { /* the run is started and seeded; the assignment can be fixed on the run */ }
      }
      setDraft(EMPTY_DRAFT);
      setStepId("brief");
      onStarted(String(run?.run?.id ?? run?.id ?? "") || null);
    } finally {
      setBusy(false);
    }
  };

  const agentsNow = draft.agents.length ? draft.agents : defaultAgents;
  const agentPicker = (testid: string, note?: string) => (
    <>
      {agentsNow.map((key) => (
        <AgentChip key={key} roleKey={key}
                   label={allRoles.find((r) => r.role_key === key)?.display_name
                          || key.replace(/_/g, " ")}
                   note={note}>
          <button type="button" className="aot-x" aria-label={`Remove ${key}`}
                  onClick={() => set("agents", agentsNow.filter((k) => k !== key))}>✕</button>
        </AgentChip>
      ))}
      <select className="mr-select aot-pick" value="" data-testid={testid}
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
    </>
  );

  return (
    <section className="dw-new dw-wizard" data-testid="dw-new-project"
             data-workflow={draft.workflowKey || undefined}>
      <div className="dw-new-head">
        <h3 data-testid="dw-new-heading" data-user-text>{heading}</h3>
        {/* WHAT THIS WORKFLOW DOES, in its author's words: one line, the rest on hover. The
            product boilerplate belongs to the usual path only. */}
        {draft.workflowKey ? (
          description ? (
            <p className="detail dw-new-sub" data-testid="dw-new-subtitle" title={description}
               data-user-text>{description}</p>
          ) : null
        ) : (
          <p className="detail dw-new-sub" data-testid="dw-new-subtitle">
            Specs, builds, previews; asks before deploying.
          </p>
        )}
      </div>

      {/* WHERE YOU ARE AND WHAT IS LEFT. Only the steps this workflow has, numbered in order;
          ✓ only on a step passed with its required fields filled in. */}
      <ol className="dw-wiz-rail" data-testid="dw-wiz-rail">
        {steps.map((s, i) => {
          const done = i < at && complete(s.id);
          const state = i === at ? "current" : done ? "done" : i < at ? "incomplete" : "todo";
          return (
            <li key={s.id} data-state={state} data-step={s.id}>
              <button type="button" className="dw-wiz-crumb"
                      data-testid={`dw-wiz-crumb-${i}`}
                      // Backwards freely; forwards only over ground already covered.
                      disabled={i > at}
                      onClick={() => go(i)}>
                <span className="dw-wiz-num" aria-hidden>{done ? "✓" : i + 1}</span>
                <span>{s.label}</span>
              </button>
            </li>
          );
        })}
      </ol>

      {current === "brief" ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-brief">
          {brief ? (
            <>
              <div className="dw-field">
                <span className="dw-field-label"><label htmlFor="dw-new-directive">What should it do?</label> <HelpTip
                  label="Project description"
                  text="The panel and builder use your words. Describe what the product does and what a person can accomplish with it in two or three sentences." /></span>
                <textarea
                  id="dw-new-directive"
                  className="dw-new-input" rows={5} value={draft.directive}
                  data-testid="dw-new-directive"
                  placeholder={"A shift-swap board where staff can offer a shift they cannot work "
                    + "and colleagues can claim it, with the manager approving the swap."}
                  onChange={(e) => set("directive", e.target.value)}
                  onKeyDown={(e) => {
                    // ⌘/Ctrl+Enter starts with the defaults, as every other composer does.
                    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                      e.preventDefault();
                      if (!blocked) void start();
                    }
                  }}
                />
              </div>

              <div className="dw-field">
                <span className="dw-field-label"><label htmlFor="dw-new-audience">Who is it for?</label> <em>optional</em> <HelpTip
                  label="Intended users"
                  text="Describe the people and setting. A duty manager using a phone mid-shift needs a different interface from office staff." /></span>
                <input id="dw-new-audience" className="input" value={draft.audience} data-testid="dw-new-audience"
                       placeholder="a duty manager on a phone, mid-shift"
                       onChange={(e) => set("audience", e.target.value)} />
              </div>

              <fieldset className="dw-field">
                <legend className="dw-field-label">
                  How will you know it works? <em>optional</em> <HelpTip
                    label="Acceptance criteria"
                    text="These become acceptance criteria: checks for the build. Leave them blank and the panel will propose criteria for your approval." />
                </legend>
                {draft.criteria.map((c, i) => (
                  <div className="dw-crit-row" key={i}>
                    <input className="input" value={c} data-testid={`dw-new-criterion-${i}`}
                           placeholder={i === 0
                             ? "a member of staff can offer a shift in under 30 seconds"
                             : "another thing that must be true"}
                           onChange={(e) => {
                             const next = [...draft.criteria];
                             next[i] = e.target.value;
                             set("criteria", next);
                           }} />
                    {draft.criteria.length > 1 ? (
                      <button type="button" className="cap-btn dw-crit-drop"
                              aria-label="Remove this criterion"
                              onClick={() => set("criteria",
                                draft.criteria.filter((_, j) => j !== i))}>×</button>
                    ) : null}
                  </div>
                ))}
                <button type="button" className="linklike" data-testid="dw-new-criterion-add"
                        onClick={() => set("criteria", [...draft.criteria, ""])}>
                  + another
                </button>
              </fieldset>
            </>
          ) : null}

          {/* THE WORKFLOW'S OWN START FORM. Rendered FROM its declaration (input_schema, either
              shape): label, required mark, the author's help as a tooltip, the example as the
              placeholder, the default as the starting value, and the control its type asks
              for. For a product brief, only what the questions above do not already ask. */}
          {declaredInputs.length ? (
            <div className="dw-wiz-inputs" data-testid="dw-workflow-inputs">
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
          ) : null}
        </div>
      ) : null}

      {current === "path" ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-path">
          <span className="dw-field-label">Which path? <HelpTip
            label="Workflow paths"
            text="Paths differ in their steps, reviews, evidence and approval gates. Switching path changes what the first step asks for." /></span>
          <div className="dw-wiz-choices" role="radiogroup" aria-label="Workflow">
            <button type="button" role="radio" className="dw-wiz-choice"
                    aria-checked={draft.workflowKey === ""}
                    data-selected={draft.workflowKey === ""}
                    data-testid="dw-new-workflow-default"
                    title="Whatever this project maps for new work"
                    onClick={() => set("workflowKey", "")}>
              <span className="dw-wiz-choice-name">This project&apos;s usual path</span>
            </button>
            {workflows.map((w) => {
              const e = describe(w.workflow_key);
              return (
                <button type="button" role="radio" className="dw-wiz-choice" key={w.id}
                        aria-checked={draft.workflowKey === w.workflow_key}
                        data-selected={draft.workflowKey === w.workflow_key}
                        data-testid={`dw-new-workflow-${w.workflow_key}`}
                        onClick={() => setDraft((d) => ({
                          ...d, workflowKey: w.workflow_key,
                          inputs: { ...defaultsFor(w.workflow_key), ...d.inputs } }))}>
                  <span className="dw-wiz-choice-name">{w.name}</span>
                  {e ? <span className="detail" data-user-text>{e.description}</span>
                     : w.description ? <span className="detail" data-user-text>{w.description}</span>
                     : null}
                  {e ? (
                    <span className="dw-wiz-choice-meta detail">
                      {e.step_count} steps
                      {e.gates?.length
                        ? ` · you approve: ${e.gates.join(", ")}`
                        : " · no approval gates"}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>

          {/* WHO WILL BE ON IT. The defaults are shown pre-selected rather than as an empty
              box: they are what BOSS seeds anyway. */}
          <span className="dw-field-label dw-agents-label">Who works on it <HelpTip
            label="Agents"
            text="The agents that would run this path anyway. Take one off and it may not sign off this run's gates; add one and it may." /></span>
          <div className="aot-list" data-testid="dw-wiz-agents">{agentPicker("dw-wiz-agent-add")}</div>
          {draft.agents.length ? (
            <span className="dw-field-hint">
              Changed.{" "}
              <button type="button" className="dw-linkish"
                      onClick={() => set("agents", [])}>Put it back</button>
            </span>
          ) : null}
        </div>
      ) : null}

      {current === "where" ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-where">
          <span className="dw-field-label">Which repository? <HelpTip
            label="Repository"
            text={`Where the branch and the pull request go.${repoHint.text}`} /></span>
          {repoHint.needsConnection ? (
            <span className="dw-field-hint" data-testid="dw-new-repo-hint">
              None connected.{" "}
              <a data-testid="dw-new-repo-connect"
                 title="A fine-grained token with Contents and Pull requests: read and write"
                 href={consoleHref(
                   typeof window !== "undefined" ? window.location.search : "",
                   { area: "setup" })}>
                Connect one
              </a>
            </span>
          ) : null}
          <div className="dw-wiz-choices" role="radiogroup" aria-label="Repository">
            <button type="button" role="radio" className="dw-wiz-choice"
                    aria-checked={draft.repository === ""}
                    data-selected={draft.repository === ""}
                    data-testid="dw-new-repo-default"
                    title={suggested
                      ? "What this project resolves to today"
                      : "Resolved when the build starts"}
                    onClick={() => set("repository", "")}>
              <span className="dw-wiz-choice-name">
                {suggested ? suggested.name
                  : repoHint.needsConnection ? "No repository yet" : "The project default"}
              </span>
            </button>
            {repos
              .filter((r) => r.full_name !== suggested?.name)
              .map((r) => (
                <button type="button" role="radio" className="dw-wiz-choice" key={r.id}
                        aria-checked={draft.repository === r.full_name}
                        data-selected={draft.repository === r.full_name}
                        data-testid={`dw-new-repo-${r.full_name}`}
                        onClick={() => set("repository", r.full_name)}>
                  <span className="dw-wiz-choice-name">{r.full_name}</span>
                  <span className="detail">
                    {r.role ? `${r.role} · ` : ""}branch {r.default_branch || "main"}
                  </span>
                </button>
              ))}
          </div>

          <fieldset className="dw-field">
            <legend className="dw-field-label">
              Read first <em>optional</em> <HelpTip
                label="Links to read first"
                text="Links the agents must open before designing or writing: an API doc, a repo whose conventions to follow, a design. They are fetched and read, not indexed or searched, so they must be reachable without a login." />
            </legend>
            {[...draft.links, ""].map((link, i) => (
              <input className="input dw-link-row" key={i} value={link}
                     data-testid={`dw-new-link-${i}`}
                     placeholder="https://docs.example.com/api"
                     onChange={(e) => {
                       const next = [...draft.links];
                       if (i < next.length) next[i] = e.target.value;
                       else next.push(e.target.value);
                       set("links", next.filter((l, j) => l.trim() || j < next.length - 1));
                     }} />
            ))}
          </fieldset>
        </div>
      ) : null}

      {current === "review" ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-review">
          {/* WHAT IS ABOUT TO HAPPEN. Only what this workflow will be given. */}
          <dl className="dw-wiz-review">
            {brief ? (
              <>
                <dt>Building</dt>
                <dd data-testid="dw-review-directive" data-user-text>{briefFrom(draft) || "—"}</dd>
              </>
            ) : null}
            <dt>Path</dt>
            <dd data-testid="dw-review-workflow" data-user-text>
              {draft.workflowKey ? heading : "This project's usual path"}
            </dd>
            {needsRepository ? (
              <>
                <dt>Repository</dt>
                <dd data-testid="dw-review-repo" data-user-text>
                  {draft.repository || suggested?.name || "the project default"}
                </dd>
              </>
            ) : null}
            <dt>Agents</dt>
            <dd data-testid="dw-review-agents">
              <div className="aot-list">
                {agentPicker("dw-review-agent-add", draft.agents.length ? "chosen" : "by default")}
              </div>
            </dd>
            {Object.keys(inputsToSend).length ? (
              <>
                <dt>It will be given</dt>
                <dd data-testid="dw-review-inputs">
                  <ul>
                    {declaredInputs
                      .filter((f) => inputsToSend[f.name])
                      .map((f) => (
                        <li key={f.name} data-testid={`dw-review-input-${f.name}`}>
                          <span className="detail">{f.label}: </span>
                          <span data-user-text>{f.kind === "boolean"
                            ? (inputsToSend[f.name] === "true" ? "yes" : "no")
                            : inputsToSend[f.name]}</span>
                        </li>
                      ))}
                  </ul>
                </dd>
              </>
            ) : null}
            {brief && criteriaFrom(draft).length ? (
              <>
                <dt>Done when</dt>
                <dd data-testid="dw-review-criteria" data-user-text>
                  <ul>{criteriaFrom(draft).map((c) => <li key={c}>{c}</li>)}</ul>
                </dd>
              </>
            ) : null}
            {needsRepository && linksFrom(draft).length ? (
              <>
                <dt>Reads first</dt>
                <dd data-testid="dw-review-links" data-user-text>
                  <ul>{linksFrom(draft).map((l) => <li key={l}><code>{l}</code></li>)}</ul>
                </dd>
              </>
            ) : null}
          </dl>
          <span className="detail dw-wiz-warn">
            Starting spends model budget.{brief ? <> <HelpTip label="Cost"
              text="Research, a panel and a build. It stops and asks before it deploys anything." /></> : null}
          </span>
        </div>
      ) : null}

      <ReadinessPanel readiness={readiness} accepted={startAnyway} projectId={projectId}
                      onAccept={setStartAnyway} />

      {blocked ? <p className="dw-new-error" data-testid="dw-wiz-blocked">{blocked}</p> : null}

      <div className="dw-wiz-foot">
        {at > 0 ? (
          <button type="button" className="cap-btn" data-testid="dw-wiz-back"
                  onClick={() => go(at - 1)}>Back</button>
        ) : null}
        <span className="dw-wiz-spacer" />
        {/* START IS AVAILABLE FROM THE FIRST STEP, enabled once the required fields are in. */}
        {at < steps.length - 1 ? (
          <>
            <button type="button" className="linklike" data-testid="dw-wiz-start-now"
                    disabled={busy || !!blocked}
                    onClick={() => void start()}>
              {busy ? "Starting…" : "Start with defaults"}
            </button>
            <button type="button" className="cap-btn primary" data-testid="dw-wiz-next"
                    disabled={!!stepBlocked}
                    onClick={() => go(at + 1)}>Next</button>
          </>
        ) : (
          <button type="button" className="cap-btn primary" data-testid="dw-new-start"
                  disabled={busy || !!blocked}
                  onClick={() => void start()}>
            {busy ? "Starting…" : brief ? "Start the project" : "Start"}
          </button>
        )}
      </div>
      {error ? <p className="dw-new-error" data-testid="dw-new-error">{error}</p> : null}
    </section>
  );
}
