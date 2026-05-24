# 04 — Discovery

Given a research field and a target count, discovery returns up to N professors at
academic institutions with their recent works and a verified academic email. Implementation:
[scholarapp/modules/discovery.py](../scholarapp/modules/discovery.py).

```python
async def find_professors(
    field: str,
    count: int,
    user_interests: list[str],
) -> list[ProfessorCandidate]
```

`user_interests` is accepted but not yet used — actual relevance ranking happens in
Step 5 (matching). Today it's a forward-compatible knob for future "narrow the author
pool by interest" logic.

## Data-flow diagram

```
field (str) ──┐
              ▼
    ┌─────────────────────┐
    │ GET /topics         │  OpenAlex
    │ ?search=<field>     │
    └─────────┬───────────┘
              │  candidates: [{id, display_name, field, subfield, keywords, ...}]
              ▼
    ┌─────────────────────┐
    │ Claude: pick 1-2    │  prompts/pick_topics.txt → _TopicPick
    └─────────┬───────────┘
              │  topic_ids: [T10077, ...]
              ▼
    ┌─────────────────────┐
    │ GET /authors        │  filter=topics.id:T10077|T11601,
    │  &sort=cited_by     │         last_known_institutions.type:education,
    │                     │         works_count:>10
    └─────────┬───────────┘
              │  ~3×count authors (over-fetch)
              ▼
    ┌─────────────────────┐
    │ Dedup within run    │  _dedup_authors — collapse duplicate
    │ (name, inst, dept)  │  professors BEFORE any paid lookup
    └─────────┬───────────┘
              │  unique authors, first-seen order preserved
              ▼
    For each author, in parallel (semaphore=10):
    ┌────────────────────────────┐       ┌────────────────────────────┐
    │ GET /works                 │       │ POST tavily.com/search     │
    │  ?filter=author.id:Axxx    │       │  "<name>" "<inst>" faculty │
    │  &sort=publication_date    │       │   email                    │
    └────────────┬───────────────┘       └─────────┬──────────────────┘
                 │ recent_works                    │ search results
                 ▼                                 ▼
                                          ┌────────────────────────┐
                                          │ Claude: extract email  │
                                          │ prompts/extract_email  │
                                          └─────────┬──────────────┘
                                                    │
                                                    ▼
                                             email + faculty_page_url
                          (or None — candidate gets dropped downstream)
              ▼
    Drop candidates where email fails the academic-TLD allowlist.
    Take the first `count` survivors. Done.
```

## OpenAlex endpoints used

All requests carry `User-Agent: Scholarapp/0.1 (mailto:scholarapp@example.com)` —
OpenAlex routes mailto-identified clients into the "polite pool" with better rate
limits.

| Endpoint | What we ask | Filter / sort |
|---|---|---|
| `GET /topics` | Candidate topic IDs for a free-text field | `?search={field}&per_page=10` |
| `GET /authors` | Top-cited academic authors in those topics | `?filter=topics.id:{T…}\|{T…},last_known_institutions.type:education,works_count:>10&sort=cited_by_count:desc` |
| `GET /works` | 5 most recent works per author | `?filter=author.id:{A…}&sort=publication_date:desc&per_page=5` |

Reference docs:

- Topics entity: https://docs.openalex.org/api-entities/topics
- Authors filtering: https://docs.openalex.org/api-entities/authors/filter-authors
- Works filtering: https://docs.openalex.org/api-entities/works/filter-works
- Polite pool: https://docs.openalex.org/how-to-use-the-api/api-overview#the-polite-pool

**Why topics, not concepts:** OpenAlex deprecated the `concepts.id` filter for authors
in late 2024 — querying it returns 0 results even for valid concept IDs. The replacement
is the topics taxonomy, organized as **domain > field > subfield > topic**. Topics
themselves don't have a numeric level; instead, their position is defined by the
hierarchy above. The LLM-pick step uses `field` + `subfield` as the strongest signal
of relevance.

