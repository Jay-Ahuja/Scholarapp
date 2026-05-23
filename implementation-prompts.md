# Scholarapp — Implementation Prompts

Nine self-contained prompts, one per implementation step. Copy/paste a single prompt when you're ready to implement that step. Each instructs the agent to read prior docs first, so context compounds across steps.

---

## Step 1 — Scaffold

```
Implement Step 1 of Scholarapp.

Scholarapp is a Python CLI that drafts personalized cold emails from a user to professors. The user provides a resume PDF, a prompt (number of professors, field, end goal, other considerations), and a cold-email template. The app finds relevant professors via OpenAlex + Tavily, matches their recent work to the user's interests, drafts emails via Claude, writes them as editable markdown files, and (eventually) sends them via the user's Gmail. Sending is built but gated behind SEND_ENABLED=false.

Stack:
- Python 3.11+, Typer for the CLI
- SQLAlchemy + SQLite for persistence (user data at ~/.scholarapp/)
- Anthropic SDK with claude-sonnet-4-6
- OpenAlex API (no key), Tavily Search API
- Gmail API via google-auth + google-api-python-client (built later, gated now)

Your task for Step 1: create the project skeleton only. No business logic yet.

Deliverables:
1. pyproject.toml with project metadata and a `scholar` CLI entry point (`scholar = scholarapp.cli:app`). Dependencies: typer, sqlalchemy, anthropic, httpx, pydantic, google-auth, google-auth-oauthlib, google-api-python-client, python-dotenv, pyyaml. Dev deps: pytest, ruff.
2. Package layout:
   scholarapp/
     __init__.py
     cli.py                  # Typer app with all 7 commands stubbed
     config.py               # loads env vars: ANTHROPIC_API_KEY, TAVILY_API_KEY, SEND_ENABLED (default False), DATA_DIR (default ~/.scholarapp)
     db/__init__.py
     modules/__init__.py
     prompts/                # empty for now, future home of prompt templates
3. CLI commands stubbed to print "Not yet implemented" and exit 0:
   - `scholar init`         (will later: place input files + run Gmail OAuth)
   - `scholar run`          (will later: run full pipeline)
   - `scholar list`
   - `scholar review <run_id>`
   - `scholar approve <run_id>` (with `--only <slug>` option)
   - `scholar status <run_id>`
   - `scholar send <run_id>`
4. `scholar init` should be the one exception: it creates `~/.scholarapp/` and writes a default config.toml inside if missing. Print the path it created.
5. A `.env.example` at repo root listing all env vars.
6. A short README at repo root that points to docs/.

Conventions to establish and document:
- All public functions have type hints.
- Errors raise typed exceptions from `scholarapp/errors.py`; the CLI catches them and prints a friendly message.
- All Claude / HTTP calls happen inside `scholarapp/modules/`; the CLI layer only orchestrates.
- Prompt templates live as .txt files in `scholarapp/prompts/` and are loaded with `importlib.resources`.

Also create `docs/01-scaffold.md` — a comprehensive doc that someone seeing this repo for the first time can read to understand:
- What Scholarapp is and the 7-step pipeline at a high level
- The full tech stack and why each piece was chosen
- The complete file/directory layout with one-line explanations of every file and folder (including ones not yet created — mark them as "added in Step N")
- How to install (`pip install -e .`) and run
- All env vars and where they're loaded
- All CLI commands with their current status (stub vs implemented)
- The conventions above
- Pointers to docs/02 through docs/09 (note "to be written")

Do not implement any pipeline logic. The goal is a runnable scaffold where `scholar init` works and every other command prints "Not yet implemented".
```

---

## Step 2 — Persistence

