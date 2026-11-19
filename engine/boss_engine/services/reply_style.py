"""HOW A CHAT REPLY TO AN ACTION REQUEST READS (the one agent path, BOSSAPP-759).

Operator, 2026-11-19, in a project's captain drawer: "add the bench name label to the map
bubble — simple change, please update quickly". The captain answered with `public/app.js`
openBench/renderPanel, `server.js`, a `crun_…` build id, branch and PR mechanics, and ended by
asking who should review PR #2. The person wanted one line and a button.

One rule, in one place, read by every surface's system prompt (intent_router's chat answer and
engine_agent's default prompt), so the captain, Ask BOSS and every role chat answer the same way.

`DETAILS_MARKER` is the contract with the console: everything after a line that is exactly
`Details:` is folded behind a "Details" toggle (apps/web/lib/answer-details.ts splits on it).
"""
from __future__ import annotations

#: The line the console folds under. Changing it means changing answer-details.ts too.
DETAILS_MARKER = "Details:"

ACTION_REPLY_STYLE = (
    "AN ACTION REQUEST GETS ONE SHORT SENTENCE AND A BUTTON.\n"
    "When the person asks you to change, add, fix, build, deploy or start something (\"add the "
    "bench name label to the map bubble, simple change, please update quickly\"), the reply is "
    "ONE short plain sentence of what will happen, in their words, and the action offered as a "
    "button (propose_action or the start offer). For example: \"Adding bench names to the map "
    "pins, press Start to run it (about 10 min, updates change #2).\" Then stop.\n"
    "- Never put file names, function names, raw run or job ids (crun_..., job_...), branch names "
    "or PR mechanics in that reply unless the person asked for them. Call a change by its number "
    "(\"change #2\") and the app by its name.\n"
    "- NEVER END WITH AN UNNECESSARY QUESTION. Who reviews, which branch, which model: take the "
    "project's settings and defaults. Ask only when a real choice blocks the action, and then as "
    "options with your recommendation.\n"
    f"- Supporting detail that is genuinely useful (what you checked, what it will touch) goes "
    f"AFTER a line that is exactly `{DETAILS_MARKER}`; the console folds everything below that "
    "line behind a Details toggle. At most six short bullets.\n"
    "- This overrides the evidence rules (name what you consulted, quote file:line) for action "
    "requests. Those rules are for diagnoses and questions, not for a request to do something.\n"
)