## Why two sources (OpenAlex + Tavily) instead of one

- **OpenAlex** has clean, queryable, structured data on authors, institutions, and
  works. It's a real API, free, polite-pool friendly, and uses stable IDs.
- **OpenAlex does not reliably expose contact email.** A small fraction of author
  records have an email field, and even then it's often outdated or institutional
  aliases.
- **Tavily** is a search API designed for LLM consumption (it returns clean snippets,
  not raw HTML). We use it as a lightweight "search the open web for the professor's
  email" step, then let Claude pull the address out of the top results.
- Alternatives we ruled out:
  - **Scraping Google Scholar:** against ToS, fragile, gets rate-limited fast.
  - **University faculty-page scraping:** every school has a different layout; the
    LLM-via-Tavily path generalizes for free.
  - **Apollo / Hunter:** commercial email-finders are accurate for industry
    contacts but weak for academics, and have per-seat pricing we don't want.
- Bottom line: OpenAlex for what's clean and structured; Tavily + LLM for the messy
  bit (contact info). One source for each side of the problem.

## Field → topic resolution

We send the user's free-text field (e.g., `"neuroscience"`, `"robotics"`,
`"computational social science"`) to `/topics?search=...`. OpenAlex returns up to
10 candidates, each with `display_name`, `description`, `keywords`, and the
hierarchy `domain > field > subfield`.

We then ask Claude to pick the best 1–2 ([prompts/pick_topics.txt](../scholarapp/prompts/pick_topics.txt)).
Why use an LLM here at all?

- **Topics are narrow.** Each OpenAlex topic is one cluster of related papers — there
  are ~4,500 of them. A search for "neuroscience" returns topics like "Neuroscience
  and Neuropharmacology Research", "Neuroscience and Neural Engineering", etc. A
  bare keyword match is not enough to pick well.
- **The hierarchy is the strongest signal.** Two topics named "Neural Computation"
  might sit in field=Neuroscience or field=Computer Science. The LLM uses the
  `field`/`subfield` columns to keep picks coherent with the user's intent.
- **Breadth vs. precision.** "neuroscience" usually wants both cellular/molecular and
  cognitive/systems neuro; "computer vision" usually wants a single tight topic.
  The LLM is told to pick 1 when one clearly dominates, 2 when the user's term spans
  two sub-areas.
- **Robust to typos / synonyms.** "ML" → "Machine learning"; "AI" → both AI and ML.

The LLM is constrained to pick from the IDs we passed in (`select_topics` tool with
a `topic_ids` field; we additionally filter out any returned IDs we didn't provide,
as a defense against hallucination).

### Interest-aware topic picking

The user's `interests` (extracted from their résumé) flow through `find_professors`
→ `_resolve_topics` → `_llm_pick_topics` and into the model's user message. The
[pick_topics.txt](../scholarapp/prompts/pick_topics.txt) prompt instructs the model
to treat interests as the **strongest signal** when present — overriding a more
literal match against the bare `field` string.

Interests also feed into the **search itself**, not just the pick. OpenAlex's
`/topics?search=` is exact-phrase: multi-word queries like "computational
neuroscience" frequently return zero results because no topic is literally named
that. To work around this, `_resolve_topics` issues one search per (field +
interest), runs them in parallel, de-dupes by topic id, and passes the merged
candidate set to the LLM. So even when the field string itself is too composite,
shorter interest keywords like "neuroimaging" or "MRI segmentation" surface the
right topics. Capped at `_MAX_INTEREST_QUERIES = 5` interests, `_MAX_TOPIC_CANDIDATES = 15`
in the merged set to keep the LLM prompt small.

