# 03 — Ingestion

The ingestion module turns the three files the user drops into `./inputs/` —
`resume.pdf`, `prompt.md`, `template.md` — into typed Pydantic records that the rest
of the pipeline consumes. Implementation: [scholarapp/modules/ingestion.py](../scholarapp/modules/ingestion.py).

Two entry points:

- `parse_resume(pdf_path: Path) -> ResumeData`
- `parse_prompt(text: str) -> PromptData`

The template is stored verbatim — no parsing needed; it's just text the drafting step
will paste into a system prompt.

## What gets extracted from the resume, and why

| Field | Downstream consumer | What "good" extraction looks like |
|---|---|---|
| `name` | Drafting (sign-off) | The literal name on the resume |
| `email` | Drafting (return-to header), Delivery | The header email; empty string if absent |
| `education` | Drafting (claims of background must be grounded) | One entry per degree program |
| `experiences` | Drafting (the "here's what I worked on" hook) | Concrete projects + outcomes per bullet, not generic responsibilities |
| `skills` | Matching (technical-vocabulary signal) | Tools and techniques mentioned anywhere |
| `interests` | **Matching** (this is the core overlap signal for picking professor projects) | Inferred from the body of the resume — projects, courses, publications — not only from a literal "Interests" section |
| `publications` | Drafting (signals research maturity; can be referenced if topically relevant) | Peer-reviewed, preprints, notable workshop papers |

`interests` is the most important field for the matching step (Step 5). Resume writers
who don't have an explicit "Interests" section are common; the prompt instructs Claude
to infer interests from the rest of the resume.

## Pydantic schemas

Defined in [scholarapp/modules/ingestion.py](../scholarapp/modules/ingestion.py). These
double as Anthropic tool input schemas via `model_json_schema()`.

```python
class Education(BaseModel):
    school: str
    degree: str
    field: str
    years: str

class Experience(BaseModel):
    org: str
    role: str
    years: str
    bullets: list[str]

class Publication(BaseModel):
    title: str
    venue: str
    year: int | None = None
    url: str | None = None

class ResumeData(BaseModel):
    name: str
    email: str
    education: list[Education]
    experiences: list[Experience]
    skills: list[str]
    interests: list[str]
    publications: list[Publication]

class PromptData(BaseModel):
    count: int = Field(ge=1, le=50)
    field: str
    goal: str
    considerations: str
```

There's also an internal `_PromptExtraction` schema that the LLM populates directly. It
allows nullable required fields and an explicit `missing_fields: list[str]` so we can
distinguish "Claude is unsure" from "the user actually said this." `parse_prompt`
inspects the result, raises `IngestionError` if anything is missing, and only then
returns a fully validated `PromptData`.

## Prompt templates

| File | Loaded by | Purpose |
|---|---|---|
| [scholarapp/prompts/parse_resume.txt](../scholarapp/prompts/parse_resume.txt) | `parse_resume` | Tells Claude to be specific, infer interests from the body, drop vague bullets, return empty lists rather than guessing |
| [scholarapp/prompts/parse_prompt.txt](../scholarapp/prompts/parse_prompt.txt) | `parse_prompt` | Tells Claude to extract the four fields and **explicitly flag missing ones in `missing_fields`** rather than guessing |

Both prompts are loaded via `importlib.resources.files("scholarapp.prompts")` so they
ship as package data — no path hackery.

Rationale for the key prompt instructions:

- **Resume — "infer interests from the body":** Most resumes lack a literal Interests
  section. Without this instruction, Claude returns `interests=[]` and the matching
  step has nothing to work with.
- **Resume — "drop vague bullets":** Drafting promises that the email will ground its
  claims in real experience. A vague bullet like "Contributed to team projects" turns
  into a vague email. Better to omit and let the LLM pick concrete ones.
