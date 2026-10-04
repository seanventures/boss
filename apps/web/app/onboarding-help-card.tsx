import React from "react";

/**
 * What this console can actually do, for someone seeing it for the first time.
 *
 * Keep this honest as the product changes — it went stale once already. It described an "Ask"
 * button the composer no longer has, sent research to Discovery and builds to Meetings then
 * Factory (both hidden from the rail; Build goes to Workflow), named a "CI/CD → Hosts" area
 * that is called Infra, said BOSS-initiated deploys were paused while the Approvals deploy gate
 * says approving ships it, said merge state was not tracked while a PR approval merges, and
 * cited docs/operations/DESIGN_PARTNER_ONBOARDING.md, which does not exist.
 *
 * Every area named below is one the rail shows; tests/onboarding-help-card checks that.
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
          <h3 className="help-h">Two ways in</h3>
          <ul className="help-list">
            <li>
              <b>Build</b> — describe what you want. BOSS starts a workflow run: a panel writes the
              spec, an agent builds and tests it and opens a pull request. Watch every step in
              {" "}<b>Workflow</b>; the finished project is in <b>Factory</b>.
            </li>
            <li>
              <b>Talk to BOSS</b> — ask about your work. BOSS answers from live state and can act
              on the answer when you press the button it offers.
            </li>
          </ul>
          <h3 className="help-h">What needs you</h3>
          <ul className="help-list">
            <li>
              <b>Approvals</b> holds what BOSS will not do without you — shipping to a host,
              merging a pull request. When BOSS refuses to spend (over budget, no model credit)
              the card says what to fix and where.
            </li>
            <li>
              A step that fails stops its run. The Command Center lists it with the reason and a
              {" "}<b>Retry</b> once you have fixed the cause.
            </li>
          </ul>
        </div>

        <div className="help-col">
          <h3 className="help-h">Before you start, and the limits</h3>
          <ul className="help-list">
            <li>
              A build needs a connected repository and model — <b>Repo and models</b>.
            </li>
            <li>
              Deploying needs a <b>host and a target</b>: register the host in <b>Infra</b>,
              then bind the app to it on the run&apos;s Deploy step. Nothing ships until you approve
              the deploy gate.
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
        Setup is in <code>docs/community/GETTING_STARTED.md</code>.
      </p>
    </article>
  );
}
