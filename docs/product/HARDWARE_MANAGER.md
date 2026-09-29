# Hardware places

Community BOSS installs apps onto a machine. The default machine is the computer where the community installer ran. There is one hardware agent. The installer installs that agent on the local machine. Adding a VM, a bare-metal server, or a machine created from a provider installs the same agent there. A machine is not ready for app installs until its agent is installed. The local machine is the default even before that agent is recorded on its row. The committed installer path does not leave the local row without the agent.

This is not a deployment-host inventory. Writing a row does not open a socket, an SSH session, or a provider API.

## Table

`hardware_places` is declared in `db/schema.sql` and applied by `ensure_schema` in `engine/boss_engine/db.py`, the same path as `deployment_hosts`. There is no second migration system.

| Column | Notes |
|---|---|
| `id` | Primary key, `hp_` prefix |
| `project_id` | Every read and write is scoped to one project, except `mark_place_ready`, whose signature has only the place id |
| `kind` | `local` (the installer machine), `agent` (another machine), or `provider` (an account, not a machine) |
| `name` | For `local`, the installer machine's hostname |
| `provider_key` | Null except for provider accounts: `aws`, `digitalocean`, or `generic` |
| `status` | `ready`, `pending`, or `retired`. `ready` is refused unless the agent is installed |
| `agent_state` | `absent` or `installed`. Every row has one |
| `is_default` | True only for the local machine |
| `endpoint` | Null until a place is marked ready with an endpoint. Storing it does not dial it |
| `labels` | JSON object. The local row stores `hostname`, `arch`, and `os_name` |
| `secret_ref` | Provider accounts only. A reference such as `env:DO_API_TOKEN` or `sr_…`. Never a raw credential |
| `created_by`, `created_at`, `updated_at` | Who created the row, and when it last changed |

Rules enforced by the table and the service:

- Exactly one `local` row per project (`uq_hardware_places_one_local`).
- That local row is the default. No other kind can be the default.
- The local row cannot be retired or deleted.
- `agent_state` is only `absent` or `installed`.
- `status = ready` requires `agent_state = installed`.
- A provider row must have a `provider_key` and a `secret_ref`. Other kinds must have neither.

## Agent

The agent program is `scripts/install/node_agent.sh --local` for the installer machine. The same program is what a later machine runs; this kernel does not ship a second agent. The script body is owned elsewhere. If `scripts/install/node_agent.sh` is missing, the agent stays `absent` and the local place is not marked ready. If the file is present, it is installed only when `sh scripts/install/node_agent.sh --local` exits 0.

`register_agent_place` is another machine, not another agent. It is not the default, and `agent_state` stays `absent` until `enroll_place_agent`. A provider row is an account and can exist before any machine does. A machine created from that account is additional hardware and gets this same agent.

## Functions

```python
def ensure_local_place(connection, *, project_id: str, hostname: str, arch: str, os_name: str, actor: str) -> dict
def list_places(connection, project_id: str) -> list[dict]
def resolve_place(connection, project_id: str, place_id: str | None) -> dict  # None => the local installer machine

def register_agent_place(connection, *, project_id, name, actor, labels=None) -> dict  # pending, not default, agent_state absent
def register_provider_place(connection, *, project_id, name, provider_key, actor, secret_ref, labels=None) -> dict  # account, pending, not default, agent absent
def enroll_place_agent(connection, *, project_id: str, place_id: str | None = None) -> dict  # agent_state installed; None => local machine; default unchanged
def mark_place_ready(connection, *, place_id, endpoint=None, labels=None) -> dict  # requires agent_state installed; never changes the default
def record_community_local_place(connection, *, project_id: str, hostname: str, arch: str, os_name: str, actor: str) -> dict
```

`ensure_local_place` creates the one default local row, or updates its hostname in place. The new row is `pending` with `agent_state=absent`. A second call does not clear an agent that is already installed and does not move the default.

`record_community_local_place` is the installer write. In one transaction it ensures the local row, enrolls the agent, and marks that machine ready. The committed local row is the default and `agent_state=installed`.

`resolve_place(..., None)` returns the local machine. `mark_place_ready` looks up by primary key and refuses a machine whose agent is still absent.

```python
def retire_place(connection, *, project_id: str, place_id: str, actor: str) -> dict
def delete_place(connection, *, project_id: str, place_id: str) -> None
```

Both reject the local machine.

`secret_ref` must look like a reference (`env:NAME`, `infisical:path`, `vault:name`, `database:name`, or an id beginning with `sr_`, `sec_`, or `secret_`). Values that look like raw credentials are rejected, and the rejected value is not copied into the error.

## When the local machine is recorded

After the community seed succeeds, `install.sh` reads `BOSS_DEFAULT_PROJECT_ID` (the seeded project, `boss-dogfood` unless an operator changed it). It runs `sh scripts/install/node_agent.sh --local` on the installer machine, then calls `scripts/install/ensure_local_place.py` inside the API container with `BOSS_PLACE_AGENT_STATE=installed`. Hostname, architecture, and OS are collected on the installer machine and passed in with `BOSS_PLACE_*`.

That is the path this tree implements. The hook does not invent a project. If the env file has no project id, or the `projects` row is not there yet, the script exits 0 and writes nothing. Call `record_community_local_place` again once the project exists; a later call updates hostname, architecture, and OS on the same default row and leaves the agent installed.

If the agent script is missing or exits non-zero, the installer does not write a ready local row. A failure to record the place is printed and does not abort an otherwise healthy install.

## Out of scope

Routes, OpenAPI, the console, deployment-host write policy, and provider API calls are not part of this kernel. The body of `scripts/install/node_agent.sh` is not part of this kernel.
