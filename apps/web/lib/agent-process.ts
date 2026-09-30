/**
 * Agent process authoring helpers. Mappers never invent scores or runs.
 */

export const PROCESS_ROLES = [
  "product_manager",
  "vp_product",
  "chief_architect",
  "cto",
  "engineering_captain",
  "qa",
  "ux_engineer",
] as const;

export const PROCESS_ACTIONS = [
  "accept_prd",
  "review_prd",
  "improve_prd",
  "build",
  "qa_review",
  "ux_review",
  "verify_build",
  "certify_spec",
  "accept_work",
] as const;

export const PROCESS_KINDS = ["role", "panel"] as const;

export type ProcessRole = (typeof PROCESS_ROLES)[number];
export type ProcessAction = (typeof PROCESS_ACTIONS)[number];
export type ProcessKind = (typeof PROCESS_KINDS)[number];

export type ProcessTemplate = {
  id: string;
  processKey: string;
  name: string;
  description: string;
};

export type ProcessRun = {
  id: string;
  title: string;
  status: string;
  processKey?: string;
  templateName?: string;
};

export type ProcessStep = {
  id: string;
  stepKey: string;
  role: string;
  action: string;
  status: string;
  verdict?: string;
};

export type ProcessSpecStep = {
  id: string;
  kind: ProcessKind;
  role: ProcessRole;
  roles?: ProcessRole[];
  action: ProcessAction;
  next?: string[];
  wait_for?: string[];
};

export type ProcessSpec = {
  key: string;
  name: string;
  description: string;
  entry: string;
  steps: ProcessSpecStep[];
};

export type BuilderStep = {
  localId: string;
  id: string;
  role: ProcessRole;
  action: ProcessAction;
  kind: ProcessKind;
};

