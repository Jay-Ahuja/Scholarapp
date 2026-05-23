# 02 — Persistence

Single-user SQLite database holding everything Scholarapp learns during a run: the user's request, the discovered professors and their works, the LLM-picked relevance rationales, the drafted emails, and (eventually) a log of every send attempt.

## ER diagram

```
┌───────────────────┐                ┌────────────────────┐
│ runs              │ 1            N │ professors         │
│───────────────────│────────────────│────────────────────│
│ id (uuid, PK)     │                │ id (uuid, PK)      │
│ created_at        │                │ run_id (FK runs)   │
│ status: RunStatus │                │ name               │
│ field             │                │ institution        │
│ goal              │                │ email              │
│ considerations    │                │ openalex_id        │
│ count             │                │ faculty_page_url?  │
│ resume_path       │                │ raw_json?          │
│ template_text     │                └─────────┬──────────┘
│ error?            │                          │ 1
└─────────┬─────────┘                          │
          │ 1                                  │ N
          │                                    ▼
          │                          ┌────────────────────┐        ┌────────────────────┐
          │                          │ projects           │ 1    N │ matched_projects   │
          │                          │────────────────────│────────│────────────────────│
          │                          │ id (int, PK)       │        │ id (int, PK)       │
          │                          │ professor_id (FK)  │        │ professor_id (FK)  │
          │                          │ title              │        │ project_id (FK)    │
          │                          │ url?               │        │ why_relevant       │
          │                          │ year?              │        └────────────────────┘
          │                          │ abstract?          │
          │                          │ raw_json?          │
          │                          └────────────────────┘
          │ N
          ▼
┌─────────────────────┐               ┌──────────────────────┐
│ drafts              │ 1          N  │ send_logs            │
│─────────────────────│───────────────│──────────────────────│
│ id (uuid, PK)       │               │ id (int, PK)         │
│ run_id (FK runs)    │               │ draft_id (FK drafts) │
│ professor_id (FK)   │               │ attempted_at         │
│ subject             │               │ outcome: SendOutcome │
│ body                │               │ error?               │
│ status: DraftStatus │               │ gmail_message_id?    │
│ file_path?          │               └──────────────────────┘
│ updated_at          │
└─────────────────────┘

┌──────────────────────┐
│ resume_cache         │  (standalone — no FKs)
│──────────────────────│
│ sha256 (PK)          │
│ parsed_json          │
│ cached_at            │
└──────────────────────┘
```

`?` denotes nullable. FK columns are indexed.

## Models

All schema lives in [scholarapp/db/models.py](../scholarapp/db/models.py). The Pythonic enums are stored as their `.value` (lowercase strings) so the DB is easy to inspect by hand.

### `Run` — `runs`

One pipeline invocation. Created by `scholar run` (Step 3 onward); read by `scholar list` and `scholar status`.

| Field | Meaning |
|---|---|
| `id` | UUID; surfaces in every other CLI command as `<run_id>` |
| `created_at` | UTC datetime; used for ordering in `scholar list` |
| `status` | `RunStatus` — see lifecycle below |
| `field` | Research field from the user's prompt (e.g., "computational neuroscience") |
| `goal` | End goal from the user's prompt (e.g., "30-min chat") |
| `considerations` | Free-text extra context the user provided |
| `count` | Number of professors requested |
| `resume_path` | Absolute path to the resume PDF saved under `~/.scholarapp/runs/<id>/inputs/` |
| `template_text` | The verbatim cold-email template (stored, not just path-referenced, so the run is reproducible) |
| `error` | Populated only when `status=failed`; the user-facing failure reason |

### `Professor` — `professors`

Populated by Step 4 (discovery).

| Field | Meaning |
|---|---|
| `id` | UUID |
| `run_id` | The Run that discovered this professor (Professors are scoped per-run, not deduped across runs) |
| `name`, `institution`, `email` | Display + send-to fields |
| `openalex_id` | OpenAlex author ID; lets us re-fetch works without another search |
| `faculty_page_url` | Resolved by Tavily; nullable because not every professor has a recoverable page |
| `raw_json` | Full OpenAlex author payload, for debugging and downstream reprocessing |

