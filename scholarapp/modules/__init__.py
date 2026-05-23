"""Pipeline modules.

- `ingestion` — parse resume + prompt (Step 3)
- `discovery` — find professors via OpenAlex + Tavily (Step 4)
- `matching` — pick relevant works per professor (Step 5)
- `drafting` — generate per-professor emails via Claude (Step 6)
- `review` — write/sync editable markdown drafts (Step 7)
- `delivery` — Gmail send, gated behind SEND_ENABLED (Step 8)

Each module exposes pure functions over Pydantic dataclasses. Persistence is handled
by the caller (the CLI) so modules can be tested in isolation.
"""
