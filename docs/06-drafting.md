# 06 — Drafting

Drafting takes everything the pipeline has assembled — the user's resume, the
discovered professor, the matched projects, the user's goal — and writes a single
personalized cold email per professor. This is the artifact the whole pipeline is
built around.

Implementation: [scholarapp/modules/drafting.py](../scholarapp/modules/drafting.py).

```python
async def draft_email(
    template: str,
    resume: ResumeData,
    professor: ProfessorForDrafting,
    matched: list[MatchedProject],
    goal: str,
    considerations: str,
) -> EmailDraft
```

Plus a batch wrapper `draft_emails_for_run(template, resume, requests, goal,
considerations)` that runs the above across all professors in parallel
(`Semaphore(10)`).

## The system/user split (and why it matters for caching)

This module is the first place prompt caching actually engages, and the cache hit
is the whole reason the cost is bearable. The split:

```
SYSTEM (cache_control=ephemeral, IDENTICAL across all N professors):
    - draft_email.txt (the rules)
    - "---" delimiter
    - The user's template (verbatim)
    - "---" delimiter
    - Condensed resume (name, education, key experiences, skills, interests, pubs)
    Total: ~1500–2000 tokens, depending on resume + template size.

USER (uncached, DIFFERENT per professor):
    - professor.name, .institution, .email
    - matched projects with why_relevant
    - goal, considerations
    - "Draft the email now."
    Total: ~400–600 tokens.
```

Why this split:

- The system content **doesn't change** as we iterate through the N professors in
  a run. Re-sending it N times at full cost is wasteful.
- Anthropic's `cache_control: ephemeral` marks the system prompt as cacheable for
  ~5 minutes. The first per-professor call writes the cache (25% premium on
  those tokens). The N−1 subsequent calls within the run hit the cache and pay
  10% of normal input cost on the cached tokens.
- The user message is irreducibly per-professor, so it's never cached.

For Sonnet, the cache minimum is ~1024 tokens — the system prompt here clears
that comfortably even for short templates.

### Expected cache_read ratio