```
Implement Step 2 of Scholarapp.

Before starting: read docs/01-scaffold.md to understand the project layout, stack, and conventions.

Your task: define the SQLAlchemy models and DB helpers, and implement the two read-only CLI commands (`scholar list`, `scholar status`) that exercise them.

Deliverables:
1. scholarapp/db/models.py with SQLAlchemy 2.x typed models:
   - Run: id (uuid str), created_at, status (enum: pending|parsing|discovering|matching|drafting|review|done|failed), field, goal, considerations, count, resume_path, template_text, error (nullable)
   - Professor: id (uuid str), run_id (fk), name, institution, email, openalex_id, faculty_page_url, raw_json (text)
   - Project: id, professor_id (fk), title, url, year, abstract (nullable), raw_json
   - MatchedProject: id, professor_id (fk), project_id (fk), why_relevant
   - Draft: id (uuid str), run_id (fk), professor_id (fk), subject, body, status (enum: pending_review|approved|rejected|sent|send_disabled), file_path (nullable), updated_at
   - SendLog: id, draft_id (fk), attempted_at, outcome (enum: sent|send_disabled|error), error (nullable), gmail_message_id (nullable)

2. scholarapp/db/session.py:
   - SQLite engine pointed at `DATA_DIR/scholar.db` (DATA_DIR from config)
   - `init_db()` that creates all tables idempotently — called on first use
   - `get_session()` context manager

3. scholarapp/db/repo.py with thin CRUD helpers used by other modules (e.g., `create_run`, `get_run`, `list_runs`, `update_run_status`, `add_professor`, `add_draft`, `list_drafts_for_run`, etc.). Do not bake business logic into the repo — pure read/write only.

4. Wire `scholar list` to print a table of all runs (id, created_at, field, count, status).
5. Wire `scholar status <run_id>` to print a per-draft status table for that run (professor name, email, status, file_path).

6. Migrations: since this is single-user SQLite and we're pre-v1, no Alembic. Use `init_db()` to create tables on first use; document that schema changes require deleting the DB during dev.

Also create `docs/02-persistence.md` covering:
- An ASCII ER diagram of the schema
- Each model's fields with a one-line semantic explanation
- The two status enums (Run.status, Draft.status, SendLog.outcome) with the full lifecycle: pending_review → approved → (sent | send_disabled) and pending_review → rejected
- A "common queries" section showing how to: get all pending_review drafts for a run, get the latest run, count drafts by status
- Where the DB file lives, how to inspect it (`sqlite3 ~/.scholarapp/scholar.db`)
- Why SQLite, and what we'd change for multi-user (Postgres + Alembic + per-user data dirs)
- Schema-change policy during pre-v1

Verify: running `scholar init` then `scholar list` against a fresh DB prints an empty table without error.
```

---

## Step 3 — Ingestion

```
Implement Step 3 of Scholarapp.

Before starting: read docs/01-scaffold.md and docs/02-persistence.md.

Your task: parse the resume PDF and the user's prompt into structured data using Claude.

Deliverables:
1. scholarapp/modules/ingestion.py with:
   - Pydantic models:
     - ResumeData: name, email, education (list of {school, degree, field, years}), experiences (list of {org, role, years, bullets}), skills (list[str]), interests (list[str]), publications (list of {title, venue, year, url?})
     - PromptData: count (int, 1-50), field (str), goal (str), considerations (str)
   - `parse_resume(pdf_path: Path) -> ResumeData`: sends the PDF directly to Claude (claude-sonnet-4-6) using the documents API, returns structured output via tool-use with the ResumeData schema. Raise IngestionError on parse failure.
   - `parse_prompt(text: str) -> PromptData`: one Claude call with structured output. Raise IngestionError if any field is missing or count is out of range.

2. Prompt templates in scholarapp/prompts/:
   - parse_resume.txt: instructs Claude to extract the fields, prefer specificity (e.g., concrete projects in "experiences" bullets), and infer interests from the body of the resume rather than only from an explicit interests section.
   - parse_prompt.txt: instructs Claude to extract the four fields. If the user's prompt is vague on any field, fail loudly with which field is missing (raise from the parser).

3. Wire `scholar run` to:
   a. Read resume.pdf, prompt.md, template.md from a path argument (default ./inputs/)
   b. Call parse_resume and parse_prompt
   c. Create a Run row with the parsed PromptData fields + resume_path + template_text
   d. Print "Parsed inputs. run_id=..." then exit (later steps will continue the pipeline)

4. Use prompt caching on the system prompt of each Claude call (templates are reused across runs).

5. Add minimal tests at tests/test_ingestion.py with a tiny fixture resume PDF and a sample prompt; mock the Anthropic client to assert correct request shape (do not hit the real API in tests).

Also create `docs/03-ingestion.md` covering:
- What fields are extracted from the resume and why each is used downstream (e.g., "interests feeds matching", "experiences feeds drafting")
- The two prompt templates: pointer to their files plus the rationale for each instruction
- The Pydantic schemas in full
- How Claude PDF input works (documents API, base64 encoding, model requirements)
- Failure modes: corrupt PDF, missing required fields in prompt, low-quality resume scan; what error the user sees and how to recover
- How to add a new field to ResumeData (update model + prompt + downstream consumers)
- How prompt caching is applied here and what the expected cache savings are
- How to test locally with `scholar run` against a sample resume

Do not implement discovery, matching, drafting, or anything downstream — `scholar run` stops after parsing.
```

