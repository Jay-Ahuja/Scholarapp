# 09 — End-to-end smoke test

The unit tests in `tests/test_*.py` cover each module in isolation against
mocks. The smoke test exercises the **whole pipeline end-to-end** — `scholar
run` from inputs to drafts to the gated send — against a recorded vcrpy
cassette. It catches things the unit tests can't: stage-to-stage data flow,
DB session lifecycle across stages, CLI exit codes, sync semantics between
the DB and the markdown files.

Implementation:
- [tests/test_e2e.py](../tests/test_e2e.py) — replays the committed cassette.
- [tests/test_e2e_recording.py](../tests/test_e2e_recording.py) — opt-in,
  hits real APIs to refresh the cassette.
- [tests/conftest.py](../tests/conftest.py) — shared `e2e_setup` fixture
  (tmp DATA_DIR, dummy keys, fixture inputs copied in) and the vcrpy
  configuration.

## What's verified

For one `scholar run` against the fixture inputs:

| Assertion | What it catches |
|---|---|
| `scholar run` exits 0 | Any uncaught exception across the whole pipeline |
| Exactly 1 Run row with `status='review'` | Status machine advances correctly |
| Exactly 3 `Draft` rows with `status='pending_review'` | Drafting persisted everything |
| `scholar review` writes exactly 3 `.md` files | Step 7 round-trips DB → disk |
| Each draft body ≥ 50 words | Drafting didn't produce a stub or refuse |
| Each draft references the title of at least one matched project | The hook from matching actually made it into the email — guards against drafting going generic |
| `scholar send <run_id>` exits 1 with "Sending disabled" | The `SEND_ENABLED=false` gate works |
| Zero `gmail.googleapis.com` URLs in the cassette | Hard proof no email left the machine |

## What's deliberately NOT verified

- **Real Gmail send.** Step 8 has its own unit test (mocked Gmail service)
  that exercises the post-gate code path. Actually sending email is
  intentionally out-of-scope until the user flips the gate per
  [docs/08-delivery.md](08-delivery.md).
- **Real OpenAlex / Tavily availability.** The cassette is the source of
  truth at test time. Real-API health is checked by re-recording (see
  below).
- **Real Anthropic API latency.** Cassettes replay in milliseconds.
- **Email quality / reply rate.** No automated test can score that. The
  draft-content assertions (length, project-title reference) are proxies
  that catch the worst regressions but won't tell you if the tone is off.

## Fixtures

In [tests/fixtures/](../tests/fixtures/):

| File | Why |
|---|---|
| `sample_resume.pdf` | ~1.2 KB hand-built valid PDF (one page, plain text). Stable byte-for-byte across machines so cassette replay works. Describes a fictional CMU CS undergrad interested in CNN-based brain extraction — chosen to match the prompt and produce plausible matches against neuroimaging professors. |
| `sample_prompt.md` | Asks for 3 computational-neuroscience professors, 30-min chat goal, prefers neuroimaging / segmentation / applied-ML researchers. Minimum content that produces a usable run. |
| `sample_template.md` | Generic cold-email template in the fictional student's voice with `[bracketed slots]` for the drafting model to personalize. |