For an N-professor run: the cache should write once (call #1) and read N−1 times
(calls #2 through #N). After every run finishes you'll see this in the usage
summary:

```
  draft_email × 3      sonnet  in= 1500  cache= 3000  out= 1200  $0.02XX
                                ▲         ▲
                                |         └── cache_creation(call#1) + cache_read × 2 reads
                                └── per-call user message tokens × 3
```

The `cache` column is `sum(cache_creation + cache_read)`. As N grows the cache
read share grows toward 1.0; for N=10 you're paying full price on the cached
tokens for ~10% of the work and the rest at the discounted rate.

## Drafting rules (the full prompt)

Full text in [scholarapp/prompts/draft_email.txt](../scholarapp/prompts/draft_email.txt).
The six rules, annotated:

1. **Reference a specific project by something concrete.** Forbids "I saw your
   paper on X" — that pattern reads as form-letter. The `why_relevant` text
   from matching (Step 5) is the seed; the drafter is told to expand it into a
   concrete reference (a finding, a method, a question the work raised).

2. **Ground the connection in real resume experience.** The most common
   failure mode of LLM email writers is inventing plausible-sounding
   experience. This rule pins all claims to the `resume` section of the system
   prompt. If the resume doesn't show enough, the drafter writes one sentence
   less rather than inventing.

3. **Calibrate the ask to the goal's friction.** A 30-minute chat is low-
   friction — direct ask is fine. A lab position is high-friction — never ask
   directly; build the case for fit and invite further conversation. Wrong
   friction is the second most common failure mode (asking for a postdoc in
   the first paragraph kills the reply rate).

4. **Length: body 120–180 words, subject under 8 words.** Empirical: longer
   bodies don't get read, shorter bodies miss the specifics. Subject under 8
   words because professor inboxes are crowded and long subjects get
   truncated.

5. **Tone: respectful, specific, not sycophantic.** The prompt explicitly
   bans the worst patterns ("I hope this email finds you well", "your work is
   groundbreaking", "I would be honored", "As someone deeply passionate
   about…"). These tank credibility instantly.

6. **Do not promise capabilities the resume doesn't support.** No "I'm
   excellent at X" unless X appears with evidence in the resume.

The system prompt also instructs the drafter to use the user's `template` as
the **stylistic reference for voice** — not as a literal template to fill in.
This means the user's own writing patterns (formal vs casual, long vs short
greetings, etc.) carry through, and we don't impose a house style.

## Good vs bad draft — same inputs

Inputs: user is Jay Ahuja, Carnegie Mellon senior, project is a CNN brain-
extraction algorithm. Professor: Karl Friston @ UCL. Matched project: "Probabilistic
segmentation in SPM" with why_relevant = "Both handle partial-volume effects in
cortical boundaries." Goal: 30-minute chat.

### Good

> Subject: Question on partial-volume handling in SPM
>
> Dear Prof. Friston,
>
> I'm Jay Ahuja, a senior at Carnegie Mellon working on a CNN-based brain
> extraction algorithm. I read your work on probabilistic segmentation in SPM —
> the way it handles partial-volume effects in cortical boundaries directly
> addresses a problem my U-Net training plateaus on. I've benchmarked a
> Dice-loss + boundary-weighted regularizer against FastSurfer and consistently
> see the same partial-volume artifacts your approach is built to mitigate.
>
> Would you be open to a 30-minute chat in the next few weeks? I'd love to walk
> through my current approach and hear how you'd extend probabilistic priors to
> a CNN setting. Happy to work around your schedule.
>
> Best,
> Jay

What makes it good: names a specific *technique* in the professor's paper, ties
to a specific stuck-point in the user's own work (grounded in resume), the ask
is direct and proportional to "30-min chat," tone is plain.

### Bad

> Subject: Following up on your incredible research
>
> Dear Prof. Friston,
>
> I hope this email finds you well. I'm a senior at Carnegie Mellon and I came
> across your fascinating work in neuroimaging. As someone deeply passionate
> about computational neuroscience and machine learning, your research truly
> aligns with my interests.
>
> Your contributions to the field have been groundbreaking. I would love to
> learn more about how someone like you approaches these complex problems. I
> have done some work with neural networks myself and there could be wonderful
> synergies between our interests.
>
> Would you possibly be willing to spare some time for a chat? I would be
> honored to learn from you. Any time would work for me.
>
> Best regards,
> Jay

What makes it bad: every banned phrase from rule 5 in the first paragraph; no
specific reference to *any* paper; "neural networks" without naming the project
or method; the ask sounds desperate ("any time would work"). A professor sees
this once a day and deletes it.

The prompt rules are tuned to push hard against this style.

## Tuning tone / length / ask-friction

Currently the rules are baked into [draft_email.txt](../scholarapp/prompts/draft_email.txt).
To experiment:

- **Tone:** Edit the "Hard bans" list in rule 5. Adding bans is safer than
  loosening them — Claude is conservative when in doubt.
- **Length:** Change the "120–180 words" in rule 4. Going below 100 tends to
  produce drafts that omit the grounding; going above 200 starts to ramble.
- **Ask friction:** Rule 3 has three goal categories (chat / lab position /
  networking). If your real goal doesn't fit those, add a new category line
  and the drafter will use it. The prompt explicitly tells the drafter to read
  `considerations` for overrides.

All three knobs are in plain text in the prompt file — no code change. The
drafting tests don't pin specific tone or length; they only check the request
shape, so prompt tweaks won't break tests.

## "No matched projects" handling

If a professor's matched list is empty (matching couldn't find genuine overlap),
drafting **skips them** rather than writing a generic email.

Why not fall back to a generic "I came across your work" email?

- Generic emails have ~0% reply rate. They're worse than no email.
- The user's name is attached. Bad emails damage the user's reputation with
  that professor more than no email does.
- The pipeline already paid for discovery + email lookup on this candidate;
  losing the cost is cheaper than losing the relationship.

Implementation:

- `draft_email(...)` raises `DraftingError("No matched projects for {name}")`
  if called with an empty `matched` list. Safety net for callers who forgot
  to pre-check.
- The CLI pre-checks: it iterates professors, builds a `DraftRequest` only for
  those with matches, prints `skipping {name} — no matched projects` for the
  rest, and never calls drafting for them.
- Run status still flips to `review` after drafting, with however many drafts
  did succeed.

If you see many skipped professors, the diagnosis is upstream — matching
returned zero picks for them, usually because discovery picked the wrong topic.
See [docs/04-discovery.md](04-discovery.md#interest-aware-topic-picking) for the
fix.

## A/B testing prompt changes safely

Today there's no built-in A/B harness. To compare two prompt variants:

1. Pick a fixed run input — a copy of your `inputs/` directory.
2. Set `DATA_DIR=/tmp/scholar-baseline` and run `scholar run`. The drafts land
   in the baseline DB.
3. Edit [draft_email.txt](../scholarapp/prompts/draft_email.txt).
4. Set `DATA_DIR=/tmp/scholar-variant` and run `scholar run` against the same
   input directory.
5. Inspect both:
   ```bash
   sqlite3 /tmp/scholar-baseline/scholar.db 'select subject, body from drafts;'
   sqlite3 /tmp/scholar-variant/scholar.db 'select subject, body from drafts;'
   ```

The resume cache (Step 3 doc) hits across DBs only if you copy the cache table
between them — usually you want a fresh DB per variant so parse_resume runs
again and you're comparing apples-to-apples on the whole pipeline. The discovery
results aren't deterministic (OpenAlex returns the same authors but Tavily +
LLM email extraction has variability), so for tight A/B comparisons it helps to
fix the run by reusing the same discovered Professor + matched rows across
variants.

Future improvement: a `scholar draft --rerun <run_id>` command that re-drafts
existing professors+matches with the current prompt, leaving discovery + matching
untouched. Not built yet.

## Cost per run

Per draft, on Sonnet 4.6 ($3/M input, $15/M output, cache write $3.75/M, cache
read $0.30/M):

| Component | Tokens | Cost (first call) | Cost (call ≥ 2 in same run) |
|---|---|---|---|
| System (cached) | ~1700 | $0.00638 cache write | $0.00051 cache read |
| User message | ~500 | $0.0015 | $0.0015 |
| Output (subject + body) | ~400 | $0.006 | $0.006 |
| **Per draft** | | **~$0.014** | **~$0.0075** |

For an N-professor run, total drafting cost ≈ `$0.014 + (N-1) × $0.0075`:

| N | Drafting only | Cache savings vs uncached |
|---:|---:|---:|
| 1 | $0.014 | — (no reuse) |
| 3 | $0.029 | ~26% saved |
| 5 | $0.044 | ~35% saved |
| 10 | $0.082 | ~40% saved |

For a typical N=3 run, adding Step 6 takes the post-cache total from ~$0.057
to ~$0.085. The drafting summary in the terminal will tell you the actual
numbers — they're calibrated against your specific template + resume size, not
my estimates.

## How to test locally

### Unit tests

```bash
pytest tests/test_drafting.py -v
```

Tests cover: empty-matched raises, valid output parses, cache_control set on
system, matched projects + professor info + goal/considerations all reach the
user message, forced tool choice, missing tool_use raises, batch wrapper preserves
order.

### Real-API smoke

After running `scholar run` end-to-end:

```bash
# See the actual drafts:
sqlite3 ~/.scholarapp/scholar.db -header -column \
  "select p.name, d.subject, substr(d.body, 1, 200) || '...'
   from drafts d
   join professors p on p.id = d.professor_id
   where d.run_id = '<run_id>';"

# Check cache hit ratio in the usage summary at end of run.
```

If drafts read like the "Bad" example above, the rules aren't biting — file an
issue with the offending output and we'll tighten the prompt.
