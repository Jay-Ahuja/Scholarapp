"""CLI-level tests for the draft-count guarantee (top-up loop + exact-N gate).

These exercise `scholar run` end-to-end through the Typer CliRunner, mocking the
three expensive module boundaries (discovery, matching, drafting) so we can drive
exact scenarios deterministically and assert on DB state + exit codes.

What's covered (mirrors the orchestrator's verification list):
  (a) a short first pass triggers exclude-aware top-up passes that reach `count`,
      then drafts exactly `count`;
  (b) field exhaustion (a pass returns zero new candidates) is a PARTIAL delivery:
      the run drafts the matched professors, exits 0, ends NOT FAILED, and warns;
  (c) loop termination: one empty top-up batch then a matching batch reaches
      `count`; five consecutive empty batches stop at the fifth pass; four empty
      batches then a matching one resets the streak and keeps going;
  (d) `--stop-after matching` runs the loop and warns on shortfall (no hard fail);
  (e) a top-up DiscoveryError breaks the loop, then partial delivery drafts what
      matched.

The discovery dedup / exclude-aware fetch unit behavior lives in test_discovery.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import _NamedTextIOWrapper
from typer.testing import CliRunner

from scholarapp.cli import app
from scholarapp.db import repo
from scholarapp.db.models import RunStatus
from scholarapp.db.session import get_session
from scholarapp.errors import DiscoveryError, DraftingError
from scholarapp.modules import discovery, drafting, ingestion, matching


def _force_tty(monkeypatch) -> None:
    """Make `sys.stdin.isatty()` return True inside CliRunner.

    CliRunner isolates stdin to a click `_NamedTextIOWrapper` whose isatty() is
    always False, so the CLI's interactive-confirm branch would never fire under
    test. Patching the wrapper class's isatty lets us exercise the prompt path
    (and prove --yes skips it) while still feeding answers via `input=`.
    """
    monkeypatch.setattr(_NamedTextIOWrapper, "isatty", lambda self: True)


# ---------------------------------------------------------------------------
# Fakes for the three module boundaries
# ---------------------------------------------------------------------------


def _candidate(slug: str) -> discovery.ProfessorCandidate:
    """A minimal valid ProfessorCandidate with one work."""
    return discovery.ProfessorCandidate(
        openalex_id=slug,
        name=f"Prof {slug}",
        institution="MIT",
        email=f"{slug}@mit.edu",
        faculty_page_url=None,
        recent_works=[
            discovery.WorkRef(
                openalex_id=f"W_{slug}",
                title=f"Work by {slug}",
                url="https://doi.org/10.1/x",
                year=2024,
                abstract="some abstract",
            )
        ],
    )


class _FakeDiscovery:
    """Returns NEW candidates per call, honoring `count` + `exclude_ids`.

    `pool` is the ordered list of available slugs (most-cited first). Each call
    returns up to `count` slugs not in `exclude_ids`, simulating OpenAlex's
    exclude-aware over-fetch. Every returned professor has an email, so the
    DiscoveryResult's `attempted_ids` equals the returned (survivor) slugs.
    `exhausted` is True when the pool, after the exclude filter, no longer holds
    more than `count` NEW slugs — i.e. OpenAlex (modeled as the finite `pool`) ran
    out of fresh authors. Records every call for assertions.
    """

    def __init__(self, pool: list[str]):
        self.pool = pool
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        field: str,
        count: int,
        user_interests: list[str],
        exclude_ids: set[str] | None = None,
    ) -> discovery.DiscoveryResult:
        exclude = exclude_ids or set()
        self.calls.append(
            {"field": field, "count": count, "exclude_ids": set(exclude)}
        )
        available = [slug for slug in self.pool if slug not in exclude]
        out = [_candidate(slug) for slug in available[:count]]
        return discovery.DiscoveryResult(
            professors=out,
            attempted_ids={c.openalex_id for c in out},
            # The finite pool is fully consumed when no more than `count` NEW slugs
            # remain after the exclude filter → genuine exhaustion.
            exhausted=len(available) <= count,
        )


class _FakeMatching:
    """Matches a professor's first project iff their slug is in `qualifying`.

    Slug is recovered from the candidate name "Prof <slug>". Returns a list
    parallel to `pairs`, as the real `match_projects_for_run` does.
    """

    def __init__(self, qualifying: set[str]):
        self.qualifying = qualifying
        self.matched_names: list[str] = []

    async def __call__(
        self,
        *,
        pairs: list[tuple[str, list[matching.ProjectForMatching]]],
        user_interests: list[str],
        user_experiences: str,
    ) -> list[list[matching.MatchedProject]]:
        results: list[list[matching.MatchedProject]] = []
        for name, projects in pairs:
            slug = name.removeprefix("Prof ")
            if slug in self.qualifying and projects:
                self.matched_names.append(name)
                pr = projects[0]
                results.append(
                    [
                        matching.MatchedProject(
                            project_id=pr.project_id,
                            title=pr.title,
                            url=pr.url,
                            why_relevant="overlaps with the user's interests",
                        )
                    ]
                )
            else:
                results.append([])
        return results


class _FakeDrafting:
    """Drafts one email per request; records how many requests it received."""

    def __init__(self) -> None:
        self.request_counts: list[int] = []

    async def __call__(
        self,
        *,
        template: str,
        resume: Any,
        requests: list[drafting.DraftRequest],
        goal: str,
        considerations: str,
    ) -> list[drafting.EmailDraft]:
        self.request_counts.append(len(requests))
        out: list[drafting.EmailDraft] = []
        for req in requests:
            out.append(
                drafting.EmailDraft(
                    subject=f"Hi {req.professor.name}",
                    body=(
                        f"Dear {req.professor.name}, I read your work and found it "
                        "relevant to my research interests. " * 5
                    ),
                )
            )
        return out


def _mock_ingestion(monkeypatch, *, count: int) -> None:
    """Mock the two ingestion LLM calls so the test never hits the Anthropic API.

    parse_prompt drives the requested `count`; parse_resume returns a fixed
    resume. Both cli.py and _parse_resume_cached call these by attribute.
    """

    def _fake_parse_prompt(text: str) -> ingestion.PromptData:
        return ingestion.PromptData(
            count=count,
            field="neuroscience",
            goal="land a summer research internship",
            considerations="prefer labs doing neural imaging",
        )

    def _fake_parse_resume(pdf_path) -> ingestion.ResumeData:
        return ingestion.ResumeData(
            name="Test Student",
            email="student@school.edu",
            education=[],
            experiences=[
                ingestion.Experience(
                    org="State University",
                    role="Research Assistant",
                    years="2023",
                    bullets=["Built a CNN for MRI segmentation."],
                )
            ],
            skills=["Python", "PyTorch"],
            interests=["CNN", "MRI segmentation"],
            publications=[],
        )

    monkeypatch.setattr(ingestion, "parse_prompt", _fake_parse_prompt)
    monkeypatch.setattr(ingestion, "parse_resume", _fake_parse_resume)


def _install_fakes(
    monkeypatch,
    *,
    count: int,
    pool: list[str],
    qualifying: set[str],
) -> tuple[_FakeDiscovery, _FakeMatching, _FakeDrafting]:
    _mock_ingestion(monkeypatch, count=count)
    fake_disc = _FakeDiscovery(pool)
    fake_match = _FakeMatching(qualifying)
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)
    return fake_disc, fake_match, fake_draft


def _the_run(session):
    runs = repo.list_runs(session)
    assert len(runs) == 1
    return runs[0]


# ---------------------------------------------------------------------------
# (a) Short first pass → exclude-aware top-up reaches count → drafts exactly N
# ---------------------------------------------------------------------------


def test_topup_reaches_count_and_drafts_exactly_n(e2e_setup, monkeypatch):
    """First pass yields 1 match of 3; top-up passes reach 3, draft exactly 3."""
    inputs = e2e_setup["inputs"]

    # Pool of 6 distinct professors. Discovery returns `count` new ones per call,
    # but only a SUBSET qualifies in matching — so the first pass falls short of 3
    # and the exclude-aware top-up loop must keep discovering to reach the target.
    pool = ["a", "b", "c", "d", "e", "f"]
    fake_disc, fake_match, fake_draft = _install_fakes(
        monkeypatch, count=3, pool=pool, qualifying={"a", "d", "f"}
    )

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.stdout

    # Drafting got exactly 3 requests, and exactly 3 drafts persisted.
    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 3
    assert fake_draft.request_counts == [3]

    # Top-up happened: more than one discovery call, and each top-up excluded the
    # professors already discovered (exclude grows monotonically, never empty).
    assert len(fake_disc.calls) >= 2
    assert fake_disc.calls[0]["exclude_ids"] == set()
    # Every professor returned by an earlier pass is excluded in the next.
    seen: set[str] = set()
    for i, call in enumerate(fake_disc.calls):
        assert call["exclude_ids"] == seen, f"call {i} exclude mismatch"
        # After this call, the slugs it would have returned join `seen`.
        returned = [s for s in pool if s not in call["exclude_ids"]][: call["count"]]
        seen.update(returned)


# ---------------------------------------------------------------------------
# (a.2) Enriched-but-dropped (no-email) authors are excluded on the NEXT pass —
#       no author incurs a paid lookup twice across passes.
# ---------------------------------------------------------------------------


class _PoolDiscovery:
    """Simulates the real pull→enrich→drop pipeline at the pass level.

    Each call pulls the first `count` NON-excluded slugs from `pool` as its
    enriched pool, returns only the ones in `with_email` as survivors, and
    reports the WHOLE enriched pool (survivors + email-less discards) as
    `attempted_ids`. This is the scenario the bug fix targets: discards must be
    excluded so a later pass never re-pulls (re-pays for) them.
    """

    def __init__(self, pool: list[str], with_email: set[str]):
        self.pool = pool
        self.with_email = with_email
        self.calls: list[dict[str, Any]] = []
        self.enriched_per_call: list[list[str]] = []

    async def __call__(
        self,
        *,
        field: str,
        count: int,
        user_interests: list[str],
        exclude_ids: set[str] | None = None,
    ) -> discovery.DiscoveryResult:
        exclude = set(exclude_ids or set())
        self.calls.append({"field": field, "count": count, "exclude_ids": exclude})
        available = [s for s in self.pool if s not in exclude]
        enriched = available[:count]
        self.enriched_per_call.append(enriched)
        survivors = [_candidate(s) for s in enriched if s in self.with_email]
        return discovery.DiscoveryResult(
            professors=survivors,
            attempted_ids=set(enriched),
            # Exhausted once the finite pool has no more than `count` NEW authors
            # left after the exclude filter.
            exhausted=len(available) <= count,
        )


def test_topup_excludes_enriched_but_dropped_authors(e2e_setup, monkeypatch):
    """A pass's email-less discards are excluded next pass — no double paid lookup."""
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=2)

    # Pool of 4. count=2 → pass 1 enriches a,b but only `a` has an email; `b` is a
    # paid-but-dropped discard. Pass 2 must advance to c,d (NOT re-pull a or b).
    fake_disc = _PoolDiscovery(pool=["a", "b", "c", "d"], with_email={"a", "c", "d"})
    fake_match = _FakeMatching(qualifying={"a", "c", "d"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.output

    # Pass 1 excludes nothing; pass 2 excludes BOTH the survivor `a` AND the
    # email-less discard `b` — the whole enriched pool of pass 1.
    assert len(fake_disc.calls) == 2
    assert fake_disc.calls[0]["exclude_ids"] == set()
    assert fake_disc.calls[1]["exclude_ids"] == {"a", "b"}

    # No author is enriched twice across passes (the core regression guard).
    all_enriched = [s for call in fake_disc.enriched_per_call for s in call]
    assert len(all_enriched) == len(set(all_enriched)), (
        f"an author was enriched twice: {all_enriched}"
    )
    # Pass 2 advances PAST the pass-1 discard `b` straight to fresh ranks — `b` is
    # never re-enriched (which was the bug: discards used to get re-pulled).
    assert "b" not in fake_disc.enriched_per_call[1]
    assert "a" not in fake_disc.enriched_per_call[1]
    assert fake_disc.enriched_per_call[1] == ["c"]

    assert fake_draft.request_counts == [2]
    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 2


# ---------------------------------------------------------------------------
# (b) Field exhausted → PARTIAL delivery: drafts what matched, exits 0, REVIEW
# ---------------------------------------------------------------------------


def test_field_exhausted_partial_delivery(e2e_setup, monkeypatch):
    """Only 2 professors exist for a count=3 request → draft the 2, exit 0, warn."""
    inputs = e2e_setup["inputs"]

    # Pool has only 2 professors and both qualify. A later top-up pass returns [].
    fake_disc, fake_match, fake_draft = _install_fakes(
        monkeypatch, count=3, pool=["a", "b"], qualifying={"a", "b"}
    )

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    # A shortfall is a successful partial delivery, not a failure.
    assert result.exit_code == 0, result.output

    # Drafting ran on the 2 matched professors.
    assert fake_draft.request_counts == [2]

    # A shortfall warning was surfaced.
    assert "Found 2 of 3" in result.output

    with get_session() as session:
        run = _the_run(session)
        # NOT FAILED — partial delivery ends in the normal REVIEW state.
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
        professors = repo.list_professors_for_run(session, run.id)
    assert len(drafts) == 2
    assert len(professors) == 2


# ---------------------------------------------------------------------------
# (c) Loop termination: empty-batch streak, reset, and stop-at-fifth
# ---------------------------------------------------------------------------


class _BatchDiscovery:
    """Returns an explicit, pre-scripted batch of candidates per call.

    `batches` is an ordered list where each element is the list of slugs that
    pass surfaces. A batch may contain slugs that won't qualify in matching
    (an "empty" top-up batch). Once `batches` is exhausted, every further call
    returns `[]`. Slugs are NOT re-filtered against `exclude_ids` — the script
    controls novelty — but `exclude_ids` is still recorded for assertions. These
    scenarios drive termination via the empty-batch streak, NOT via field
    exhaustion, so every pass reports `exhausted=False`.
    """

    def __init__(self, batches: list[list[str]]):
        self.batches = batches
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        field: str,
        count: int,
        user_interests: list[str],
        exclude_ids: set[str] | None = None,
    ) -> discovery.DiscoveryResult:
        idx = len(self.calls)
        self.calls.append(
            {"field": field, "count": count, "exclude_ids": set(exclude_ids or set())}
        )
        slugs = self.batches[idx] if idx < len(self.batches) else []
        cands = [_candidate(s) for s in slugs]
        return discovery.DiscoveryResult(
            professors=cands,
            attempted_ids={c.openalex_id for c in cands},
            exhausted=False,
        )


def test_topup_one_empty_then_matching_reaches_count(e2e_setup, monkeypatch):
    """Initial short, one empty top-up batch, then a matching batch reaches count."""
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    # Pass 1 (initial): a,b,c → only `a` qualifies (have=1).
    # Pass 2 (top-up):  d,e   → neither qualifies (empty batch; streak=1).
    # Pass 3 (top-up):  f,g   → both qualify (have=3) → reached count.
    batches = [["a", "b", "c"], ["d", "e"], ["f", "g"]]
    fake_disc = _BatchDiscovery(batches)
    fake_match = _FakeMatching(qualifying={"a", "f", "g"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.output
    # 3 discovery calls: initial + the empty batch + the matching batch.
    assert len(fake_disc.calls) == 3
    assert fake_draft.request_counts == [3]

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 3


def test_topup_five_consecutive_empty_batches_stop_at_fifth(e2e_setup, monkeypatch):
    """Five consecutive empty top-up batches stop the search at the fifth pass."""
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    # Pass 1 (initial): a → qualifies (have=1). Passes 2..6 each surface fresh,
    # non-qualifying candidates (5 consecutive empty batches). The 5th empty
    # batch (pass 6, the 5th top-up) trips MAX_EMPTY_TOPUP_BATCHES and stops.
    batches = [
        ["a"],
        ["b1", "b2"],  # top-up 1 — empty streak=1
        ["c1", "c2"],  # top-up 2 — empty streak=2
        ["d1", "d2"],  # top-up 3 — empty streak=3
        ["e1", "e2"],  # top-up 4 — empty streak=4
        ["f1", "f2"],  # top-up 5 — empty streak=5 → STOP
        ["g1", "g2"],  # should never be requested
    ]
    fake_disc = _BatchDiscovery(batches)
    fake_match = _FakeMatching(qualifying={"a"})  # only the initial qualifies
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.output
    # 1 initial + exactly 5 top-up passes, then the streak trips. The 7th batch
    # is never requested.
    assert len(fake_disc.calls) == 6
    # Partial delivery: the single matched professor is drafted.
    assert fake_draft.request_counts == [1]

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 1


def test_topup_streak_resets_after_four_empty_then_matching(e2e_setup, monkeypatch):
    """Four empty batches then a matching one resets the streak and keeps going."""
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    # Pass 1 (initial): a → qualifies (have=1).
    # Top-ups 1..4: non-qualifying (streak climbs to 4 — one short of the cap).
    # Top-up 5: a matching batch → streak RESETS to 0, have=3, reached count.
    # If the streak had NOT reset, a 5th empty batch would have stopped first;
    # here the matching batch proves the reset by letting the run reach count.
    batches = [
        ["a"],
        ["b1", "b2"],  # empty streak=1
        ["c1", "c2"],  # empty streak=2
        ["d1", "d2"],  # empty streak=3
        ["e1", "e2"],  # empty streak=4
        ["f1", "f2"],  # MATCHING → reset, have=3
    ]
    fake_disc = _BatchDiscovery(batches)
    fake_match = _FakeMatching(qualifying={"a", "f1", "f2"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.output
    # 1 initial + 5 top-ups (4 empty + 1 matching). The loop did NOT stop at the
    # 4th empty batch, proving the streak only trips at 5 CONSECUTIVE empties.
    assert len(fake_disc.calls) == 6
    assert fake_draft.request_counts == [3]

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 3


# ---------------------------------------------------------------------------
# (c.2) Zero-survivor passes (authors pulled but all email-dropped) do NOT stop
#       the loop; only an empty attempted_ids (no authors pulled) is exhaustion.
# ---------------------------------------------------------------------------


class _ScriptedDiscovery:
    """Per-call scripted (survivor_slugs, attempted_slugs) — decoupled.

    Each scripted entry is `(survivors, attempted)`:
      - `survivors`: slugs that survive email validation → returned candidates.
      - `attempted`: every author this pass pulled+paid to enrich (survivors +
        email-less discards) → the DiscoveryResult's `attempted_ids`.
    A pass with `survivors=[]` but `attempted=[...]` models "pulled new authors
    but all dropped at email validation" — NOT field exhaustion, so `exhausted`
    is False and the loop keeps going. A pass with `attempted=[]` models genuine
    exhaustion (OpenAlex paging surfaced no new author at all), so `exhausted` is
    True and the loop stops. The fake derives `exhausted = not attempted`,
    mirroring how the real `find_professors` reports exhaustion when its paged
    pool comes back empty for a top-up. Once the script is exhausted, every
    further call returns `([], [])` (→ exhausted=True). `exclude_ids` is recorded
    but does NOT re-filter — the script controls novelty deterministically.
    """

    def __init__(self, script: list[tuple[list[str], list[str]]]):
        self.script = script
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        field: str,
        count: int,
        user_interests: list[str],
        exclude_ids: set[str] | None = None,
    ) -> discovery.DiscoveryResult:
        idx = len(self.calls)
        self.calls.append(
            {"field": field, "count": count, "exclude_ids": set(exclude_ids or set())}
        )
        survivors, attempted = (
            self.script[idx] if idx < len(self.script) else ([], [])
        )
        cands = [_candidate(s) for s in survivors]
        return discovery.DiscoveryResult(
            professors=cands,
            attempted_ids=set(attempted),
            exhausted=not attempted,
        )


def test_topup_zero_survivor_pass_does_not_stop_then_reaches_count(
    e2e_setup, monkeypatch
):
    """A pass that pulls authors but yields ZERO survivors keeps the loop going.

    The all-email-dropped pass is NOT field exhaustion: its attempted_ids is
    non-empty, so deeper ranks remain. The loop must continue and a later pass
    with matchable professors reaches count.
    """
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=2)

    # Pass 1 (initial): survivor a, qualifies → have=1.
    # Pass 2 (top-up):  pulled b,c but BOTH email-dropped → zero survivors. This
    #                   must NOT stop the loop (attempted_ids = {b, c}).
    # Pass 3 (top-up):  survivor d, qualifies → have=2 → reached count.
    script = [
        (["a"], ["a"]),
        ([], ["b", "c"]),
        (["d"], ["d"]),
    ]
    fake_disc = _ScriptedDiscovery(script)
    fake_match = _FakeMatching(qualifying={"a", "d"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.output
    # The zero-survivor pass did NOT stop the loop: a third discovery happened.
    assert len(fake_disc.calls) == 3
    # The zero-survivor pass excluded both pulled-but-dropped authors next pass.
    assert fake_disc.calls[2]["exclude_ids"] >= {"a", "b", "c"}
    # Match was never called with an empty professor list: only 2 matched drafts.
    assert fake_draft.request_counts == [2]

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 2


def test_topup_empty_attempted_ids_is_genuine_exhaustion(e2e_setup, monkeypatch):
    """A pass whose attempted_ids is empty (no author pulled) stops the loop."""
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    # Pass 1 (initial): survivor a, qualifies → have=1.
    # Pass 2 (top-up):  NOTHING pulled at all (attempted_ids empty) → exhaustion.
    script = [
        (["a"], ["a"]),
        ([], []),
        # A further matching batch is scripted but must NEVER be requested: the
        # empty-attempted pass stops the loop first.
        (["z"], ["z"]),
    ]
    fake_disc = _ScriptedDiscovery(script)
    fake_match = _FakeMatching(qualifying={"a", "z"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.output
    # Exactly 2 discovery calls: the initial + the empty-attempted pass. The
    # scripted matching batch (call 3) is never reached.
    assert len(fake_disc.calls) == 2
    assert "field exhausted" in result.output
    # Partial delivery: only the single matched professor is drafted.
    assert fake_draft.request_counts == [1]

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 1


def test_topup_five_zero_survivor_batches_stop_at_fifth(e2e_setup, monkeypatch):
    """Five consecutive zero-survivor passes stop via the 5-streak, not the first.

    Each top-up pulls fresh authors that are all email-dropped (zero survivors,
    non-empty attempted_ids). This is the uniform empty-batch streak: the loop
    must NOT stop on the first such pass — only after 5 consecutive ones.
    """
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    # Pass 1 (initial): survivor a, qualifies → have=1.
    # Top-ups 1..5: each pulls fresh authors that are ALL email-dropped (zero
    # survivors). Each counts one toward the streak; the 5th trips the cap.
    script = [
        (["a"], ["a"]),
        ([], ["b1", "b2"]),  # top-up 1 — zero-survivor, streak=1
        ([], ["c1", "c2"]),  # top-up 2 — zero-survivor, streak=2
        ([], ["d1", "d2"]),  # top-up 3 — zero-survivor, streak=3
        ([], ["e1", "e2"]),  # top-up 4 — zero-survivor, streak=4
        ([], ["f1", "f2"]),  # top-up 5 — zero-survivor, streak=5 → STOP
        ([], ["g1", "g2"]),  # should never be requested
    ]
    fake_disc = _ScriptedDiscovery(script)
    fake_match = _FakeMatching(qualifying={"a"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.output
    # 1 initial + exactly 5 zero-survivor top-ups, then the streak trips. The
    # loop did NOT stop on the first zero-survivor pass. The 7th is never asked.
    assert len(fake_disc.calls) == 6
    # The match boundary was never invoked for a top-up (every pass had zero
    # new professors), so only the initial pass's single match was drafted.
    assert fake_match.matched_names == ["Prof a"]
    assert fake_draft.request_counts == [1]

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 1


# ---------------------------------------------------------------------------
# (d) --stop-after matching: loop runs, shortfall warns, no hard fail
# ---------------------------------------------------------------------------


def test_stop_after_matching_warns_on_shortfall(e2e_setup, monkeypatch):
    """A shortfall under --stop-after matching warns and exits 0 (no drafts)."""
    inputs = e2e_setup["inputs"]

    fake_disc, fake_match, fake_draft = _install_fakes(
        monkeypatch, count=3, pool=["a", "b"], qualifying={"a", "b"}
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--stop-after", "matching"]
    )

    assert result.exit_code == 0, result.stdout
    assert "Found 2 of 3" in result.stdout
    assert "Stopped after matching" in result.stdout
    # No drafting under --stop-after matching.
    assert fake_draft.request_counts == []

    with get_session() as session:
        run = _the_run(session)
        # Not FAILED — a shortfall here is a warning, not a fatal error.
        assert run.status != RunStatus.FAILED
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 0


def test_stop_after_matching_runs_the_loop(e2e_setup, monkeypatch):
    """--stop-after matching still drives the top-up loop (multiple disc calls)."""
    inputs = e2e_setup["inputs"]

    # Pass 1 returns a,b,c but only `a` qualifies (have=1); the loop must run a
    # top-up that discovers d,e (both qualify) to reach 3 — two discovery calls.
    fake_disc, _, fake_draft = _install_fakes(
        monkeypatch, count=3, pool=["a", "b", "c", "d", "e"], qualifying={"a", "d", "e"}
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--stop-after", "matching"]
    )

    assert result.exit_code == 0, result.output
    # Loop ran: at least one top-up pass beyond the initial discovery.
    assert len(fake_disc.calls) >= 2
    assert fake_draft.request_counts == []  # never drafts in this mode


# ---------------------------------------------------------------------------
# (e) Top-up DiscoveryError breaks the loop, then partial delivery drafts what
#     matched
# ---------------------------------------------------------------------------


def test_topup_discovery_error_breaks_then_partial_delivery(e2e_setup, monkeypatch):
    """A DiscoveryError on a TOP-UP pass breaks the loop; the run delivers partial."""
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    # Initial pass succeeds with 1 qualifying professor; the first top-up raises.
    pool = ["a", "b", "c", "d", "e"]

    class _RaiseOnTopup(_FakeDiscovery):
        async def __call__(self, *, exclude_ids=None, **kwargs):
            # The initial pass has empty exclude_ids; top-ups have a non-empty set.
            if exclude_ids:
                raise DiscoveryError("Tavily returned 429 (quota exhausted).")
            return await super().__call__(exclude_ids=exclude_ids, **kwargs)

    fake_disc = _RaiseOnTopup(pool)
    fake_match = _FakeMatching(qualifying={"a"})  # only the first one matches
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    # Mid-loop error is swallowed (warning, not re-raised); the run then falls
    # through to partial delivery, drafting the 1 matched professor.
    assert result.exit_code == 0, result.output
    assert "Top-up discovery stopped early" in result.output  # the swallowed error
    assert "Found 1 of 3" in result.output  # the shortfall warning
    assert fake_draft.request_counts == [1]  # the one matched professor

    with get_session() as session:
        run = _the_run(session)
        # NOT FAILED — partial delivery ends in the normal REVIEW state.
        assert run.status == RunStatus.REVIEW
        drafts = repo.list_drafts_for_run(session, run.id)
    assert len(drafts) == 1


# ===========================================================================
# Cost estimate, confirmation, --budget ceiling, and RunUsage persistence
#
# These exercise the pre-flight gate (estimate panel + confirm/refuse) and the
# mid-run budget break that drops into partial delivery, plus the single
# RunUsage row written per completed run. The fakes here additionally RECORD
# Claude usage via usage.record(...) so tracker.total_cost_usd is non-zero and
# the budget boundary checks have something to trip on — the topup fakes above
# never spend, so they model "free" runs that never hit a ceiling.
#
# Token→cost: haiku input is $1e-6/token, so 1_000_000 input tokens == $1.00.
# We pick per-call token counts that make each stage cost an exact round dollar
# figure, so budget thresholds in the assertions are unambiguous.
# ===========================================================================


class _Usage:
    """Minimal stand-in for an Anthropic response.usage object."""

    def __init__(self, input_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.cache_creation_input_tokens = 0
        self.cache_read_input_tokens = 0
        self.output_tokens = 0


def _record(label: str, input_tokens: int) -> None:
    """Record one haiku call against the CLI-installed tracker (contextvar).

    Uses haiku ($1e-6/input token) so cost == input_tokens * 1e-6. The CLI
    installs the tracker for the duration of `scholar run`; asyncio.run copies
    the current context into the coroutine, so a record() from inside an async
    fake lands on that same tracker.
    """
    from scholarapp import usage

    usage.record(label, "claude-haiku-4-5", _Usage(input_tokens))


class _CostingDiscovery(_FakeDiscovery):
    """_FakeDiscovery that records a fixed discovery cost per call.

    `dollars_per_call` is charged under the "extract_email" label (→ discovery
    stage) on every discovery invocation, so each pass advances spend by a known
    amount and the between-stage budget checks have a real total to compare.
    """

    def __init__(self, pool: list[str], dollars_per_call: float) -> None:
        super().__init__(pool)
        self.dollars_per_call = dollars_per_call

    async def __call__(self, **kwargs: Any) -> discovery.DiscoveryResult:
        _record("extract_email", int(self.dollars_per_call * 1_000_000))
        return await super().__call__(**kwargs)


class _CostingMatching(_FakeMatching):
    """_FakeMatching that records a fixed matching cost per call."""

    def __init__(self, qualifying: set[str], dollars_per_call: float) -> None:
        super().__init__(qualifying)
        self.dollars_per_call = dollars_per_call

    async def __call__(self, **kwargs: Any) -> list[list[matching.MatchedProject]]:
        _record("match_projects", int(self.dollars_per_call * 1_000_000))
        return await super().__call__(**kwargs)


class _CostingDrafting(_FakeDrafting):
    """_FakeDrafting that records a fixed drafting cost per call."""

    def __init__(self, dollars_per_call: float) -> None:
        super().__init__()
        self.dollars_per_call = dollars_per_call

    async def __call__(self, **kwargs: Any) -> list[drafting.EmailDraft]:
        _record("draft_email", int(self.dollars_per_call * 1_000_000))
        return await super().__call__(**kwargs)


# ---------------------------------------------------------------------------
# Estimate panel + confirmation prompt
# ---------------------------------------------------------------------------


def test_estimate_panel_shown_before_discover_and_noninteractive_proceeds(
    e2e_setup, monkeypatch
):
    """The estimate panel renders before Discover; no TTY ⇒ no prompt, run proceeds.

    CliRunner feeds non-TTY stdin, so the opt-out confirm is skipped entirely and
    the pipeline runs to completion without hanging.
    """
    inputs = e2e_setup["inputs"]
    _install_fakes(monkeypatch, count=2, pool=["a", "b"], qualifying={"a", "b"})

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    assert result.exit_code == 0, result.output
    # The estimate section header + panel title appear, and they come BEFORE the
    # Discover section (the gate sits after parse, before discovery).
    assert "Cost estimate" in result.output
    assert "Estimated cost" in result.output
    assert result.output.index("Cost estimate") < result.output.index("Discover")
    # Static basis on a first-ever run (no history to average yet).
    assert "static estimate" in result.output


def test_confirm_decline_aborts_before_any_paid_work(e2e_setup, monkeypatch):
    """A declined confirm aborts cleanly: no discovery, no drafts, no RunUsage row.

    We force a TTY (isatty → True) so the prompt fires, then feed "n" to decline.
    Nothing past parse should run.
    """
    inputs = e2e_setup["inputs"]
    fake_disc, _, fake_draft = _install_fakes(
        monkeypatch, count=2, pool=["a", "b"], qualifying={"a", "b"}
    )
    _force_tty(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Aborted." in result.output
    # Discovery/drafting never ran — declining spends nothing past parse.
    assert fake_disc.calls == []
    assert fake_draft.request_counts == []

    with get_session() as session:
        run = _the_run(session)
        # The run row was created at parse, but no drafts and no usage row exist.
        assert repo.list_drafts_for_run(session, run.id) == []
        assert repo.list_recent_run_usage(session) == []


def test_yes_flag_skips_prompt_even_on_a_tty(e2e_setup, monkeypatch):
    """--yes opts out of the confirm prompt even when stdin is a TTY."""
    inputs = e2e_setup["inputs"]
    fake_disc, _, fake_draft = _install_fakes(
        monkeypatch, count=2, pool=["a", "b"], qualifying={"a", "b"}
    )
    _force_tty(monkeypatch)

    runner = CliRunner()
    # No stdin provided: if the prompt fired, typer.confirm would hit EOF and the
    # run would not complete cleanly. --yes must skip it.
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 0, result.output
    assert "Proceed with this run" not in result.output
    assert fake_draft.request_counts == [2]


# ---------------------------------------------------------------------------
# Refuse-to-start: --budget / RUN_MAX_USD below the estimate
# ---------------------------------------------------------------------------


def test_budget_below_estimate_refuses_to_start(e2e_setup, monkeypatch):
    """--budget under the estimate exits 1 before discovery; nothing is spent."""
    inputs = e2e_setup["inputs"]
    fake_disc, _, fake_draft = _install_fakes(
        monkeypatch, count=3, pool=["a", "b", "c"], qualifying={"a", "b", "c"}
    )

    runner = CliRunner()
    # The static estimate for count=3 is well above $0.0001; refuse-to-start.
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "0.0001", "--yes"]
    )

    assert result.exit_code == 1, result.output
    assert "exceeds the budget" in result.output
    # No discovery, no drafts: the gate fires before any paid stage.
    assert fake_disc.calls == []
    assert fake_draft.request_counts == []

    with get_session() as session:
        run = _the_run(session)
        assert repo.list_drafts_for_run(session, run.id) == []
        assert repo.list_recent_run_usage(session) == []


def test_run_max_usd_setting_refuses_when_below_estimate(e2e_setup, monkeypatch):
    """RUN_MAX_USD acts as the ceiling when --budget is absent."""
    inputs = e2e_setup["inputs"]
    fake_disc, _, _ = _install_fakes(
        monkeypatch, count=3, pool=["a", "b", "c"], qualifying={"a", "b", "c"}
    )
    monkeypatch.setenv("RUN_MAX_USD", "0.0001")

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 1, result.output
    assert "exceeds the budget" in result.output
    assert fake_disc.calls == []


def test_budget_overrides_run_max_usd(e2e_setup, monkeypatch):
    """An explicit --budget wins over a stricter RUN_MAX_USD env setting."""
    inputs = e2e_setup["inputs"]
    _install_fakes(monkeypatch, count=2, pool=["a", "b"], qualifying={"a", "b"})
    # RUN_MAX_USD would refuse, but a generous --budget overrides and allows it.
    monkeypatch.setenv("RUN_MAX_USD", "0.0001")

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "100", "--yes"]
    )

    assert result.exit_code == 0, result.output
    with get_session() as session:
        run = _the_run(session)
        assert len(repo.list_drafts_for_run(session, run.id)) == 2


# ---------------------------------------------------------------------------
# Malformed budget ceilings: non-finite (NaN/±inf) and negative are rejected
# identically whether they arrive via --budget or RUN_MAX_USD. A rejection is a
# fail-closed hard-stop: clean error, exit 1, and NOTHING past parse runs (no
# discovery, no drafts, no RunUsage row).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-5"])
def test_malformed_budget_flag_hard_stops(e2e_setup, monkeypatch, bad):
    """--budget {nan,inf,-inf,-5} hard-stops before any paid work."""
    inputs = e2e_setup["inputs"]
    fake_disc, _, fake_draft = _install_fakes(
        monkeypatch, count=3, pool=["a", "b", "c"], qualifying={"a", "b", "c"}
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", bad, "--yes"]
    )

    assert result.exit_code == 1, result.output
    # The error names the malformed value and the source knob, and is NOT the
    # refuse-to-start ("exceeds the budget") message — this is a validation stop.
    assert "Invalid budget ceiling" in result.output
    assert "--budget" in result.output
    assert "exceeds the budget" not in result.output
    # Nothing past parse ran: the gate sits before discovery/drafting.
    assert fake_disc.calls == []
    assert fake_draft.request_counts == []

    with get_session() as session:
        run = _the_run(session)
        assert repo.list_drafts_for_run(session, run.id) == []
        assert repo.list_recent_run_usage(session) == []


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-5"])
def test_malformed_run_max_usd_hard_stops(e2e_setup, monkeypatch, bad):
    """RUN_MAX_USD {nan,inf,-inf,-5} hard-stops identically to --budget."""
    inputs = e2e_setup["inputs"]
    fake_disc, _, fake_draft = _install_fakes(
        monkeypatch, count=3, pool=["a", "b", "c"], qualifying={"a", "b", "c"}
    )
    monkeypatch.setenv("RUN_MAX_USD", bad)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 1, result.output
    # Same validation message, but the source knob is now the env var, proving the
    # single convergence-point check polices both channels the same way.
    assert "Invalid budget ceiling" in result.output
    assert "RUN_MAX_USD" in result.output
    assert "exceeds the budget" not in result.output
    assert fake_disc.calls == []
    assert fake_draft.request_counts == []

    with get_session() as session:
        run = _the_run(session)
        assert repo.list_drafts_for_run(session, run.id) == []
        assert repo.list_recent_run_usage(session) == []


def test_zero_budget_flag_is_valid_and_refuses_to_start(e2e_setup, monkeypatch):
    """--budget 0 is a legitimate ceiling: it refuses to run, NOT a validation error.

    A zero ceiling is below any positive estimate, so it trips the existing
    refuse-to-start check ("exceeds the budget") — it must NOT be treated as
    malformed input.
    """
    inputs = e2e_setup["inputs"]
    fake_disc, _, _ = _install_fakes(
        monkeypatch, count=3, pool=["a", "b", "c"], qualifying={"a", "b", "c"}
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "0", "--yes"]
    )

    assert result.exit_code == 1, result.output
    assert "exceeds the budget" in result.output
    assert "Invalid budget ceiling" not in result.output
    assert fake_disc.calls == []


def test_zero_run_max_usd_is_valid_and_refuses_to_start(e2e_setup, monkeypatch):
    """RUN_MAX_USD=0 is a legitimate ceiling that refuses to run (not malformed)."""
    inputs = e2e_setup["inputs"]
    fake_disc, _, _ = _install_fakes(
        monkeypatch, count=3, pool=["a", "b", "c"], qualifying={"a", "b", "c"}
    )
    monkeypatch.setenv("RUN_MAX_USD", "0")

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 1, result.output
    assert "exceeds the budget" in result.output
    assert "Invalid budget ceiling" not in result.output
    assert fake_disc.calls == []


def test_unset_budget_runs_unbounded(e2e_setup, monkeypatch):
    """No --budget and no RUN_MAX_USD ⇒ effective_budget is None ⇒ unbounded run."""
    inputs = e2e_setup["inputs"]
    monkeypatch.delenv("RUN_MAX_USD", raising=False)
    _, _, fake_draft = _install_fakes(
        monkeypatch, count=2, pool=["a", "b"], qualifying={"a", "b"}
    )

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 0, result.output
    assert "Invalid budget ceiling" not in result.output
    assert fake_draft.request_counts == [2]


def test_valid_positive_budget_runs_as_before(e2e_setup, monkeypatch):
    """A generous finite, positive --budget is accepted and the run completes."""
    inputs = e2e_setup["inputs"]
    _, _, fake_draft = _install_fakes(
        monkeypatch, count=2, pool=["a", "b"], qualifying={"a", "b"}
    )

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "100", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert "Invalid budget ceiling" not in result.output
    assert fake_draft.request_counts == [2]


# ---------------------------------------------------------------------------
# Mid-run ceiling crossed → partial delivery + budget-named summary
# ---------------------------------------------------------------------------


def test_budget_crossed_during_topup_partial_delivery(e2e_setup, monkeypatch):
    """Ceiling crossed mid-run ⇒ stop topping up, draft what matched, name budget.

    Each discovery pass records $1.00. The initial pass matches 1 of 3, so a
    top-up would be needed; but spend ($1.00 after the initial discovery, then
    matching/drafting add more) crosses a $1.50 ceiling before/at the first
    top-up boundary, so the loop breaks and we deliver the 1 matched professor.
    """
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    fake_disc = _CostingDiscovery(pool=["a", "b", "c", "d", "e"], dollars_per_call=1.0)
    fake_match = _CostingMatching(qualifying={"a", "d", "e"}, dollars_per_call=0.5)
    fake_draft = _CostingDrafting(dollars_per_call=0.1)
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    # Initial discovery ($1.00) + initial matching ($0.50) == $1.50 ⇒ the top-up
    # boundary check (>= ceiling) trips and the loop stops at 1 matched professor.
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "1.50", "--yes"]
    )

    assert result.exit_code == 0, result.output
    # Only the initial discovery ran — no top-up pass after the ceiling was hit.
    assert len(fake_disc.calls) == 1
    # Partial delivery drafted the single matched professor.
    assert fake_draft.request_counts == [1]
    # The break happened at the TOP-UP boundary (the loop's pre-pass budget check),
    # not merely at the pre-draft check — prove the loop itself stopped early.
    assert "Budget reached during top-up" in result.output
    # The end-of-run summary names the budget stop (spent vs. budget).
    assert "budget reached" in result.output.lower()

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        assert len(repo.list_drafts_for_run(session, run.id)) == 1


def test_partial_run_records_realized_not_requested_counts(e2e_setup, monkeypatch):
    """A budget-stopped run records the REALIZED per-stage size, not `count`=3.

    Drafting realized count must be the number actually drafted (1), so future
    estimates divide drafting spend by 1 — not by the requested 3 — and don't
    understate the drafting rate.
    """
    from scholarapp import usage

    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    fake_disc = _CostingDiscovery(pool=["a", "b", "c", "d", "e"], dollars_per_call=1.0)
    fake_match = _CostingMatching(qualifying={"a", "d", "e"}, dollars_per_call=0.5)
    fake_draft = _CostingDrafting(dollars_per_call=0.1)
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "1.50", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert fake_draft.request_counts == [1]  # only 1 professor was drafted

    with get_session() as session:
        run = _the_run(session)
        rows = repo.list_recent_run_usage(session)
        assert len(rows) == 1
        # The requested count is preserved as-is...
        assert rows[0].count == 3
        _, counts = usage.unpack_stage_usage(rows[0].stage_costs)
        # ...but the realized drafting count is what was actually drafted (1),
        # NOT the requested 3. Discovery persisted only the initial pass's profs.
        n_profs = len(repo.list_professors_for_run(session, run.id))
        assert counts["discovery"] == n_profs
        assert counts["matching"] == 1  # only `a` matched before the budget broke
        assert counts["drafting"] == 1


# ---------------------------------------------------------------------------
# Exactly one RunUsage row per completed run
# ---------------------------------------------------------------------------


def test_completed_run_writes_exactly_one_run_usage_row(e2e_setup, monkeypatch):
    """A full run persists one RunUsage row with per-stage costs keyed by STAGES."""
    from scholarapp import usage

    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=2)

    fake_disc = _CostingDiscovery(pool=["a", "b"], dollars_per_call=0.2)
    fake_match = _CostingMatching(qualifying={"a", "b"}, dollars_per_call=0.3)
    fake_draft = _CostingDrafting(dollars_per_call=0.4)
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 0, result.output

    with get_session() as session:
        run = _the_run(session)
        rows = repo.list_recent_run_usage(session)
        assert len(rows) == 1
        row = rows[0]
        assert row.run_id == run.id
        assert row.count == 2
        # The JSON value carries the tagged costs + realized counts; unpack to read.
        costs, counts = usage.unpack_stage_usage(row.stage_costs)
        # Costs are keyed by usage.STAGES and total the recorded spend:
        # discovery $0.20, matching $0.30, drafting $0.40.
        assert set(costs) == set(usage.STAGES)
        assert costs["discovery"] == pytest.approx(0.2)
        assert costs["matching"] == pytest.approx(0.3)
        assert costs["drafting"] == pytest.approx(0.4)
        assert row.total_usd == pytest.approx(0.9)
        # A full run drafted all 2 professors -> realized per-stage counts all 2.
        assert counts == {"discovery": 2, "matching": 2, "drafting": 2}


# ---------------------------------------------------------------------------
# Fail-closed on unpriced (unknown-model) spend under a budget ceiling
#
# An unknown-model call costs $0.00 to total_cost_usd only because we have no
# rate for it — NOT because it was free. Under an active budget that unmeasurable
# spend is itself a stop condition (fail-closed), so the run must stop at the
# next boundary even when recognized spend is tiny. With NO budget the unknown
# model must NOT stop the run (it only surfaces in the usage summary).
# ---------------------------------------------------------------------------


def _record_unpriced(label: str, input_tokens: int) -> None:
    """Record one UNKNOWN-model call against the CLI-installed tracker.

    The model id is absent from PRICING, so the call is unpriced: it contributes
    $0.00 to total_cost_usd but flips tracker.has_unpriced_calls to True.
    """
    from scholarapp import usage

    usage.record(label, "claude-bogus-9-9", _Usage(input_tokens))


class _UnpricedMatching(_FakeMatching):
    """_FakeMatching that records an UNPRICED (unknown-model) matching call.

    Matching still completes its matches (so a professor IS matched), but the
    recorded spend is unmeasurable against any budget. The post-matching boundary
    checks must fail closed regardless of recognized spend.
    """

    async def __call__(self, **kwargs: Any) -> list[list[matching.MatchedProject]]:
        _record_unpriced("match_projects", 10_000)
        return await super().__call__(**kwargs)


class _UnpricedDiscovery(_FakeDiscovery):
    """_FakeDiscovery that records an UNPRICED (unknown-model) discovery call.

    Spend is unmeasurable against any budget, so the next boundary check must
    fail closed and stop the run regardless of recognized spend.
    """

    async def __call__(self, **kwargs: Any) -> discovery.DiscoveryResult:
        _record_unpriced("extract_email", 10_000)
        return await super().__call__(**kwargs)


def test_unpriced_spend_under_budget_stops_at_next_boundary(e2e_setup, monkeypatch):
    """An unknown-model call under a generous budget stops the run fail-closed.

    Matching records an UNPRICED call (cost $0.00) under a $100 budget — far above
    any recognized spend — after matching the single professor `a`. The post-match
    boundary checks must still trip on the unpriced spend and fall into partial
    delivery (drafts the 1 matched professor, ends REVIEW), and the summary must
    name the unaccountable spend.
    """
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    # Initial pass matches only `a`; a top-up would normally follow, but the
    # unpriced spend recorded during matching trips the budget gate first.
    fake_disc = _FakeDiscovery(pool=["a", "b", "c", "d", "e"])
    fake_match = _UnpricedMatching(qualifying={"a", "d", "e"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    # $100 ceiling is never reached by recognized spend ($0.00 here); only the
    # unpriced call trips the gate — proving fail-closed, not a ceiling crossing.
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "100", "--yes"]
    )

    assert result.exit_code == 0, result.output
    # The gate tripped right after the initial match — no top-up pass ran.
    assert len(fake_disc.calls) == 1
    # Partial delivery drafted the single matched professor.
    assert fake_draft.request_counts == [1]
    # The end-of-run summary names the fail-closed unaccountable spend, and the
    # plain "budget reached: spent ... of ... budget" summary line is NOT used
    # (the unpriced message takes precedence). The mid-run top-up progress warning
    # may still say "budget reached during top-up" — that's a separate notice.
    assert "could not be measured" in result.output
    assert "budget reached: spent" not in result.output.lower()

    with get_session() as session:
        run = _the_run(session)
        # Partial delivery ends in the normal REVIEW state, not FAILED.
        assert run.status == RunStatus.REVIEW
        assert len(repo.list_drafts_for_run(session, run.id)) == 1


def test_unpriced_spend_during_discovery_skips_matching_fail_closed(e2e_setup, monkeypatch):
    """Unpriced spend recorded in DISCOVERY trips the pre-matching gate, drafts none.

    Discovery records the unpriced call, so the pre-matching boundary check (after
    discovery) fails closed before matching runs — nothing matches and partial
    delivery drafts zero. A clean, non-FAILED run that names the unaccountable
    spend.
    """
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=3)

    fake_disc = _UnpricedDiscovery(pool=["a", "b", "c", "d", "e"])
    fake_match = _FakeMatching(qualifying={"a", "d", "e"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "100", "--yes"]
    )

    assert result.exit_code == 0, result.output
    # Matching was skipped (fail-closed before it ran), so nothing was drafted.
    assert fake_match.matched_names == []
    assert fake_draft.request_counts == []
    # The post-discovery boundary check fired on the unpriced spend, skipping match.
    assert "Budget reached after discovery" in result.output
    # The end-of-run summary names the fail-closed unaccountable spend, not the
    # plain ceiling line.
    assert "could not be measured" in result.output
    assert "budget reached: spent" not in result.output.lower()

    with get_session() as session:
        run = _the_run(session)
        # A nothing-to-draft run is NOT a failure (it ends in the drafting phase's
        # nothing-to-draft terminal path), but it must never be FAILED.
        assert run.status != RunStatus.FAILED
        assert repo.list_drafts_for_run(session, run.id) == []


def test_unpriced_spend_with_no_budget_does_not_stop(e2e_setup, monkeypatch):
    """No budget + an unknown-model call must NOT stop the run (gate stays off)."""
    inputs = e2e_setup["inputs"]
    monkeypatch.delenv("RUN_MAX_USD", raising=False)
    _mock_ingestion(monkeypatch, count=2)

    # Both professors qualify; the unpriced discovery call must not gate anything
    # because there's no active budget.
    fake_disc = _UnpricedDiscovery(pool=["a", "b"])
    fake_match = _FakeMatching(qualifying={"a", "b"})
    fake_draft = _FakeDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 0, result.output
    # No budget ⇒ unpriced spend never gates: the full run drafts both professors.
    assert fake_draft.request_counts == [2]
    # No budget-stop / fail-closed notice was emitted.
    assert "could not be measured" not in result.output
    assert "budget reached" not in result.output.lower()

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        assert len(repo.list_drafts_for_run(session, run.id)) == 2


def test_all_priced_under_budget_behaves_as_before(e2e_setup, monkeypatch):
    """All-priced spend under a generous budget completes a full run unchanged.

    A regression guard: introducing the unpriced trip must not alter the
    all-known-model path. No fail-closed notice, full delivery, REVIEW status.
    """
    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=2)

    fake_disc = _CostingDiscovery(pool=["a", "b"], dollars_per_call=0.2)
    fake_match = _CostingMatching(qualifying={"a", "b"}, dollars_per_call=0.3)
    fake_draft = _CostingDrafting(dollars_per_call=0.4)
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "100", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert fake_draft.request_counts == [2]
    assert "could not be measured" not in result.output
    assert "budget reached" not in result.output.lower()

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        assert len(repo.list_drafts_for_run(session, run.id)) == 2


def test_refused_run_writes_no_run_usage_row(e2e_setup, monkeypatch):
    """A refuse-to-start run leaves no RunUsage history sample behind."""
    inputs = e2e_setup["inputs"]
    _install_fakes(monkeypatch, count=3, pool=["a", "b", "c"], qualifying={"a", "b", "c"})

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "0.0001", "--yes"]
    )

    assert result.exit_code == 1, result.output
    with get_session() as session:
        assert repo.list_recent_run_usage(session) == []