- **Prompt — "do not guess; list missing in `missing_fields`":** The pipeline is
  expensive (multiple Claude calls + API rate limits) — better to fail at ingestion
  than discover the user meant something different after drafting 10 emails.

## How Claude PDF input works

Claude accepts PDFs as a content block of type `document` inside a user message:

```python
{
    "type": "document",
    "source": {
        "type": "base64",
        "media_type": "application/pdf",
        "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
    },
}
```

Notes:

- Claude reads both the text and the visual layout of the PDF. Scanned-image PDFs
  with no embedded text are still OCR'd by the model, but accuracy drops.
- We don't extract text ourselves — no `pdfplumber`/`pypdf` dependency. Claude handles
  it.
- The PDF must be ≤ 32 MB and ≤ 100 pages (model limits). Resumes are well under both.
- **Model split:** `parse_resume` uses `claude-sonnet-4-6` (constant
  `ingestion.MODEL_SONNET`) because PDF accuracy on multi-column resumes is
  materially better on Sonnet. `parse_prompt` uses `claude-haiku-4-5` (constant
  `ingestion.MODEL_HAIKU`) — pulling four fields out of a paragraph is a narrow
  task Haiku handles at parity for ~⅓ the cost. The same Sonnet/Haiku split is
  applied in [discovery](04-discovery.md) and will be used by drafting (Step 6,
  Sonnet for quality).

For structured output we use **forced tool use**: Claude must call the
`extract_resume` (or `extract_prompt`) tool, whose `input_schema` is the Pydantic
model's JSON schema. We set:

```python
tool_choice={"type": "tool", "name": "extract_resume"}
```

This guarantees the response contains exactly one `tool_use` block with a dict
matching the schema (within the limits of model compliance). We then run it through
`ResumeData.model_validate(...)` to lock down types.

## Prompt caching

Both calls put the system prompt behind `cache_control={"type": "ephemeral"}`:

```python
system=[{
    "type": "text",
    "text": SYSTEM_TEMPLATE,
    "cache_control": {"type": "ephemeral"},
}]
```

Per Anthropic's cache rules, the cached prefix must exceed a minimum size (currently
~1024 tokens for Sonnet) for the cache to actually engage. **Today our system prompts
are smaller than that** — setting `cache_control` is a no-op until the templates grow.
It's wired now so we don't forget when the drafting prompt (Step 6) easily exceeds
the threshold.

Expected savings once it engages: subsequent calls within ~5 min reuse the cached
prefix and pay ~10% of normal input cost on those tokens. For a 10-professor run,
the drafting step alone reuses one ~2k-token system prompt 10 times — meaningful.

## Resume parse cache (content-hash)

`parse_resume` is the most expensive call per run (the Sonnet PDF call). Across
multiple runs with the same résumé, re-parsing is wasteful — the bytes are
identical, so the parsed fields will be too.

The CLI helper [`_parse_resume_cached`](../scholarapp/cli.py) sits in front of
`parse_resume` and memoizes by `sha256(pdf_bytes)`:

```python
def _parse_resume_cached(pdf_path: Path) -> ResumeData:
    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    with get_session() as session:
        cached = repo.get_cached_resume(session, sha)
    if cached is not None:
        return ResumeData.model_validate(cached)
    data = parse_resume(pdf_path)
    with get_session() as session:
        repo.cache_resume(session, sha, data.model_dump())
    return data
```

The cache lives in the `resume_cache` SQLite table (see
[docs/02-persistence.md](02-persistence.md)). One row per unique PDF content
hash. The hash IS the validity check — no invalidation logic needed. Edit the
PDF by one byte and the hash changes, so we re-parse automatically.

Effect on the cost formula:

- **First time** you run with a new resume: pays the ~$0.015 Sonnet PDF parse,
  then caches.
- **Every subsequent run with the same PDF**: free for the resume parse step.
  The fixed term in the cost formula drops from ~$0.02 to ~$0.005.

