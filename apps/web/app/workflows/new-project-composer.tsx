"use client";

/**
 * The Runs composer: describe an outcome, choose the workflow that delivers it, say where it
 * lives, and start it, with the readiness checklist for what the project still lacks.
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
import { inputsWithTheDirective, missingRequired, suppliedInputs, workflowInputFields }
  from "../../lib/workflow-inputs";
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

const STEPS = ["What you are building", "How it gets built", "Where it lives", "Review"] as const;

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
      return "Describe what you want built — a sentence or two, not a title. "
           + "The panel argues from this text, so a thin brief produces a thin spec.";
    }
  }
  return "";
}

/**
 * STARTING WORK, WHERE THE WORK IS WATCHED.
 *
 * "New project" lived in Factory because Factory was where projects were listed. Now the
 * workflow drives — the press starts a run, and the run convenes the panel, waits for the
 * approval, builds, previews and asks before it deploys — so the page that shows those steps
 * is the page that should start them.
 *
 * AND IT IS A WIZARD, NOT A BOX.
 *
 * It was one textarea and a workflow dropdown. Everything else a build needs — which repository
 * the code goes in, what "done" means, anything the builder should read first — was either
 * defaulted silently or not asked at all, and the first chance to correct any of it was the
 * spec review, after a panel had already been convened on a guess. Starting a project is the
 * one moment where a minute of the operator's attention is worth more than any later
 * intervention, and it was the moment the console asked for the least.
 *
 * Four steps, each of which can be skipped by pressing on: nothing here is required except the
 * description, because a wizard that demands ten answers is a wizard people route around.
 */
/** The inputs the composer draws for a workflow: what it declared, less what the wizard's own
 *  steps already collect for a catalogue template (the brief, its name, the repository...). A
 *  workflow somebody made keeps every field it declared — its `title` may mean a YouTube title,
 *  not the run's name. */
export function composerFields(schema: unknown, isCatalogue: boolean) {
  return workflowInputFields(schema).filter((f) => !(isCatalogue && WIZARD_COLLECTS.has(f.name)));
}

