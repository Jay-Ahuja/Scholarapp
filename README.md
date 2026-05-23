# Scholarapp

CLI that drafts personalized cold emails from you to professors. Provide a resume PDF, a short prompt (how many emails, what field, what's your goal), and a cold-email template. Scholarapp finds relevant professors, matches their recent work to your interests, drafts each email, and writes the drafts to disk for you to review and edit.

> **Sending is currently disabled.** The Gmail send path is built but gated behind `SEND_ENABLED=false`. See [docs/08-delivery.md](docs/08-delivery.md) (added in Step 8) for the procedure to enable it.

## Quick start

```bash
pip install -e .
cp .env.example .env   # fill in ANTHROPIC_API_KEY and TAVILY_API_KEY
scholar init           # creates ~/.scholarapp/
```

`scholar --help` lists the rest of the commands.

## Docs

The `docs/` directory has one file per implementation step. Read them in order if you're new to the codebase.

- [docs/01-scaffold.md](docs/01-scaffold.md) — project layout, stack, conventions
- [docs/02-persistence.md](docs/02-persistence.md) — DB schema and lifecycle
- `docs/03-ingestion.md` — resume + prompt parsing (added in Step 3)
- `docs/04-discovery.md` — professor discovery via OpenAlex + Tavily (added in Step 4)
- `docs/05-matching.md` — project-to-interest matching (added in Step 5)
- `docs/06-drafting.md` — email drafting with prompt caching (added in Step 6)
- `docs/07-review.md` — editable markdown drafts (added in Step 7)
- `docs/08-delivery.md` — Gmail OAuth + the SEND_ENABLED gate (added in Step 8)
- `docs/09-smoke-test.md` — end-to-end test (added in Step 9)
