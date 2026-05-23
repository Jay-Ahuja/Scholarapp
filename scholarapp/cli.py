"""Scholarapp CLI — Typer entrypoint.

Each command is a thin orchestrator. Real work happens in `scholarapp.modules.*` and
`scholarapp.db.*`. Commands catch `ScholarError` and render a friendly message; any
other exception bubbles up as a traceback (treated as a bug).

Step 1 ships only the scaffold: `init` creates the data directory; every other command
prints "Not yet implemented".
"""

from __future__ import annotations

import typer

from scholarapp.config import load_settings
from scholarapp.db import repo
from scholarapp.db.session import get_session
from scholarapp.errors import NotFoundError, ScholarError

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


@app.command("run")
def run() -> None:
    """Run the full pipeline: parse inputs, discover, match, draft."""
    _not_implemented("run")


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
