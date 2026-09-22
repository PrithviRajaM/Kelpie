"""Business logic for profile creation and validation."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from .config import settings

# The Kelpie project root is two levels up from this file
# (<kelpie_root>/NumbatAPI/app/services.py). Add it to sys.path so we can reuse
# the task runner's discovery/execution logic directly.
_KELPIE_ROOT = Path(__file__).resolve().parents[2]
if str(_KELPIE_ROOT) not in sys.path:
    sys.path.insert(0, str(_KELPIE_ROOT))

from Tasks.task_runner import (  # noqa: E402  (import after sys.path tweak)
    DiscoveredTask,
    execute_task,
    load_execution_state,
)
from .models import (
    LogDate,
    LogRun,
    TaskConfig,
    TaskDetailResponse,
    TaskSummary,
)

CONFIG_FILENAME = "TaskConfig.json"
PROMPT_FILENAME = "TaskPrompt.txt"
LOGS_DIRNAME = "Logs"
# Suffix appended to a task folder when it is "deleted". A folder carrying this
# suffix is ignored by task listings, freeing the original name for reuse.
DELETED_SUFFIX = "_DELETED"

# Log filenames look like ``task_2026-09-10.log``; capture the date part.
_LOG_FILENAME_RE = re.compile(r"^task_(\d{4}-\d{2}-\d{2})\.log$", re.IGNORECASE)
# A log line starts with ``HH:MM:SS [<run_id>] ...``; capture time and run id.
_LOG_LINE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+\[(\d+)\]")


class DomainNotAllowedError(Exception):
    """Raised when the email is not under the allowed domain."""


class ProfileNotFoundError(Exception):
    """Raised when a user's profile folder does not exist."""


class TaskNotFoundError(Exception):
    """Raised when a requested task does not exist for the user."""


class LogRunNotFoundError(Exception):
    """Raised when a requested log date/run cannot be found for a task."""


class TaskExistsError(Exception):
    """Raised when creating a task whose name is already taken."""


def validate_domain(email: str) -> str:
    """Validate that the email belongs to the allowed domain.

    Args:
        email: Full email address.

    Returns:
        The local part of the email (portion before the '@').

    Raises:
        DomainNotAllowedError: If the domain does not match the allowed domain.
    """
    local_part, _, domain = email.partition("@")
    if domain.lower() != settings.allowed_domain.lower():
        raise DomainNotAllowedError(
            f"Email must be under @{settings.allowed_domain}"
        )
    return local_part


def process_profile(email: str) -> tuple[str, str, bool]:
    """Create or detect a profile folder for the given email.

    A folder named after the full email is created under the configured
    data root. If it already exists, the user is welcomed back.

    Args:
        email: Full email address (already validated as allowed domain).

    Returns:
        A tuple of (message, local_part, created).
    """
    local_part = validate_domain(email)

    root: Path = settings.data_root
    root.mkdir(parents=True, exist_ok=True)

    profile_dir = root / email

    if profile_dir.exists():
        return f"Well come back {local_part}", local_part, False

    profile_dir.mkdir()
    return f"A profile for {local_part} is created", email, True


def _profile_dir(email: str) -> Path:
    """Return the profile directory for an email, validating the domain.

    Raises:
        DomainNotAllowedError: If the email domain is not allowed.
    """
    validate_domain(email)
    return settings.data_root / email


def _tasks_root(email: str) -> Path:
    """Return the ``Tasks`` folder under a user's profile directory."""
    return _profile_dir(email) / "Tasks"


