/**
 * Extensions (boss.extension/v1, BOSSAPP-861): what the console reads from /v1/extensions.
 *
 * Install-wide like the plugin registry, enabled per project. Each row carries the capabilities it
 * was installed with (in words, for the consent), the trust gate's live verdict, and whether it is
 * on in the project being viewed.
 */
import { arr, obj, opt, str, usePolled, type Polled } from "./factory-floor";

export type ExtensionCapability = { capability: string; words: string };
export type ExtensionTrust = { trusted: boolean; problems: string[]; approvedChecksum: string; bundleChecksum: string };
export type ExtensionRow = {
  id: string; version: string; name: string; description: string; publisher: string;
  capabilities: ExtensionCapability[];
  tools: { name: string; description: string; riskTier: number }[];
  events: string[];
  jobs: { name: string; schedule: string }[];
  webhooks: { name: string; description: string; url: string }[];
  slots: { type: string; name: string; title: string }[];
  trust: ExtensionTrust;
  enabled: boolean;
  enabledProjects: string[];
  installedBy: string | null;
};
export type ExtensionList = { rows: ExtensionRow[] };
export type ExtensionPreview = {
  id: string; version: string; name: string; description: string; publisher: string;
  capabilities: ExtensionCapability[]; computedChecksum: string; declaredChecksum: string;
  trust: { trusted: boolean; problems: string[] };
};
export type SlotType = "project_panel" | "settings_panel";
export type ExtensionSlot = { extensionId: string; name: string; title: string; type: SlotType };

const strs = (v: unknown): string[] => arr(v).map((x) => str(x)).filter(Boolean);
const caps = (v: unknown): ExtensionCapability[] =>
  arr(v).map((c) => ({ capability: str(obj(c).capability), words: str(obj(c).words) })).filter((c) => c.capability);

function parseTrust(raw: unknown): ExtensionTrust {
  const t = obj(raw);
  return { trusted: t.trusted === true, problems: strs(t.problems),
           approvedChecksum: str(t.approvedChecksum), bundleChecksum: str(t.bundleChecksum) };
}

export function parseExtension(raw: unknown): ExtensionRow {
  const o = obj(raw);
  return {
    id: str(o.id), version: str(o.version), name: str(o.name) || str(o.id),
    description: str(o.description), publisher: str(o.publisher),
    capabilities: caps(o.capabilities),
    tools: arr(o.tools).map((t) => ({ name: str(obj(t).name), description: str(obj(t).description),
                                      riskTier: Number(obj(t).riskTier ?? 1) || 0 })),
    events: strs(o.events),
    jobs: arr(o.jobs).map((j) => ({ name: str(obj(j).name), schedule: str(obj(j).schedule) })),
    webhooks: arr(o.webhooks).map((w) => ({ name: str(obj(w).name), description: str(obj(w).description),
                                            url: str(obj(w).url) })),
    slots: arr(o.slots).map((s) => ({ type: str(obj(s).type), name: str(obj(s).name), title: str(obj(s).title) })),
    trust: parseTrust(o.trust),
    enabled: o.enabled === true,
    enabledProjects: strs(o.enabledProjects),
    installedBy: opt(o.installedBy) ?? null,
  };
}

export function parseExtensionList(raw: unknown): ExtensionList {
  return { rows: arr(obj(raw).extensions).map(parseExtension).filter((e) => e.id) };
}

export function parsePreview(raw: unknown): ExtensionPreview {
  const o = obj(obj(raw).preview);
  const t = obj(o.trust);
  return { id: str(o.id), version: str(o.version), name: str(o.name) || str(o.id),
           description: str(o.description), publisher: str(o.publisher), capabilities: caps(o.capabilities),
           computedChecksum: str(o.computedChecksum), declaredChecksum: str(o.declaredChecksum),
           trust: { trusted: t.trusted === true, problems: strs(t.problems) } };
}

export function parseSlots(raw: unknown): ExtensionSlot[] {
  return arr(obj(raw).slots).map((s) => {
    const o = obj(s);
    return { extensionId: str(o.extensionId), name: str(o.name), title: str(o.title) || str(o.name),
             type: (str(o.type) === "settings_panel" ? "settings_panel" : "project_panel") as SlotType };
  }).filter((s) => s.extensionId && s.name);
}

const enc = encodeURIComponent;
export const extensionsUrl = (projectId: string) => `/v1/extensions?project_id=${enc(projectId)}`;
export const slotsUrl = (type: SlotType, projectId: string) =>
  `/v1/extensions/slots?type=${type}&project_id=${enc(projectId)}`;
export const frameUrl = (extensionId: string, slot: string, projectId: string) =>
  `/v1/ext/${enc(extensionId)}/ui/${enc(slot)}?project_id=${enc(projectId)}`;
export const frameContextUrl = (extensionId: string, slot: string, projectId: string) =>
  `/v1/ext/${enc(extensionId)}/ui/${enc(slot)}/context?project_id=${enc(projectId)}`;

export const EXTENSIONS_POLL_MS = 60_000;
export const useExtensions = (projectId: string): Polled<ExtensionList> =>
  usePolled(projectId ? extensionsUrl(projectId) : null, parseExtensionList, () => EXTENSIONS_POLL_MS);
export const useExtensionSlots = (type: SlotType, projectId: string): Polled<ExtensionSlot[]> =>
  usePolled(projectId ? slotsUrl(type, projectId) : null, parseSlots, () => EXTENSIONS_POLL_MS);

/** The two words the console shows for a tool's tier (what tool policy does with it). */
export const tierWords = (tier: number): string =>
  tier >= 3 ? "off until turned on" : tier === 2 ? "asks a person" : "runs";
