"""Scholarapp CLI — Typer entrypoint.

Each command is a thin orchestrator. Real work happens in `scholarapp.modules.*` and
`scholarapp.db.*`. Commands catch `ScholarError` and render a friendly message; any
other exception bubbles up as a traceback (treated as a bug).

Step 1 ships only the scaffold: `init` creates the data directory; every other command
prints "Not yet implemented".
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer


class PipelineStage(str, enum.Enum):
    """Checkpoints where `scholar run --stop-after` can halt."""

    parse = "parse"
    discovery = "discovery"
    matching = "matching"

from scholarapp import usage as usage_tracker
from scholarapp.config import load_settings
from scholarapp.db import repo
from scholarapp.db.models import DraftStatus, Project, RunStatus
from scholarapp.db.session import get_session
from scholarapp.errors import IngestionError, NotFoundError, ScholarError
from scholarapp.modules import (
    discovery,
    drafting,
    ingestion,
    matching,
    review as review_module,
)

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


def _summarize_experiences(experiences: list[ingestion.Experience]) -> str:
    """Compact, model-friendly summary of resume experiences for the matching call."""
    if not experiences:
        return ""
    lines: list[str] = []
    for exp in experiences[:6]:  # cap at 6 entries to keep tokens bounded
        header = f"- {exp.role} at {exp.org}"
        if exp.years:
            header += f" ({exp.years})"
        lines.append(header)
        for bullet in exp.bullets[:2]:  # first 2 bullets each
            lines.append(f"    • {bullet}")
    return "\n".join(lines)


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
    stop_after: PipelineStage | None = typer.Option(
        None,
        "--stop-after",
        help="Stop the pipeline after this stage. Saves cost for cheap exploration.",
        case_sensitive=False,
    ),
) -> None:
    """End-to-end: parse inputs, discover professors, match relevant works, draft personalized emails. Drafts saved to the DB; Step 7 writes them as markdown for editing."""

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

        if stop_after == PipelineStage.parse:
            typer.echo("Stopped after parse as requested (--stop-after parse).")
            return

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

        typer.echo(f"Discovered {len(candidates)} professors:")
        for c in candidates:
            typer.echo(f"  - {c.name} @ {c.institution}  <{c.email}>")
        typer.echo(f"run_id={run_id}")

        if stop_after == PipelineStage.discovery:
            typer.echo("Stopped after discovery as requested (--stop-after discovery).")
            return

        # --- Step 5: matching -------------------------------------------------
        with get_session() as session:
            professors = repo.list_professors_for_run(session, run_id)
            pairs: list[tuple[str, list[matching.ProjectForMatching]]] = []
            for prof in professors:
                projects = repo.list_projects_for_professor(session, prof.id)
                pairs.append(
                    (
                        prof.name,
                        [
                            matching.ProjectForMatching(
                                project_id=pr.id,
                                title=pr.title,
                                url=pr.url,
                                abstract=pr.abstract,
                                year=pr.year,
                            )
                            for pr in projects
                        ],
                    )
                )

        if not pairs:
            typer.echo("No professors to match — skipping.")
            return

        typer.echo(f"Matching projects for {len(pairs)} professors...")
        experiences_text = _summarize_experiences(resume_data.experiences)
        try:
            matched_lists = asyncio.run(
                matching.match_projects_for_run(
                    pairs=pairs,
                    user_interests=resume_data.interests,
                    user_experiences=experiences_text,
                )
            )
        except ScholarError as e:
            with get_session() as session:
                repo.update_run_status(
                    session, run_id, RunStatus.FAILED, error=f"matching failed: {e}"
                )
            raise

        with get_session() as session:
            total_matches = 0
            professors_with_matches = 0
            for prof, matches in zip(professors, matched_lists):
                if matches:
                    professors_with_matches += 1
                for m in matches:
                    repo.add_matched_project(
                        session,
                        professor_id=prof.id,
                        project_id=m.project_id,
                        why_relevant=m.why_relevant,
                    )
                    total_matches += 1
            repo.update_run_status(session, run_id, RunStatus.DRAFTING)

        typer.echo(
            f"Matched projects for {len(pairs)} professors "
            f"({professors_with_matches} with ≥1 match, {total_matches} matches total). "
            f"run_id={run_id}"
        )

        if stop_after == PipelineStage.matching:
            typer.echo("Stopped after matching as requested (--stop-after matching).")
            return

        # --- Step 6: drafting -------------------------------------------------
        with get_session() as session:
            draft_requests: list[drafting.DraftRequest] = []
            request_to_prof_id: list[str] = []
            for prof in repo.list_professors_for_run(session, run_id):
                matched_rows = repo.list_matched_projects_for_professor(session, prof.id)
                if not matched_rows:
                    typer.echo(f"  skipping {prof.name} — no matched projects")
                    continue
                matched_pydantic: list[matching.MatchedProject] = []
                for mp in matched_rows:
                    project = session.get(Project, mp.project_id)
                    if project is None:
                        continue
                    matched_pydantic.append(
                        matching.MatchedProject(
                            project_id=mp.project_id,
                            title=project.title,
                            url=project.url,
                            why_relevant=mp.why_relevant,
                        )
                    )
                if not matched_pydantic:
                    typer.echo(f"  skipping {prof.name} — matched rows had no readable projects")
                    continue
                draft_requests.append(
                    drafting.DraftRequest(
                        professor=drafting.ProfessorForDrafting(
                            name=prof.name,
                            institution=prof.institution,
                            email=prof.email,
                        ),
                        matched=matched_pydantic,
                    )
                )
                request_to_prof_id.append(prof.id)

        if not draft_requests:
            typer.echo("No professors with matched projects — nothing to draft.")
            return

        typer.echo(f"Drafting {len(draft_requests)} emails...")
        try:
            email_drafts = asyncio.run(
                drafting.draft_emails_for_run(
                    template=template_text,
                    resume=resume_data,
                    requests=draft_requests,
                    goal=prompt_data.goal,
                    considerations=prompt_data.considerations,
                )
            )
        except ScholarError as e:
            with get_session() as session:
                repo.update_run_status(
                    session, run_id, RunStatus.FAILED, error=f"drafting failed: {e}"
                )
            raise

        with get_session() as session:
            for prof_id, email_draft in zip(request_to_prof_id, email_drafts):
                repo.add_draft(
                    session,
                    run_id=run_id,
                    professor_id=prof_id,
                    subject=email_draft.subject,
                    body=email_draft.body,
                    status=DraftStatus.PENDING_REVIEW,
                )
            repo.update_run_status(session, run_id, RunStatus.REVIEW)

        typer.echo(f"Drafted {len(email_drafts)} emails. run_id={run_id}")
        typer.echo(f"Run `scholar review {run_id}` to see them.")

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
def review_cmd(run_id: str = typer.Argument(..., help="Run ID to review.")) -> None:
    """Write drafts to disk as editable markdown; open the directory in $EDITOR."""

    def _impl() -> None:
        path = review_module.write_drafts_to_disk(run_id)
        typer.echo(f"Wrote drafts to {path}")
        editor = os.environ.get("EDITOR")
        if editor:
            try:
                subprocess.run([editor, str(path)], check=False)
            except FileNotFoundError:
                typer.echo(
                    f"Could not launch $EDITOR ({editor!r}). Edit the files yourself."
                )
        else:
            typer.echo(
                "Open the files in your editor of choice (set $EDITOR to auto-launch)."
            )
        typer.echo(
            f"When you're done editing, run `scholar approve {run_id}` to sync your changes."
        )

    _run_safely(_impl)


@app.command("approve")
def approve_cmd(
    run_id: str = typer.Argument(..., help="Run ID to approve drafts for."),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Approve only the draft whose filename slug matches this value.",
    ),
) -> None:
    """Set status: approved on drafts (in their .md files) and sync to the DB."""

    def _impl() -> None:
        settings = load_settings()
        drafts_dir = settings.drafts_root / run_id
        if not drafts_dir.exists():
            raise NotFoundError(
                f"No drafts directory at {drafts_dir} for run {run_id}. "
                f"Run `scholar review {run_id}` first."
            )

        if only:
            target_files = [drafts_dir / f"{only}.md"]
            if not target_files[0].exists():
                raise NotFoundError(
                    f"No draft file matching slug {only!r} in {drafts_dir}"
                )
        else:
            target_files = sorted(drafts_dir.glob("*.md"))

        approved_in_file = 0
        for fp in target_files:
            try:
                parsed = review_module.parse_draft_file(fp)
            except Exception as e:
                typer.echo(f"WARNING: {fp.name}: {e}", err=True)
                continue
            if parsed.status.strip().lower() == "pending_review":
                review_module.write_status_in_file(fp, "approved")
                approved_in_file += 1

        typer.echo(
            f"Marked {approved_in_file} draft file(s) as approved. "
            f"Syncing to DB..."
        )

        report = review_module.sync_drafts_from_disk(run_id)
        typer.echo("")
        typer.echo(report.summary())

    _run_safely(_impl)


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