To clear the cache (e.g., to re-run extraction after a prompt-template change):

```bash
sqlite3 ~/.scholarapp/scholar.db 'delete from resume_cache;'
```

## Failure modes

| Symptom | Cause | What the user sees | How to recover |
|---|---|---|---|
| `Missing input file: ./inputs/resume.pdf` | One of the three files isn't in `--inputs` | `Error: Missing input file: ...` | Add the file or pass `--inputs <other-dir>` |
| `ANTHROPIC_API_KEY is not set` | `.env` not configured | `Error: ANTHROPIC_API_KEY is not set.` | Copy `.env.example` to `.env` and fill in the key |
| `Resume PDF is empty: ...` | Zero-byte file | `Error: Resume PDF is empty: ...` | Re-export from your resume tool |
| `Anthropic API error while parsing resume: ...` | Network, auth, or rate-limit issue | The error from the SDK | Retry; check API status |
| `Claude did not call the expected tool extract_resume` | Model refused (unusual; can happen with malformed input) | The error message | Inspect the PDF; report a bug if it reproduces |
| `Resume parser returned data that did not match the schema: ...` | Model returned malformed JSON or missing required fields (rare with forced tool use) | The pydantic ValidationError | Usually transient; retry |
| `Your prompt is missing or unclear on: count, goal` | The user's prompt didn't state those fields | The list of missing fields | Rewrite `prompt.md` so each field is explicit |
| `count must be between 1 and 50, got 100` | User requested too many | The error message | Lower the count |
| Low-quality scan extracts garbage | The PDF is an image scan with poor resolution | `name` is wrong, or many fields empty | Use a text-based PDF export, not a phone-camera scan |

All failures raise `IngestionError` (a subclass of `ScholarError`), caught by the CLI
and rendered as `Error: <message>` with exit code 1. No tracebacks for expected
failures.

## How to add a new field to ResumeData

For example, adding `awards: list[str]`:

1. Add the field to `ResumeData` in
   [scholarapp/modules/ingestion.py](../scholarapp/modules/ingestion.py).
2. Update [scholarapp/prompts/parse_resume.txt](../scholarapp/prompts/parse_resume.txt)
   with an extraction rule for the new field.
3. Update any downstream consumer — for the drafting step (Step 6), decide whether
   awards should appear in the condensed resume passed to the LLM.
4. If `awards` is required (not list-defaulted), update the test fixture
   `_VALID_RESUME_INPUT` in [tests/test_ingestion.py](../tests/test_ingestion.py).

The Pydantic schema is re-derived from the model on every call, so the Anthropic
tool definition picks up the new field automatically — no separate schema file to
keep in sync.

## How to test locally

### Unit tests (no API calls)

```bash
pytest tests/test_ingestion.py
```

These mock the Anthropic client and verify both response parsing and the request
shape (cache_control, document block, forced tool choice).

### Real-API smoke test

```bash
mkdir -p inputs
cp /path/to/your/resume.pdf inputs/resume.pdf
cat > inputs/prompt.md <<EOF
I want to email 5 computational neuroscience professors at west-coast universities
for a 30-minute chat about a research project I'm exploring.
EOF
cat > inputs/template.md <<EOF
Dear Prof. {last_name},

[your template here]

Best,
{your_name}
EOF
scholar run
```

Expected output:

```
Parsing resume: inputs/resume.pdf
Parsed resume for Jane Doe.
Parsing prompt: inputs/prompt.md
Parsed prompt: field='computational neuroscience', count=5, goal='30-min chat'
Parsed inputs. run_id=<uuid>
```

After the run completes, the inputs are copied to
`~/.scholarapp/runs/<run_id>/inputs/` for reproducibility, and the Run row is in the
DB. Verify with:

```bash
scholar list
scholar status <run_id>     # will show "(no drafts yet)" until Steps 4-6 land
```

The pipeline stops here — discovery (Step 4) is not yet wired up.