---

## Step 4 — Discovery

```
Implement Step 4 of Scholarapp.

Before starting: read docs/01 through docs/03.

Your task: find N professors in the user's field, fetch their recent works, and resolve their email.

Deliverables:
1. scholarapp/modules/discovery.py with:
   - Pydantic models:
     - ProfessorCandidate: openalex_id, name, institution, email (validated as .edu or known academic TLD), faculty_page_url, recent_works (list of WorkRef)
     - WorkRef: title, url, year, abstract?, openalex_id
   - `async find_professors(field: str, count: int, user_interests: list[str]) -> list[ProfessorCandidate]`

2. Pipeline inside find_professors:
   a. Resolve `field` to OpenAlex concept IDs:
      - Call OpenAlex /concepts?search={field}
      - Pass top results to Claude to pick the best 1-2 matching concept IDs (handles "neuroscience" → multiple, "robotics" → one)
   b. Query OpenAlex /authors:
      - filter: x_concepts.id:{concept_id},last_known_institutions.type:education,works_count:>10
      - sort: cited_by_count:desc
      - fetch ~3x count candidates
   c. For each candidate, fetch /works?filter=author.id:{id}&sort=publication_date:desc&per_page=5
   d. For each candidate, resolve email + faculty page:
      - Tavily search: `"{name}" "{institution}" faculty email`
      - Pass top 5 results to Claude with a strict prompt: "Extract the .edu email address for this professor from these search results. Return null if not confidently found."
      - Validate the email matches an academic TLD (.edu, .ac.uk, etc. — keep an allowlist)
   e. Drop candidates without confirmed email
   f. Return top `count` (if fewer than `count` survive, return what we have and log a warning)

3. Use asyncio.gather to parallelize steps c and d across candidates. Set sensible concurrency caps (e.g., 10) to avoid hammering APIs.

4. Wire into `scholar run`:
   - After ingestion (Step 3), update Run.status to 'discovering'
   - Call find_professors
   - Persist each result as a Professor row + its WorkRefs as Project rows
   - Update Run.status to 'matching' (next step's job; for now stop and print "Discovered N professors. run_id=...")

5. Add a small HTTP client wrapper (httpx.AsyncClient) with retry on 429/5xx (exponential backoff, max 3 attempts).

6. Tests at tests/test_discovery.py: mock OpenAlex + Tavily responses with fixture JSON; verify the pipeline filters correctly and handles the "no email found" case.

Also create `docs/04-discovery.md` covering:
- Full data-flow diagram (field input → concept IDs → authors → works → email resolution → filtered output)
- OpenAlex endpoints used, their filters, and links to API docs
- Why this two-source approach (OpenAlex for academic data, Tavily for email) instead of scraping Google Scholar
- The field-to-concept resolution strategy and why it needs an LLM step
- The over-fetch-then-filter pattern and the expected drop rate (~20-30% lose email)
- Tavily query construction patterns, including alternatives we tried/ruled out
- Email validation rules: the academic TLD allowlist and how to extend it
- Rate-limiting notes: OpenAlex is generous but asks for a polite email in User-Agent; Tavily has a free tier with N requests/month
- What to do when the field is too narrow (no concepts match) or too broad (>1000 authors)
- How to add a new data source (e.g., Semantic Scholar) — extension points to look at
- How to test locally without burning API quota (fixture replay)

Do not implement matching or drafting — the pipeline stops after persisting professors.
```

