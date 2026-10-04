# boss-kit-v1 — the BOSS crew

Every agent face in BOSS is drawn by one pure function, `avatarSvg(spec, {size, state, variant})`
in `lib/agent-avatar.ts`. React (`app/agent-avatar.tsx`), the share card (`app/agents/share-card-visual.tsx`)
and the README cast (`docs/cast/`, `pnpm --filter boss-web cast:export`) all call it, so an agent is the same
picture everywhere. Original art, Apache-2.0, no third-party icon set or generator.

| File | What it holds |
|---|---|
| `palettes.ts` | 9 colour families from the brand tokens; contrast helpers |
| `hats.ts` | head gear: one owned hat per hand-designed character, plus generic hats for custom agents |
| `visors.ts` | the face plate: glass, eyes and mouth bars, with three levels of detail |
| `emblems.ts` | chest glyphs, drawn in a ±8 box |
| `cast.ts` | every shipped role's face (hand-designed or hand-picked), name, callsign, tagline |
| `states.ts` | the 9 states, their words, and the only mappers from a real status to a state |

Custom agents get a seeded face (cyrb53 of their `agent_id`); `Reroll` changes only the seed. A seeded face
never wears a cast hat and never lands on a shipped role's exact face.

## Checklist for every change to the kit

- [ ] **No real-person signifiers.** No likeness, signature clothing (turtleneck, specific hair + glasses),
      logos or brand marks. Characters are robots with light-bar faces; callsigns are invented words.
- [ ] **Reads at 24px.** Check the change in the 24px row of the cast preview: silhouette and colour alone
      must tell this character from its neighbours.
- [ ] **Contrast.** Every palette's `body` is ≥ 3:1 on `--surface`/`--surface-2`/`--void` and every `glow`
      ≥ 3:1 on the visor glass (`tests/agent-avatar.test.ts` computes it).
- [ ] **Stays in the porthole.** Hats use absolute path commands only; the test measures every drawn point
      against the disc.
- [ ] **No ids, filters, gradients or clip paths.** Many faces share one page; a duplicate id repaints another.
- [ ] **Motion only for a real state.** New keyframes are `agav-*`, animate only transform/opacity, live in
      `app/agents/agent-world.css`, and stop under `prefers-reduced-motion`.
- [ ] **Regenerate the cast.** `pnpm --filter boss-web cast:export`, then commit `docs/cast/`.
