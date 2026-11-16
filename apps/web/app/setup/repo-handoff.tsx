"use client";

/**
 * After repositories are allowed (BOSSAPP-1018): hand the person forward. One primary per
 * allowed repository imports it as a project; the other adds it to the current project.
 * The same block closes the app route, sign-in, and a pasted token.
 */
import React, { useState } from "react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { refusalWords } from "../../lib/github-accounts";
import { multirepo } from "../../lib/multirepo";
import { startProjectApi } from "../../lib/start-project";
import { consoleHref } from "../console-href";

export type HandoffRepo = { full_name: string; id?: string | null };

function projectName(fullName: string): string {
  const tail = fullName.split("/").pop() ?? fullName;
  const words = tail.replace(/[-_.]+/g, " ").trim();
  return words ? words.replace(/\b\w/g, (c) => c.toUpperCase()) : tail;
}

function place(projectId: string, extra: Record<string, string | undefined>): string {
  return consoleHref({ project_id: projectId, new_project: undefined, focus: undefined, ...extra });
}

export function RepoHandoff({ projectId, repos, variant = "setup" }: {
  projectId: string;
  repos: HandoffRepo[];
  variant?: "setup" | "guided";
}) {
  const guarded = useGuardedCall();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  if (!repos.length) return null;
  const primary = variant === "guided" ? "ga-btn ga-btn-primary" : "su-btn su-btn-primary";
  const secondary = variant === "guided" ? "ga-btn" : "su-btn";
  const link = variant === "guided" ? "ga-link" : "su-link";

  const registryId = async (repo: HandoffRepo): Promise<string> => {
    if (repo.id) return repo.id;
    const reg = await multirepo.registry(projectId);
    const hit = reg.items.find((item) => item.full_name.toLowerCase() === repo.full_name.toLowerCase());
    if (!hit) throw new Error(`${repo.full_name} is not in the registry yet.`);
    return hit.id;
  };

  const addHere = async (repo: HandoffRepo) => {
    setBusy(`add:${repo.full_name}`);
    setNote(null);
    try {
      const id = await registryId(repo);
      await guarded(`Add ${repo.full_name} to this project.`, () => multirepo.putProjectRepo(projectId, id, {}));
      setNote(`Added ${repo.full_name}. It shows in the project's Repositories.`);
    } catch (e) {
      setNote(refusalWords(e));
    } finally {
      setBusy(null);
    }
  };

  const importAs = async (repo: HandoffRepo) => {
    setBusy(`import:${repo.full_name}`);
    setNote(null);
    try {
      const id = await registryId(repo);
      const name = projectName(repo.full_name);
      const made = await guarded("Create the project.", () => startProjectApi.create(name));
      await guarded(`Add ${repo.full_name} to the project.`, () => multirepo.putProjectRepo(made.id, id, {}));
      window.location.assign(place(made.id, { area: "project", tab: "settings", section: "repos" }));
    } catch (e) {
      const exists = (e as { exists?: unknown }).exists;
      setNote(exists !== undefined ? `A project called ${projectName(repo.full_name)} exists. Choose another name in Start a project.` : refusalWords(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="su-stack" data-testid="acct-handoff">
      <p data-testid="acct-handoff-where">
        Afterwards this lives in <a className={link} href={consoleHref({ new_project: "1", focus: undefined })}>Start a project</a>
        {", "}<a className={link} href={consoleHref({ new_project: "import", focus: undefined })}>Add from GitHub</a>
        {", or "}<a className={link} href={place(projectId, { area: "project", tab: "settings", section: "repos" })}>the project&apos;s Repositories</a>.
      </p>
      <ul className="su-stack su-checklist">
        {repos.map((repo) => (
          <li key={repo.full_name} className="su-line" data-testid={`acct-handoff-${repo.full_name}`}>
            <span className="su-mono" data-user-text>{repo.full_name}</span>
            <GatedButton permission="project:create" projectId={projectId} className={primary}
                         data-testid={`acct-import-${repo.full_name}`} disabled={busy !== null}
                         onClick={() => void importAs(repo)}>
              {busy === `import:${repo.full_name}` ? "Importing…" : "Import as a project"}
            </GatedButton>
            <GatedButton permission="repository:manage" projectId={projectId} className={secondary}
                         data-testid={`acct-add-${repo.full_name}`} disabled={busy !== null}
                         onClick={() => void addHere(repo)}>
              {busy === `add:${repo.full_name}` ? "Adding…" : "Add to this project"}
            </GatedButton>
          </li>
        ))}
      </ul>
      {note ? <p role="status" data-testid="acct-handoff-note">{note}</p> : null}
    </section>
  );
}