### `Project` — `projects`

Populated by Step 4. The full pool of recent works fetched per professor.

| Field | Meaning |
|---|---|
| `id` | Autoincrement integer (not surfaced to users) |
| `professor_id` | FK to professors |
| `title`, `url`, `year` | Display fields |
| `abstract` | Optional — many OpenAlex records have one |
| `raw_json` | Full OpenAlex work payload |

### `MatchedProject` — `matched_projects`

Populated by Step 5 (matching). The 2–3 projects per professor that the LLM judged most relevant to the user's interests.

| Field | Meaning |
|---|---|
| `id` | Autoincrement |
| `professor_id` | The professor; redundant with `project.professor_id` but indexed for cheap lookup |
| `project_id` | FK to projects |
| `why_relevant` | One-sentence rationale from the matching LLM call; fed into the drafting prompt |

### `Draft` — `drafts`

Populated by Step 6 (drafting). Mutated by Step 7 (review sync) and Step 8 (delivery).

| Field | Meaning |
|---|---|
| `id` | UUID; appears in filenames as `<lastname>-<id[:8]>.md` |
| `run_id` | FK to runs |
| `professor_id` | FK to professors |
| `subject`, `body` | The actual email content |
| `status` | `DraftStatus` — see lifecycle below |
| `file_path` | Absolute path to the on-disk markdown; nullable until Step 7 writes it |
| `updated_at` | Refreshed on every write, used for conflict detection in Step 7 |

### `SendLog` — `send_logs`

Populated by Step 8. One row per send *attempt*, including attempts that were intercepted by the SEND_ENABLED gate. This is the audit trail.

| Field | Meaning |
|---|---|
| `id` | Autoincrement |
| `draft_id` | FK to drafts |
| `attempted_at` | UTC datetime; used for the per-day send cap |
| `outcome` | `SendOutcome` — `sent`, `send_disabled`, or `error` |
| `error` | Error message if `outcome=error` |
| `gmail_message_id` | Gmail's returned message id if `outcome=sent` |

### `ResumeCache` — `resume_cache`

