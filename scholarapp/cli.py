"""Scholarapp CLI — Typer entrypoint.

Each command is a thin orchestrator. Real work happens in `scholarapp.modules.*` and
`scholarapp.db.*`. All visual rendering goes through `scholarapp.ui`, so this file
stays free of Rich imports beyond a few thin shims.

Commands catch `ScholarError` and render a friendly message via `ui.error`; any
other exception bubbles up as a traceback (treated as a bug).
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

from scholarapp import ui
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


class PipelineStage(str, enum.Enum):
    """Checkpoints where `scholar run --stop-after` can halt."""

    parse = "parse"
    discovery = "discovery"
    matching = "matching"


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
    ui.info(f"[dim]Not yet implemented:[/dim] {command}")
    raise typer.Exit(code=0)


def _run_safely(fn, *args, **kwargs) -> None:
    """Invoke a command body, catching ScholarError and rendering it via ui.error."""
    try:
        fn(*args, **kwargs)
    except ScholarError as e:
        ui.error(str(e))
        raise typer.Exit(code=1) from e


@app.command("init")
def init() -> None:
    """Create the data directory and write a default config.toml if missing."""

    def _impl() -> None:
        settings = load_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if not settings.config_path.exists():
            settings.config_path.write_text(DEFAULT_CONFIG_TOML)
            ui.info(f"[green]Created[/green] {settings.config_path}")
        else:
            ui.info(f"[dim]Already exists:[/dim] {settings.config_path}")
        ui.info(f"Data directory: [bold]{settings.data_dir}[/bold]")

    _run_safely(_impl)


def _summarize_experiences(experiences: list[ingestion.Experience]) -> str:
    """Compact, model-friendly summary of resume experiences for the matching call."""
    if not experiences:
        return ""
    lines: list[str] = []
    for exp in experiences[:6]:
        header = f"- {exp.role} at {exp.org}"
        if exp.years:
            header += f" ({exp.years})"
        lines.append(header)
        for bullet in exp.bullets[:2]:
            lines.append(f"    • {bullet}")
    return "\n".join(lines)


def _parse_resume_cached(pdf_path: Path) -> ingestion.ResumeData:
    """Resume parse, memoized by SHA-256 of the PDF bytes."""
    sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    with get_session() as session:
        cached = repo.get_cached_resume(session, sha)
    if cached is not None:
        ui.info(f"[dim]Using cached resume parse (sha256={sha[:8]})[/dim]")
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
    """End-to-end: parse inputs, discover professors, match relevant works, draft personalized emails. Drafts saved to the DB; `scholar review` writes them as markdown for editing."""

    def _impl() -> None:
        tracker = usage_tracker.UsageTracker()
        usage_tracker.set_tracker(tracker)
        try:
            _run_pipeline(tracker)
        finally:
            usage_tracker.set_tracker(None)
            if tracker.records:
                ui.info("")
                ui.section("Claude usage")
                ui.render_usage_table(tracker)

    def _run_pipeline(tracker: usage_tracker.UsageTracker) -> None:
        settings = load_settings()

        # --- Parse -------------------------------------------------------------
        ui.section("Parse")

        resume_path = inputs / "resume.pdf"
        prompt_path = inputs / "prompt.md"
        template_path = inputs / "template.md"
        for required in (resume_path, prompt_path, template_path):
            if not required.exists():
                raise IngestionError(
                    f"Missing input file: {required}. Expected resume.pdf, "
                    "prompt.md, and template.md inside the --inputs directory."
                )

        ui.info(f"Parsing resume: [bold]{resume_path}[/bold]")
        resume_data = _parse_resume_cached(resume_path)
        ui.info(f"Parsed resume for [bold]{resume_data.name}[/bold].")

        ui.info(f"Parsing prompt: [bold]{prompt_path}[/bold]")
        with ui.spinner("Parsing prompt with Claude..."):
            prompt_data = ingestion.parse_prompt(prompt_path.read_text())
        ui.info(
            f"Parsed prompt: field=[italic]{prompt_data.field}[/italic], "
            f"count=[bold]{prompt_data.count}[/bold], "
            f"goal=[italic]{prompt_data.goal}[/italic]"
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

        ui.info(f"[green]✓[/green] Parsed inputs. run_id=[dim]{run_id}[/dim]")

        if stop_after == PipelineStage.parse:
            ui.info("[dim]Stopped after parse as requested (--stop-after parse).[/dim]")
            return

        # --- Discover ----------------------------------------------------------
        ui.section("Discover")

        with get_session() as session:
            repo.update_run_status(session, run_id, RunStatus.DISCOVERING)

        try:
            with ui.spinner(
                f"Discovering up to {prompt_data.count} professors in "
                f"[italic]{prompt_data.field}[/italic]..."
            ):
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

        ui.professors_table(candidates)

        if stop_after == PipelineStage.discovery:
            ui.info("[dim]Stopped after discovery as requested (--stop-after discovery).[/dim]")
            return

        # --- Match -------------------------------------------------------------
        ui.section("Match")

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
            ui.warn("No professors to match — skipping.")
            return

        experiences_text = _summarize_experiences(resume_data.experiences)
        try:
            with ui.spinner(f"Matching projects for {len(pairs)} professors..."):
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

        ui.info(
            f"[green]✓[/green] Matched projects for [bold]{len(pairs)}[/bold] professors "
            f"([green]{professors_with_matches}[/green] with ≥1 match, "
            f"[bold]{total_matches}[/bold] matches total)."
        )

        if stop_after == PipelineStage.matching:
            ui.info("[dim]Stopped after matching as requested (--stop-after matching).[/dim]")
            return

        # --- Draft -------------------------------------------------------------
        ui.section("Draft")

        with get_session() as session:
            draft_requests: list[drafting.DraftRequest] = []
            request_to_prof_id: list[str] = []
            for prof in repo.list_professors_for_run(session, run_id):
                matched_rows = repo.list_matched_projects_for_professor(session, prof.id)
                if not matched_rows:
                    ui.warn(f"skipping {prof.name} — no matched projects")
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
                    ui.warn(f"skipping {prof.name} — matched rows had no readable projects")
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
            ui.warn("No professors with matched projects — nothing to draft.")
            return

        try:
            with ui.spinner(f"Drafting {len(draft_requests)} emails..."):
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

        # --- Done --------------------------------------------------------------
        ui.section("Done")
        ui.info(
            f"[green]✓[/green] Drafted [bold]{len(email_drafts)}[/bold] emails. "
            f"run_id=[dim]{run_id}[/dim]"
        )
        ui.info(
            f"  → Run [bold cyan]scholar review {run_id}[/bold cyan] to edit them."
        )

    _run_safely(_impl)


@app.command("list")
def list_runs() -> None:
    """List all runs, newest first."""

    def _impl() -> None:
        with get_session() as session:
            runs = repo.list_runs(session)
        ui.runs_table(runs)

    _run_safely(_impl)


@app.command("review")
def review_cmd(run_id: str = typer.Argument(..., help="Run ID to review.")) -> None:
    """Write drafts to disk as editable markdown; open the directory in $EDITOR."""

    def _impl() -> None:
        path = review_module.write_drafts_to_disk(run_id)
        ui.info(f"[green]✓[/green] Wrote drafts to [bold]{path}[/bold]")
        editor = os.environ.get("EDITOR")
        if editor:
            try:
                subprocess.run([editor, str(path)], check=False)
            except FileNotFoundError:
                ui.warn(
                    f"Could not launch $EDITOR ({editor!r}). Edit the files yourself."
                )
        else:
            ui.info(
                "  → Open the files in your editor of choice "
                "(set [bold]$EDITOR[/bold] to auto-launch)."
            )
        ui.info(
            f"  → When you're done editing, run "
            f"[bold cyan]scholar approve {run_id}[/bold cyan] to sync your changes."
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
                ui.warn(f"{fp.name}: {e}")
                continue
            if parsed.status.strip().lower() == "pending_review":
                review_module.write_status_in_file(fp, "approved")
                approved_in_file += 1

        ui.info(
            f"Marked [bold]{approved_in_file}[/bold] draft file(s) as approved. "
            "Syncing to DB..."
        )

        report = review_module.sync_drafts_from_disk(run_id)
        ui.info("")
        ui.sync_report_panel(report)

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
        ui.run_header(run)
        ui.info("")
        ui.drafts_table(drafts, professors)

    _run_safely(_impl)


@app.command("send")
def send(run_id: str = typer.Argument(..., help="Run ID whose approved drafts to send.")) -> None:
    """Send approved drafts (gated behind SEND_ENABLED)."""
    _not_implemented("send")


if __name__ == "__main__":
    app()
