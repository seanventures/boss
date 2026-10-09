"use client";

/**
 * THE TEMPLATES GALLERY (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.8): one card per approved template
 * — name, one-line summary, stack chips (greyed when this install's toolchain for it is off, the
 * tooltip says how to turn it on), labels (`Insecure by design` in amber) and the trial's
 * "Tested ✓ java, node". A card opens its side sheet (template-sheet.tsx). Used by the composer's
 * "From a template" and by "+ Template" in "Add to an existing app" (filtered to the app's stack).
 */
import React, { useEffect, useMemo, useState } from "react";

import {
  INSECURE, STACKS, installedStacks, labelWord, refusalWords, stackLabel, templatesApi, testedWord,
  type TemplateCard,
} from "../../../lib/templates";

/** The cards a gallery shows: approved ones, of the stack asked for (when one is). */
export function galleryCards(all: TemplateCard[], stack: string | null): TemplateCard[] {
  return all.filter((t) => t.status === "active" && (!stack || t.stacks.includes(stack)));
}

/** A stack chip's tooltip: on, off (how to turn it on), or unknown. */
export function stackTip(stack: string, installed: Set<string> | null): string {
  if (!installed) return `${stackLabel(stack)} reference and tests`;
  return installed.has(stack)
    ? `${stackLabel(stack)}: this install can build it`
    : `${stackLabel(stack)} is off on this install. Turn it on on the BOSS host: boss toolchains enable ${stack}`;
}

/** The toolchains this install has, once per mount; null while unknown (nothing greyed). */
export function useInstalledStacks(projectId: string): Set<string> | null {
  const [installed, setInstalled] = useState<Set<string> | null>(null);
  useEffect(() => {
    let alive = true;
    void installedStacks().then((s) => { if (alive) setInstalled(s); });
    return () => { alive = false; };
  }, [projectId]);
  return installed;
}

export function StackChips({ stacks, installed }: { stacks: string[]; installed: Set<string> | null }) {
  return (
    <span className="tpl-chips" aria-label="Stacks">
      {stacks.map((s) => {
        const off = !!installed && !installed.has(s);
        return <span key={s} className="tpl-chip" data-off={off || undefined} title={stackTip(s, installed)}>{stackLabel(s)}{off ? " · off" : ""}</span>;
      })}
    </span>
  );
}

export function LabelChips({ labels }: { labels: string[] }) {
  if (!labels.length) return null;
  return (
    <span className="tpl-chips">
      {labels.map((l) => (
        <span key={l} className="tpl-chip" data-tone={l === INSECURE ? "amber" : undefined}
              title={l === INSECURE ? "Deliberately vulnerable, for security practice. Applies only to a new app; BOSS refuses to deploy it." : undefined}>
          {labelWord(l)}
        </span>
      ))}
    </span>
  );
}

export function TemplateGallery({ projectId, stack, onPick, testId = "tpl-gallery" }: {
  projectId: string;
  /** Only templates for this stack (the app's, in "Add to an existing app"). */
  stack?: string | null;
  onPick: (t: TemplateCard) => void;
  testId?: string;
}) {
  const [all, setAll] = useState<TemplateCard[] | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<string | null>(stack ?? null);
  const installed = useInstalledStacks(projectId);
  useEffect(() => {
    let alive = true;
    templatesApi.list().then((t) => { if (alive) setAll(t); }).catch((e) => { if (alive) { setError(refusalWords(e)); setAll([]); } });
    return () => { alive = false; };
  }, []);
  const cards = useMemo(() => galleryCards(all ?? [], filter), [all, filter]);
  const stacksHere = useMemo(() => STACKS.filter((s) => (all ?? []).some((t) => t.status === "active" && t.stacks.includes(s))), [all]);
  return (
    <section className="tpl-gallery" data-testid={testId} aria-label="Templates">
      <div className="tpl-gallery-head" role="radiogroup" aria-label="Stack">
        {stack ? (
          <span className="tpl-chip" title="The app's stack: only templates that fit it">{stackLabel(stack)} only</span>
        ) : (
          <>
            <button type="button" role="radio" className="tpl-chip" aria-checked={!filter} data-testid="tpl-filter-all"
                    onClick={() => setFilter(null)}>All</button>
            {stacksHere.map((s) => (
              <button key={s} type="button" role="radio" className="tpl-chip" aria-checked={filter === s}
                      data-testid={`tpl-filter-${s}`} title={stackTip(s, installed)} onClick={() => setFilter(s)}>{stackLabel(s)}</button>
            ))}
          </>
        )}
      </div>
      {all === null ? <p className="tpl-muted">Loading templates…</p> : null}
      {error ? <p className="tpl-err" role="alert">{error}</p> : null}
      {all !== null && !error && !cards.length ? (
        <p className="tpl-muted" data-testid="tpl-gallery-empty">
          {filter ? `No approved template for ${stackLabel(filter)} yet.` : "No approved templates yet. Import one in Skills & MCP › Templates."}
        </p>
      ) : null}
      <div className="tpl-grid">
        {cards.map((t) => (
          <button key={t.id} type="button" className="tpl-card" data-testid={`tpl-card-${t.key}`}
                  aria-label={`${t.name}: ${t.summary}`} onClick={() => onPick(t)}>
            <span className="tpl-card-name" data-user-text={t.source === "git" || undefined}>{t.name}</span>
            <span className="tpl-card-sum" data-user-text={t.source === "git" || undefined}>{t.summary}</span>
            <LabelChips labels={t.labels} />
            <span className="tpl-card-foot">
              <StackChips stacks={t.stacks} installed={installed} />
              {t.tested.length ? <span className="tpl-tested" title="A trial ran the template's own tests in a throwaway sandbox">{testedWord(t.tested)}</span> : null}
              {t.source === "git" ? <span className="tpl-faint" title={t.gitUrl}>Imported</span> : null}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}