# ---------------------------------------------------------------------------
# Budget-stop partial delivery with N>1 matched professors
# ---------------------------------------------------------------------------


def test_budget_crossed_with_multiple_matched_drafts_all_matched(e2e_setup, monkeypatch):
    """Ceiling trips after >=2 already matched ⇒ partial delivery drafts all of them.

    Requested count is 5; the initial pass matches 2 (`a`, `b`) of the 5 discovered.
    Initial discovery ($1.00) + initial matching ($0.50) == $1.50 hits the $1.50
    ceiling, so the top-up boundary check trips with have=2 and the loop breaks.
    Partial delivery then drafts BOTH matched professors (the existing budget-stop
    tests only ever exercise a single matched professor).
    """
    from scholarapp import usage

    inputs = e2e_setup["inputs"]
    _mock_ingestion(monkeypatch, count=5)

    fake_disc = _CostingDiscovery(pool=["a", "b", "c", "d", "e"], dollars_per_call=1.0)
    fake_match = _CostingMatching(qualifying={"a", "b"}, dollars_per_call=0.5)
    fake_draft = _CostingDrafting(dollars_per_call=0.1)
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(
        app, ["run", "--inputs", str(inputs), "--budget", "1.50", "--yes"]
    )

    assert result.exit_code == 0, result.output
    # Only the initial discovery ran — the ceiling broke the top-up loop.
    assert len(fake_disc.calls) == 1
    # Both matched professors were drafted in a single drafting call.
    assert fake_draft.request_counts == [2]
    assert "Budget reached during top-up" in result.output

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.REVIEW
        assert len(repo.list_drafts_for_run(session, run.id)) == 2
        rows = repo.list_recent_run_usage(session)
        assert len(rows) == 1
        _, counts = usage.unpack_stage_usage(rows[0].stage_costs)
        # Realized drafting count is the 2 actually drafted, not the requested 5.
        assert counts["drafting"] == 2