Content-hash memo of parsed resumes. Standalone (no FKs) — multiple Runs can hit
the same cache entry. See [docs/03-ingestion.md](03-ingestion.md#resume-parse-cache-content-hash).

| Field | Meaning |
|---|---|
| `sha256` | SHA-256 of the PDF bytes, primary key |
| `parsed_json` | The `ResumeData` JSON; rebuilt via `ResumeData.model_validate(...)` on hit |
| `cached_at` | When the entry was written; informational only — there's no TTL |

## Status enums and lifecycles

### `RunStatus`

```
                       ┌──> failed (sink; populated alongside error)
                       │
pending ─> parsing ─> discovering ─> matching ─> drafting ─> review ─> done
```

- `pending` is the initial state at row creation.
- The pipeline advances through `parsing`, `discovering`, `matching`, `drafting`, `review` (drafts written to disk).
- `done` is set after the user runs `scholar send` (or `scholar approve` if you treat review as terminal).
- `failed` is reachable from any stage; the corresponding `error` field is populated with a user-readable reason.

### `DraftStatus`

```
                     ┌──> rejected (terminal)
                     │
pending_review ──────┼──> approved ─┬──> sent          (Step 8, SEND_ENABLED=true)
       ▲             │              │
       │             │              └──> send_disabled (Step 8, SEND_ENABLED=false)
       └─────────────┘
        user changes
        their mind
```

- `pending_review` — Step 6 just wrote the draft; user hasn't looked yet.
- `approved` — user set this in the markdown file (Step 7) or via `scholar approve`.
- `rejected` — user dropped this draft from the run.
- `sent` — Gmail accepted the message and returned a `gmail_message_id`.
- `send_disabled` — `scholar send` was invoked while `SEND_ENABLED=false`; the SendLog is written but the draft status is **not** changed by the gated path. (Set this status only when sending is actually enabled and the gate intercepts a single draft for some other reason — current code logs and leaves status untouched. See Step 8 doc when written.)
- `approved → pending_review` is allowed (Step 7) so the user can pull a draft back from the approval queue.
- `sent → anything` is rejected by the Step 7 sync.

### `SendOutcome`

| Value | Meaning |
|---|---|
| `sent` | Gmail accepted the message |
| `send_disabled` | `SEND_ENABLED=false` blocked the send |
| `error` | Gmail returned an error; see the `error` field |

## Common queries

These are the patterns each pipeline module needs. Helpers live in [scholarapp/db/repo.py](../scholarapp/db/repo.py).

```python
from scholarapp.db import repo
from scholarapp.db.models import DraftStatus
from scholarapp.db.session import get_session

# All pending_review drafts for a run
with get_session() as s:
    pending = repo.list_drafts_for_run_by_status(s, run_id, DraftStatus.PENDING_REVIEW)

# The latest run (newest first ordering)
with get_session() as s:
    latest = repo.list_runs(s)[0] if (runs := repo.list_runs(s)) else None
    # or: next(iter(repo.list_runs(s)), None)

# Per-draft status table for a run (drives `scholar status`)
with get_session() as s:
    drafts = repo.list_drafts_for_run(s, run_id)
    profs = {p.id: p for p in repo.list_professors_for_run(s, run_id)}
    rows = [(profs[d.professor_id].name, d.status.value, d.file_path) for d in drafts]

# Count drafts by status — no helper today; SA one-liner:
from sqlalchemy import func, select
from scholarapp.db.models import Draft

with get_session() as s:
    stmt = (
        select(Draft.status, func.count())
        .where(Draft.run_id == run_id)
        .group_by(Draft.status)
    )
    counts = dict(s.execute(stmt).all())
```

If a query is being copy/pasted across modules, lift it into `repo.py`. Until then, inline it — `repo.py` stays minimal.

## Where the DB lives + how to inspect

- File: `$DATA_DIR/scholar.db` (default `~/.scholarapp/scholar.db`).
- Created lazily on the first call to `get_session()` (via `init_db()`).
- Tables created idempotently with `Base.metadata.create_all` — no Alembic, no migrations.

```bash
sqlite3 ~/.scholarapp/scholar.db '.tables'
# matched_projects  professors  send_logs
# projects          runs        drafts

sqlite3 ~/.scholarapp/scholar.db 'select id, status, field, count from runs;'
sqlite3 ~/.scholarapp/scholar.db -header -column \
    'select status, count(*) from drafts group by status;'
```

For interactive exploration:

```bash
sqlite3 ~/.scholarapp/scholar.db
sqlite> .mode column
sqlite> .headers on
sqlite> select * from runs order by created_at desc limit 5;
```

## Why SQLite (and what we'd change for multi-user)

SQLite was picked because:

- **Single-user CLI.** No concurrent writers; one process per `scholar` invocation.
- **Zero setup.** The DB file is created on first use; no daemon, no auth.
- **Easy inspection.** `sqlite3 ~/.scholarapp/scholar.db` and you're querying.
- **Trivial backups.** Copy the file.

To go multi-user (hosted Scholarapp), you'd need:

1. **Postgres** for concurrent writers and stronger types. `create_engine("postgresql+psycopg://…")` and that side of SQLAlchemy is unchanged.
2. **Alembic** for migrations. Pre-v1 we drop and recreate; in production you can't.
3. **A `user_id` column** on `Run`, indexed; every repo query takes it as a filter. Per-user data dirs (`~/.scholarapp/users/<user_id>/runs/...`) for the markdown drafts.
4. **OAuth credentials per user** rather than a single `credentials.json`. Likely encrypted at rest.
5. **A real session model** (login, cookies, CSRF) since the CLI assumption breaks down.

None of that is on the roadmap; this list exists so the next person doesn't have to re-derive it.

## Schema-change policy (pre-v1)

The schema is not yet stable. **Adding a non-nullable column or renaming an existing one will not migrate existing data.** During development:

```bash
rm ~/.scholarapp/scholar.db
scholar init       # not strictly required; next session call recreates the file
scholar run        # starts fresh
```

When the app reaches v1 we will:

1. Pin the current schema as `revision 0` with Alembic.
2. From then on, every schema change ships with an Alembic migration and a `scholar migrate` command.
3. The `delete-the-db` workflow becomes destructive only, not part of normal upgrades.
