/**
 * What each template actually asks BOSS to build — and what the operator reads before saying go.
 *
 * The brief was five bullets and an acceptance test. That is enough to specify an MVP and it is
 * exactly what BOSS built: something that works and looks like a prototype. Compared against
 * lovable.dev's detail page — a hero screenshot, a description, "Designed for", key highlights,
 * a technology stack, and a dozen "Features & capabilities" cards each with a paragraph — ours
 * was thin, and the operator said so.
 *
 * The fix is not a longer page. It is a longer SPECIFICATION, because on this product the page
 * and the build are the same text: what you read here is appended to the brief that BOSS is
 * held to. A template that lists a what-if slider, an animated reveal and a CSV export gets an
 * app with those in it. One that lists four bullets gets four bullets.
 *
 * So each template below carries:
 *   pitch        — what it is, in the words someone deciding would use
 *   designedFor  — who specifically, not "users"
 *   features     — the capabilities, each with enough detail to be built and tested against
 *   surfaces     — the screens it needs, so the build does not stop at one route
 *
 * And every one of them inherits CRAFT: the standard that separates a demo from something you
 * would put in front of a colleague. It is shared because it is not per-template — an empty
 * state, a keyboard path and a real error message are owed by all sixteen.
 */

export type Feature = { title: string; detail: string };

export type TemplateDetail = {
  pitch: string;
  designedFor: string[];
  features: Feature[];
  surfaces: string[];
};

/**
 * THE QUALITY BAR, applied to every template.
 *
 * Written from what BOSS actually shipped without being asked: apps that worked on the happy
 * path, rendered a blank rectangle with no rows, threw an unstyled stack trace on bad input,
 * and could not be operated without a mouse. None of that is a missing FEATURE — it is the
 * difference between a demo and software, and it has to be in the brief or it does not happen.
 */
export const CRAFT: string[] = [
  "Every list has a designed empty state that says what to do next, not a blank panel.",
  "Every destructive action is confirmable and reversible, or it is not destructive.",
  "Loading is a skeleton of the thing arriving, never a spinner over a white page.",
  "Errors say what failed and what to try, in a sentence a non-engineer can act on.",
  "Keyboard-operable end to end: tab order, visible focus, Enter to submit, Escape to close.",
  "Responsive from 360px to a wide monitor — no horizontal scroll, no truncated controls.",
  "Seed it with realistic sample data so the first screen is populated, not empty.",
  "One consistent visual language: one type scale, one spacing rhythm, one accent used sparingly.",
];

