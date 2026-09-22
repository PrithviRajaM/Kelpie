"""Pydantic request/response models for the API."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class ProfileRequest(BaseModel):
    """Incoming request carrying the user's email id."""

    email: EmailStr


class ProfileResponse(BaseModel):
    """Response returned after processing a profile request."""

    message: str
    email: str
    created: bool


# Task names become folder names, so keep them filesystem-safe.
TASK_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _-]*$"


class TaskConfig(BaseModel):
    """The persisted task configuration (saved as TaskConfig.json)."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=TASK_NAME_PATTERN,
        description="Task name; also used as its folder name.",
    )
    frequency_in_minutes: int = Field(
        ...,
        ge=1,
        le=525600,  # up to a year in minutes
        description="How often the task runs, in minutes.",
    )
    enabled: bool = Field(default=True, description="Whether the task is active.")
    web_access: bool = Field(
        default=False,
        description="Whether the task is allowed to access the web.",
    )


class SaveTaskRequest(BaseModel):
    """Request to create/update a task for a given user."""

    email: EmailStr
    config: TaskConfig
    prompt: str = Field(default="", description="Multiline task prompt text.")
    create_only: bool = Field(
        default=False,
        description=(
            "When true, the request is a create: it fails if a task with the "
            "same name already exists."
        ),
    )


class TaskSummary(BaseModel):
    """Lightweight task entry used for the list view."""

    name: str
    enabled: bool


class TaskListResponse(BaseModel):
    """List of tasks belonging to a user."""

    tasks: list[TaskSummary]


class TaskDetailResponse(BaseModel):
    """Full task detail: config plus prompt text."""

    config: TaskConfig
    prompt: str


class SaveTaskResponse(BaseModel):
    """Result of saving a task."""

    message: str
    name: str
    created: bool


class RunTaskRequest(BaseModel):
    """Request to trigger an immediate run of a task."""

    email: EmailStr


class RunTaskResponse(BaseModel):
    """Acknowledgement that a run request was received."""

    message: str
    name: str


class LogRun(BaseModel):
    """A single task run within a log file.

    A run groups all log lines that share the same ``task_run_id`` (the number
    inside the second pair of square brackets on each line). ``start_time`` is
    the timestamp of the first line belonging to the run.
    """

    task_run_id: int = Field(..., description="Run id parsed from the log line.")
    start_time: str = Field(..., description="Timestamp of the run's first line.")


class LogDate(BaseModel):
    """All runs found inside one log file (one file per date)."""

    date: str = Field(..., description="Log date, parsed from the filename (YYYY-MM-DD).")
    runs: list[LogRun] = Field(
        default_factory=list,
        description="Runs in the file, sorted newest to oldest.",
    )


class LogRunListResponse(BaseModel):
    """Lightweight listing: dates and their runs (no log lines)."""

    dates: list[LogDate] = Field(default_factory=list)


class LogLinesResponse(BaseModel):
    """The raw log lines for a single run, oldest to newest as written."""

    date: str
    task_run_id: int
    lines: list[str] = Field(default_factory=list)