# ---------------------------------------------------------------------------
# RUN_MAX_USD=<non-numeric> through `run` ⇒ clean ConfigError, no traceback
# ---------------------------------------------------------------------------


def test_run_max_usd_non_numeric_renders_clean_error(e2e_setup, monkeypatch):
    """A genuinely non-numeric RUN_MAX_USD hard-stops `run` with a friendly message.

    nan/inf/-5 all parse via float() and are unit-tested elsewhere; a value that
    fails float() parsing (`abc`) must surface as the controlled config message —
    exit 1, no Python traceback — when `run` reads the ceiling.
    """
    inputs = e2e_setup["inputs"]
    fake_disc, _, fake_draft = _install_fakes(
        monkeypatch, count=2, pool=["a", "b"], qualifying={"a", "b"}
    )
    monkeypatch.setenv("RUN_MAX_USD", "abc")

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 1, result.output
    # The controlled ConfigError message, not a raw float() ValueError.
    assert "RUN_MAX_USD must be a number" in result.output
    assert "abc" in result.output
    # No traceback leaked: ScholarError is caught and rendered via ui.error.
    assert "Traceback" not in result.output
    # The bad ceiling stops the run before any paid stage.
    assert fake_disc.calls == []
    assert fake_draft.request_counts == []


# ---------------------------------------------------------------------------
# A FAILED drafting run writes no RunUsage history sample
# ---------------------------------------------------------------------------