export const DETAILS: Record<string, TemplateDetail> = {
  "Incident timeline builder": {
    pitch: "Turns an alert log and a chat transcript into one defensible timeline, with the "
      + "four milestones marked and the two durations computed. The post-mortem writes itself "
      + "from the evidence rather than from memory the morning after.",
    designedFor: [
      "An on-call engineer writing the post-mortem the morning after",
      "An incident commander who needs time-to-detect and time-to-resolve defended with sources",
      "A team that keeps re-litigating what happened because nobody wrote it down in order",
    ],
    features: [
      { title: "Two sources, one timeline", detail: "Paste an alert log and a chat export; both are parsed, timestamped and merged into a single ordered spine with the source shown on every entry." },
      { title: "Milestones you can argue with", detail: "Detection, escalation, mitigation and all-clear are proposed from the evidence — and every one can be moved, with the tool showing which line it inferred it from." },
      { title: "Durations that recompute", detail: "Time-to-detect and time-to-resolve update live as milestones move, so a correction costs a drag rather than a rewrite." },
      { title: "Gap detection", detail: "Stretches with no activity are flagged: the twenty silent minutes are usually the finding, and they are invisible in a raw log." },
      { title: "Markdown export", detail: "One post-mortem file with the timeline embedded, the durations stated, and every claim linked to the log line behind it." },
    ],
    surfaces: ["Import (paste or upload)", "Timeline editor", "Post-mortem preview", "Incident list"],
  },

  "Subscription audit": {
    pitch: "Imports a card statement and turns recurring charges into a decision list: what you "
      + "pay, what it has risen to, and what cancelling would actually save. The number at the "
      + "bottom is the point.",
    designedFor: [
      "A founder who suspects they are paying for tools nobody opens",
      "An operations lead doing a cost pass before a board meeting",
      "Anyone whose statement has two spellings of the same vendor",
    ],
    features: [
      { title: "Fuzzy vendor grouping, reviewable", detail: "Charges group by vendor across spelling variants — and every grouping can be split by hand, because a wrong merge silently hides a subscription." },
      { title: "Price-rise detection", detail: "Flags any vendor whose charge increased, with the before and after and the month it changed." },
      { title: "Keep / cancel / renegotiate", detail: "Each row takes a decision, and the running savings total reflects only the marked ones." },
      { title: "Annualised view", detail: "Monthly and annual cost side by side, because $40/month is a decision people make differently when it reads $480/year." },
      { title: "Cancellation evidence", detail: "Export the marked rows with dates and amounts — the list you actually work through, not a chart." },
    ],
    surfaces: ["CSV import with column mapping", "Audit table", "Vendor detail", "Savings summary"],
  },

  "Interview scorecard": {
    pitch: "Independent scoring with the other interviewers' scores hidden until you commit. The "
      + "spread is the output: it shows where the panel disagrees, which is the conversation "
      + "worth having.",
    designedFor: [
      "A hiring manager tired of vague feedback",
      "A panel where the loudest voice currently sets the tone",
      "A team that wants an auditable reason for a decision months later",
    ],
    features: [
      { title: "Anti-anchoring by construction", detail: "An interviewer cannot see any other score until their own is submitted — enforced server-side, not hidden in the UI." },
      { title: "Competencies per role", detail: "Define what a role is actually assessed on once, and every interview for it uses the same rubric." },
      { title: "Disagreement, surfaced", detail: "The summary leads with the competencies the panel diverged on most, not the average." },
      { title: "Evidence required", detail: "A score without a written justification cannot be submitted — the note is the artefact anybody reads later." },
      { title: "Candidate summary", detail: "One page per candidate: scores, spread, evidence and the decision, exportable for the record." },
    ],
    surfaces: ["Role & rubric setup", "Scoring form", "Panel summary", "Candidate history"],
  },

  "Docs freshness board": {
    pitch: "Reads a docs folder and sorts it by how likely each page is to be wrong — last "
      + "edited against whether the code it describes has changed since. Nobody trusts docs "
      + "because nobody knows which parts rotted.",
    designedFor: [
      "Whoever owns documentation nobody trusts any more",
      "A team onboarding people onto pages last touched two years ago",
      "A maintainer deciding what to fix first with an afternoon",
    ],
    features: [
      { title: "Staleness, computed not guessed", detail: "Each page's age is compared against the last change to the code paths it mentions — a doc is stale relative to its subject, not to the calendar." },
      { title: "Code references extracted", detail: "Paths, symbols and commands are pulled out of each page and linked, so you can see what a page claims to describe." },
      { title: "Ranked worklist", detail: "Sorted most-likely-wrong first, with the reason shown, so an afternoon spent on it is spent well." },
      { title: "Mark reviewed", detail: "Confirming a page is still correct resets its clock and records who checked — the difference between old and unverified." },
      { title: "Trend over time", detail: "One number for the whole set, so a team can see whether the rot is getting better or worse." },
    ],
    surfaces: ["Repository connect", "Freshness board", "Page detail with references", "Trend"],
  },

  "Lead triage inbox": {
    pitch: "Scores inbound enquiries against an editable ideal-customer profile, drafts a reply "
      + "for each, and puts a human in front of every send. The scoring is explainable — you can "
      + "see which words earned the points.",
    designedFor: [
      "A small team drowning in inbound leads",
      "A founder who answers everything personally and is falling behind",
      "Anyone who needs to show why a lead was declined",
    ],
    features: [
      { title: "An ICP you can edit", detail: "Industries, budget, keywords and any other dimension you add — stored as data, so tuning who qualifies is an edit rather than a deploy." },
      { title: "Explainable scoring", detail: "Every score shows the tokens that earned it and what each was worth. A number nobody can interrogate gets ignored." },
      { title: "Drafted replies, two tones", detail: "Accept and decline drafts generated per lead, with a tone control, and the decline carries a disclaimer that cannot be edited out." },
      { title: "Human gate", detail: "Nothing sends without an explicit review tick. The approver, the timestamp and a hash of what was sent are recorded." },
      { title: "Audit trail", detail: "Append-only log of every send, exportable — the thing you need when somebody asks what was said in March." },
    ],
    surfaces: ["ICP editor", "Inbox with scores", "Draft review", "Audit log"],
  },

  "Change calendar": {
    pitch: "One calendar of what is changing, when, and who is on call for it — with conflicts "
      + "flagged before they collide rather than in the incident review.",
    designedFor: [
      "A team where two people ship to the same service on the same afternoon",
      "An on-call engineer who wants to know what landed before the pager went off",
      "Anyone maintaining a freeze window nobody can see",
    ],
    features: [
      { title: "Conflict detection", detail: "Two changes touching the same service in the same window are flagged when they are scheduled, not after." },
      { title: "Freeze windows", detail: "Declare a period where nothing ships; anything scheduled into it is blocked with the reason shown." },
      { title: "On-call context", detail: "Every change shows who is holding the pager during its window, so the person who will be woken knows it exists." },
      { title: "Retrospective view", detail: "Filter to a time range and see exactly what changed — the first question of every incident." },
      { title: "Subscribable feed", detail: "An iCal feed per service or team, so it lives in the calendar people already look at." },
    ],
    surfaces: ["Calendar", "Schedule a change", "Service view", "Freeze windows"],
  },

  "Bug triage board": {
    pitch: "Turns an unsorted bug list into a queue with a defensible order — severity against "
      + "frequency against age — and makes the reason for each position visible.",
    designedFor: [
      "A maintainer with two hundred open issues and no order",
      "A team whose triage meeting is an hour of re-reading the same tickets",
      "Anyone who needs to justify why a bug is not being fixed yet",
    ],
    features: [
      { title: "Ranking you can inspect", detail: "Position comes from severity, how many people hit it, and how long it has waited — with the arithmetic shown on every card." },
      { title: "Duplicate clustering", detail: "Reports describing the same fault group together, so a bug hit by forty people ranks like one, not forty." },
      { title: "Triage in one pass", detail: "Keyboard-driven: severity, assignee and decision without touching the mouse, because triage is a hundred small decisions." },
      { title: "Stale escalation", detail: "Anything untouched past its severity's threshold rises and says why, so nothing rots quietly at the bottom." },
      { title: "Decision record", detail: "Won't-fix carries a written reason, shown to anyone who finds the issue later." },
    ],
    surfaces: ["Queue", "Issue detail", "Triage mode", "Cluster view"],
  },

  "Onboarding path": {
    pitch: "A path through a codebase rather than a folder of documents: read this, run that, "
      + "ship a tiny change — with progress visible to the new joiner and to whoever is "
      + "supposed to be helping them.",
    designedFor: [
      "A new engineer on day one with a wiki and no order to it",
      "A manager who wants to know if onboarding is actually working",
      "A team whose setup instructions silently broke three months ago",
    ],
    features: [
      { title: "Ordered steps with real work", detail: "Each step is read, run or ship — and the ship step is a genuine small change, because nothing proves an environment works like a merged PR." },
      { title: "Verified environment", detail: "Setup steps carry a command whose success is checked, so 'it works on my machine' is caught on day one." },
      { title: "Progress both ways", detail: "The joiner sees where they are; the buddy sees where they are stuck, without asking." },
      { title: "Rot detection", detail: "A step that repeatedly fails for new joiners is flagged as broken, which is how stale instructions get found." },
      { title: "Per-role paths", detail: "A backend joiner and a designer do not need the same twelve steps." },
    ],
    surfaces: ["Path editor", "Joiner view", "Buddy dashboard", "Step detail"],
  },

  "Pricing experiment log": {
    pitch: "Every pricing change, what it was expected to do, and what it actually did — so the "
      + "argument about whether the last change worked has an answer.",
    designedFor: [
      "A founder changing prices on instinct and remembering selectively",
      "A team that has run four pricing experiments and can describe none of them",
      "Anyone who needs to show a board why the price is what it is",
    ],
    features: [
      { title: "Hypothesis before result", detail: "The expected effect is recorded before the change ships and locked afterwards — otherwise every experiment succeeds in hindsight." },
      { title: "Cohort boundaries", detail: "Who was exposed and who was not, with dates, so the comparison is between comparable groups." },
      { title: "Outcome against expectation", detail: "The result is shown beside what was predicted, including the ones that went the other way." },
      { title: "Interference warnings", detail: "Overlapping experiments on the same cohort are flagged — the commonest reason a pricing result is meaningless." },
      { title: "Decision log", detail: "Kept, reverted or extended, with the reasoning, so the next person does not re-run it." },
    ],
    surfaces: ["Experiment list", "New experiment", "Results", "Decision archive"],
  },

  "Vendor status watch": {
    pitch: "Polls the status pages of everything you depend on and tells you when one of them is "
      + "having the incident you are about to blame yourself for.",
    designedFor: [
      "An on-call engineer debugging an outage that is not theirs",
      "A team that finds out about a provider incident from a customer",
      "Anyone maintaining a mental list of nine status pages",
    ],
    features: [
      { title: "One board, every dependency", detail: "Current state of each provider, with the last check time shown — a stale green light is worse than no light." },
      { title: "Incident history", detail: "What happened and when, kept locally, so 'was there an outage on the 4th' is answerable after their page has moved on." },
      { title: "Correlation with your own", detail: "Provider incidents overlaid on your deploys and alerts, which is the comparison that ends the argument." },
      { title: "Alerting that respects silence", detail: "Notify on state change, once, with a cooldown — a watcher that pages repeatedly gets muted and then misses the real one." },
      { title: "Degraded, not just down", detail: "Partial degradations are tracked distinctly, because that is the state that confuses people." },
    ],
    surfaces: ["Status board", "Provider detail", "Incident history", "Alert settings"],
  },

  "Decision log": {
    pitch: "The decisions a team made, why, and what would have to change to revisit them — so "
      + "the same argument is not had three times a year.",
    designedFor: [
      "A team re-litigating a choice made eight months ago",
      "A new joiner asking why the system is like this",
      "Anyone who has to defend a decision they inherited",
    ],
    features: [
      { title: "The alternatives, kept", detail: "What was rejected and why, because the value of a decision record is mostly in the paths not taken." },
      { title: "Reversal conditions", detail: "Every decision states what would make it wrong — which turns a revisit from an argument into a check." },
      { title: "Superseding, not editing", detail: "A changed decision creates a new record linked to the old one; the history stays readable." },
      { title: "Searchable by subject", detail: "Find decisions by the thing they are about, not by when somebody happened to write them down." },
      { title: "Consequences tracked", detail: "Link a decision to what followed from it, so the record shows outcomes and not just intentions." },
    ],
    surfaces: ["Decision list", "New decision", "Decision detail", "Supersession chain"],
  },

  "Changelog to email": {
    pitch: "Turns merged pull requests into a release note somebody outside engineering would "
      + "read, with a human editing before anything is sent.",
    designedFor: [
      "A team whose changelog is a list of commit messages",
      "A founder writing the same update by hand every fortnight",
      "Customers who currently learn about features by accident",
    ],
    features: [
      { title: "Grouped by what changed for the reader", detail: "New, improved, fixed — not by repository or author, which nobody outside the team cares about." },
      { title: "Internal work filtered out", detail: "Refactors and dependency bumps are excluded by default and can be pulled back in." },
      { title: "Plain-language rewrite", detail: "Each entry is rewritten from the PR title into something a customer understands, with the original kept alongside for checking." },
      { title: "Edit before send", detail: "The draft is always editable and nothing sends without an explicit action — a wrong release note cannot be recalled." },
      { title: "Send history", detail: "What went out and when, so the next edition starts from the right point." },
    ],
    surfaces: ["Draft editor", "PR selection", "Preview", "Send history"],
  },

  "Test coverage trend": {
    pitch: "Coverage as a direction rather than a number — which files are getting worse, and "
      + "which of them are the ones that keep breaking.",
    designedFor: [
      "A team whose coverage number has been 71% for a year",
      "A lead deciding where testing effort would actually pay",
      "Anyone arguing that a file needs tests before it can be changed safely",
    ],
    features: [
      { title: "Direction, not just level", detail: "Per file and per directory, whether coverage is rising or falling and by how much — the level alone tells you nothing." },
      { title: "Weighted by churn", detail: "A file that changes weekly and is untested ranks above a stable one, because that is where the risk actually is." },
      { title: "Uncovered and recently broken", detail: "Cross-references coverage with recent failures — the intersection is the worklist." },
      { title: "Pull request delta", detail: "What a change does to coverage, stated as a number, so it is visible at review time rather than in a monthly report." },
      { title: "No vanity target", detail: "No single percentage goal; the board is about movement, because a target invites tests written to hit it." },
    ],
    surfaces: ["Trend dashboard", "File detail", "Worklist", "PR delta"],
  },

  "Access expiry tracker": {
    pitch: "Who has access to what, when it was granted, and when it should end — with the "
      + "expiry actually enforced instead of noted.",
    designedFor: [
      "A team where a contractor's access outlived the contract",
      "Whoever answers an access-review question at audit time",
      "Anyone who granted temporary access eleven months ago",
    ],
    features: [
      { title: "Every grant has an end date", detail: "Access without an expiry cannot be recorded — permanent is a decision somebody makes explicitly, not a default." },
      { title: "Escalating reminders", detail: "The owner is warned before expiry, again at it, and the record turns overdue rather than quietly lapsing." },
      { title: "Review campaigns", detail: "Ask every owner to confirm their grants in one pass, with the unanswered ones visible." },
      { title: "Revocation evidence", detail: "When access ends, the record shows who removed it and when — the artefact an auditor asks for." },
      { title: "Orphan detection", detail: "Grants whose owner has left are surfaced first, because those are the ones nobody is watching." },
    ],
    surfaces: ["Access register", "Grant detail", "Review campaign", "Expiry queue"],
  },

  "Style guide linter": {
    pitch: "Checks prose against a house style — terms, tone, banned phrasing — and shows the "
      + "rule behind every flag, so it teaches rather than nags.",
    designedFor: [
      "A team where four people write docs four different ways",
      "A writer inheriting a style guide nobody enforces",
      "Anyone reviewing copy for the same three mistakes every time",
    ],
    features: [
      { title: "Rules as data", detail: "The style guide is editable in the tool, not compiled into it — a rule change is an edit." },
      { title: "The rule, shown", detail: "Every flag names the rule and quotes it, with a suggested rewrite, because a flag with no reason gets dismissed." },
      { title: "Severity levels", detail: "Errors block, warnings advise, suggestions are optional — a linter that treats everything as an error gets turned off." },
      { title: "Accepted exceptions", detail: "A flag can be dismissed with a reason, remembered, so the same false positive is not re-argued." },
      { title: "Bulk check", detail: "Run over a whole folder and get a ranked list of the pages that need work most." },
    ],
    surfaces: ["Editor with inline flags", "Rule manager", "Bulk report", "Exceptions"],
  },

  "Rotation fairness board": {
    pitch: "Shows who has actually carried the on-call and the weekend work, over time — because "
      + "the person who thinks they do more than their share is usually right or usually wrong, "
      + "and nobody can tell which.",
    designedFor: [
      "A team where the same two people take every awkward shift",
      "A manager building next quarter's rota",
      "Anyone who feels the rotation is unfair and has no way to check",
    ],
    features: [
      { title: "Weighted by unpleasantness", detail: "A weekend and a Tuesday are not the same shift; nights and holidays carry more weight, and the weighting is visible and editable." },
      { title: "Cumulative fairness", detail: "Balance measured over quarters, not weeks — short-run fairness is noise and long-run unfairness is the complaint." },
      { title: "What actually happened", detail: "Tracks swaps and cover as they occurred, not the rota as published — those diverge, and the divergence is the whole point." },
      { title: "Forecast the next rota", detail: "Proposes an assignment that reduces the imbalance, with the reasoning shown and every slot overridable." },
      { title: "Load, not just count", detail: "Shifts that were actually paged during count for more than quiet ones." },
    ],
    surfaces: ["Fairness board", "Person detail", "Rota planner", "Swap log"],
  },
};

/**
 * The brief BOSS is actually held to.
 *
 * The page and the build are the same text on this product: what the operator reads in the
 * enlarged view is appended to the specification. Reading a feature list and then getting an
 * app without those features would be the same broken promise as a green health check over a
 * dead app — so the list is not marketing copy, it is the contract.
 */
export function buildBrief(title: string, base: string): string {
  const detail = DETAILS[title];
  if (!detail) return base;
  return [
    base.trim(),
    "",
    "Features it must ship with (each of these is part of done, not a stretch):",
    ...detail.features.map((f) => `- ${f.title}: ${f.detail}`),
    "",
    `Screens: ${detail.surfaces.join(", ")}.`,
    "",
    "Quality bar — this is the difference between a demo and something usable:",
    ...CRAFT.map((c) => `- ${c}`),
  ].join("\n");
}
