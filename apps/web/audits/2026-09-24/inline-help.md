# Inline help audit (BOSS-437)

The 24 September console pass compared Command Center, Govern, Repo and model, and the new-project workflow wizard at desktop width. Current-run screenshots are kept in the local `.audit-inline/` directory for review; they contain internal hostnames and are excluded from public export.

| Finding | Evidence before change | Change |
| --- | --- | --- |
| Repeated setting explanations make Govern a long list of paragraphs. | Every setting printed `help` between its label and input. | Put each explanation behind a labeled `?`; keep value source, limits, reset, save feedback, and the Captain 1/0 consequence visible. |
| The main composer repeats its purpose above the input. | Two lines of static instruction sat between the title and form. | Keep the approval consequence visible and move usage guidance beside the title. |
| Setup introduces itself with a second paragraph above the actual connection status. | The first card showed an abstract explanation before repository/model status. | Name the two required connections directly; put the concept in help. Keep connection checks, token scopes, replacement warning, and validation inline. |
| New project labels have long hints that compete with inputs. | The description and audience hints stretched across the wizard; declared workflow inputs did the same. | Put writing guidance and field descriptions beside labels. Keep acceptance-criteria consequence, agent sign-off effect, repository destination, and inaccessible-link behavior visible. |
| Dashboard intro duplicates its heading. | A sentence explained that the consolidated snapshot includes budget, queue, work, proof, and health. | Put that definition beside the heading. Keep dynamic KPI sublabels and unavailable state visible. |
| The workflow tab strip expands the page on a narrow phone. | At 390 px, its fit-content width made the document 414 px wide before the change. | Constrain the tab strip to the available width and let it scroll within itself. Final document width was 382 px at a 390 px viewport. |

The shared `HelpTip` uses a button with a specific accessible name and a tooltip associated through `aria-describedby` only while open. Mouse hover, keyboard focus, click/touch toggle, Escape, and outside press are supported. Its fixed bubble is positioned above or below the trigger and clamped to the viewport; it is not clipped by cards.

## Remaining help inventory

- Workflow builder and catalog still contain static capability and gate explanations. Review each before conversion because many explain an irreversible or approval-gated choice.
- Govern's cost governance, policy, and provider sections still have longer introductory copy. Preserve live provenance, thresholds, and security warnings when simplifying them.
- Repo and model has an existing token disclosure. Do not replace its permission scopes or secret-storage explanation with hover-only help.
- Onboarding help is already behind a disclosure; it is a deliberate first-run guide rather than repeated page chrome.
- Other areas such as Infra, Observability, and Agent Studio were not covered by this slice. Their repeated static help should use the shared pattern where the primary label remains clear without it.

## Verification

Run `pnpm --dir apps/web test`, `pnpm --dir apps/web lint`, and `pnpm --dir apps/web build`. Review the four flows at desktop and narrow width. In the wizard, activating `?` inside a label must leave the field value unchanged and the tooltip in view. See BOSS-437 for PR and final validation evidence.
