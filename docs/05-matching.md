# 05 — Matching

For each professor that survived discovery, the matching step picks the 2–3 recent
works most relevant to the user's resume — and writes a one-sentence rationale for
each pick. Those picks are the "hook" the drafting step (Step 6) builds the email
around.

Implementation: [scholarapp/modules/matching.py](../scholarapp/modules/matching.py).

```python
async def match_projects(
    professor_name: str,
    projects: list[ProjectForMatching],
    user_interests: list[str],
    user_experiences: str,
) -> list[MatchedProject]
```

Plus a batch wrapper `match_projects_for_run(pairs, interests, experiences)` that
runs the above across all professors in parallel with a `Semaphore(10)`.

## Role in the pipeline

```
Discovery (Step 4)      Matching (Step 5)            Drafting (Step 6, pending)
─────────────────       ──────────────────           ───────────────────────────
Professor              ┌─ one Claude call ─┐
  + 5 recent works ───►│ pick 2–3 picks    │───► MatchedProject ───► Email body
                       │ + why_relevant    │       (with title +     ("I noticed
                       │ (Sonnet)          │        rationale)        your work
                       └───────────────────┘                          on X...")
```

The drafting step does **not** see the full project list — only the 2–3 picks plus
their rationales. So matching is the bottleneck that decides which professor work
ends up cited in the email. A bad match → a draft about something irrelevant.

## Prompt template

Full text: [scholarapp/prompts/match_projects.txt](../scholarapp/prompts/match_projects.txt).

Three rules carry the weight:

1. **Technical specifics over field-level overlap.** "Both are neuroscience" is not
   enough; "both use CNNs to segment 3D MRI volumes" is. Without this, the model
   defaults to keyword matching and the email reads generic.
2. **No invented overlap.** If a work is genuinely unrelated, omit it. Returning 0
   picks is fine — drafting will skip the professor in that case rather than write
   a vague message. This is the single most important rule; without it, every
   professor gets 3 picks and many are spurious.
3. **`why_relevant` ≤ 25 words, must name the concrete overlap.** No vague
   flattery, no "your interests align with this work." A reader should learn what
   the overlap actually is.

The model is also constrained to use only `project_id`s present in the input. We
additionally filter the response against the input set (drop hallucinated IDs).

## Pydantic schemas

```python
class ProjectForMatching(BaseModel):
    """Module input. CLI builds these from DB Project rows."""
    project_id: int          # DB id, opaque pass-through
    title: str
    url: str | None = None
    abstract: str | None = None
    year: int | None = None

class MatchedProject(BaseModel):
    """Module output. CLI persists as MatchedProject DB rows."""
    project_id: int          # echoes input project_id
    title: str               # echoed from input — saves the caller a lookup
    url: str | None = None
    why_relevant: str        # one sentence, ≤ 25 words target
```

Downstream (Step 6) usage:

- `title` and `url` → cited in the email body (so the professor can find the work).
- `why_relevant` → fed verbatim into the drafting system prompt as the "hook" — it
  defines what the email is actually *about*.

Internal schema `_MatchExtraction.matches` is bounded to `MAX_PICKS = 3`. The
model literally cannot return more without failing Pydantic validation, which the
caller raises as `MatchingError`.

## Why 2–3 (and not 1 or 5)

- **1 is too brittle.** If the single pick is weak or feels generic, the whole
  email is weak. With 2–3, drafting can lean on the strongest one or weave two
  together.
- **5 is too many.** The user message gets longer (more cost), and drafting has
  to compress more (each pick gets less weight). The reader can tell when an email
  mentions 5 unrelated works trying to look thorough.
- **2–3 matches the structure of a real cold email** — one specific hook with a
  second supporting reference.

`MAX_PICKS = 3` is a module constant. Adjust if a future experiment shows
otherwise.

## Model: Haiku (was Sonnet)

Matching uses `claude-haiku-4-5` (`MATCH_MODEL = MODEL_HAIKU` in the module).
Originally Sonnet — the argument was that distinguishing **surface overlap**
("both about neuroscience") from **real overlap** ("both use CNNs on MRI volumes")
needed Sonnet's judgment. In practice:

- The task is bounded: read 2–5 abstracts, pick 2–3 that overlap, write a
  one-sentence rationale per pick. This is extract-and-justify, not deep
  reasoning.
