/** Who did something, in words a person reads. Never an id.
 *
 *  "Stopped by usr_kdzhawzy7mxmp5hpb3bqlgqojs", "Approved by ceo", "set by usr_…": every screen
 *  that named an actor printed the id it was given. A name the server sent wins; a word that is
 *  already a name ("BOSS", "workflow", a role) passes through; an opaque id never does. */

const ID = /^(usr|agt|svc|sa|prn)_[a-z0-9]{6,}$/i;

export function looksLikeAnId(value: string | null | undefined): boolean {
  return ID.test(String(value ?? "").trim());
}

export function actorWords(actor: string | null | undefined, name?: string | null,
                           viewer?: string | null): string {
  const id = String(actor ?? "").trim();
  if (viewer && id && id === viewer) return "you";
  const named = String(name ?? "").trim();
  if (named && !looksLikeAnId(named)) return named;
  if (!id) return "someone";
  if (id === "ceo") return "the owner";
  if (id === "auto_approval") return "automatic approval";
  if (id.startsWith("policy:")) return "a deploy rule";
  if (!looksLikeAnId(id)) return id;
  return id.toLowerCase().startsWith("agt_") ? "an agent" : "a person";
}
