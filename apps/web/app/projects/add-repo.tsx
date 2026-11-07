"use client";

/**
 * ADD REPO ON A PROJECT (06-01 Repo & knowledge › Repositories "+ Add repo", 13-23 Settings › Repos
 * "+ Add"): today's Import-a-repo picker (start-project/import-repo.tsx RepoPicker — the account
 * chips, the search, each repository with its last push or a tag), in a drawer on the project page.
 * Picking one and pressing Add puts it on THIS project (PUT /v1/projects/{id}/repos/{repo}, the
 * same binding the Knowledge Graph import and Code Tasks › Repositories write); a refusal is said
 * in the drawer in the API's words, with Connect GitHub when GitHub is what is missing.
 * Never the "Add a GitHub account" sheet: adding an account is Repo and models' job (§15).
 */
import React, { useEffect, useMemo, useState } from "react";
import { GithubLogo, Plus, Warning } from "@phosphor-icons/react";

import { GatedButton } from "../../components/gated";
import { useGuardedCall } from "../../components/identity/identity-provider";
import { IdentityError } from "../../lib/identity/api";
import { multirepo, refusalCode, refusalMessage } from "../../lib/multirepo";
import { notifyChanged } from "../changed";
import { consoleHref } from "../console-href";
import { RepoPicker, useRegistry } from "../start-project/import-repo";
import { Drawer, Ico } from "./kit";
import "../start-project/start-project.css";

export function AddRepoDrawer({ projectId, onClose, onAdded }: { projectId: string; onClose: () => void; onAdded?: () => void }) {
  const { registry, loadError, account, setAccount } = useRegistry(projectId);
  const guarded = useGuardedCall();
  const [query, setQuery] = useState("");
  const [picked, setPicked] = useState("");
  const [here, setHere] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ ok: boolean; text: string; connect?: boolean } | null>(null);

  useEffect(() => {
    let alive = true;
    multirepo.projectRepos(projectId).then((v) => {
      if (!alive || !v) return;
      const names = new Set(v.items.map((i) => i.full_name.toLowerCase()));
      const ids = new Set(v.items.map((i) => i.registry_repo_id).filter((x): x is string => !!x));
      for (const r of registry?.items ?? []) if (names.has(r.full_name.toLowerCase())) ids.add(r.id);
      setHere(ids);
    }).catch(() => undefined);
    return () => { alive = false; };
  }, [projectId, registry]);

  const repo = useMemo(() => (registry?.items ?? []).find((r) => r.id === picked) ?? null, [registry, picked]);
  const addAccount = consoleHref({ area: "setup", focus: "repository", new_project: undefined });

  const add = async () => {
    if (!repo || busy) return;
    setBusy(true); setSaid(null);
    try {
      await guarded(`Add ${repo.full_name} to this project.`, () => multirepo.putProjectRepo(projectId, repo.id, {}));
      setSaid({ ok: true, text: `Added ${repo.full_name}. Helm maps it within a minute; it shows here as it does.` });
      setHere((h) => new Set([...h, repo.id]));
      setPicked("");
      notifyChanged("project.knowledge");
      onAdded?.();
    } catch (e) {
      if (e instanceof IdentityError && e.code === "step_up_cancelled") setSaid({ ok: false, text: "Not added: you cancelled the check." });
      else setSaid({ ok: false, text: refusalMessage(e), connect: refusalCode(e) === "github_not_connected" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer title="Add repo" sub="To this project · from the repositories allowed in Repo and models" icon={<Ico icon={GithubLogo} />}
            onClose={onClose} testid="px-add-repo"
            footer={(
              <div className="px-row" style={{ width: "100%" }}>
                <GatedButton permission="repository:manage" projectId={projectId} className="px-btn" data-kind="primary" data-testid="px-add-repo-go"
                             disabled={!repo || busy} title={repo ? `Add ${repo.full_name} to this project` : "Pick a repository first"}
                             onClick={() => void add()}><Plus size={12} aria-hidden /> {busy ? "Adding…" : repo ? `Add ${repo.full_name.split("/")[1]}` : "Add"}</GatedButton>
                <button type="button" className="px-btn" data-kind="ghost" onClick={onClose}>Close</button>
              </div>
            )}>
      <div className="px-add-repo" data-testid="px-add-repo-picker">
        <RepoPicker registry={registry} loadError={loadError} account={account} onAccount={(a) => { setAccount(a); setPicked(""); }}
                    query={query} onQuery={setQuery} picked={picked} onPick={(id) => { setPicked(id); setSaid(null); }}
                    addAccount={addAccount} here={here} testid="px-add-repo" />
        {said ? (
          <p className={said.ok ? "spj-quiet" : "spj-warn"} role={said.ok ? "status" : "alert"} data-testid="px-add-repo-said">
            {said.ok ? null : <Warning size={13} weight="bold" aria-hidden />}
            <span>{said.text}{said.connect ? <> <a className="spj-link" href={addAccount} data-testid="px-add-repo-connect">Connect GitHub ›</a></> : null}</span>
          </p>
        ) : null}
      </div>
    </Drawer>
  );
}