def list_tasks(email: str) -> list[TaskSummary]:
    """List all tasks for a user.

    A task is any subfolder of ``<profile>/Tasks`` that contains a
    ``TaskConfig.json``. The ``enabled`` flag is read from that config when
    available so the UI can show status without loading full details.

    Raises:
        ProfileNotFoundError: If the user's profile folder is missing.
    """
    profile_dir = _profile_dir(email)
    if not profile_dir.exists():
        raise ProfileNotFoundError(f"No profile found for {email}")

    tasks_root = _tasks_root(email)
    if not tasks_root.exists():
        return []

    summaries: list[TaskSummary] = []
    for entry in sorted(tasks_root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        # Deleted tasks are kept on disk with a suffix but hidden from the UI.
        if entry.name.endswith(DELETED_SUFFIX):
            continue
        config_path = entry / CONFIG_FILENAME
        if not config_path.exists():
            continue

        enabled = True
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            enabled = bool(data.get("enabled", True))
        except (json.JSONDecodeError, OSError):
            # Keep listing resilient: a malformed config still shows up.
            enabled = True

        summaries.append(TaskSummary(name=entry.name, enabled=enabled))

    return summaries


def get_task(email: str, task_name: str) -> TaskDetailResponse:
    """Load a single task's config and prompt text.

    Raises:
        ProfileNotFoundError: If the user's profile folder is missing.
        TaskNotFoundError: If the task folder or its config is missing.
    """
    profile_dir = _profile_dir(email)
    if not profile_dir.exists():
        raise ProfileNotFoundError(f"No profile found for {email}")

    task_dir = _tasks_root(email) / task_name
    config_path = task_dir / CONFIG_FILENAME
    if not config_path.exists():
        raise TaskNotFoundError(f"Task '{task_name}' not found")

    config = TaskConfig.model_validate_json(
        config_path.read_text(encoding="utf-8")
    )

    prompt_path = task_dir / PROMPT_FILENAME
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""

    return TaskDetailResponse(config=config, prompt=prompt)


def save_task(
    email: str,
    config: TaskConfig,
    prompt: str,
    create_only: bool = False,
) -> tuple[str, bool]:
    """Create or update a task for the user.

    Writes ``TaskConfig.json`` and ``TaskPrompt.txt`` inside a folder named
    after the task, under ``<profile>/Tasks``.

    Args:
        create_only: When True, refuse to overwrite an existing task and raise
            ``TaskExistsError`` instead.

    Returns:
        A tuple of (task_name, created) where ``created`` is True when the
        task folder did not previously exist.

    Raises:
        ProfileNotFoundError: If the user's profile folder is missing.
        TaskExistsError: If ``create_only`` is True and the task already exists.
    """
    profile_dir = _profile_dir(email)
    if not profile_dir.exists():
        raise ProfileNotFoundError(f"No profile found for {email}")

    task_dir = _tasks_root(email) / config.name
    created = not task_dir.exists()
    if create_only and not created:
        raise TaskExistsError(f"A task named '{config.name}' already exists")
    task_dir.mkdir(parents=True, exist_ok=True)

    config_path = task_dir / CONFIG_FILENAME
    config_path.write_text(
        json.dumps(config.model_dump(mode="json"), indent=4),
        encoding="utf-8",
    )

    prompt_path = task_dir / PROMPT_FILENAME
    prompt_path.write_text(prompt, encoding="utf-8")

    return config.name, created


def delete_task(email: str, task_name: str) -> str:
    """Soft-delete a task by renaming its folder with the ``_DELETED`` suffix.

    The folder is not removed; it is renamed to ``<task_name>_DELETED`` so it is
    no longer treated as an existing task (listings skip suffixed folders) and
    the original name becomes available again.

    If a ``<task_name>_DELETED`` folder already exists from a prior deletion, it
    is hard-deleted first so the current folder can take its place.

    Args:
        email: The owning user.
        task_name: Name of the task (folder) to delete.

    Returns:
        The deleted task's name.

    Raises:
        ProfileNotFoundError: If the user's profile folder is missing.
        TaskNotFoundError: If the task folder or its config is missing.
    """
    profile_dir = _profile_dir(email)
    if not profile_dir.exists():
        raise ProfileNotFoundError(f"No profile found for {email}")

    tasks_root = _tasks_root(email)
    task_dir = tasks_root / task_name
    if task_name.endswith(DELETED_SUFFIX) or not (
        task_dir / CONFIG_FILENAME
    ).exists():
        raise TaskNotFoundError(f"Task '{task_name}' not found")

    deleted_dir = tasks_root / f"{task_name}{DELETED_SUFFIX}"
    if deleted_dir.exists():
        # A previous deletion still occupies the target name: hard-delete it so
        # the current folder can be renamed into place.
        shutil.rmtree(deleted_dir)

    task_dir.rename(deleted_dir)
    return task_name


def run_task(email: str, task_name: str) -> str:
    """Handle an immediate 'run now' request for a task.

    Builds the same ``DiscoveredTask`` shape the scheduler uses (the on-disk
    layout is identical: ``<data_root>/<email>/Tasks/<task_name>``), loads the
    shared execution state, and hands off to the task runner's ``execute_task``
    to perform the run immediately, bypassing the enabled/frequency/due checks.

    Raises:
        ProfileNotFoundError: If the user's profile folder is missing.
        TaskNotFoundError: If the task folder or its config is missing.
    """
    profile_dir = _profile_dir(email)
    if not profile_dir.exists():
        raise ProfileNotFoundError(f"No profile found for {email}")

    task_dir = _tasks_root(email) / task_name
    config_path = task_dir / CONFIG_FILENAME
    if not config_path.exists():
        raise TaskNotFoundError(f"Task '{task_name}' not found")

    # Parse the task's config so we can resolve its display name the same way
    # the scheduler does (config 'name' preferred, folder name as fallback).
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config_name = config.get("name") or task_name

    # Assemble the DiscoveredTask the runner expects. The runner resolves all
    # of its paths (prompt, logs, state key) from these fields.
    task = DiscoveredTask(
        email=email,
        name=task_name,
        config=config,
        task_dir=str(task_dir),
        profile_dir=str(profile_dir),
    )

    state = load_execution_state()
    execute_task(task, config_name, state)

    return task_name


def _logs_dir(email: str, task_name: str) -> Path:
    """Return the ``Logs`` folder inside a task's folder.

    Raises:
        ProfileNotFoundError: If the user's profile folder is missing.
        TaskNotFoundError: If the task folder is missing.
    """
    profile_dir = _profile_dir(email)
    if not profile_dir.exists():
        raise ProfileNotFoundError(f"No profile found for {email}")

    task_dir = _tasks_root(email) / task_name
    if not task_dir.exists():
        raise TaskNotFoundError(f"Task '{task_name}' not found")

    return task_dir / LOGS_DIRNAME


def _parse_line(line: str) -> tuple[str, int] | None:
    """Return ``(time, run_id)`` for a log line, or None if it doesn't match."""
    match = _LOG_LINE_RE.match(line)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def list_log_runs(email: str, task_name: str) -> list[LogDate]:
    """List log files (by date) and the runs inside each, without log lines.

    For every ``task_<date>.log`` in the task's ``Logs`` folder, the file is
    scanned once to discover distinct ``task_run_id`` values and the start time
    of each run (the timestamp of the run's first line).

    Dates are returned newest-to-oldest, and runs within a date are also sorted
    newest-to-oldest by run id.

    Returns an empty list when the ``Logs`` folder does not exist.

    Raises:
        ProfileNotFoundError: If the user's profile folder is missing.
        TaskNotFoundError: If the task folder is missing.
    """
    logs_dir = _logs_dir(email, task_name)
    if not logs_dir.exists():
        return []

    dates: list[LogDate] = []
    for entry in logs_dir.iterdir():
        if not entry.is_file():
            continue
        name_match = _LOG_FILENAME_RE.match(entry.name)
        if not name_match:
            continue

        date = name_match.group(1)
        # Preserve run discovery order but keep the first-seen start time.
        first_seen: dict[int, str] = {}
        try:
            with entry.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    parsed = _parse_line(line)
                    if parsed is None:
                        continue
                    time_str, run_id = parsed
                    if run_id not in first_seen:
                        first_seen[run_id] = time_str
        except OSError:
            # Skip unreadable files rather than failing the whole listing.
            continue

        runs = [
            LogRun(task_run_id=run_id, start_time=time_str)
            for run_id, time_str in first_seen.items()
        ]
        runs.sort(key=lambda r: r.task_run_id, reverse=True)
        dates.append(LogDate(date=date, runs=runs))

    dates.sort(key=lambda d: d.date, reverse=True)
    return dates


def get_log_run_lines(
    email: str, task_name: str, date: str, task_run_id: int
) -> list[str]:
    """Return all log lines for a single run, oldest-to-newest as written.

    Lines are returned exactly as they appear in the file (with trailing
    newlines stripped), preserving their original order.

    Raises:
        ProfileNotFoundError: If the user's profile folder is missing.
        TaskNotFoundError: If the task folder is missing.
        LogRunNotFoundError: If the log file for the date is missing or the run
            id is not present in it.
    """
    logs_dir = _logs_dir(email, task_name)
    log_path = logs_dir / f"task_{date}.log"
    if not log_path.exists():
        raise LogRunNotFoundError(f"No log file for date '{date}'")

    lines: list[str] = []
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parsed = _parse_line(line)
                if parsed is None:
                    continue
                if parsed[1] == task_run_id:
                    lines.append(line.rstrip("\n"))
    except OSError as exc:
        raise LogRunNotFoundError(
            f"Unable to read log file for date '{date}'"
        ) from exc

    if not lines:
        raise LogRunNotFoundError(
            f"Run '{task_run_id}' not found in log for date '{date}'"
        )

    return lines