---

## Step 5 — Matching

```
Implement Step 5 of Scholarapp.

Before starting: read docs/01 through docs/04.

Your task: for each discovered professor, pick the 2-3 recent works most relevant to the user's interests and explain why.

Deliverables:
1. scholarapp/modules/matching.py with:
   - Pydantic model MatchedProject: project_id, title, url, why_relevant (one sentence)
   - `async match_projects(professor: Professor, projects: list[Project], user_interests: list[str], user_experiences: list[str]) -> list[MatchedProject]`

2. Behavior:
   - One Claude call per professor (claude-sonnet-4-6) with structured output (tool use)
   - Input: the professor's recent works (title + abstract if available) + the user's interests + a brief summary of user's experiences
   - Output: 2-3 MatchedProjects ranked by relevance. If fewer than 2 works show meaningful overlap, return what's relevant (could be 1, could be 0 — log warning if 0)
   - `why_relevant` must reference a specific concept that overlaps (not "your interests align with this work")
   - Parallelize across professors with asyncio.gather, concurrency cap 10

3. Prompt template at scholarapp/prompts/match_projects.txt with explicit rules:
   - Prefer overlap on technical specifics over generic field-level overlap
   - Do not invent connections — if the work is genuinely unrelated, omit it
   - `why_relevant` must name the concrete overlap in <= 25 words

4. Wire into `scholar run`:
   - After discovery, update Run.status to 'matching'
   - Call match_projects for each professor
   - Persist results as MatchedProject rows
   - Update Run.status to 'drafting'; for now stop and print "Matched projects for N professors. run_id=..."

5. Tests at tests/test_matching.py: mock Claude responses; verify projects with no overlap are dropped, the cap of 3 is respected, and the structured output is parsed.

Also create `docs/05-matching.md` covering:
- The role this module plays between discovery and drafting (it produces the "hook" that the email is built around)
- The prompt template's full text and the rationale for each rule
- The Pydantic schema and how each field is used downstream by drafting
- Why 2-3 and not 1 or 5 (drafting wants enough material to choose from but not so much it dilutes)
- How to tune the relevance threshold (currently implicit in the LLM judgment — alternatives: embedding-based score, explicit numeric rating)
- The "0 matched" case: when it happens and how the email-drafting step handles it (Step 6 will need to skip or fall back)
- How to test with a fixture professor + interests pair

Do not implement drafting — the pipeline stops after persisting matched projects.
```

---

## Step 6 — Drafting

