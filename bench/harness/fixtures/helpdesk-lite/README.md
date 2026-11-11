# HelpDesk (lite)

A small support-ticket service: people open tickets, each ticket is filed under a category
from its words, and agents move tickets between `open` and `closed`.

- `src/classify.js`: picks a category from a ticket's title and description.
- `src/store.js`: the tickets, kept in memory.
- `src/app.js`: the HTTP routes.
- `src/server.js`: starts the service on `$PORT` (default 3000) and `$HOST` (default 0.0.0.0).

Plain Node ESM (Node 20 or newer), no dependencies. `npm test` runs the tests under `node --test`.

## Routes

| Method | Path | Body | Answer |
|---|---|---|---|
| GET | `/health` | | `{"ok": true}` |
| GET | `/tickets` | | `{"tickets": [...]}`, newest first |
| POST | `/tickets` | `{"title", "description"}` | 201 and the ticket; 400 `{"error": "title_required"}` without a title |
| GET | `/tickets/:id` | | the ticket, or 404 |
| PATCH | `/tickets/:id` | `{"status": "open" \| "closed"}` | the ticket; 400 `{"error": "invalid_status"}` |
