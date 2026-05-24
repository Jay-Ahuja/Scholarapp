# 01 — Scaffold

This doc is for someone seeing Scholarapp for the first time. It covers what the app does, how the codebase is laid out, the stack we chose, the conventions to follow, and where to find more detailed docs for each pipeline stage.

## What Scholarapp is

Scholarapp is a single-user Python CLI that drafts personalized cold emails from you to professors with the goal of starting a real conversation (a 30-minute chat, a lab position, networking — whichever you specify).

The user provides three things:

1. A **resume PDF**.
2. A **prompt** answering four questions: how many professors to email, what field, what's the end goal, and any other considerations.
3. A **cold-email template** in their own voice.

Scholarapp then:

1. Parses the resume + prompt into structured data.
2. Searches academic data sources for relevant professors and their recent work.
3. Matches a few of each professor's projects to the user's interests.
4. Drafts a personalized email per professor using the template + matched material.
5. Writes the drafts as editable markdown files for the user to review.
6. (Eventually) sends approved drafts via the user's own Gmail account.

Step 6 is **disabled today** — the code path exists but is gated behind `SEND_ENABLED=false`. See [08-delivery.md](08-delivery.md) (added in Step 8) for the procedure to enable it.

## The 7-module pipeline

Scholarapp is organized into seven modules. The first six are pipeline stages; the seventh (`persistence`) underlies everything.

| # | Module | Role | Implemented in |
|---|---|---|---|
| 1 | `ingestion` | Parse resume PDF + prompt + template → structured records | Step 3 |
| 2 | `discovery` | Find N professors in the requested field, resolve emails | Step 4 |
| 3 | `matching` | For each professor, pick 2–3 recent works most relevant to the user | Step 5 |
| 4 | `drafting` | Generate a personalized email per (professor, matched works) | Step 6 |
| 5 | `review` | Write drafts as editable markdown; sync edits back to the DB | Step 7 |
| 6 | `delivery` | Send approved drafts via Gmail (gated) | Step 8 |
| 7 | `persistence` | SQLAlchemy models for Run, Professor, Project, Draft, SendLog | Step 2 |

`scholar run` chains modules 1–5. Module 6 is invoked separately via `scholar send`. Module 7 is shared infrastructure used by all of them.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Modern typing, ergonomic async, native fit for LLM + data work |
| CLI framework | Typer | Type-hint-driven commands, sensible defaults, good help output |
| Persistence | SQLite + SQLAlchemy 2.x | Plenty for a single-user local app; trivial to inspect via `sqlite3` |
| LLM | Anthropic SDK with `claude-sonnet-4-6` | Strong structured output, native PDF input, prompt caching |
| HTTP | `httpx` | Async-first; pairs cleanly with concurrent OpenAlex/Tavily fan-out |
| Academic data | OpenAlex (free, no key) | Real API for author/works data; replaces fragile Google Scholar scraping |
| Web search | Tavily | Resolves faculty pages + emails that OpenAlex lacks |
| Email send | Gmail API via `google-auth*` + `google-api-python-client` | Lets email come from the user's real address (much higher reply rate than transactional SMTP) |
| Config loading | `python-dotenv` | Loads `.env` at startup; standard for CLI tools |
| YAML | `pyyaml` | Used in Step 7 for draft-file frontmatter |
| Validation | Pydantic 2 | Used by every module for I/O dataclasses and structured-output schemas |
| Tests | pytest | Default choice; quick to wire up |
| Lint | ruff | One tool for lint + format; configured in `pyproject.toml` |

## File and directory layout

Files marked **(Step N)** are placeholders that will be created in the indicated step.

