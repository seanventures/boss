# Console v2 reference

The `Autonomous Company Builder` artifact, saved from the browser so the migration is planned
against the real thing rather than against screenshots.

| File | What it is |
|---|---|
| `artifact.html` | the saved frame document — rendered DOM of the Projects screen, plus all CSS |
| `app.js` | the React component extracted from it — state, nav, and `renderVals()` |

`renderVals()` is the useful part: it computes the data for **every** screen, including the
eight the DOM never contains (only the active view is rendered). That is where the nav ids,
the agent roster, the panel stances, the workflow stage kinds and the settings toggles were
read from.

Design tokens extracted from `artifact.html` are recorded in `../CONSOLE_V2_MIGRATION.md` §2.

Kept in the repo because a plan that cites a mock nobody else can open is unreviewable.
