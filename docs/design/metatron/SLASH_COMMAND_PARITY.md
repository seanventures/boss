# Metatron Rust CLI Slash Command Parity Table (`MT-2b`)

Every slash command in the legacy Python client (`scripts/metatron_tui.py`) has a first-class Rust equivalent in `metatron/crates/metatron-tui/src/commands.rs`, verified by the unit test `every_python_slash_command_is_present_in_rust_catalog`.

## Python TUI Parity Commands

| Command | Aliases / Shortcuts | Python TUI (`metatron_tui.py`) | Rust TUI (`metatron-tui`) | Status |
| --- | --- | --- | --- | --- |
| `/help` | `/?`, `help` | Show commands and keys | `ParsedCommand::Help` | Parity |
| `/agent [KEY\|auto]` | `/agents` | Pick or switch agent | `ParsedCommand::Agent` + picker | Parity |
| `/project [ID]` | — | Pick or switch project | `ParsedCommand::Project` | Parity |
| `/projects` | — | List visible projects | `ParsedCommand::Projects` | Parity |
| `/model [ENGINE[:MODEL]\|auto]` | `/models` | Show or set chat model | `ParsedCommand::Model` | Parity |
| `/status` | — | Connection checklist | `ParsedCommand::Status` (adds policy + sandbox status) | Parity+ |
| `/pick [N]` | bare `N` | Run numbered offer | `ParsedCommand::Pick` | Parity |
| `/confirm` | — | Confirm pending plan | `ParsedCommand::Confirm` | Parity |
| `/expand` | — | Expand last reply's tool calls | `ParsedCommand::Expand` | Parity |
| `/new` | — | Start a new conversation | `ParsedCommand::New` | Parity |
| `/thread [ID]` | — | Read server thread | `ParsedCommand::Thread` | Parity |
| `/tasks [PAGE]` | — | List tasks (25/page) | `ParsedCommand::Tasks` | Parity |
| `/task ID \| create KEY TITLE` | — | Show or create task | `ParsedCommand::TaskShow` / `TaskCreate` | Parity |
| `/decisions [open\|all] [PAGE]` | — | Read decision inbox | `ParsedCommand::Decisions` | Parity |
| `/decision ID \| approve\|reject ID NOTE` | — | Inspect or act on decision | `ParsedCommand::DecisionShow` / `DecisionAct` | Parity |
| `/runs [STATUS]` | — | List workflow runs | `ParsedCommand::Runs` | Parity |
| `/run ID` | — | Show one run's steps | `ParsedCommand::Run { follow: false }` | Parity |
| `/follow ID` | — | Follow run steps live | `ParsedCommand::Run { follow: true }` | Parity |
| `/alerts [firing\|resolved\|all]` | — | List alerts across apps | `ParsedCommand::Alerts` | Parity |
| `/goals` | — | Captain goals | `ParsedCommand::Goals` | Parity |
| `/watch` | — | Captain watch tick | `ParsedCommand::Watch` | Parity |
| `/overwatch` | `/ow` | Captain overwatch loop | `ParsedCommand::Overwatch` | Parity |
| `/drive [auto\|go]` | — | Captain propose/apply moves | `ParsedCommand::Drive` | Parity |
| `/go` | — | Captain drive auto | `ParsedCommand::Drive { auto: true }` | Parity |
| `/ops [TERM] [--page N]` | — | Search live API operations | `ParsedCommand::Ops` | Parity |
| `/describe OPERATION_ID` | — | Inspect operation schema | `ParsedCommand::Describe` | Parity |
| `/op OPERATION_ID [JSON]` | — | Invoke operation by ID | `ParsedCommand::Op` | Parity |
| `/api [TERM \| N \| run N \| METHOD /v1/...]` | — | Browse or call `/v1/` routes | `ParsedCommand::ApiBrowse` / `ApiShow` / `ApiRun` / `ApiRaw` | Parity |
| `/coverage` | — | OpenAPI vs route catalog | `ParsedCommand::Coverage` | Parity |
| `/clear` | — | Clear transcript | `ParsedCommand::Clear` | Parity |
| `/quit` | `/exit`, `/q` | Exit Metatron | `ParsedCommand::Quit` | Parity |

## New Rust Operator Commands (`MT-2`, `MT-7`, `MT-3`)

| Command | Purpose | Ticket |
| --- | --- | --- |
| `/resume [SESSION_ID]` | Open local session JSONL picker or resume a session | `BOSSAPP-1369` (`MT-2`) |
| `/fork` | Fork current local session JSONL into a new branch | `BOSSAPP-1369` (`MT-2`) |
| `/compact [KEEP]` | Compact older session items into a summary entry | `BOSSAPP-1369` (`MT-2`) |
| `/copy` | Copy the last assistant reply | `BOSSAPP-1369` (`MT-2`) |
| `/export [PATH]` | Export the local session transcript as Markdown | `BOSSAPP-1369` (`MT-2`) |
| `/diff` | Display read-only local `git diff` | `BOSSAPP-1369` (`MT-2`) |
| `/mode [MODE]` | Show or tighten approval mode (`read-only`, `ask`, `risky-only`, `act-on-my-behalf`) | `BOSSAPP-1375` (`MT-7`) |
| `/grants [revoke ID]` | List or revoke `act-on-my-behalf` TTL grants | `BOSSAPP-1375` (`MT-7`) |
| `/policy` | Inspect verified Ed25519 BOSS policy bundle status | `BOSSAPP-1375` (`MT-7`) |
| `/hosts` | List BOSS registered hosts and `~/.ssh/config` hosts (with vault origin) | `BOSSAPP-1370` (`MT-3`) |