```
Scholarapp/
├── README.md                       # Project blurb + pointers to docs/
├── pyproject.toml                  # Package metadata, deps, `scholar` entry point
├── .env.example                    # Documents every env var; copy to .env
├── implementation-prompts.md       # Prompts for an agent implementing Steps 1–9
├── docs/
│   ├── 01-scaffold.md              # This file
│   ├── 02-persistence.md           # (Step 2) DB schema and lifecycle
│   ├── 03-ingestion.md             # (Step 3) resume + prompt parsing
│   ├── 04-discovery.md             # (Step 4) OpenAlex + Tavily flow
│   ├── 05-matching.md              # (Step 5) project relevance scoring
│   ├── 06-drafting.md              # (Step 6) email drafting + caching
│   ├── 07-review.md                # (Step 7) markdown draft format
│   ├── 08-delivery.md              # (Step 8) Gmail OAuth + SEND_ENABLED
│   └── 09-smoke-test.md            # (Step 9) end-to-end test
├── scholarapp/
│   ├── __init__.py                 # Package version
│   ├── cli.py                      # Typer app with the seven commands
│   ├── config.py                   # Env vars → immutable Settings
│   ├── errors.py                   # Typed exceptions
│   ├── db/
│   │   ├── __init__.py             # Package marker
│   │   ├── models.py               # (Step 2) SQLAlchemy models
│   │   ├── session.py              # (Step 2) Engine + session context manager
│   │   └── repo.py                 # (Step 2) Thin CRUD helpers
│   ├── modules/
│   │   ├── __init__.py             # Package marker
│   │   ├── ingestion.py            # (Step 3)
│   │   ├── discovery.py            # (Step 4)
│   │   ├── matching.py             # (Step 5)
│   │   ├── drafting.py             # (Step 6)
│   │   ├── review.py               # (Step 7)
│   │   └── delivery.py             # (Step 8)
│   └── prompts/
│       ├── __init__.py             # Package marker (so importlib.resources works)
│       ├── parse_resume.txt        # (Step 3)
│       ├── parse_prompt.txt        # (Step 3)
│       ├── match_projects.txt      # (Step 5)
│       └── draft_email.txt         # (Step 6)
└── tests/                          # (Steps 3–9) pytest suites
    ├── conftest.py                 # (Step 9) shared fixtures
    ├── fixtures/                   # (Step 9) sample resume, prompt, template
    ├── cassettes/                  # (Step 9) vcrpy recordings
    ├── test_ingestion.py           # (Step 3)
    ├── test_discovery.py           # (Step 4)
    ├── test_matching.py            # (Step 5)
    ├── test_drafting.py            # (Step 6)
    ├── test_review.py              # (Step 7)
    ├── test_delivery.py            # (Step 8)
    ├── test_e2e.py                 # (Step 9)
    └── test_e2e_recording.py       # (Step 9)
```

Separately, Scholarapp creates the following on first run (outside the repo):

```
~/.scholarapp/                      # DATA_DIR, configurable via env
├── config.toml                     # Written by `scholar init`
├── scholar.db                      # (Step 2) SQLite DB
├── client_secret.json              # (Step 8) Google OAuth client — user-provided
├── credentials.json                # (Step 8) Refresh token, written by OAuth flow
└── runs/
    └── <run_id>/
        ├── inputs/                 # (Step 3) resume.pdf, prompt.md, template.md
        └── drafts/                 # (Step 7) one .md per professor
```

## Install + run

```bash
cd Scholarapp
pip install -e .
cp .env.example .env                # then fill in API keys
scholar init                        # creates ~/.scholarapp/ and config.toml
scholar --help                      # see every command
```

For development:

```bash
pip install -e ".[dev]"             # also installs pytest + ruff
ruff check .
pytest                              # (Step 3 onward)
```

## Environment variables

All env vars are read in `scholarapp/config.py`. `.env` at the working directory is loaded automatically via `python-dotenv`.

| Var | Default | Used by |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Steps 3, 5, 6 |
| `TAVILY_API_KEY` | — | Step 4 |
| `SEND_ENABLED` | `false` | Step 8 — gates the Gmail send path |
| `SEND_DAILY_CAP` | `20` | Step 8 — per-day cap once sending is enabled |
| `DATA_DIR` | `~/.scholarapp` | Everywhere — root of all on-disk state |

Anything else (cache dirs, log levels, etc.) should be added here, not scattered.

## CLI commands

| Command | Status | Purpose |
|---|---|---|
| `scholar init` | implemented | Creates `~/.scholarapp/` and writes `config.toml` if missing |
| `scholar run` | implemented | Full pipeline: parse → discover → match → draft (Steps 3-6). Drafts saved to the DB as `pending_review`; Step 7 writes them as editable markdown files. |
| `scholar list` | implemented | Tabulates all runs (added in Step 2) |
| `scholar review <run_id>` | implemented | Writes drafts to disk and opens `$EDITOR` (added in Step 7) |
| `scholar approve <run_id> [--only <slug>]` | implemented | Sets `status: approved` in matching files, then syncs (added in Step 7) |
| `scholar status <run_id>` | implemented | Per-draft status table for a run (added in Step 2) |
| `scholar send <run_id>` | stub | Future: send approved drafts (gated) |

Stubs print `Not yet implemented: <command>` and exit 0. The wiring exists so the CLI surface is stable from Step 1 onward.

## Conventions

These apply to every step. When adding code in later steps, follow them.