Why this matters: a user with `field="neuroscience"` and interests `["CNN",
"MRI segmentation", "brain extraction"]` should land on topic
`T11601` (Neuroscience and Neural Engineering), not `T10077` (Neuroscience and
Neuropharmacology Research). Without the interests signal, the LLM picks the most
literal-sounding match — usually the broad clinical / cellular topic — and
discovery returns the wrong *kind* of neuroscientist. Matching (Step 5) then
correctly returns 0 picks for everyone, and the user pays for a useless run.

Pass `user_interests=[]` (the default) to fall back to field-only picking.

## Over-fetch then filter

We fetch `count * 3` authors from `/authors` and try to enrich every one in
parallel. Why over-fetch?

- **~20–30% of candidates lose email** in the Tavily + LLM step (no faculty page
  surfaced; results are in PDFs not crawled; email obfuscated as "name [at] mit
  [dot] edu"; etc.).
- Without over-fetching, a 10-professor run regularly comes back with 6–7.
- 3x is a starting point; if we see consistently higher drop rates, raise to 4x or
  add a second Tavily query pattern. The constant lives at
  `discovery.OVERFETCH_MULTIPLIER`.

If fewer than `count` survive, `find_professors` logs a warning and returns what
we have rather than failing — partial results are still useful.

## Within-run deduplication

OpenAlex sometimes surfaces the same professor more than once in a single
`/authors` response (e.g., near-duplicate author records, or the same person under
slightly different formatting). We collapse those duplicates **after** the
over-fetched pool comes back from `/authors` and **before** the per-author
enrichment fan-out (`_enrich` → Tavily web search + Claude email extraction). The
call site is a single line in `find_professors`:

```python
authors = _dedup_authors(authors)
```

**Why before enrichment — the cost rationale.** Enrichment is the only paid step in
discovery (one Tavily request + one Claude `_llm_extract_email` call per candidate).
If a duplicate slipped through, we'd pay for that professor's lookup twice and could
return them twice. Deduping first guarantees each distinct professor is looked up at
most once.

**Identity key** (`_dedup_key`, built from `_author_identity`): two candidates are
the same professor when their **name, institution, AND department** all match.
Department comes from the first `last_known_institutions` entry when OpenAlex
supplies one — most author records don't carry it. When department is absent the key
falls back to **(name, institution)** alone. Matching is case- and
whitespace-insensitive (`" ".join(s.split()).casefold()`), so trivial formatting
differences don't defeat the dedup. First-seen order is preserved.

`_author_identity` reads name and institution exactly the way `_enrich` does, so the
dedup key and the enriched candidate stay consistent.

**No-identity records pass through.** A candidate with an empty name AND empty
institution has no usable identity. Rather than collapse all such records into one,
`_dedup_authors` keeps each as-is — losing distinct candidates is worse than letting
a rare signal-free record through to enrichment (where it'll likely drop on email
validation anyway).

**Scope: in-memory, single run only.** The `seen` set lives for the duration of one
`find_professors` call. There is **no cross-run dedup and no schema change** — a
professor discovered in an earlier run is not remembered here. If you want
persistent de-duplication across runs, that's a separate, database-backed feature
(query existing `professors` rows before enriching).

**No backfill.** Dedup runs after the `count * 3` over-fetch, so it normally removes
only a handful of dupes from a deliberately oversized pool. If it ever leaves fewer
than `count` unique professors, that's acceptable — we do **not** re-query OpenAlex
to top the pool back up. Returning fewer is the same partial-results contract as the
email-validation drop above.

When any duplicates are dropped, `_dedup_authors` logs at INFO:
`Deduplicated N author(s) within run (U unique of T).`

## Tavily query construction

Current query:

```
"<professor name>" "<institution>" faculty email
```

Notes on the pattern:

- Double-quoting both name and institution forces exact-phrase matching — without
  quotes Tavily often returns pages about other people who share a surname.
- "faculty email" steers toward directory and personal pages; it's a small loss for
  professors whose page doesn't say "faculty" but a big win on average.

Alternatives considered and not used:

- `"<name>" "<institution>" CV` — surfaces academic CVs but often without email.
- `<name> <institution> site:.edu` — site-restricted searches sometimes miss the
  professor's actual department (e.g., adjuncts at hospitals).
- Constructing the email pattern from the name and guessing
  (`first.last@university.edu`): plausible but produces silent failures — we'd
  email the wrong person. The Tavily + Claude pipeline only returns an address it
  actually saw on a page.

If a professor needs a second pass, the natural extension is to add a fallback
query (`"<name>" <institution> "@" .edu`) and concatenate results — the
`_resolve_email` function is a single place to add that.

## Email validation: the academic-TLD allowlist

In [scholarapp/modules/discovery.py](../scholarapp/modules/discovery.py):

```python
ACADEMIC_EMAIL_SUFFIXES = (
    ".edu",
    ".edu.au", ".edu.cn", ".edu.hk", ".edu.sg", ".edu.tw",
    ".ac.at", ".ac.be", ".ac.cn", ".ac.il", ".ac.in",
    ".ac.jp", ".ac.kr", ".ac.nz", ".ac.uk", ".ac.za",
)
```

A candidate's email is dropped if its domain does not end in one of these suffixes.
This is enforced both by the `is_academic_email` filter in `find_professors` and
by a `field_validator` on `ProfessorCandidate.email`, so any bypass attempt would
fail Pydantic validation.

**Known gap:** continental Europe doesn't use a unified academic suffix.
`uni-heidelberg.de`, `ethz.ch`, `polytechnique.fr` are all legitimate but don't end
in `.edu` or `.ac.*`. To extend:

1. Add the country's TLD or a known per-institution suffix to
   `ACADEMIC_EMAIL_SUFFIXES`.
2. If the country uses bare ccTLDs (`.de`, `.fr`) for universities, a TLD-only
   allowlist is too coarse — switch to an institution-domain allowlist instead
   (load a list from `data/academic_domains.txt`).
3. Re-run the unit test `test_is_academic_email` with new parametrize cases.

This is intentionally conservative — false positives (emailing a non-academic) are
worse than false negatives (losing a legitimate candidate).

## Rate-limit notes

- **OpenAlex** has no published per-day limit for polite-pool users, but it
  recommends keeping concurrent requests modest. The per-stage Anthropic
  concurrency semaphore (`settings.anthropic_concurrency`, default **3**) also
  governs the per-author OpenAlex fan-out — well below anything they'd throttle.
- **Tavily free tier** is around 1,000 requests/month at the time of writing. Each
  discovery run uses `count * 3` Tavily requests (one per candidate). A 10-person
  run = 30 requests. You can do ~30 runs/month on the free tier.
- **Anthropic** is governed by your account's RPM/TPM. Each run does 1
  `_llm_pick_topics` call + `count * 3` `_llm_extract_email` calls (one per
  candidate). For a 10-person run that's ~31 calls. **Both helpers use
  `claude-haiku-4-5`** (constant `discovery.MODEL_HAIKU`) — they're narrow
  extraction tasks where Haiku performs at parity with Sonnet for ~⅓ the cost.
  A 3-professor run lands at ~$0.025 of Claude spend.
- The `_request_with_retry` helper retries 429 + 5xx with exponential backoff (1s,
  2s, 4s) and honors `Retry-After`. Three attempts max; the third failure raises.

## Failure modes

| What you see | Cause | What to do |
|---|---|---|
| `OpenAlex returned no topics for field 'comp neuro'` | Field is too colloquial | Try the standard term: `"computational neuroscience"` |
| `Could not match field 'X' to an OpenAlex topic` | Topics found but LLM rejected all | The pick prompt got too strict — try a synonym, or relax the prompt |
| `OpenAlex returned no authors for field 'X'` | Topic matched but no qualifying authors | Topic is too narrow / too new; broaden the field |
| `Wanted N professors but only M survived...` | Email resolution drop rate higher than expected | Either accept M, raise OVERFETCH_MULTIPLIER, or improve the Tavily query |
| `ANTHROPIC_API_KEY is not set` / `TAVILY_API_KEY is not set` | Missing env var | Add to `.env` |
| `Tavily returned 429 (rate limit / quota exhausted)...` | Monthly Tavily quota used up (free tier ~1000/month) | Wait until next billing cycle, lower the requested count, or upgrade. A run uses `count × 3` Tavily requests. |
| Retried HTTP 429/503 still failing (OpenAlex) | Upstream actually unavailable | Wait + retry the run; check OpenAlex status |

**Tavily rate-limit handling:** when Tavily returns 429 (almost always monthly
quota exhausted), discovery aborts immediately rather than burning more requests.
A module-level `asyncio.Event` circuit-breaks sibling fan-out tasks so the
console doesn't flood with retry warnings — you'll see one clean error message
and `scholar run` exits with code 1.

All errors are `DiscoveryError` (subclass of `ScholarError`), caught by the CLI
and rendered as `Error: <message>`.

## What to do when the field is too narrow or too broad

**Too narrow** (`/topics` returns 0 results, or all candidates' `field` is unrelated):

- The user's term is too colloquial or too specific. Either rename the field to a
  more standard form ("microscopy" instead of "two-photon microscopy"), or edit
  `prompts/pick_topics.txt` to be more forgiving when a candidate's `field` partially
  matches.

**Too broad** (e.g., user types "biology" and we get 50,000 authors):

- `/authors?sort=cited_by_count:desc&per_page=N` already returns the top N by
  citations, so the breadth doesn't blow up costs — but the relevance to the user's
  interests likely will. The matching step (Step 5) will drop most of them, leading
  to many empty drafts.
- Mitigation: `pick_topics.txt` already steers Claude toward topics whose `field`
  matches the user's phrasing (vs. broader siblings). If the user genuinely wants
  "biology" we still pick a topic in field=Biology, but downstream matching will
  trim.

## Extension points

To add a new academic data source (e.g., Semantic Scholar):

1. Add a fetcher: `async def _list_authors_semantic_scholar(http, field, count)`
   returning the same `dict` shape used by the OpenAlex path.
2. In `find_professors`, after `_list_authors`, merge `_list_authors_semantic_scholar`
   results, then run them through the existing `_dedup_authors` (which already
   de-dupes by name + institution + department) before enrichment — that way a
   professor surfaced by both sources is enriched once.
3. Update `docs/04-discovery.md` (this file) and add tests with fixture JSON for
   the new source's response shape.
4. New env var for any API key needed; add to `config.py` and `.env.example`.

To swap Tavily for a different search provider:

1. Add a `_brave_search` (or whatever) function with the same signature as
   `_tavily_search` — `(http, key, query) -> list[dict]` with at least `title`,
   `url`, `content` fields.
2. Replace the call in `_resolve_email`. Or, for A/B testing, race both with
   `asyncio.gather` and merge.

## Local testing without API quota

Unit tests use a `FakeAsyncClient` that returns canned JSON keyed by `(method, url
prefix)`. The two Claude helpers are monkeypatched directly. No real API calls.

```bash
pytest tests/test_discovery.py -v
```

To exercise the full path against real APIs (Step 9 will set this up properly via
cassettes), set both `.env` keys and run:

```bash
scholar run --inputs inputs/
```

This will:

1. Parse the inputs as in Step 3
2. Update the Run status to `discovering`
3. Fan out to OpenAlex + Tavily + Claude
4. Persist `Professor` + `Project` rows
5. Update the Run status to `matching`
6. Print `Discovered N professors. run_id=<uuid>`

You can then inspect the rows:

```bash
scholar status <run_id>
sqlite3 ~/.scholarapp/scholar.db \
    'select name, institution, email from professors where run_id="<uuid>";'
```

Note: a real run costs a handful of Tavily quota and a few cents of Claude usage.
Don't loop it on accident.
