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
        </form>
      ) : <p role="alert">Console authentication is not configured on this server.</p>}
    </main>
  );
}