1. **Type hints on every public function.** Pydantic models or dataclasses for all module I/O.
2. **Typed errors only.** Every recoverable failure raises a subclass of `ScholarError` from [`scholarapp/errors.py`](../scholarapp/errors.py). The CLI catches `ScholarError` via `_run_safely` and renders a one-line message. Anything else is treated as a bug and bubbles up as a traceback.
3. **Modules don't touch the DB directly.** Pipeline modules in `scholarapp/modules/` are pure functions over Pydantic dataclasses. The CLI layer (or a small orchestrator added in Step 3) calls modules and persists results via `scholarapp/db/repo.py`. This keeps modules independently testable.
4. **All Claude / HTTP calls live in `scholarapp/modules/`.** The CLI layer only orchestrates. No `httpx.AsyncClient` in `cli.py`.
5. **Prompts as `.txt` resources.** Templates go in `scholarapp/prompts/*.txt` and are loaded via `importlib.resources.files("scholarapp.prompts").joinpath("name.txt").read_text()`. This keeps prompts diffable and out of Python string literals.
6. **One settings load per command.** Call `config.load_settings()` at the top of the command body; pass `Settings` down. No module-level singletons that capture env state at import time.
7. **`DATA_DIR` is the only writable directory outside the repo.** Tests must override it (typically via `tmp_path`) so they don't pollute the user's real `~/.scholarapp/`.

## Cheap exploration: `scholar run --stop-after`

`scholar run` accepts a `--stop-after {parse,discovery,matching}` flag that halts
the pipeline at the named checkpoint. Use this to inspect intermediate state
without paying for later stages.

| `--stop-after` | What runs (and gets charged) | What you see |
|---|---|---|
| `parse` | Resume + prompt parse only | `Run` row created, no professors yet |
| `discovery` | Above + topic pick + author search + email resolution | List of discovered professors with emails |
| `matching` | Above + project relevance picks per professor | Matched-project rows visible in the DB |
| (omitted) | Whole pipeline | Drafts (after Step 6) |

Examples:

```bash
# See who discovery would pick before paying for matching (~$0.02 saved on N=3):
scholar run --stop-after discovery

# Verify the resume parser extracted what you expected; skip everything else:
scholar run --stop-after parse

# Run discovery + matching but stop before drafting (relevant once Step 6 lands):
scholar run --stop-after matching
```

The Claude usage summary still prints at the end, so you see exactly what the
partial run cost. If the discovered list looks wrong (off-topic professors,
missing emails), edit `inputs/prompt.md` to be more specific and re-run.

## Cost observability

Every `scholar run` prints a per-call token + cost summary when the pipeline
finishes (success or failure). Example:

```
────────────────────────────────────────────────────────────────
  Claude usage
────────────────────────────────────────────────────────────────
  parse_resume           sonnet  in=  4823  cache=    0  out=  512  $0.0220
  parse_prompt           haiku   in=   812  cache=    0  out=   98  $0.0013
  pick_topics            haiku   in=  1456  cache=    0  out=   89  $0.0019
  extract_email × 9      haiku   in= 14580  cache=    0  out= 1500  $0.0220
────────────────────────────────────────────────────────────────
  Total: $0.0472   (Sonnet $0.0220, Haiku $0.0252)
────────────────────────────────────────────────────────────────
```

Implementation: [scholarapp/usage.py](../scholarapp/usage.py). Each Claude-calling
function records its `response.usage` against a `UsageTracker` held in a
`contextvars.ContextVar`. The CLI installs the tracker before running the pipeline
and prints the formatted summary in a `finally` so you see the spend even when the
run fails. The tracker is silent — `record()` is a no-op when no tracker is
installed (i.e., during unit tests that don't care).

Pricing table lives at `scholarapp.usage.PRICING`. Update if Anthropic publishes
new rates; the figures are estimates, not ground truth. The Anthropic console is
the authoritative source for billing.

If a row is missing from your summary, that call wasn't made (cache hit, branch
short-circuit). For example, a re-run with the same resume PDF hits the resume
cache and produces no `parse_resume` row.

## What ships in Step 1

- The full file/directory skeleton above.
- A working `scholar init` that creates `~/.scholarapp/config.toml`.
- Every other command stubbed.
- This doc.

## What's next

| Step | Doc | What it adds |
|---|---|---|
| 2 | [`02-persistence.md`](02-persistence.md) | SQLAlchemy models, `init_db()`, `scholar list` and `scholar status` reading the DB |
| 3 | [`03-ingestion.md`](03-ingestion.md) | Resume + prompt parsing via Claude |
| 4 | [`04-discovery.md`](04-discovery.md) | OpenAlex + Tavily professor lookup |
| 5 | [`05-matching.md`](05-matching.md) | Per-professor project relevance |
| 6 | [`06-drafting.md`](06-drafting.md) | Email drafting with prompt caching |
| 7 | [`07-review.md`](07-review.md) | Editable markdown draft files |
| 8 | `08-delivery.md` (to be written) | Gmail OAuth + the SEND_ENABLED gate |
| 9 | `09-smoke-test.md` (to be written) | End-to-end test + top-level README polish |

The prompts to give an agent for each step live in [`implementation-prompts.md`](../implementation-prompts.md) at the repo root.
