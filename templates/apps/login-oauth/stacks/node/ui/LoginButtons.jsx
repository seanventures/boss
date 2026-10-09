// The login buttons and the signed-in header for the React/Vite scaffold. Adapt the markup to
// the app's design tokens; keep the behaviour: providers come from /auth/providers, sign-in is a
// full-page navigation (never fetch), sign-out is a POST carrying the CSRF token from /api/me.
import { useEffect, useState } from "react";

export function useCurrentUser() {
  const [state, setState] = useState({ loading: true, user: null });
  useEffect(() => {
    fetch("/api/me", { credentials: "same-origin" })
      .then((r) => (r.ok ? r.json() : null))
      .then((user) => setState({ loading: false, user }))
      .catch(() => setState({ loading: false, user: null }));
  }, []);
  return state;
}

export function LoginButtons({ returnTo = window.location.pathname }) {
  const [providers, setProviders] = useState([]);
  useEffect(() => {
    fetch("/auth/providers").then((r) => r.json()).then((b) => setProviders(b.providers || []));
  }, []);
  return (
    <div role="group" aria-label="Sign in">
      {providers.map((p) => (
        <a key={p.id} className="btn" href={`${p.login_url}?return_to=${encodeURIComponent(returnTo)}`}>
          Sign in with {p.name}
        </a>
      ))}
    </div>
  );
}

export function AccountHeader() {
  const { loading, user } = useCurrentUser();
  if (loading) return null;
  if (!user) return <LoginButtons />;
  async function signOut() {
    await fetch("/auth/logout", { method: "POST", headers: { "X-CSRF-Token": user.csrf_token } });
    window.location.assign("/");
  }
  return (
    <div className="account">
      {user.avatar && <img src={user.avatar} alt="" width={24} height={24} />}
      <span>{user.name || user.email}</span>
      <button type="button" onClick={signOut}>Sign out</button>
    </div>
  );
}
