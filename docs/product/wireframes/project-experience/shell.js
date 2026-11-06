/* The console chrome for every wireframe: rail, top bar, and (on a project page) the persistent
   project header — so every screen shows the same placement. Query flags for review/rendering:
   ?theme=light  ?nav=collapsed  ?drawer=1 (phone nav open)  ?notes=0 (hide design pins). */
(function () {
  const q = new URLSearchParams(location.search);
  const root = document.documentElement;
  root.dataset.theme = q.get("theme") === "light" ? "light" : "dark";
  if (q.get("notes") === "0") root.setAttribute("data-hide-notes", "");
  const B = document.body.dataset;
  const base = B.base || "../";

  const NAV = [
    ["Platform", [["overview", "squares-four", "Command Center", "What needs you, across every project"]]],
    ["Run", [
      ["projects", "folders", "Projects", "Every project: its work, crew, repo and deploys", { isNew: true }],
      ["code_tasks", "code", "Code Tasks", "Builds, apps and gated PRs (was Factory)"],
      ["agent_ops", "robot", "Agents at work", "What agents resolved on their own"],
      ["workflows", "flow-arrow", "Workflows", "Build, run and watch workflows"],
      ["workforce", "users-three", "Agent Studio", "Who does the work"],
      ["knowledge", "graph", "Knowledge Graph", "Code, rules and what agents learned"],
      ["decisions", "seal-check", "Approvals", "Explicit approvals before real effects", { badge: "2" }],
      ["infra", "hard-drives", "Infra", "Where your apps run"],
      ["observability", "pulse", "Observability", "Is it alive, how fast, what it costs"],
      ["self_fixes", "wrench", "Self-fixes", "Bugs in BOSS that BOSS fixed itself"],
    ]],
    ["Settings", [
      ["setup", "database", "Repo and models", "Where BOSS writes code and what it thinks with"],
      ["skills", "puzzle-piece", "Skills & MCP", "What your crew can do"],
      ["budget", "scales", "Govern", "Spend caps, pace and limits"],
      ["enterprise", "identification-card", "Identity", "People, roles, agents and sign-in"],
    ]],
  ];
  const PROJECTS = [["HelpDesk AI Demo", "you"], ["NorthPeak Retail", "run"], ["Hello World", "done"]];

  const active = B.active || "projects";
  const side = [`<div class="wf-brand"><span class="wf-logo">B</span><span>BOSS</span></div>`];
  for (const [grp, items] of NAV) {
    side.push(`<div class="wf-grp">${grp}</div><div class="wf-sep"></div>`);
    for (const [id, icon, label, tip, o = {}] of items) {
      const cur = id === active ? ' aria-current="page"' : "";
      side.push(`<a class="wf-item" title="${label} — ${tip}"${cur}><i class="ph ph-${icon}"></i><span class="wf-label">${label}</span>` +
        (o.badge ? `<span class="wf-badge">${o.badge}</span>` : "") + (o.isNew && id !== active ? `<span class="wf-new">NEW</span>` : "") + `</a>`);
      if (id === "projects" && (active === "projects" || B.navOpenProjects)) {
        side.push(`<div class="wf-sub">` + (B.empty ? [] : PROJECTS).map(([n, s], i) =>
          `<a class="wf-item" title="${n}"${B.project && i === 0 ? ' aria-current="page"' : ""}><span class="wf-dot dot" data-s="${s}"></span><span class="wf-label ell">${n}</span></a>`).join("") +
          `<a class="wf-item" title="Add a project — new from an idea, or an existing repo"><i class="ph ph-plus"></i><span class="wf-label">Add project</span></a></div>`);
      }
    }
  }
  side.push(`<div class="wf-foot"><img class="face" data-size="s" src="${base}faces/you.svg" alt=""><span class="wf-label">Param · Owner</span></div>`);

  const crumb = B.crumb ? B.crumb.split("|") : ["Projects"];
  const top = `<header class="wf-top">
    <span class="wf-burger" title="Menu"><i class="ph ph-list"></i></span>
    <div class="wf-crumb">${crumb.map((c, i) => (i ? `<span class="sep hide-s">/</span>` : "") + `<span class="${i < crumb.length - 1 ? "muted hide-s" : ""}">${c}</span>`).join("")}</div>
    <span class="wf-spacer"></span>
    <span class="wf-search hide-phone" title="Jump to any project, run, PR, deploy or agent"><i class="ph ph-magnifying-glass"></i> Jump to… <span class="wf-kbd">⌘K</span></span>
    <span class="wf-live hide-phone">Live</span>
    <span class="wf-icon-btn" title="Approvals waiting on you"><i class="ph ph-bell"></i><b>2</b></span>
    <img class="face" src="${base}faces/you.svg" alt="You">
  </header>`;

  let pj = "";
  if (B.project) {
    const tab = B.tab || "overview";
    const TABS = [["overview", "house", "Overview"], ["work", "kanban", "Work", "9"], ["roadmap", "map-trifold", "Roadmap", "5"],
                  ["repo", "git-branch", "Repo & knowledge"], ["deploy", "rocket-launch", "Deployments"]];
    pj = `<div class="pj-sticky">
      <div class="pj-head">
        <span class="pj-mark" title="HelpDesk AI Demo">HD</span>
        <div class="pj-title"><h1>HelpDesk AI Demo</h1>
          <div class="row wrap"><a class="link-chip" title="Repository — opens Repo & knowledge"><i class="ph ph-github-logo"></i>paramg/helpdesk-ai-demo</a>
          <a class="link-chip" title="Staging — healthy; opens the live app"><span class="dot" data-s="done"></span>staging · 2.29.2.202:8004 <i class="ph ph-arrow-square-out"></i></a>
          <span class="tag" data-tone="green" title="Project stage on the journey"><i class="ph ph-hammer"></i>Working</span></div></div>
        <span class="wf-spacer"></span>
        <div class="pj-captain" title="Helm captains this project. Click to talk to Helm.">
          <img class="face" data-size="m" src="${base}faces/helm.svg" alt=""><span><b>Helm</b><small>Captain</small></span>
          <span class="btn" data-size="s"><i class="ph ph-chat-circle-dots"></i>Ask</span></div>
        <span class="btn" data-kind="primary" title="A feature, a bug or a ticket — Helm plans it"><i class="ph ph-plus"></i>New work</span>
      </div>
      <div class="needs-bar" title="Always on top of the project: what only you can do">
        <span class="ny-ico">!</span><b>2 need you</b>
        <a class="needs-item"><i class="ph ph-git-merge"></i>Merge PR #3</a>
        <a class="needs-item"><i class="ph ph-cloud-check"></i>Pick prod host</a>
        <span class="wf-spacer"></span><span class="muted hide-phone">≈ 2 min</span>
      </div>
      <nav class="pj-tabs" role="tablist">${TABS.map(([id, ic, l, n]) =>
        `<span class="pj-tab" role="tab" aria-selected="${id === tab}"><i class="ph ph-${ic}"></i>${l}${n ? `<span class="n">${n}</span>` : ""}</span>`).join("")}</nav>
    </div>`;
  }

  const content = document.getElementById("content");
  const overlay = document.getElementById("overlay");
  const shell = document.createElement("div");
  shell.className = "wf-shell";
  if (q.get("nav") === "collapsed" || B.nav === "collapsed") shell.dataset.nav = "collapsed";
  if (q.get("drawer") === "1") shell.setAttribute("data-drawer-open", "");
  shell.innerHTML = `<aside class="wf-side">${side.join("")}</aside><div class="wf-main">${top}<main class="wf-content">${pj}</main></div>`;
  const main = shell.querySelector(".wf-content");
  while (content.firstChild) main.appendChild(content.firstChild);
  content.remove();
  document.body.prepend(shell);
  if (overlay) document.body.appendChild(overlay);

  // <span class="face" data-a="helm"> → the kit's real avatar SVG (rendered from lib/agent-avatar.ts)
  document.querySelectorAll("span.face[data-a]").forEach((el) => {
    const img = document.createElement("img");
    img.className = el.className; img.alt = ""; img.title = el.title || "";
    for (const k of Object.keys(el.dataset)) img.dataset[k] = el.dataset[k];
    img.src = `${base}faces/${el.dataset.a}.svg`;
    el.replaceWith(img);
  });
})();