```
Implement Step 6 of Scholarapp.

Before starting: read docs/01 through docs/05.

Your task: generate a personalized email per professor, using the user's template + resume + the matched projects as material.

Deliverables:
1. scholarapp/modules/drafting.py with:
   - Pydantic model EmailDraft: subject (str), body (str)
   - `async draft_email(template: str, resume: ResumeData, professor: Professor, matched: list[MatchedProject], goal: str, considerations: str) -> EmailDraft`

2. Prompt structure (this is the most important detail):
   - SYSTEM prompt with cache_control=ephemeral, containing:
     * The user's cold-email template (verbatim, labeled)
     * A condensed view of the user's resume (name, education, key experiences, interests, publications) — same for every professor in the run
     * Drafting rules (see below)
   - USER message (uncached, per-professor):
     * Professor name, institution, email
     * The matched projects with their why_relevant
     * The user's stated goal and considerations
     * "Draft the email now."
   - Structured output via tool use returning {subject, body}

3. Drafting rules (in the system prompt):
   - The body MUST reference at least one specific project by something concrete (a finding, a method, a question it raised) — not just by title
   - The user's connection to the work MUST be grounded in a real experience from the resume — do not invent
   - The ask in the closing MUST naturally lead toward the stated goal; calibrate friction (a 30-min chat is low friction; a lab position is high friction and needs more setup)
   - Length: 120-180 words for the body; subject under 8 words
   - Tone: respectful, specific, not sycophantic; no "I hope this email finds you well"
   - Do not promise anything the user hasn't said they can do
   - If matched is empty, raise DraftingError("No matched projects for {prof.name}") — caller should skip

4. Caching: use the Anthropic SDK's cache_control on the system prompt. Verify cache hits via the response's usage.cache_read_input_tokens.

5. Wire into `scholar run`:
   - After matching, update Run.status to 'drafting'
   - For each professor with matched projects, call draft_email, persist as Draft row (status='pending_review')
   - For professors with no matched projects, log and skip
   - Update Run.status to 'review' and print "Drafted N emails. run_id=... Run `scholar review <run_id>` to see them."

6. Tests at tests/test_drafting.py: mock Claude; verify cache_control is set on the system message, the request includes matched projects in the user message, and the output is parsed into EmailDraft. One test should verify the empty-matched case raises DraftingError.

Also create `docs/06-drafting.md` covering:
- Why the system/user split matters for prompt caching, and what the expected cache_read ratio is (system reused N-1 times per run)
- The full text of the drafting prompt with annotations on each rule
- Examples of a good draft and a bad draft for the same input, with commentary on what makes the difference
- How tone/length/ask-friction were calibrated and how to tune them
- The "no matched projects" handling and why we skip rather than fall back to a generic email
- How to A/B test prompt changes safely (e.g., capture the rendered prompt + response pairs to compare across runs)
- The estimated cost per run: system tokens × 1 cache write + N cache reads + N user-message tokens + N output tokens

Do not implement the review/edit flow — drafts are written to the DB only; file-on-disk handling is Step 7.
```

---

## Step 7 — Review

```
Implement Step 7 of Scholarapp.

Before starting: read docs/01 through docs/06.

Your task: write drafts to editable markdown files, and read them back to sync edits + status changes into the DB.

Deliverables:
1. scholarapp/modules/review.py with:
   - `write_drafts_to_disk(run_id: str) -> Path`: reads all Draft rows for the run, writes one .md file per draft to `~/.scholarapp/runs/<run_id>/drafts/<slug>.md`, updates Draft.file_path. Returns the drafts directory path. Slug = lowercase last-name + first 8 of draft_id.
   - `sync_drafts_from_disk(run_id: str) -> SyncReport`: parses each .md file in that directory; updates Draft.subject, Draft.body, Draft.status from the file. Returns a SyncReport with counts (updated, unchanged, status_changed, errors).

2. Markdown format — must round-trip exactly:
   ---
   draft_id: <uuid>
   professor: Jane Doe
   institution: MIT
   email: jdoe@mit.edu
   matched_projects:
     - title: "Neural circuits in zebrafish larvae"
       why: "Overlaps with your CS+neuro thesis on optogenetics"
   status: pending_review        # user edits this to "approved" or "rejected"
   ---
   Subject: <subject line>

   <body, multi-paragraph, plain text>

   Read-only fields (draft_id, professor, institution, email, matched_projects): if the user changes these in the file, ignore them and warn. Editable: subject, body, status.

3. Status transition rules in sync:
   - pending_review → approved | rejected | pending_review : allowed
   - approved → pending_review : allowed (user changed their mind)
   - any other transition (e.g., sent → approved) : reject with a clear error, no DB change

4. Wire commands:
   - `scholar review <run_id>`: calls write_drafts_to_disk, prints the path, opens $EDITOR on the directory if $EDITOR is set
   - `scholar approve <run_id> [--only <slug>]`: convenience — sets status: approved in the matching file(s) and calls sync. If --only is omitted, applies to all pending_review drafts.

5. Tests at tests/test_review.py:
   - Round-trip: write drafts → read them back → DB unchanged
   - Edit body in file → sync → DB updated
   - Change status to approved → sync → DB status updated, no other change
   - Tamper with read-only field (e.g., email) → sync warns + ignores
   - Invalid status transition (sent → approved) → sync errors with clear message

Also create `docs/07-review.md` covering:
- The exact markdown format with an annotated example
- Which fields are editable vs read-only, and why
- The status state machine (with a small diagram) and which transitions are valid
- The sync semantics: last-write-wins for editable fields, file is source of truth at sync time
- Why files-on-disk instead of a TUI/web review: leverages the user's editor, easy diffing, no UI to build
- Where files live (`~/.scholarapp/runs/<run_id>/drafts/`) and naming convention
- How conflict-detection works if the DB was modified between write and sync (use Draft.updated_at; warn on mismatch)
- How to extend the format with new fields without breaking old files

Do not touch delivery — sending is Step 8.
```

