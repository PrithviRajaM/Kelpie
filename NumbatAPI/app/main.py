"""FastAPI application entry point (unified under Kelpie).

Exposes endpoints for a web UI to interact with. CORS is enabled so a
browser-based UI (served from a different origin) can call the API.

Logging goes through the shared Kelpie logger so NumbatAPI activity is
recorded in the same global Kelpie log as the rest of the project.
"""
from __future__ import annotations

import os
import sys

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Make the Kelpie project root and its Logger importable regardless of the
# working directory uvicorn is launched from. main.py -> app -> NumbatAPI ->
# Kelpie (root).
_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
_LOGGER_DIR = os.path.join(_PROJECT_ROOT, "Logger")
if _LOGGER_DIR not in sys.path:
    sys.path.insert(0, _LOGGER_DIR)

import kelpie_logger as logger  # noqa: E402  (path set up above)

from .config import settings  # noqa: E402
from .models import (  # noqa: E402
    LogLinesResponse,
    LogRunListResponse,
    ProfileRequest,
    ProfileResponse,
    RunTaskRequest,
    RunTaskResponse,
    SaveTaskRequest,
    SaveTaskResponse,
    TaskDetailResponse,
    TaskListResponse,
)
from .services import (  # noqa: E402
    DomainNotAllowedError,
    LogRunNotFoundError,
    ProfileNotFoundError,
    TaskExistsError,
    TaskNotFoundError,
    get_log_run_lines,
    get_task,
    list_log_runs,
    list_tasks,
    process_profile,
    run_task,
    save_task,
)

SCRIPT_NAME = "main.py"

app = FastAPI(
    title="Numbat API",
    description="Backend API for the Numbat web UI (unified under Kelpie).",
    version="0.1.0",
)

# Allow the web UI to call the API from the browser. Tighten allow_origins
# to your UI's origin(s) in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _on_startup() -> None:
    """Log service startup and the resolved data root for traceability."""
    logger.log_info(
        SCRIPT_NAME,
        f"Numbat API starting; data_root={settings.data_root} "
        f"allowed_domain={settings.allowed_domain}",
    )


@app.get("/health")
def health() -> dict[str, str]:
    """Simple health check for the UI / load balancers."""
    return {"status": "ok", "data_root": str(settings.data_root)}


@app.post("/profile", response_model=ProfileResponse)
def create_or_get_profile(payload: ProfileRequest) -> ProfileResponse:
    """Validate an email and create/detect its profile folder.

    - Rejects emails not under the allowed domain (400).
    - Creates a folder named after the email under the data root.
    - Welcomes the user back if the folder already exists.
    """
    try:
        message, local_part, created = process_profile(str(payload.email))
    except DomainNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ProfileResponse(
        message=message,
        email=local_part,
        created=created,
    )


@app.get("/tasks", response_model=TaskListResponse)
def get_tasks(email: str) -> TaskListResponse:
    """List all tasks for a user.

    - `email`: the user whose tasks to list (query parameter).
    - 400 if the email domain is not allowed.
    - 404 if the user's profile folder does not exist.
    """
    try:
        tasks = list_tasks(email)
    except DomainNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return TaskListResponse(tasks=tasks)


@app.get("/tasks/{task_name}", response_model=TaskDetailResponse)
def get_task_detail(task_name: str, email: str) -> TaskDetailResponse:
    """Return a single task's config and prompt.

    - `email`: the owning user (query parameter).
    - 400 if the email domain is not allowed.
    - 404 if the profile or task does not exist.
    """
    try:
        return get_task(email, task_name)
    except DomainNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ProfileNotFoundError, TaskNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/tasks", response_model=SaveTaskResponse)
def create_or_update_task(payload: SaveTaskRequest) -> SaveTaskResponse:
    """Create or update a task, persisting its config and prompt.

    Writes `TaskConfig.json` and `TaskPrompt.txt` under
    `<profile>/Tasks/<task_name>`.

    - 400 if the email domain is not allowed.
    - 404 if the user's profile folder does not exist.
    """
    try:
        name, created = save_task(
            str(payload.email),
            payload.config,
            payload.prompt,
            create_only=payload.create_only,
        )
    except DomainNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TaskExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    message = (
        f"Task '{name}' created" if created else f"Task '{name}' updated"
    )
    return SaveTaskResponse(message=message, name=name, created=created)


@app.post("/tasks/{task_name}/run", response_model=RunTaskResponse)
def run_task_now(task_name: str, payload: RunTaskRequest) -> RunTaskResponse:
    """Trigger an immediate run of a task.

    For now the backend simply logs the received request; actual execution
    will be added later.

    - 400 if the email domain is not allowed.
    - 404 if the profile or task does not exist.
    """
    try:
        name = run_task(str(payload.email), task_name)
    except DomainNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ProfileNotFoundError, TaskNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return RunTaskResponse(message=f"Run requested for '{name}'", name=name)


@app.get("/tasks/{task_name}/logs", response_model=LogRunListResponse)
def get_task_log_runs(task_name: str, email: str) -> LogRunListResponse:
    """List a task's log dates and the runs within each (no log lines).

    This is the lightweight endpoint the UI calls first to render the
    collapsible date -> run tree. Log lines are fetched separately per run.

    - `email`: the owning user (query parameter).
    - 400 if the email domain is not allowed.
    - 404 if the profile or task does not exist.
    """
    try:
        dates = list_log_runs(email, task_name)
    except DomainNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ProfileNotFoundError, TaskNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return LogRunListResponse(dates=dates)


@app.get(
    "/tasks/{task_name}/logs/{date}/{task_run_id}",
    response_model=LogLinesResponse,
)
def get_task_log_run_lines(
    task_name: str, date: str, task_run_id: int, email: str
) -> LogLinesResponse:
    """Return all log lines for a single run, oldest-to-newest.

    - `email`: the owning user (query parameter).
    - `date`: the log date (YYYY-MM-DD) that identifies the log file.
    - `task_run_id`: the run whose lines to return.
    - 400 if the email domain is not allowed.
    - 404 if the profile, task, log file, or run does not exist.
    """
    try:
        lines = get_log_run_lines(email, task_name, date, task_run_id)
    except DomainNotAllowedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (
        ProfileNotFoundError,
        TaskNotFoundError,
        LogRunNotFoundError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return LogLinesResponse(date=date, task_run_id=task_run_id, lines=lines)
