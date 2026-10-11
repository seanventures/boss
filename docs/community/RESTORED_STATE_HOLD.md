# Restored state stays held

Restoring a backup can remove local evidence of a provider action that already happened. A completed receipt, pending claim or held budget recorded after the snapshot can disappear while the provider still retains the effect. A matching identity key does not make replay safe.

Current recovery tools leave restored application startup held. The hold lives on its own volume outside Postgres and the identity vault. API, worker, egress and MCP mount it read-only and refuse startup before application imports. API checks before sandbox initialization, key ownership repair or build-service password creation. MCP receives no identity material. A held, unreadable, malformed, oversized or symlink marker refuses startup; an unrecognized recovery directory also refuses. An empty fresh-install recovery volume permits ordinary startup.

## Recover local data

Use current `scripts/install/restore.sh` against a new project and a clean checkout at the archive's exact revision. Archive checksums, complete manifest, cached immutable database/API image identities and key pairing must pass. Normal recovery additionally checks the guarded image interface, source revision and exact guard/entrypoint file hashes before creating target data resources. It never builds or pulls an image.

Run the current reviewed recovery tool from its current checkout. The `--dir` argument names a separate, fresh checkout matching the archive's revision. Do not run the old archive's restore script; older tools do not provide this startup hold.

```bash
./scripts/install/restore.sh --backup /absolute/path/to/private-backup \
  --dir /absolute/path/to/matching-fresh-checkout --project boss-recovered
```

The base Compose and saved image override retain the immutable pins and independent recovery mount. Omitting the override does not remove the hold. The verified archived API/Python image is used for the four held backend roles with explicit commands, preserving their existing users, capabilities and mounts. This is a held-startup contract, not a claim of general image interchangeability, readiness or automatic recovery.

## Recover a pre-guard archive offline

RC8 and other archives whose source/image pair lacks this guard refuse normal recovery before target data resources. Explicit offline mode restores local data while persisting a DB-only base Compose:

```bash
./scripts/install/restore.sh --backup /absolute/path/to/private-backup \
  --dir /absolute/path/to/matching-fresh-checkout --project boss-offline \
  --offline-data-only
```

The target base declares only the database, even without the image override. The archived static Compose is retained separately in `.boss-archived-compose.yml`. Identity and artifact archives are extracted by network-disabled utility containers. No API, worker, egress or MCP is started. The recovery workspace differs from the archived Git source by design; reinstalling old source, replacing the DB-only base or overriding the guard is outside this recovery procedure.

## Reconcile before any restart

The marker is written before usable target startup configuration is published or any data is restored, and retained on failure. The complete canonical `.env` appears atomically only after the hold exists; offline base replacement also precedes that environment publication. This prevents an ordinary concurrent base-Compose launch from admitting an empty recovery mount. Owned volumes carry the reviewed project/logical-volume labels. A competing marker writer refuses instead of replacing another attempt's hold. Preserve it, the original stack and provider evidence. There is no generic resume switch, acknowledgment endpoint or supplied marker-deletion command. Restore prints no application-start command.

Inspect the snapshot's jobs, decisions, receipts and holds together with actual provider acceptance/operation records for the interval the snapshot may have lost. Treat missing or uncertain evidence as unresolved liability. Follow [Provider recovery](PROVIDER_RECOVERY.md) for provider-specific reconciliation and an operator-reviewed exception or migration; local history alone cannot prove that a remote action did not happen.

Offline inspection can check archive completeness, database/key metadata, encrypted data using controlled local code and recorded artifacts/history. It does not prove actual Owner password/MFA sign-in through the installed API, installed `/health`, live provider recovery, or permission to resume. Those require separately reviewed runtime, identity and provider qualification after reconciliation. Source/inert-process tests prove the hold logic; installed image and restored-volume qualification are separate evidence.
