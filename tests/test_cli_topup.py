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

from typer.testing import CliRunner

from scholarapp.cli import app
from scholarapp.db import repo
from scholarapp.db.models import RunStatus
from scholarapp.db.session import get_session
from scholarapp.errors import DiscoveryError
from scholarapp.modules import discovery, drafting, ingestion, matching

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
    Records every call for assertions.
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
        out: list[discovery.ProfessorCandidate] = []
        for slug in self.pool:
            if len(out) >= count:
                break
            if slug in exclude:
                continue
            out.append(_candidate(slug))
        return discovery.DiscoveryResult(
            professors=out, attempted_ids={c.openalex_id for c in out}
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
        enriched = [s for s in self.pool if s not in exclude][:count]
        self.enriched_per_call.append(enriched)
        survivors = [_candidate(s) for s in enriched if s in self.with_email]
        return discovery.DiscoveryResult(
            professors=survivors, attempted_ids=set(enriched)
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
    (an "empty" top-up batch) or `[]` to simulate genuine field exhaustion.
    Once `batches` is exhausted, every further call returns `[]`. Slugs are
    NOT re-filtered against `exclude_ids` — the script controls novelty — but
    `exclude_ids` is still recorded for assertions.
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
            professors=cands, attempted_ids={c.openalex_id for c in cands}
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
