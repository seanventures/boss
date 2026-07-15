# BOSS Mission Control Design System

Status: implemented under Plane issue `BOSS-112`.

The BOSS interface is a calm, high-density mission-control surface for a
financially governed autonomous company. It favors flat, hairline-divided
planes over decorative cards, makes operator identity explicit, and reserves
motion for live system state.

## Source of truth

The implementation is based on the approved screenshot handoff
`BOSS Design system from screenshots.zip`. The production token and component
layer lives in `apps/web/app/design-system.css`; the supplied transparent BOSS
mark lives in `apps/web/public/boss-mark.png`.

## Foundations

### Color

| Role | Value |
| --- | --- |
| Void | `#090D17` |
| Base | `#0C1424` |
| Sidebar | `#0B101E` |
| Panel | `#101A30` |
| Alternate panel | `#0E1730` |
| Graph | `#0C1526` |
| Raised | `#16213C` |
| Hairline | `#131D34` / `#14203A` |
| Strong border | `#26365C` |
| Active blue | `#3A6DE0` |
| Primary blue | `#2F6FE0` |
| Live teal | `#2EC5C5` |
| Verified | `#34D399` |
| Awaiting | `#F5B544` |
| Blocked | `#F2647A` |
| Primary text | `#EEF3FB` |
| Secondary text | `#C3D0E8` |
| Muted text | `#9FB0CE` |

The brand gradient runs from teal to primary blue, with deep navy at the edge.
It is reserved for the BOSS mark and primary actions.

### Type

- Sora: display headings and high-signal numeric values.
- Manrope: UI labels, controls, and body copy.
- JetBrains Mono: status, timestamps, identifiers, eyebrows, and operational data.

### Shape and layout

- Standard radius: 8px.
- Modal radius: 12px.
- Compact control radius: 5–6px.
- Data matrices and divided rows stay flat; circles are limited to operators,
  agent nodes, and status dots.
- The desktop shell uses a 244px sidebar and 72px top bar.

## Components

- Outcome bar: single-line command input with the gradient primary action.
- KPI strip: one divided data plane; never a row of floating cards.
- Coordination graph: supplied BOSS mark at the orchestration center, live agent
  nodes, dashed handoff paths, and a restrained graph grid.
- Status text: dot plus plain monospaced text; status is not a filled pill.
- Task board: five canonical states—Backlog, In progress, Review, Blocked, Done.
- Agent roster: flat rows with identity, operational status, trust, and work link.
- Approval queue: flat decision rows with explicit risk, default, deadline, and
  governed actions.
- Evidence receipt: identity, effect state, provider/job/dedupe lineage, and
  verification facts in one auditable plane.
- Inputs and controls: dark inset field, strong hairline, blue focus ring.
- Toasts and modals: raised only when temporal layering is necessary.

## Interaction and motion

Navigation, command submission, task tracing, approval actions, filters, forms,
and the command palette remain functional. Animation is restricted to live
status pulse, orchestration liveness, and existing workflow progress. All
motion is disabled under `prefers-reduced-motion: reduce`.

## Accessibility

- Every form control has a visible or programmatic label.
- Keyboard focus uses a high-contrast teal outline.
- The shell has labelled navigation and main landmarks plus a skip link.
- Status never relies on color alone; every dot has accompanying text.
- Mobile layouts keep the full operational hierarchy and provide horizontal
  scrolling for the five-column task board rather than crushing its content.

## Applying the system

Use existing primitives before adding another component. New surfaces must use
the tokens in `design-system.css`, remain flat by default, avoid filled status
pills and decorative gradients, expose loading/empty/error/success states, and
honor reduced motion. Any visible external effect must continue to follow BOSS
receipt, dedupe, budget, lease, and audit requirements; visual treatment never
weakens control-plane policy.
