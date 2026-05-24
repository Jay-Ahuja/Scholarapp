# 07 — Review

Step 6 (drafting) writes `Draft` rows to the DB. Step 7 takes those rows and
makes them editable by writing one markdown file per draft, opening them in your
editor, and syncing your edits back to the DB.

Implementation: [scholarapp/modules/review.py](../scholarapp/modules/review.py).

Two entry points:

- `write_drafts_to_disk(run_id) → Path` — materializes every Draft for a run as
  a `.md` file under `<DRAFTS_DIR>/<run_id>/` (default `./drafts/<run_id>/`).
  Updates each `Draft.file_path` to point at the new file. Idempotent.
- `sync_drafts_from_disk(run_id) → SyncReport` — reads every `.md` in that
  directory, applies the editable fields to the DB, returns a typed report
  with counts and warnings.

Two CLI wrappers:

- `scholar review <run_id>` — runs `write_drafts_to_disk`, prints the path,
  opens `$EDITOR` on the directory if it's set.
- `scholar approve <run_id> [--only <slug>]` — flips `status: pending_review`
  to `status: approved` in matching files, then calls
  `sync_drafts_from_disk` and prints the report.

## Markdown format

One file per draft. Annotated example:

```markdown
---
draft_id: 7f3a-4b51-8c92-...        # ← READ-ONLY (used by sync to find the DB row)
professor: Karl Friston             # ← READ-ONLY (display only; edits ignored)
institution: University College London   # ← READ-ONLY
email: k.friston@ucl.ac.uk          # ← READ-ONLY
matched_projects:                   # ← READ-ONLY (the picks from Step 5)
  - title: "Probabilistic segmentation in SPM"
    why: "Both handle partial-volume effects in cortical boundaries."
status: pending_review              # ← EDITABLE (state machine — see below)
updated_at: 2026-05-23T23:30:00+00:00  # ← READ-ONLY (snapshot for conflict detection)
---
Subject: Question on partial-volume handling in SPM   # ← EDITABLE

Dear Prof. Friston,                                   # ← EDITABLE (body, multi-paragraph)

I'm Jay Ahuja, a senior at Carnegie Mellon...

Would you be open to a 30-minute chat in the next few weeks?

Best,
Jay
```

Layout rules:

1. **YAML frontmatter** between the leading `---` and the closing `---`.
2. **First line after frontmatter** is `Subject: <subject line>`.
3. **Blank line**, then the body (multi-paragraph, plain text).
4. Trailing newline.

The format round-trips exactly: read → write → read returns the same content.
You can hand-write a file in this shape and it'll parse — the format is
intentionally human-friendly.

## Editable vs read-only fields

| Field | Status | Reason |
|---|---|---|
| `draft_id` | read-only | DB row key; changing it would orphan your edits |
| `professor` | read-only | Display only; the professor is set at discovery, not by the user |
| `institution` | read-only | Same |
| `email` | read-only | Same |
| `matched_projects` | read-only | The picks come from Step 5; if they're wrong, fix matching, not the file |
| `updated_at` | read-only | Used for conflict detection (see below) |
| `status` | **editable** | The whole point of review — flip to approved/rejected |
| `subject` | **editable** | Tweak the subject before sending |
| `body` | **editable** | Tweak the body before sending |

Why read-only enforcement at all? Two reasons:

- If you re-typed a name and submitted via `scholar approve`, the only way to
  send to the corrected name would be to also re-discover the professor — but
  the underlying OpenAlex record still points at the original person. Better to
  fail loudly than silently send to the wrong address.
- If we accepted edits to all fields, the read-only ones become a forgery surface
  ("the email I drafted to Jane was actually to Joe — look, the file says so")
  that we don't want.

Edits to read-only fields are **ignored with a warning** during sync. The DB is
untouched for those fields.

## Status state machine

```
                    approve / approve --only <slug>
                    or edit file: status: approved
                    ─────────────────────────────────┐
                    │                                ▼
            ┌───────┴────────┐                ┌─────────────┐
            │ pending_review │ ◄────────────► │  approved   │
            └───────┬────────┘                └─────┬───────┘
                    │                               │
                    │ edit file: status: rejected   │ (Step 8 only)
                    ▼                               ▼
            ┌────────────┐               ┌──────────────────────┐
            │  rejected  │ (terminal)    │  sent / send_disabled │ (terminal)
            └────────────┘               └──────────────────────┘
```

Allowed transitions via `sync_drafts_from_disk`:

- `pending_review` → `pending_review` (no-op)
- `pending_review` → `approved`
- `pending_review` → `rejected`
- `approved` → `approved` (no-op)
- `approved` → `pending_review` (changed your mind)

**Rejected** by the sync (raises a report error, no DB change):

- `approved` → `rejected` — go through `pending_review` first if you want this
- Anything → `sent` or `send_disabled` — Step 8 owns these transitions
- `sent` / `send_disabled` / `rejected` → anything else — these are terminal

The error message tells you exactly which transitions ARE allowed from your
current state, so you can recover.

## Sync semantics

**The file is the source of truth at sync time.** Whatever editable values
appear in the file overwrite the DB. The DB's previous values for those fields
are gone.

