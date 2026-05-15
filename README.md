# cronatron [![PyPI version](https://img.shields.io/pypi/v/cronatron.svg?123)](https://pypi.org/project/cronatron/)

Schedule commands on macOS using launchd — a friendlier interface to `launchctl`.

> *`crontab` is too simple! `launchd` is too complex! `cronatron` is just right!*

```bash
# Every 30 seconds
cronatron register "echo hello" --every 30

# Every day at 9:00 AM
cronatron register "echo hello" --hour 9 --minute 0

# Weekdays at 8:30 AM
cronatron register "echo hello" --hour 8 --minute 30 --weekday 1,2,3,4,5
```

## Installation

```bash
uv tool install cronatron

# or run without installing
uvx cronatron
```

## Usage

### Scheduling

Jobs can be scheduled with calendar flags or an interval:

**Calendar flags** (`--minute`, `--hour`, `--day`, `--weekday`, `--month`) accept comma-separated values and can be combined. `--every` accepts a number of seconds. The two modes are mutually exclusive.

```
--name      -n   Label shown in list output (defaults to the command)
--minute    -m   0–59 (comma-separated)
--hour      -h   0–23 (comma-separated)
--day       -d   1–31 (comma-separated)
--weekday   -w   0–7  (comma-separated, 0 = Sunday)
--month     -M   1–12 (comma-separated)
--every     -e   Run every N seconds
--verbose   -v   Print underlying launchctl commands
```

### Examples

```bash
# Register a named job that runs every minute
cronatron register "date >> /tmp/out.txt" --every 60 --name "timestamp"

# List jobs
cronatron list

# Tail logs for a job
cronatron logs <job-id> --follow

# Run a job right now
cronatron trigger <job-id>

# Pause / resume
cronatron pause <job-id>
cronatron resume <job-id>

# Remove a job
cronatron unregister <job-id>

# Remove everything (with confirmation prompt)
cronatron reset
```

## State

Plists are stored in `~/Library/LaunchAgents/` and logs are written to `~/.cronatron/logs/<job-id>.log`.
