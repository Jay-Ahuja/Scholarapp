"""CLI rendering — Rich-based formatters used by scholarapp.cli.

Modules in scholarapp/modules/ never import this (per the conventions doc). Only
cli.py talks to ui. All visual concerns live here so cli.py stays orchestration-only
and modules stay pure.

Compatibility:
- Rich auto-detects non-TTY contexts (pipes, CI). Output stays clean.
- `NO_COLOR=1` disables ANSI colors (Rich respects it).
- stdout and stderr stay separated — `error()` writes to stderr; everything else
  goes to stdout via the module-level `console`.
- `logging` continues to write warnings to stderr unrelated to this module.

For tests: monkeypatch `ui.console` (or `ui.error_console`) with a
`Console(record=True)`. Don't import the module's `console` directly into tests.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from scholarapp.usage import UNPRICED_MARKER

# Production singletons. Tests replace these via monkeypatch.
console: Console = Console()
error_console: Console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Status color tables (run + draft + model)
# ---------------------------------------------------------------------------


_RUN_STATUS_COLORS: dict[str, str] = {
    "pending": "dim",
    "parsing": "yellow",
    "discovering": "yellow",
    "matching": "yellow",
    "drafting": "yellow",
    "review": "cyan",
    "done": "green",
    "failed": "red",
}

_DRAFT_STATUS_COLORS: dict[str, str] = {
    "pending_review": "yellow",
    "approved": "green",
    "rejected": "red",
    "sent": "bright_green",
    "send_disabled": "dim",
}

_MODEL_COLORS: dict[str, str] = {
    "sonnet": "magenta",
    "haiku": "cyan",
    "opus": "yellow",
}


def _run_status_color(status: str) -> str:
    return _RUN_STATUS_COLORS.get(status, "white")


def _draft_status_color(status: str) -> str:
    return _DRAFT_STATUS_COLORS.get(status, "white")


def _model_color(model_short: str) -> str:
    return _MODEL_COLORS.get(model_short, "white")


# ---------------------------------------------------------------------------
# Plain output: section / info / warn / error / spinner
# ---------------------------------------------------------------------------


def section(title: str) -> None:
    """Colored horizontal rule with title — marks the start of a pipeline stage."""
    console.rule(f"[bold cyan]{title}[/bold cyan]", style="cyan")


def info(msg: str) -> None:
    """Plain informational line on stdout."""
    console.print(msg)


def warn(msg: str) -> None:
    """Yellow-prefixed warning line on stdout."""
    console.print(f"[yellow]warning:[/yellow] {msg}")


def error(msg: str) -> None:
    """Red error line on stderr."""
    error_console.print(f"[bold red]Error:[/bold red] {msg}")


@contextmanager
def spinner(text: str):
    """Context manager wrapping Rich's `console.status` spinner.

    Falls back to a plain info line when stdout isn't a TTY (so piped output
    stays readable) — Rich's `status` already handles that internally.
    """
    with console.status(text, spinner="dots"):
        yield


# ---------------------------------------------------------------------------
# Tables + panels
# ---------------------------------------------------------------------------


def runs_table(runs: Iterable[Any]) -> None:
    """`scholar list` — colored table of runs (newest first ordering preserved)."""
    runs_list = list(runs)
    if not runs_list:
        console.print("[dim]No runs yet. Try `scholar run`.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", no_wrap=True)
    table.add_column("Created")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    table.add_column("Field")

    for r in runs_list:
        ts = r.created_at.strftime("%Y-%m-%d %H:%M:%S")
        color = _run_status_color(r.status.value)
        table.add_row(
            r.id,
            ts,
            f"[{color}]{r.status.value}[/{color}]",
            str(r.count),
            r.field,
        )

    console.print(table)


def run_header(run: Any) -> None:
    """One-line header used at the top of `scholar status`."""
    color = _run_status_color(run.status.value)
    console.print(
        f"Run [bold]{run.id}[/bold]  "
        f"status=[{color}]{run.status.value}[/{color}]  "
        f"field=[italic]{run.field}[/italic]  "
        f"count={run.count}"
    )


def drafts_table(drafts: Iterable[Any], professors_by_id: dict[str, Any]) -> None:
    """`scholar status` — per-draft listing with color-coded status."""
    drafts_list = list(drafts)
    if not drafts_list:
        console.print("[dim](no drafts yet)[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Professor")
    table.add_column("Email")
    table.add_column("Status")
    table.add_column("File")

    for d in drafts_list:
        prof = professors_by_id.get(d.professor_id)
        name = (prof.name if prof else "?")[:40]
        email = (prof.email if prof else "?")
        color = _draft_status_color(d.status.value)
        file_repr = d.file_path or "[dim]-[/dim]"
        table.add_row(
            name,
            email,
            f"[{color}]{d.status.value}[/{color}]",
            file_repr,
        )

    console.print(table)


def professors_table(candidates: Iterable[Any]) -> None:
    """The "Discovered professors" listing during `scholar run`."""
    table = Table(
        title="[bold]Discovered professors[/bold]",
        title_justify="left",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Name")
    table.add_column("Institution")
    table.add_column("Email")
    table.add_column("# Works", justify="right")

    for c in candidates:
        table.add_row(
            c.name,
            c.institution,
            c.email,
            str(len(c.recent_works)),
        )

    console.print(table)


def render_usage_table(tracker: Any) -> None:
    """`scholar run`'s end-of-pipeline Claude usage breakdown.

    The text-only equivalent `usage.summarize()` is unchanged — this is just a
    prettier display layer used by the CLI.
    """
    if not tracker.records:
        console.print("[dim](no Claude calls recorded)[/dim]")
        return

    grouped: OrderedDict[str, list] = OrderedDict()
    for r in tracker.records:
        grouped.setdefault(r.label, []).append(r)

    table = Table(
        title="[bold]Claude usage[/bold]",
        title_justify="left",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Call")
    table.add_column("Model")
    table.add_column("In", justify="right")
    table.add_column("Cache", justify="right")
    table.add_column("Out", justify="right")
    table.add_column("Cost", justify="right")

    total_cost = 0.0
    by_model_cost: dict[str, float] = {}
    any_unpriced = False

    for label, records in grouped.items():
        n = len(records)
        in_tot = sum(r.input_tokens for r in records)
        cache_tot = sum(
            r.cache_creation_input_tokens + r.cache_read_input_tokens
            for r in records
        )
        out_tot = sum(r.output_tokens for r in records)
        cost = sum(r.cost_usd for r in records)
        # A group is unpriced when any call used a model id not in PRICING. Its
        # $0.00 cost is "unknown rate," not "free," so flag it rather than show $0.
        group_unpriced = any(not r.is_priced for r in records)

        model_id = records[0].model
        model_short = model_id.split("-")[1] if "-" in model_id else model_id
        mc = _model_color(model_short)

        cost_cell = (
            f"[yellow]{UNPRICED_MARKER}[/yellow]"
            if group_unpriced
            else f"${cost:.4f}"
        )
        label_with_count = f"{label} × {n}" if n > 1 else label
        table.add_row(
            label_with_count,
            f"[{mc}]{model_short}[/{mc}]",
            f"{in_tot:,}",
            f"{cache_tot:,}",
            f"{out_tot:,}",
            cost_cell,
        )

        total_cost += cost
        if group_unpriced:
            any_unpriced = True
        else:
            by_model_cost[model_short] = by_model_cost.get(model_short, 0.0) + cost

    table.add_section()
    total_cell = f"[bold]${total_cost:.4f}[/bold]"
    if any_unpriced:
        total_cell += f" [yellow](+ {UNPRICED_MARKER})[/yellow]"
    table.add_row(
        "[bold]Total[/bold]",
        "",
        "",
        "",
        "",
        total_cell,
    )

    console.print(table)
    breakdown = "  ".join(
        f"[{_model_color(m)}]{m.capitalize()} ${c:.4f}[/{_model_color(m)}]"
        for m, c in by_model_cost.items()
    )
    if breakdown:
        console.print(f"  {breakdown}")


def delivery_report(report: Any) -> None:
    """Counts + optional error panel for `scholar send` output."""
    sent_color = "green" if report.sent else "white"
    err_color = "red" if report.errors else "white"
    counts = (
        f"attempted=[bold]{report.attempted}[/bold]  "
        f"sent=[{sent_color}]{report.sent}[/{sent_color}]  "
        f"send_disabled=[dim]{report.send_disabled}[/dim]  "
        f"[{err_color}]errors={report.errors}[/{err_color}]"
    )
    console.print(counts)
    if report.error_messages:
        err_text = "\n".join(f"• {e}" for e in report.error_messages)
        console.print(
            Panel(
                err_text,
                title="[red]Errors[/red]",
                border_style="red",
                title_align="left",
            )
        )


def sync_report_panel(report: Any) -> None:
    """Counts + optional warning/error panels for `scholar approve` output."""
    counts_color = "red" if report.errors else "white"
    counts = (
        f"updated=[green]{report.updated}[/green]  "
        f"unchanged=[dim]{report.unchanged}[/dim]  "
        f"status_changed=[cyan]{report.status_changed}[/cyan]  "
        f"[{counts_color}]errors={report.errors}[/{counts_color}]"
    )
    console.print(counts)

    if report.warnings:
        warn_text = "\n".join(f"• {w}" for w in report.warnings)
        console.print(
            Panel(
                warn_text,
                title="[yellow]Warnings[/yellow]",
                border_style="yellow",
                title_align="left",
            )
        )

    if report.error_messages:
        err_text = "\n".join(f"• {e}" for e in report.error_messages)
        console.print(
            Panel(
                err_text,
                title="[red]Errors[/red]",
                border_style="red",
                title_align="left",
            )
        )