- The prompt's hard constraints — "technical specifics over field-level overlap",
  "do not invent overlap", "≤25 words naming the concrete overlap" — force
  specificity regardless of model. Both Haiku and Sonnet hit the same output
  shape; Haiku doesn't flatten in this regime.

Cost per professor: ~$0.003 (1,500 input + 200 output on Haiku) vs ~$0.008 on
Sonnet. For N=3 that's ~$0.009 instead of ~$0.024 added to the per-run total —
a ~$0.015 saving per run that scales with N.

**Bonus:** keeping matching off Sonnet frees the entire Sonnet ITPM budget for
drafting, which is the main 429 bottleneck on Anthropic Tier 1 accounts.

**To swap back to Sonnet:** change `MATCH_MODEL = MODEL_HAIKU` to
`MATCH_MODEL = MODEL_SONNET` in [matching.py](../scholarapp/modules/matching.py).
One-line revert. If you suspect Haiku is picking weak hooks, run a comparison
between two `DATA_DIR`s — same inputs, different `MATCH_MODEL` — and diff the
resulting `why_relevant` strings.

## Tuning relevance — current state and alternatives

Today the relevance threshold is **implicit in the LLM's judgment**. The prompt
tells the model to "omit unrelated works" and we trust it. There is no numeric
score we compare against a cutoff.

Alternatives if this proves too loose or too strict:

- **Explicit numeric rating.** Have the model return a `relevance: int (1–10)`
  for each pick; drop anything below a threshold. Easy to add (one schema field
  + one filter line). Adds calibration overhead — the model's "7" today may not
  match tomorrow's "7".
- **Embedding similarity.** Embed `user_interests + experiences` and each
  professor work; threshold on cosine. Removes the LLM from the pick, but
  introduces an embedding step (another API), and embedding similarity is a
  worse proxy for "would lead to a real conversation" than a Sonnet judgment.
- **Hybrid.** LLM picks; embedding score is logged alongside for retrospective
  analysis. Best of both for debugging.

None of these is implemented; flagging them so future you doesn't re-derive.

## The "0 matched" case

When no work overlaps meaningfully with the user's interests:

1. `match_projects` returns `[]` and logs a warning:
   `No projects matched for <name> — drafting will skip this professor.`
2. The CLI counts this professor toward the total but **no MatchedProject rows
   are written**.
3. The Run status still flips to `drafting`.
4. Step 6 (drafting) will see this professor has zero matched projects and
   **skip drafting an email** for them. The user ends the run with N – k draft
   files, where k is the number of skipped professors.

This is intentional: the alternative is writing a generic "I came across your
work and would love to chat" email, which has reply rates near zero. Better to
under-deliver than send a bad email under the user's name.

If you're seeing many skipped professors, the most likely cause is the field
being too broad — discovery returned authors who don't actually work on the
narrower topic the user cares about. Re-running with a tighter `field` in the
prompt usually fixes it.

## How to test locally

### Unit tests

```bash
pytest tests/test_matching.py -v
```

Tests cover: schema cap (≤3), hallucinated ID filtering, 0-pick warning + empty
return, request shape (cache_control, forced tool, user message contents), and
the batch parallelism wrapper.

### Real-API smoke

```bash
# Re-run the full pipeline; matching runs after discovery.
scholar run
```

Expected output now includes:

```
Discovered 3 professors. run_id=...
Matching projects for 3 professors...
Matched projects for 3 professors (3 with ≥1 match, 7 matches total). run_id=...

────────────────────────────────────────────────────────────────
  Claude usage
────────────────────────────────────────────────────────────────
  parse_resume           sonnet  in= ...   out= ...   $0.0...
  parse_prompt           haiku   in= ...   out= ...   $0.00...
  pick_topics            haiku   in= ...   out= ...   $0.00...
  extract_email × 9      haiku   in= ...   out= ...   $0.02...
  match_projects × 3     haiku   in= ...   out= ...   $0.00...
────────────────────────────────────────────────────────────────
  Total: $0.0XX
────────────────────────────────────────────────────────────────
```

Inspect what got picked:

```bash
sqlite3 ~/.scholarapp/scholar.db -header -column \
  "select p.name, pr.title, mp.why_relevant
   from matched_projects mp
   join projects pr on pr.id = mp.project_id
   join professors p on p.id = mp.professor_id
   where p.run_id = '<run_id>';"
```

If `why_relevant` reads like generic flattery, the prompt rules aren't biting hard
enough — tighten [match_projects.txt](../scholarapp/prompts/match_projects.txt) or
file an issue with the offending pick.