def test_failed_drafting_run_writes_no_run_usage_row(e2e_setup, monkeypatch):
    """Drafting raising a ScholarError ⇒ run FAILED, exit 1, no history sample.

    The refused/declined no-row cases are covered; this pins the FAILED path: a
    crash during drafting must not leave a misleading RunUsage sample behind.
    """
    inputs = e2e_setup["inputs"]

    class _RaisingDrafting(_FakeDrafting):
        async def __call__(self, **kwargs: Any) -> list[drafting.EmailDraft]:
            raise DraftingError("Anthropic returned 500 during drafting.")

    _mock_ingestion(monkeypatch, count=2)
    fake_disc = _FakeDiscovery(pool=["a", "b"])
    fake_match = _FakeMatching(qualifying={"a", "b"})
    fake_draft = _RaisingDrafting()
    monkeypatch.setattr(discovery, "find_professors", fake_disc)
    monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
    monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])

    assert result.exit_code == 1, result.output

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.FAILED
        # No drafts persisted and — crucially — no history sample written.
        assert repo.list_drafts_for_run(session, run.id) == []
        assert repo.list_recent_run_usage(session) == []


# ---------------------------------------------------------------------------
# Historical-basis feedback loop end-to-end
# ---------------------------------------------------------------------------


def test_historical_basis_estimate_after_enough_priced_runs(e2e_setup, monkeypatch):
    """Once enough completed priced runs exist, a later run estimates HISTORICALLY.

    The first run writes a count-bearing RunUsage sample (all stages realized 2
    professors). With _MIN_HISTORY_SAMPLES monkeypatched down to 1, the second
    run's pre-flight estimate crosses the threshold and renders the historical
    basis wording ("based on the last N run(s)") instead of the static guess —
    exercising _recent_cost_history's ORM->RunCostSample unpack end-to-end.
    """
    from scholarapp import usage

    inputs = e2e_setup["inputs"]
    # Small threshold so a single completed run is enough — fast and non-flaky.
    monkeypatch.setattr(usage, "_MIN_HISTORY_SAMPLES", 1)

    def _install_costing() -> _CostingDrafting:
        fake_disc = _CostingDiscovery(pool=["a", "b"], dollars_per_call=0.2)
        fake_match = _CostingMatching(qualifying={"a", "b"}, dollars_per_call=0.3)
        fake_draft = _CostingDrafting(dollars_per_call=0.4)
        monkeypatch.setattr(discovery, "find_professors", fake_disc)
        monkeypatch.setattr(matching, "match_projects_for_run", fake_match)
        monkeypatch.setattr(drafting, "draft_emails_for_run", fake_draft)
        return fake_draft

    runner = CliRunner()

    # First run: no history yet ⇒ static basis, and it persists one sample.
    _mock_ingestion(monkeypatch, count=2)
    first_draft = _install_costing()
    first = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])
    assert first.exit_code == 0, first.output
    assert "static estimate" in first.output
    assert first_draft.request_counts == [2]
    with get_session() as session:
        assert len(repo.list_recent_run_usage(session)) == 1

    # Second run: one count-bearing prior sample crosses the (patched) threshold,
    # so the estimate panel must now read the historical-basis wording.
    _mock_ingestion(monkeypatch, count=2)
    _install_costing()
    second = runner.invoke(app, ["run", "--inputs", str(inputs), "--yes"])
    assert second.exit_code == 0, second.output
    assert "based on the last 1 run" in second.output
    assert "static estimate" not in second.output