export type CompilePreview = {
  spec: ProcessSpec | null;
  steps: ProcessSpecStep[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function isProcessRole(value: string): value is ProcessRole {
  return (PROCESS_ROLES as readonly string[]).includes(value);
}

export function isProcessAction(value: string): value is ProcessAction {
  return (PROCESS_ACTIONS as readonly string[]).includes(value);
}

export function isProcessKind(value: string): value is ProcessKind {
  return (PROCESS_KINDS as readonly string[]).includes(value);
}

export function slugProcessKey(value: string): string {
  return (
    value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 80) || "custom"
  );
}

export function emptyBuilderStep(index = 1): BuilderStep {
  return {
    localId: `local_${index}`,
    id: `step_${index}`,
    role: "product_manager",
    action: "accept_prd",
    kind: "role",
  };
}

export function mapProcessTemplates(payload: Record<string, unknown>): ProcessTemplate[] {
  const raw = Array.isArray(payload.templates) ? payload.templates : [];
  return raw.flatMap((item, index) => {
    if (!isRecord(item)) return [];
    const processKey = asString(item.process_key);
    if (!processKey) return [];
    return [
      {
        id: asString(item.id, `template-${index}`),
        processKey,
        name: asString(item.name, processKey),
        description: asString(item.description),
      },
    ];
  });
}

export function mapProcessRuns(payload: Record<string, unknown>): ProcessRun[] {
  const raw = Array.isArray(payload.runs) ? payload.runs : [];
  return raw.flatMap((item) => {
    if (!isRecord(item)) return [];
    const id = asString(item.id);
    if (!id) return [];
    return [
      {
        id,
        title: asString(item.title, "process"),
        status: asString(item.status),
        processKey: asString(item.process_key) || undefined,
        templateName: asString(item.template_name) || undefined,
      },
    ];
  });
}

function verdictFrom(row: Record<string, unknown>): string | undefined {
  if (typeof row.verdict === "string" && row.verdict.trim()) return row.verdict;
  if (isRecord(row.output) && typeof row.output.verdict === "string" && row.output.verdict.trim()) {
    return row.output.verdict;
  }
  return undefined;
}

export function mapProcessSteps(payload: Record<string, unknown> | unknown[]): ProcessStep[] {
  const raw = Array.isArray(payload)
    ? payload
    : Array.isArray((payload as Record<string, unknown>).steps)
      ? ((payload as Record<string, unknown>).steps as unknown[])
      : [];
  return raw.flatMap((item, index) => {
    if (!isRecord(item)) return [];
    const stepKey = asString(item.step_key || item.id, `step_${index + 1}`);
    return [
      {
        id: asString(item.id, stepKey),
        stepKey,
        role: asString(item.role_key || item.role),
        action: asString(item.action),
        status: asString(item.status),
        verdict: verdictFrom(item),
      },
    ];
  });
}

function mapSpecStep(raw: unknown, index: number): ProcessSpecStep | null {
  if (!isRecord(raw)) return null;
  const id = asString(raw.id, `step_${index + 1}`);
  const kind = isProcessKind(asString(raw.kind, "role")) ? (raw.kind as ProcessKind) : "role";
  const listedRoles = Array.isArray(raw.roles)
    ? raw.roles.filter((item): item is string => typeof item === "string" && isProcessRole(item))
    : [];
  const roleValue = asString(raw.role || listedRoles[0], "product_manager");
  const role = isProcessRole(roleValue) ? roleValue : "product_manager";
  const actionValue = asString(raw.action, "accept_prd");
  const action = isProcessAction(actionValue) ? actionValue : "accept_prd";
  const next = Array.isArray(raw.next)
    ? raw.next.filter((item): item is string => typeof item === "string" && Boolean(item))
    : [];
  const waitFor = Array.isArray(raw.wait_for)
    ? raw.wait_for.filter((item): item is string => typeof item === "string" && Boolean(item))
    : [];
  return {
    id,
    kind,
    role,
    roles: kind === "panel" ? (listedRoles.length ? (listedRoles as ProcessRole[]) : [role]) : [role],
    action,
    next,
    wait_for: waitFor,
  };
}

export function mapProcessSpec(payload: Record<string, unknown>): ProcessSpec | null {
  const spec = isRecord(payload.spec) ? payload.spec : payload;
  const stepsRaw = Array.isArray(spec.steps) ? spec.steps : [];
  const steps = stepsRaw
    .map((item, index) => mapSpecStep(item, index))
    .filter((item): item is ProcessSpecStep => Boolean(item));
  if (!steps.length) return null;
  const key = slugProcessKey(asString(spec.key || spec.process_key, "custom"));
  return {
    key,
    name: asString(spec.name, "Custom process"),
    description: asString(spec.description),
    entry: asString(spec.entry, steps[0].id),
    steps,
  };
}

export function mapCompilePreview(payload: Record<string, unknown>): CompilePreview {
  const spec = mapProcessSpec(payload);
  const previewRaw = isRecord(payload.preview) ? payload.preview : null;
  const previewSpec = previewRaw ? mapProcessSpec(previewRaw) : null;
  const chosen = spec ?? previewSpec;
  const looseSteps = Array.isArray(payload.steps)
    ? payload.steps
        .map((item, index) => mapSpecStep(item, index))
        .filter((item): item is ProcessSpecStep => Boolean(item))
    : [];
  return {
    spec: chosen,
    steps: chosen?.steps.length ? chosen.steps : looseSteps,
  };
}

export function specFromBuilderSteps(input: {
  processKey: string;
  name: string;
  description?: string;
  steps: BuilderStep[];
}): ProcessSpec {
  const steps = input.steps
    .map((step, index) => ({
      ...step,
      id: slugProcessKey(step.id || `step_${index + 1}`),
    }))
    .filter((step) => step.id);
  const mapped: ProcessSpecStep[] = steps.map((step, index) => {
    const next = steps[index + 1] ? [steps[index + 1].id] : [];
    return {
      id: step.id,
      kind: step.kind,
      role: step.role,
      roles: step.kind === "panel" ? [step.role] : [step.role],
      action: step.action,
      next,
      wait_for: [],
    };
  });
  const key = slugProcessKey(input.processKey || input.name || "custom");
  return {
    key,
    name: input.name.trim() || "Custom process",
    description: (input.description || "").trim(),
    entry: mapped[0]?.id || "step_1",
    steps: mapped,
  };
}

export function builderStepsFromSpec(spec: ProcessSpec): BuilderStep[] {
  return spec.steps.map((step, index) => ({
    localId: `local_${index + 1}_${step.id}`,
    id: step.id,
    role: step.role,
    action: step.action,
    kind: step.kind,
  }));
}

export function formatProcessStepLine(step: ProcessStep): string {
  const parts = [step.stepKey, step.role, step.action, step.status];
  parts.push(step.verdict || "—");
  return parts.filter((part) => part !== "").join(" · ");
}