export function NewProjectComposer({ projectId, actor, workflows, catalogue, onStarted, startWith,
                             example }: {
  projectId: string; actor: string;
  workflows: Installed[];
  catalogue: CatalogueEntry[];
  onStarted: (runId: string | null) => void;
  /** A workflow the operator asked to start, chosen already. Opens on the step that collects
   *  what that workflow declared it needs, because they have already made the path decision. */
  startWith?: string;
  /** "Try an example": the template's own example brief, filled in for the operator to read
   *  and change — never started unseen. */
  example?: { title?: string; directive?: string } | null;
}) {
  // A CATALOGUE TEMPLATE OPENS ON ITS BRIEF. Its directive is collected on the first step, and
  // opening on the path step (right for a workflow whose inputs are all its own) put the one
  // required answer behind a Back button.
  const [step, setStep] = useState(() => {
    if (!startWith) return 0;
    const isCatalogue = catalogue.some((c) => c.key === startWith);
    const schema = workflows.find((w) => w.workflow_key === startWith)?.input_schema;
    return isCatalogue || !composerFields(schema, isCatalogue).length ? 0 : 1;
  });
  const [draft, setDraft] = useState<NewProjectDraft>(
    startWith ? { ...EMPTY_DRAFT, workflowKey: startWith,
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

  // WHERE THE CODE GOES, ANSWERED BEFORE IT IS ASKED.
  //
  // The project's registry knows its repositories and which one is primary; nothing over HTTP
  // could read it, so this form could not offer them and every build went to the deployment's
  // global default. Loaded on mount so the field arrives filled in — the operator confirms a
  // real answer instead of composing one.
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

  // WHAT THE CHOSEN WORKFLOW WILL ASK FOR. Read from the installed definition rather than
  // hardcoded: a custom workflow declares its own, and the form has to draw whatever that is.
  const selectedWorkflowSchema = workflows.find(
    (w) => w.workflow_key === draft.workflowKey)?.input_schema;
  const selectedIsCatalogue = !!describe(draft.workflowKey);
  const declaredInputs = composerFields(selectedWorkflowSchema, selectedIsCatalogue);

  // WHAT WILL ACTUALLY BE SENT, worked out once and used for the guard, the review and the
  // start — three places that disagreed. The operator's directive stands in for a single blank
  // prose input rather than being asked for twice; see `inputsWithTheDirective`.
  const inputsToSend = selectedIsCatalogue
    ? suppliedInputs(declaredInputs, draft.inputs)
    : inputsWithTheDirective(declaredInputs, draft.inputs, draft.directive);
  const stillBlank = missingRequired(declaredInputs, inputsToSend);

  // CAN IT RUN HERE. Asked as soon as the path is known, so what is missing is on screen
  // before Start rather than in a run that fails at its last step.
  const asked = useWorkflowReadiness(projectId, draft.workflowKey, inputsToSend);
  const readiness = asked ?? refusal;
  const notReady = unaccepted(readiness, startAnyway);

  // `??` WAS WRONG HERE AND IT COST A RUN. `blockingReason` returns "" for "nothing is
  // blocking", and "" is neither null nor undefined, so the right-hand side never ran and this
  // whole check was dead code: a required input left blank sailed past the form and was refused
  // by the runtime instead, as `WorkflowValidationError: this workflow needs 1 input(s) it was
  // not given: instructions` under a button that had looked perfectly ready to press.
  // A catalogue template's brief IS its declared directive, collected on step 0 — so the
  // twenty-character rule applies to it like any software brief.
  // WHAT STOPS MOVING ON is only this step's own question. A required field drawn on a later
  // step (Deprecation / Sunset's "Who to tell") must not disable Next on the step before it —
  // that is a form you cannot get to the end of. It still blocks Start, below.
  const stepBlocked = blockingReason(step, draft,
                                     declaredInputs.length > 0 && !selectedIsCatalogue);
  const blocked = stepBlocked
    // A required input still blank blocks the start, and says which one — the run would be
    // refused by the runtime anyway, and being told at the form is the difference between a
    // correction and a failed run.
    || (stillBlank.length ? `Needs ${stillBlank.join(", ")}` : "")
    // So does something the project lacks, unless the operator chose to start without it.
    || (notReady.length
        ? `Not ready here yet: ${notReady.map((m) => m.label).join("; ")} — see below.`
        : "");

  const start = async () => {
    // WHAT THIS RUN IS CALLED, when the operator wrote no brief.
    //
    // A workflow that declares its own inputs is started from those, and `briefFrom` returns
    // "" — which made the button silently do nothing, the worst possible response to a press.
    // The run still needs something readable in the runs list, so it is named after the
    // workflow and the instruction the operator actually gave.
    const chosen = workflows.find((w) => w.workflow_key === draft.workflowKey);
    const declared = declaredInputs.length > 0;
    const supplied = String(draft.inputs.instructions ?? "").trim();
    const text = briefFrom(draft).trim()
      || (declared
          ? [chosen?.name ?? draft.workflowKey, supplied].filter(Boolean).join(": ")
          : "");
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      const title = draft.title.trim()
        || (text.length > 72 ? `${text.slice(0, 69)}…` : text);
      const criteria = criteriaFrom(draft);
      const links = linksFrom(draft);
      const r = await post("start", {
        project_id: projectId, actor,
        ...(draft.workflowKey
            ? { workflow_key: draft.workflowKey }
            : { work_class: "project.new" }),
        input: {
          title, directive: text,
          // Sent only when the operator gave one. An empty string here would read as a
          // deliberate choice of "" further down and beat the project's own default.
          ...(criteria.length ? { acceptance_criteria: criteria } : {}),
          ...(links.length ? { reference_links: links } : {}),
          ...(draft.repository ? { repository: draft.repository } : {}),
          // WHAT THIS WORKFLOW DECLARED IT NEEDS. from_input resolves any field name, so a
          // step binding `video_url` reads it straight from here. Only filled-in values are
          // sent: an empty string would read further down as a deliberate choice of empty.
          ...inputsToSend,
        },
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
      const chosen = draft.agents;
      const sameAsDefault = chosen.length === defaultAgents.length
        && chosen.every((a) => defaultAgents.includes(a));
      if (chosen.length && !sameAsDefault) {
        try {
          await apiWrite("/api/project-agents", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ project_id: projectId, app: title, actor,
                                   role_keys: chosen }),
          });
          // Anyone the operator took OFF the default list has to come off for real, or the
          // wizard would show a choice the run does not honour.
          for (const gone of defaultAgents.filter((d) => !chosen.includes(d))) {
            await apiWrite("/api/project-agents", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ project_id: projectId, app: title, actor,
                                     action: "unassign", role_key: gone }),
            });
          }
        } catch { /* the run is started and seeded; the assignment can be fixed on the run */ }
      }
      setDraft(EMPTY_DRAFT);
      setStep(0);
      onStarted(String(run?.run?.id ?? run?.id ?? "") || null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="dw-new dw-wizard" data-testid="dw-new-project">
      <div className="dw-new-head">
        <h3>New project</h3>
        {/* "you approve" was the PRD approval, which is automatic by default — the step now
            says so, and so does this. The person's decision is the release. */}
        <p className="detail">
          A panel specs it, the spec is approved automatically, then it builds, previews, and
          asks you before it deploys.
        </p>
      </div>

      {/* WHERE YOU ARE AND WHAT IS LEFT. Four unlabelled dots would be decoration; the names
          are what let somebody decide whether to keep going or press Start now. */}
      <ol className="dw-wiz-rail" data-testid="dw-wiz-rail">
        {STEPS.map((label, i) => (
          <li key={label} data-state={i === step ? "current" : i < step ? "done" : "todo"}>
            <button type="button" className="dw-wiz-crumb"
                    data-testid={`dw-wiz-crumb-${i}`}
                    // Backwards freely; forwards only over ground already covered, so the
                    // rail cannot be used to skip the one question that is required.
                    disabled={i > step}
                    onClick={() => setStep(i)}>
              <span className="dw-wiz-num" aria-hidden>{i < step ? "✓" : i + 1}</span>
              <span>{label}</span>
            </button>
          </li>
        ))}
      </ol>

      {step === 0 ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-0">
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
                // ⌘/Ctrl+Enter starts with the defaults, as it did before the wizard and as
                // every other composer in this console does. The remaining steps all have a
                // defensible default, so the shortcut is not a way of skipping a decision —
                // it is the same "Start with defaults" the footer offers, from the keyboard.
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
                text="These become checks for the build. Leave them blank and the panel will propose criteria for your approval." />
            </legend>
            <span className="dw-field-hint">These become acceptance criteria.</span>
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
        </div>
      ) : null}

      {step === 1 ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-1">
          <span className="dw-field-label">Which path should it take? <HelpTip
            label="Workflow paths"
            text="Every path produces software you can review. Paths differ in the review, evidence, and approval gates along the way." /></span>
          <div className="dw-wiz-choices" role="radiogroup" aria-label="Workflow">
            <button type="button" role="radio" className="dw-wiz-choice"
                    aria-checked={draft.workflowKey === ""}
                    data-selected={draft.workflowKey === ""}
                    data-testid="dw-new-workflow-default"
                    onClick={() => set("workflowKey", "")}>
              <span className="dw-wiz-choice-name">This project&apos;s usual path</span>
              <span className="detail">
                Whatever this project maps for new work. The same resolution every other entry
                point uses.
              </span>
            </button>
            {workflows.map((w) => {
              const entry = describe(w.workflow_key);
              return (
                <button type="button" role="radio" className="dw-wiz-choice" key={w.id}
                        aria-checked={draft.workflowKey === w.workflow_key}
                        data-selected={draft.workflowKey === w.workflow_key}
                        data-testid={`dw-new-workflow-${w.workflow_key}`}
                        onClick={() => set("workflowKey", w.workflow_key)}>
                  <span className="dw-wiz-choice-name">{w.name}</span>
                  {entry ? <span className="detail">{entry.description}</span> : null}
                  {entry ? (
                    <span className="dw-wiz-choice-meta detail">
                      {entry.step_count} steps
                      {entry.gates?.length
                        ? ` · you approve: ${entry.gates.join(", ")}`
                        : " · no approval gates"}
                    </span>
                  ) : null}
                </button>
              );
            })}
          </div>

          {/* WHAT THIS WORKFLOW NEEDS SUPPLIED.
              Rendered FROM the chosen workflow's declaration, not hardcoded: a custom workflow
              that fetches a video, trims a clip and publishes to a channel declares those, and
              until now was startable only by a caller that already knew the field names.
              Nothing renders for a workflow that declares none; for a catalogue template only
              what the wizard's own steps do not already ask (who to tell) is drawn here. */}
          {declaredInputs.length ? (
            <div className="dw-wiz-inputs" data-testid="dw-workflow-inputs">
              <span className="dw-field-label">What this path needs <HelpTip
                label="Workflow inputs"
                text="These values are read when the run starts. Put lasting choices such as a channel, icon, or house style in the skill or agent instead." /></span>
              {declaredInputs.map((field) => (
                <div className="dw-wiz-input" key={field.name}>
                  <span className="dw-field-label">
                    <label htmlFor={`dw-workflow-input-${field.name}`}>{field.label}</label>
                    {field.required ? <em className="dw-req" aria-hidden> required</em> : null}
                    {field.help ? <HelpTip label={field.label} text={field.help} /> : null}
                  </span>
                  {field.help ? <span className="dw-field-hint">{field.help}</span> : null}
                  {/* Multi-line instructions need enough room to review the complete value. */}
                  {field.kind === "multiline" ? (
                    <textarea
                      id={`dw-workflow-input-${field.name}`}
                      rows={3}
                      value={draft.inputs[field.name] ?? ""}
                      placeholder={field.example}
                      data-testid={`dw-workflow-input-${field.name}`}
                      aria-required={field.required || undefined}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d,
                          inputs: { ...d.inputs, [field.name]: e.target.value } }))}
                    />
                  ) : (
                    <input
                      id={`dw-workflow-input-${field.name}`}
                      type={field.kind === "number" ? "number" : "text"}
                      inputMode={field.kind === "number" ? "numeric" : undefined}
                      value={draft.inputs[field.name] ?? ""}
                      placeholder={field.example}
                      data-testid={`dw-workflow-input-${field.name}`}
                      aria-required={field.required || undefined}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d,
                          inputs: { ...d.inputs, [field.name]: e.target.value } }))}
                    />
                  )}
                </div>
              ))}
            </div>
          ) : null}

          {/* WHO WILL BE ON IT. The wizard collected a title, a directive, criteria, a workflow
              and a repository, and never once mentioned agents — so a project was created with
              no say in who would build it. The defaults are shown pre-selected rather than as
              an empty box: they are what BOSS seeds anyway, and an empty picker would imply
              nobody is coming. */}
          <span className="dw-field-label dw-agents-label">Who works on it</span>
          <span className="dw-field-hint">
            These are the agents that would run this path anyway. Take one off and it may not
            sign off this app&rsquo;s gates; add one and it may.
          </span>
          <div className="aot-list" data-testid="dw-wiz-agents">
            {(draft.agents.length ? draft.agents : defaultAgents).map((key) => (
              <AgentChip key={key} roleKey={key}
                         label={allRoles.find((r) => r.role_key === key)?.display_name
                                || key.replace(/_/g, " ")}>
                <button type="button" className="aot-x" aria-label={`Remove ${key}`}
                        onClick={() => set("agents",
                          (draft.agents.length ? draft.agents : defaultAgents)
                            .filter((k) => k !== key))}>✕</button>
              </AgentChip>
            ))}
            <select className="mr-select aot-pick" value="" data-testid="dw-wiz-agent-add"
                    onChange={(e) => {
                      if (!e.target.value) return;
                      const now = draft.agents.length ? draft.agents : defaultAgents;
                      set("agents", [...now, e.target.value]);
                    }}>
              <option value="">+ Add an agent…</option>
              {allRoles
                .filter((r) => !(draft.agents.length ? draft.agents : defaultAgents)
                  .includes(r.role_key))
                .map((r) => (
                  <option key={r.role_key} value={r.role_key}>
                    {r.display_name || r.role_key}
                  </option>
                ))}
            </select>
          </div>
          {draft.agents.length ? (
            <span className="dw-field-hint">
              Changed from the default.{" "}
              <button type="button" className="dw-linkish"
                      onClick={() => set("agents", [])}>Put it back</button>
            </span>
          ) : null}
        </div>
      ) : null}

      {step === 2 ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-2">
          <span className="dw-field-label">Which repository?</span>
          <span className="dw-field-hint" data-testid="dw-new-repo-hint">
            Where the branch and the pull request go.
            {repoHint.text}
            {repoHint.needsConnection ? (
              <>
                {" "}Connect one in{" "}
                <a data-testid="dw-new-repo-connect"
                   href={consoleHref(
                     typeof window !== "undefined" ? window.location.search : "",
                     { area: "setup" })}>
                  Settings → Repo and model → Repository
                </a>
                {" "}(a fine-grained token with Contents and Pull requests: read and write),
                then come back.
              </>
            ) : null}
          </span>
          <div className="dw-wiz-choices" role="radiogroup" aria-label="Repository">
            <button type="button" role="radio" className="dw-wiz-choice"
                    aria-checked={draft.repository === ""}
                    data-selected={draft.repository === ""}
                    data-testid="dw-new-repo-default"
                    onClick={() => set("repository", "")}>
              <span className="dw-wiz-choice-name">
                {suggested ? suggested.name
                  : repoHint.needsConnection ? "No repository yet" : "The project default"}
              </span>
              <span className="detail">
                {suggested
                  ? "What this project resolves to today. Leave it unless you mean elsewhere."
                  : repoHint.needsConnection
                    ? "Resolved when the build starts; connect a repository before then."
                    : "Resolved when the build starts."}
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
              Anything it should read first? <em>optional</em>
            </legend>
            <span className="dw-field-hint">
              Links the agents must open before designing or writing — an API doc, a repo whose
              conventions to follow, a design. They are fetched and read, not indexed or
              searched, so they have to be reachable without a login. A link that cannot be
              opened is reported, never guessed at.
            </span>
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

      {step === 3 ? (
        <div className="dw-wiz-body" data-testid="dw-wiz-step-3">
          {/* WHAT IS ABOUT TO HAPPEN, IN THE ORDER IT WILL HAPPEN. A confirmation screen that
              only repeats the form is a second form. This says what the press DOES, including
              the parts that cost money and the parts that will stop and wait for a person. */}
          <dl className="dw-wiz-review">
            <dt>Building</dt>
            <dd data-testid="dw-review-directive">{briefFrom(draft) || "—"}</dd>
            <dt>Path</dt>
            <dd data-testid="dw-review-workflow">
              {draft.workflowKey
                ? (workflows.find((w) => w.workflow_key === draft.workflowKey)?.name
                   ?? draft.workflowKey)
                : "This project's usual path"}
            </dd>
            <dt>Repository</dt>
            <dd data-testid="dw-review-repo">
              {draft.repository || suggested?.name || "the project default"}
            </dd>
            {/* WHO RUNS IT, on the screen that says what the press does.
                It is chosen on step 2 and was reported twice as missing — a thing on a screen
                you have to click through to is a thing nobody finds. Repeated here, where
                everything about to happen is confirmed, and editable in place. */}
            <dt>Agents</dt>
            <dd data-testid="dw-review-agents">
              <div className="aot-list">
                {(draft.agents.length ? draft.agents : defaultAgents).map((key) => (
                  <AgentChip key={key} roleKey={key}
                             label={allRoles.find((r) => r.role_key === key)?.display_name
                                    || key.replace(/_/g, " ")}
                             note={draft.agents.length ? "chosen" : "by default"}>
                    <button type="button" className="aot-x" aria-label={`Remove ${key}`}
                            onClick={() => set("agents",
                              (draft.agents.length ? draft.agents : defaultAgents)
                                .filter((k) => k !== key))}>✕</button>
                  </AgentChip>
                ))}
                <select className="mr-select aot-pick" value=""
                        data-testid="dw-review-agent-add"
                        onChange={(e) => {
                          if (!e.target.value) return;
                          const now = draft.agents.length ? draft.agents : defaultAgents;
                          set("agents", [...now, e.target.value]);
                        }}>
                  <option value="">+ Add an agent…</option>
                  {allRoles
                    .filter((r) => !(draft.agents.length ? draft.agents : defaultAgents)
                      .includes(r.role_key))
                    .map((r) => (
                      <option key={r.role_key} value={r.role_key}>
                        {r.display_name || r.role_key}
                      </option>
                    ))}
                </select>
              </div>
            </dd>
            {/* WHAT THE WORKFLOW WILL BE GIVEN. The review listed the brief, the path, the
                repository and the agents, and said nothing about the fields the run is
                actually started from — so an operator whose directive is standing in for the
                instruction could not see that it was. */}
            {declaredInputs.length ? (
              <>
                <dt>It will be given</dt>
                <dd data-testid="dw-review-inputs">
                  <ul>
                    {declaredInputs
                      .filter((f) => inputsToSend[f.name])
                      .map((f) => (
                        <li key={f.name}>
                          <span className="detail">{f.label}: </span>
                          {inputsToSend[f.name]}
                        </li>
                      ))}
                  </ul>
                </dd>
              </>
            ) : null}
            {criteriaFrom(draft).length ? (
              <>
                <dt>Done when</dt>
                <dd data-testid="dw-review-criteria">
                  <ul>{criteriaFrom(draft).map((c) => <li key={c}>{c}</li>)}</ul>
                </dd>
              </>
            ) : null}
            {linksFrom(draft).length ? (
              <>
                <dt>Reads first</dt>
                <dd data-testid="dw-review-links">
                  <ul>{linksFrom(draft).map((l) => <li key={l}><code>{l}</code></li>)}</ul>
                </dd>
              </>
            ) : null}
          </dl>
          <p className="detail dw-wiz-warn">
            Starting spends model budget: research, a panel and a build. It will stop and ask
            you before it deploys anything.
          </p>
        </div>
      ) : null}

      <ReadinessPanel readiness={readiness} accepted={startAnyway} projectId={projectId}
                      onAccept={setStartAnyway} />

      {blocked ? <p className="dw-new-error" data-testid="dw-wiz-blocked">{blocked}</p> : null}

      <div className="dw-wiz-foot">
        {step > 0 ? (
          <button type="button" className="cap-btn" data-testid="dw-wiz-back"
                  onClick={() => setStep((n) => n - 1)}>Back</button>
        ) : null}
        <span className="dw-wiz-spacer" />
        {/* START IS AVAILABLE FROM THE FIRST STEP. Everything after the description has a
            defensible default, so making somebody walk four screens to accept four defaults
            would be ceremony. The wizard is there for the operator who wants it. */}
        {step < STEPS.length - 1 ? (
          <>
            <button type="button" className="linklike" data-testid="dw-wiz-start-now"
                    disabled={busy || !!blocked}
                    onClick={() => void start()}>
              {busy ? "Starting…" : "Start with defaults"}
            </button>
            <button type="button" className="cap-btn primary" data-testid="dw-wiz-next"
                    disabled={!!stepBlocked}
                    onClick={() => setStep((n) => n + 1)}>Next</button>
          </>
        ) : (
          <button type="button" className="cap-btn primary" data-testid="dw-new-start"
                  disabled={busy || !!blocked}
                  onClick={() => void start()}>
            {busy ? "Starting…" : "Start the project"}
          </button>
        )}
      </div>
      {error ? <p className="dw-new-error" data-testid="dw-new-error">{error}</p> : null}
    </section>
  );
}
