"""Scholarapp CLI — Typer entrypoint.

Each command is a thin orchestrator. Real work happens in `scholarapp.modules.*` and
`scholarapp.db.*`. Commands catch `ScholarError` and render a friendly message; any
other exception bubbles up as a traceback (treated as a bug).

Step 1 ships only the scaffold: `init` creates the data directory; every other command
prints "Not yet implemented".
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import sys
from pathlib import Path

import typer

from scholarapp import usage as usage_tracker
from scholarapp.config import load_settings
from scholarapp.db import repo
from scholarapp.db.models import RunStatus
from scholarapp.db.session import get_session
from scholarapp.errors import IngestionError, NotFoundError, ScholarError
from scholarapp.modules import discovery, ingestion

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s: %(message)s",
    stream=sys.stderr,
)

app = typer.Typer(
    name="scholar",
    help="Draft personalized cold emails to professors.",
    no_args_is_help=True,
    add_completion=False,
)


DEFAULT_CONFIG_TOML = """# Scholarapp config
# Runtime values come from env vars (see .env.example at the repo root).
# This file is reserved for future per-user preferences.

[app]
version = "0.1.0"
"""


def _not_implemented(command: str) -> None:
    typer.echo(f"Not yet implemented: {command}")
    raise typer.Exit(code=0)


def _run_safely(fn, *args, **kwargs) -> None:
    """Invoke a command body, catching ScholarError and rendering it cleanly."""
    try:
        fn(*args, **kwargs)
    except ScholarError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


@app.command("init")
def init() -> None:
    """Create the data directory and write a default config.toml if missing."""

    def _impl() -> None:
        settings = load_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if not settings.config_path.exists():
            settings.config_path.write_text(DEFAULT_CONFIG_TOML)
            typer.echo(f"Created {settings.config_path}")
        else:
            typer.echo(f"Already exists: {settings.config_path}")
        typer.echo(f"Data directory: {settings.data_dir}")

    _run_safely(_impl)


def _parse_resume_cached(pdf_path: Path) -> ingestion.ResumeData:
    """Resume parse, memoized by SHA-256 of the PDF bytes.

    First call with a given PDF hits Claude (~$0.015 on Sonnet); every subsequent
    call with the same bytes is free. Edit the resume by one byte and the hash
    changes, so the cache invalidates automatically.
    """
    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    with get_session() as session:
        cached = repo.get_cached_resume(session, sha)
    if cached is not None:
        typer.echo(f"Using cached resume parse (sha256={sha[:8]})")
        return ingestion.ResumeData.model_validate(cached)
    data = ingestion.parse_resume(pdf_path)
    with get_session() as session:
        repo.cache_resume(session, sha, data.model_dump())
    return data


@app.command("run")
def run(
    inputs: Path = typer.Option(
        Path("./inputs"),
        "--inputs",
        help="Directory containing resume.pdf, prompt.md, and template.md.",
    ),
) -> None:
    """Parse inputs, discover professors with verified emails, persist them. (Matching + drafting land in Steps 5-6.)"""

    def _impl() -> None:
        tracker = usage_tracker.UsageTracker()
        usage_tracker.set_tracker(tracker)
        try:
            _run_pipeline(tracker)
        finally:
            usage_tracker.set_tracker(None)
            if tracker.records:
                typer.echo("")
                typer.echo(usage_tracker.summarize(tracker))

    def _run_pipeline(tracker: usage_tracker.UsageTracker) -> None:
        settings = load_settings()

        resume_path = inputs / "resume.pdf"
        prompt_path = inputs / "prompt.md"
        template_path = inputs / "template.md"
        for required in (resume_path, prompt_path, template_path):
            if not required.exists():
                raise IngestionError(
                    f"Missing input file: {required}. Expected resume.pdf, "
                    "prompt.md, and template.md inside the --inputs directory."
                )

        typer.echo(f"Parsing resume: {resume_path}")
        resume_data = _parse_resume_cached(resume_path)
        typer.echo(f"Parsed resume for {resume_data.name}.")

        typer.echo(f"Parsing prompt: {prompt_path}")
        prompt_data = ingestion.parse_prompt(prompt_path.read_text())
        typer.echo(
            f"Parsed prompt: field={prompt_data.field!r}, count={prompt_data.count}, "
            f"goal={prompt_data.goal!r}"
        )

        template_text = template_path.read_text()

        with get_session() as session:
            run_row = repo.create_run(
                session,
                field=prompt_data.field,
                goal=prompt_data.goal,
                considerations=prompt_data.considerations,
                count=prompt_data.count,
                resume_path=str(resume_path.resolve()),
                template_text=template_text,
            )

            run_inputs_dir = settings.runs_dir / run_row.id / "inputs"
            run_inputs_dir.mkdir(parents=True, exist_ok=True)
            copied_resume = run_inputs_dir / "resume.pdf"
            shutil.copy2(resume_path, copied_resume)
            shutil.copy2(prompt_path, run_inputs_dir / "prompt.md")
            shutil.copy2(template_path, run_inputs_dir / "template.md")

            run_row.resume_path = str(copied_resume.resolve())
            run_id = run_row.id

        typer.echo(f"Parsed inputs. run_id={run_id}")

        # --- Step 4: discovery -------------------------------------------------
        with get_session() as session:
            repo.update_run_status(session, run_id, RunStatus.DISCOVERING)

        typer.echo(
            f"Discovering up to {prompt_data.count} professors in {prompt_data.field!r}..."
        )
        try:
            candidates = asyncio.run(
                discovery.find_professors(
                    field=prompt_data.field,
                    count=prompt_data.count,
                    user_interests=resume_data.interests,
                )
            )
        except ScholarError as e:
            with get_session() as session:
                repo.update_run_status(
                    session, run_id, RunStatus.FAILED, error=f"discovery failed: {e}"
                )
            raise

        with get_session() as session:
            for c in candidates:
                prof = repo.add_professor(
                    session,
                    run_id=run_id,
                    name=c.name,
                    institution=c.institution,
                    email=c.email,
                    openalex_id=c.openalex_id,
                    faculty_page_url=c.faculty_page_url,
                )
                for w in c.recent_works:
                    repo.add_project(
                        session,
                        professor_id=prof.id,
                        title=w.title,
                        url=w.url,
                        year=w.year,
                        abstract=w.abstract,
                        raw_json={"openalex_id": w.openalex_id},
                    )
            repo.update_run_status(session, run_id, RunStatus.MATCHING)

        typer.echo(f"Discovered {len(candidates)} professors. run_id={run_id}")

    _run_safely(_impl)


@app.command("list")
def list_runs() -> None:
    """List all runs, newest first."""

    def _impl() -> None:
        with get_session() as session:
            runs = repo.list_runs(session)
        if not runs:
            typer.echo("No runs yet. Try `scholar run`.")
            return
        typer.echo(
            f"{'ID':<36}  {'CREATED':<19}  {'STATUS':<11}  {'COUNT':>5}  FIELD"
        )
        for r in runs:
            ts = r.created_at.strftime("%Y-%m-%d %H:%M:%S")
            typer.echo(
                f"{r.id:<36}  {ts:<19}  {r.status.value:<11}  {r.count:>5}  {r.field}"
            )

    _run_safely(_impl)


@app.command("review")
def review(run_id: str = typer.Argument(..., help="Run ID to review.")) -> None:
    """Write drafts to disk and open them in $EDITOR."""
    _not_implemented("review")


@app.command("approve")
def approve(
    run_id: str = typer.Argument(..., help="Run ID to approve drafts for."),
    only: str | None = typer.Option(
        None, "--only", help="Approve only the draft with this slug."
    ),
) -> None:
    """Mark drafts as approved (ready for send)."""
    _not_implemented("approve")


@app.command("status")
def status(run_id: str = typer.Argument(..., help="Run ID to inspect.")) -> None:
    """Show per-draft status for a run."""

    def _impl() -> None:
        with get_session() as session:
            run = repo.get_run(session, run_id)
            if run is None:
                raise NotFoundError(f"No run with id {run_id}")
            drafts = repo.list_drafts_for_run(session, run_id)
            professors = {
                p.id: p for p in repo.list_professors_for_run(session, run_id)
            }
        typer.echo(f"Run {run.id}  status={run.status.value}  field={run.field}  count={run.count}")
        if not drafts:
            typer.echo("(no drafts yet)")
            return
        typer.echo(
            f"\n{'PROFESSOR':<25}  {'EMAIL':<35}  {'STATUS':<14}  FILE"
        )
        for d in drafts:
            prof = professors.get(d.professor_id)
            name = (prof.name if prof else "?")[:25]
            email = (prof.email if prof else "?")[:35]
            file_path = d.file_path or "-"
            typer.echo(
                f"{name:<25}  {email:<35}  {d.status.value:<14}  {file_path}"
            )

    _run_safely(_impl)


@app.command("send")
def send(run_id: str = typer.Argument(..., help="Run ID whose approved drafts to send.")) -> None:
    """Send approved drafts (gated behind SEND_ENABLED)."""
    _not_implemented("send")


if __name__ == "__main__":
    app()
