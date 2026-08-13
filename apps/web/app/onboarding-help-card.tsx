import React from "react";

/**
 * Honest Command Center pointer for private design-partner setup.
 * Not a Cloud product, not a public-OSS flip, not a sandbox guarantee.
 */
export function OnboardingHelpCard() {
  return (
    <article className="stack-card" data-testid="design-partner-onboarding-card">
      <div className="section-head">
        <div>
          <span className="eyebrow">Help</span>
          <h2>Design-partner setup</h2>
        </div>
      </div>
      <p className="detail">
        This checkout is a <strong>private</strong> design-partner evaluation, not a
        public open-source flip and not BOSS Cloud. Repo access is granted by a
        human; Mission Control cannot mint GitHub or Plane credentials.
      </p>
      <p className="detail">
        Week-1 path (never print secrets):{" "}
        <code>make onboard</code> then{" "}
        <code>make install</code>, <code>make seed</code>, <code>make dev</code>,{" "}
        <code>make smoke</code>. Optional control-plane gate:{" "}
        <code>make qa-live</code> (requires <code>BOSS_API_KEY</code> in the
        environment; the target does not echo it).
      </p>
      <ul className="detail" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
        <li>
          <strong>Community</strong> is the self-hosted plane (BYOK, GitHub bind,
          local Mission Control). Cloud is optional Easy Connect — not required.
        </li>
        <li>
          Paste a model key via System Configuration paste-and-connect, or put it
          in <code>.env</code>. GitHub bind uses an env SecretRef — do not paste a
          raw token here.
        </li>
        <li>
          Sandboxes and previews are <strong>not</strong> guaranteed. An empty or
          blocked surface is fail-closed, not a silent pass.
        </li>
        <li>
          File friction as a GitHub issue on this private repo. Do not invent
          Plane API keys. Internal ops <code>/requests</code> already become Plane
          issues — do not duplicate them.
        </li>
      </ul>
      <p className="detail" style={{ marginTop: 10 }}>
        Checkable guide:{" "}
        <code>docs/operations/DESIGN_PARTNER_ONBOARDING.md</code>
        {" · "}
        Engine-only pack: <code>engine/DESIGN_PARTNER_ONBOARDING.md</code>
      </p>
    </article>
  );
}