---

## Step 8 — Delivery (gated)

```
Implement Step 8 of Scholarapp.

Before starting: read docs/01 through docs/07.

Your task: build the full Gmail send path, but keep it gated behind SEND_ENABLED=false so it never actually sends today.

Deliverables:
1. scholarapp/modules/delivery.py with:
   - `_oauth_flow() -> Credentials`: runs the Google OAuth installed-app flow (google-auth-oauthlib InstalledAppFlow), opens browser, stores refresh token at `~/.scholarapp/credentials.json`. Scope: 'https://www.googleapis.com/auth/gmail.send' only.
   - `_load_credentials() -> Credentials`: loads from credentials.json, refreshes if expired. Raises DeliveryError if missing — instructs user to run `scholar init`.
   - `_build_mime(draft: Draft, from_addr: str) -> str`: builds an RFC 822 MIME message (To, From, Subject, body) and returns base64url-encoded raw string for Gmail.
   - `send_approved(run_id: str) -> DeliveryReport`:
     * Fetch all Draft rows for run_id with status='approved'
     * If config.SEND_ENABLED is False:
         - For each draft, write SendLog(outcome='send_disabled'), do NOT change draft status, do NOT call Gmail
         - Raise SendingDisabled(f"{N} drafts would have been sent. SEND_ENABLED is False; no email was sent.")
     * If True (future):
         - Build Gmail service from credentials
         - Enforce a per-day cap (default 20) — read existing SendLog rows for today; if cap reached, raise
         - For each draft: build MIME → users.messages.send → on success update Draft.status='sent' + SendLog(outcome='sent', gmail_message_id=...); on error SendLog(outcome='error', error=...) and continue
         - Return DeliveryReport with counts

2. Update `scholar init`:
   - After creating the data dir + config, check for `client_secret.json` at `~/.scholarapp/client_secret.json`. If present and credentials.json missing, run _oauth_flow. If client_secret.json missing, print clear instructions for creating it (see doc) and exit 0 without erroring.

3. Wire `scholar send <run_id>`:
   - Calls send_approved
   - Catches SendingDisabled: prints the message, exits 1
   - Catches DeliveryError: prints + exits 1
   - On success: prints DeliveryReport summary

4. Config: add SEND_DAILY_CAP (default 20) to config.py.

5. Tests at tests/test_delivery.py:
   - With SEND_ENABLED=False and 3 approved drafts: send_approved raises SendingDisabled, exactly 3 SendLog rows with outcome='send_disabled', draft statuses unchanged
   - With SEND_ENABLED=True (mock the Gmail client): builds correct MIME, calls users.messages.send 3 times, draft statuses become 'sent', SendLog rows have gmail_message_id
   - Daily cap: mock 20 existing send logs for today, attempt 1 more → raises before any Gmail call

Also create `docs/08-delivery.md` covering:
- The complete OAuth setup, step-by-step:
  1. Create a Google Cloud project
  2. Enable the Gmail API
  3. Configure the OAuth consent screen (External, testing mode, add the user's Gmail as a test user)
  4. Create OAuth client credentials (type: Desktop app)
  5. Download client_secret.json and place at ~/.scholarapp/client_secret.json
  6. Run `scholar init` to authorize — opens browser, returns to terminal
- The exact OAuth scope used and why we use the narrow `gmail.send` scope (not `gmail.modify`)
- MIME construction details (headers, encoding, attachments unsupported)
- The send loop, the per-day cap, and the failure-handling policy (one draft failing does not stop the rest)
- The SEND_ENABLED gate: where it's checked, what happens in each branch, and the EXACT steps to flip it on:
  1. Verify all approved drafts look correct (`scholar status <run_id>`)
  2. Open at least 3 drafts and read them end-to-end
  3. Set SEND_ENABLED=true in .env
  4. Run `scholar send <run_id>` — first run will be capped at SEND_DAILY_CAP
- How token refresh works and what to do if credentials.json is corrupted (delete it, re-init)
- Security notes: credentials.json contains a refresh token — do not commit, do not share. Suggest chmod 600.
- Rate limits: Gmail allows 250 quota units/sec; sending is 100 units. Practical cap is well below the per-day cap.
- A "before you ship" checklist of risks to revisit (anti-spam, deliverability, unsubscribe handling)

Do not enable sending. SEND_ENABLED stays False by default.
```

