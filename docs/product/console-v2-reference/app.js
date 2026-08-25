window.__resources = {"https://unpkg.com/react@18.3.1/umd/react.production.min.js":"blob:https://b7f2f82f-8996-4467-bc31-71da0fdb5602.frame.claudeusercontent.com/2e0c189b-0176-4a04-9dee-b9919d873aa5","https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js":"blob:https://b7f2f82f-8996-4467-bc31-71da0fdb5602.frame.claudeusercontent.com/cd180978-d405-4544-8675-f4abd0a1a9f0"};




class Component extends DCLogic {
  state = {
    view: 'missions', settingsTab: 'github', companyOpen: false, filter: 'ALL', missionFilter: 'ALL',
    chatOpen: false, chatWith: null, chatMsgs: [], chatDraft: '', chatTyping: false,
    launchOpen: false, brief: '', launchWf: 'Venture Launch', launchBudget: '$5k',
    gates: [
      {id:1, title:'Deploy GeneVault API v0.4 to prod', detail:'CI green, 214 tests passing. Terraform plan adds 2 GPU nodes ($310/mo).', by:'Forge (CTO)', ts:'14:02:11', state:'pending'},
      {id:2, title:'Purchase genevault.bio domain — $1,240', detail:'Marketing panel recommends over .com alternative ($8,900).', by:'Ledger (CFO)', ts:'13:47:52', state:'pending'},
    ],
    feed: [], clock: '14:06:31',
    verdictOpen: true, verdictDecided: false, verdictNote: '', panelMsgIdx: 3, searchQ: '',
    wfSel: 0, deployed: false, wfStagesCustom: null,
    auditProj: 'ALL', infraScope: 'GLOBAL',
    ghPerms: {merge: true, deploy: false, secrets: false, branch: true},
    ssoPols: {enforce: true, mfa: true, session: true, jit: false},
    missions: [
      {id:'m1', title:'GeneVault — genetic decoder app', desc:'Decode 23andMe / Ancestry raw data, trait + ancestry reports, launch & market.', status:'RUNNING', lead:'Atlas (CEO)', agents:12, steps:1847, spend:'$2,318 spent', stage:'BUILD', pct:64},
      {id:'m2', title:'GeneVault launch campaign', desc:'Positioning, landing page, paid + organic channels for public beta.', status:'RUNNING', lead:'Muse (PMM)', agents:4, steps:412, spend:'$486 spent', stage:'DISCOVERY', pct:22},
      {id:'m3', title:'SOC 2 readiness', desc:'Controls mapping, evidence collection, vendor review for genomic data handling.', status:'GATED', lead:'Forge (CTO)', agents:3, steps:238, spend:'$104 spent', stage:'REVIEW', pct:41},
      {id:'m4', title:'Competitive teardown: DNA analysis tools', desc:'Feature/pricing matrix across Promethease, Genomelink, SelfDecode.', status:'DONE', lead:'Scout (Analyst)', agents:2, steps:156, spend:'$38 spent', stage:'SHIPPED', pct:100},
    ],
  };
  agents = [
    {id:'atlas', name:'Atlas', role:'CEO', model:'opus-5', status:'active', task:'Reviewing Q3 project priorities; drafted directive for GeneVault beta scope.', tpm:'8.2k', proj:'DECODER', lastTouched:'directive-q3-beta.md', checkin:'42s ago'},
    {id:'meta', name:'Metatron', role:'Interface & Watchdog', model:'sonnet-5', status:'active', task:'Watching all lanes. 2 decisions need you; Quill is stuck on consent copy 22m.', tpm:'3.1k', proj:'ALL', lastTouched:'watchdog-digest-14:05.md', checkin:'8s ago'},
    {id:'rhea', name:'Rhea', role:'COO', model:'opus-5', status:'active', task:'Rebalancing agent capacity: moved Pixel to launch campaign for 48h.', tpm:'5.1k', proj:'ALL', lastTouched:'capacity-plan.json', checkin:'1m ago'},
    {id:'ledger', name:'Ledger', role:'CFO', model:'sonnet-5', status:'waiting', task:'Holding domain purchase at gate. Modeling freemium conversion scenarios.', tpm:'2.4k', proj:'CAMPAIGN', lastTouched:'domain-analysis.csv', checkin:'3m ago'},
    {id:'forge', name:'Forge', role:'CTO', model:'opus-5', status:'active', task:'Cut API v0.4 release; reviewing Vector\u2019s VCF parser benchmark results.', tpm:'11.6k', proj:'DECODER', lastTouched:'release-v0.4 tag', checkin:'19s ago'},
    {id:'arc', name:'Arc', role:'Chief Architect', model:'opus-5', status:'panel', task:'In panel: pricing & GTM. Also drafting genome-pipeline ADR-017.', tpm:'6.8k', proj:'DECODER', lastTouched:'ADR-017-draft.md', checkin:'2m ago'},
    {id:'vector', name:'Vector', role:'Backend Engineer', model:'sonnet-5', status:'active', task:'Optimizing SNP lookup — 23andMe v5 chip parse now 3.1s → 0.8s.', tpm:'14.2k', proj:'DECODER', lastTouched:'PR #212 · vcf-stream.rs', checkin:'12s ago'},
    {id:'pixel', name:'Pixel', role:'Frontend Engineer', model:'sonnet-5', status:'active', task:'Building ancestry chromosome-painting view; 3 PRs open.', tpm:'12.7k', proj:'DECODER', lastTouched:'PR #214 · ChromePaint.tsx', checkin:'28s ago'},
    {id:'nova', name:'Nova', role:'Product Manager', model:'sonnet-5', status:'panel', task:'In panel: pricing & GTM. Spec\u2019d trait-report card format.', tpm:'4.9k', proj:'DECODER', lastTouched:'trait-report-spec.md', checkin:'4m ago'},
    {id:'muse', name:'Muse', role:'Product Marketer', model:'sonnet-5', status:'active', task:'Writing launch narrative: “Your DNA file already knows.” A/B drafts ready.', tpm:'7.3k', proj:'CAMPAIGN', lastTouched:'launch-narrative-v2.md', checkin:'51s ago'},
    {id:'scout', name:'Scout', role:'Marketing Analyst', model:'haiku-4', status:'idle', task:'Teardown shipped. Awaiting next assignment from Rhea.', tpm:'0.2k', proj:'CAMPAIGN', lastTouched:'teardown-matrix.xlsx', checkin:'38m ago'},
    {id:'quill', name:'Quill', role:'Legal Counsel', model:'opus-5', status:'stuck', task:'STUCK 22m: consent-flow copy conflicts with retention policy. Needs Nova.', tpm:'1.8k', proj:'SOC2', lastTouched:'legal-memo-gina.md', checkin:'22m ago'},
    {id:'sentry', name:'Sentry', role:'Compliance', model:'sonnet-5', status:'active', task:'Mapping SOC 2 CC6 controls to infra; 14 of 61 evidenced.', tpm:'3.5k', proj:'SOC2', lastTouched:'cc6.1-evidence.json', checkin:'1m ago'},
  ];
  handoffs = [
    {from:'Vector', to:'Forge', what:'PR #212 benchmark results for release review', ts:'14:05', proj:'DECODER', color:'#4FE3C1'},
    {from:'Nova', to:'Ledger', what:'Freemium conversion assumptions for cost model', ts:'14:01', proj:'DECODER', color:'#A78BFA'},
    {from:'Muse', to:'Atlas', what:'Launch narrative v2 for directive alignment', ts:'13:57', proj:'CAMPAIGN', color:'#4FE3C1'},
    {from:'Sentry', to:'Quill', what:'CC6 evidence pack for legal review', ts:'13:52', proj:'SOC2', color:'#F5B14C'},
    {from:'Pixel', to:'Nova', what:'Chrome-paint prototype for spec check', ts:'13:48', proj:'DECODER', color:'#4FE3C1'},
    {from:'Metatron', to:'Rhea', what:'Stuck alert: Quill blocked 22m on consent copy', ts:'14:06', proj:'SOC2', color:'#F5B14C'},
  ];
  feedPool = [
    ['Vector','#4FE3C1','merged PR #212 — streaming VCF parser, +38 tests'],
    ['Pixel','#4FE3C1','pushed chromosome-painting component behind feature flag'],
    ['Muse','#4FE3C1','generated 4 landing-page hero variants for review'],
    ['Ledger','#F5B14C','flagged model spend: opus-5 lane at 71% of weekly cap'],
    ['Forge','#4FE3C1','autoscaled build fleet 3 → 5 runners (queue depth 12)'],
    ['Nova','#A78BFA','cast vote in pricing panel: FOR freemium'],
    ['Atlas','#4FE3C1','ratified ADR-016: DuckDB for variant annotation store'],
    ['Sentry','#4FE3C1','attached evidence: encryption-at-rest config → SOC2/CC6.1'],
    ['Quill','#F5B14C','raised concern: ancestry claims need confidence-interval copy'],
    ['Rhea','#4FE3C1','reassigned Scout → churn-benchmark research task'],
    ['Arc','#A78BFA','updated ADR-017 draft: genome pipeline idempotency'],
    ['Vector','#4FE3C1','deployed preview env pr-212.genevault.dev'],
  ];
  panelScript = [
    {n:'Nova', r:'Product Manager', s:'FOR', text:'Freemium wins discovery. Raw-file upload is a magic moment — paywalling it kills word of mouth. Cap free at 3 trait reports, upsell deep ancestry.'},
    {n:'Ledger', r:'CFO', s:'AGAINST', text:'Free genome processing costs us $0.14/user in compute. At projected 50k signups that\u2019s $7k/mo against zero committed revenue. One-time $29 is cleaner.'},
    {n:'Arc', r:'Chief Architect', s:'FOR', text:'Compute concern is solvable: batch free-tier jobs on spot instances overnight. Cuts Ledger\u2019s number to ~$0.03/user. Architecture shouldn\u2019t block the funnel.'},
    {n:'Muse', r:'Product Marketer', s:'FOR', text:'Every comparable that gates upload front-loads churn. “Free decode, paid depth” is the whole campaign hook. I can\u2019t market a $29 wall.'},
  ];
  timeline = [
    {ts:'14:05:58', actor:'Vector', dot:'#4FE3C1', kind:'CODE', hash:'#a41f9c2', text:'Merged PR #212: streaming VCF parser. Parse time for 23andMe v5 raw file: 0.8s (was 3.1s).', artifact:'pr-212-diff.patch'},
    {ts:'14:02:11', actor:'Forge', dot:'#F5B14C', kind:'GATE', hash:'#88d0e11', text:'Raised approval gate: deploy API v0.4 to production. Awaiting human sign-off.', artifact:null},
    {ts:'13:58:40', actor:'Panel', dot:'#A78BFA', kind:'PANEL', hash:'#c2b7f04', text:'Pricing & GTM panel convened. Members: Nova, Ledger, Arc, Muse. Quorum 4, CFO tiebreak.', artifact:null},
    {ts:'13:51:22', actor:'Muse', dot:'#4FE3C1', kind:'ARTIFACT', hash:'#71aa3d8', text:'Produced launch narrative draft v2 with A/B hero copy.', artifact:'launch-narrative-v2.md'},
    {ts:'13:47:52', actor:'Ledger', dot:'#F5B14C', kind:'GATE', hash:'#5f30b96', text:'Raised approval gate: purchase genevault.bio ($1,240). Cheaper than .com by $7,660.', artifact:'domain-analysis.csv'},
    {ts:'13:40:17', actor:'Atlas', dot:'#4FE3C1', kind:'DECISION', hash:'#e93c771', text:'Ratified ADR-016: DuckDB for variant annotation store. Rationale: columnar scans over 600k SNPs, zero-ops.', artifact:'ADR-016.md'},
    {ts:'13:31:05', actor:'Pixel', dot:'#4FE3C1', kind:'CODE', hash:'#2d8ea50', text:'Opened PR #214: chromosome-painting ancestry view (canvas, 60fps pan).', artifact:'pr-214-preview.png'},
    {ts:'13:24:48', actor:'Quill', dot:'#F5B14C', kind:'REVIEW', hash:'#b06c4e3', text:'Legal memo: GINA applies to employer/insurer use, not consumer reports. Consent flow needs explicit raw-file retention choice.', artifact:'legal-memo-gina.md'},
    {ts:'13:16:02', actor:'Sentry', dot:'#4FE3C1', kind:'EVIDENCE', hash:'#94f1d27', text:'Mapped encryption-at-rest (KMS, aes-256) to SOC 2 CC6.1. Evidence attached to control register.', artifact:'cc6.1-evidence.json'},
    {ts:'13:02:33', actor:'Rhea', dot:'#4FE3C1', kind:'STAFFING', hash:'#3ce082a', text:'Moved Pixel to launch-campaign project for 48h. Factory capacity rebalanced: build 7 → 6, marketing 3 → 4.', artifact:null},
  ];
  workflowDefs = [
    {name:'Venture Launch', chip:'ACTIVE', desc:'Zero-to-launch for a new product project.', meta:'7 stages · 3 panels · 2 human gates · used by 2 projects', stages:[
      {name:'Discovery', kind:'PANEL', detail:'CEO, PM, Analyst scope the brief into a project charter: target user, success metric, budget ask.', owners:['Atlas','Nova','Scout'], gated:false},
      {name:'Charter sign-off', kind:'GATE', detail:'Charter and budget cap presented to you. Nothing is spent until ratified.', owners:['Atlas'], gated:true},
      {name:'Architecture & plan', kind:'SEQUENTIAL', detail:'Chief Architect drafts ADRs; CTO decomposes into epics with cost estimates.', owners:['Arc','Forge'], gated:false},
      {name:'Build loop', kind:'PARALLEL', detail:'Engineers build; every merge auto-runs CI. Marketing drafts positioning in parallel.', owners:['Vector','Pixel','Muse'], gated:false},
      {name:'Pricing & GTM panel', kind:'PANEL', detail:'PM, CFO, Architect, Marketer deliberate pricing. Verdict goes to you for ratify/veto.', owners:['Nova','Ledger','Arc','Muse'], gated:false},
      {name:'Launch readiness', kind:'GATE', detail:'Deploy to prod, spend on domains/ads — batched into one approval.', owners:['Forge','Ledger'], gated:true},
      {name:'Ship & monitor', kind:'PARALLEL', detail:'Deploy, watch metrics, iterate. Analyst reports weekly to CEO.', owners:['Forge','Scout'], gated:false},
    ]},
    {name:'Feature Sprint', chip:'DRAFT', desc:'Scoped feature inside a running project.', meta:'4 stages · 1 panel · 1 human gate', stages:[
      {name:'Spec', kind:'SEQUENTIAL', detail:'PM writes spec with acceptance criteria; Architect reviews for pipeline impact.', owners:['Nova','Arc'], gated:false},
      {name:'Build', kind:'PARALLEL', detail:'Frontend + backend build against the spec; CI on every merge.', owners:['Vector','Pixel'], gated:false},
      {name:'Design review panel', kind:'PANEL', detail:'CTO + PM + Architect review the built feature against spec.', owners:['Forge','Nova','Arc'], gated:false},
      {name:'Release', kind:'GATE', detail:'Prod deploy gated on your approval.', owners:['Forge'], gated:true},
    ]},
    {name:'Compliance Review', chip:'DRAFT', desc:'Recurring audit loop for regulated data.', meta:'3 stages · 1 panel · 1 human gate', stages:[
      {name:'Evidence sweep', kind:'PARALLEL', detail:'Compliance agent collects control evidence from infra + CI automatically.', owners:['Sentry'], gated:false},
      {name:'Counsel review panel', kind:'PANEL', detail:'Legal + Compliance + CTO assess gaps and rank remediations.', owners:['Quill','Sentry','Forge'], gated:false},
      {name:'Remediation plan', kind:'GATE', detail:'Ranked plan with cost estimates goes to you before work is scheduled.', owners:['Quill'], gated:true},
    ]},
  ];
  fleetData = [
    {name:'API cluster', tag:'AWS · us-east-1', tagFg:'#5A6785', load:62, detail:'8 nodes'},
    {name:'Genome pipeline', tag:'GCP · spot', tagFg:'#5A6785', load:87, detail:'14 preempt.'},
    {name:'CI runners', tag:'BARE METAL', tagFg:'#4FE3C1', load:44, detail:'5 boxes'},
    {name:'Inference lane', tag:'ANTHROPIC API', tagFg:'#A78BFA', load:71, detail:'3 keys'},
    {name:'Vector store', tag:'AWS · us-east-1', tagFg:'#5A6785', load:23, detail:'r6g.xl ×2'},
  ];
  budgetData = [
    {name:'Model tokens', raw:[3912, 6000]},
    {name:'Cloud compute', raw:[2204, 3500]},
    {name:'Bare metal amortized', raw:[640, 800]},
    {name:'SaaS & domains', raw:[318, 1700]},
  ];
  fleetProj = {
    'API cluster': ['DECODER'], 'Genome pipeline': ['DECODER'], 'CI runners': ['DECODER','CAMPAIGN','SOC2'],
    'Inference lane': ['DECODER','CAMPAIGN','SOC2'], 'Vector store': ['DECODER','SOC2'],
  };
  integrationData = [
    {name:'Anthropic', detail:'opus-5 · sonnet-5 · haiku-4', ok:true},
    {name:'OpenAI', detail:'fallback lane · o5-mini', ok:true},
    {name:'AWS', detail:'us-east-1 · 2 accounts', ok:true},
    {name:'GCP', detail:'spot fleet · genome jobs', ok:true},
    {name:'GitHub', detail:'CI/CD · 14 repos · Actions', ok:true},
    {name:'Terraform Cloud', detail:'drift check 12m ago', ok:true},
    {name:'Okta SSO', detail:'SAML · SCIM · 4 seats', ok:true},
    {name:'Vault', detail:'secrets · auto-rotate 30d', ok:true},
    {name:'Resend', detail:'transactional email', ok:false},
    {name:'Stripe', detail:'billing · test mode', ok:true},
  ];
  repoData = [
    {name:'genevault/api', desc:'Core decode API — Vector owns', prs:3, ci:'PASSING', agent:'Vector'},
    {name:'genevault/web', desc:'Consumer app — Pixel owns', prs:4, ci:'PASSING', agent:'Pixel'},
    {name:'genevault/pipeline', desc:'Genome batch jobs — Arc owns', prs:1, ci:'RUNNING', agent:'Arc'},
    {name:'genevault/infra', desc:'Terraform + fleet config — Forge owns', prs:0, ci:'PASSING', agent:'Forge'},
    {name:'genevault/marketing-site', desc:'Launch site — Muse owns', prs:2, ci:'FAILED', agent:'Muse'},
  ];
  ciData = [
    {name:'Build & test on every agent merge', status:'ENABLED', color:'#58D68A'},
    {name:'Preview env per PR (*.genevault.dev)', status:'ENABLED', color:'#58D68A'},
    {name:'Prod deploy', status:'GATED — HUMAN', color:'#F5B14C'},
    {name:'Nightly dependency + CVE scan', status:'ENABLED', color:'#58D68A'},
  ];
  memberData = [
    {name:'Jordan Diaz', email:'jordan@genevault.bio', role:'Owner', roleColor:'#4FE3C1', companies:'All 3', auth:'Okta SAML', active:'now'},
    {name:'Sam Okafor', email:'sam@genevault.bio', role:'Operator', roleColor:'#E6EDF5', companies:'GeneVault', auth:'Okta SAML', active:'2h ago'},
    {name:'Priya Natarajan', email:'priya@genevault.bio', role:'Operator', roleColor:'#E6EDF5', companies:'GeneVault, Driftwake', auth:'Okta SAML', active:'1d ago'},
    {name:'Lee Tran', email:'lee@genevault.bio', role:'Auditor', roleColor:'#8D9AB5', companies:'All 3 (read-only)', auth:'Okta SAML', active:'3d ago'},
  ];
  roleData = [
    {name:'Owner', color:'#4FE3C1', desc:'Full control: gates, vetoes, budgets, workflows, member management.'},
    {name:'Operator', color:'#E6EDF5', desc:'Approve gates and steer agents on assigned companies. No billing or member changes.'},
    {name:'Auditor', color:'#8D9AB5', desc:'Read-only access to audit trail, panels, and spend. Cannot approve or steer.'},
  ];
  apiKeyData = [
    {name:'CI reporter', key:'boss_sk_…9f2a', scope:'audit:write', used:'used 14:05'},
    {name:'Metrics export', key:'boss_sk_…c771', scope:'read-only', used:'used 11:20'},
  ];
  componentDidMount() {
    let i = 0;
    this.feedTimer = setInterval(() => {
      const p = this.feedPool[i % this.feedPool.length]; i++;
      const now = new Date(); const ts = ['14', String(6 + Math.floor(i/2)).padStart(2,'0'), String((now.getSeconds()+i*7)%60).padStart(2,'0')].join(':');
      this.setState(s => ({ feed: [{actor:p[0], color:p[1], text:p[2], ts}, ...s.feed].slice(0, 14), clock: ts }));
    }, 3200);
    this.panelTimer = setInterval(() => {
      this.setState(s => s.panelMsgIdx < 4 ? {panelMsgIdx: s.panelMsgIdx + 1} : {});
    }, 6000);
  }
  componentWillUnmount() { clearInterval(this.feedTimer); clearInterval(this.panelTimer); }
  openChat(a) {
    this.setState({chatOpen: true, chatWith: a, chatDraft: '', chatTyping: false, chatMsgs: [
      {who:'agent', text: 'Hi — ' + a.name + ' here (' + a.role + '). Current focus: ' + a.task + ' What do you need?'}
    ]});
  }
  sendChat() {
    const d = this.state.chatDraft.trim(); if (!d) return;
    this.setState(s => ({chatMsgs: [...s.chatMsgs, {who:'me', text:d}], chatDraft:'', chatTyping:true}));
    setTimeout(() => {
      this.setState(s => ({chatTyping:false, chatMsgs: [...s.chatMsgs, {who:'agent', text:'Understood. I\u2019ve folded that into my working plan and logged it to the audit trail as directive #' + Math.floor(1000+Math.random()*9000) + '. I\u2019ll flag you at the next gate if it changes cost or scope.'}]}));
    }, 1600);
  }
  resolveGate(id, ok) {
    this.setState(s => ({gates: s.gates.map(g => g.id === id ? {...g, state: ok ? 'approved' : 'rejected'} : g),
      feed: [{actor:'You', color:'#4FE3C1', text: (ok ? 'approved' : 'rejected') + ' gate: ' + s.gates.find(g=>g.id===id).title, ts:s.clock}, ...s.feed].slice(0,14)}));
  }
  statusMeta(st) {
    if (st === 'active') return {dot:'#4FE3C1', label:'WORKING', anim:'pulse 1.8s infinite'};
    if (st === 'panel') return {dot:'#A78BFA', label:'IN PANEL', anim:'pulse 2.4s infinite'};
    if (st === 'waiting') return {dot:'#F5B14C', label:'AT GATE', anim:'none'};
    if (st === 'stuck') return {dot:'#F26D6D', label:'STUCK', anim:'blink 1s infinite'};
    return {dot:'#3E4D6B', label:'IDLE', anim:'none'};
  }
  toggle(row) { return {tbg: row ? 'rgba(79,227,193,.15)' : '#141B2B', tbd: row ? '#4FE3C1' : '#2C3A55', knob: row ? '#4FE3C1' : '#5A6785', knobLeft: row ? '18px' : '2px'}; }
  renderVals() {
    const S = this.state;
    const go = v => () => this.setState({view: v});
    const pendingGates = S.gates.filter(g => g.state === 'pending');
    const nav = [
      ['missions','Projects'], ['hive','Hive'], ['factory','Workforce'], ['org','Org Chart'], ['panels','Decisions'],
      ['boss','Workflows'], ['timeline','Receipts'], ['infra','Infra & Models'], ['settings','Settings'],
    ].map(([id, label]) => ({
      label, badge: id === 'panels' ? '1' : (id === 'factory' && pendingGates.length ? String(pendingGates.length) : false),
      onGo: go(id), bg: S.view === id ? '#0E1422' : 'transparent',
      bd: S.view === id ? '#4FE3C1' : 'transparent', fg: S.view === id ? '#E6EDF5' : '#8D9AB5',
    }));
    const tabify = (list, cur, set) => list.map(f => ({label: f, onPick: () => this.setState(set(f)),
      bd: cur === f ? '#4FE3C1' : 'transparent', fg: cur === f ? '#4FE3C1' : '#5A6785',
      bg: 'transparent'}));
    const filters = tabify(['ALL','WORKING','IN PANEL','AT GATE','IDLE'], S.filter, f => ({filter: f}));
    const projName = {ALL:'All projects', DECODER:'GeneVault decoder', CAMPAIGN:'Launch campaign', SOC2:'SOC 2'};
    const agents = this.agents.map(a => {
      const m = this.statusMeta(a.status);
      return {...a, dot: m.dot, statusLabel: m.label, anim: m.anim, projLabel: projName[a.proj] || a.proj, onChat: () => this.openChat(a)};
    });
    const wfScopes = ['GLOBAL','DECODER','CAMPAIGN','SOC2'].map(p => ({label: p,
      onPick: () => this.setState({wfScope: p}), bd: (S.wfScope||'GLOBAL') === p ? '#4FE3C1' : 'transparent', fg: (S.wfScope||'GLOBAL') === p ? '#4FE3C1' : '#5A6785'}));
    const scopeAgents = (S.wfScope||'GLOBAL') === 'GLOBAL' ? agents : agents.filter(a => a.proj === S.wfScope || a.proj === 'ALL');
    const agentsFiltered = S.filter === 'ALL' ? scopeAgents : scopeAgents.filter(a => a.statusLabel === S.filter);
    const stripProjects = S.missions.map(m => {
      const sc = m.status === 'RUNNING' ? '#4FE3C1' : m.status === 'GATED' ? '#F5B14C' : m.status === 'DONE' ? '#58D68A' : '#8D9AB5';
      return {name: m.title.split('—')[0].split(':')[0].trim().slice(0, 22), stage: m.stage, pct: m.pct, dot: sc,
        anim: m.status === 'RUNNING' ? 'pulse 2s infinite' : 'none',
        alert: m.status === 'GATED' ? 'GATED' : false, onGo: () => this.setState({view: 'missions'})};
    });
    const hiveById = {}; agents.forEach(a => hiveById[a.name] = a);
    const inner = ['Rhea','Forge','Ledger','Arc','Metatron'], outer = ['Vector','Pixel','Nova','Muse','Scout','Quill','Sentry'];
    const pos = {Atlas: [450, 320]};
    inner.forEach((n, i) => { const t = -Math.PI/2 + i * 2*Math.PI/inner.length; pos[n] = [450 + 130*Math.cos(t), 320 + 130*Math.sin(t)]; });
    outer.forEach((n, i) => { const t = -Math.PI/2 + .35 + i * 2*Math.PI/outer.length; pos[n] = [450 + 250*Math.cos(t), 320 + 250*Math.sin(t)]; });
    const roleTags = {Atlas:'CEO', Metatron:'WATCHDOG', Rhea:'COO', Ledger:'CFO', Forge:'CTO', Arc:'ARCHITECT', Vector:'BACKEND', Pixel:'FRONTEND', Nova:'PM', Muse:'PMM', Scout:'ANALYST', Quill:'LEGAL', Sentry:'COMPLIANCE'};
    const hiveNodes = Object.keys(pos).map(n => {
      const a = hiveById[n]; const [x, y] = pos[n]; const r = n === 'Atlas' ? 30 : 24;
      return {name: n, x: Math.round(x), y: Math.round(y), r, halo: r + 12, ny: Math.round(y) + 4, ry: Math.round(y) + r + 13, sy: Math.round(y) - r - 8,
        color: a.dot, anim: a.anim === 'none' ? 'none' : a.anim, roleTag: roleTags[n], statusLabel: a.statusLabel, onChat: a.onChat};
    });
    const orgPairs = [['Atlas','Rhea'],['Atlas','Forge'],['Atlas','Ledger'],['Atlas','Arc'],['Atlas','Metatron'],['Rhea','Nova'],['Rhea','Scout'],['Forge','Vector'],['Forge','Pixel'],['Ledger','Quill'],['Ledger','Sentry'],['Arc','Muse']];
    const mkEdge = (f, t, color, w, label) => {
      const [x1,y1] = pos[f], [x2,y2] = pos[t];
      const mx = (x1+x2)/2, my = (y1+y2)/2;
      const dx = x2-x1, dy = y2-y1, len = Math.hypot(dx,dy) || 1;
      const cx = mx - dy/len*28, cy = my + dx/len*28;
      return {d: 'M'+x1.toFixed(0)+' '+y1.toFixed(0)+' Q'+cx.toFixed(0)+' '+cy.toFixed(0)+' '+x2.toFixed(0)+' '+y2.toFixed(0),
        color, w, label: label || false, lx: cx.toFixed(0), ly: (cy-5).toFixed(0)};
    };
    const hiveEdges = [
      ...orgPairs.map(([f,t]) => mkEdge(f, t, '#253350', 1, null)),
      ...this.handoffs.map(hh => mkEdge(hh.from, hh.to, hh.color, 1.8, hh.what.split(' ').slice(0,3).join(' '))),
    ];
    const hiveHandoffs = this.handoffs.map(hh => ({...hh}));
    const gates = S.gates.map(g => ({...g, pending: g.state === 'pending', resolved: g.state !== 'pending',
      resColor: g.state === 'approved' ? '#58D68A' : '#F26D6D',
      resolution: g.state === 'approved' ? 'APPROVED BY YOU · LOGGED' : 'REJECTED BY YOU · AGENTS NOTIFIED',
      onApprove: () => this.resolveGate(g.id, true), onReject: () => this.resolveGate(g.id, false)}));
    const byName = n => this.agents.find(a => a.name === n) || this.agents[0];
    const mkNode = a => { const m = this.statusMeta(a.status); return {name: a.name, role: a.role, model: a.model, task: a.task, dot: m.dot, anim: m.anim, onChat: () => this.openChat(a)}; };
    const orgColumns = [
      {mgr: 'Rhea', reports: ['Nova','Scout']},
      {mgr: 'Forge', reports: ['Vector','Pixel']},
      {mgr: 'Ledger', reports: ['Quill','Sentry']},
      {mgr: 'Arc', reports: ['Muse']},
    ].map(c => { const m = mkNode(byName(c.mgr)); return {mgrName: m.name, mgrRole: m.role, mgrModel: m.model, mgrDot: m.dot, mgrAnim: m.anim, mgrChat: m.onChat, reports: c.reports.map(r => mkNode(byName(r)))}; });
    const panelMsgs = this.panelScript.slice(0, S.panelMsgIdx).map(m => ({name: m.n, role: m.r, stance: m.s, text: m.text, stanceFg: m.s === 'FOR' ? '#58D68A' : '#F26D6D'}));
    const deliberating = S.panelMsgIdx < 4;
    const sessions = [
      {title:'Pricing & go-to-market model', summary:'Freemium vs one-time purchase for GeneVault beta.', status: deliberating ? 'LIVE' : 'VERDICT', ts:'13:58', hot:true},
      {title:'Variant annotation store', summary:'DuckDB vs Postgres+pgvector. Verdict ratified by Atlas as ADR-016.', status:'CLOSED', ts:'11:20', hot:false},
      {title:'Beta cohort size', summary:'500 vs 5,000 invites. Panel chose 500; you ratified.', status:'CLOSED', ts:'YESTERDAY', hot:false},
    ].map(s => ({...s, bd: s.hot ? '#A78BFA' : 'transparent',
      chipFg: s.status === 'LIVE' ? '#A78BFA' : s.status === 'VERDICT' ? '#58D68A' : '#5A6785'}));
    const wf = this.workflowDefs[S.wfSel];
    const workflows = this.workflowDefs.map((w, i) => ({...w, onPick: () => this.setState({wfSel: i, wfStagesCustom: null, deployed: false}),
      bd: S.wfSel === i ? '#4FE3C1' : 'transparent', chipFg: w.chip === 'ACTIVE' ? '#58D68A' : '#5A6785'}));
    const stagesSrc = S.wfStagesCustom || wf.stages;
    const kindFg = k => k === 'PANEL' ? '#A78BFA' : k === 'GATE' ? '#F5B14C' : '#4FE3C1';
    const wfStages = stagesSrc.map((st, i) => ({...st, num: String(i+1).padStart(2,'0'), notLast: i < stagesSrc.length - 1,
      numFg: st.gated ? '#F5B14C' : '#4FE3C1', kindFg: kindFg(st.kind),
      owners: st.owners.map(o => ({name: o, dot: this.statusMeta(byName(o).status).dot})),
      onRemove: () => this.setState({wfStagesCustom: stagesSrc.filter((_, j) => j !== i)})}));
    const q = S.searchQ.toLowerCase();
    const projTag = {DECODER:'GeneVault decoder', CAMPAIGN:'Launch campaign', SOC2:'SOC 2'};
    const auditProjects = ['ALL','DECODER','CAMPAIGN','SOC2'].map(p => ({label: p === 'ALL' ? 'ALL PROJECTS' : p,
      onPick: () => this.setState({auditProj: p}), bd: S.auditProj === p ? '#4FE3C1' : 'transparent', fg: S.auditProj === p ? '#4FE3C1' : '#5A6785'}));
    const timelineFiltered = this.timeline
      .filter(t => S.auditProj === 'ALL' || t.proj === S.auditProj)
      .filter(t => !q || (t.actor + t.text + t.kind + (t.artifact||'')).toLowerCase().includes(q));
    const infraScopes = ['GLOBAL','DECODER','CAMPAIGN','SOC2'].map(p => ({label: p,
      onPick: () => this.setState({infraScope: p}), bd: S.infraScope === p ? '#4FE3C1' : 'transparent', fg: S.infraScope === p ? '#4FE3C1' : '#5A6785'}));
    const scoped = S.infraScope !== 'GLOBAL';
    const scale = {DECODER: .62, CAMPAIGN: .18, SOC2: .08}[S.infraScope] || 1;
    const fleetSrc = scoped ? this.fleetData.filter(f => (this.fleetProj[f.name]||[]).includes(S.infraScope)) : this.fleetData;
    const fleet = fleetSrc.map(f => ({...f, load: scoped ? Math.round(f.load * (scale + .3)) : f.load}))
      .map(f => ({...f, loadW: Math.min(f.load, 100) + '%', barColor: f.load > 80 ? '#F5B14C' : '#4FE3C1'}));
    const money = n => '$' + Math.round(n).toLocaleString();
    const budgets = this.budgetData.map(b => {
      const sp = scoped ? b.raw[0] * scale : b.raw[0], cp = scoped ? b.raw[1] * scale : b.raw[1];
      const pct = Math.round(sp / cp * 100);
      return {...b, spent: money(sp), cap: money(cp), w: pct + '%', color: pct > 75 ? '#F5B14C' : '#4FE3C1'};
    });
    const integrations = this.integrationData.map(g => ({...g, dot: g.ok ? '#58D68A' : '#F5B14C'}));
    const missionFilters = tabify(['ALL','RUNNING','GATED','DONE'], S.missionFilter, f => ({missionFilter: f}));
    const stCol = s => s === 'RUNNING' ? '#4FE3C1' : s === 'GATED' ? '#F5B14C' : s === 'DONE' ? '#58D68A' : '#8D9AB5';
    const missionsFiltered = (S.missionFilter === 'ALL' ? S.missions : S.missions.filter(m => m.status === S.missionFilter)).map(m => ({...m,
      stFg: stCol(m.status), pctW: m.pct + '%', barColor: stCol(m.status)}));
    const companies = [
      {name:'GeneVault Inc', desc:'Consumer genomics · 12 agents', status:'LIVE', dot:'#4FE3C1'},
      {name:'Driftwake Labs', desc:'Sleep analytics wearable SaaS', status:'LIVE', dot:'#4FE3C1'},
      {name:'Copperline', desc:'B2B invoice factoring — paused', status:'IDLE', dot:'#3E4D6B'},
    ].map(c => ({...c, onPick: () => this.setState({companyOpen: false})}));
    const chatMsgs = S.chatMsgs.map(m => m.who === 'me'
      ? {text: m.text, align:'flex-end', bg:'#141B2B', bd:'#4FE3C1'}
      : {text: m.text, align:'flex-start', bg:'#0E1422', bd:'#2C3A55'});
    const launchWfs = this.workflowDefs.map(w => ({name: w.name, onPick: () => this.setState({launchWf: w.name}),
      bd: S.launchWf === w.name ? '#4FE3C1' : '#1E2940', fg: S.launchWf === w.name ? '#4FE3C1' : '#8D9AB5'}));
    const launchBudgets = ['$1k','$5k','$20k'].map(b => ({label: b, onPick: () => this.setState({launchBudget: b}),
      bd: S.launchBudget === b ? '#4FE3C1' : '#1E2940', fg: S.launchBudget === b ? '#4FE3C1' : '#8D9AB5'}));
    const settingsTabs = [['github','GitHub'],['sso','SSO & Security'],['members','Members & Access']].map(([id, label]) => ({
      label, onPick: () => this.setState({settingsTab: id}), bd: S.settingsTab === id ? '#4FE3C1' : 'transparent', fg: S.settingsTab === id ? '#E6EDF5' : '#5A6785'}));
    const repos = this.repoData.map(r => ({...r, ciColor: r.ci === 'PASSING' ? '#58D68A' : r.ci === 'RUNNING' ? '#F5B14C' : '#F26D6D'}));
    const ghPermDefs = [
      ['merge','Merge to main','Agents may merge PRs when CI passes'],
      ['deploy','Production deploy','Agents deploy without a human gate'],
      ['secrets','Manage repo secrets','Rotate and write Actions secrets'],
      ['branch','Create branches & PRs','Standard agent working mode'],
    ];
    const ghPerms = ghPermDefs.map(([k, name, desc]) => ({name, desc, ...this.toggle(S.ghPerms[k]),
      onToggle: () => this.setState(s => ({ghPerms: {...s.ghPerms, [k]: !s.ghPerms[k]}}))}));
    const ssoPolDefs = [
      ['enforce','Enforce SSO','Password login disabled for all members'],
      ['mfa','Require MFA at IdP','Assertion must carry MFA claim'],
      ['session','8-hour session limit','Re-authenticate daily'],
      ['jit','Just-in-time provisioning','Create accounts on first SSO login'],
    ];
    const ssoPolicies = ssoPolDefs.map(([k, name, desc]) => ({name, desc, ...this.toggle(S.ssoPols[k]),
      onToggle: () => this.setState(s => ({ssoPols: {...s.ssoPols, [k]: !s.ssoPols[k]}}))}));
    const ssoFields = [
      {label:'SSO URL (ACS)', value:'https://app.boss.dev/sso/saml/acs/genevault'},
      {label:'ENTITY ID', value:'urn:boss:genevault-inc'},
      {label:'IDP ISSUER', value:'http://www.okta.com/exk9f2a8c771gv'},
      {label:'CERTIFICATE', value:'SHA-256 · expires 2027-03-14 · rotate reminder set'},
    ];
    const members = this.memberData.map(m => ({...m, initials: m.name.split(' ').map(x=>x[0]).join('')}));
    return {
      company: 'GeneVault Inc', companyOpen: S.companyOpen, toggleCompany: () => this.setState(s => ({companyOpen: !s.companyOpen})), companies,
      budgetLabel: '$7,074', gateCount: pendingGates.length, showCosts: this.props.showCosts ?? true,
      autonomyMode: this.props.autonomyMode ?? 'Supervised', autonomyDesc: (this.props.autonomyMode ?? 'Supervised') === 'Full auto' ? 'Gates auto-approve under $500.' : 'Spend & deploys pause for you.',
      navItems: nav, clock: S.clock,
      hideImg: e => { e.target.style.display = 'none'; },
      stripProjects, wfScopes, hiveNodes, hiveEdges, hiveHandoffs,
      isHive: S.view === 'hive',
      isMissions: S.view === 'missions', isFactory: S.view === 'factory', isOrg: S.view === 'org', isPanels: S.view === 'panels', isBoss: S.view === 'boss', isTimeline: S.view === 'timeline', isInfra: S.view === 'infra', isSettings: S.view === 'settings',
      isSetGithub: S.settingsTab === 'github', isSetSso: S.settingsTab === 'sso', isSetMembers: S.settingsTab === 'members',
      settingsTabs, repos, ghPerms, ciRows: this.ciData, ssoFields, ssoPolicies,
      members, roles: this.roleData, apiKeys: this.apiKeyData,
      missionFilters, missionsFiltered,
      statusFilters: filters, agentsFiltered, gates, feed: S.feed,
      orgCeo: [mkNode(byName('Atlas'))], orgColumns,
      sessions, panelists: this.panelScript.map(p => ({name: p.n, role: p.r})), panelMsgs,
      panelTyping: deliberating, typingWho: deliberating ? this.panelScript[Math.min(S.panelMsgIdx, 3)].n : '',
      sessionStatus: deliberating ? 'DELIBERATING' : 'VERDICT READY', sessionChipFg: deliberating ? '#A78BFA' : '#58D68A',
      verdictOut: !deliberating, verdictOpen: S.verdictOpen && !S.verdictDecided, verdictDecided: S.verdictDecided, verdictColor: S.verdictNote.startsWith('RATIFIED') ? '#58D68A' : '#F26D6D', verdictNote: S.verdictNote,
      onRatify: () => this.setState({verdictDecided: true, verdictNote: 'RATIFIED BY YOU — MUSE BEGINS FREEMIUM CAMPAIGN'}),
      onVeto: () => this.setState({verdictDecided: true, verdictNote: 'VETOED — PANEL REOPENS WITH YOUR NOTE'}),
      votesFor: S.panelMsgIdx >= 4 ? 3 : Math.max(0, S.panelMsgIdx - 1), votesAgainst: S.panelMsgIdx >= 2 ? 1 : 0, tallyW: S.panelMsgIdx >= 4 ? '75%' : '50%',
      workflows, wfName: wf.name, wfVersion: S.wfStagesCustom ? '1.4-draft' : '1.3', wfDesc: wf.desc, wfStages,
      deployLabel: S.deployed ? 'Deployed ✓' : 'Deploy version', onDeploy: () => this.setState({deployed: true}),
      onAddStage: () => this.setState({wfStagesCustom: [...stagesSrc, {name:'New stage', kind:'SEQUENTIAL', detail:'Describe what happens here and assign owners.', owners:['Rhea'], gated:false}]}),
      onNewWorkflow: () => this.setState({wfSel: 1, wfStagesCustom: null, deployed: false}),
      searchQ: S.searchQ, onSearch: e => this.setState({searchQ: e.target.value}), timelineFiltered,
      fleet, fleetStats: [{val:'32', label:'nodes live', color:'#4FE3C1'}, {val:'+5', label:'scaled 12m', color:'#58D68A'}, {val:'99.98%', label:'uptime 30d', color:'#8D9AB5'}],
      budgets, integrations,
      chatOpen: S.chatOpen, chatName: S.chatWith ? S.chatWith.name : '', chatRole: S.chatWith ? S.chatWith.role : '',
      chatMsgs, chatTyping: S.chatTyping, chatDraft: S.chatDraft,
      onChatInput: e => this.setState({chatDraft: e.target.value}), onChatKey: e => { if (e.key === 'Enter') this.sendChat(); },
      onChatSend: () => this.sendChat(), closeChat: () => this.setState({chatOpen: false}),
      launchOpen: S.launchOpen, openLaunch: () => this.setState({launchOpen: true}), closeLaunch: () => this.setState({launchOpen: false}),
      brief: S.brief, onBrief: e => this.setState({brief: e.target.value}), launchWfs, launchBudgets,
      onLaunch: () => this.setState(s => ({launchOpen: false, view: 'missions', missions: [{id:'m'+(s.missions.length+1), title: (s.brief.trim() ? s.brief.trim().slice(0, 48) : 'New project'), desc: 'Discovery panel convening — Atlas, Nova, Scout scoping the charter.', status:'RUNNING', lead:'Atlas (CEO)', agents:3, steps:1, spend:'$0 spent', stage:'DISCOVERY', pct:2}, ...s.missions]})),
    };
  }
}
