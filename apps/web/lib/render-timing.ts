/**
 * Where the page's time actually goes.
 *
 * Temporary. Three hypotheses about the 20-30s load have already been wrong — that rendering
 * ~50 areas was the cost (it is 1.9s with feeds cached), that the API was slow (82 concurrent
 * requests in 1.6s), and that the seven sequential awaits at the top were the bulk (collapsing
 * them changed nothing measurable). A fourth guess is not worth making.
 *
 * The page writes phase marks here; /api/render-timing reads them back. Same process, so no
 * transport and nothing to render into the DOM — the previous attempt failed because
 * ConsoleShell does not forward unknown props to an element.
 */
export type RenderTiming = { at: string; area: string; marks: Array<[string, number]> };

const g = globalThis as unknown as { __bossRenderTiming?: RenderTiming[] };

export function recordRenderTiming(area: string, marks: Array<[string, number]>): void {
  g.__bossRenderTiming = [{ at: new Date().toISOString(), area, marks },
                          ...(g.__bossRenderTiming ?? [])].slice(0, 10);
}

export function lastRenderTimings(): RenderTiming[] {
  return g.__bossRenderTiming ?? [];
}
