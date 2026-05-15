import itertools
import os
import re
import secrets
import shlex
import subprocess
from pathlib import Path

import launchd
import typer
from rich.console import Console
from rich.table import Table


STATE_DIR = Path.home() / ".cronatron"
LOG_DIR = STATE_DIR / "logs"
LABEL_PREFIX = "com.cronatron."
MAX_CALENDAR_ENTRIES = 500

app = typer.Typer(
    help="Schedule commands with macOS launchd.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
error_console = Console(stderr=True)

class Launchctl:
    def __init__(self, label_prefix=LABEL_PREFIX):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.label_prefix = label_prefix
        self.domain = f"gui/{os.getuid()}"

    def label(self, job_id):
        return f"{self.label_prefix}{job_id}"

    def target(self, job_id):
        return f"{self.domain}/{self.label(job_id)}"

    def run(self, *args, job=None, silent=False, verbose=False):
        launch_arguments = list(args)
        if job is not None:
            launch_arguments.append(self.target(job))
        full_cmd = ["launchctl", *launch_arguments]
        if verbose:
            console.print(f"$ {shlex.join(full_cmd)}", style="dim", highlight=False)
        result = subprocess.run(full_cmd, capture_output=True, text=True, check=False)
        if not silent and result.returncode != 0:
            output = (result.stderr or result.stdout).strip()
            error_console.print(f"[red]{args[0]}:[/red] {output}")
            raise typer.Exit(1)
        return result

    def exists(self, job_id):
        return launchd.plist.discover_filename(self.label(job_id), launchd.plist.USER) is not None

    def read_plist(self, job_id):
        return launchd.plist.read(self.label(job_id), launchd.plist.USER)

    def write_plist(self, job_id, plist):
        return Path(launchd.plist.write(self.label(job_id), plist, launchd.plist.USER))

    def register(self, job_id, plist, verbose=False):
        plist_path = self.write_plist(job_id, plist)
        try:
            self.run("bootstrap", self.domain, str(plist_path), verbose=verbose)
        except typer.Exit:
            plist_path.unlink(missing_ok=True)
            raise

    def unregister(self, job_id, verbose=False):
        plist_path = self.plist_path(job_id)
        self.run("bootout", job=job_id, verbose=verbose)
        self.run("enable", job=job_id, verbose=verbose)
        plist_path.unlink()

    def trigger(self, job_id, verbose=False):
        self.plist_path(job_id)
        self.run("kickstart", "-k", job=job_id, verbose=verbose)

    def pause(self, job_id, verbose=False):
        self.plist_path(job_id)
        self.run("bootout", job=job_id, verbose=verbose)
        self.run("disable", job=job_id, verbose=verbose)

    def resume(self, job_id, verbose=False):
        plist_path = self.plist_path(job_id)
        self.run("enable", job=job_id, verbose=verbose)
        self.run("bootstrap", self.domain, str(plist_path), verbose=verbose)

    def existing_labels(self):
        return sorted([job.label for job in launchd.jobs() if job.label.startswith(self.label_prefix)])

    def disabled_labels(self, verbose=False):
        result = self.run("print-disabled", self.domain, verbose=verbose)
        disabled = set()
        pattern = re.compile(r'"([^"]+)"\s*=>\s*(\w+)')
        for line in result.stdout.splitlines():
            match = pattern.search(line)
            if match and match.group(2).lower() == "disabled":
                disabled.add(match.group(1))
        return disabled

    def loaded_labels(self, verbose=False):
        result = self.run("list", verbose=verbose)
        lines = result.stdout.splitlines()[1:]
        return [line.split("\t")[2] for line in lines]

    def is_paused(self, job_id, verbose=False):
        return self.label(job_id) in self.disabled_labels(verbose=verbose)

    def plist_path(self, job_id):
        discovered_filename = launchd.plist.discover_filename(self.label(job_id), launchd.plist.USER)
        if discovered_filename is None:
            error_console.print(f"[red]× unknown job:[/red] {job_id!r}")
            raise typer.Exit(1)
        return Path(discovered_filename)

    def log_path(self, job_id) -> Path | None:
        plist = self.read_plist(job_id)
        stdout_path = str(plist.get("StandardOutPath", "") or "").strip()
        return Path(stdout_path) if stdout_path else None

    def run_count(self, job_id, verbose=False):
        result = self.run("print", self.target(job_id), verbose=verbose)
        pattern = re.compile(r"^\s*runs\s*=\s*(\d+)\s*$", re.MULTILINE)
        match = pattern.search(result.stdout)
        return int(match.group(1)) if match else 0


launchctl = Launchctl()


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------
_FIELDS = [
    ("minute", "Minute", 0, 59),
    ("hour", "Hour", 0, 23),
    ("day", "Day", 1, 31),
    ("weekday", "Weekday", 0, 7),
    ("month", "Month", 1, 12),
]


def _parse_field(name, raw, lo, hi):
    parsed_values: list[int] = []
    for segment in raw.split(","):
        segment = segment.strip()
        if not segment:
            error_console.print(f"[red]--{name}: empty segment[/red]")
            raise typer.Exit(2)
        try:
            parsed_int = int(segment)
        except ValueError:
            error_console.print(f"[red]--{name}: not an integer:[/red] {segment!r}")
            raise typer.Exit(2)
        if parsed_int < lo or parsed_int > hi:
            error_console.print(f"[red]--{name}: {parsed_int} out of range {lo}-{hi}[/red]")
            raise typer.Exit(2)
        parsed_values.append(parsed_int)
    return parsed_values


def _build_schedule(minute, hour, day, weekday, month, every):
    calendar_options = {"minute": minute, "hour": hour, "day": day, "weekday": weekday, "month": month}
    has_calendar_schedule = any(value is not None for value in calendar_options.values())
    if every is not None and has_calendar_schedule:
        error_console.print("[red]Use --every or calendar flags, not both.[/red]")
        raise typer.Exit(2)
    if every is None and not has_calendar_schedule:
        error_console.print("[red]Provide a schedule: --minute/--hour/--day/--weekday/--month or --every[/red]")
        raise typer.Exit(2)
    if every is not None:
        if every <= 0:
            error_console.print("[red]--every must be a positive integer[/red]")
            raise typer.Exit(2)
        return {"StartInterval": every}

    parsed = {}
    for field_name, calendar_key, lo, hi in _FIELDS:
        raw = calendar_options[field_name]
        if raw is not None:
            parsed[calendar_key] = _parse_field(field_name, raw, lo, hi)

    calendar_keys = list(parsed.keys())
    value_lists = [parsed[calendar_key] for calendar_key in calendar_keys]
    total = 1
    for values in value_lists:
        total *= len(values)
    if total > MAX_CALENDAR_ENTRIES:
        error_console.print(
            f"[red]Schedule is {total} entries (max {MAX_CALENDAR_ENTRIES}).[/red]\n"
            "[dim]Use --every for short intervals.[/dim]"
        )
        raise typer.Exit(2)

    entries = [
        {
            calendar_key: calendar_value
            for calendar_key, calendar_value in zip(calendar_keys, combo, strict=True)
        }
        for combo in itertools.product(*value_lists)
    ]
    return {"StartCalendarInterval": entries}


def _schedule_str(plist):
    if "StartInterval" in plist:
        return f"every {plist['StartInterval']}s"
    stored_schedule = plist.get("CronatronMeta", {}).get("schedule", {})
    parts = []
    for field_name in ("minute", "hour", "day", "weekday", "month"):
        field_value = stored_schedule.get(field_name)
        if field_value not in (None, ""):
            parts.append(f"{field_name}={field_value}")
    return " ".join(parts) if parts else "?"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

JOB_ID = typer.Argument(..., help="Job id (from list)")
VERBOSE = typer.Option(False, "--verbose", "-v", help="Print underlying launchctl commands")

@app.command("register")
def register(
    name: str | None = typer.Option(None, "--name", "-n", help="Label shown in list output (defaults to the command)"),
    command: str = typer.Argument(..., help="Command string"),
    minute: str | None = typer.Option(None, "--minute", "-m", help="0-59 (comma-separated)"),
    hour: str | None = typer.Option(None, "--hour", "-h", help="0-23 (comma-separated)"),
    day: str | None = typer.Option(None, "--day", "-d", help="1-31 (comma-separated)"),
    weekday: str | None = typer.Option(None, "--weekday", "-w", help="0-7  (comma-separated, 0 = Sunday)"),
    month: str | None = typer.Option(None, "--month", "-M", help="1-12 (comma-separated)"),
    every: int | None = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    verbose: bool = VERBOSE,
):
    """Add a job."""
    schedule = _build_schedule(minute, hour, day, weekday, month, every)

    # generate job_id
    job_id = secrets.token_hex(3)
    while launchctl.exists(job_id):
        job_id = secrets.token_hex(3)

    # create plist
    log_path = LOG_DIR / f"{job_id}.log"
    plist = {
        "Label": launchctl.label(job_id),
        "ProgramArguments": ["/bin/sh", "-c", command],
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "RunAtLoad": False,
        "CronatronMeta": {
            "name": name or command[:40],
            "command": command,
            "schedule": {
                "minute": minute or "",
                "hour": hour or "",
                "day": day or "",
                "weekday": weekday or "",
                "month": month or "",
                "every": "" if every is None else every,
            },
        },
    }
    plist.update(schedule)
    if "PATH" in os.environ:
        plist["EnvironmentVariables"] = {"PATH": os.environ["PATH"]}

    # register job
    launchctl.register(job_id, plist, verbose=verbose)
    console.print(f"[green]✓ registered[/green] [cyan]{job_id}[/cyan]  {_schedule_str(plist)}  {command}")


@app.command("unregister")
def unregister(job_id: str = JOB_ID, verbose: bool = VERBOSE):
    """Remove a job."""
    launchctl.unregister(job_id, verbose=verbose)
    console.print(f"[green]✓ removed[/green] {job_id}")


@app.command("trigger")
def trigger(job_id: str = JOB_ID, verbose: bool = VERBOSE):
    """Run a job."""
    if launchctl.is_paused(job_id, verbose=verbose):
        console.print(f"[cyan]{job_id}[/cyan] [yellow]is paused and cannot run.[/yellow]")
        console.print(f"[dim]Run[/dim] [bold]cronatron resume {job_id}[/bold] [dim]and try again.[/dim]")
        return
    with console.status(f"[cyan]Running[/cyan] {job_id}..."):
        launchctl.trigger(job_id, verbose=verbose)
    console.print(f"[green]✓ ran[/green] {job_id}")


@app.command("pause")
def pause(job_id: str = JOB_ID, verbose: bool = VERBOSE):
    """Pause a job."""
    if launchctl.is_paused(job_id, verbose=verbose):
        console.print(f"[yellow]⚠ already paused:[/yellow] {job_id}")
        return
    launchctl.pause(job_id, verbose=verbose)
    console.print(f"[green]✓ paused[/green] {job_id}")


@app.command("resume")
def resume(job_id: str = JOB_ID, verbose: bool = VERBOSE):
    """Resume a job."""
    if not launchctl.is_paused(job_id, verbose=verbose):
        console.print(f"[yellow]⚠ not paused:[/yellow] {job_id}")
        return
    launchctl.resume(job_id, verbose=verbose)
    console.print(f"[green]✓ resumed[/green] {job_id}")


@app.command()
def logs(
    job_id: str = JOB_ID,
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep streaming new lines"),
    lines: int = typer.Option(50, "--lines", "-n", help="Initial line count"),
):
    """Show job logs."""
    launchctl.plist_path(job_id)
    log_path = LOG_DIR / f"{job_id}.log"
    if not log_path.exists():
        error_console.print(f"[yellow]⚠ No log yet:[/yellow] {log_path}")
        raise typer.Exit(1)
    tail_argv = ["tail", f"-n{lines}"]
    if follow:
        tail_argv.append("-f")
    tail_argv.append(str(log_path))
    try:
        subprocess.run(tail_argv)
    except KeyboardInterrupt:
        pass


@app.command("list")
def list_jobs(verbose: bool = VERBOSE):
    """List jobs."""
    existing_labels = launchctl.existing_labels()
    disabled = launchctl.disabled_labels(verbose=verbose)
    loaded = launchctl.loaded_labels(verbose=verbose)

    if not existing_labels:
        console.print("[dim]No jobs.[/dim]")
        return

    table = Table()
    table.add_column("id", style="cyan")
    table.add_column("name")
    table.add_column("status")
    table.add_column("schedule", style="magenta")
    table.add_column("runs", justify="right")
    table.add_column("command", overflow="fold")
    if verbose:
        table.add_column("target", overflow="fold")
        table.add_column("plist", overflow="fold")
        table.add_column("logs", overflow="fold")

    for registered_label in existing_labels:
        job_id = registered_label[len(LABEL_PREFIX) :]
        try:
            plist = launchctl.read_plist(job_id)
        except Exception as exc:
            error_console.print(f"[yellow]⚠ Skip {job_id}:[/yellow] {exc}")
            continue

        metadata = plist.get("CronatronMeta", {})
        job_label = plist.get("Label", launchctl.label(job_id))
        if job_label in disabled:
            status = "[yellow]paused[/yellow]"
        elif job_label in loaded:
            status = "[green]active[/green]"
        else:
            status = "[red]not loaded[/red]"
        runs = str(launchctl.run_count(job_id, verbose=verbose))

        row = [job_id, metadata.get("name", ""), status, _schedule_str(plist), runs, metadata.get("command", "")]
        if verbose:
            job_log_path = launchctl.log_path(job_id)
            row.extend(
                [
                    launchctl.target(job_id),
                    str(launchctl.plist_path(job_id)),
                    str(job_log_path) if job_log_path else "—",
                ]
            )

        table.add_row(*row)
    console.print(table)


@app.command("reset")
def reset(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    verbose: bool = VERBOSE,
):
    """Remove all jobs."""
    existing_labels = launchctl.existing_labels()
    if not existing_labels:
        console.print("[dim]No jobs to remove.[/dim]")
        return
    job_count = len(existing_labels)
    if not yes and not typer.confirm(f"Remove all {job_count} job(s)?"):
        console.print("Cancelled.")
        raise typer.Exit(0)
    for registered_label in existing_labels:
        job_id = registered_label[len(LABEL_PREFIX) :]
        launchctl.unregister(job_id, verbose=verbose)
        console.print(f"[dim]{job_id}[/dim]")
    console.print(f"[green]✓ removed[/green] {job_count} job(s)")