---

## Step 9 — E2E smoke test

```
Implement Step 9 of Scholarapp.

Before starting: read docs/01 through docs/08.

Your task: add a real end-to-end smoke test, fixtures, and a top-level README that points to all docs.

Deliverables:
1. tests/fixtures/:
   - sample_resume.pdf: a small synthetic resume for a fictional CS undergrad interested in neuroscience + ML
   - sample_prompt.md: realistic prompt asking for 3 professors in computational neuroscience, goal = 30-min chat about a project
   - sample_template.md: a generic cold-email template with placeholders the model will personalize

2. tests/test_e2e.py with a single test `test_full_pipeline_no_send`:
   - Use pytest tmp_path to isolate DATA_DIR
   - Use vcrpy cassettes (tests/cassettes/) for OpenAlex + Tavily + Anthropic HTTP traffic; record once, replay in CI
   - Run: equivalent of `scholar run` with the fixtures (call the CLI command via Typer's CliRunner, or call the pipeline function directly)
   - Assert:
     * Run row exists, status='review'
     * 3 Draft rows exist with status='pending_review'
     * 3 .md files exist in the drafts dir
     * Each draft body is 50+ words and references the title of at least one matched project (case-insensitive substring)
     * `scholar send <run_id>` exits 1 with message containing "Sending disabled"
     * 0 Gmail API calls were made (verified by cassette absence)

3. tests/test_e2e_recording.py (skipped by default, requires real API keys): a recording variant that hits real APIs to refresh cassettes. Run with `pytest -m record --record-mode=rewrite`.

4. Top-level README.md replacing the Step 1 stub:
   - One-paragraph project description
   - Quick start: install, env setup, init, run on a sample
   - Pointers to each doc in docs/ with a one-line summary
   - "Sending is currently disabled" callout linking to docs/08
   - Testing section: how to run unit tests, the e2e test, and the recording variant

5. tests/conftest.py: shared fixtures (tmp DATA_DIR, mocked clients for unit tests, cassette config for e2e)

Also create `docs/09-smoke-test.md` covering:
- What the e2e test verifies and what it deliberately does NOT verify (e.g., real Gmail send, real OpenAlex availability, real Anthropic latency)
- Required env vars for recording (ANTHROPIC_API_KEY, TAVILY_API_KEY) and how to set them up
- How vcrpy cassettes work in this project: where they live, what's redacted (API keys in headers), how to re-record when the API changes
- The fixture set: what each fixture contains and why it was chosen
- Troubleshooting common failures:
  * Cassette mismatch after refactor → re-record
  * Anthropic SDK version bump changed request shape → re-record
  * Cassette file too large → check we're not embedding raw PDFs in fixture requests
- How to extend the test (e.g., add a "draft references at least one user-experience" assertion)
- Pre-merge checklist: unit tests pass, e2e test passes against cassettes, manual smoke run against real APIs at least weekly

After Step 9, the app should be feature-complete for everything except actually sending email, with full documentation and a green test suite.
```
