"""Tests for RunUsage persistence (db/models.py + db/repo.py).

The run_usage table must be created by the existing idempotent init_db()
(create_all) — there are no migrations — and rows must list newest-first so the
cost engine can average recent history. We use a throwaway DATA_DIR so the real
~/.scholarapp DB is never touched, and reset_engine() to rebind the cached
engine to it.
"""

from __future__ import annotations

import pytest

from scholarapp.db import repo
from scholarapp.db.session import get_session, reset_engine


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Bind the engine to a fresh sqlite file under a temp DATA_DIR."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    reset_engine()  # forget any engine bound to a previous DATA_DIR
    yield
    reset_engine()  # leave no engine pointing at the now-deleted tmp dir


def _make_run(session):
    """A RunUsage row needs a real Run to satisfy the run_id FK."""
    return repo.create_run(
        session,
        field="ml",
        goal="phd",
        considerations="none",
        count=3,
        resume_path="/tmp/r.pdf",
        template_text="hi",
    )


def test_run_usage_round_trips(db):
    with get_session() as session:
        run = _make_run(session)
        stage_costs = {"discovery": 0.01, "matching": 0.02, "drafting": 0.03}
        usage = repo.add_run_usage(
            session,
            run_id=run.id,
            count=3,
            total_usd=0.06,
            stage_costs=stage_costs,
        )
        assert usage.id is not None

    with get_session() as session:
        rows = repo.list_recent_run_usage(session)
        assert len(rows) == 1
        row = rows[0]
        assert row.count == 3
        assert row.total_usd == 0.06
        # JSON column round-trips the dict keyed by STAGES.
        assert row.stage_costs == {"discovery": 0.01, "matching": 0.02, "drafting": 0.03}


def test_list_recent_run_usage_newest_first(db):
    with get_session() as session:
        run = _make_run(session)
        # Insert three rows; created_at default uses _utcnow at insert time.
        for i in range(3):
            repo.add_run_usage(
                session,
                run_id=run.id,
                count=i + 1,
                total_usd=float(i),
                stage_costs={"discovery": 0.0, "matching": 0.0, "drafting": float(i)},
            )

    with get_session() as session:
        rows = repo.list_recent_run_usage(session)
        assert len(rows) == 3
        # Newest first => created_at descending (non-increasing).
        times = [r.created_at for r in rows]
        assert times == sorted(times, reverse=True)


def test_list_recent_run_usage_respects_limit(db):
    with get_session() as session:
        run = _make_run(session)
        for i in range(5):
            repo.add_run_usage(
                session,
                run_id=run.id,
                count=1,
                total_usd=float(i),
                stage_costs={"discovery": 0.0, "matching": 0.0, "drafting": 0.0},
            )

    with get_session() as session:
        assert len(repo.list_recent_run_usage(session, limit=2)) == 2


def test_empty_history_lists_nothing(db):
    with get_session() as session:
        assert repo.list_recent_run_usage(session) == []