For read-only fields, the DB is the source of truth. The file's values are
ignored and warnings are emitted.

The sync is **per-file** and **best-effort**: one file failing (invalid YAML,
invalid status transition, missing required field) does not stop the others
from syncing. The `SyncReport` carries the counts:

```python
class SyncReport(BaseModel):
    updated: int          # files where subject or body changed
    unchanged: int        # files where nothing changed
    status_changed: int   # files where status changed (may overlap with `updated`)
    errors: int           # files that failed (parse, transition, missing)
    warnings: list[str]   # human-readable warnings (read-only tampering, conflicts)
    error_messages: list[str]
```

`SyncReport.summary()` formats this for terminal output (used by `scholar
approve`).

## Why files-on-disk (not a TUI or web UI)

- **Your editor is already better than anything we'd build.** Vim / VSCode /
  Sublime / nano / etc. — multi-buffer, syntax, undo, search-replace,
  spell-check, AI suggestions, your keybindings, your color scheme. We get
  all of that for free.
- **Git-friendly.** You can commit the drafts directory if you want to track
  email versions across iterations.
- **Diffable.** `diff <(scholar review old_id)` vs another run shows you
  exactly what the drafter did differently between prompts.
- **No UI to build / no UI to maintain.** Adding a web UI would be a separate
  product. The CLI is the product.
- **Easy to script.** Want to bulk-approve every draft that mentions "PhD"?
  `grep -l PhD drafts/<id>/*.md | xargs sed -i ...` →
  `scholar approve <id>`.

## Where files live + naming

Default location is **the directory you ran `scholar` from**, so files show up in
Finder (macOS hides `~/.scholarapp/`):

```
<cwd>/drafts/
└── <run_id>/
    ├── friston-7f3aabcd.md
    ├── ashburner-12ef34cd.md
    └── landman-9b2c5d10.md
```

Internal pipeline state (resume snapshot, SQLite DB) still lives under
`~/.scholarapp/` — only the editable artifacts are surfaced to the visible
location.

Override with the `DRAFTS_DIR` env var if you want them somewhere specific:

```bash
DRAFTS_DIR=~/Documents/scholarapp-drafts scholar review <run_id>
```

**Slug:** `<lowercase-last-name>-<first-8-chars-of-draft-id>`. Hyphenated
last names preserve the hyphen ("hyman-smith-..."). Multi-word names take the
last token ("Wei Zhang" → "zhang"). Initials and punctuation are stripped.

The slug is just a filename — sync resolves the DB row via the `draft_id`
field in the frontmatter, not the filename. You can rename files if you want.

## Conflict detection

Each file's frontmatter includes an `updated_at` snapshot of the corresponding
`Draft.updated_at` at write time:

```yaml
updated_at: 2026-05-23T23:30:00+00:00
```

On sync, if `Draft.updated_at` in the DB has advanced past this snapshot, that
means **something else modified the draft after the file was written** —
another process, a manual sqlite edit, etc. The sync emits a warning per
affected file but still applies your edits (file remains source of truth).

If you want to keep the DB-side changes, don't sync — re-run `scholar review
<run_id>` first to regenerate the files with the current DB state, then edit
those fresh files.

For a single-user CLI this is mostly defensive plumbing — you'd notice the
warning, decide which side has the right version, and act. Multi-user setups
(if scholarapp ever has one) would need stronger locking.

## CLI usage

### `scholar review <run_id>`

```bash
scholar review 7dd7e616-4229-43bc-833e-7b9d24e1c4a8
# → Wrote drafts to /Users/you/path/to/project/drafts/7dd7e616...
# → (launches $EDITOR on the directory if set)
# → When you're done editing, run `scholar approve <run_id>` to sync your changes.
```

Edit the files. Save and close.

### `scholar approve <run_id>`

Bulk-approves every `pending_review` draft in the directory (sets `status:
approved` in each file), then syncs.

```bash
scholar approve 7dd7e616-...
# → Marked 3 draft file(s) as approved. Syncing to DB...
# → updated: 0
# → unchanged: 0
# → status_changed: 3
# → errors: 0
```

### `scholar approve <run_id> --only <slug>`

Approve a single draft:

```bash
scholar approve 7dd7e616-... --only friston-7f3aabcd
```

### What if I just want to sync edits without approving?

Today `scholar approve` is the only sync command — but if you didn't change any
statuses in the files, the "marked 0 as approved" path still runs a sync and
applies your subject/body edits. The "approve" verb is a bit overloaded; a
future `scholar sync <run_id>` would be a non-status-flipping sibling.

## Extending the format

To add a new field to the markdown file without breaking old files:

1. Add the field to `ParsedDraft` in
   [scholarapp/modules/review.py](../scholarapp/modules/review.py) with a
   safe default (so old files without the field still parse).
2. Add the field to `render_draft_file()`'s `frontmatter` dict.
3. Update `_sync_one_file()` to apply the new field if it's editable, or warn
   on tampering if it's read-only. Add a status-transition rule if it's a
   status-like field.
4. Add a test in `tests/test_review.py` for the new field's round-trip.

Avoid removing or renaming fields — files in user's existing run directories
won't parse. If you must, write a migration that rewrites all files in
`~/.scholarapp/runs/`.