If you change a fixture, the existing cassette will probably stop matching
(see [Troubleshooting](#troubleshooting) below).

## Required env vars (for recording only)

The default `pytest` run never touches the network. Only `test_e2e_recording.py`
calls real APIs, and only when you opt in via `pytest -m record`.

Set BEFORE running the recording test:

```bash
export ANTHROPIC_API_KEY=sk-ant-...       # required, real key
export TAVILY_API_KEY=tvly-...            # required, real key
```

If the test sees placeholder values (anything starting with `test-` or shorter
than 20 chars), it skips with a clear message — defensive against accidentally
recording cassettes against the dummy keys `e2e_setup` installs.

Cost of one recording run: ~$0.05 of Anthropic credits + ~9 Tavily search
requests + ~12 OpenAlex requests + zero Gmail calls.

## How vcrpy works here

[tests/conftest.py](../tests/conftest.py) builds the `VCR` instance shared by
both e2e tests:

```python
vcr.VCR(
    cassette_library_dir="tests/cassettes",
    record_mode="none",     # strict replay; recording fixture overrides to "all"
    match_on=["method", "scheme", "host", "port", "path", "query"],   # body excluded — see below
    filter_headers=[
        ("authorization", "REDACTED"),
        ("x-api-key", "REDACTED"),
        ("anthropic-version", None),
    ],
    before_record_request=_redact_request,    # strips api_key from Tavily JSON bodies
)
```

### Where cassettes live

```
tests/cassettes/
└── test_full_pipeline_no_send.yaml    # committed once recorded
```

The directory is committed (via `.gitkeep`) so vcrpy has somewhere to write.
Cassettes themselves are committed normally — they're the source of truth
for the replay test.

### What's redacted

- `authorization` request header (Bearer tokens)
- `x-api-key` request header (Anthropic key)
- `anthropic-version` header — not sensitive, just noise
- `api_key` field inside Tavily's JSON POST body

The PDF base64 stays in the cassette — it's roughly 1.6 KB of base64 (from a
1.2 KB PDF) and matching depends on it being stable.

### Why `match_on` excludes `body`

Two reasons:

1. **PDF bytes.** The resume request includes ~1.6 KB of base64. If vcrpy
   matched on body byte-for-byte, any change to the fixture PDF would break
   replay. We commit a stable PDF so this is mostly theoretical — but
   excluding body is still a safer default.
2. **JSON ordering.** Anthropic SDK sometimes re-serializes dicts in
   different orders across versions. Body-matching would create false
   mismatches.

The downside: two requests with the same URL but different bodies (e.g.,
multiple Tavily POSTs in a single run) match against each other only by
order. We mitigate by setting `ANTHROPIC_CONCURRENCY=1` in `e2e_setup` so
the pipeline serializes its requests — stable order on replay.

## Re-recording the cassette

When to re-record:

- A fixture changed (resume.pdf, prompt.md, template.md)
- A prompt template changed (any `scholarapp/prompts/*.txt`)
- The Anthropic SDK was upgraded and the request shape moved
- OpenAlex or Tavily changed their response format
- You added a new pipeline stage that issues HTTP

```bash
# 1. Make sure your env has real keys (NOT the placeholders e2e_setup uses)
echo "$ANTHROPIC_API_KEY" | head -c 20   # should look like "sk-ant-api03-..."
echo "$TAVILY_API_KEY" | head -c 20      # should look like "tvly-..."

# 2. Re-record
pytest -m record tests/test_e2e_recording.py

# 3. Verify the regular e2e test now passes against the new cassette
pytest tests/test_e2e.py

# 4. Inspect the cassette before committing — look for any unredacted secrets
less tests/cassettes/test_full_pipeline_no_send.yaml

# 5. Commit
git add tests/cassettes/test_full_pipeline_no_send.yaml
git commit -m "refresh e2e cassette"
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Cassette not yet recorded at ...` (test skips) | First run on a fresh checkout | Run the recording test once: `pytest -m record tests/test_e2e_recording.py` |
| `CannotOverwriteExistingCassetteException` | A request was made that doesn't match any cassette entry — usually a refactor changed URLs/params | Re-record |
| `Set ANTHROPIC_API_KEY to a real key...` (recording test skips) | Env still has `e2e_setup`'s placeholder, or you forgot to export real keys | Export real keys in the shell you run pytest in |
| Test fails on draft body length | Drafting prompt got tighter, or output_tokens cap got lower | Either accept the new shape (loosen the assertion) or revert the prompt change |
| Test fails on "body does not reference any matched project title" | Drafting started writing generic emails — real regression | Inspect `tests/cassettes/...yaml` to see the actual draft body; debug the drafting prompt |
| Cassette file is enormous (> 1 MB) | Maybe the PDF got embedded multiple times, or we recorded with resume_cache populated AND empty, or response bodies aren't being decoded | Re-record from scratch; check `decode_compressed_response=True` is still in conftest |
| `gmail.googleapis.com` in the cassette | The SEND_ENABLED gate failed — serious bug | DO NOT commit the cassette; investigate `delivery.send_approved` |

## How to extend

The most common addition is a new assertion. Example: "draft references at
least one user experience from the resume":

```python
# In test_e2e.py, after the existing assertions:
resume_experience_phrases = ["MRI segmentation", "CNN", "brain extraction"]
for d in drafts:
    body_lower = d.body.lower()
    assert any(phrase.lower() in body_lower for phrase in resume_experience_phrases), (
        f"draft {d.id}: body doesn't reference any resume experience"
    )
```

If you add a stage that issues new HTTP, you'll need to re-record the cassette
to capture those requests.

## Pre-merge checklist

Before merging a change that touches `scholarapp/modules/*` or the prompts:

- [ ] `pytest` passes (unit tests, no network)
- [ ] `pytest tests/test_e2e.py` passes against the committed cassette
- [ ] If you changed a prompt or a fixture: re-record the cassette and
      visually inspect ≥ 1 generated draft for sanity
- [ ] At least weekly: a full `pytest -m record` run against real APIs, to
      catch silent upstream changes (OpenAlex field renames, Tavily response
      format drift, etc.)

After Step 9, the project is feature-complete per the original plan:
parse → discover → match → draft → review, with a gated Gmail send, end-to-end
test infrastructure, and full documentation. The only thing intentionally
turned off is real outbound email.
