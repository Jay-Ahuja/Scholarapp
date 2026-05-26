# Scholarapp

A Python CLI that drafts personalized cold emails from you to professors.
Provide a resume PDF, a short prompt (how many emails, what field, what's your
goal), and a cold-email template; Scholarapp searches OpenAlex for matching
faculty, resolves their emails via Tavily web search, picks 2–3 of each
professor's recent works that overlap with your background, drafts one
personalized email per professor via Claude, and writes them as editable
markdown for you to review.

> **Sending is currently disabled.** The Gmail send path is fully built but
> gated behind `SEND_ENABLED=false`. The pipeline produces drafts in
> `./drafts/<run_id>/` for you to edit; nothing leaves your machine. See
> [docs/08-delivery.md](docs/08-delivery.md) for the exact procedure to enable
> sending.

## Quick start

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Configure API keys
cp .env.example .env
$EDITOR .env        # fill in ANTHROPIC_API_KEY and TAVILY_API_KEY

# 3. Bootstrap
scholar init        # creates ~/.scholarapp/ and prints Gmail OAuth instructions

# 4. Drop your inputs
mkdir -p inputs
cp /path/to/your/resume.pdf inputs/resume.pdf
$EDITOR inputs/prompt.md      # see tests/fixtures/sample_prompt.md for shape
$EDITOR inputs/template.md    # see tests/fixtures/sample_template.md

# 5. Run the pipeline
scholar run                   # full pipeline; ~$0.02-0.04/email
scholar review <run_id>       # write drafts to ./drafts/<run_id>/ and open in $EDITOR
scholar approve <run_id>      # sync your edits back to the DB and flip to approved
scholar send <run_id>         # gated: prints "Sending disabled" until SEND_ENABLED=true
```

Other commands:

```bash
scholar list                  # all runs, newest first
scholar status <run_id>       # per-draft status table for a run
scholar attach-resume on|off  # toggle attaching resume.pdf to outgoing emails (sticky; gated send path only)
```

Cheap-exploration shortcuts:

```bash
scholar run --stop-after parse        # ~$0.002 — just verify your inputs parse
scholar run --stop-after discovery    # ~$0.03 — see who'd get picked before paying for matching
scholar run --stop-after matching     # ~$0.06 — see the picks before paying for drafting
```

`scholar --help` lists every command. Each run ends with a Rich-rendered
Claude usage breakdown so you see exactly what it cost.

## How it fits together

```
inputs/                           ~/.scholarapp/
├── resume.pdf       ──┐          ├── scholar.db        (Run/Professor/Draft/...)
├── prompt.md        ──┼──► scholar run ─┐    ├── runs/<id>/inputs/  (snapshot)
└── template.md      ──┘                 │    └── credentials.json   (Gmail OAuth, gated)
                                         │
                                         ▼
                                  ./drafts/<run_id>/    ◄── you edit these
                                  ├── friston-...md         (then `scholar approve`)
                                  ├── ashburner-...md
                                  └── landman-...md
                                         │
                                         ▼
                                  scholar send <run_id>  ◄── currently a no-op (gated)
```

## Documentation

The `docs/` directory has one file per implementation step. Read in order if
you're new to the codebase.

| | |
|---|---|
| [docs/01-scaffold.md](docs/01-scaffold.md) | Project layout, stack, conventions, CLI styling, cost observability, Anthropic rate-limit notes |
| [docs/02-persistence.md](docs/02-persistence.md) | SQLAlchemy schema, status lifecycles, common queries |
| [docs/03-ingestion.md](docs/03-ingestion.md) | Resume + prompt parsing via Claude (structured output + caching) |
| [docs/04-discovery.md](docs/04-discovery.md) | OpenAlex topic + author lookup, Tavily email resolution, rate-limit circuit breaker |
| [docs/05-matching.md](docs/05-matching.md) | Per-professor project relevance picks |
| [docs/06-drafting.md](docs/06-drafting.md) | Personalized email drafting (prompt caching mechanics, good/bad examples) |
| [docs/07-review.md](docs/07-review.md) | Editable markdown drafts (write/sync, state machine) |
| [docs/08-delivery.md](docs/08-delivery.md) | Gmail OAuth + the `SEND_ENABLED` gate + the exact flip-on procedure |
| [docs/09-smoke-test.md](docs/09-smoke-test.md) | End-to-end test, vcrpy cassettes, troubleshooting |

## Testing

```bash
# Unit tests (mocked Anthropic, mocked HTTP — no API calls, no quota burn)
pytest

# End-to-end test (replays a vcrpy cassette)
pytest tests/test_e2e.py
# Skips if no cassette is committed — see docs/09 for how to record one.

# Refresh the cassette against real APIs (~$0.05, real quota)
pytest -m record tests/test_e2e_recording.py
```

There is no automated test CI. The only GitHub Actions workflows are the Claude
PR-assistant (`.github/workflows/claude.yml`) and the Claude code-review bot
(`.github/workflows/claude-code-review.yml`); neither runs the suite. Run
`pytest` locally before pushing — it covers the unit tests plus the e2e test
when its cassette exists.

## Status

| Feature | Status |
|---|---|
| Pipeline (parse → discover → match → draft → review) | implemented |
| Gmail send | implemented but **gated**; flip `SEND_ENABLED=true` in `.env` after reading [docs/08](docs/08-delivery.md) |
| End-to-end smoke test | infrastructure in place; cassette is opt-in per developer |

Per-run cost on Anthropic Tier 2: ~$0.02–0.03 per drafted email at N=20. See
[docs/01-scaffold.md#anthropic-rate-limits](docs/01-scaffold.md#anthropic-rate-limits)
if you're on Tier 1 and hitting 429s.
