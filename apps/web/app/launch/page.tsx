import styles from "./launch.module.css";

const workflow = [
  ["01", "Intent", "One directive enters a governed work queue."],
  ["02", "Evidence", "Roles, tests, and adversarial review make the case."],
  ["03", "Decision", "A human approval is recorded before irreversible work."],
  ["04", "Receipt", "Every consequential action leaves an inspectable trail."],
];

export default function LaunchPage() {
  return (
    <main className={styles.page}>
      <section className={styles.hero}>
        <div className={styles.grid} aria-hidden="true" />
        <nav className={styles.nav} aria-label="Launch navigation">
          <a className={styles.mark} href="#top" aria-label="BOSS launch page">
            BOSS<span>™</span>
          </a>
          <a className={styles.navLink} href="#install">Install the engine ↗</a>
        </nav>

        <div id="top" className={styles.heroContent}>
          <p className={styles.kicker}>THE OPERATING SYSTEM FOR AI LABOR</p>
          <h1>Make AI work<br />accountable.</h1>
          <p className={styles.lede}>
            BOSS turns a directive into governed execution: defined roles, explicit
            approvals, budget controls, durable receipts, and evidence you can inspect.
          </p>
          <div className={styles.actions}>
            <a className={styles.primary} href="#install">Run BOSS locally <span>↓</span></a>
            <a className={styles.secondary} href="#workflow">See the control loop</a>
          </div>
        </div>

        <div className={styles.signal} aria-label="Illustration of governed work moving through BOSS">
          <div className={styles.signalHead}>
            <span>LIVE CONTROL PLANE</span><span>01: GOV</span>
          </div>
          <svg viewBox="0 0 760 250" role="img" aria-label="Intent progresses through proof and approval to a receipt">
            <path className={styles.trace} d="M24 184 C105 184 102 53 190 53 S275 194 357 194 S440 86 528 86 S609 155 736 155" />
            <circle className={styles.pulse} cx="190" cy="53" r="8" />
            <circle className={styles.pulse} cx="357" cy="194" r="8" />
            <circle className={styles.pulse} cx="528" cy="86" r="8" />
            <circle className={styles.pulse} cx="736" cy="155" r="8" />
          </svg>
          <div className={styles.signalLabels}><span>DIRECTIVE</span><span>CHECK</span><span>APPROVAL</span><span>RECEIPT</span></div>
        </div>
      </section>

      <section id="workflow" className={styles.loop}>
        <div className={styles.sectionIntro}>
          <p className={styles.kicker}>THE CONTROL LOOP</p>
          <h2>More than a model<br />with a to-do list.</h2>
        </div>
        <div className={styles.workflow}>
          {workflow.map(([number, title, body]) => (
            <article key={number} className={styles.workflowItem}>
              <span>{number}</span><h3>{title}</h3><p>{body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className={styles.proof}>
        <div>
          <p className={styles.kicker}>BUILT FOR THE REAL WORK</p>
          <h2>Control the work<br />without slowing it down.</h2>
        </div>
        <ul className={styles.proofList}>
          <li><b>Self-hosted engine</b><span>Run the OSS engine with your own Postgres workspace.</span></li>
          <li><b>Local model lanes</b><span>Bring Ollama, vLLM, or any OpenAI-compatible endpoint.</span></li>
          <li><b>MCP-native</b><span>Use Claude, Cursor, or another MCP client as a governed interface.</span></li>
          <li><b>Evidence by default</b><span>Receipts, budget reservations, and approval records stay inspectable.</span></li>
        </ul>
      </section>

      <section id="install" className={styles.install}>
        <p className={styles.kicker}>START WITH THE ENGINE</p>
        <h2>Bring your own model.<br />Keep your own evidence.</h2>
        <div className={styles.command}>
          <span>$</span><code>docker build -f engine/Dockerfile -t boss-engine .</code>
        </div>
        <p>Connect a self-hosted Postgres workspace, bootstrap once, then drive BOSS from the CLI or MCP.</p>
        <a className={styles.primary} href="/">Open Mission Control <span>→</span></a>
      </section>

      <footer className={styles.footer}>
        <span>BOSS / GOVERNED AI LABOR</span><span>ENGINE + CONTROL PLANE</span><span>2026</span>
      </footer>
    </main>
  );
}
