import { consoleAuthConfig, safeNext } from "../../lib/console-session";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ next?: string; error?: string }> };

export default async function ConsoleLogin({ searchParams }: Props) {
  const params = await searchParams;
  return (
    <main style={{ maxWidth: 420, margin: "12vh auto", padding: "2rem" }}>
      <h1>BOSS Mission Control</h1>
      {consoleAuthConfig() ? (
        <form action="/api/auth/login" method="post">
          <input type="hidden" name="next" value={safeNext(params.next ?? null)} />
          <label htmlFor="console-password">Console password</label>
          <input id="console-password" name="password" type="password" required
            autoComplete="current-password" style={{ display: "block", width: "100%", margin: "1rem 0" }} />
          {params.error === "invalid" ? <p role="alert">Incorrect password.</p> : null}
          <button type="submit">Sign in</button>
          {/* WHERE THE PASSWORD LIVES, ON THE PAGE THAT ASKS FOR IT. The installer prints the
              command once, at the end of a long run, and this page is where somebody who missed
              it is stuck. It is a secret, so the page names the command, never the value. */}
          <div data-testid="console-password-hint"
               style={{ marginTop: "2rem", fontSize: "0.9rem", opacity: 0.8, lineHeight: 1.5 }}>
            <p style={{ margin: "0 0 0.5rem" }}>
              The installer generated this password and kept it on the server where BOSS is
              installed. To see it, run this in the install directory on that server
              (<code>~/boss</code> by default):
            </p>
            <pre style={{ whiteSpace: "pre-wrap", margin: "0 0 0.5rem" }}><code>python3 scripts/install/console_password.py show</code></pre>
            <p style={{ margin: "0 0 0.5rem" }}>From another machine, over SSH:</p>
            <pre style={{ whiteSpace: "pre-wrap", margin: "0 0 0.5rem" }}><code>{"ssh user@your-server 'cd ~/boss && python3 scripts/install/console_password.py show'"}</code></pre>
            <p style={{ margin: 0 }}>
              <a href="https://boss.sean.network/docs/CONSOLE_AUTH/" target="_blank"
                 rel="noreferrer">Console access guide</a>, including how to reset it.
            </p>
          </div>
        </form>
      ) : <p role="alert">Console authentication is not configured on this server.</p>}
    </main>
  );
}
