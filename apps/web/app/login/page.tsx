import { consoleAuthConfig, safeNext } from "../../lib/console-session";
import { CopyCommand } from "./copy-command";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ next?: string; error?: string }> };

const SHOW_ON_SERVER = "python3 scripts/install/console_password.py show";
// -t: the script prints only to a real terminal, and ssh running a command allocates none.
const SHOW_OVER_SSH = `ssh -t user@your-server 'cd ~/boss && ${SHOW_ON_SERVER}'`;

export default async function ConsoleLogin({ searchParams }: Props) {
  const params = await searchParams;
  const failed = params.error === "invalid";
  const throttled = params.error === "throttled";
  return (
    <main className="login-page">
      <div className="login-card">
        <p className="login-eyebrow">BOSS</p>
        <h1 className="login-title">Mission Control</h1>
        {consoleAuthConfig() ? (
          <form action="/api/auth/login" method="post" className="login-form">
            <input type="hidden" name="next" value={safeNext(params.next ?? null)} />
            <label htmlFor="console-password" className="login-label">Console password</label>
            <input id="console-password" name="password" type="password" required autoFocus
              autoComplete="current-password" className="input" />
            {failed ? <p role="alert" className="login-error">Incorrect password.</p> : null}
            {throttled ? (
              <p role="alert" className="login-error" data-testid="login-throttled">
                Too many wrong passwords from this address. Wait five minutes, then sign in
                again — the password below is unchanged.
              </p>
            ) : null}
            <button type="submit" className="cap-btn primary login-submit">Sign in</button>

            {/* WHERE THE PASSWORD LIVES, ON THE PAGE THAT ASKS FOR IT — folded away until wanted,
                and opened by itself after a wrong password, when it is needed most. The installer
                prints the command once at the end of a long run; this page names the command,
                never the value. Each command stays on one line and selects in one click. */}
            <details className="login-help" data-testid="console-password-hint" open={failed || throttled}>
              <summary>Where&apos;s my password?</summary>
              <p>
                The installer generated it and kept it on the server where BOSS is installed.
                Print it from the install directory (<code>~/boss</code> by default):
              </p>
              <CopyCommand command={SHOW_ON_SERVER} />
              <p>Or from your own machine, over SSH:</p>
              <CopyCommand command={SHOW_OVER_SSH} />
              <p>
                <a href="https://boss.sean.network/docs/CONSOLE_AUTH/" target="_blank"
                   rel="noreferrer">Console access guide</a>, including how to reset it.
              </p>
            </details>
          </form>
        ) : <p role="alert" className="login-error">Console authentication is not configured on this server.</p>}
      </div>
    </main>
  );
}
