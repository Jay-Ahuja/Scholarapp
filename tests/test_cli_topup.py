"""CLI-level tests for the draft-count guarantee (top-up loop + exact-N gate).

These exercise `scholar run` end-to-end through the Typer CliRunner, mocking the
three expensive module boundaries (discovery, matching, drafting) so we can drive
exact scenarios deterministically and assert on DB state + exit codes.

What's covered (mirrors the orchestrator's verification list):
  (a) a short first pass triggers exclude-aware top-up passes that reach `count`,
      then drafts exactly `count`;
  (b) field exhaustion (a pass returns zero new qualifying professors) raises
      CountUnreachableError, exits non-zero, marks the run FAILED, drafts nothing;
  (d) `--stop-after matching` runs the loop and warns on shortfall (no hard fail);
  (e) a top-up DiscoveryError breaks the loop, then the gate fails loudly.

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
    exclude-aware over-fetch. Records every call for assertions.
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
    ) -> list[discovery.ProfessorCandidate]:
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
        return out


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
# (b) Field exhausted → CountUnreachableError, non-zero exit, FAILED, no drafts
# ---------------------------------------------------------------------------


def test_field_exhausted_fails_loudly_before_drafting(e2e_setup, monkeypatch):
    """Only 2 professors exist for a count=3 request → run FAILS, drafts nothing."""
    inputs = e2e_setup["inputs"]

    # Pool has only 2 professors and both qualify. A later top-up pass returns [].
    fake_disc, fake_match, fake_draft = _install_fakes(
        monkeypatch, count=3, pool=["a", "b"], qualifying={"a", "b"}
    )

    runner = CliRunner()
    result = runner.invoke(app, ["run", "--inputs", str(inputs)])

    # CountUnreachableError → non-zero exit. (Its message renders to stderr via
    # ui.error; we assert its substance against the persisted run.error below.)
    assert result.exit_code == 1, result.output

    # No drafting was attempted (fail BEFORE any Sonnet spend).
    assert fake_draft.request_counts == []

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.FAILED
        assert run.error and "count unreachable" in run.error
        assert "found 2 of 3" in run.error
        drafts = repo.list_drafts_for_run(session, run.id)
        # The 2 discovered+matched professors persist; just no drafts.
        professors = repo.list_professors_for_run(session, run.id)
    assert len(drafts) == 0
    assert len(professors) == 2


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
# (e) Top-up DiscoveryError breaks the loop, then the gate fails loudly
# ---------------------------------------------------------------------------


def test_topup_discovery_error_breaks_then_gate_fails(e2e_setup, monkeypatch):
    """A DiscoveryError on a TOP-UP pass breaks the loop; gate then fails loudly."""
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

    # Mid-loop error is swallowed (warning, not re-raised); the exact-N gate then
    # fails loudly because have (1) < count (3). Nothing is drafted.
    assert result.exit_code == 1, result.output
    assert "Top-up discovery stopped early" in result.output  # the swallowed error
    assert fake_draft.request_counts == []  # nothing drafted

    with get_session() as session:
        run = _the_run(session)
        assert run.status == RunStatus.FAILED
        assert run.error and "count unreachable" in run.error
        assert "found 1 of 3" in run.error
