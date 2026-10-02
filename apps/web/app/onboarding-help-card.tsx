import React from "react";

/**
 * What this console can actually do, for someone seeing it for the first time.
 *
 * This card used to be design-partner SETUP: `make onboard`, `make seed`, Plane API keys,
 * "repo access is granted by a human". Two problems. It was setup instructions on a page you
 * only reach after setup has already succeeded — nobody reading it needs it. And it described a
 * private evaluation checkout, so a reader could not tell which parts apply to the release in
 * front of them.
 *
 * It now answers the question a first-time operator actually has: what can I do here, and what
 * is not here yet. The second half matters more than the first — a feature list that quietly
 * omits its own limits is how "it says it can deploy" becomes an hour of confusion.
 *
 * Keep this honest as the product changes. Setup lives in
 * docs/operations/DESIGN_PARTNER_ONBOARDING.md, where instructions belong.
 */
export function OnboardingHelpCard() {
  return (
    <article className="stack-card" data-testid="console-capabilities-card">
      <div className="section-head">
        <div>
          <span className="eyebrow">Help</span>
          <h2>What you can do here</h2>
        </div>
      </div>

      <div className="help-grid">
        <div className="help-col">
          <h3 className="help-h">Three ways in</h3>
          <ul className="help-list">
            <li>
              <b>Ask</b> — a question about your work. BOSS reads live state and answers with
              real numbers; it can also act on the answer with your approval.
            </li>
            <li>
              <b>Research</b> — when the idea is not formed yet. Web research produces a cited
              dossier and candidate outcomes under <b>Discovery</b>.
            </li>
            <li>
              <b>Build</b> — a directive. A panel debates it, drafts a PRD, you approve, and the
              project runs. Watch it in <b>Meetings</b>, then <b>Factory</b>.
            </li>
          </ul>
        </div>

        <div className="help-col">
          <h3 className="help-h">What is not here yet</h3>
          <ul className="help-list">
            <li>
              You can record a <b>host and a target</b> under Infra → Hosts. BOSS-initiated
              SSH planning and deployment are paused in Community.
            </li>
            <li>
              Merge state is <b>not tracked</b> — the console shows PRs opened, not merged. That
              needs GitHub.
            </li>
            <li>
              Previews and sandboxes are <b>not guaranteed</b>. A blocked surface fails closed
              and says so rather than passing silently.
            </li>
          </ul>
        </div>
      </div>

      <p className="detail help-foot">
        Anything BOSS starts is inspectable — every run, its cost, and the evidence behind it.
        Setup and operations live in <code>docs/operations/DESIGN_PARTNER_ONBOARDING.md</code>.
      </p>
    </article>
  );
}
